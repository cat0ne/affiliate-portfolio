#!/usr/bin/env python3
"""
Agent CWV — Core Web Vitals & PageSpeed Insights monitor.

Why this exists
---------------
The architecture doc claims `agent-seo-auditor` checks Core Web Vitals via the
PageSpeed Insights API. The reality is that no agent calls PSI at all —
matelas and aspirateur sit at average position 30 with many CWV-suppressed
top-10 candidates and nobody notices because no LCP/CLS/INP signal flows
through the bus.

This agent:

1. Picks the top-N most-clicked pages per site from `page_metrics` (or
   homepage + category pages when GSC is empty) and runs PSI v5 against each
   on **mobile** strategy. Uses `PSI_API_KEY` when set; otherwise the same
   GSC service-account JSON as other agents (Bearer token, `openid` scope);
   unauthenticated calls are a last resort (very low quota).
2. Stores LCP, CLS, INP, TTFB, FCP, performance score, and the failing
   audits per URL/date in a new `cwv_metrics` table.
3. Compares against the previous day. If LCP > 2500 ms, CLS > 0.1, INP > 200 ms,
   or any metric regressed > 20% vs yesterday, emits `seo.cwv_regressed`
   events with the failing audit IDs so the SEO Auditor can pick them up.
4. Writes a per-site Markdown report so a human can scan trends.

Environment
-----------
* `PSI_API_KEY` / `PAGESPEED_API_KEY` (optional — higher quota; create at
  https://console.cloud.google.com/apis/credentials).
* If no PSI key is set, the agent reuses the **same service-account JSON** as
  GSC: `GSC_CREDENTIALS_PATH`, `GOOGLE_APPLICATION_CREDENTIALS`, `~/gsc-credentials.json`,
  or `gsc-credentials.json` at the repo root. The GCP project must have
  **PageSpeed Insights API** enabled for that service account.

Usage
-----
    python3 agent_cwv.py --consume --limit 10
    python3 agent_cwv.py --daily                      # all sites, top 10 URLs each
    python3 agent_cwv.py --site matelas --top 50      # one site, top 50
    python3 agent_cwv.py --url https://www.matelas-expert.fr/ --strategy mobile
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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

# google-auth / google-api-python-client (see scripts/requirements.txt)
try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account as google_service_account
except ImportError:
    GoogleAuthRequest = None  # type: ignore[misc,assignment]
    google_service_account = None  # type: ignore[misc,assignment]

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

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

AGENT_NAME = "agent-cwv"

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_API_KEY = os.environ.get("PSI_API_KEY") or os.environ.get("PAGESPEED_API_KEY") or ""

# Documented scope for PageSpeed Insights API v5 (see OAuth scopes list).
_PSI_SA_SCOPES = ("openid",)

_psi_sa_credentials: Optional[Any] = None


def _resolve_gsc_credentials_path() -> Optional[Path]:
    """Match GSC scripts: explicit path, ADC, home, then repo-root file."""
    for raw in (
        os.environ.get("GSC_CREDENTIALS_PATH"),
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
    ):
        if raw:
            p = Path(raw).expanduser()
            if p.is_file():
                return p
    home_json = Path.home() / "gsc-credentials.json"
    if home_json.is_file():
        return home_json
    repo_root = Path(__file__).resolve().parent.parent / "gsc-credentials.json"
    if repo_root.is_file():
        return repo_root
    return None


def _psi_oauth_access_token() -> Optional[str]:
    """Bearer token for PSI when using a service account (no API key)."""
    if not google_service_account or not GoogleAuthRequest:
        return None
    path = _resolve_gsc_credentials_path()
    if not path:
        return None
    global _psi_sa_credentials
    try:
        if _psi_sa_credentials is None:
            _psi_sa_credentials = google_service_account.Credentials.from_service_account_file(
                str(path),
                scopes=list(_PSI_SA_SCOPES),
            )
        if not _psi_sa_credentials.valid:
            _psi_sa_credentials.refresh(GoogleAuthRequest())
        return _psi_sa_credentials.token
    except Exception as exc:
        print(f"  ⚠️  PSI service-account auth failed ({path}): {exc}")
        _psi_sa_credentials = None
        return None

SITES: dict[str, dict[str, str]] = {
    "aspirateur": {"domain": "top-aspirateur.fr"},
    "bureau":     {"domain": "bureau-expert.fr"},
    "matelas":    {"domain": "matelas-expert.fr"},
    "cafe":       {"domain": "brewmance.fr"},
    "pixinstant": {"domain": "pixinstant.com"},
    "airpurify":  {"domain": "airpurifyhq.com"},
    "safehive":   {"domain": "safehivehq.com"},
    "pawhive":    {"domain": "pawhivehq.com"},
}

# Google CWV "Good" thresholds (mobile, p75) — March 2024 onwards.
THRESHOLDS = {
    "lcp_ms":     {"good": 2500, "poor": 4000},
    "cls":        {"good": 0.10, "poor": 0.25},
    "inp_ms":     {"good": 200,  "poor": 500},
    "ttfb_ms":    {"good": 800,  "poor": 1800},
    "fcp_ms":     {"good": 1800, "poor": 3000},
}

# Which audits we surface to seo.cwv_regressed events so the SEO Auditor
# knows which file/component to investigate.
INTERESTING_AUDITS = {
    "largest-contentful-paint-element",
    "cumulative-layout-shift",
    "render-blocking-resources",
    "uses-responsive-images",
    "uses-optimized-images",
    "modern-image-formats",
    "unminified-javascript",
    "unminified-css",
    "unused-javascript",
    "unused-css-rules",
    "uses-text-compression",
    "uses-rel-preload",
    "uses-rel-preconnect",
    "font-display",
    "third-party-summary",
    "total-blocking-time",
    "interaction-to-next-paint",
    "max-potential-fid",
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
            CREATE TABLE IF NOT EXISTS cwv_metrics (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                site_slug       TEXT NOT NULL,
                url             TEXT NOT NULL,
                strategy        TEXT NOT NULL,             -- mobile | desktop
                date            DATE NOT NULL,
                perf_score      REAL,                       -- 0..1
                lcp_ms          REAL,
                cls             REAL,
                inp_ms          REAL,
                ttfb_ms         REAL,
                fcp_ms          REAL,
                tbt_ms          REAL,
                lcp_status      TEXT,                       -- good | needs-improvement | poor
                cls_status      TEXT,
                inp_status      TEXT,
                source          TEXT,                       -- field | lab
                failing_audits  JSON,
                raw             JSON,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(site_slug, url, strategy, date)
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cwv_site_date ON cwv_metrics(site_slug, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cwv_url ON cwv_metrics(url)")
        conn.commit()


# ---------------------------------------------------------------------------
# PSI client
# ---------------------------------------------------------------------------

def call_psi(url: str, strategy: str = "mobile", timeout: int = 60) -> Optional[dict]:
    params: dict[str, Any] = {
        "url": url,
        "strategy": strategy,
        "category": ["performance"],
    }
    if PSI_API_KEY:
        params["key"] = PSI_API_KEY
    qs = urllib.parse.urlencode(params, doseq=True)
    full = f"{PSI_ENDPOINT}?{qs}"
    headers: dict[str, str] = {"User-Agent": "hermes-agent-cwv/1.0"}
    if not PSI_API_KEY:
        token = _psi_oauth_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(full, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"  ⚠️  PSI HTTP {exc.code} for {url}: {body[:160]}")
        return None
    except Exception as exc:
        print(f"  ⚠️  PSI failed for {url}: {exc}")
        return None


def _status_for(metric: str, value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    t = THRESHOLDS.get(metric)
    if not t:
        return "unknown"
    if value <= t["good"]:
        return "good"
    if value <= t["poor"]:
        return "needs-improvement"
    return "poor"


def parse_psi(raw: dict) -> dict[str, Any]:
    """Extract LCP/CLS/INP/TTFB/FCP from a PSI response.

    Prefers field data (loadingExperience) over lab data (lighthouseResult)
    because field is what Google ranks on. Falls back to lab when field is
    empty (low-traffic pages).
    """
    out: dict[str, Any] = {
        "perf_score": None,
        "lcp_ms": None, "cls": None, "inp_ms": None, "ttfb_ms": None, "fcp_ms": None, "tbt_ms": None,
        "source": "lab",
        "failing_audits": [],
    }

    field = (raw.get("loadingExperience") or {}).get("metrics", {}) or {}
    if field:
        out["source"] = "field"
        if "LARGEST_CONTENTFUL_PAINT_MS" in field:
            out["lcp_ms"] = float(field["LARGEST_CONTENTFUL_PAINT_MS"]["percentile"])
        if "CUMULATIVE_LAYOUT_SHIFT_SCORE" in field:
            out["cls"] = float(field["CUMULATIVE_LAYOUT_SHIFT_SCORE"]["percentile"]) / 100.0
        if "INTERACTION_TO_NEXT_PAINT" in field:
            out["inp_ms"] = float(field["INTERACTION_TO_NEXT_PAINT"]["percentile"])
        if "EXPERIMENTAL_TIME_TO_FIRST_BYTE" in field:
            out["ttfb_ms"] = float(field["EXPERIMENTAL_TIME_TO_FIRST_BYTE"]["percentile"])
        if "FIRST_CONTENTFUL_PAINT_MS" in field:
            out["fcp_ms"] = float(field["FIRST_CONTENTFUL_PAINT_MS"]["percentile"])

    lh = raw.get("lighthouseResult", {})
    if lh:
        cats = lh.get("categories", {}) or {}
        perf = (cats.get("performance") or {}).get("score")
        if perf is not None:
            out["perf_score"] = float(perf)

        audits = lh.get("audits", {}) or {}
        # Lab-only fallback for any missing field metric.
        def _audit_ms(audit_id: str) -> Optional[float]:
            a = audits.get(audit_id) or {}
            v = a.get("numericValue")
            return float(v) if v is not None else None

        if out["lcp_ms"] is None:
            out["lcp_ms"] = _audit_ms("largest-contentful-paint")
        if out["cls"] is None:
            v = (audits.get("cumulative-layout-shift") or {}).get("numericValue")
            out["cls"] = float(v) if v is not None else None
        if out["fcp_ms"] is None:
            out["fcp_ms"] = _audit_ms("first-contentful-paint")
        if out["ttfb_ms"] is None:
            out["ttfb_ms"] = _audit_ms("server-response-time")
        if out["inp_ms"] is None:
            out["inp_ms"] = _audit_ms("interaction-to-next-paint")
        out["tbt_ms"] = _audit_ms("total-blocking-time")

        # Surface failing/opportunity audits for the SEO Auditor.
        for aid, a in audits.items():
            if aid not in INTERESTING_AUDITS:
                continue
            score = a.get("score")
            if score is not None and score < 0.9:
                out["failing_audits"].append({
                    "id": aid,
                    "title": a.get("title", aid),
                    "score": score,
                    "display_value": a.get("displayValue", ""),
                    "details_summary": (a.get("details") or {}).get("type"),
                })

    out["lcp_status"] = _status_for("lcp_ms", out["lcp_ms"])
    out["cls_status"] = _status_for("cls", out["cls"])
    out["inp_status"] = _status_for("inp_ms", out["inp_ms"])
    return out


# ---------------------------------------------------------------------------
# URL selection
# ---------------------------------------------------------------------------

DEFAULT_PATHS = ["/", "/comparatif/", "/guide/", "/test/"]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def top_urls_for_site(site_slug: str, top_n: int = 10) -> list[str]:
    """Return the top-N most-clicked URLs from `gsc_page_daily` (pipeline import),
    then legacy `page_metrics`, then homepage defaults."""
    domain = SITES[site_slug]["domain"]
    urls: list[str] = []
    try:
        with _conn() as conn:
            rows: list[sqlite3.Row] = []
            if _has_table(conn, "gsc_page_daily"):
                rows = conn.execute(
                    """
                    SELECT page_path, SUM(clicks) AS clicks
                    FROM gsc_page_daily
                    WHERE site_slug = ? AND date >= date('now','-30 days')
                    GROUP BY page_path
                    HAVING SUM(clicks) > 0
                    ORDER BY SUM(clicks) DESC
                    LIMIT ?
                    """,
                    (site_slug, top_n),
                ).fetchall()
            if not rows and _has_table(conn, "page_metrics"):
                try:
                    rows = conn.execute(
                        """
                        SELECT page_path, SUM(clicks) AS clicks
                        FROM page_metrics
                        WHERE site_slug = ? AND date >= date('now','-30 days')
                        GROUP BY page_path
                        HAVING SUM(clicks) > 0
                        ORDER BY SUM(clicks) DESC
                        LIMIT ?
                        """,
                        (site_slug, top_n),
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
        for r in rows:
            page = r["page_path"] or ""
            if page.startswith("http"):
                urls.append(page)
            elif page.startswith("/"):
                urls.append(f"https://www.{domain}{page}")
    except Exception as exc:
        print(f"  ⚠️  GSC top-urls query failed: {exc}")

    if not urls:
        urls = [f"https://www.{domain}{p}" for p in DEFAULT_PATHS]
    # de-dupe but keep order
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:top_n]


# ---------------------------------------------------------------------------
# Persist + diff against yesterday
# ---------------------------------------------------------------------------

def _get_previous(site_slug: str, url: str, strategy: str) -> Optional[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            """
            SELECT * FROM cwv_metrics
            WHERE site_slug = ? AND url = ? AND strategy = ? AND date < date('now')
            ORDER BY date DESC LIMIT 1
            """,
            (site_slug, url, strategy),
        ).fetchone()


def _store(site_slug: str, url: str, strategy: str, parsed: dict, raw: dict) -> None:
    today = date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO cwv_metrics (
                site_slug, url, strategy, date,
                perf_score, lcp_ms, cls, inp_ms, ttfb_ms, fcp_ms, tbt_ms,
                lcp_status, cls_status, inp_status,
                source, failing_audits, raw
            ) VALUES (?,?,?,?, ?,?,?,?,?,?,?, ?,?,?, ?,?,?)
            ON CONFLICT(site_slug, url, strategy, date) DO UPDATE SET
                perf_score=excluded.perf_score,
                lcp_ms=excluded.lcp_ms, cls=excluded.cls, inp_ms=excluded.inp_ms,
                ttfb_ms=excluded.ttfb_ms, fcp_ms=excluded.fcp_ms, tbt_ms=excluded.tbt_ms,
                lcp_status=excluded.lcp_status, cls_status=excluded.cls_status, inp_status=excluded.inp_status,
                source=excluded.source, failing_audits=excluded.failing_audits, raw=excluded.raw
            """,
            (
                site_slug, url, strategy, today,
                parsed["perf_score"], parsed["lcp_ms"], parsed["cls"],
                parsed["inp_ms"], parsed["ttfb_ms"], parsed["fcp_ms"], parsed["tbt_ms"],
                parsed["lcp_status"], parsed["cls_status"], parsed["inp_status"],
                parsed["source"],
                json.dumps(parsed["failing_audits"], ensure_ascii=False),
                json.dumps({"id": raw.get("id"), "analysisUTCTimestamp": (raw.get("lighthouseResult") or {}).get("analysisUTCTimestamp")}),
            ),
        )
        conn.commit()


