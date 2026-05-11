#!/usr/bin/env python3
"""
Agent Internal Linking — auto-rescues orphan pages and improves crawl depth.

Why this exists
---------------
`reports/internal-link-audit-{site}.md` (produced by `cross-site-link-audit.py`)
shows that matelas alone has 25 orphan articles (18% of inventory). Each
rescued orphan typically lifts CTR 5–10% because Google can finally crawl
through the site graph naturally. No agent currently acts on those reports.

This agent:

1. Runs `cross-site-link-audit.py` to refresh the audit data.
2. Reuses that script's parsing functions to identify per-site orphans
   (0 inbound) and the best "anchor" pages in the same category to link
   FROM (high inbound count, complementary topic).
3. For each orphan, edits the source MDX of the chosen anchors to insert
   a contextual link in a `## Voir aussi` block — appending if it
   already exists, creating it just before the final `<Disclaimer />` /
   end-of-file otherwise.
4. Caps to N edits per site per run (default 5) so we ship small,
   reviewable batches; emits `seo.fix_applied` per modified file so the
   Publisher creates PRs.
5. Also processes the cross-site link map (matelas ↔ bureau back-pain,
   pixinstant ↔ bureau decor, etc.) — adds 1 cross-site link per high-score
   theme pair per run.

Outputs
-------
* Direct MDX edits in each submodule (publisher will PR them).
* `reports/internal-linking-actions-{date}.md` — what was changed.
* Events:
    - `seo.fix_applied` per modified file → Publisher
    - `content.update_requested` for orphan with no good anchor → Strategist
    - `internal_linking.run_completed` → Analytics

Usage
-----
    python3 agent_internal_linking.py --consume --limit 10
    python3 agent_internal_linking.py --daily --dry-run
    python3 agent_internal_linking.py --site matelas --max 3
    python3 agent_internal_linking.py --refresh-audit-only
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
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
REPORTS_DIR = BASE_DIR / "reports"
_HP_INIT = ensure_hermes_dirs()
EVENTS_BASE = _HP_INIT.base
INBOX_DIR = _HP_INIT.inbox
PROCESSING_DIR = _HP_INIT.processing
COMPLETED_DIR = _HP_INIT.completed
FAILED_DIR = _HP_INIT.failed

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

AGENT_NAME = "agent-internal-linking"

SITES = ["aspirateur", "bureau", "cafe", "matelas", "pixinstant"]
DOMAINS = {
    "aspirateur": "top-aspirateur.fr",
    "bureau": "bureau-expert.fr",
    "cafe": "brewmance.fr",
    "matelas": "matelas-expert.fr",
    "pixinstant": "pixinstant.com",
}

# Heading we standardise on (matches the existing convention in
# matelas/content/avis/hypnia.mdx and other hand-curated pages).
RELATED_HEADING_FR = "## Voir aussi"
RELATED_HEADING_EN = "## See also"

# Fallback sentinels we look for to slot the block before them.
TAIL_SENTINELS = [
    "<Disclaimer",
    "<AffiliateDisclaimer",
    "{/* end-of-article */}",
]

# How many orphans to rescue per site per run.
DEFAULT_MAX_PER_SITE = 5
DEFAULT_MAX_CROSS_SITE = 3

# ---------------------------------------------------------------------------
# Reuse cross-site-link-audit.py via importlib (it has no proper module name)
# ---------------------------------------------------------------------------

def _load_audit_module():
    audit_path = SCRIPT_DIR / "cross-site-link-audit.py"
    if not audit_path.exists():
        raise FileNotFoundError(audit_path)
    spec = importlib.util.spec_from_file_location("xsite_audit", audit_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("can't load cross-site-link-audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# MDX patcher
# ---------------------------------------------------------------------------

ANCHOR_EMOJI = {
    "comparatif": "🏆",
    "comparatifs": "🏆",
    "guide": "📘",
    "guides": "📘",
    "test": "🧪",
    "tests": "🧪",
    "avis": "⭐",
}


def _build_link_line(target_type: str, target_slug: str, target_title: str, locale: str = "fr") -> str:
    emoji = ANCHOR_EMOJI.get(target_type, "🔗")
    href = f"/{target_type}/{target_slug}/"
    title = (target_title or target_slug).replace("|", "·").strip()
    return f"- {emoji} [{title}]({href})"


def _related_block_exists(body: str, locale: str) -> Optional[re.Match]:
    """Find a `## Voir aussi` (FR) / `## See also` (EN) block in the body."""
    heading = "Voir aussi" if locale != "en" else "See also"
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.MULTILINE)
    return pattern.search(body)


def insert_related_link(file_text: str, link_line: str, locale: str = "fr") -> tuple[str, str]:
    """Return (new_text, action). action ∈ {"appended", "created", "duplicate"}."""
    if link_line in file_text:
        return file_text, "duplicate"

    match = _related_block_exists(file_text, locale)
    if match:
        # Append after the heading block (find the next blank line after heading).
        start = match.end()
        rest = file_text[start:]
        # Skip past the existing bullets to the first non-bullet/non-blank line.
        lines = rest.split("\n")
        i = 0
        # First skip blank lines after heading
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        # Then skip bullets
        while i < len(lines) and lines[i].startswith("- "):
            i += 1
        # Insert before that point.
        bullets = lines[:i]
        # Avoid trailing-blank duplication
        while bullets and bullets[-1].strip() == "":
            bullets.pop()
        bullets.append(link_line)
        new_rest = "\n".join(bullets + [""] + lines[i:])
        return file_text[:start] + "\n" + new_rest, "appended"

    # Need to create a new block. Insert it before any tail sentinel; otherwise
    # before the closing of the doc.
    heading = RELATED_HEADING_FR if locale != "en" else RELATED_HEADING_EN
    block = f"\n{heading}\n\n{link_line}\n"
    insert_at = None
    for sent in TAIL_SENTINELS:
        idx = file_text.rfind(sent)
        if idx > 0:
            # Walk back to start-of-line.
            line_start = file_text.rfind("\n", 0, idx)
            insert_at = line_start if line_start > 0 else idx
            break
    if insert_at is None:
        # Append at very end (strip trailing whitespace then add block).
        return file_text.rstrip() + "\n" + block + "\n", "created"
    return file_text[:insert_at] + "\n" + block + file_text[insert_at:], "created"


# ---------------------------------------------------------------------------
# Per-site orphan rescue
# ---------------------------------------------------------------------------

def _detect_locale_from_path(path: Path) -> str:
    parts = path.parts
    for p in parts:
        if p in ("en", "de", "es", "it", "uk"):
            return p
        if p.startswith("content-") and len(p) > 8:
            return p.split("-", 1)[1]
    return "fr"


def rescue_site_orphans(
    site: str,
    audit_mod,
    max_edits: int = DEFAULT_MAX_PER_SITE,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Rescue up to `max_edits` orphan articles by linking them from the best
    same-category anchor pages."""
    actions: list[dict[str, Any]] = []
    articles = audit_mod.load_articles(site)
    if not articles:
        print(f"  ⚠️  {site}: no articles loaded")
        return actions
    by_slug = {a["slug"]: a for a in articles}
    out_links, in_links, suggestions, _ = audit_mod.audit_internal(articles, site)
    orphans = [a for a in articles if len(in_links.get(a["slug"], ())) == 0 and a["category"]]
    # Skip legal/about pages — they don't deserve internal SEO juice.
    orphans = [a for a in orphans if a["type"] not in ("pages", "")]
    orphans.sort(key=lambda a: a["slug"])
    print(f"  → {site}: {len(orphans)} orphan(s) eligible (cap {max_edits})")

    # Anchor candidates: same-category articles with HIGH inbound count.
    by_category: dict[str, list[dict[str, Any]]] = {}
    for a in articles:
        if a["category"]:
            by_category.setdefault(a["category"].lower(), []).append(a)

    edits = 0
    for orphan in orphans:
        if edits >= max_edits:
            break
        cat = (orphan["category"] or "").lower()
        peers = by_category.get(cat, [])
        # Best anchors: not the orphan itself, not already linking to it,
        # high inbound count, prefer commercial pages (comparatif > guide > test).
        type_priority = {"comparatif": 0, "comparatifs": 0, "guide": 1, "guides": 1, "test": 2, "tests": 2, "avis": 3}
        ranked = sorted(
            (p for p in peers if p["slug"] != orphan["slug"]),
            key=lambda p: (
                type_priority.get(p["type"], 5),
                -len(in_links.get(p["slug"], ())),
            ),
        )
        anchor = next(
            (p for p in ranked if orphan["slug"] not in out_links.get(p["slug"], set())),
            None,
        )
        if anchor is None:
            actions.append({"orphan": orphan["slug"], "anchor": None, "status": "no_anchor"})
            continue

        anchor_path = Path(anchor["path"])
        try:
            text = anchor_path.read_text(encoding="utf-8")
        except OSError:
            actions.append({"orphan": orphan["slug"], "anchor": anchor["slug"], "status": "read_error"})
            continue

        locale = _detect_locale_from_path(anchor_path.relative_to(BASE_DIR))
        link_line = _build_link_line(orphan["type"], orphan["slug"], orphan["title"] or orphan["slug"], locale=locale)
        new_text, action = insert_related_link(text, link_line, locale=locale)
        if action == "duplicate":
            actions.append({"orphan": orphan["slug"], "anchor": anchor["slug"], "status": "duplicate"})
            continue
        if dry_run:
            actions.append({
                "orphan": orphan["slug"], "anchor": anchor["slug"], "status": f"would_{action}",
                "anchor_path": str(anchor_path), "link": link_line,
            })
            edits += 1
            continue
        try:
            anchor_path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            actions.append({"orphan": orphan["slug"], "anchor": anchor["slug"], "status": f"write_error:{exc}"})
            continue
        actions.append({
            "orphan": orphan["slug"], "anchor": anchor["slug"], "status": action,
            "anchor_path": str(anchor_path), "link": link_line,
        })
        edits += 1
    return actions


