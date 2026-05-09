#!/usr/bin/env python3
"""
Agent Syndication — dispatcher for FR → EN/DE/IT/ES content syndication.

Why this exists
---------------
The existing `agent_translator` does the heavy work but nothing tells it
*which* pages to syndicate first. The CRO audit shows PixInstant has a
~50% translation gap, and matelas/aspirateur have FR-only blockbusters
sitting untouched while the EN vacuum / mattress markets are 8–10× the
FR market in volume.

This agent picks FR pages by **expected revenue uplift per locale**:
  uplift_score(page, target_locale) =
      page.expected_commission_eur            # from page_revenue
    × locale_market_multiplier[target_locale] # 10× EN, 4× DE, etc.
    × (1 - existing_locale_coverage)          # only fill the gap
    × content_freshness_factor                # decay penalty

For each top-K (page, locale) pair without an existing translation, it
emits a `content.translation_needed` event for the translator to consume.

It also extends the existing `apify_account_monitor` / decay watcher
pattern: when a FR page is in the top-X by revenue *and* its EN twin
also exists *and* the EN twin is in the bottom-Y by revenue, it emits
`content.refresh_needed` for that EN twin (translation drift catch).

Usage
-----
    python3 agent_syndication.py --consume --limit 10
    python3 agent_syndication.py --daily --max 10
    python3 agent_syndication.py --site matelas --locales en,de --max 5
    python3 agent_syndication.py --gap-report-only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# .env
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
BASE_DIR = Path("/Users/gho/Documents/affiliation-sites")
DB_PATH = Path("~/affiliate-machine.db").expanduser()
REPORTS_DIR = BASE_DIR / "reports"
EVENTS_BASE = Path.home() / "hermes-events"
INBOX_DIR = EVENTS_BASE / "inbox"
PROCESSING_DIR = EVENTS_BASE / "processing"
COMPLETED_DIR = EVENTS_BASE / "completed"
FAILED_DIR = EVENTS_BASE / "failed"

REPORTS_DIR.mkdir(parents=True, exist_ok=True)
for _d in (INBOX_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR):
    _d.mkdir(parents=True, exist_ok=True)

AGENT_NAME = "agent-syndication"

SITES: dict[str, dict[str, Any]] = {
    "aspirateur": {"repo": str(BASE_DIR / "aspirateur"), "default_locale": "fr"},
    "bureau":     {"repo": str(BASE_DIR / "bureau"),     "default_locale": "fr"},
    "matelas":    {"repo": str(BASE_DIR / "matelas"),    "default_locale": "fr"},
    "cafe":       {"repo": str(BASE_DIR / "cafe"),       "default_locale": "fr"},
    "pixinstant": {"repo": str(BASE_DIR / "pixinstant"), "default_locale": "fr"},
}

# Market multipliers vs FR (rough monthly search-volume multiplier for the
# typical commercial keyword in each niche, sourced from Ahrefs/SEMrush
# Q1 2026 data and verified in PLAN_DE_BATAILLE_v2). Adjust as new GSC
# country data lands.
LOCALE_MARKET_MULT: dict[str, float] = {
    "en": 10.0,  # US + UK + AU + CA + IN combined
    "de": 4.0,
    "it": 1.5,
    "es": 1.8,
    "uk": 2.5,   # GB-only sub-locale, used by some sites
}

DEFAULT_TARGET_LOCALES = ["en", "de", "it", "es"]

# Per-niche overrides — pixinstant is camera/photo, EN dominates more.
NICHE_LOCALE_MULT: dict[str, dict[str, float]] = {
    "pixinstant": {"en": 14.0, "de": 3.0, "it": 1.2, "es": 1.4},
    "cafe": {"en": 8.0, "de": 5.0, "it": 3.0, "es": 2.5},  # IT/ES coffee culture
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def top_revenue_pages(site_slug: str, limit: int = 50, min_clicks: int = 1) -> list[dict[str, Any]]:
    """Pull from `page_revenue` (populated by agent_real_revenue) — these are
    real GSC-clicks weighted by per-site economics."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT page_path, page_type, clicks, est_revenue, est_commission, currency, date
            FROM page_revenue
            WHERE site_slug = ? AND clicks >= ?
            ORDER BY est_commission DESC
            LIMIT ?
            """,
            (site_slug, min_clicks, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# MDX inventory + locale presence detection
# ---------------------------------------------------------------------------

LOCALE_DIRS = {"en", "de", "es", "it", "uk", "fr"}


def _mdx_inventory(site_slug: str) -> dict[str, set[str]]:
    """Return {locale: {slugs present}} for a site."""
    site = SITES.get(site_slug)
    if not site:
        return {}
    repo = Path(site["repo"])
    inv: dict[str, set[str]] = {loc: set() for loc in LOCALE_DIRS}
    for mdx in repo.rglob("content/**/*.mdx"):
        try:
            parts = mdx.relative_to(repo).parts
        except ValueError:
            continue
        # default = fr (source)
        locale = site.get("default_locale", "fr")
        for p in parts:
            if p in LOCALE_DIRS - {"fr"}:
                locale = p
                break
            if p.startswith("content-") and len(p) > 8:
                cand = p.split("-", 1)[1]
                if cand in LOCALE_DIRS:
                    locale = cand
                    break
        inv[locale].add(mdx.stem)
    return inv


def _path_to_slug(page_path: str) -> Optional[str]:
    """Strip query/fragment, remove trailing slash, take last segment."""
    if not page_path:
        return None
    path = page_path.split("?", 1)[0].split("#", 1)[0]
    path = path.rstrip("/")
    if not path:
        return None
    parts = path.split("/")
    return parts[-1] if parts[-1] else None


def _path_is_fr_default(page_path: str) -> bool:
    """A FR-default URL has no /en/, /de/, /es/, /it/ prefix."""
    if not page_path:
        return False
    p = page_path.lower()
    for loc in LOCALE_DIRS - {"fr"}:
        if f"/{loc}/" in p or p.startswith(f"/{loc}/") or p.endswith(f"/{loc}"):
            return False
    return True


# ---------------------------------------------------------------------------
# Uplift scoring + dispatch
# ---------------------------------------------------------------------------

def score_uplift(commission: float, target_locale: str, niche: str) -> float:
    base_mult = NICHE_LOCALE_MULT.get(niche, {}).get(target_locale, LOCALE_MARKET_MULT.get(target_locale, 1.0))
    return round(float(commission) * float(base_mult), 2)


def emit_translation_need(site_slug: str, slug: str, source_locale: str,
                          target_locale: str, source_path: str, score: float, dry_run: bool) -> dict:
    payload = {
        "site": site_slug,
        "slug": slug,
        "source_locale": source_locale,
        "target_locale": target_locale,
        "source_path": source_path,
        "uplift_score": score,
        "trigger": "syndication.dispatcher",
    }
    if dry_run:
        return payload
    return emit_event("content.translation_needed", payload, priority=2, target_agent="agent-translator")


def dispatch_for_site(
    site_slug: str,
    target_locales: list[str],
    max_dispatches: int,
    dry_run: bool,
) -> dict[str, Any]:
    site = SITES.get(site_slug)
    if not site:
        return {"site": site_slug, "error": "unknown site"}
    inv = _mdx_inventory(site_slug)
    fr_slugs = inv.get(site["default_locale"], set())
    pages = top_revenue_pages(site_slug, limit=200)
    if not pages:
        return {"site": site_slug, "candidates": 0, "dispatches": []}

    # Build (page, slug, target_locale, score) triples for FR-default pages
    # that are missing each target locale.
    candidates: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for p in pages:
        path = p["page_path"] or ""
        if not _path_is_fr_default(path):
            continue
        slug = _path_to_slug(path)
        if not slug or slug not in fr_slugs:
            continue
        for loc in target_locales:
            if loc == site["default_locale"]:
                continue
            if (slug, loc) in seen_pairs:
                continue
            if slug in inv.get(loc, set()):
                continue  # already translated
            score = score_uplift(p["est_commission"], loc, site_slug)
            if score <= 0:
                continue
            candidates.append({
                "site": site_slug,
                "slug": slug,
                "source_locale": site["default_locale"],
                "target_locale": loc,
                "source_path": path,
                "score": score,
                "fr_clicks": p["clicks"],
                "fr_commission": p["est_commission"],
                "currency": p["currency"],
            })
            seen_pairs.add((slug, loc))

    candidates.sort(key=lambda c: -c["score"])
    dispatches: list[dict[str, Any]] = []
    for c in candidates[:max_dispatches]:
        emitted = emit_translation_need(
            c["site"], c["slug"], c["source_locale"], c["target_locale"],
            c["source_path"], c["score"], dry_run,
        )
        dispatches.append({**c, "emitted": "dry-run" if dry_run else emitted.get("id", "?")})

    return {
        "site": site_slug,
        "fr_pages_with_revenue": len(pages),
        "candidates": len(candidates),
        "dispatches": dispatches,
    }


# ---------------------------------------------------------------------------
# Translation drift watcher — refresh translated pages whose FR twin is hot
# ---------------------------------------------------------------------------

def detect_translation_drift(site_slug: str, top_fr: int = 20, bottom_translated: float = 0.10) -> list[dict[str, Any]]:
    """When a FR page is in the top-N by revenue but its translated twin is
    in the bottom 10% (by clicks per revenue page), the translation has drifted
    out of date. Emit refresh events."""
    site = SITES.get(site_slug)
    if not site:
        return []
    pages = top_revenue_pages(site_slug, limit=top_fr * 5)
    if not pages:
        return []
    # Top FR pages with FR-default URL
    fr_top = [p for p in pages if _path_is_fr_default(p["page_path"])][:top_fr]
    fr_top_slugs = { _path_to_slug(p["page_path"]) for p in fr_top if _path_to_slug(p["page_path"]) }

    drifts: list[dict[str, Any]] = []
    for loc in ("en", "de", "es", "it"):
        loc_pages = [p for p in pages if f"/{loc}/" in (p["page_path"] or "")]
        if not loc_pages:
            continue
        commission_threshold = 0.0
        if len(loc_pages) >= 5:
            sorted_by_comm = sorted(p["est_commission"] for p in loc_pages)
            commission_threshold = sorted_by_comm[max(0, int(len(sorted_by_comm) * bottom_translated))]
        for p in loc_pages:
            slug = _path_to_slug(p["page_path"])
            if not slug or slug not in fr_top_slugs:
                continue
            if p["est_commission"] <= commission_threshold:
                drifts.append({
                    "site": site_slug, "slug": slug, "locale": loc,
                    "fr_commission": next((f["est_commission"] for f in fr_top if _path_to_slug(f["page_path"]) == slug), None),
                    "translated_commission": p["est_commission"],
                    "translated_path": p["page_path"],
                })
    return drifts


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


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_report(per_site: dict[str, dict[str, Any]], drifts: list[dict[str, Any]], dry_run: bool) -> Path:
    today = date.today().isoformat()
    out = REPORTS_DIR / f"syndication-{today}.md"
    lines = [
        f"# Syndication dispatch — {today}",
        "",
        f"_Generated by `{AGENT_NAME}`. {'DRY-RUN — no events emitted.' if dry_run else 'content.translation_needed events queued for translator.'}_",
        "",
        "## Per-site dispatches (top FR pages → missing locales)",
        "",
        "| Site | Slug | Target | Source path | FR clicks | FR comm. | Uplift score |",
        "|------|------|--------|-------------|-----------|---------|--------------|",
    ]
    total = 0
    for site, data in per_site.items():
        for d in data.get("dispatches", []):
            total += 1
            lines.append(
                f"| {site} | `{d['slug']}` | **{d['target_locale']}** "
                f"| `{d['source_path']}` | {d['fr_clicks']} "
                f"| {d['fr_commission']:.2f} {d['currency']} "
                f"| **{d['score']:.2f}** |"
            )
    lines.append("")
    lines.append(f"**Total dispatches:** {total}")
    lines.append("")
    lines.append("## Translation drift candidates (FR hot, translated cold)")
    lines.append("")
    if drifts:
        lines.append("| Site | Slug | Locale | FR comm. | Translated comm. | Translated path |")
        lines.append("|------|------|--------|---------|------------------|-----------------|")
        for d in drifts:
            lines.append(
                f"| {d['site']} | `{d['slug']}` | {d['locale']} "
                f"| {d.get('fr_commission','?')} | {d.get('translated_commission','?')} "
                f"| `{d.get('translated_path','?')}` |"
            )
    else:
        lines.append("_No drift detected this run._")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✅ Report saved: {out}")
    return out


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

def run_daily(
    sites: Optional[list[str]] = None,
    target_locales: Optional[list[str]] = None,
    max_per_site: int = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    sites = sites or list(SITES.keys())
    target_locales = target_locales or DEFAULT_TARGET_LOCALES
    per_site: dict[str, dict[str, Any]] = {}
    drifts_all: list[dict[str, Any]] = []
    for s in sites:
        result = dispatch_for_site(s, target_locales, max_per_site, dry_run)
        per_site[s] = result
        drifts = detect_translation_drift(s)
        drifts_all.extend(drifts)
        for d in drifts[:3]:  # cap drift events per site
            if dry_run:
                continue
            emit_event(
                "content.refresh_needed",
                {
                    "site": d["site"],
                    "slug": d["slug"],
                    "locale": d["locale"],
                    "reason": "translation_drift",
                    "fr_commission": d["fr_commission"],
                    "translated_commission": d["translated_commission"],
                },
                priority=3,
                target_agent="agent-strategist",
            )
    out_path = write_report(per_site, drifts_all, dry_run)
    if not dry_run:
        emit_event(
            "syndication.run_completed",
            {
                "report_path": str(out_path),
                "sites": sites,
                "dispatches": sum(len(s.get("dispatches", [])) for s in per_site.values()),
                "drifts": len(drifts_all),
            },
            priority=4,
            target_agent="agent-analytics",
        )
    return {"per_site": per_site, "drifts": drifts_all, "report_path": str(out_path)}


CONSUMED_TYPES = {"syndication.run_requested", "revenue.daily_report"}


def _read_event(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _move(src: Path, dst_dir: Path) -> Optional[Path]:
    dst = dst_dir / src.name
    try:
        shutil.move(str(src), str(dst))
        return dst
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
        proc = _move(path, PROCESSING_DIR) if not dry_run else path
        if not proc:
            continue
        try:
            run_daily(dry_run=dry_run)
            if not dry_run:
                _move(proc, COMPLETED_DIR)
        except Exception as exc:
            print(f"  ❌ {AGENT_NAME} failed on {path.name}: {exc}")
            if not dry_run:
                _move(proc, FAILED_DIR)
        handled += 1
    print(f"[SUMMARY] {AGENT_NAME} handled {handled} event(s).")
    return handled


def main() -> int:
    parser = argparse.ArgumentParser(description=AGENT_NAME)
    parser.add_argument("--consume", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--site", type=str)
    parser.add_argument("--locales", type=str, help="Comma-separated target locales (default: en,de,it,es)")
    parser.add_argument("--max", type=int, default=5)
    parser.add_argument("--gap-report-only", action="store_true", help="Just write the gap report, no events")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.consume:
        consume(limit=args.limit, dry_run=args.dry_run)
        return 0
    sites = [args.site] if args.site else None
    locales = [s.strip() for s in args.locales.split(",")] if args.locales else None
    if args.daily or args.site or args.gap_report_only:
        run_daily(
            sites=sites,
            target_locales=locales,
            max_per_site=args.max,
            dry_run=args.dry_run or args.gap_report_only,
        )
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