def _detect_regression(parsed: dict, prev: Optional[sqlite3.Row]) -> dict[str, Any]:
    """Return a regression report (empty dict if no regression)."""
    issues: list[dict[str, Any]] = []
    abs_thresholds = {"lcp_ms": 2500, "cls": 0.10, "inp_ms": 200}
    for metric, ceiling in abs_thresholds.items():
        v = parsed.get(metric)
        if v is None:
            continue
        if v > ceiling:
            issues.append({"metric": metric, "current": round(v, 3), "threshold": ceiling, "kind": "absolute"})
    if prev:
        for metric in ("lcp_ms", "cls", "inp_ms", "ttfb_ms", "fcp_ms"):
            cur = parsed.get(metric)
            old = prev[metric]
            if cur is None or old is None or old == 0:
                continue
            change = (cur - old) / old
            if change >= 0.20 and cur > 0:
                issues.append({
                    "metric": metric,
                    "current": round(float(cur), 3),
                    "previous": round(float(old), 3),
                    "change_pct": round(change * 100, 1),
                    "kind": "regression",
                })
    return {"issues": issues, "failing_audits": parsed.get("failing_audits", [])}


# ---------------------------------------------------------------------------
# Events
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
# Site runner
# ---------------------------------------------------------------------------

def audit_site(
    site_slug: str,
    top_n: int = 10,
    strategy: str = "mobile",
    sleep_between: float = 1.5,
    dry_run: bool = False,
) -> dict[str, Any]:
    ensure_schema()
    urls = top_urls_for_site(site_slug, top_n=top_n)
    print(f"  → {site_slug}: {len(urls)} URL(s) to audit on {strategy}")
    results: list[dict[str, Any]] = []
    regressions = 0

    for url in urls:
        if dry_run:
            print(f"     [DRY-RUN] PSI {url}")
            continue
        raw = call_psi(url, strategy=strategy)
        if not raw:
            time.sleep(sleep_between)
            continue
        parsed = parse_psi(raw)
        prev = _get_previous(site_slug, url, strategy)
        _store(site_slug, url, strategy, parsed, raw)

        regression = _detect_regression(parsed, prev)
        record = {"url": url, "parsed": parsed, "regression": regression}
        results.append(record)

        if regression["issues"]:
            regressions += 1
            emit_event(
                "seo.cwv_regressed",
                {
                    "site": site_slug,
                    "url": url,
                    "strategy": strategy,
                    "metrics": {
                        "lcp_ms": parsed["lcp_ms"], "cls": parsed["cls"], "inp_ms": parsed["inp_ms"],
                        "ttfb_ms": parsed["ttfb_ms"], "fcp_ms": parsed["fcp_ms"],
                    },
                    "statuses": {
                        "lcp": parsed["lcp_status"], "cls": parsed["cls_status"], "inp": parsed["inp_status"],
                    },
                    "issues": regression["issues"],
                    "failing_audits": parsed["failing_audits"][:8],
                    "perf_score": parsed["perf_score"],
                    "source": parsed["source"],
                },
                priority=2,
                target_agent="agent-seo-auditor",
            )
        time.sleep(sleep_between)

    return {"site": site_slug, "audited": len(results), "regressions": regressions, "results": results}


