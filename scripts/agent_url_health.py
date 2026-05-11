#!/usr/bin/env python3
"""
Agent URL Health — closes the GSC error backlog.

Why this exists
---------------
The Hermes DB has 466 open `gsc_errors` rows (457 of them
`index_coverage_not_indexed`). The `agent_seo_auditor` flags them but no
agent acts. There's no auto-redirect generator for typo'd 404s and no
"poke Google" loop for orphan-but-valid pages.

This agent:

1. **404 reaper** — for each `crawl_error_404` (or any `404` URL surfaced
   by the existing broken-link monitor), HEAD-checks it live. If still
   404, fuzzy-matches the slug against the live MDX inventory of that
   site to find a near-twin (≥ 0.7 SequenceMatcher ratio). Writes a 301
   into a per-site `data/redirects-auto.json` and patches `next.config.ts`
   ONCE to import that file (idempotent).
2. **Indexability prober** — calls GSC URL Inspection API for every
   `index_coverage_not_indexed` URL. If Google says `URL is on Google`
   already, marks the row resolved (often the GSC error report is just
   stale). If still not indexed and the page is a real article on the
   site, emits `seo.recrawl_requested` so the publisher re-pings IndexNow
   and the strategist optionally schedules a refresh.
3. **Status reconciliation** — marks resolved rows in `gsc_errors`,
   updates a daily summary report.

Outputs
-------
* `<site>/data/redirects-auto.json` — net-new 301 entries per site
* `<site>/next.config.ts` — patched once to import the JSON (skipped if
  already imported)
* `reports/url-health-{date}.md` — what was actioned
* Events:
    - `seo.fix_applied` per modified site (file list) → Publisher
    - `seo.recrawl_requested` per orphan-but-valid URL → Publisher
    - `url_health.run_completed` → Analytics

Usage
-----
    python3 agent_url_health.py --consume --limit 10
    python3 agent_url_health.py --daily --max-redirects 30 --max-inspections 100
    python3 agent_url_health.py --site matelas --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

from affiliate_paths import portfolio_root
from hermes_bus import (
    claim_inbox_json,
    complete_claimed_event,
    ensure_hermes_dirs,
    fail_claimed_event,
)

# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def _load_env() -> None:
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    with open(env_file) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if key not in os.environ:
                os.environ[key] = val.strip().strip("'").strip('"')

_load_env()

# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = portfolio_root()
DB_PATH = Path("~/affiliate-machine.db").expanduser()
REPORTS_DIR = BASE_DIR / "reports"
_HP_INIT = ensure_hermes_dirs()
EVENTS_BASE = _HP_INIT.base
INBOX_DIR = _HP_INIT.inbox
PROCESSING_DIR = _HP_INIT.processing
COMPLETED_DIR = _HP_INIT.completed
FAILED_DIR = _HP_INIT.failed
GSC_CREDS = Path(os.environ.get("GSC_CREDENTIALS_PATH", str(Path.home() / "gsc-credentials.json")))

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

AGENT_NAME = "agent-url-health"

# (site_slug, repo_dir, prod_host, gsc_property)
SITES: dict[str, dict[str, str]] = {
    "aspirateur": {"repo": str(BASE_DIR / "aspirateur"), "host": "www.top-aspirateur.fr",  "gsc": "sc-domain:top-aspirateur.fr"},
    "bureau":     {"repo": str(BASE_DIR / "bureau"),     "host": "www.bureau-expert.fr",   "gsc": "sc-domain:bureau-expert.fr"},
    "matelas":    {"repo": str(BASE_DIR / "matelas"),    "host": "www.matelas-expert.fr",  "gsc": "sc-domain:matelas-expert.fr"},
    "cafe":       {"repo": str(BASE_DIR / "cafe"),       "host": "www.brewmance.fr",       "gsc": "sc-domain:brewmance.fr"},
    "pixinstant": {"repo": str(BASE_DIR / "pixinstant"), "host": "www.pixinstant.com",     "gsc": "sc-domain:pixinstant.com"},
}
SITE_ID_TO_SLUG = {1: "aspirateur", 2: "bureau", 3: "matelas", 4: "cafe", 5: "pixinstant"}

REDIRECTS_RELPATH = "data/redirects-auto.json"
NEXT_CONFIG_REL = "next.config.ts"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def fetch_open_errors(error_types: list[str], limit: int = 500) -> list[sqlite3.Row]:
    qmarks = ",".join("?" * len(error_types))
    with _conn() as conn:
        return conn.execute(
            f"""
            SELECT error_id, site_id, error_type, severity, url, message, details_json, detected_at
            FROM gsc_errors
            WHERE status = 'open' AND error_type IN ({qmarks})
            ORDER BY detected_at DESC
            LIMIT ?
            """,
            (*error_types, limit),
        ).fetchall()


def mark_resolved(error_id: int, by: str, notes: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE gsc_errors SET status='resolved', resolved_at=date('now'), resolved_by=?, resolution_notes=? WHERE error_id = ?",
            (by, notes[:500], error_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Live HTTP HEAD checker (no body fetch)
# ---------------------------------------------------------------------------

def http_status(url: str, timeout: int = 15) -> Optional[int]:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "hermes-url-health/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Slug inventory + fuzzy matcher
# ---------------------------------------------------------------------------

def _site_slugs(site_slug: str) -> list[tuple[str, str]]:
    """Return [(public_path, slug)] for every MDX article in a site."""
    site = SITES.get(site_slug)
    if not site:
        return []
    repo = Path(site["repo"])
    out: list[tuple[str, str]] = []
    type_map = {"comparatifs": "comparatif", "guides": "guide", "tests": "test", "avis": "avis"}
    for mdx in repo.rglob("content/**/*.mdx"):
        try:
            parts = mdx.relative_to(repo).parts
        except ValueError:
            continue
        # Skip translation forks
        if any(p in {"en", "de", "es", "it", "uk"} for p in parts):
            # still emit but mark as locale-prefixed
            locale = next((p for p in parts if p in {"en", "de", "es", "it", "uk"}), "")
            prefix = f"/{locale}"
        else:
            prefix = ""
        article_type = ""
        for p in parts:
            if p in type_map:
                article_type = type_map[p]
                break
        slug = mdx.stem
        if article_type:
            path = f"{prefix}/{article_type}/{slug}/"
        else:
            path = f"{prefix}/{slug}/"
        out.append((path, slug))
    return out


def _slug_from_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.rstrip("/")
        return path.split("/")[-1] if path else ""
    except Exception:
        return ""


def best_slug_match(target_slug: str, candidates: list[tuple[str, str]], threshold: float = 0.70) -> Optional[tuple[str, str, float]]:
    if not target_slug:
        return None
    best: tuple[Optional[str], Optional[str], float] = (None, None, 0.0)
    for path, slug in candidates:
        ratio = SequenceMatcher(None, target_slug.lower(), slug.lower()).ratio()
        if ratio > best[2]:
            best = (path, slug, ratio)
    if best[2] >= threshold and best[0] is not None and best[1] is not None:
        return (best[0], best[1], best[2])
    return None


# ---------------------------------------------------------------------------
# Redirects file + next.config.ts patcher
# ---------------------------------------------------------------------------

NEXT_CONFIG_IMPORT_LINE = (
    "import autoRedirects from \"./data/redirects-auto.json\";"
)


def _ensure_redirects_file(repo: Path) -> Path:
    redirects_path = repo / REDIRECTS_RELPATH
    redirects_path.parent.mkdir(parents=True, exist_ok=True)
    if not redirects_path.exists():
        redirects_path.write_text("[]\n", encoding="utf-8")
    return redirects_path


def add_redirect_entry(repo: Path, source: str, destination: str) -> bool:
    """Append one (source → 301 destination) pair if not present.

    Returns True when added, False when duplicate or invalid."""
    if not source or not destination or source == destination:
        return False
    p = _ensure_redirects_file(repo)
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        data = []
    seen = {(d.get("source"), d.get("destination")) for d in data}
    if (source, destination) in seen or (source.rstrip("/"), destination.rstrip("/")) in seen:
        return False
    data.append({"source": source, "destination": destination, "permanent": True})
    # Also mirror the trailing-slash counterpart so users hit it either way.
    if not source.endswith("/"):
        data.append({"source": f"{source}/", "destination": destination, "permanent": True})
    elif source.endswith("/") and len(source) > 1:
        data.append({"source": source.rstrip("/"), "destination": destination, "permanent": True})
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def ensure_redirects_wired(repo: Path) -> bool:
    """Patch `next.config.ts` once to import + spread `redirects-auto.json`.

    Returns True when the file was modified."""
    cfg = repo / NEXT_CONFIG_REL
    if not cfg.exists():
        return False
    text = cfg.read_text(encoding="utf-8")
    if "redirects-auto.json" in text:
        return False  # already wired

    # 1. Insert import after the last existing import line.
    lines = text.split("\n")
    last_import_idx = -1
    for i, ln in enumerate(lines):
        if re.match(r"^\s*import\s+.+\s+from\s+['\"]", ln):
            last_import_idx = i
    if last_import_idx == -1:
        lines.insert(0, NEXT_CONFIG_IMPORT_LINE)
    else:
        lines.insert(last_import_idx + 1, NEXT_CONFIG_IMPORT_LINE)
    text = "\n".join(lines)

    # 2. Splice `...autoRedirects` into `async redirects() { return [ ... ] }`
    #    by replacing the very first `return [` inside the redirects() body.
    redirects_match = re.search(
        r"async\s+redirects\s*\(\s*\)\s*\{\s*return\s*\[",
        text,
    )
    if redirects_match:
        end = redirects_match.end()
        text = text[:end] + "\n      ...autoRedirects,\n" + text[end:]
    else:
        # No redirects() function — append one before the closing `};` of the
        # config object. Best-effort regex; skip silently if it doesn't match.
        replaced = re.sub(
            r"(\n};\s*\n\s*export default)",
            "\n  async redirects() {\n    return [...autoRedirects];\n  },\n};\n\nexport default",
            text,
            count=1,
        )
        if replaced != text:
            text = replaced
    cfg.write_text(text, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# 404 reaper
# ---------------------------------------------------------------------------

def reap_404s(max_redirects: int, dry_run: bool = False) -> dict[str, list[dict[str, Any]]]:
    rows = fetch_open_errors(["crawl_error_404", "crawl_error", "soft_404"], limit=500)
    print(f"  → {len(rows)} 404-class error rows to inspect")

    per_site: dict[str, list[dict[str, Any]]] = {s: [] for s in SITES}
    inv_cache: dict[str, list[tuple[str, str]]] = {}
    actions = 0

    for row in rows:
        if actions >= max_redirects:
            break
        site_slug = SITE_ID_TO_SLUG.get(row["site_id"])
        if not site_slug or site_slug not in SITES:
            continue
        url = row["url"] or ""
        if not url:
            continue
        # Confirm it's still 404 (GSC report can be days stale).
        live = http_status(url)
        if live and live != 404 and live != 410:
            mark_resolved(row["error_id"], AGENT_NAME, f"live HTTP {live} — false positive")
            per_site[site_slug].append({"error_id": row["error_id"], "url": url, "action": "false_positive", "live_status": live})
            continue
        # Try to find a near-twin live slug.
        if site_slug not in inv_cache:
            inv_cache[site_slug] = _site_slugs(site_slug)
        target = _slug_from_url(url)
        match = best_slug_match(target, inv_cache[site_slug])
        if not match:
            per_site[site_slug].append({"error_id": row["error_id"], "url": url, "action": "no_match"})
            continue
        dest_path, dest_slug, ratio = match
        # Source path = the path component of the dead URL.
        try:
            src_path = urllib.parse.urlparse(url).path
        except Exception:
            continue
        if not src_path or src_path == dest_path:
            continue
        repo = Path(SITES[site_slug]["repo"])
        if dry_run:
            per_site[site_slug].append({
                "error_id": row["error_id"], "url": url,
                "source": src_path, "destination": dest_path, "ratio": round(ratio, 2),
                "action": "would_redirect",
            })
            actions += 1
            continue
        added = add_redirect_entry(repo, src_path, dest_path)
        wired = ensure_redirects_wired(repo)
        if added:
            mark_resolved(row["error_id"], AGENT_NAME,
                          f"301 -> {dest_path} (slug ratio {ratio:.2f})")
            per_site[site_slug].append({
                "error_id": row["error_id"], "url": url,
                "source": src_path, "destination": dest_path,
                "ratio": round(ratio, 2), "action": "redirect_added",
                "wiring_patched": wired,
            })
            actions += 1
        else:
            per_site[site_slug].append({
                "error_id": row["error_id"], "url": url,
                "action": "already_present",
            })
    return per_site


# ---------------------------------------------------------------------------
# GSC URL Inspection prober (for not-indexed errors)
# ---------------------------------------------------------------------------

def _gsc_service():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        if not GSC_CREDS.exists():
            return None
        creds = service_account.Credentials.from_service_account_file(
            str(GSC_CREDS),
            scopes=["https://www.googleapis.com/auth/webmasters", "https://www.googleapis.com/auth/webmasters.readonly"],
        )
        return build("webmasters", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:
        print(f"  ⚠️  GSC service init failed: {exc}")
        return None


def probe_indexability(max_inspections: int, dry_run: bool = False) -> dict[str, list[dict[str, Any]]]:
    rows = fetch_open_errors(["index_coverage_not_indexed", "index_coverage_excluded"], limit=max_inspections)
    print(f"  → {len(rows)} not-indexed URLs to probe (cap {max_inspections})")
    service = _gsc_service()
    per_site: dict[str, list[dict[str, Any]]] = {s: [] for s in SITES}
    if service is None:
        print("  ⚠️  GSC unavailable — skipping inspection probe")
        return per_site

    for row in rows:
        site_slug = SITE_ID_TO_SLUG.get(row["site_id"])
        if not site_slug or site_slug not in SITES:
            continue
        url = row["url"] or ""
        if not url:
            continue
        gsc_property = SITES[site_slug]["gsc"]
        if dry_run:
            per_site[site_slug].append({"error_id": row["error_id"], "url": url, "action": "would_inspect"})
            continue
        try:
            result = service.urlInspection().index().inspect(
                body={"inspectionUrl": url, "siteUrl": gsc_property, "languageCode": "fr-FR"}
            ).execute()
        except Exception as exc:
            print(f"     ❌ inspect {url}: {exc}")
            per_site[site_slug].append({"error_id": row["error_id"], "url": url, "action": "inspect_failed", "error": str(exc)})
            continue

        idx = (result.get("inspectionResult") or {}).get("indexStatusResult") or {}
        verdict = idx.get("verdict")  # PASS | PARTIAL | FAIL | NEUTRAL
        coverage = idx.get("coverageState", "")
        if verdict == "PASS" and "Submitted and indexed" in coverage:
            mark_resolved(row["error_id"], AGENT_NAME, f"resolved: {coverage}")
            per_site[site_slug].append({"error_id": row["error_id"], "url": url, "action": "now_indexed", "verdict": verdict, "coverage": coverage})
            continue
        # Page exists but not indexed — emit a recrawl hint so publisher
        # IndexNow-pings it next cycle and strategist can refresh content.
        live = http_status(url)
        if live and 200 <= live < 300:
            emit_event(
                "seo.recrawl_requested",
                {
                    "site": site_slug,
                    "url": url,
                    "reason": coverage or "not_indexed",
                    "verdict": verdict,
                    "live_status": live,
                },
                priority=3,
                target_agent="agent-publisher",
            )
            per_site[site_slug].append({
                "error_id": row["error_id"], "url": url, "action": "recrawl_requested",
                "verdict": verdict, "coverage": coverage, "live_status": live,
            })
        else:
            per_site[site_slug].append({
                "error_id": row["error_id"], "url": url, "action": "skipped_dead",
                "verdict": verdict, "coverage": coverage, "live_status": live,
            })
        # be polite to GSC API
        time.sleep(0.6)
    return per_site


# ---------------------------------------------------------------------------
# Hermes events
# ---------------------------------------------------------------------------

def emit_event(event_type: str, payload: dict, priority: int = 3, target_agent: Optional[str] = None) -> dict:
    event = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "priority": priority,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_agent": AGENT_NAME,
        "routing_key": f"agent.{event_type.split('.')[0]}",
    }
    if target_agent:
        event["target_agent"] = target_agent
    fname = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{event_type.replace('.', '_')}_{event['id'][:8]}.json"
    (INBOX_DIR / fname).write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  📤 Emitted: {event_type} → {fname}")
    return event


def emit_publisher_for_redirects(per_site: dict[str, list[dict[str, Any]]]) -> int:
    n = 0
    for site_slug, actions in per_site.items():
        added = [a for a in actions if a.get("action") == "redirect_added"]
        if not added:
            continue
        files = [REDIRECTS_RELPATH]
        # If we patched next.config.ts in this run, include it too.
        if any(a.get("wiring_patched") for a in added):
            files.append(NEXT_CONFIG_REL)
        emit_event(
            "seo.fix_applied",
            {
                "site": site_slug,
                "fix_type": "auto_301",
                "files": files,
                "summary": f"Add {len(added)} 301 redirect(s) for typo'd URLs",
                "edits": added,
                "auto_merge": True,
            },
            priority=2,
            target_agent="agent-publisher",
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# Reports + drivers
# ---------------------------------------------------------------------------

def write_report(reaper: dict, prober: dict, dry_run: bool) -> Path:
    today = date.today().isoformat()
    out = REPORTS_DIR / f"url-health-{today}.md"
    lines = [
        f"# URL health actions — {today}",
        "",
        f"_Generated by `{AGENT_NAME}`. {'DRY-RUN — no files written.' if dry_run else 'Files modified in working tree.'}_",
        "",
        "## 404 reaper",
        "",
        "| Site | URL | Action | Source → Destination | Ratio |",
        "|------|-----|--------|----------------------|-------|",
    ]
    for site, actions in reaper.items():
        for a in actions:
            src = a.get("source", "")
            dst = a.get("destination", "")
            redir = f"`{src}` → `{dst}`" if src and dst else "—"
            lines.append(f"| {site} | `{a.get('url','')[:80]}` | {a.get('action','?')} | {redir} | {a.get('ratio','—')} |")
    lines.append("")
    lines.append("## Indexability prober (GSC URL Inspection)")
    lines.append("")
    lines.append("| Site | URL | Action | Verdict | Coverage |")
    lines.append("|------|-----|--------|---------|----------|")
    for site, actions in prober.items():
        for a in actions:
            lines.append(
                f"| {site} | `{a.get('url','')[:80]}` | {a.get('action','?')} "
                f"| {a.get('verdict','—')} | {a.get('coverage','—')[:50]} |"
            )
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✅ Report saved: {out}")
    return out


def run_daily(max_redirects: int = 30, max_inspections: int = 100, dry_run: bool = False) -> dict[str, Any]:
    print("→ 404 reaper…")
    reaper = reap_404s(max_redirects, dry_run=dry_run)
    print("→ Indexability prober…")
    prober = probe_indexability(max_inspections, dry_run=dry_run)
    pub_events = 0
    if not dry_run:
        pub_events = emit_publisher_for_redirects(reaper)
    out_path = write_report(reaper, prober, dry_run)
    if not dry_run:
        emit_event(
            "url_health.run_completed",
            {
                "report_path": str(out_path),
                "redirects_added": sum(1 for _, acts in reaper.items() for a in acts if a.get("action") == "redirect_added"),
                "false_positives": sum(1 for _, acts in reaper.items() for a in acts if a.get("action") == "false_positive"),
                "now_indexed": sum(1 for _, acts in prober.items() for a in acts if a.get("action") == "now_indexed"),
                "recrawl_requested": sum(1 for _, acts in prober.items() for a in acts if a.get("action") == "recrawl_requested"),
                "publisher_events": pub_events,
            },
            priority=4,
            target_agent="agent-analytics",
        )
    return {"reaper": reaper, "prober": prober, "report_path": str(out_path), "publisher_events": pub_events}


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------

CONSUMED_TYPES = {"url_health.run_requested", "seo.gsc_errors_detected"}


def _read_event(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def consume(limit: int = 10, dry_run: bool = False) -> int:
    handled = 0
    for path in sorted(INBOX_DIR.glob("*.json")):
        if handled >= limit:
            break
        event = _read_event(path)
        if not event:
            continue
        target = event.get("target_agent")
        if target and target != AGENT_NAME:
            continue
        if not target and event.get("type") not in CONSUMED_TYPES:
            continue
        proc = claim_inbox_json(path, dry_run=dry_run)
        if not proc:
            continue
        event["_file_path"] = str(proc)
        try:
            run_daily(dry_run=dry_run)
            complete_claimed_event(event, dry_run=dry_run)
        except Exception as exc:
            print(f"  ❌ {AGENT_NAME} failed on {path.name}: {exc}")
            fail_claimed_event(event, dry_run=dry_run)
        handled += 1
    print(f"[SUMMARY] {AGENT_NAME} handled {handled} event(s).")
    return handled


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=AGENT_NAME)
    parser.add_argument("--consume", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--site", type=str)
    parser.add_argument("--max-redirects", type=int, default=30)
    parser.add_argument("--max-inspections", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.consume:
        consume(limit=args.limit, dry_run=args.dry_run)
        return 0
    if args.daily or args.site:
        run_daily(max_redirects=args.max_redirects, max_inspections=args.max_inspections, dry_run=args.dry_run)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
