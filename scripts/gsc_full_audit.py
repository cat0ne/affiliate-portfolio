#!/usr/bin/env python3
"""
GSC Full Audit — Performance + Sitemaps for all 12 sites
"""

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
    {"url": "sc-domain:brewmance.fr", "name": "Brewmance", "local": "cafe"},
    {"url": "sc-domain:matelas-expert.fr", "name": "Matelas", "local": "matelas"},
    {"url": "sc-domain:top-aspirateur.fr", "name": "Top-Aspirateur", "local": "aspirateur"},
    {"url": "sc-domain:pixinstant.com", "name": "PixInstant", "local": "pixinstant"},
    {"url": "sc-domain:bureau-expert.fr", "name": "Bureau-Expert", "local": "bureau"},
    {"url": "sc-domain:ikasia.ai", "name": "Ikasia", "local": "ikasia/ikasia/ikasia-website"},
    {"url": "sc-domain:mon-instant-photo.fr", "name": "Mon-Instant-Photo", "local": "mon-instant-photo"},
    {"url": "sc-domain:weloveinstant.com", "name": "WeLoveInstant", "local": None},
    {"url": "sc-domain:celestiastro.com", "name": "CelestiAstro", "local": None},
    {"url": "sc-domain:airpurifyhq.com", "name": "AirPurifyHQ", "local": None},
    {"url": "sc-domain:safehivehq.com", "name": "SafeHiveHQ", "local": None},
    {"url": "sc-domain:pawhivehq.com", "name": "PawHiveHQ", "local": None},
]

ALERT_CLICKS_DROP_PCT = 20
# Raised from 5 → 10 (2026-05-11) after CelestiAstro 5→0 triage showed
# 100% drops are routine at <10 clicks/week base. Below this floor the
# alert is pure sampling noise — page indexed at pos 13, CTR ~0.5%, so
# 0 clicks on 316 impressions is well within variance.
ALERT_CLICKS_ABS_MIN = 10
ALERT_POSITION_DROP = 5
ALERT_POSITION_MIN_IMPRESSIONS = 50
ALERT_IMPRESSIONS_DROP_PCT = 30
ALERT_IMPRESSIONS_ABS_MIN = 200
ALERT_CTR_LOW_PCT = 0.5
ALERT_CTR_IMPRESSIONS_MIN = 1000


def gsc_date(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def get_gsc_service():
    creds = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH),
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    return build("webmasters", "v3", credentials=creds, cache_discovery=False)


def query_gsc(service, site_url, start_date, end_date, dimensions=None, row_limit=25000):
    body = {"startDate": start_date, "endDate": end_date, "rowLimit": row_limit}
    if dimensions:
        body["dimensions"] = dimensions
    try:
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return response.get("rows", [])
    except HttpError as e:
        print(f"  ⚠️  API error ({e.resp.status}) for {site_url}: {e}")
        return []


def fetch_sitemaps(service, site_url):
    """Fetch sitemap status from GSC"""
    try:
        response = service.sitemaps().list(siteUrl=site_url).execute()
        return response.get("sitemap", [])
    except HttpError as e:
        print(f"  ⚠️  Sitemap API error ({e.resp.status}): {e}")
        return []