def write_site_report(audit: dict[str, Any]) -> Optional[Path]:
    if not audit.get("results"):
        return None
    site = audit["site"]
    today = date.today().isoformat()
    out = REPORTS_DIR / f"cwv-{site}-{today}.md"
    lines = [
        f"# Core Web Vitals — {site} ({today})",
        "",
        f"_Generated by `{AGENT_NAME}`. Mobile strategy via PSI v5._",
        "",
        f"- URLs audited: **{audit['audited']}**",
        f"- Regressions detected: **{audit['regressions']}**",
        "",
        "| URL | Perf | LCP (ms) | CLS | INP (ms) | TTFB (ms) | Source | Issues |",
        "|-----|------|----------|-----|----------|-----------|--------|--------|",
    ]
    for r in audit["results"]:
        p = r["parsed"]
        issues = "; ".join(f"{i['metric']}={i.get('current','?')}" for i in r["regression"]["issues"][:3]) or "—"
        lines.append(
            f"| `{r['url']}` "
            f"| {round((p['perf_score'] or 0)*100)} "
            f"| {int(p['lcp_ms'] or 0)} ({p['lcp_status']}) "
            f"| {round(p['cls'] or 0, 2)} ({p['cls_status']}) "
            f"| {int(p['inp_ms'] or 0)} ({p['inp_status']}) "
            f"| {int(p['ttfb_ms'] or 0)} "
            f"| {p['source']} "
            f"| {issues} |"
        )
    lines.append("")
    lines.append("## Top failing audits")
    lines.append("")
    bag: dict[str, int] = {}
    for r in audit["results"]:
        for fa in r["parsed"].get("failing_audits", []):
            bag[fa["id"]] = bag.get(fa["id"], 0) + 1
    if bag:
        lines.append("| Audit | Pages affected |")
        lines.append("|-------|----------------|")
        for aid, n in sorted(bag.items(), key=lambda kv: -kv[1])[:15]:
            lines.append(f"| `{aid}` | {n} |")
    else:
        lines.append("_None._")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✅ Report saved: {out}")
    return out


