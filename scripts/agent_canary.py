#!/usr/bin/env python3
"""
Agent Canary — traffic-loss rollback guard for the publisher.

Why this exists
---------------
Today the publisher auto-merges PRs as soon as CI is green. There's no
post-merge guard: a content rewrite that tanks GSC clicks the next day
just stays live forever. P1-23 in AUDIT_2026-05-09.md.

How it works
------------
1. Subscribes to `deployment.completed` events. For every merged event
   that touched real content (.mdx / .json / next.config.ts), it stores
   a baseline snapshot in the `canary_deployments` table:
     - merged_at, baseline_clicks_7d, baseline_impressions_7d, files

2. Once per run (--check), it queries `canary_deployments` for rows
   that are at least `--canary-window-h` hours old (default 24h) and
   not yet evaluated. For each:
     - Computes current_clicks_7d, current_impressions_7d for the
       affected URLs from `page_metrics` (now seeded by GSC import).
     - If current/baseline shows a drop ≥ click_drop_threshold (20%)
       or impression_drop_threshold (30%), emits
       `deployment.rollback_requested` targeted at agent-publisher
       with the bad SHA and a Markdown explanation.
     - Marks row as evaluated either way.

3. The publisher (already lockfile-safe after P1-10) consumes
   `deployment.rollback_requested` and reverts the offending merge
   commit on the affected submodule.

This agent never auto-rolls back — it only emits the request. The
publisher confirms the rollback happens through the normal PR/merge
flow, so a human still sees what's reverting and why.

Usage
-----
    python3 agent_canary.py --consume --limit 20      # ingest deploys
    python3 agent_canary.py --check                   # evaluate aged rows
    python3 agent_canary.py --check --window-h 6      # tighter window
    python3 agent_canary.py --backfill                # one-shot from completed/
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
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

AGENT_NAME = "agent-canary"

DEFAULT_CANARY_WINDOW_H = 24
CLICK_DROP_THRESHOLD = 0.20      # 20% drop in clicks ⇒ rollback
IMPRESSION_DROP_THRESHOLD = 0.30  # 30% drop in impressions ⇒ rollback
MIN_BASELINE_CLICKS = 10          # below this we can't trust the signal
MIN_AFFECTED_URLS = 1

PROD_HOSTS = {
    "matelas": "www.matelas-expert.fr",
    "aspirateur": "www.top-aspirateur.fr",
    "cafe": "www.brewmance.fr",
    "pixinstant": "www.pixinstant.com",
    "bureau": "www.bureau-expert.fr",
    "airpurify": "www.airpurifyhq.com",
    "safehive": "www.safehivehq.com",
    "pawhive": "www.pawhivehq.com",
}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canary_deployments (
                deploy_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                site TEXT NOT NULL,
                pr_url TEXT,
                branch TEXT,
                merged_at TIMESTAMP NOT NULL,
                files_json TEXT,
                affected_urls_json TEXT,
                baseline_clicks_7d INTEGER,
                baseline_impressions_7d INTEGER,
                evaluated_at TIMESTAMP,
                current_clicks_7d INTEGER,
                current_impressions_7d INTEGER,
                click_drop_pct REAL,
                impression_drop_pct REAL,
                verdict TEXT,
                rollback_event_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_canary_site_evaluated ON canary_deployments(site, evaluated_at)")
        conn.commit()


# ---------------------------------------------------------------------------
# URL inference (mirrors the publisher's slugs_to_canonical_urls)
# ---------------------------------------------------------------------------

CONTENT_DIR_TO_TYPE = {"comparatifs": "comparatif", "guides": "guide", "tests": "test", "avis": "avis"}
LOCALE_DIRS = {"en", "de", "es", "it", "uk"}


def affected_urls_from_files(site_slug: str, files: list[str]) -> list[str]:
    host = PROD_HOSTS.get(site_slug)
    if not host:
        return []
    out: list[str] = []
    for f in files:
        if not f.endswith(".mdx"):
            continue
        parts = f.split("/")
        slug = parts[-1].rsplit(".", 1)[0]
        atype = ""
        locale = ""
        for p in parts:
            if p in CONTENT_DIR_TO_TYPE:
                atype = CONTENT_DIR_TO_TYPE[p]
            elif p in LOCALE_DIRS:
                locale = p
        prefix = f"/{locale}" if locale else ""
        if atype:
            url = f"https://{host}{prefix}/{atype}/{slug}/"
        else:
            url = f"https://{host}{prefix}/{slug}/"
        out.append(url)
    # Always include the host root as a sanity URL
    if files:
        out.append(f"https://{host}/")
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# GSC metrics rollup (uses page_metrics seeded by agent_analytics)
# ---------------------------------------------------------------------------

def metrics_for_urls(urls: list[str], days: int = 7, end_date: Optional[date] = None) -> tuple[int, int]:
    if not urls:
        return (0, 0)
    end = end_date or date.today()
    start = end - timedelta(days=days)
    qmarks = ",".join("?" * len(urls))
    with _conn() as conn:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(clicks), 0) AS c, COALESCE(SUM(impressions), 0) AS i
            FROM page_metrics
            WHERE page_path IN ({qmarks})
              AND date BETWEEN ? AND ?
            """,
            (*urls, start.isoformat(), end.isoformat()),
        ).fetchone()
    return (int(row["c"] or 0), int(row["i"] or 0))


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
# Ingest deployment.completed events
# ---------------------------------------------------------------------------

