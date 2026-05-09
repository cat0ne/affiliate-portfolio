#!/usr/bin/env python3
"""
Agent Email Marketer — Weekly Affiliate Newsletter Generator

Standalone agent that runs weekly. Queries the affiliate-machine database for:
  • Price drops (products where price changed >10%)
  • New top-rated products (rating ≥ 4.5, recent additions)
  • Seasonal content opportunities (articles with seasonal angles not yet published)

Generates a markdown newsletter with product cards, affiliate links, and a seasonal angle.
Sends via Resend API to subscriber list. Emits marketing.newsletter_sent events.

Usage:
    python agent_email_marketer.py --weekly [--dry-run]
    python agent_email_marketer.py --dry-run          # preview without sending
    python agent_email_marketer.py --weekly            # normal weekly run

Environment:
    RESEND_API_KEY   — required for live sends
    FROM_EMAIL       — sender address (default: newsletter@top-aspirateur.fr)
    TEST_EMAIL       — override recipient for testing (default: hochard@gmail.com)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


# Load .env from scripts directory so RESEND_API_KEY etc. are available
# regardless of how this script is invoked (cron, manual, GH Actions).
def _load_env() -> None:
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    if key not in os.environ:
                        os.environ[key] = val


_load_env()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path("/Users/gho/Documents/affiliation-sites")
SCRIPTS_DIR = BASE_DIR / "scripts"
REPORTS_DIR = BASE_DIR / "reports"
EVENTS_DIR = Path.home() / "hermes-events" / "inbox"

DB_PATH = Path.home() / "affiliate-machine.db"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_AUDIENCE_ID = os.environ.get("RESEND_AUDIENCE_ID", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "newsletter@top-aspirateur.fr")
TEST_EMAIL = os.environ.get("TEST_EMAIL", "hochard@gmail.com")

# Affiliate site definitions (must match db_sync.py)
SITES = [
    {"slug": "aspirateur", "name": "Top-Aspirateur", "niche": "Aspirateurs", "domain": "top-aspirateur.fr"},
    {"slug": "bureau", "name": "Bureau-Expert", "niche": "Bureaux", "domain": "bureau-expert.fr"},
    {"slug": "cafe", "name": "Brewmance", "niche": "Café", "domain": "brewmance.fr"},
    {"slug": "matelas", "name": "Matelas-Expert", "niche": "Matelas", "domain": "matelas-expert.fr"},
    {"slug": "pixinstant", "name": "PixInstant", "niche": "Appareils photo", "domain": "pixinstant.com"},
    {"slug": "airpurify", "name": "AirPurify", "niche": "Purificateurs d'air", "domain": "airpurify.com"},
    {"slug": "safehive", "name": "SafeHive", "niche": "Sécurité maison", "domain": "safehivehq.com"},
    {"slug": "pawhive", "name": "PawHive", "niche": "Animaux", "domain": "pawhivehq.com"},
]

# Seasonal keywords mapped to months (1-12)
SEASONAL_MAP: dict[int, list[str]] = {
    1: ["nouvelle année", "résolution", "organisation", "bureau"],
    2: ["saint-valentin", "amour", "cadeau", "romantique"],
    3: ["printemps", "ménage", "renouveau", "allergies", "air"],
    4: ["printemps", "jardin", "ménage", "aspirateur"],
    5: ["mère", "fête des mères", "cadeau", "confort"],
    6: ["été", "vacances", "voyage", "photo", "instant"],
    7: ["été", "vacances", "chaleur", "air", "rafraîchir"],
    8: ["rentrée", "école", "organisation", "bureau"],
    9: ["rentrée", "automne", "confort", "matelas"],
    10: ["halloween", "automne", "confort", "maison"],
    11: ["black friday", "noël", "cadeaux", "promotions"],
    12: ["noël", "fêtes", "cadeaux", "fin d'année"],
}

PRICE_DROP_THRESHOLD_PCT = 10.0
PRICE_LOOKBACK_DAYS = 7
TOP_RATED_MIN_SCORE = 4.5
MAX_PRODUCT_CARDS = 6
MAX_SEASONAL_PICKS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def price_seven_days_ago(history: list[dict[str, Any]]) -> float | None:
    """Return the oldest price within the last 7 days from a history list."""
    cutoff = datetime.now() - timedelta(days=PRICE_LOOKBACK_DAYS)
    candidates = []
    for entry in history:
        date_str = entry.get("date", "")
        try:
            entry_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if entry_date >= cutoff:
            candidates.append((entry_date, entry.get("price", 0)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return float(candidates[0][1])


def detect_price_drops(site: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare current prices against 7-day-old prices and flag drops ≥ threshold."""
    site_dir = BASE_DIR / site["slug"] / "public"
    prices_path = site_dir / "amazon-prices.json"
    history_path = site_dir / "amazon-prices-history.json"

    current = load_json(prices_path)
    history = load_json(history_path)
    if not current or not history:
        return []

    alerts = []
    for asin, curr_item in current.items():
        curr_price = curr_item.get("price")
        if curr_price is None:
            continue
        raw_hist = history.get(asin, [])
        if isinstance(raw_hist, dict):
            hist = raw_hist.get("history", [])
        else:
            hist = raw_hist
        old_price = price_seven_days_ago(hist)
        if old_price is None or old_price <= 0:
            continue
        drop_pct = ((old_price - curr_price) / old_price) * 100
        if drop_pct >= PRICE_DROP_THRESHOLD_PCT:
            alerts.append({
                "site": site["name"],
                "site_slug": site["slug"],
                "niche": site["niche"],
                "asin": asin,
                "title": curr_item.get("title", ""),
                "affiliate_url": curr_item.get("affiliateUrl", ""),
                "image_url": curr_item.get("imageUrl", ""),
                "currency": curr_item.get("currency", "EUR"),
                "price_current": round(curr_price, 2),
                "price_old": round(old_price, 2),
                "drop_amount": round(old_price - curr_price, 2),
                "drop_pct": round(drop_pct, 2),
                "static_price": curr_item.get("staticPrice", ""),
            })
    alerts.sort(key=lambda x: x["drop_pct"], reverse=True)
    return alerts


