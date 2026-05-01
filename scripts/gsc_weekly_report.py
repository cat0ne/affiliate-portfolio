#!/usr/bin/env python3
"""
GSC Weekly Monitoring Report
Compare current 28-day GSC performance against baseline_post_deploy.json
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Configuration ───────────────────────────────────────────────────────────
BASE_DIR = Path("/Users/gho/Documents/affiliation-sites")
BASELINE_PATH = BASE_DIR / "baseline_post_deploy.json"
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

DAYS = 28


# ── Helpers ─────────────────────────────────────────────────────────────────

def load_json(path: Path):
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def pct_delta(current, previous):
    if previous == 0 or previous is None:
        return 100.0 if current and current > 0 else 0.0
    return round(((current - previous) / previous) * 100, 2)


def abs_delta(current, previous):
    if current is None:
        current = 0
    if previous is None:
        previous = 0
    return round(current - previous, 2)


def format_num(n):
    if n is None:
        return "0"
    if isinstance(n, float):
        return f"{n:,.2f}"
    return f"{n:,}"


def gsc_date(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


# ── GSC API ─────────────────────────────────────────────────────────────────

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


def fetch_site_data(service, site):
    end = datetime.now() - timedelta(days=3)  # GSC data lag
    start = end - timedelta(days=DAYS - 1)
    start_s = gsc_date(start)
    end_s = gsc_date(end)

    print(f"  Fetching {site['name']} ({start_s} → {end_s}) ...")

    # Overall
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

    # Top pages
    pages = query_gsc(service, site["url"], start_s, end_s, dimensions=["page"], row_limit=50)
    top_pages = []
    for r in pages:
        top_pages.append({
            "page": r["keys"][0],
            "clicks": int(r["clicks"]),
            "impressions": int(r["impressions"]),
            "ctr": round(r["ctr"] * 100, 2),
            "position": round(r["position"], 2),
        })

    # Top queries
    queries = query_gsc(service, site["url"], start_s, end_s, dimensions=["query"], row_limit=50)
    top_queries = []
    for r in queries:
        top_queries.append({
            "query": r["keys"][0],
            "clicks": int(r["clicks"]),
            "impressions": int(r["impressions"]),
            "ctr": round(r["ctr"] * 100, 2),
            "position": round(r["position"], 2),
        })

    # Devices
    devices = query_gsc(service, site["url"], start_s, end_s, dimensions=["device"], row_limit=10)
    device_data = []
    for r in devices:
        device_data.append({
            "device": r["keys"][0],
            "clicks": int(r["clicks"]),
            "impressions": int(r["impressions"]),
            "ctr": round(r["ctr"] * 100, 2),
            "position": round(r["position"], 2),
        })

    # Countries
    countries = query_gsc(service, site["url"], start_s, end_s, dimensions=["country"], row_limit=10)
    country_data = []
    for r in countries:
        country_data.append({
            "country": r["keys"][0].upper(),
            "clicks": int(r["clicks"]),
            "impressions": int(r["impressions"]),
            "ctr": round(r["ctr"] * 100, 2),
            "position": round(r["position"], 2),
        })

    return {
        "url": site["url"],
        "name": site["name"],
        "period": {"start": start_s, "end": end_s},
        "performance": perf,
        "top_pages": top_pages,
        "top_queries": top_queries,
        "devices": device_data,
        "countries": country_data,
    }


# ── Comparison logic ────────────────────────────────────────────────────────

def compare_with_baseline(current, baseline_sites):
    baseline = next((s for s in baseline_sites if s["url"] == current["url"]), None)
    if not baseline:
        return None

    bp = baseline.get("performance_current", {})
    cp = current["performance"]

    return {
        "clicks_delta": abs_delta(cp["clicks"], bp.get("clicks", 0)),
        "clicks_pct": pct_delta(cp["clicks"], bp.get("clicks", 0)),
        "impressions_delta": abs_delta(cp["impressions"], bp.get("impressions", 0)),
        "impressions_pct": pct_delta(cp["impressions"], bp.get("impressions", 0)),
        "ctr_delta": round(cp["ctr"] - bp.get("ctr", 0) * 100 if isinstance(bp.get("ctr"), float) and bp.get("ctr", 0) < 1 else cp["ctr"] - bp.get("ctr", 0), 2),
        "position_delta": round(bp.get("position", 0) - cp["position"], 2),
    }


def find_growth_decline(current_pages, baseline_sites, site_url):
    baseline = next((s for s in baseline_sites if s["url"] == site_url), None)
    if not baseline:
        return [], []
    baseline_pages = {p["keys"][0]: p for p in baseline.get("rich_analysis", {}).get("top_pages", [])}

    growth = []
    decline = []
    for p in current_pages:
        url = p["page"]
        old = baseline_pages.get(url)
        if old:
            old_clicks = int(old.get("clicks", 0))
            new_clicks = p["clicks"]
            delta = new_clicks - old_clicks
            if delta > 0:
                growth.append({"page": url, "delta": delta, "old": old_clicks, "new": new_clicks})
            elif delta < 0:
                decline.append({"page": url, "delta": delta, "old": old_clicks, "new": new_clicks})
        else:
            if p["clicks"] > 0:
                growth.append({"page": url, "delta": p["clicks"], "old": 0, "new": p["clicks"], "is_new": True})

    growth.sort(key=lambda x: x["delta"], reverse=True)
    decline.sort(key=lambda x: x["delta"])
    return growth[:10], decline[:10]


def find_opportunities(queries):
    ops = []
    for q in queries:
        if 11 <= q["position"] <= 20 and q["impressions"] >= 5:
            ops.append(q)
    ops.sort(key=lambda x: x["impressions"], reverse=True)
    return ops[:15]


def find_ctr_problems(queries):
    probs = []
    for q in queries:
        if q["position"] <= 15 and q["ctr"] < 1.0 and q["impressions"] >= 20:
            probs.append(q)
    probs.sort(key=lambda x: x["impressions"], reverse=True)
    return probs[:10]


def generate_device_breakdown(devices):
    """Return markdown lines for detailed device analysis."""
    lines = []
    by_device = {d["device"].lower(): d for d in devices}
    desktop = by_device.get("desktop", {})
    mobile = by_device.get("mobile", {})

    lines.append("### 📱 Device Breakdown")
    lines.append("")
    lines.append("| Device | Impressions | Clicks | CTR | Position |")
    lines.append("|--------|-------------|--------|-----|----------|")
    for d in devices:
        lines.append(f"| {d['device']} | {d['impressions']} | {d['clicks']} | {d['ctr']}% | {d['position']} |")
    lines.append("")

    if desktop and mobile:
        # Delta mobile vs desktop
        imp_delta = mobile.get("impressions", 0) - desktop.get("impressions", 0)
        clk_delta = mobile.get("clicks", 0) - desktop.get("clicks", 0)
        ctr_delta = round(mobile.get("ctr", 0) - desktop.get("ctr", 0), 2)
        pos_delta = round(mobile.get("position", 0) - desktop.get("position", 0), 2)

        lines.append("#### 📊 Delta Mobile vs Desktop")
        lines.append("")
        lines.append("| Métrique | Mobile | Desktop | Δ (Mobile – Desktop) |")
        lines.append("|----------|--------|---------|----------------------|")
        lines.append(f"| Impressions | {mobile.get('impressions', 0)} | {desktop.get('impressions', 0)} | {imp_delta:+} |")
        lines.append(f"| Clicks | {mobile.get('clicks', 0)} | {desktop.get('clicks', 0)} | {clk_delta:+} |")
        lines.append(f"| CTR | {mobile.get('ctr', 0)}% | {desktop.get('ctr', 0)}% | {ctr_delta:+.2f}pp |")
        lines.append(f"| Position | {mobile.get('position', 0)} | {desktop.get('position', 0)} | {pos_delta:+.2f} |")
        lines.append("")

        # Recommendations
        recs = []
        mob_ctr = mobile.get("ctr", 0)
        desk_ctr = desktop.get("ctr", 0)
        if desk_ctr > 0 and mob_ctr > desk_ctr * 5:
            recs.append("Mobile CTR 5× desktop → investir CRO mobile (boutons plus grands, checkout simplifié).")
        elif mob_ctr < desk_ctr * 0.5:
            recs.append("Mobile CTR < 50% du desktop → vérifier responsive, Core Web Vitals mobile, popups intrusives.")
        if mobile.get("position", 0) > desktop.get("position", 0) + 3:
            recs.append("Position mobile nettement plus faible → optimiser le contenu au-dessus de la ligne de flottaison mobile.")
        if mobile.get("impressions", 0) > desktop.get("impressions", 0) * 2:
            recs.append("Volume d'impressions majoritairement mobile → prioriser le format AMP / responsive.")
        if not recs:
            recs.append("Répartition device équilibrée — continuer la surveillance.")

        lines.append("#### 💡 Recommandations device")
        lines.append("")
        for rec in recs:
            lines.append(f"- {rec}")
        lines.append("")

    return lines


# ── Markdown report ─────────────────────────────────────────────────────────

def generate_markdown(results, baseline_data):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 📊 Rapport GSC Hebdomadaire — {today}",
        "",
        f"**Période analysée :** 28 jours  ",
        f"**Sites :** Brewmance, Matelas, Top-Aspirateur, PixInstant  ",
        f"**Baseline :** {baseline_data.get('generated_at', 'N/A')[:10] if baseline_data else 'N/A'}",
        "",
        "---",
        "",
    ]

    total_clicks = sum(r["performance"]["clicks"] for r in results)
    total_impressions = sum(r["performance"]["impressions"] for r in results)

    lines.extend([
        "## 🏆 Vue d'ensemble",
        "",
        f"- **Clicks total :** {format_num(total_clicks)}",
        f"- **Impressions total :** {format_num(total_impressions)}",
        "",
        "### Tableau comparatif Baseline → Actuel",
        "",
        "| Site | Clicks | Δ Clicks | Impressions | Δ Imp | Position | Δ Pos | CTR | Δ CTR |",
        "|------|--------|----------|-------------|-------|----------|-------|-----|-------|",
    ])

    baseline_sites = baseline_data.get("sites", []) if baseline_data else []
    notif_parts = []

    for r in results:
        p = r["performance"]
        comp = compare_with_baseline(r, baseline_sites)
        if comp:
            c = comp
            sign_c = "📈" if c["clicks_delta"] >= 0 else "📉"
            sign_i = "📈" if c["impressions_delta"] >= 0 else "📉"
            sign_p = "📈" if c["position_delta"] > 0 else "📉" if c["position_delta"] < 0 else "➡️"
            sign_ctr = "📈" if c["ctr_delta"] >= 0 else "📉"
            lines.append(
                f"| {r['name']} | {format_num(p['clicks'])} | {sign_c} {c['clicks_delta']:+} ({c['clicks_pct']:+.1f}%) | "
                f"{format_num(p['impressions'])} | {sign_i} {c['impressions_delta']:+} ({c['impressions_pct']:+.1f}%) | "
                f"{p['position']} | {sign_p} {c['position_delta']:+.2f} | "
                f"{p['ctr']}% | {sign_ctr} {c['ctr_delta']:+.2f}pp |"
            )
            notif_parts.append(f"{r['name']}: {p['clicks']} clics ({c['clicks_delta']:+})")
        else:
            lines.append(
                f"| {r['name']} | {format_num(p['clicks'])} | — | {format_num(p['impressions'])} | — | "
                f"{p['position']} | — | {p['ctr']}% | — |"
            )
            notif_parts.append(f"{r['name']}: {p['clicks']} clics")

    lines.append("")

    for r in results:
        lines.extend([
            f"## {r['name']}",
            "",
            f"**URL :** `{r['url']}`  ",
            f"**Période :** {r['period']['start']} → {r['period']['end']}",
            "",
        ])

        # Pages croissance / déclin
        growth, decline = find_growth_decline(r["top_pages"], baseline_sites, r["url"])
        if growth:
            lines.append("### 📈 Pages en croissance")
            lines.append("")
            lines.append("| Page | Avant | Maintenant | Δ |")
            lines.append("|------|-------|------------|---|")
            for g in growth:
                new_flag = " 🆕" if g.get("is_new") else ""
                lines.append(f"| `{g['page'][:70]}`{new_flag} | {g['old']} | {g['new']} | **+{g['delta']}** |")
            lines.append("")

        if decline:
            lines.append("### 📉 Pages en déclin")
            lines.append("")
            lines.append("| Page | Avant | Maintenant | Δ |")
            lines.append("|------|-------|------------|---|")
            for d in decline:
                lines.append(f"| `{d['page'][:70]}` | {d['old']} | {d['new']} | **{d['delta']}** |")
            lines.append("")

        # Opportunités
        ops = find_opportunities(r["top_queries"])
        if ops:
            lines.append("### 🎯 Opportunités (position 11–20)")
            lines.append("")
            lines.append("| Requête | Position | Impressions | Clicks | CTR |")
            lines.append("|---------|----------|-------------|--------|-----|")
            for o in ops:
                lines.append(f"| `{o['query'][:60]}` | {o['position']} | {o['impressions']} | {o['clicks']} | {o['ctr']}% |")
            lines.append("")

        # CTR problems
        ctr_probs = find_ctr_problems(r["top_queries"])
        if ctr_probs:
            lines.append("### ⚠️ Problèmes CTR (position ≤15, CTR <1%)")
            lines.append("")
            lines.append("| Requête | Position | Impressions | CTR |")
            lines.append("|---------|----------|-------------|-----|")
            for c in ctr_probs:
                lines.append(f"| `{c['query'][:60]}` | {c['position']} | {c['impressions']} | {c['ctr']}% |")
            lines.append("")

        # Devices
        if r["devices"]:
            lines.extend(generate_device_breakdown(r["devices"]))

        # Top countries
        if r["countries"]:
            lines.append("### 🌍 Top pays")
            lines.append("")
            lines.append("| Pays | Clicks | Impressions | CTR | Position |")
            lines.append("|------|--------|-------------|-----|----------|")
            for c in r["countries"][:5]:
                lines.append(f"| {c['country']} | {c['clicks']} | {c['impressions']} | {c['ctr']}% | {c['position']} |")
            lines.append("")

    # Recommandations globales
    lines.extend([
        "---",
        "",
        "## 💡 Recommandations actions",
        "",
    ])

    for r in results:
        ops = find_opportunities(r["top_queries"])
        ctr_probs = find_ctr_problems(r["top_queries"])
        growth, _ = find_growth_decline(r["top_pages"], baseline_sites, r["url"])

        recs = []
        if ops:
            recs.append(f"- **{r['name']}** : {len(ops)} requête(s) en page 2 → optimiser le contenu (H2, FAQ, liens internes) pour viser la page 1.")
        if ctr_probs:
            recs.append(f"- **{r['name']}** : {len(ctr_probs)} requête(s) avec CTR faible → réécrire les title tags et meta descriptions.")
        if not ops and not ctr_probs and not growth:
            recs.append(f"- **{r['name']}** : Pas de données significatives encore — continuer la production de contenu.")
        if growth:
            recs.append(f"- **{r['name']}** : {len(growth)} page(s) en croissance → doubler les liens internes vers ces pages.")

        lines.extend(recs)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*Rapport généré automatiquement le {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")

    return "\n".join(lines), notif_parts


# ── macOS notification ──────────────────────────────────────────────────────

def send_notification(title, subtitle, message):
    script = (
        f'display notification "{message}" '
        f'with title "{title}" '
        f'subtitle "{subtitle}" sound name "Glass"'
    )
    os.system(f"osascript -e '{script}' 2>/dev/null")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("GSC Weekly Report Generator")
    print("=" * 60)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    baseline_data = load_json(BASELINE_PATH)
    if baseline_data:
        print(f"✅ Baseline loaded: {BASELINE_PATH.name}")
    else:
        print(f"⚠️  Baseline not found at {BASELINE_PATH}")

    print("\n🔌 Connecting to GSC API...")
    try:
        service = get_gsc_service()
        print("✅ Connected")
    except Exception as e:
        print(f"❌ Auth failed: {e}")
        sys.exit(1)

    results = []
    for site in SITES:
        print(f"\n📡 {site['name']}")
        try:
            data = fetch_site_data(service, site)
            results.append(data)
        except Exception as e:
            print(f"  ❌ Failed: {e}")

    print("\n📝 Generating report...")
    markdown, notif_parts = generate_markdown(results, baseline_data)

    today = datetime.now().strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"weekly_{today}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(markdown)
    print(f"✅ Report saved: {report_path}")

    # Notification
    total_clicks = sum(r["performance"]["clicks"] for r in results)
    total_impressions = sum(r["performance"]["impressions"] for r in results)
    notif_msg = f"Clicks: {total_clicks} | Impressions: {total_impressions}"
    send_notification(
        "GSC Weekly Report",
        f"Rapport du {today}",
        notif_msg,
    )
    print("🔔 Notification sent")
    print("\n✨ Done")


if __name__ == "__main__":
    main()
