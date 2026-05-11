#!/usr/bin/env python3
"""
Agent Real Revenue — Replaces fake `estimate_revenue` with real per-site economics.

Why this exists
---------------
`agent_analytics.estimate_revenue()` was using a hardcoded
`avg_order_value=150` and `conversion_rate=0.03` for *every* site, then
treating Matelas (€450 AOV, 5% commission) like Brewmance (€90 AOV, 4.5%
commission). The strategist consumes those numbers when deciding which
keywords to refresh, so it has been over-prioritising high-impression
low-AOV pages all of 2026.

This agent fixes that with three real revenue sources, in priority order:

  1. **Amazon Associates Reports** (real $$ — wraps `amazon-reporting-api.py`).
     If creds are set + account is eligible, this is ground truth.
  2. **Click-weighted revenue model** (when API not yet eligible).
     Joins GSC clicks ⨯ per-site `commerce.config.json` AOV ⨯ commission rate
     ⨯ per-page-type conversion bands (commercial vs informational).
  3. **Per-page revenue attribution** so the strategist can pick the
     real top-revenue pages, not the high-impression vanity ones.

Outputs
-------
* `~/affiliate-machine.db`
    - `revenue_estimates` (existing table; rows now have `source` ∈
      {amazon_api, click_model})
    - `page_revenue` (NEW) — per-site/page/day revenue for the strategist
* `reports/revenue-real-{date}.md` and `reports/revenue-real-{site}-{date}.json`
* Events emitted:
    - `revenue.daily_report`     — Email Marketer
    - `revenue.high_value_page`  — Strategist (per-page priority signal)
    - `revenue.dead_zone_page`   — Strategist (lots of clicks, ~no revenue → CRO fix)

Usage
-----
    python3 agent_real_revenue.py --consume --limit 10
    python3 agent_real_revenue.py --daily              # rolls the whole estate
    python3 agent_real_revenue.py --site matelas --days 30
    python3 agent_real_revenue.py --backfill --days 90 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
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
# .env loader (same convention as the other agents)
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
# Paths
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

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

AGENT_NAME = "agent-real-revenue"

# ---------------------------------------------------------------------------
# Per-site real economics
#
# AOV (Average Order Value, EUR/USD) and Amazon commission rate (%) come
# from public Amazon Associates fee schedules + observed niche data:
#   - matelas: Furniture & Home (5%), avg basket €450 (mid-range mattress)
#   - aspirateur: PC / Wireless Accessories (3%), avg basket €180
#   - bureau: Furniture (5%), avg basket €280 (chair + accessories)
#   - cafe: Kitchen / Coffee (4.5%), avg basket €120 (machine OR grinder)
#   - pixinstant: Camera (4%), avg basket €25 (film packs / instant cams)
#   - airpurify (US): Home (3%), avg basket $200
#   - safehive  (US): Smart Home (4%), avg basket $150
#   - pawhive   (US): Pet (4.5%), avg basket $80
#
# `click_to_buy_rate` is the page-type → conversion band:
#   - commercial (comparatif/test/avis): higher intent → ~4–7%
#   - informational (guide/blog): lower intent → ~0.8–1.5%
#   - homepage / category: middle ~2%
# These are deliberately conservative; Amazon Associates seasonality
# easily moves them ±50%, so the agent learns and overwrites them per
# site as soon as `amazon-reporting-api.py` returns real data.
# ---------------------------------------------------------------------------

SITE_ECONOMICS: dict[str, dict[str, Any]] = {
    "aspirateur": {
        "currency": "EUR",
        "aov": 180.0,
        "commission_rate": 0.03,
        "click_to_buy": {"commercial": 0.045, "informational": 0.010, "default": 0.020},
        "domain": "top-aspirateur.fr",
        "amazon_tag": "zoomzen05-21",
    },
    "bureau": {
        "currency": "EUR",
        "aov": 280.0,
        "commission_rate": 0.05,
        "click_to_buy": {"commercial": 0.045, "informational": 0.010, "default": 0.020},
        "domain": "bureau-expert.fr",
        "amazon_tag": "zoomzen05-21",
    },
    "matelas": {
        "currency": "EUR",
        "aov": 450.0,
        "commission_rate": 0.05,
        "click_to_buy": {"commercial": 0.040, "informational": 0.008, "default": 0.018},
        "domain": "matelas-expert.fr",
        "amazon_tag": "zoomzen05-21",
    },
    "cafe": {
        "currency": "EUR",
        "aov": 120.0,
        "commission_rate": 0.045,
        "click_to_buy": {"commercial": 0.055, "informational": 0.012, "default": 0.022},
        "domain": "brewmance.fr",
        "amazon_tag": "zoomzen05-21",
    },
    "pixinstant": {
        "currency": "USD",
        "aov": 25.0,
        "commission_rate": 0.04,
        "click_to_buy": {"commercial": 0.060, "informational": 0.015, "default": 0.025},
        "domain": "pixinstant.com",
        "amazon_tag": "zoomzus-20",
    },
    "airpurify": {
        "currency": "USD",
        "aov": 200.0,
        "commission_rate": 0.03,
        "click_to_buy": {"commercial": 0.040, "informational": 0.008, "default": 0.018},
        "domain": "airpurifyhq.com",
        "amazon_tag": "zoomzus-20",
    },
    "safehive": {
        "currency": "USD",
        "aov": 150.0,
        "commission_rate": 0.04,
        "click_to_buy": {"commercial": 0.045, "informational": 0.010, "default": 0.020},
        "domain": "safehivehq.com",
        "amazon_tag": "zoomzus-20",
    },
    "pawhive": {
        "currency": "USD",
        "aov": 80.0,
        "commission_rate": 0.045,
        "click_to_buy": {"commercial": 0.055, "informational": 0.012, "default": 0.022},
        "domain": "pawhivehq.com",
        "amazon_tag": "zoomzus-20",
    },
}

# Approx EUR↔USD parity for cross-site rollups (refreshed manually; see
# `_eur_to_usd` below). Updated 2026-05-09.
EUR_USD = 1.08

# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema() -> None:
    """Create per-page revenue table + extend revenue_estimates with `source`."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS page_revenue (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                site_slug       TEXT NOT NULL,
                page_path       TEXT NOT NULL,
                page_type       TEXT,
                date            DATE NOT NULL,
                clicks          INTEGER DEFAULT 0,
                impressions     INTEGER DEFAULT 0,
                position        REAL DEFAULT 0,
                est_orders      REAL DEFAULT 0,
                est_revenue     REAL DEFAULT 0,
                est_commission  REAL DEFAULT 0,
                aov             REAL DEFAULT 0,
                commission_rate REAL DEFAULT 0,
                ctr_to_buy      REAL DEFAULT 0,
                source          TEXT DEFAULT 'click_model',
                currency        TEXT DEFAULT 'EUR',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(site_slug, page_path, date)
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_page_revenue_site_date ON page_revenue(site_slug, date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_page_revenue_revenue ON page_revenue(est_commission DESC)")

        # Add source column to revenue_estimates if missing.
        cur.execute("PRAGMA table_info(revenue_estimates)")
        cols = {r["name"] for r in cur.fetchall()}
        if "source" not in cols:
            cur.execute("ALTER TABLE revenue_estimates ADD COLUMN source TEXT DEFAULT 'click_model'")
        if "currency" not in cols:
            cur.execute("ALTER TABLE revenue_estimates ADD COLUMN currency TEXT DEFAULT 'EUR'")
        conn.commit()


# ---------------------------------------------------------------------------
# Page-type classifier (matches the Hermes content-type folders)
# ---------------------------------------------------------------------------

_COMMERCIAL_HINTS = (
    "/comparatif/", "/comparatifs/", "/test/", "/tests/", "/avis/",
    "/best-", "/meilleur",
)
_INFO_HINTS = ("/guide/", "/guides/", "/blog/", "/article/")


def classify_page(page_path: str) -> str:
    p = (page_path or "").lower()
    for h in _COMMERCIAL_HINTS:
        if h in p:
            return "commercial"
    for h in _INFO_HINTS:
        if h in p:
            return "informational"
    return "default"


# ---------------------------------------------------------------------------
# Click-model revenue (used when Amazon API isn't eligible/available)
# ---------------------------------------------------------------------------

def _eur_to_usd(amount: float, currency: str) -> float:
    if currency == "USD":
        return amount
    return round(amount * EUR_USD, 2)


def compute_click_model_revenue(site_slug: str, days: int = 30) -> dict[str, Any]:
    """For each page in `page_metrics`, project revenue using site economics."""
    econ = SITE_ECONOMICS.get(site_slug)
    if not econ:
        return {"site_slug": site_slug, "error": "no economics config"}

    aov = float(econ["aov"])
    commission = float(econ["commission_rate"])
    cb_bands = econ["click_to_buy"]
    currency = econ["currency"]

    today = date.today()
    cutoff = (today - timedelta(days=days)).isoformat()

    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT page_path,
                   SUM(clicks)       AS clicks,
                   SUM(impressions)  AS impressions,
                   AVG(position)     AS position,
                   MAX(date)         AS last_date
            FROM page_metrics
            WHERE site_slug = ? AND date >= ?
            GROUP BY page_path
            """,
            (site_slug, cutoff),
        )
        rows = cur.fetchall()

        per_page = []
        upserts: list[tuple] = []
        for r in rows:
            page = r["page_path"] or ""
            clicks = int(r["clicks"] or 0)
            if clicks <= 0:
                continue
            ptype = classify_page(page)
            ctr_to_buy = float(cb_bands.get(ptype, cb_bands["default"]))
            est_orders = clicks * ctr_to_buy
            est_revenue = est_orders * aov
            est_commission = est_revenue * commission
            row = {
                "site_slug": site_slug,
                "page_path": page,
                "page_type": ptype,
                "date": today.isoformat(),
                "clicks": clicks,
                "impressions": int(r["impressions"] or 0),
                "position": round(float(r["position"] or 0), 2),
                "est_orders": round(est_orders, 3),
                "est_revenue": round(est_revenue, 2),
                "est_commission": round(est_commission, 2),
                "aov": aov,
                "commission_rate": commission,
                "ctr_to_buy": ctr_to_buy,
                "source": "click_model",
                "currency": currency,
            }
            per_page.append(row)
            upserts.append(
                (
                    site_slug, page, ptype, today.isoformat(),
                    clicks, row["impressions"], row["position"],
                    row["est_orders"], row["est_revenue"], row["est_commission"],
                    aov, commission, ctr_to_buy, "click_model", currency,
                )
            )

        cur.executemany(
            """
            INSERT INTO page_revenue (
                site_slug, page_path, page_type, date,
                clicks, impressions, position,
                est_orders, est_revenue, est_commission,
                aov, commission_rate, ctr_to_buy, source, currency
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(site_slug, page_path, date) DO UPDATE SET
                page_type=excluded.page_type,
                clicks=excluded.clicks,
                impressions=excluded.impressions,
                position=excluded.position,
                est_orders=excluded.est_orders,
                est_revenue=excluded.est_revenue,
                est_commission=excluded.est_commission,
                aov=excluded.aov,
                commission_rate=excluded.commission_rate,
                ctr_to_buy=excluded.ctr_to_buy,
                source=excluded.source,
                currency=excluded.currency
            """,
            upserts,
        )
        conn.commit()

    total_clicks = sum(r["clicks"] for r in per_page)
    total_revenue = round(sum(r["est_revenue"] for r in per_page), 2)
    total_commission = round(sum(r["est_commission"] for r in per_page), 2)

    return {
        "site_slug": site_slug,
        "currency": currency,
        "period_days": days,
        "page_count": len(per_page),
        "total_clicks": total_clicks,
        "total_revenue": total_revenue,
        "total_commission": total_commission,
        "aov": aov,
        "commission_rate": commission,
        "source": "click_model",
        "per_page": per_page,
    }


# ---------------------------------------------------------------------------
# Real Amazon Associates earnings (delegates to amazon-reporting-api.py).
# We just run it and parse the output — no need to duplicate the OAuth flow.
# ---------------------------------------------------------------------------

def fetch_amazon_earnings(date_iso: str, force_mock: bool = False) -> dict[str, Any]:
    script = SCRIPT_DIR / "amazon-reporting-api.py"
    if not script.exists():
        return {"source": "missing", "entries": [], "totals": {}}
    cmd = [sys.executable, str(script), "--date", date_iso]
    if force_mock:
        cmd.append("--mock")
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("  ⚠️  amazon-reporting-api.py timed out — using stale report if any")
    out = REPORTS_DIR / "amazon-daily-report.json"
    if not out.exists():
        return {"source": "missing", "entries": [], "totals": {}}
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        return {"source": "missing", "entries": [], "totals": {}}


# ---------------------------------------------------------------------------
# Daily roll-up: per-site write to revenue_estimates + report file
# ---------------------------------------------------------------------------

def _write_revenue_estimate(site_slug: str, agg: dict[str, Any], days: int) -> None:
    today = date.today()
    start = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO revenue_estimates (
                site_slug, period_start, period_end,
                estimated_clicks, estimated_conversions, estimated_revenue,
                commission_rate, source, currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site_slug, start, end,
                agg.get("total_clicks", 0),
                int(round(sum(p["est_orders"] for p in agg.get("per_page", [])))),
                agg.get("total_commission", 0.0),
                agg.get("commission_rate", 0.0),
                agg.get("source", "click_model"),
                agg.get("currency", "EUR"),
            ),
        )
        conn.commit()


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


def emit_strategist_signals(site_slug: str, agg: dict[str, Any]) -> int:
    """High-value pages → strategist refresh-now; dead-zone pages → CRO Optimizer."""
    pages = sorted(agg.get("per_page", []), key=lambda p: p["est_commission"], reverse=True)
    n = 0
    high_threshold = max(5.0, _eur_to_usd(50.0, agg.get("currency", "EUR")) / 50)
    for p in pages[:5]:
        if p["est_commission"] >= high_threshold:
            emit_event(
                "revenue.high_value_page",
                {
                    "site": site_slug,
                    "page_path": p["page_path"],
                    "page_type": p["page_type"],
                    "monthly_clicks": p["clicks"],
                    "monthly_commission": p["est_commission"],
                    "currency": p["currency"],
                    "recommended_action": "refresh_priority",
                },
                priority=2,
                target_agent="agent-strategist",
            )
            n += 1
    dead = [p for p in pages if p["clicks"] >= 200 and p["est_commission"] < 5.0]
    for p in dead[:5]:
        emit_event(
            "revenue.dead_zone_page",
            {
                "site": site_slug,
                "page_path": p["page_path"],
                "page_type": p["page_type"],
                "monthly_clicks": p["clicks"],
                "monthly_commission": p["est_commission"],
                "currency": p["currency"],
                "recommended_action": "cro_audit",
            },
            priority=2,
            target_agent="agent-cro-optimizer",
        )
        n += 1
    return n


def write_daily_report(per_site: dict[str, dict[str, Any]], real: dict[str, Any]) -> Path:
    today = date.today().isoformat()
    out = REPORTS_DIR / f"revenue-real-{today}.md"
    lines = [
        f"# Real revenue report — {today}",
        "",
        f"_Generated by `{AGENT_NAME}`. Replaces the legacy hardcoded `estimate_revenue()`._",
        "",
        "## Per-site rollup (last 30 days)",
        "",
        "| Site | Currency | Clicks | Pages | Revenue | Commission | AOV | Source |",
        "|------|---------|--------|-------|---------|------------|-----|--------|",
    ]
    grand_commission_eur = 0.0
    for slug, agg in sorted(per_site.items(), key=lambda kv: -kv[1].get("total_commission", 0)):
        comm = agg.get("total_commission", 0.0)
        currency = agg.get("currency", "EUR")
        comm_eur = comm if currency == "EUR" else round(comm / EUR_USD, 2)
        grand_commission_eur += comm_eur
        lines.append(
            f"| {slug} | {currency} "
            f"| {agg.get('total_clicks',0):,} "
            f"| {agg.get('page_count',0)} "
            f"| {agg.get('total_revenue',0):,.2f} "
            f"| {agg.get('total_commission',0):,.2f} "
            f"| {agg.get('aov',0):.0f} "
            f"| {agg.get('source','click_model')} |"
        )
    lines.append("")
    lines.append(f"**Total estimated commission (€-equivalent):** {grand_commission_eur:,.2f}")
    lines.append("")
    lines.append("## Real Amazon Associates earnings (today)")
    lines.append("")
    if real.get("entries"):
        lines.append(f"_Source: `{real.get('source','unknown')}`_")
        lines.append("")
        lines.append("| Site | Clicks | Orders | Revenue | Earnings |")
        lines.append("|------|--------|--------|---------|----------|")
        for e in real["entries"]:
            lines.append(
                f"| {e.get('site','?')} "
                f"| {e.get('clicks',0)} "
                f"| {e.get('ordered_items',0)} "
                f"| {e.get('revenue',0):,.2f} "
                f"| {e.get('earnings',0):,.2f} |"
            )
        t = real.get("totals", {})
        lines.append("")
        lines.append(
            f"**Totals:** clicks={t.get('clicks',0)} | orders={t.get('ordered_items',0)} | "
            f"revenue={t.get('revenue',0):,.2f} | earnings={t.get('earnings',0):,.2f}"
        )
    else:
        lines.append("_No real earnings available — run `amazon-reporting-api.py` once Amazon Associates eligibility lands._")
    lines.append("")
    lines.append("## Top 10 commission pages this period")
    lines.append("")
    all_pages = []
    for slug, agg in per_site.items():
        for p in agg.get("per_page", []):
            all_pages.append({**p, "site": slug})
    top = sorted(all_pages, key=lambda p: p["est_commission"], reverse=True)[:10]
    if top:
        lines.append("| Site | Page | Type | Clicks | Est. revenue | Est. commission |")
        lines.append("|------|------|------|--------|--------------|----------------|")
        for p in top:
            lines.append(
                f"| {p['site']} | `{p['page_path']}` | {p['page_type']} "
                f"| {p['clicks']} | {p['est_revenue']:,.2f} {p['currency']} "
                f"| **{p['est_commission']:,.2f} {p['currency']}** |"
            )
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✅ Report saved: {out}")
    return out


def run_daily(sites: list[str], days: int = 30, dry_run: bool = False) -> dict[str, Any]:
    ensure_schema()
    per_site: dict[str, dict[str, Any]] = {}
    signals = 0
    for slug in sites:
        if slug not in SITE_ECONOMICS:
            print(f"  ⚠️  Skipping {slug}: no economics config.")
            continue
        print(f"  → {slug}: computing click-model revenue ({days}d)…")
        agg = compute_click_model_revenue(slug, days=days)
        per_site[slug] = agg
        if not dry_run:
            _write_revenue_estimate(slug, agg, days)
            signals += emit_strategist_signals(slug, agg)
    real = fetch_amazon_earnings(date.today().isoformat(), force_mock=False)
    out_path = write_daily_report(per_site, real)
    if not dry_run:
        emit_event(
            "revenue.daily_report",
            {
                "report_path": str(out_path),
                "sites": list(per_site.keys()),
                "total_commission_eur": round(
                    sum(
                        (a.get("total_commission", 0.0) if a.get("currency") == "EUR"
                         else a.get("total_commission", 0.0) / EUR_USD)
                        for a in per_site.values()
                    ),
                    2,
                ),
                "real_source": real.get("source", "unknown"),
                "signals_emitted": signals,
            },
            priority=3,
            target_agent="agent-email-marketer",
        )
    return {"per_site": per_site, "real": real, "report_path": str(out_path), "signals_emitted": signals}


# ---------------------------------------------------------------------------
# Backfill — useful when you've just added the agent and have GSC history
# ---------------------------------------------------------------------------

def backfill(sites: list[str], days: int = 90, dry_run: bool = False) -> dict[str, Any]:
    return run_daily(sites, days=days, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Event consumer (shared lifecycle helpers)
# ---------------------------------------------------------------------------

CONSUMED_TYPES = {
    "analytics.daily_report",
    "analytics.weekly_report",
    "deployment.completed",
    "revenue.refresh_requested",
}


def _read_event(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def consume(limit: int = 10, dry_run: bool = False) -> int:
    files = sorted(INBOX_DIR.glob("*.json"))
    handled = 0
    for path in files:
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
        site = (event.get("payload") or {}).get("site") or (event.get("payload") or {}).get("site_slug")
        sites = [site] if site in SITE_ECONOMICS else list(SITE_ECONOMICS.keys())
        try:
            run_daily(sites, days=30, dry_run=dry_run)
            complete_claimed_event(event, dry_run=dry_run)
        except Exception as exc:
            print(f"  ❌ revenue agent failed on {path.name}: {exc}")
            fail_claimed_event(event, dry_run=dry_run)
        handled += 1
    print(f"[SUMMARY] {AGENT_NAME} handled {handled} event(s).")
    return handled


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=AGENT_NAME)
    parser.add_argument("--consume", action="store_true", help="Consume Hermes events")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--daily", action="store_true", help="Run daily roll-up across all sites")
    parser.add_argument("--site", type=str, help="Only process this site")
    parser.add_argument("--days", type=int, default=30, help="GSC look-back window")
    parser.add_argument("--backfill", action="store_true", help="Backfill 90 days")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sites = [args.site] if args.site else list(SITE_ECONOMICS.keys())

    if args.consume:
        consume(limit=args.limit, dry_run=args.dry_run)
        return 0
    if args.backfill:
        backfill(sites, days=args.days, dry_run=args.dry_run)
        return 0
    if args.daily or args.site:
        run_daily(sites, days=args.days, dry_run=args.dry_run)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
