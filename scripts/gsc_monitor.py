#!/usr/bin/env python3
"""
GSC Auto-Alerts Monitor
Compare current GSC period vs previous period and trigger alerts.
Usage:
    python gsc_monitor.py --days 28
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BASE_DIR = Path("/Users/gho/Documents/affiliation-sites")
CREDENTIALS_PATH = BASE_DIR / "gsc-credentials.json"
REPORTS_DIR = BASE_DIR / "reports"

SITES = [
    {"url": "sc-domain:brewmance.fr", "name": "Brewmance"},
    {"url": "sc-domain:matelas-expert.fr", "name": "Matelas"},
    {"url": "sc-domain:top-aspirateur.fr", "name": "Top-Aspirateur"},
    {"url": "sc-domain:pixinstant.com", "name": "PixInstant"},
    {"url": "sc-domain:bureau-expert.fr", "name": "Bureau-Expert"},
    {"url": "sc-domain:ikasia.ai", "name": "Ikasia"},
]

# Alert thresholds
ALERT_CLICKS_DROP_PCT = 20
ALERT_CLICKS_ABS_MIN = 5      # only alert if previous clicks >= this (noise filter)
ALERT_POSITION_DROP = 5
ALERT_POSITION_MIN_IMPRESSIONS = 50  # only alert position drop if page has enough data
ALERT_IMPRESSIONS_DROP_PCT = 30
ALERT_IMPRESSIONS_ABS_MIN = 200      # only alert if previous impressions >= this
ALERT_CTR_LOW_PCT = 0.5
ALERT_CTR_IMPRESSIONS_MIN = 1000


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


def query_gsc(service, site_url, start_date, end_date, dimensions=None, row_limit=25000):
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "rowLimit": row_limit,
    }
    if dimensions:
        body["dimensions"] = dimensions
    try:
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return response.get("rows", [])
    except HttpError as e:
        if e.resp.status == 403:
            print(f"  ⚠️  Permission denied for {site_url}")
        else:
            print(f"  ⚠️  API error ({e.resp.status}) for {site_url}: {e}")
        return []


def fetch_period(service, site, days):
    end = datetime.now() - timedelta(days=3)
    start = end - timedelta(days=days - 1)
    start_s = gsc_date(start)
    end_s = gsc_date(end)

    overall = query_gsc(service, site["url"], start_s, end_s)
    perf = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    if overall:
        row = overall[0]
        perf = {
            "clicks": int(row["clicks"]),
            "impressions": int(row["impressions"]),
            "ctr": round(row["ctr"] * 100, 2),
            "position": round(row["position"], 2),
        }

    pages = query_gsc(service, site["url"], start_s, end_s, dimensions=["page"], row_limit=5000)
    page_data = []
    for r in pages:
        page_data.append({
            "page": r["keys"][0],
            "clicks": int(r["clicks"]),
            "impressions": int(r["impressions"]),
            "ctr": round(r["ctr"] * 100, 2),
            "position": round(r["position"], 2),
        })

    return {
        "start": start_s,
        "end": end_s,
        "performance": perf,
        "pages": page_data,
    }


def check_alerts(current, previous, site_name):
    alerts = []
    cp = current["performance"]
    pp = previous["performance"]

    # Clicks drop (ignore low-volume noise)
    if pp["clicks"] >= ALERT_CLICKS_ABS_MIN:
        clicks_drop = ((pp["clicks"] - cp["clicks"]) / pp["clicks"]) * 100
        if clicks_drop > ALERT_CLICKS_DROP_PCT:
            alerts.append({
                "site": site_name,
                "type": "clicks_drop",
                "severity": "high" if clicks_drop > 50 else "medium",
                "message": f"Clicks dropped {clicks_drop:.1f}% ({pp['clicks']} → {cp['clicks']})",
                "current": cp["clicks"],
                "previous": pp["clicks"],
            })

    # Impressions drop (ignore low-volume noise)
    if pp["impressions"] >= ALERT_IMPRESSIONS_ABS_MIN:
        imp_drop = ((pp["impressions"] - cp["impressions"]) / pp["impressions"]) * 100
        if imp_drop > ALERT_IMPRESSIONS_DROP_PCT:
            alerts.append({
                "site": site_name,
                "type": "impressions_drop",
                "severity": "high" if imp_drop > 50 else "medium",
                "message": f"Impressions dropped {imp_drop:.1f}% ({pp['impressions']} → {cp['impressions']})",
                "current": cp["impressions"],
                "previous": pp["impressions"],
            })

    # Position drop (only if we have enough data)
    if pp["impressions"] >= ALERT_POSITION_MIN_IMPRESSIONS and pp["position"] > 0:
        pos_delta = cp["position"] - pp["position"]
        if pos_delta > ALERT_POSITION_DROP:
            alerts.append({
                "site": site_name,
                "type": "position_drop",
                "severity": "high" if pos_delta > 10 else "medium",
                "message": f"Average position dropped {pos_delta:.1f} positions ({pp['position']} → {cp['position']})",
                "current": cp["position"],
                "previous": pp["position"],
            })

    # Low CTR pages
    for p in current["pages"]:
        if p["impressions"] >= ALERT_CTR_IMPRESSIONS_MIN and p["ctr"] < ALERT_CTR_LOW_PCT:
            alerts.append({
                "site": site_name,
                "type": "low_ctr",
                "severity": "low",
                "message": f"CTR {p['ctr']}% on page with {p['impressions']} impressions",
                "page": p["page"],
                "current": p["ctr"],
                "previous": None,
            })

    return alerts


def generate_markdown(all_alerts, details):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 🚨 GSC Auto-Alerts — {today}",
        "",
        f"**Période comparée :** {details['days']} jours (actuel) vs {details['days']} jours (précédent)",
        f"**Total alertes :** {len(all_alerts)}",
        "",
        "---",
        "",
    ]

    if not all_alerts:
        lines.append("✅ Aucune alerte détectée. Tous les sites sont stables.")
        lines.append("")
    else:
        # Summary by severity
        sev_counts = {"high": 0, "medium": 0, "low": 0}
        for a in all_alerts:
            sev_counts[a["severity"]] = sev_counts.get(a["severity"], 0) + 1
        lines.append("## Résumé par sévérité")
        lines.append("")
        lines.append(f"- 🔴 High: {sev_counts['high']}")
        lines.append(f"- 🟠 Medium: {sev_counts['medium']}")
        lines.append(f"- 🟡 Low: {sev_counts['low']}")
        lines.append("")

        # Group by site
        by_site = {}
        for a in all_alerts:
            by_site.setdefault(a["site"], []).append(a)

        for site_name, alerts in by_site.items():
            lines.append(f"## {site_name}")
            lines.append("")
            lines.append("| Type | Sévérité | Message | Valeur | Précédent |")
            lines.append("|------|----------|---------|--------|-----------|")
            for a in alerts:
                prev_val = a.get("previous") if a.get("previous") is not None else "—"
                lines.append(
                    f"| {a['type']} | {a['severity']} | {a['message']} | {a['current']} | {prev_val} |"
                )
            lines.append("")

    lines.extend([
        "---",
        "",
        "*Rapport généré automatiquement*",
        "",
    ])
    return "\n".join(lines)


def send_notification(title, subtitle, message):
    script = (
        f'display notification "{message}" '
        f'with title "{title}" '
        f'subtitle "{subtitle}" sound name "Glass"'
    )
    os.system(f"osascript -e '{script}' 2>/dev/null")


def main():
    parser = argparse.ArgumentParser(description="GSC Auto-Alerts Monitor")
    parser.add_argument("--days", type=int, default=28, help="Number of days to analyze")
    args = parser.parse_args()
    days = args.days

    print("=" * 60)
    print("GSC Auto-Alerts Monitor")
    print("=" * 60)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("\n🔌 Connecting to GSC API...")
    try:
        service = get_gsc_service()
        print("✅ Connected")
    except Exception as e:
        print(f"❌ Auth failed: {e}")
        sys.exit(1)

    all_alerts = []
    detail_data = {"days": days, "sites": []}

    for site in SITES:
        print(f"\n📡 {site['name']}")
        try:
            current = fetch_period(service, site, days)
            previous = fetch_period(service, site, days)  # same call but we'll adjust dates manually
        except Exception as e:
            print(f"  ❌ Failed to fetch data: {e}")
            continue

        # Adjust previous period dates manually (re-fetch with correct offset)
        end_prev = datetime.now() - timedelta(days=3 + days)
        start_prev = end_prev - timedelta(days=days - 1)
        start_prev_s = gsc_date(start_prev)
        end_prev_s = gsc_date(end_prev)
        prev_overall = query_gsc(service, site["url"], start_prev_s, end_prev_s)
        prev_perf = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        if prev_overall:
            row = prev_overall[0]
            prev_perf = {
                "clicks": int(row["clicks"]),
                "impressions": int(row["impressions"]),
                "ctr": round(row["ctr"] * 100, 2),
                "position": round(row["position"], 2),
            }
        prev_pages = query_gsc(service, site["url"], start_prev_s, end_prev_s, dimensions=["page"], row_limit=5000)
        prev_page_data = []
        for r in prev_pages:
            prev_page_data.append({
                "page": r["keys"][0],
                "clicks": int(r["clicks"]),
                "impressions": int(r["impressions"]),
                "ctr": round(r["ctr"] * 100, 2),
                "position": round(r["position"], 2),
            })
        previous = {
            "start": start_prev_s,
            "end": end_prev_s,
            "performance": prev_perf,
            "pages": prev_page_data,
        }

        print(f"  Current: {current['start']} → {current['end']} | Clicks: {current['performance']['clicks']}")
        print(f"  Previous: {previous['start']} → {previous['end']} | Clicks: {previous['performance']['clicks']}")

        alerts = check_alerts(current, previous, site["name"])
        if alerts:
            print(f"  🚨 {len(alerts)} alert(s) triggered")
            for a in alerts:
                print(f"     - [{a['severity']}] {a['message']}")
            all_alerts.extend(alerts)
        else:
            print(f"  ✅ No alerts")

        detail_data["sites"].append({
            "name": site["name"],
            "url": site["url"],
            "current": current,
            "previous": previous,
            "alerts": alerts,
        })

    # Generate reports
    today = datetime.now().strftime("%Y-%m-%d")
    md = generate_markdown(all_alerts, detail_data)
    md_path = REPORTS_DIR / f"gsc-alerts-{today}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"\n✅ Report saved: {md_path}")

    json_path = REPORTS_DIR / "gsc-alerts-latest.json"
    json_path.write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(),
            "days": days,
            "total_alerts": len(all_alerts),
            "alerts": all_alerts,
            "details": detail_data["sites"],
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"✅ JSON snapshot saved: {json_path}")

    if all_alerts:
        msg = f"{len(all_alerts)} alerte(s) GSC détectée(s)"
        send_notification("GSC Auto-Alerts", f"Rapport du {today}", msg)
        print("🔔 Notification sent")
    else:
        send_notification("GSC Auto-Alerts", f"Rapport du {today}", "Aucune alerte détectée")
        print("🔔 Notification sent (all clear)")

    print("\n✨ Done")


if __name__ == "__main__":
    main()