def _read_event(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def ingest_deployment(event: dict, dry_run: bool = False) -> Optional[dict[str, Any]]:
    payload = event.get("payload", {}) or {}
    if not payload.get("merged"):
        return None
    site = payload.get("site")
    files = payload.get("files") or []
    if not site or not files:
        return None
    urls = affected_urls_from_files(site, files)
    if len(urls) < MIN_AFFECTED_URLS:
        return None
    ensure_schema()
    bclicks, bimpr = metrics_for_urls(urls, days=7, end_date=date.today() - timedelta(days=1))
    record = {
        "event_id": event.get("id"),
        "site": site,
        "pr_url": payload.get("pr_url"),
        "branch": payload.get("branch"),
        "merged_at": event.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "files": files,
        "urls": urls,
        "baseline_clicks_7d": bclicks,
        "baseline_impressions_7d": bimpr,
    }
    if dry_run:
        print(f"  [DRY-RUN] Would store baseline: {site} ({len(urls)} URLs, baseline_clicks={bclicks})")
        return record
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO canary_deployments
            (event_id, site, pr_url, branch, merged_at, files_json, affected_urls_json,
             baseline_clicks_7d, baseline_impressions_7d)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                record["event_id"], site, record["pr_url"], record["branch"], record["merged_at"],
                json.dumps(files, ensure_ascii=False),
                json.dumps(urls, ensure_ascii=False),
                bclicks, bimpr,
            ),
        )
        conn.commit()
    print(f"  📊 Baselined deployment {event.get('id')[:8]} for {site} ({len(urls)} URLs, baseline_clicks={bclicks})")
    return record


# ---------------------------------------------------------------------------
# Evaluate aged baselines + emit rollbacks
# ---------------------------------------------------------------------------

