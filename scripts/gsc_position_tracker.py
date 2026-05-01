#!/usr/bin/env python3
"""
GSC Keyword Position Tracker
Tracks keyword positions per page over time, identifies losers & winners.
Compares current period vs previous period for each site.

Usage:
    python gsc_position_tracker.py --days 28 --top-queries 50
"""

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BASE_DIR = Path("/Users/gho/Documents/affiliation-sites")
CREDENTIALS_PATH = BASE_DIR / "gsc-credentials.json"
REPORTS_DIR = BASE_DIR / "reports"
DATA_DIR = BASE_DIR / "reports" / "gsc-tracking"

SITES = [
    {"url": "sc-domain:brewmance.fr", "name": "Brewmance", "dir": "cafe"},
    {"url": "sc-domain:matelas-expert.fr", "name": "Matelas", "dir": "matelas"},
    {"url": "sc-domain:top-aspirateur.fr", "name": "Top-Aspirateur", "dir": "aspirateur"},
    {"url": "sc-domain:pixinstant.com", "name": "PixInstant", "dir": "pixinstant"},
    {"url": "sc-domain:bureau-expert.fr", "name": "Bureau-Expert", "dir": "bureau"},
]

# Thresholds
MIN_IMPRESSIONS = 50      # Only track queries with enough data
POSITION_CHANGE_THRESHOLD = 3.0  # Flag changes >= 3 positions
MIN_CLICKS_CURRENT = 1    # Must have at least 1 click currently to be interesting


def gsc_date(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def get_gsc_service():
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"Credentials missing: {CREDENTIALS_PATH}")
    creds = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH),
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    return build("webmasters", "v3", credentials=creds, cache_discovery=False)


def query_gsc(service, site_url, start_date, end_date, dimensions=None, row_limit=25000, filters=None):
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "rowLimit": row_limit,
    }
    if dimensions:
        body["dimensions"] = dimensions
    if filters:
        body["dimensionFilterGroups"] = filters
    try:
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return response.get("rows", [])
    except HttpError as e:
        if e.resp.status == 403:
            print(f"  ⚠️  Permission denied for {site_url}")
        else:
            print(f"  ⚠️  API error ({e.resp.status}) for {site_url}: {e}")
        return []


def fetch_queries(service, site, days, top_n=50):
    """Fetch top queries for a site with current vs previous period comparison."""
    end = datetime.now() - timedelta(days=3)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    curr_start, curr_end = gsc_date(start), gsc_date(end)
    prev_start_s, prev_end_s = gsc_date(prev_start), gsc_date(prev_end)

    # Current period: top queries
    curr_rows = query_gsc(
        service, site["url"], curr_start, curr_end,
        dimensions=["query"], row_limit=top_n * 2
    )
    # Previous period: same queries
    prev_rows = query_gsc(
        service, site["url"], prev_start_s, prev_end_s,
        dimensions=["query"], row_limit=top_n * 2
    )

    prev_map = {r["keys"][0]: r for r in prev_rows}

    results = []
    for r in curr_rows:
        query = r["keys"][0]
        curr = {
            "clicks": int(r["clicks"]),
            "impressions": int(r["impressions"]),
            "ctr": round(r["ctr"] * 100, 2),
            "position": round(r["position"], 2),
        }
        prev_r = prev_map.get(query)
        if prev_r:
            prev = {
                "clicks": int(prev_r["clicks"]),
                "impressions": int(prev_r["impressions"]),
                "ctr": round(prev_r["ctr"] * 100, 2),
                "position": round(prev_r["position"], 2),
            }
        else:
            prev = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}

        pos_change = prev["position"] - curr["position"]  # positive = improved
        clicks_change = curr["clicks"] - prev["clicks"]
        clicks_pct = round((clicks_change / prev["clicks"]) * 100, 1) if prev["clicks"] > 0 else (100 if curr["clicks"] > 0 else 0)

        results.append({
            "query": query,
            "current": curr,
            "previous": prev,
            "position_change": round(pos_change, 2),
            "clicks_change": clicks_change,
            "clicks_change_pct": clicks_pct,
        })

    # Sort by absolute position change (largest movers first)
    results.sort(key=lambda x: abs(x["position_change"]), reverse=True)
    return {
        "site": site["name"],
        "site_url": site["url"],
        "period_current": {"start": curr_start, "end": curr_end},
        "period_previous": {"start": prev_start_s, "end": prev_end_s},
        "queries": results[:top_n],
    }


def fetch_page_queries(service, site, days, page_url, top_n=20):
    """Fetch top queries for a specific page."""
    end = datetime.now() - timedelta(days=3)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    curr_start, curr_end = gsc_date(start), gsc_date(end)
    prev_start_s, prev_end_s = gsc_date(prev_start), gsc_date(prev_end)

    filters = [{
        "filters": [{
            "dimension": "page",
            "operator": "equals",
            "expression": page_url,
        }]
    }]

    curr_rows = query_gsc(
        service, site["url"], curr_start, curr_end,
        dimensions=["query"], row_limit=top_n * 2, filters=filters
    )
    prev_rows = query_gsc(
        service, site["url"], prev_start_s, prev_end_s,
        dimensions=["query"], row_limit=top_n * 2, filters=filters
    )

    prev_map = {r["keys"][0]: r for r in prev_rows}

    results = []
    for r in curr_rows:
        query = r["keys"][0]
        curr_pos = round(r["position"], 2)
        curr_clicks = int(r["clicks"])
        curr_impressions = int(r["impressions"])

        prev_r = prev_map.get(query)
        if prev_r:
            prev_pos = round(prev_r["position"], 2)
            prev_clicks = int(prev_r["clicks"])
        else:
            prev_pos = 0.0
            prev_clicks = 0

        pos_change = prev_pos - curr_pos
        results.append({
            "query": query,
            "current_position": curr_pos,
            "previous_position": prev_pos,
            "position_change": round(pos_change, 2),
            "current_clicks": curr_clicks,
            "previous_clicks": prev_clicks,
            "current_impressions": curr_impressions,
        })

    results.sort(key=lambda x: abs(x["position_change"]), reverse=True)
    return results[:top_n]


