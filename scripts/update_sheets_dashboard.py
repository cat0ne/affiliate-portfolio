#!/usr/bin/env python3
"""
Affiliation Dashboard - GSC
Auto-updating Google Sheets dashboard for 4 affiliate sites.

Usage:
    python update_sheets_dashboard.py
"""

import json
import os
from datetime import datetime
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "gsc-credentials.json")
BASELINE_PATH = os.path.join(BASE_DIR, "baseline_post_deploy.json")
SHEET_TITLE = "Affiliation Dashboard - GSC"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SITE_NAMES = {
    "sc-domain:brewmance.fr": "Brewmance",
    "sc-domain:matelas-expert.fr": "Matelas",
    "sc-domain:top-aspirateur.fr": "Aspirateur",
    "sc-domain:pixinstant.com": "PixInstant",
    "sc-domain:bureau-expert.fr": "Bureau-Expert",
    "sc-domain:ikasia.ai": "Ikasia",
}

ORDERED_SITES = ["Brewmance", "Matelas", "Aspirateur", "PixInstant", "Bureau-Expert", "Ikasia"]


def get_client():
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def get_or_create_sheet(client, title):
    try:
        sheet = client.open(title)
        print(f"Sheet '{title}' already exists. Updating...")
        return sheet
    except gspread.SpreadsheetNotFound:
        sheet = client.create(title)
        print(f"Sheet '{title}' created.")
        return sheet


def setup_worksheets(sheet):
    expected = ["Overview", "Evolution", "Opportunities", "Top Pages", "Alerts", "Content Decay"]
    existing = [ws.title for ws in sheet.worksheets()]

    for title in expected:
        if title not in existing:
            sheet.add_worksheet(title=title, rows=2000, cols=20)
            print(f"Worksheet '{title}' created.")

    # Remove default Sheet1 if it exists and is empty
    if "Sheet1" in existing:
        try:
            ws = sheet.worksheet("Sheet1")
            # Only remove if it's the only sheet besides our expected ones
            # Actually, just remove it safely
            sheet.del_worksheet(ws)
            print("Removed default Sheet1.")
        except Exception as e:
            print(f"Could not remove Sheet1: {e}")

    return sheet


def prepare_data():
    with open(BASELINE_PATH, encoding="utf-8") as f:
        baseline = json.load(f)

    data = {}
    for site in baseline["sites"]:
        url = site["url"]
        if url not in SITE_NAMES:
            continue
        name = SITE_NAMES[url]
        perf = site["performance_current"]
        rich = site.get("rich_analysis", {})
        data[name] = {
            "clicks": perf["clicks"],
            "impressions": perf["impressions"],
            "ctr": perf["ctr"],
            "position": perf["position"],
            "top_queries": site.get("top_queries", []),
            "top_pages": rich.get("top_pages", []),
            "daily_trend": rich.get("daily_trend", []),
        }
    return data


def aggregate_weekly(daily_trend):
    """Aggregate daily trend into weekly data."""
    weeks = defaultdict(
        lambda: {
            "clicks": 0,
            "impressions": 0,
            "position_weighted": 0.0,
            "position_imp": 0,
            "ctr_weighted": 0.0,
            "ctr_imp": 0,
        }
    )

    for day in daily_trend:
        date_str = day["keys"][0]
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        year, week, _ = dt.isocalendar()
        key = f"{year}-W{week:02d}"

        clicks = day.get("clicks", 0)
        impressions = day.get("impressions", 0)
        position = day.get("position", 0)
        ctr = day.get("ctr", 0)

        weeks[key]["clicks"] += clicks
        weeks[key]["impressions"] += impressions

        if impressions > 0 and position > 0:
            weeks[key]["position_weighted"] += position * impressions
            weeks[key]["position_imp"] += impressions

        if impressions > 0 and ctr > 0:
            weeks[key]["ctr_weighted"] += ctr * impressions
            weeks[key]["ctr_imp"] += impressions

    result = []
    for wk in sorted(weeks.keys()):
        vals = weeks[wk]
        avg_pos = (
            round(vals["position_weighted"] / vals["position_imp"], 2)
            if vals["position_imp"] > 0
            else 0
        )
        avg_ctr = (
            round(vals["ctr_weighted"] / vals["ctr_imp"] * 100, 2)
            if vals["ctr_imp"] > 0
            else 0
        )
        result.append(
            {
                "week": wk,
                "clicks": vals["clicks"],
                "impressions": vals["impressions"],
                "position": avg_pos,
                "ctr": avg_ctr,
            }
        )
    return result