def query_top_rated_from_db(db_path: Path, limit: int = 10) -> list[dict[str, Any]]:
    """Query products with high ratings from the affiliate-machine database."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT p.product_id, p.asin, p.name, p.brand, p.category, p.rating, p.review_count,
               p.image_url, p.price_usd, p.last_checked
        FROM products p
        WHERE p.rating >= ? AND p.status = 'active'
        ORDER BY p.rating DESC, p.review_count DESC
        LIMIT ?
        """,
        (TOP_RATED_MIN_SCORE, limit),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def query_seasonal_articles(db_path: Path, month: int, limit: int = 10) -> list[dict[str, Any]]:
    """Query articles whose angle matches current seasonal keywords."""
    if not db_path.exists():
        return []
    keywords = SEASONAL_MAP.get(month, [])
    if not keywords:
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # Build LIKE clauses for angle and title
    conditions = " OR ".join(["a.angle LIKE ? OR a.title LIKE ?" for _ in keywords])
    params = []
    for kw in keywords:
        params.extend([f"%{kw}%", f"%{kw}%"])
    params.append(limit)
    cursor.execute(
        f"""
        SELECT a.article_id, a.slug, a.title, a.angle, a.target_keyword, a.word_count,
               s.name AS site_name, s.slug AS site_slug, s.niche
        FROM articles a
        JOIN sites s ON a.site_id = s.site_id
        WHERE ({conditions})
        ORDER BY a.word_count DESC
        LIMIT ?
        """,
        params,
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def build_affiliate_url_from_asin(asin: str, locale: str = "fr") -> str:
    """Build a best-effort affiliate URL from ASIN and locale."""
    domain_map = {
        "fr": ("amazon.fr", "zoomzen05-21"),
        "de": ("amazon.de", "zoomzen-21"),
        "es": ("amazon.es", "zoomzen08-21"),
        "it": ("amazon.it", "zoomzen01-21"),
        "uk": ("amazon.co.uk", "zoomzen07-21"),
        "en": ("amazon.com", "zoomzus-20"),
    }
    domain, tag = domain_map.get(locale, domain_map["fr"])
    return f"https://www.{domain}/dp/{asin}?tag={tag}"


def generate_newsletter_markdown(
    price_drops: list[dict[str, Any]],
    top_rated: list[dict[str, Any]],
    seasonal_articles: list[dict[str, Any]],
    season_title: str,
) -> str:
    """Generate a markdown newsletter with product cards and seasonal angle."""
    today = datetime.now().strftime("%d %B %Y")
    lines = [
        f"# 🔥 Newsletter Affiliation — {season_title}",
        f"*{today}*",
        "",
        "Bienvenue dans notre sélection hebdomadaire des meilleures opportunités produits !",
        "",
    ]

    # Price drops section
    if price_drops:
        lines.extend([
            "## 💸 Baisses de prix (≥10 % cette semaine)",
            "",
        ])
        for item in price_drops[:MAX_PRODUCT_CARDS]:
            url = item.get("affiliate_url") or build_affiliate_url_from_asin(item["asin"])
            img = item.get("image_url") or ""
            lines.extend([
                f"### {item['title']}",
                f"- **Site** : {item['site']} ({item['niche']})",
                f"- **Ancien prix** : {item['price_old']:.2f} {item['currency']}",
                f"- **Nouveau prix** : {item['price_current']:.2f} {item['currency']}",
                f"- **Économie** : -{item['drop_pct']:.0f} % (-{item['drop_amount']:.2f} {item['currency']})",
                f"- [👉 Voir l'offre]({url})",
                "",
            ])
            if img:
                lines.append(f"![{item['title']}]({img})")
                lines.append("")
    else:
        lines.extend([
            "## 💸 Baisses de prix",
            "",
            "Aucune baisse significative détectée cette semaine. Revenez la semaine prochaine !",
            "",
        ])

    # Top-rated section
    if top_rated:
        lines.extend([
            "## ⭐ Nouveautés top-rated",
            "",
        ])
        for prod in top_rated[:MAX_PRODUCT_CARDS]:
            asin = prod.get("asin", "")
            url = build_affiliate_url_from_asin(asin)
            img = prod.get("image_url") or ""
            lines.extend([
                f"### {prod.get('name', 'Produit')}",
                f"- **Marque** : {prod.get('brand', 'N/A')}",
                f"- **Note** : {prod.get('rating', 'N/A')} ⭐ ({prod.get('review_count', 0)} avis)",
                f"- **Catégorie** : {prod.get('category', 'N/A')}",
                f"- [👉 Voir sur Amazon]({url})",
                "",
            ])
            if img:
                lines.append(f"![{prod.get('name', '')}]({img})")
                lines.append("")

    # Seasonal section
    if seasonal_articles:
        lines.extend([
            f"## 🌿 Opportunités saisonnières — {season_title}",
            "",
        ])
        for art in seasonal_articles[:MAX_SEASONAL_PICKS]:
            lines.extend([
                f"### {art['title']}",
                f"- **Site** : {art['site_name']} ({art['niche']})",
                f"- **Angle** : {art.get('angle', 'N/A')}",
                f"- **Mot-clé** : {art.get('target_keyword', 'N/A')}",
                "",
            ])
    else:
        lines.extend([
            f"## 🌿 Opportunités saisonnières — {season_title}",
            "",
            "Aucun contenu saisonnier à mettre en avant cette semaine.",
            "",
        ])

    lines.extend([
        "---",
        "",
        "Vous recevez cette newsletter car vous êtes inscrit à notre liste d'affiliation.",
        "[Se désinscrire](#unsubscribe)",
        "",
    ])
    return "\n".join(lines)


def emit_event(event_type: str, payload: dict[str, Any]) -> Path | None:
    """Emit a Hermes event to the inbox directory."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    event = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "source_agent": "agent-email-marketer",
        "routing_key": f"marketing.{event_type}",
        "target_agent": "marketing",
        "priority": "normal",
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    filename = f"{event['id']}.json"
    path = EVENTS_DIR / filename
    try:
        path.write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
        return path
    except OSError as exc:
        print(f"[WARN] Failed to emit event: {exc}")
        return None


def send_via_resend(
    to_email: str,
    subject: str,
    html_body: str,
    from_email: str = FROM_EMAIL,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send an email via Resend API. Returns response dict."""
    if dry_run:
        print(f"[DRY-RUN] Would send email to {to_email}:")
        print(f"  Subject: {subject}")
        print(f"  From: {from_email}")
        print(f"  HTML length: {len(html_body)} chars")
        return {"id": "dry-run", "status": "dry-run"}

    if not RESEND_API_KEY:
        print("[ERROR] RESEND_API_KEY not set. Cannot send email.")
        return {"error": "missing_api_key"}

    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        print(f"[OK] Email sent to {to_email} — Resend ID: {data.get('id')}")
        return data
    except requests.exceptions.RequestException as exc:
        print(f"[ERROR] Resend API failed: {exc}")
        return {"error": str(exc)}


def markdown_to_html(md: str) -> str:
    """Lightweight markdown-to-HTML converter for email bodies.

    The previous implementation used `.replace("**", "<b>", 1)` twice, which
    only converted the first bold pair in the entire newsletter (every
    subsequent `**foo**` was left as raw markdown), and never closed `<h*>`
    tags. Rewritten with proper regex.
    """
    html = md
    # 1. Escape HTML so we can safely re-introduce tags below.
    html = html.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 2. Images BEFORE links (image syntax is a superset of link syntax).
    html = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        r'<img src="\2" alt="\1" style="max-width:600px;height:auto;"/>',
        html,
    )

    # 3. Markdown links → <a>. Anchors get target=_blank for newsletter clicks.
    html = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        html,
    )

    # 4. Headers — must be at start of a line, must close the tag.
    def _header_sub(match: re.Match) -> str:
        hashes, text = match.group(1), match.group(2).strip()
        level = min(len(hashes), 6)
        return f"<h{level}>{text}</h{level}>"

    html = re.sub(r"^(#{1,6})\s+(.+)$", _header_sub, html, flags=re.MULTILINE)

    # 5. Bold (**…**) and italic (*…* / _…_) — non-greedy, all occurrences.
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html, flags=re.DOTALL)
    html = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", html)
    html = re.sub(r"(?<!_)_(?!\s)(.+?)(?<!\s)_(?!_)", r"<em>\1</em>", html)

    # 6. Paragraphs / line breaks.
    html = html.replace("\r\n", "\n")
    html = re.sub(r"\n{2,}", "</p><p>", html)
    html = html.replace("\n", "<br>")

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "</head><body style=\"font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "line-height:1.55;color:#222;max-width:640px;margin:0 auto;padding:24px;\">"
        f"<p>{html}</p></body></html>"
    )