def check_alerts(current, previous, site_name):
    alerts = []
    cp = current["performance"]
    pp = previous["performance"]

    # Clicks drop
    if pp["clicks"] >= ALERT_CLICKS_ABS_MIN:
        clicks_drop = ((pp["clicks"] - cp["clicks"]) / pp["clicks"]) * 100
        if clicks_drop > ALERT_CLICKS_DROP_PCT:
            alerts.append({
                "site": site_name, "type": "clicks_drop",
                "severity": "high" if clicks_drop > 50 else "medium",
                "message": f"Clicks dropped {clicks_drop:.1f}% ({pp['clicks']} → {cp['clicks']})",
                "current": cp["clicks"], "previous": pp["clicks"],
            })

    # Impressions drop
    if pp["impressions"] >= ALERT_IMPRESSIONS_ABS_MIN:
        imp_drop = ((pp["impressions"] - cp["impressions"]) / pp["impressions"]) * 100
        if imp_drop > ALERT_IMPRESSIONS_DROP_PCT:
            alerts.append({
                "site": site_name, "type": "impressions_drop",
                "severity": "high" if imp_drop > 50 else "medium",
                "message": f"Impressions dropped {imp_drop:.1f}% ({pp['impressions']} → {cp['impressions']})",
                "current": cp["impressions"], "previous": pp["impressions"],
            })

    # Position drop
    if pp["impressions"] >= ALERT_POSITION_MIN_IMPRESSIONS and pp["position"] > 0:
        pos_delta = cp["position"] - pp["position"]
        if pos_delta > ALERT_POSITION_DROP:
            alerts.append({
                "site": site_name, "type": "position_drop",
                "severity": "high" if pos_delta > 10 else "medium",
                "message": f"Avg position dropped {pos_delta:.1f} ({pp['position']} → {cp['position']})",
                "current": cp["position"], "previous": pp["position"],
            })

    # Low CTR pages
    for p in current["pages"]:
        if p["impressions"] >= ALERT_CTR_IMPRESSIONS_MIN and p["ctr"] < ALERT_CTR_LOW_PCT:
            alerts.append({
                "site": site_name, "type": "low_ctr", "severity": "low",
                "message": f"CTR {p['ctr']}% on {p['impressions']} impressions",
                "page": p["page"], "current": p["ctr"], "previous": None,
            })

    return alerts


