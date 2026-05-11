#!/usr/bin/env python3
"""
Agent Strategist — Hermes Event Consumer

Processes content.decay_detected and seo.content_gap_detected events.
Analyzes traffic drop, identifies root cause (seasonality, competition, content rot),
generates keyword research briefs using Apify Google Autocomplete + PAA,
emits content.requested events with full briefs (angle, keywords, outline, target word count).

Usage:
    python agent_strategist.py --consume --limit 10
    python agent_strategist.py --consume --limit 5 --dry-run
    python agent_strategist.py --consume --event-type content.decay_detected
    python agent_strategist.py --consume --event-type seo.content_gap_detected --limit 20
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from affiliate_paths import portfolio_root
from hermes_bus import (
    claim_inbox_json,
    complete_claimed_event,
    ensure_hermes_dirs,
    fail_claimed_event,
    get_hermes_paths,
    plain_move,
)

# Load .env from scripts directory
def _load_env():
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

# ── Paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = portfolio_root()
REPORTS_DIR = BASE_DIR / "reports"
DB_PATH = Path("~/affiliate-machine.db").expanduser()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agent_strategist")

# ── Constants ──────────────────────────────────────────────────────────────
SUPPORTED_EVENT_TYPES = {"content.decay_detected", "seo.content_gap_detected"}
ROUTING_KEY = "agent.strategist"
TARGET_AGENT = "agent-strategist"

# Always brief writers with the *current* year. Hardcoded literals here meant
# briefs kept telling the writer to produce "...2025" headlines well into 2026,
# which is a CTR/SEO killer on day one. Recomputed at module load.
CURRENT_YEAR = datetime.now(timezone.utc).year

SITE_GSC_MAP = {
    "aspirateur": "sc-domain:top-aspirateur.fr",
    "bureau": "sc-domain:bureau-expert.fr",
    "matelas": "sc-domain:matelas-expert.fr",
    "cafe": "sc-domain:brewmance.fr",
    "pixinstant": "sc-domain:pixinstant.com",
    "airpurify": "sc-domain:airpurifyhq.com",
    "safehive": "sc-domain:safehivehq.com",
    "pawhive": "sc-domain:pawhivehq.com",
}

SITE_BASE_URL = {
    "aspirateur": "https://www.top-aspirateur.fr",
    "bureau": "https://www.bureau-expert.fr",
    "matelas": "https://www.matelas-expert.fr",
    "cafe": "https://www.brewmance.fr",
    "pixinstant": "https://www.pixinstant.com",
    "airpurify": "https://www.airpurifyhq.com",
    "safehive": "https://www.safehivehq.com",
    "pawhive": "https://www.pawhivehq.com",
}

# ── Apify client import ────────────────────────────────────────────────────
try:
    sys.path.insert(0, str(SCRIPT_DIR))
    from apify_client import ApifyClient
except ImportError as exc:
    logger.warning("ApifyClient not importable (%s). Will use fallback local patterns.", exc)
    ApifyClient = None


# ═════════════════════════════════════════════════════════════════════════════
#  DB helpers
# ═════════════════════════════════════════════════════════════════════════════

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_article_metrics(conn: sqlite3.Connection, site_slug: str, article_slug: str) -> Optional[Dict[str, Any]]:
    """Fetch latest page_metrics for an article."""
    cursor = conn.execute(
        """
        SELECT pm.*, a.article_id, a.title, a.target_keyword, a.word_count, a.angle
        FROM articles a
        LEFT JOIN page_metrics pm ON pm.article_id = a.article_id
        WHERE a.slug = ? AND a.site_id = (SELECT site_id FROM sites WHERE slug = ?)
        ORDER BY pm.date DESC
        LIMIT 1
        """,
        (article_slug, site_slug),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def get_historical_metrics(
    conn: sqlite3.Connection,
    site_slug: str,
    article_slug: str,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Fetch historical page_metrics for trend analysis."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    cursor = conn.execute(
        """
        SELECT pm.date, pm.impressions, pm.clicks, pm.ctr, pm.position, pm.gsc_data
        FROM articles a
        JOIN page_metrics pm ON pm.article_id = a.article_id
        WHERE a.slug = ? AND a.site_id = (SELECT site_id FROM sites WHERE slug = ?)
          AND pm.date >= ?
        ORDER BY pm.date DESC
        """,
        (article_slug, site_slug, since),
    )
    return [dict(row) for row in cursor.fetchall()]


def get_content_gap_info(conn: sqlite3.Connection, site_slug: str, article_slug: str, missing_locale: str) -> Optional[Dict[str, Any]]:
    """Fetch content gap record from DB."""
    cursor = conn.execute(
        """
        SELECT * FROM content_gaps
        WHERE site_id = (SELECT site_id FROM sites WHERE slug = ?)
          AND article_slug = ? AND missing_locale = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (site_slug, article_slug, missing_locale),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