def get_seasonal_context() -> tuple[str, list[str]]:
    """Return a season title and keywords for the current month."""
    month = datetime.now().month
    keywords = SEASONAL_MAP.get(month, [])
    month_names = {
        1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
        5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
        9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
    }
    title = f"{month_names.get(month, 'Ce mois')} — {', '.join(keywords[:2])}"
    return title, keywords


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent Email Marketer — Weekly Affiliate Newsletter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --weekly
  %(prog)s --weekly --dry-run
  %(prog)s --dry-run
""",
    )
    parser.add_argument("--weekly", action="store_true", help="Run the weekly newsletter cycle")
    parser.add_argument("--dry-run", action="store_true", help="Preview without sending emails or moving files")
    args = parser.parse_args()

    dry_run = args.dry_run
    weekly_mode = args.weekly

    print("=" * 60)
    print("Agent Email Marketer — Weekly Newsletter")
    print("=" * 60)
    if dry_run:
        print("[MODE] Dry-run — no emails will be sent, no events emitted.\n")
    if weekly_mode:
        print("[MODE] Weekly run — scanning all sites and DB.\n")

    season_title, _ = get_seasonal_context()
    print(f"Seasonal context: {season_title}\n")

    # ------------------------------------------------------------------
    # 1. Price drops
    # ------------------------------------------------------------------
    print("🔍 Scanning for price drops...")
    all_price_drops: list[dict[str, Any]] = []
    for site in SITES:
        drops = detect_price_drops(site)
        if drops:
            print(f"  📦 {site['name']}: {len(drops)} drop(s)")
            all_price_drops.extend(drops)
        else:
            print(f"  📦 {site['name']}: no drops")
    all_price_drops.sort(key=lambda x: x["drop_pct"], reverse=True)
    print(f"  → Total price drops: {len(all_price_drops)}\n")

    # ------------------------------------------------------------------
    # 2. Top-rated products from DB
    # ------------------------------------------------------------------
    print("🔍 Querying top-rated products from DB...")
    top_rated = query_top_rated_from_db(DB_PATH, limit=20)
    print(f"  → Found {len(top_rated)} top-rated product(s)\n")

    # ------------------------------------------------------------------
    # 3. Seasonal articles from DB
    # ------------------------------------------------------------------
    print("🔍 Querying seasonal content opportunities...")
    month = datetime.now().month
    seasonal_articles = query_seasonal_articles(DB_PATH, month=month, limit=20)
    print(f"  → Found {len(seasonal_articles)} seasonal article(s)\n")

    # ------------------------------------------------------------------
    # 4. Build newsletter
    # ------------------------------------------------------------------
    print("📝 Generating newsletter markdown...")
    newsletter_md = generate_newsletter_markdown(
        price_drops=all_price_drops,
        top_rated=top_rated,
        seasonal_articles=seasonal_articles,
        season_title=season_title,
    )
    newsletter_html = markdown_to_html(newsletter_md)

    # Save snapshot
    today_str = datetime.now().strftime("%Y-%m-%d")
    snapshot_path = REPORTS_DIR / f"newsletter-{today_str}.md"
    if not dry_run:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(newsletter_md, encoding="utf-8")
        print(f"  ✅ Saved markdown: {snapshot_path}")
    else:
        print(f"  [DRY-RUN] Would save: {snapshot_path}")

    # ------------------------------------------------------------------
    # 5. Send email
    # ------------------------------------------------------------------
    subject = f"🔥 Newsletter Affiliation — {season_title}"
    recipient = TEST_EMAIL
    print(f"\n📤 Sending newsletter to {recipient}...")
    result = send_via_resend(
        to_email=recipient,
        subject=subject,
        html_body=newsletter_html,
        dry_run=dry_run,
    )

    # ------------------------------------------------------------------
    # 6. Emit event
    # ------------------------------------------------------------------
    event_payload = {
        "newsletter_date": today_str,
        "season_title": season_title,
        "recipient": recipient,
        "price_drops_count": len(all_price_drops),
        "top_rated_count": len(top_rated),
        "seasonal_articles_count": len(seasonal_articles),
        "resend_status": result.get("status") or result.get("error", "unknown"),
        "resend_id": result.get("id"),
        "snapshot_path": str(snapshot_path),
    }
    if not dry_run:
        event_path = emit_event("newsletter_sent", event_payload)
        if event_path:
            print(f"\n📡 Emitted marketing.newsletter_sent event: {event_path.name}")
    else:
        print(f"\n[DRY-RUN] Would emit marketing.newsletter_sent event:")
        print(json.dumps(event_payload, indent=2, ensure_ascii=False))

    print("\n✨ Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