def evaluate(window_h: int = DEFAULT_CANARY_WINDOW_H, dry_run: bool = False) -> dict[str, Any]:
    ensure_schema()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_h)).isoformat().replace("+00:00", "Z")
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM canary_deployments
            WHERE evaluated_at IS NULL
              AND merged_at <= ?
            ORDER BY merged_at ASC
            """,
            (cutoff,),
        ).fetchall()
    print(f"  → {len(rows)} deployment(s) past canary window ({window_h}h)")
    results: list[dict[str, Any]] = []
    rollbacks_emitted = 0

    for r in rows:
        urls = json.loads(r["affected_urls_json"] or "[]")
        files = json.loads(r["files_json"] or "[]")
        cclicks, cimpr = metrics_for_urls(urls, days=7)
        bclicks = int(r["baseline_clicks_7d"] or 0)
        bimpr = int(r["baseline_impressions_7d"] or 0)
        click_drop = (1 - (cclicks / bclicks)) if bclicks else 0.0
        impr_drop = (1 - (cimpr / bimpr)) if bimpr else 0.0

        verdict = "ok"
        if bclicks >= MIN_BASELINE_CLICKS and (
            click_drop >= CLICK_DROP_THRESHOLD or impr_drop >= IMPRESSION_DROP_THRESHOLD
        ):
            verdict = "regression"
        elif bclicks < MIN_BASELINE_CLICKS:
            verdict = "insufficient_baseline"

        rollback_id = ""
        if verdict == "regression" and not dry_run:
            ev = emit_event(
                "deployment.rollback_requested",
                {
                    "site": r["site"],
                    "pr_url": r["pr_url"],
                    "branch": r["branch"],
                    "merged_at": r["merged_at"],
                    "files": files,
                    "affected_urls": urls,
                    "baseline_clicks_7d": bclicks,
                    "current_clicks_7d": cclicks,
                    "click_drop_pct": round(click_drop * 100, 2),
                    "baseline_impressions_7d": bimpr,
                    "current_impressions_7d": cimpr,
                    "impression_drop_pct": round(impr_drop * 100, 2),
                    "reason": "canary_regression",
                },
                priority=1,
                target_agent="agent-publisher",
            )
            rollback_id = ev["id"]
            rollbacks_emitted += 1

        if not dry_run:
            with _conn() as conn:
                conn.execute(
                    """
                    UPDATE canary_deployments
                    SET evaluated_at = ?, current_clicks_7d = ?, current_impressions_7d = ?,
                        click_drop_pct = ?, impression_drop_pct = ?, verdict = ?, rollback_event_id = ?
                    WHERE deploy_id = ?
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        cclicks, cimpr,
                        round(click_drop, 4), round(impr_drop, 4),
                        verdict, rollback_id, r["deploy_id"],
                    ),
                )
                conn.commit()
        results.append({
            "deploy_id": r["deploy_id"], "site": r["site"], "verdict": verdict,
            "baseline_clicks_7d": bclicks, "current_clicks_7d": cclicks,
            "click_drop_pct": round(click_drop * 100, 2),
            "impression_drop_pct": round(impr_drop * 100, 2),
            "rollback_event_id": rollback_id,
            "urls": urls[:5],
        })

    out_path = REPORTS_DIR / f"canary-{date.today().isoformat()}.md"
    lines = [
        f"# Canary evaluation — {date.today().isoformat()}",
        "",
        f"_Window: {window_h}h. Threshold: clicks ≥ {int(CLICK_DROP_THRESHOLD*100)}% drop OR impressions ≥ {int(IMPRESSION_DROP_THRESHOLD*100)}% drop._",
        "",
        f"**Deployments evaluated:** {len(results)}    **Rollbacks emitted:** {rollbacks_emitted}",
        "",
        "| Site | Verdict | Baseline clicks | Current clicks | Click Δ | Impr Δ | Rollback evt |",
        "|------|---------|-----------------|----------------|--------|--------|--------------|",
    ]
    for r in results:
        lines.append(
            f"| {r['site']} | {r['verdict']} | {r['baseline_clicks_7d']} | {r['current_clicks_7d']} "
            f"| {r['click_drop_pct']}% | {r['impression_drop_pct']}% "
            f"| `{r['rollback_event_id'] or '—'}` |"
        )
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✅ Report saved: {out_path}")
    if not dry_run:
        emit_event(
            "canary.run_completed",
            {"report_path": str(out_path), "evaluated": len(results), "rollbacks_emitted": rollbacks_emitted},
            priority=4,
            target_agent="agent-analytics",
        )
    return {"evaluated": len(results), "rollbacks_emitted": rollbacks_emitted, "report": str(out_path), "rows": results}


# ---------------------------------------------------------------------------
# Consumer (deployment.completed)
# ---------------------------------------------------------------------------

def _move(src: Path, dst_dir: Path) -> Optional[Path]:
    dst = dst_dir / src.name
    try:
        shutil.move(str(src), str(dst))
        return dst
    except Exception:
        return None


def consume(limit: int = 20, dry_run: bool = False) -> int:
    handled = 0
    for path in sorted(INBOX_DIR.glob("*.json")):
        if handled >= limit:
            break
        event = _read_event(path)
        if not event:
            continue
        # We are interested in deployment.completed regardless of target.
        if event.get("type") != "deployment.completed":
            continue
        proc = _move(path, PROCESSING_DIR) if not dry_run else path
        if not proc:
            continue
        try:
            ingest_deployment(event, dry_run=dry_run)
            if not dry_run:
                _move(proc, COMPLETED_DIR)
        except Exception as exc:
            print(f"  ❌ {AGENT_NAME} failed on {path.name}: {exc}")
            if not dry_run:
                _move(proc, FAILED_DIR)
        handled += 1
    print(f"[SUMMARY] {AGENT_NAME} ingested {handled} deployment event(s).")
    return handled


def backfill_from_completed(dry_run: bool = False, max_age_days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    n = 0
    for path in sorted(COMPLETED_DIR.glob("*deployment_completed*.json")):
        if path.stat().st_mtime < cutoff.timestamp():
            continue
        event = _read_event(path)
        if not event or event.get("type") != "deployment.completed":
            continue
        ingest_deployment(event, dry_run=dry_run)
        n += 1
    print(f"[SUMMARY] backfill ingested {n} historical deployment(s).")
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description=AGENT_NAME)
    parser.add_argument("--consume", action="store_true", help="Ingest deployment.completed events from inbox")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--check", action="store_true", help="Evaluate aged baselines + emit rollbacks if needed")
    parser.add_argument("--window-h", type=int, default=DEFAULT_CANARY_WINDOW_H)
    parser.add_argument("--backfill", action="store_true", help="Re-ingest historical deployment.completed from completed/")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.backfill:
        backfill_from_completed(dry_run=args.dry_run)
    if args.consume:
        consume(limit=args.limit, dry_run=args.dry_run)
    if args.check:
        evaluate(window_h=args.window_h, dry_run=args.dry_run)
    if not (args.consume or args.check or args.backfill):
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