def format_header(ws, range_str):
    ws.format(
        range_str,
        {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.6},
            "horizontalAlignment": "CENTER",
        },
    )


def update_overview(sheet, data):
    ws = sheet.worksheet("Overview")
    ws.clear()
    headers = ["Site", "Clicks (28j)", "Impressions", "Position", "CTR", "Trend"]
    rows = [headers]

    for name in ORDERED_SITES:
        site = data.get(name, {})
        rows.append(
            [
                name,
                site.get("clicks", 0),
                site.get("impressions", 0),
                round(site.get("position", 0), 2),
                f"{site.get('ctr', 0)}%",
                "",  # sparkline formula added after Evolution
            ]
        )

    ws.update("A1", rows, value_input_option="USER_ENTERED")
    format_header(ws, "A1:F1")

    # Auto-resize columns roughly
    ws.columns_auto_resize(0, 6)
    print("Overview updated.")


def update_evolution(sheet, data):
    ws = sheet.worksheet("Evolution")
    ws.clear()
    headers = ["Date", "Site", "Clicks", "Impressions", "Position", "CTR"]
    ws.update("A1", [headers], value_input_option="USER_ENTERED")
    format_header(ws, "A1:F1")

    rows = []
    for name in ORDERED_SITES:
        site = data.get(name, {})
        weekly = aggregate_weekly(site.get("daily_trend", []))
        for w in weekly:
            rows.append(
                [
                    w["week"],
                    name,
                    w["clicks"],
                    w["impressions"],
                    w["position"],
                    f"{w['ctr']}%",
                ]
            )

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    print(f"Evolution updated with {len(rows)} rows.")


def update_opportunities(sheet, data):
    ws = sheet.worksheet("Opportunities")
    ws.clear()
    headers = ["Requête", "Position", "Impressions", "Clicks", "Recommandation"]
    ws.update("A1", [headers], value_input_option="USER_ENTERED")
    format_header(ws, "A1:E1")

    rows = []
    for name in ORDERED_SITES:
        site = data.get(name, {})
        queries = site.get("top_queries", [])
        # Queries in position 11-20 (low-hanging fruit)
        opp = [q for q in queries if 11 <= q.get("position", 0) <= 20]
        opp.sort(key=lambda x: x.get("impressions", 0), reverse=True)
        for q in opp[:20]:
            query_text = q["keys"][0]
            pos = round(q.get("position", 0), 1)
            imp = q.get("impressions", 0)
            clk = q.get("clicks", 0)
            rec = "Optimiser le titre/meta pour grimper en page 1"
            rows.append([query_text, pos, imp, clk, rec])

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    print(f"Opportunities updated with {len(rows)} rows.")


def update_top_pages(sheet, data):
    ws = sheet.worksheet("Top Pages")
    ws.clear()
    headers = ["Site", "Page", "Clicks", "Impressions", "CTR"]
    ws.update("A1", [headers], value_input_option="USER_ENTERED")
    format_header(ws, "A1:E1")

    rows = []
    for name in ORDERED_SITES:
        site = data.get(name, {})
        pages = sorted(
            site.get("top_pages", []), key=lambda x: x.get("clicks", 0), reverse=True
        )
        for p in pages[:20]:
            page_url = p["keys"][0]
            rows.append(
                [
                    name,
                    page_url,
                    p.get("clicks", 0),
                    p.get("impressions", 0),
                    f"{round(p.get('ctr', 0) * 100, 2)}%",
                ]
            )

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    print(f"Top Pages updated with {len(rows)} rows.")


def update_alerts(sheet):
    ws = sheet.worksheet("Alerts")
    ws.clear()
    headers = ["Site", "Type", "Sévérité", "Message", "Valeur", "Précédent"]
    ws.update(range_name="A1", values=[headers], value_input_option="USER_ENTERED")
    format_header(ws, "A1:F1")

    rows = []
    alerts_path = os.path.join(BASE_DIR, "reports", "gsc-alerts-latest.json")
    if os.path.exists(alerts_path):
        with open(alerts_path, encoding="utf-8") as f:
            data = json.load(f)
        for a in data.get("alerts", []):
            rows.append([
                a.get("site", ""),
                a.get("type", ""),
                a.get("severity", ""),
                a.get("message", ""),
                a.get("current", ""),
                a.get("previous") if a.get("previous") is not None else "",
            ])
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    print(f"Alerts updated with {len(rows)} rows.")