# ═════════════════════════════════════════════════════════════════════════════
#  Event I/O helpers (Hermes filesystem bus)
# ═════════════════════════════════════════════════════════════════════════════

def ensure_dirs() -> None:
    ensure_hermes_dirs()


def list_inbox_events(event_type: Optional[str] = None) -> List[Path]:
    """Return sorted list of unprocessed JSON event files in inbox, optionally filtered by type."""
    inbox = get_hermes_paths().inbox
    if not inbox.exists():
        return []
    files = sorted(
        [f for f in inbox.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda p: p.stat().st_mtime,
    )
    matched = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                ev = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        ev_type = ev.get("type", "")
        target = ev.get("target_agent") or ev.get("routing_key", "").split(".")[-1]
        if event_type and ev_type != event_type:
            continue
        # Accept events either explicitly routed to agent-strategist OR
        # events of supported types that are unrouted / broadcast
        if target and target != TARGET_AGENT:
            # If it's a supported event type and routing_key is generic (e.g. agent.content),
            # still process it
            if ev_type not in SUPPORTED_EVENT_TYPES:
                continue
        matched.append(path)
    return matched


def read_event(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read event %s: %s", path.name, exc)
        return None


def move_event(src: Path, dst_dir: Path, dry_run: bool = False) -> Optional[Path]:
    return plain_move(src, dst_dir, dry_run=dry_run)


def emit_event(
    event_type: str,
    payload: Dict[str, Any],
    priority: int = 3,
    source: str = TARGET_AGENT,
    target_agent: str = "agent-writer",
    dry_run: bool = False,
) -> Optional[Path]:
    """Write a Hermes event JSON file to the inbox."""
    inbox = ensure_hermes_dirs().inbox
    event = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "priority": priority,
        "payload": payload,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source_agent": source,
        "target_agent": target_agent,
        "routing_key": f"agent.{target_agent}",
    }
    filename = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{event_type.replace('.', '_')}_{event['id'][:8]}.json"
    path = inbox / filename
    if dry_run:
        logger.info("[DRY-RUN] Would emit %s -> %s", event_type, filename)
        return path
    path.write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Emitted: %s -> %s", event_type, filename)
    return path


# ═════════════════════════════════════════════════════════════════════════════
#  Root-cause classification
# ═════════════════════════════════════════════════════════════════════════════

def classify_drop(
    metrics: List[Dict[str, Any]],
    issue_type: str,
) -> Tuple[str, List[str], Dict[str, Any]]:
    """
    Classify the root cause of a traffic drop / decay.
    Returns (classification, signals, summary_stats).
    """
    if not metrics:
        return "unknown", ["no_metrics_available"], {}

    # Sort ascending by date for trend analysis
    metrics = sorted(metrics, key=lambda x: x.get("date", ""))

    # Split into first half (older) and second half (recent)
    n = len(metrics)
    mid = n // 2
    older = metrics[:mid] if mid else metrics[:1]
    recent = metrics[mid:] if mid else metrics[-1:]

    def avg(rows, key):
        vals = [r.get(key, 0) or 0 for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0

    old_clicks = avg(older, "clicks")
    new_clicks = avg(recent, "clicks")
    old_impr = avg(older, "impressions")
    new_impr = avg(recent, "impressions")
    old_pos = avg(older, "position")
    new_pos = avg(recent, "position")
    old_ctr = avg(older, "ctr")
    new_ctr = avg(recent, "ctr")

    signals = []

    # Position shift
    pos_delta = new_pos - old_pos
    if abs(pos_delta) > 2:
        signals.append(f"position_shift:{pos_delta:+.1f}")

    # CTR shift
    ctr_delta = new_ctr - old_ctr
    if abs(ctr_delta) > 0.01:
        signals.append(f"ctr_shift:{ctr_delta:+.2f}")

    # Impressions shift (seasonality signal)
    impr_delta = new_impr - old_impr
    if old_impr > 0:
        impr_pct = (impr_delta / old_impr) * 100
        if abs(impr_pct) > 30 and abs(pos_delta) < 2:
            signals.append(f"seasonal_pattern:impr_change_{impr_pct:+.1f}%")

    # Uniform drop across period (technical)
    if old_clicks > 0:
        click_pct = ((new_clicks - old_clicks) / old_clicks) * 100
        if click_pct < -30:
            signals.append(f"steep_drop:{click_pct:.1f}%")

    # Issue-type specific overrides
    if issue_type == "stale":
        signals.append("content_staleness")
    elif issue_type == "thin":
        signals.append("thin_content")
    elif issue_type == "outdated_year":
        signals.append("outdated_year_references")

    # Determine classification
    classification = "unknown"
    if any(s.startswith("seasonal_pattern") for s in signals):
        classification = "seasonal"
    elif any(s.startswith("position_shift") for s in signals) or "steep_drop" in str(signals):
        classification = "competitive"
    elif issue_type in ("stale", "thin", "outdated_year"):
        classification = "content_rot"
    elif "steep_drop" in str(signals):
        classification = "technical"

    summary = {
        "old_clicks": round(old_clicks, 1),
        "new_clicks": round(new_clicks, 1),
        "old_position": round(old_pos, 1),
        "new_position": round(new_pos, 1),
        "old_impressions": round(old_impr, 1),
        "new_impressions": round(new_impr, 1),
        "old_ctr": round(old_ctr, 3),
        "new_ctr": round(new_ctr, 3),
    }
    return classification, signals, summary


# ═════════════════════════════════════════════════════════════════════════════
#  Keyword research (Apify + fallback)
# ═════════════════════════════════════════════════════════════════════════════

def _has_apify_token() -> bool:
    return bool(os.getenv("APIFY_API_TOKEN"))


def run_apify_autocomplete(seed_keyword: str, locale: str = "fr", limit: int = 10) -> List[str]:
    """Run Apify Google Autocomplete actor with short timeout."""
    if not _has_apify_token():
        return []
    try:
        client = ApifyClient()
        input_data = {
            "keywords": [seed_keyword],
            "language": locale,
            "countryCode": _locale_to_country(locale),
            "maxResults": limit,
        }
        # Use short timeout — if Apify is slow, fall back immediately
        results = client.run("google_autocomplete", input_data, memory_mb=512, timeout_secs=30)
        suggestions = []
        for item in results:
            suggestions.extend(item.get("suggestions", []))
        return suggestions[:limit]
    except Exception as exc:
        logger.warning("Apify autocomplete failed: %s", exc)
        return []


def run_apify_paa(seed_keyword: str, locale: str = "fr", limit: int = 8) -> List[str]:
    """Run Apify People Also Ask actor with short timeout."""
    if not _has_apify_token():
        return []
    try:
        client = ApifyClient()
        input_data = {
            "queries": seed_keyword,
            "language": locale,
            "country": _locale_to_country(locale),
            "maxResults": 1,
        }
        # Use short timeout — PAA is nice-to-have, not critical
        results = client.run("google_serp", input_data, memory_mb=512, timeout_secs=30)
        paa = []
        for item in results:
            paa.extend(item.get("peopleAlsoAsk", []))
            paa.extend(item.get("peopleAlsoAskQuestions", []))
            paa.extend(item.get("relatedQuestions", []))
        return paa[:limit]
    except Exception as exc:
        logger.warning("Apify PAA failed: %s", exc)
        return []


def _locale_to_country(locale: str) -> str:
    mapping = {
        "fr": "FR",
        "en": "US",
        "de": "DE",
        "es": "ES",
        "it": "IT",
        "uk": "GB",
        "ja": "JP",
    }
    return mapping.get(locale, "US")


def _fallback_autocomplete(seed_keyword: str, locale: str = "fr") -> List[str]:
    """Local fallback: generate likely long-tail patterns."""
    next_year = CURRENT_YEAR + 1
    templates = [
        f"{{kw}} {CURRENT_YEAR}",
        f"{{kw}} {next_year}",
        "meilleur {kw}",
        "{kw} avis",
        "{kw} comparatif",
        "{kw} test",
        "{kw} pas cher",
        "{kw} guide",
        "{kw} professionnel",
        "{kw} pour debutant",
        "{kw} haut de gamme",
        "acheter {kw}",
        "{kw} amazon",
        "{kw} promo",
    ]
    kw = seed_keyword
    suggestions = [t.format(kw=kw) for t in templates]
    # Locale-specific tweaks
    if locale == "en":
        suggestions += [
            f"best {kw}",
            f"{kw} review",
            f"{kw} buying guide",
            f"{kw} cheap",
            f"{kw} deals",
        ]
    elif locale == "de":
        suggestions += [
            f"beste {kw}",
            f"{kw} test",
            f"{kw} vergleich",
            f"{kw} günstig",
        ]
    elif locale == "es":
        suggestions += [
            f"mejor {kw}",
            f"{kw} opiniones",
            f"{kw} comparativa",
        ]
    elif locale == "it":
        suggestions += [
            f"miglior {kw}",
            f"{kw} recensione",
            f"{kw} confronto",
        ]
    return list(dict.fromkeys(suggestions))[:15]


def _fallback_paa(seed_keyword: str, locale: str = "fr") -> List[str]:
    """Local fallback: generate likely PAA-style questions."""
    templates = [
        "Quel est le meilleur {kw} ?",
        "Comment choisir un {kw} ?",
        "{kw} : que vaut-il vraiment ?",
        "Est-ce que {kw} vaut le coup ?",
        "Quelle marque de {kw} choisir ?",
        "{kw} pas cher : lesquels sont fiables ?",
        "Quels sont les avantages de {kw} ?",
        "{kw} : guide d'achat complet",
    ]
    if locale == "en":
        templates = [
            f"What is the best {seed_keyword}?",
            f"How to choose a {seed_keyword}?",
            f"Is {seed_keyword} worth it?",
            f"Which brand of {seed_keyword} is best?",
            f"Cheap {seed_keyword}: which ones are reliable?",
            f"What are the benefits of {seed_keyword}?",
            f"{seed_keyword} buying guide",
        ]
    elif locale == "de":
        templates = [
            f"Was ist der beste {seed_keyword}?",
            f"Wie wählt man einen {seed_keyword}?",
            f"Ist {seed_keyword} sein Geld wert?",
            f"Welche Marke {seed_keyword} ist die beste?",
        ]
    elif locale == "es":
        templates = [
            f"¿Cuál es el mejor {seed_keyword}?",
            f"¿Cómo elegir un {seed_keyword}?",
            f"¿Vale la pena {seed_keyword}?",
        ]
    elif locale == "it":
        templates = [
            f"Qual è il miglior {seed_keyword}?",
            f"Come scegliere un {seed_keyword}?",
            f"{seed_keyword} vale la pena?",
        ]
    return [t.format(kw=seed_keyword) for t in templates][:8]


def generate_keyword_brief(
    seed_keyword: str,
    locale: str = "fr",
    issue_type: str = "stale",
    classification: str = "content_rot",
    existing_word_count: int = 0,
    use_apify: bool = False,  # DISABLED by default — too slow for batch processing
) -> Dict[str, Any]:
    """
    Generate a keyword research brief using Apify (if enabled) or fast local fallback.
    Returns dict with angle, keywords, outline, target_word_count.
    """
    # 1. Autocomplete suggestions (fast local fallback — no API call)
    auto = _fallback_autocomplete(seed_keyword, locale)
    if use_apify:
        try:
            apify_auto = run_apify_autocomplete(seed_keyword, locale, limit=10)
            if apify_auto:
                auto = apify_auto
        except Exception:
            pass

    # 2. People Also Ask (fast local fallback — no API call)
    paa = _fallback_paa(seed_keyword, locale)
    if use_apify:
        try:
            apify_paa = run_apify_paa(seed_keyword, locale, limit=8)
            if apify_paa:
                paa = apify_paa
        except Exception:
            pass

    # 3. Build keyword clusters
    primary = seed_keyword
    secondary = [k for k in auto if k != primary][:5]
    questions = paa[:5]

    # 4. Determine angle based on classification
    angle_map = {
        "seasonal": f"Mise à jour saisonnière : {primary} — comparatif et guide d'achat {CURRENT_YEAR}",
        "competitive": f"Reprendre le terrain sur {primary} : guide complet avec comparatif {CURRENT_YEAR}",
        "content_rot": f"Rafraîchir et approfondir : {primary} — guide d'achat complet {CURRENT_YEAR}",
        "technical": f"Réviser et consolider : {primary} après problème technique",
        "unknown": f"Optimiser {primary} : guide d'achat et comparatif complet",
    }
    angle = angle_map.get(classification, angle_map["unknown"])
    if locale == "en":
        angle = angle.replace("guide d'achat", "buying guide").replace("comparatif", "comparison")
    elif locale == "de":
        angle = angle.replace("guide d'achat", "Kaufberater").replace("comparatif", "Vergleich")
    elif locale == "es":
        angle = angle.replace("guide d'achat", "guía de compra").replace("comparatif", "comparativa")
    elif locale == "it":
        angle = angle.replace("guide d'achat", "guida all'acquisto").replace("comparatif", "confronto")

    # 5. Target word count based on issue type + existing size
    base_target = {
        "stale": 2500,
        "thin": 3000,
        "outdated_year": 2200,
        "content_gap": 2800,
    }.get(issue_type, 2500)

    if existing_word_count > 0 and existing_word_count < base_target:
        target_word_count = max(base_target, int(existing_word_count * 1.5))
    else:
        target_word_count = base_target

    # 6. Outline
    outline = _generate_outline(seed_keyword, classification, questions, locale)

    return {
        "primary_keyword": primary,
        "secondary_keywords": secondary,
        "questions": questions,
        "angle": angle,
        "target_word_count": target_word_count,
        "outline": outline,
        "sources": {
            "autocomplete": auto,
            "people_also_ask": paa,
        },
    }


def _generate_outline(
    keyword: str,
    classification: str,
    questions: List[str],
    locale: str = "fr",
) -> List[Dict[str, Any]]:
    """Generate a structured article outline."""
    year = CURRENT_YEAR
    if locale == "en":
        outline = [
            {"heading": f"Introduction: Why {keyword} Matters in {year}", "type": "intro", "word_target": 250},
            {"heading": "Quick Comparison Table", "type": "table", "word_target": 100},
            {"heading": f"Top Picks: Best {keyword} This Year", "type": "section", "word_target": 600},
            {"heading": f"In-Depth Reviews", "type": "section", "word_target": 800},
            {"heading": "Buying Guide: What to Look For", "type": "section", "word_target": 500},
            {"heading": "FAQs", "type": "faq", "word_target": 400},
            {"heading": "Conclusion", "type": "conclusion", "word_target": 150},
        ]
        whats_new_heading = f"What's New in {year}"
    elif locale == "de":
        outline = [
            {"heading": f"Einleitung: Warum {keyword} {year} wichtig ist", "type": "intro", "word_target": 250},
            {"heading": "Schnellvergleich", "type": "table", "word_target": 100},
            {"heading": f"Top-Empfehlungen: Beste {keyword} dieses Jahr", "type": "section", "word_target": 600},
            {"heading": f"Detaillierte Tests", "type": "section", "word_target": 800},
            {"heading": "Kaufberater: Worauf achten", "type": "section", "word_target": 500},
            {"heading": "Häufige Fragen", "type": "faq", "word_target": 400},
            {"heading": "Fazit", "type": "conclusion", "word_target": 150},
        ]
        whats_new_heading = f"Was ist neu {year}"
    elif locale == "es":
        outline = [
            {"heading": f"Introducción: Por qué {keyword} importa en {year}", "type": "intro", "word_target": 250},
            {"heading": "Tabla comparativa rápida", "type": "table", "word_target": 100},
            {"heading": f"Mejores opciones: {keyword} este año", "type": "section", "word_target": 600},
            {"heading": f"Reseñas detalladas", "type": "section", "word_target": 800},
            {"heading": "Guía de compra: qué buscar", "type": "section", "word_target": 500},
            {"heading": "Preguntas frecuentes", "type": "faq", "word_target": 400},
            {"heading": "Conclusión", "type": "conclusion", "word_target": 150},
        ]
        whats_new_heading = f"Novedades {year}"
    elif locale == "it":
        outline = [
            {"heading": f"Introduzione: Perché {keyword} conta nel {year}", "type": "intro", "word_target": 250},
            {"heading": "Tabella comparativa rapida", "type": "table", "word_target": 100},
            {"heading": f"Migliori scelte: {keyword} quest'anno", "type": "section", "word_target": 600},
            {"heading": f"Recensioni dettagliate", "type": "section", "word_target": 800},
            {"heading": "Guida all'acquisto: cosa cercare", "type": "section", "word_target": 500},
            {"heading": "Domande frequenti", "type": "faq", "word_target": 400},
            {"heading": "Conclusione", "type": "conclusion", "word_target": 150},
        ]
        whats_new_heading = f"Cosa c'è di nuovo nel {year}"
    else:  # fr default
        outline = [
            {"heading": f"Introduction : pourquoi {keyword} compte en {year}", "type": "intro", "word_target": 250},
            {"heading": "Tableau comparatif rapide", "type": "table", "word_target": 100},
            {"heading": f"Notre sélection : meilleurs {keyword} cette année", "type": "section", "word_target": 600},
            {"heading": f"Tests et avis détaillés", "type": "section", "word_target": 800},
            {"heading": "Guide d'achat : les critères essentiels", "type": "section", "word_target": 500},
            {"heading": "Questions fréquentes", "type": "faq", "word_target": 400},
            {"heading": "Conclusion", "type": "conclusion", "word_target": 150},
        ]
        whats_new_heading = f"Nouveautés {year}"

    # Inject PAA questions into FAQ section if available
    if questions:
        outline[-3]["faq_questions"] = questions[:5]

    # Add a refresh-specific section for content_rot
    if classification == "content_rot":
        outline.insert(2, {"heading": whats_new_heading, "type": "section", "word_target": 300})

    return outline


# ═════════════════════════════════════════════════════════════════════════════
#  Event processors
# ═════════════════════════════════════════════════════════════════════════════

def process_decay_event(
    event: Dict[str, Any],
    conn: sqlite3.Connection,
    dry_run: bool = False,
) -> Optional[Path]:
    """Process a single content.decay_detected event."""
    payload = event.get("payload", {})
    site = payload.get("site", "")
    locale = payload.get("locale", "fr")
    slug = payload.get("slug", "")
    issue_type = payload.get("issue_type", "stale")
    path = payload.get("path", "")
    days_since = payload.get("days_since", 0)

    logger.info("Processing decay event: site=%s slug=%s issue=%s", site, slug, issue_type)

    # 1. Fetch metrics from DB
    metrics = get_historical_metrics(conn, site, slug, days=30)
    latest = get_article_metrics(conn, site, slug)

    # 2. Classify root cause
    classification, signals, summary = classify_drop(metrics, issue_type)
    logger.info("  Classification: %s | Signals: %s", classification, signals)

    # 3. Determine seed keyword
    seed_keyword = latest.get("target_keyword", "") if latest else ""
    if not seed_keyword:
        # Derive from slug — take meaningful parts, not just first token
        slug_clean = slug.replace("__", " ").replace("-", " ")
        # Drop common stop-words and prefixes
        stop_words = {"le", "la", "les", "un", "une", "des", "du", "de", "et", "en", "pour", "meilleur", "meilleure", "top", "guide", "avis", "test", "comparatif"}
        parts = [p for p in slug_clean.split() if p.lower() not in stop_words and len(p) > 2]
        seed_keyword = " ".join(parts[:3]) if parts else slug_clean.split()[0] if slug_clean else "produit"

    existing_word_count = latest.get("word_count", 0) if latest else 0

    # 4. Generate brief
    brief = generate_keyword_brief(
        seed_keyword=seed_keyword,
        locale=locale,
        issue_type=issue_type,
        classification=classification,
        existing_word_count=existing_word_count,
    )

    # 5. Build content.requested payload
    requested_payload = {
        "site": site,
        "locale": locale,
        "slug": slug,
        "path": path,
        "trigger_event": "content.decay_detected",
        "trigger_event_id": event.get("id", ""),
        "classification": classification,
        "signals": signals,
        "metrics_summary": summary,
        "days_since": days_since,
        "existing_word_count": existing_word_count,
        "brief": brief,
        "priority_reason": f"{issue_type} + {classification}",
        "due_date": (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d"),
    }

    priority = 4 if classification in ("competitive", "content_rot") and issue_type in ("stale", "thin") else 3

    # 6. Emit event
    return emit_event("content.requested", requested_payload, priority=priority, dry_run=dry_run)


def process_gap_event(
    event: Dict[str, Any],
    conn: sqlite3.Connection,
    dry_run: bool = False,
) -> Optional[Path]:
    """Process a single seo.content_gap_detected event."""
    payload = event.get("payload", {})
    site = payload.get("site", "")
    locale = payload.get("locale", "fr")
    article_slug = payload.get("article_slug", "")
    source_locale = payload.get("source_locale", "fr")
    missing_locale = payload.get("missing_locale", locale)
    gap_type = payload.get("gap_type", "missing_locale")
    estimated_traffic = payload.get("estimated_traffic", 0)

    logger.info("Processing gap event: site=%s slug=%s missing_locale=%s", site, article_slug, missing_locale)

    # 1. Fetch source article metrics to understand the topic
    source_metrics = get_article_metrics(conn, site, article_slug)
    seed_keyword = source_metrics.get("target_keyword", "") if source_metrics else ""
    if not seed_keyword:
        slug_clean = article_slug.replace("__", " ").replace("-", " ")
        stop_words = {"le", "la", "les", "un", "une", "des", "du", "de", "et", "en", "pour", "meilleur", "meilleure", "top", "guide", "avis", "test", "comparatif"}
        parts = [p for p in slug_clean.split() if p.lower() not in stop_words and len(p) > 2]
        seed_keyword = " ".join(parts[:3]) if parts else slug_clean.split()[0] if slug_clean else "produit"

    existing_word_count = source_metrics.get("word_count", 0) if source_metrics else 0

    # 2. Classification for gaps is always "content_gap"
    classification = "content_gap"
    signals = [f"missing_locale:{missing_locale}", f"source_locale:{source_locale}"]

    # 3. Generate brief in target locale
    brief = generate_keyword_brief(
        seed_keyword=seed_keyword,
        locale=missing_locale,
        issue_type="content_gap",
        classification=classification,
        existing_word_count=existing_word_count,
    )

    # 4. Build content.requested payload
    requested_payload = {
        "site": site,
        "locale": missing_locale,
        "slug": article_slug,
        "path": payload.get("path", ""),
        "trigger_event": "seo.content_gap_detected",
        "trigger_event_id": event.get("id", ""),
        "classification": classification,
        "signals": signals,
        "source_locale": source_locale,
        "estimated_traffic": estimated_traffic,
        "existing_word_count": existing_word_count,
        "brief": brief,
        "priority_reason": f"content_gap:{missing_locale}",
        "due_date": (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%d"),
    }

    priority = 3 if estimated_traffic and estimated_traffic > 100 else 2

    # 5. Emit event
    return emit_event("content.requested", requested_payload, priority=priority, dry_run=dry_run)


# ═════════════════════════════════════════════════════════════════════════════
#  Main consume loop
# ═════════════════════════════════════════════════════════════════════════════

def consume_events(
    event_type: Optional[str] = None,
    limit: int = 10,
    dry_run: bool = False,
) -> int:
    """Consume and process events from inbox. Returns number of events processed."""
    ensure_dirs()
    conn = get_db_connection() if DB_PATH.exists() else None
    if not conn:
        logger.warning("Database not found at %s — proceeding without DB metrics.", DB_PATH)

    files = list_inbox_events(event_type=event_type)
    processed = 0

    for path in files[:limit]:
        event = read_event(path)
        if not event:
            move_event(path, get_hermes_paths().failed, dry_run=dry_run)
            continue

        ev_type = event.get("type", "")
        if ev_type not in SUPPORTED_EVENT_TYPES:
            logger.info("Skipping unsupported event type: %s", ev_type)
            continue

        proc_path = claim_inbox_json(path, dry_run=dry_run)
        if not proc_path:
            continue

        event["_file_path"] = str(proc_path)

        try:
            if ev_type == "content.decay_detected":
                result = process_decay_event(event, conn, dry_run=dry_run) if conn else None
                if not result and conn:
                    logger.warning("Decay event processing produced no output for %s", event.get("id"))
                elif not conn:
                    # No DB: emit a lightweight request with whatever payload we have
                    emit_event(
                        "content.requested",
                        {
                            "site": event.get("payload", {}).get("site", ""),
                            "locale": event.get("payload", {}).get("locale", "fr"),
                            "slug": event.get("payload", {}).get("slug", ""),
                            "trigger_event": ev_type,
                            "trigger_event_id": event.get("id", ""),
                            "classification": "unknown",
                            "signals": ["no_db_available"],
                            "brief": generate_keyword_brief(
                                seed_keyword=event.get("payload", {}).get("slug", "").replace("-", " ").split("__")[0] or "produit",
                                locale=event.get("payload", {}).get("locale", "fr"),
                                issue_type=event.get("payload", {}).get("issue_type", "stale"),
                            ),
                        },
                        priority=3,
                        dry_run=dry_run,
                    )
            elif ev_type == "seo.content_gap_detected":
                result = process_gap_event(event, conn, dry_run=dry_run) if conn else None
                if not result and conn:
                    logger.warning("Gap event processing produced no output for %s", event.get("id"))
                elif not conn:
                    emit_event(
                        "content.requested",
                        {
                            "site": event.get("payload", {}).get("site", ""),
                            "locale": event.get("payload", {}).get("missing_locale", "fr"),
                            "slug": event.get("payload", {}).get("article_slug", ""),
                            "trigger_event": ev_type,
                            "trigger_event_id": event.get("id", ""),
                            "classification": "content_gap",
                            "signals": ["no_db_available"],
                            "brief": generate_keyword_brief(
                                seed_keyword=event.get("payload", {}).get("article_slug", "").replace("-", " ").split("__")[0] or "produit",
                                locale=event.get("payload", {}).get("missing_locale", "fr"),
                                issue_type="content_gap",
                            ),
                        },
                        priority=2,
                        dry_run=dry_run,
                    )

            complete_claimed_event(event, dry_run=dry_run)
            processed += 1

        except Exception as exc:
            logger.exception("Failed to process event %s: %s", event.get("id"), exc)
            fail_claimed_event(event, dry_run=dry_run)

    if conn:
        conn.close()

    logger.info("Processed %d/%d event(s)", processed, len(files[:limit]))
    return processed


# ═════════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════════

def main() -> int:
    global DB_PATH

    _inbox_default = str(ensure_hermes_dirs().inbox)
    _db_path = str(DB_PATH)

    parser = argparse.ArgumentParser(
        description="Agent Strategist — Hermes Event Consumer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --consume --limit 10
  %(prog)s --consume --limit 5 --dry-run
  %(prog)s --consume --event-type content.decay_detected
  %(prog)s --consume --event-type seo.content_gap_detected --limit 20
""",
    )
    parser.add_argument("--consume", action="store_true", help="Consume events from inbox")
    parser.add_argument("--limit", type=int, default=10, help="Max events to process (default: 10)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing/moving files")
    parser.add_argument("--event-type", type=str, choices=list(SUPPORTED_EVENT_TYPES), help="Filter by event type")
    parser.add_argument("--inbox", type=str, default=_inbox_default, help="Hermes events inbox directory")
    parser.add_argument("--db", type=str, default=_db_path, help="Path to affiliate-machine.db")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.inbox:
        os.environ["HERMES_EVENTS_DIR"] = args.inbox.strip()
        get_hermes_paths(reset_cache=True)
    if args.db:
        DB_PATH = Path(args.db)

    if args.consume:
        processed = consume_events(
            event_type=args.event_type,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        print(f"\n[SUMMARY] Processed {processed} event(s).")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