# ---------------------------------------------------------------------------
# Cross-site link rescue (matelas ↔ bureau, pixinstant ↔ bureau, etc.)
# ---------------------------------------------------------------------------

def rescue_cross_site_links(
    audit_mod,
    max_edits: int = DEFAULT_MAX_CROSS_SITE,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    articles_by_site = {s: audit_mod.load_articles(s) for s in SITES}

    rows = audit_mod.build_cross_site_csv(
        articles_by_site,
        REPORTS_DIR / "cross-site-link-map.csv",
    )
    rows.sort(key=lambda r: -r["score"])

    edits = 0
    for r in rows:
        if edits >= max_edits:
            break
        src_site, src_slug = r["source_site"], r["source_slug"]
        tgt_site, tgt_slug = r["target_site"], r["target_slug"]
        src_articles = articles_by_site.get(src_site, [])
        src_article = next((a for a in src_articles if a["slug"] == src_slug), None)
        if not src_article:
            continue
        anchor_path = Path(src_article["path"])
        try:
            text = anchor_path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Already links to target?
        target_url = r["target_url"]
        if target_url in text or f"({target_url})" in text:
            continue
        locale = _detect_locale_from_path(anchor_path.relative_to(BASE_DIR))
        emoji = ANCHOR_EMOJI.get(r["target_type"], "🔗")
        title = (r["target_title"] or tgt_slug).replace("|", "·").strip()
        link_line = f"- {emoji} [{title}]({target_url})"
        new_text, action = insert_related_link(text, link_line, locale=locale)
        if action == "duplicate":
            continue
        if dry_run:
            actions.append({
                "from": f"{src_site}/{src_slug}",
                "to": f"{tgt_site}/{tgt_slug}",
                "status": f"would_{action}", "score": r["score"],
                "theme": r["theme"], "link": link_line,
            })
            edits += 1
            continue
        try:
            anchor_path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            actions.append({
                "from": f"{src_site}/{src_slug}", "to": f"{tgt_site}/{tgt_slug}",
                "status": f"write_error:{exc}",
            })
            continue
        actions.append({
            "from": f"{src_site}/{src_slug}", "to": f"{tgt_site}/{tgt_slug}",
            "status": action, "score": r["score"], "theme": r["theme"],
            "anchor_path": str(anchor_path), "link": link_line,
        })
        edits += 1
    return actions


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


def emit_publisher_events(site: str, actions: list[dict[str, Any]]) -> int:
    """Group the modified files of a site into one fix_applied event per file
    so the Publisher can PR them."""
    n = 0
    files: dict[str, list[dict[str, Any]]] = {}
    for a in actions:
        path = a.get("anchor_path")
        if not path or a["status"] not in ("appended", "created"):
            continue
        files.setdefault(path, []).append(a)
    for fpath, edits in files.items():
        rel = str(Path(fpath).relative_to(BASE_DIR))
        emit_event(
            "seo.fix_applied",
            {
                "site": site,
                "fix_type": "internal_linking",
                "files": [rel],
                "summary": f"Add {len(edits)} internal link(s) — orphan rescue",
                "edits": edits,
                "auto_merge": True,
            },
            priority=3,
            target_agent="agent-publisher",
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# Audit-data refresh + report
# ---------------------------------------------------------------------------

def refresh_audit() -> int:
    audit_path = SCRIPT_DIR / "cross-site-link-audit.py"
    if not audit_path.exists():
        return 1
    print("  🔄 Refreshing internal-link-audit reports…")
    proc = subprocess.run([sys.executable, str(audit_path)], capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"  ⚠️  audit refresh failed: {proc.stderr[:300]}")
    return proc.returncode


def write_run_report(per_site_actions: dict[str, list], cross_site_actions: list, dry_run: bool) -> Path:
    today = date.today().isoformat()
    out = REPORTS_DIR / f"internal-linking-actions-{today}.md"
    lines = [
        f"# Internal-linking actions — {today}",
        "",
        f"_Generated by `{AGENT_NAME}`. {'DRY-RUN — no files written.' if dry_run else 'Files modified in working tree.'}_",
        "",
        "## Per-site orphan rescue",
        "",
        "| Site | Action | Orphan | Anchor | Status |",
        "|------|--------|--------|--------|--------|",
    ]
    for site, actions in per_site_actions.items():
        for a in actions:
            lines.append(
                f"| {site} | rescue | `{a.get('orphan','?')}` "
                f"| `{a.get('anchor','—')}` | {a.get('status','?')} |"
            )
    lines.append("")
    lines.append("## Cross-site theme links")
    lines.append("")
    lines.append("| Theme | From | To | Score | Status |")
    lines.append("|-------|------|----|-------|--------|")
    for a in cross_site_actions:
        lines.append(
            f"| {a.get('theme','?')} | {a.get('from','?')} | {a.get('to','?')} "
            f"| {a.get('score','?')} | {a.get('status','?')} |"
        )
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✅ Report saved: {out}")
    return out


# ---------------------------------------------------------------------------
# Daily driver + consumer
# ---------------------------------------------------------------------------

def run_daily(
    sites: Optional[list[str]] = None,
    max_per_site: int = DEFAULT_MAX_PER_SITE,
    max_cross_site: int = DEFAULT_MAX_CROSS_SITE,
    skip_audit_refresh: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not skip_audit_refresh:
        refresh_audit()
    audit_mod = _load_audit_module()
    sites = sites or SITES

    per_site: dict[str, list[dict[str, Any]]] = {}
    publisher_events = 0
    for s in sites:
        actions = rescue_site_orphans(s, audit_mod, max_edits=max_per_site, dry_run=dry_run)
        per_site[s] = actions
        if not dry_run:
            publisher_events += emit_publisher_events(s, actions)

    cross_site = rescue_cross_site_links(audit_mod, max_edits=max_cross_site, dry_run=dry_run)
    if not dry_run:
        # Cross-site edits are grouped per file too; emit one per modified file.
        # We re-use emit_publisher_events by inferring the site from path.
        for a in cross_site:
            apath = a.get("anchor_path")
            if not apath or a["status"] not in ("appended", "created"):
                continue
            rel = Path(apath).relative_to(BASE_DIR)
            site = rel.parts[0] if rel.parts else "unknown"
            emit_event(
                "seo.fix_applied",
                {
                    "site": site,
                    "fix_type": "internal_linking_cross_site",
                    "files": [str(rel)],
                    "summary": f"Cross-site link ({a['theme']}): {a['from']} → {a['to']}",
                    "edits": [a],
                    "auto_merge": True,
                },
                priority=3,
                target_agent="agent-publisher",
            )
            publisher_events += 1

    out_path = write_run_report(per_site, cross_site, dry_run=dry_run)

    if not dry_run:
        emit_event(
            "internal_linking.run_completed",
            {
                "sites": sites,
                "report_path": str(out_path),
                "publisher_events": publisher_events,
                "orphans_rescued": sum(
                    1 for actions in per_site.values()
                    for a in actions if a["status"] in ("appended", "created")
                ),
                "cross_site_links": sum(
                    1 for a in cross_site if a["status"] in ("appended", "created")
                ),
            },
            priority=4,
            target_agent="agent-analytics",
        )
    return {
        "per_site": per_site,
        "cross_site": cross_site,
        "publisher_events": publisher_events,
        "report_path": str(out_path),
    }


CONSUMED_TYPES = {
    "internal_linking.run_requested",
    "content.published",
    "deployment.completed",
}


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
        payload = event.get("payload") or {}
        site = payload.get("site") or payload.get("site_slug")
        sites = [site] if site in SITES else SITES
        try:
            run_daily(
                sites=sites,
                max_per_site=int(payload.get("max_per_site", DEFAULT_MAX_PER_SITE)),
                max_cross_site=int(payload.get("max_cross_site", DEFAULT_MAX_CROSS_SITE)),
                skip_audit_refresh=bool(payload.get("skip_audit_refresh", False)),
                dry_run=dry_run,
            )
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
    parser.add_argument("--site", type=str, help="Restrict to one site")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_PER_SITE)
    parser.add_argument("--max-cross-site", type=int, default=DEFAULT_MAX_CROSS_SITE)
    parser.add_argument("--skip-audit-refresh", action="store_true")
    parser.add_argument("--refresh-audit-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.refresh_audit_only:
        return refresh_audit()
    if args.consume:
        consume(limit=args.limit, dry_run=args.dry_run)
        return 0
    sites = [args.site] if args.site else None
    if args.daily or args.site:
        run_daily(
            sites=sites,
            max_per_site=args.max,
            max_cross_site=args.max_cross_site,
            skip_audit_refresh=args.skip_audit_refresh,
            dry_run=args.dry_run,
        )
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