def generate_report(all_data, days):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 📊 GSC Keyword Position Tracker — {today}",
        "",
        f"**Période analysée :** {days} jours (actuel) vs {days} jours (précédent)",
        "",
    ]

    for data in all_data:
        site_name = data["site"]
        queries = data["queries"]
        period = data["period_current"]

        lines.append(f"## {site_name}")
        lines.append("")
        lines.append(f"*Période : {period['start']} → {period['end']}*")
        lines.append("")

        # Winners (position improved)
        winners = [q for q in queries if q["position_change"] >= POSITION_CHANGE_THRESHOLD and q["current"]["impressions"] >= MIN_IMPRESSIONS]
        # Losers (position dropped)
        losers = [q for q in queries if q["position_change"] <= -POSITION_CHANGE_THRESHOLD and q["current"]["impressions"] >= MIN_IMPRESSIONS]
        # New queries (no previous data but current clicks > 0)
        new_queries = [q for q in queries if q["previous"]["position"] == 0 and q["current"]["clicks"] > 0]

        if winners:
            lines.append(f"### 🟢 Gagnants (+{POSITION_CHANGE_THRESHOLD}+ positions)")
            lines.append("")
            lines.append("| Requête | Pos Avant | Pos Actuelle | Δ | Clics Avant | Clics Actuels |")
            lines.append("|---------|-----------|--------------|---|-------------|---------------|")
            for q in winners[:10]:
                prev_pos = q["previous"]["position"] if q["previous"]["position"] > 0 else "—"
                lines.append(
                    f"| {q['query']} | {prev_pos} | {q['current']['position']} | "
                    f"+{q['position_change']:.1f} | {q['previous']['clicks']} | {q['current']['clicks']} |"
                )
            lines.append("")

        if losers:
            lines.append(f"### 🔴 Perdants (-{POSITION_CHANGE_THRESHOLD}+ positions)")
            lines.append("")
            lines.append("| Requête | Pos Avant | Pos Actuelle | Δ | Clics Avant | Clics Actuels |")
            lines.append("|---------|-----------|--------------|---|-------------|---------------|")
            for q in losers[:10]:
                prev_pos = q["previous"]["position"] if q["previous"]["position"] > 0 else "—"
                lines.append(
                    f"| {q['query']} | {prev_pos} | {q['current']['position']} | "
                    f"{q['position_change']:.1f} | {q['previous']['clicks']} | {q['current']['clicks']} |"
                )
            lines.append("")

        if new_queries:
            lines.append(f"### 🆕 Nouvelles requêtes (pas de données période précédente)")
            lines.append("")
            lines.append("| Requête | Position | Clics | Impressions |")
            lines.append("|---------|----------|-------|-------------|")
            for q in new_queries[:10]:
                lines.append(
                    f"| {q['query']} | {q['current']['position']} | {q['current']['clicks']} | {q['current']['impressions']} |"
                )
            lines.append("")

        if not winners and not losers and not new_queries:
            lines.append("Aucun mouvement significatif détecté.")
            lines.append("")

    lines.extend(["", "*Report generated automatically*", ""])
    return "\n".join(lines)


def save_json_snapshot(all_data, days):
    today = datetime.now().strftime("%Y-%m-%d")
    snapshot = {
        "generated_at": datetime.now().isoformat(),
        "days_compared": days,
        "sites": all_data,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"positions-{today}.json"
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    # Also save as latest
    latest = DATA_DIR / "positions-latest.json"
    latest.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description="GSC Keyword Position Tracker")
    parser.add_argument("--days", type=int, default=28, help="Days per period (default: 28)")
    parser.add_argument("--top-queries", type=int, default=50, help="Top N queries to track per site (default: 50)")
    args = parser.parse_args()

    print("=" * 60)
    print("GSC Keyword Position Tracker")
    print("=" * 60)

    service = get_gsc_service()
    all_data = []

    for site in SITES:
        print(f"\n📊 Fetching {site['name']}...")
        data = fetch_queries(service, site, args.days, args.top_queries)
        all_data.append(data)
        movers = [q for q in data["queries"] if abs(q["position_change"]) >= POSITION_CHANGE_THRESHOLD]
        print(f"   {len(data['queries'])} queries tracked, {len(movers)} significant movers")

    print("\n📝 Generating report...")
    md = generate_report(all_data, args.days)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    md_path = REPORTS_DIR / f"gsc-positions-{today}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"✅ Report saved: {md_path}")

    json_path = save_json_snapshot(all_data, args.days)
    print(f"✅ JSON snapshot saved: {json_path}")

    print("\n✨ Done")


if __name__ == "__main__":
    main()