def main():
    days = 7  # Last 7 days vs previous 7 days
    print("=" * 70)
    print(f"GSC Full Audit — {days} days vs previous {days} days")
    print("=" * 70)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    service = get_gsc_service()
    print("✅ Connected to GSC API\n")

    all_alerts = []
    all_sitemap_issues = []
    all_low_ctr = []
    site_reports = []

    end_curr = datetime.now() - timedelta(days=3)
    start_curr = end_curr - timedelta(days=days - 1)
    end_prev = end_curr - timedelta(days=days)
    start_prev = end_prev - timedelta(days=days - 1)

    for site in SITES:
        print(f"📡 {site['name']} ({site['url']})")

        # --- Performance ---
        curr_overall = query_gsc(service, site["url"], gsc_date(start_curr), gsc_date(end_curr))
        prev_overall = query_gsc(service, site["url"], gsc_date(start_prev), gsc_date(end_prev))

        curr_perf = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        if curr_overall:
            r = curr_overall[0]
            curr_perf = {"clicks": int(r["clicks"]), "impressions": int(r["impressions"]),
                         "ctr": round(r["ctr"] * 100, 2), "position": round(r["position"], 2)}

        prev_perf = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        if prev_overall:
            r = prev_overall[0]
            prev_perf = {"clicks": int(r["clicks"]), "impressions": int(r["impressions"]),
                         "ctr": round(r["ctr"] * 100, 2), "position": round(r["position"], 2)}

        curr_pages = query_gsc(service, site["url"], gsc_date(start_curr), gsc_date(end_curr), dimensions=["page"], row_limit=5000)
        prev_pages = query_gsc(service, site["url"], gsc_date(start_prev), gsc_date(end_prev), dimensions=["page"], row_limit=5000)

        curr_page_data = [{"page": r["keys"][0], "clicks": int(r["clicks"]), "impressions": int(r["impressions"]),
                           "ctr": round(r["ctr"] * 100, 2), "position": round(r["position"], 2)} for r in curr_pages]
        prev_page_data = [{"page": r["keys"][0], "clicks": int(r["clicks"]), "impressions": int(r["impressions"]),
                           "ctr": round(r["ctr"] * 100, 2), "position": round(r["position"], 2)} for r in prev_pages]

        current = {"start": gsc_date(start_curr), "end": gsc_date(end_curr), "performance": curr_perf, "pages": curr_page_data}
        previous = {"start": gsc_date(start_prev), "end": gsc_date(end_prev), "performance": prev_perf, "pages": prev_page_data}

        alerts = check_alerts(current, previous, site["name"])

        # Collect low CTR for CTR optimization
        for p in curr_page_data:
            if p["impressions"] >= 30 and p["ctr"] == 0 and p["position"] < 20:
                all_low_ctr.append({"site": site["name"], "page": p["page"], "impressions": p["impressions"], "position": p["position"]})

        # --- Sitemaps ---
        sitemaps = fetch_sitemaps(service, site["url"])
        sitemap_issues = []
        for sm in sitemaps:
            errors = int(sm.get("errors", 0)) if str(sm.get("errors", "0")).isdigit() else 0
            warnings = int(sm.get("warnings", 0)) if str(sm.get("warnings", "0")).isdigit() else 0
            if errors > 0 or warnings > 0:
                sitemap_issues.append({
                    "path": sm.get("path"), "errors": errors, "warnings": warnings,
                    "isPending": sm.get("isPending", False), "isSitemapsIndex": sm.get("isSitemapsIndex", False),
                    "lastSubmitted": sm.get("lastSubmitted"), "lastDownloaded": sm.get("lastDownloaded"),
                })
        if sitemap_issues:
            all_sitemap_issues.extend([{"site": site["name"], **si} for si in sitemap_issues])

        print(f"  Clicks: {curr_perf['clicks']} (prev: {prev_perf['clicks']}) | Impressions: {curr_perf['impressions']} (prev: {prev_perf['impressions']}) | Position: {curr_perf['position']}")
        if alerts:
            print(f"  🚨 {len(alerts)} alert(s)")
            for a in alerts:
                print(f"     - [{a['severity']}] {a['message']}")
            all_alerts.extend(alerts)
        else:
            print(f"  ✅ No performance alerts")

        if sitemap_issues:
            print(f"  ⚠️  Sitemap issues: {len(sitemap_issues)}")
            for si in sitemap_issues:
                print(f"     - {si['path']}: {si['errors']} errors, {si['warnings']} warnings")
        else:
            print(f"  ✅ Sitemap OK")

        site_reports.append({
            "name": site["name"], "url": site["url"], "local": site["local"],
            "current": current, "previous": previous, "alerts": alerts,
            "sitemaps": sitemaps, "sitemap_issues": sitemap_issues,
        })
        print()

    # --- Generate Report ---
    today = datetime.now().strftime("%Y-%m-%d")
    report = {
        "generated_at": datetime.now().isoformat(),
        "days": days,
        "total_alerts": len(all_alerts),
        "total_sitemap_issues": len(all_sitemap_issues),
        "total_low_ctr_pages": len(all_low_ctr),
        "alerts": all_alerts,
        "sitemap_issues": all_sitemap_issues,
        "low_ctr_pages": all_low_ctr,
        "sites": site_reports,
    }

    json_path = REPORTS_DIR / f"gsc-full-audit-{today}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ Full audit saved: {json_path}")

    # Print summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total performance alerts: {len(all_alerts)}")
    print(f"Total sitemap issues: {len(all_sitemap_issues)}")
    print(f"Total 0% CTR pages (position < 20, impressions >= 30): {len(all_low_ctr)}")

    if all_alerts:
        print("\n🔴 Performance Alerts by Site:")
        by_site = {}
        for a in all_alerts:
            by_site.setdefault(a["site"], []).append(a)
        for site_name, alerts in by_site.items():
            print(f"  {site_name}: {len(alerts)} alerts")

    if all_sitemap_issues:
        print("\n🟠 Sitemap Issues:")
        for si in all_sitemap_issues:
            print(f"  {si['site']}: {si['path']} — {si['errors']} errors, {si['warnings']} warnings")

    if all_low_ctr:
        print(f"\n🟡 Top 10 Low CTR Pages ( CTR = 0%, position < 20 ):")
        for p in sorted(all_low_ctr, key=lambda x: x["impressions"], reverse=True)[:10]:
            print(f"  {p['site']}: {p['page']} — {p['impressions']} impressions, position ~{p['position']:.1f}")

    return report


if __name__ == "__main__":
    main()