# ---------------------------------------------------------------------------
# Daily driver + consumer
# ---------------------------------------------------------------------------

def run_daily(sites: list[str], top_n: int = 10, strategy: str = "mobile", dry_run: bool = False) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for slug in sites:
        if slug not in SITES:
            print(f"  ⚠️  unknown site {slug}")
            continue
        audit = audit_site(slug, top_n=top_n, strategy=strategy, dry_run=dry_run)
        write_site_report(audit)
        summary[slug] = {"audited": audit["audited"], "regressions": audit["regressions"]}
    if not dry_run:
        emit_event(
            "cwv.daily_audit_completed",
            {"summary": summary, "strategy": strategy, "top_n": top_n},
            priority=4,
            target_agent="agent-analytics",
        )
    return summary


CONSUMED_TYPES = {"deployment.completed", "cwv.audit_requested", "seo.audit_completed"}


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
        sites = [site] if site in SITES else list(SITES.keys())
        try:
            run_daily(sites, top_n=int(payload.get("top_n", 10)), dry_run=dry_run)
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
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--strategy", choices=("mobile", "desktop"), default="mobile")
    parser.add_argument("--url", type=str, help="Audit a single URL (no DB lookup)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.url:
        ensure_schema()
        raw = call_psi(args.url, strategy=args.strategy)
        if not raw:
            return 1
        parsed = parse_psi(raw)
        print(json.dumps({"url": args.url, **parsed}, indent=2, ensure_ascii=False))
        return 0

    if args.consume:
        consume(limit=args.limit, dry_run=args.dry_run)
        return 0

    sites = [args.site] if args.site else list(SITES.keys())
    if args.daily or args.site:
        run_daily(sites, top_n=args.top, strategy=args.strategy, dry_run=args.dry_run)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