def update_content_decay(sheet):
    ws = sheet.worksheet("Content Decay")
    ws.clear()
    headers = ["Site", "Locale", "Titre", "Slug", "Dernière modif", "Jours", "Recommandation"]
    ws.update(range_name="A1", values=[headers], value_input_option="USER_ENTERED")
    format_header(ws, "A1:G1")

    rows = []
    decay_path = os.path.join(BASE_DIR, "reports", "content-decay-latest.json")
    if os.path.exists(decay_path):
        with open(decay_path, encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("items", []):
            rows.append([
                item.get("site", ""),
                item.get("locale", ""),
                item.get("title", ""),
                item.get("slug", ""),
                item.get("last_modified", ""),
                item.get("days_since", 0),
                item.get("recommendation", ""),
            ])
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    print(f"Content Decay updated with {len(rows)} rows.")


def add_sparklines(sheet):
    ws = sheet.worksheet("Overview")
    # Add SPARKLINE formulas in column F referencing Evolution sheet
    # Evolution columns: A=Date, B=Site, C=Clicks
    for i, name in enumerate(ORDERED_SITES, start=2):
        formula = f'=SPARKLINE(FILTER(Evolution!C:C,Evolution!B:B="{name}"),{{"charttype","line";"color","#34A853"}})'
        ws.update_cell(i, 6, formula)
    print("Sparklines added to Overview.")


def share_sheet(sheet):
    """Make readable by anyone with the link."""
    try:
        sheet.share("", perm_type="anyone", role="reader")
        print("Sheet shared publicly (read-only).")
    except Exception as e:
        print(f"Could not share sheet: {e}")


def test_connection(client):
    """Verify that the service account can create a Google Sheet."""
    try:
        # Try to create a temporary spreadsheet to validate write permissions
        temp = client.create("_temp_quota_test")
        client.del_spreadsheet(temp.id)
        return True, "OK"
    except gspread.exceptions.APIError as e:
        error_msg = str(e)
        if "quota" in error_msg.lower() or "storage" in error_msg.lower():
            return False, (
                "❌ Le service account a un quota Google Drive de 0.\n"
                "   → Active la facturation sur le projet GCP, OU\n"
                "   → Crée le Sheet manuellement et partage-le avec :\n"
                f"     {Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES).service_account_email}"
            )
        elif "permission" in error_msg.lower():
            return False, (
                "❌ L'API Google Sheets n'est pas activée pour ce service account.\n"
                "   → Vérifie dans la console GCP que 'Google Sheets API' est activée."
            )
        return False, f"❌ Erreur API Sheets: {error_msg}"
    except Exception as e:
        return False, f"❌ Erreur inattendue: {e}"


def main():
    print("Connecting to Google Sheets...")
    client = get_client()

    ok, msg = test_connection(client)
    if not ok:
        print(msg)
        # Try to use an existing sheet ID if provided
        sheet_id = os.environ.get("SPREADSHEET_ID")
        if not sheet_id:
            print("\n💡 Pour contourner ce problème, crée manuellement un Google Sheet")
            print("   nommé 'Affiliation Dashboard - GSC', partage-le avec le service account")
            print("   en éditeur, puis relance avec :")
            print(f"   SPREADSHEET_ID=<id> python3 {__file__}")
            return None
        else:
            print(f"\n🔄 Using existing SPREADSHEET_ID: {sheet_id}")
            try:
                sheet = client.open_by_key(sheet_id)
            except Exception as e:
                print(f"❌ Impossible d'ouvrir le sheet existant: {e}")
                return None
    else:
        sheet = get_or_create_sheet(client, SHEET_TITLE)

    setup_worksheets(sheet)

    print("Loading baseline data...")
    data = prepare_data()

    # Evolution first so sparklines can reference it
    update_evolution(sheet, data)
    update_overview(sheet, data)
    update_opportunities(sheet, data)
    update_top_pages(sheet, data)
    update_alerts(sheet)
    update_content_decay(sheet)
    add_sparklines(sheet)
    share_sheet(sheet)

    print(f"\n✅ Dashboard ready!")
    print(f"URL: {sheet.url}")
    return sheet.url


if __name__ == "__main__":
    main()
