#!/usr/bin/env python3
"""
Agent Translator — Hermes Event Consumer for Content Translation

Consumes content.translation_needed events from the Hermes inbox (env paths via ``scripts/hermes_bus.py``).
For each event:
  1. Reads event payload (article slug, site, source locale, target locale)
  2. Resolves the source MDX file path from the repo
  3. Extracts article content, products, ASINs from source locale
  4. Translates content to target locale using Anthropic Claude API
  5. Validates ASINs on the target Amazon store (amazon.de, amazon.es, etc.)
  6. Replaces invalid ASINs with equivalents found via search
  7. Adapts prices (USD→EUR, etc.) and cultural references
  8. Generates locale-specific SEO meta (title, description in target language)
  9. Writes translated content to content-<locale>/ directory
  10. Emits content.translated event for reviewer

Handles:
  - Primary-first sites (FR→EN/DE/ES/IT)
  - Translation-first sites (EN→DE/ES/FR/IT/JA)
  - ASIN validation and replacement per locale
  - Currency conversion and cultural adaptation
  - SEO metadata generation in target language

Usage:
    python3 agent_translator.py --consume [--limit N] [--dry-run] [--test]
    python3 agent_translator.py --consume --limit 5 --dry-run
    python3 agent_translator.py --test
    python3 agent_translator.py --test --dry-run
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from affiliate_paths import portfolio_root
from hermes_bus import (
    claim_inbox_json,
    complete_claimed_event,
    ensure_hermes_dirs,
    fail_claimed_event,
    plain_move,
)

# ── Load .env from scripts directory ───────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent


def _load_env():
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    if key not in os.environ:
                        os.environ[key] = val


_load_env()

# ── Configuration ──────────────────────────────────────────────────────────

DB_PATH = Path.home() / "affiliate-machine.db"
_HP_INIT = ensure_hermes_dirs()
EVENTS_DIR = _HP_INIT.base
INBOX_DIR = _HP_INIT.inbox
PROCESSING_DIR = _HP_INIT.processing
COMPLETED_DIR = _HP_INIT.completed
FAILED_DIR = _HP_INIT.failed

BASE_DIR = portfolio_root()

# Frontmatter keys we preserve / expect
FRONTMATTER_SCHEMA = {
    "slug", "title", "meta_description", "description", "date", "og_image", "thumbnail",
    "angle", "target_keyword", "keywords",
    "datePublished", "dateModified", "publishedAt", "updatedAt",
    "image", "author", "authorId", "category", "type", "locale",
    "hreflangGroup", "topPick", "priceRange", "verdict", "products",
    "criteria", "faq", "targetAudience", "budgetRanges", "translationSlugs",
}

LOCALES = {"fr", "en", "de", "es", "it", "uk", "ja"}

# Amazon store mapping by locale
AMAZON_STORES = {
    "fr": {"domain": "amazon.fr", "currency": "EUR", "language": "fr", "tag": "zoomzen05-21"},
    "en": {"domain": "amazon.com", "currency": "USD", "language": "en", "tag": "zoomzus-20"},
    "de": {"domain": "amazon.de", "currency": "EUR", "language": "de", "tag": "zoomzen-21"},
    "es": {"domain": "amazon.es", "currency": "EUR", "language": "es", "tag": "zoomzen08-21"},
    "it": {"domain": "amazon.it", "currency": "EUR", "language": "it", "tag": "zoomzen01-21"},
    "uk": {"domain": "amazon.co.uk", "currency": "GBP", "language": "en", "tag": "zoomzen07-21"},
    "ja": {"domain": "amazon.co.jp", "currency": "JPY", "language": "ja", "tag": "zoomzen-21"},
}

# Site configurations (mirrors agent_writer.py)
SITES = {
    "aspirateur": {
        "name": "Aspirateur",
        "repo_path": BASE_DIR / "aspirateur",
        "is_monorepo": False,
        "default_locale": "fr",
        "secondary_locales": ["en", "de", "es", "it"],
        "strategy": "primary_first",
    },
    "bureau": {
        "name": "Bureau",
        "repo_path": BASE_DIR / "bureau",
        "is_monorepo": False,
        "default_locale": "fr",
        "secondary_locales": ["en", "de", "es", "it"],
        "strategy": "primary_first",
    },
    "matelas": {
        "name": "Matelas",
        "repo_path": BASE_DIR / "matelas",
        "is_monorepo": False,
        "default_locale": "fr",
        "secondary_locales": ["en", "de", "es", "it"],
        "strategy": "primary_first",
    },
    "cafe": {
        "name": "Cafe",
        "repo_path": BASE_DIR / "cafe",
        "is_monorepo": False,
        "default_locale": "fr",
        "secondary_locales": ["en", "de", "es", "it"],
        "strategy": "primary_first",
    },
    "pixinstant": {
        "name": "PixInstant",
        "repo_path": BASE_DIR / "pixinstant",
        "is_monorepo": False,
        "default_locale": "fr",
        "secondary_locales": [],
        "strategy": "primary_first",
    },
    "airpurify": {
        "name": "AirPurify",
        "repo_path": BASE_DIR / "affiliate-suite" / "apps" / "airpurify",
        "is_monorepo": True,
        "default_locale": "en",
        "secondary_locales": ["de", "es", "fr", "it", "ja"],
        "strategy": "translate",
    },
    "safehive": {
        "name": "SafeHive",
        "repo_path": BASE_DIR / "affiliate-suite" / "apps" / "safehive",
        "is_monorepo": True,
        "default_locale": "en",
        "secondary_locales": ["de", "es", "fr", "it", "ja"],
        "strategy": "translate",
    },
    "pawhive": {
        "name": "PawHive",
        "repo_path": BASE_DIR / "affiliate-suite" / "apps" / "pawhive",
        "is_monorepo": True,
        "default_locale": "en",
        "secondary_locales": ["de", "es", "fr", "it", "ja"],
        "strategy": "translate",
    },
}

# Currency conversion rates (approximate, used as guidance in prompts)
CURRENCY_RATES = {
    ("USD", "EUR"): 0.92,
    ("EUR", "USD"): 1.09,
    ("GBP", "EUR"): 1.17,
    ("EUR", "GBP"): 0.85,
    ("USD", "GBP"): 0.79,
    ("GBP", "USD"): 1.27,
    ("JPY", "EUR"): 0.0062,
    ("EUR", "JPY"): 161.0,
}

# Quality gates
TITLE_MIN = 40
TITLE_MAX = 70
META_MIN = 140
META_MAX = 170

MAX_RETRIES = 3


def ensure_dirs() -> None:
    ensure_hermes_dirs()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit_event(
    event_type: str,
    payload: dict,
    priority: int = 3,
    routing_key: str = "agent.reviewer",
    target_agent: Optional[str] = None,
) -> Path:
    """Emit a follow-up event to the Hermes event bus."""
    ensure_dirs()
    event = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "priority": priority,
        "payload": payload,
        "timestamp": now_iso(),
        "source_agent": "agent-translator",
        "routing_key": routing_key,
    }
    if target_agent:
        event["target_agent"] = target_agent
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{event_type.replace('.', '_')}_{event['id'][:8]}.json"
    path = EVENTS_DIR / "inbox" / filename
    path.write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  📤 Emitted: {event_type} → {filename}")
    return path


def read_event(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[ERROR] Failed to read event {path.name}: {exc}")
        return None


def move_event(src: Path, dst_dir: Path, dry_run: bool = False) -> Optional[Path]:
    return plain_move(src, dst_dir, dry_run=dry_run)


# ── Frontmatter helpers (mirrors agent_writer.py) ──────────────────────────

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_frontmatter(content: str) -> tuple:
    """Return (frontmatter_dict, body) from MDX content."""
    if not content.startswith("---"):
        return {}, content
    m = FM_RE.match(content)
    if not m:
        return {}, content
    fm_text = m.group(1)
    body = m.group(2)
    fm = {}
    current_key = None
    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            item = stripped[2:].strip().strip('"').strip("'")
            if current_key:
                if isinstance(fm.get(current_key), list):
                    fm[current_key].append(item)
                else:
                    # Convert scalar to list, but skip empty string values
                    old_val = fm.get(current_key, "")
                    if old_val == "":
                        fm[current_key] = [item]
                    else:
                        fm[current_key] = [old_val, item]
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            fm[key] = value
            current_key = key
    return fm, body


def build_frontmatter(fm: dict) -> str:
    """Build YAML frontmatter string from dict."""
    lines = ["---"]
    for key, value in fm.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif isinstance(value, str):
            if any(c in value for c in [":", "#", "'", '"', "\n"]):
                escaped = value.replace('"', '\\"')
                lines.append(f'{key}: "{escaped}"')
            else:
                lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def count_words(body: str) -> int:
    return len(body.split())


# ── Path resolution ──────────────────────────────────────────────────────

LOCALE_DIR_NAMES = {"en", "de", "es", "it", "uk", "ja"}


def resolve_mdx_path(site_slug: str, article_slug: str, locale_hint: Optional[str] = None) -> Optional[Path]:
    """Resolve the MDX file path for an existing article in the given locale.

    Locale-aware: on parallel-layout sites (matelas/bureau/cafe — `content/`
    default + `content-<loc>/`) and nested-layout sites (pixinstant — `content/`
    + `content/<loc>/`), the source file MUST come from the requested locale or
    we'll translate the wrong language.

    Algorithm:
      - Default locale → search only under `content/` excluding `content/<loc>/`
        subtrees.
      - Non-default locale → search `content-<loc>/**` and `content/<loc>/**`.
      - Monorepo (airpurify/safehive/pawhive) → search `content/**/<loc>/**`
        scoped to the explicit locale only.

    Returns None if no matching file exists in the requested locale.
    """
    site = SITES.get(site_slug)
    if not site:
        print(f"  ⚠️ Unknown site: {site_slug}")
        return None

    repo = site["repo_path"]
    if not repo.exists():
        print(f"  ⚠️ Repo not found: {repo}")
        return None

    locale = locale_hint or site["default_locale"]
    locale = locale.replace("content-", "").replace("content", site["default_locale"])
    if locale not in LOCALES:
        locale = site["default_locale"]

    default_locale = site["default_locale"]

    search_patterns: List[Path] = []

    if site["is_monorepo"]:
        for subdir in ["reviews", "guides", "pillars", "data", "comparatifs", "articles", "tests", "avis", "pages"]:
            search_patterns.append(repo / "content" / subdir / locale / f"{article_slug}.mdx")
            search_patterns.append(repo / "deploy" / "content" / subdir / locale / f"{article_slug}.mdx")
        search_patterns.append(repo / "content" / locale / f"{article_slug}.mdx")
        search_patterns.append(repo / "deploy" / "content" / locale / f"{article_slug}.mdx")
    else:
        # Parallel layout: content-<locale>/<slug>.mdx
        search_patterns.append(repo / f"content-{locale}" / f"{article_slug}.mdx")
        # Nested layout: content/<locale>/<slug>.mdx (non-default only — never
        # for default locale, which lives directly under content/).
        if locale != default_locale:
            search_patterns.append(repo / "content" / locale / f"{article_slug}.mdx")

    for p in search_patterns:
        if p.exists():
            return p

    # Locale-scoped recursive fallback. Critical: scope rglob to the correct
    # subtree so a FR resolve cannot return a content-en/ file (and vice versa).
    if site["is_monorepo"]:
        # content/<any>/<locale>/**/<slug>.mdx — scope to the explicit locale.
        for candidate in (repo / "content").glob(f"*/{locale}/**/*.mdx"):
            if candidate.stem == article_slug:
                return candidate
        deploy_root = repo / "deploy" / "content"
        if deploy_root.exists():
            for candidate in deploy_root.glob(f"*/{locale}/**/*.mdx"):
                if candidate.stem == article_slug:
                    return candidate
    elif locale == default_locale:
        # Default locale → walk content/ but skip content/<other-locale>/ subtrees.
        content_dir = repo / "content"
        if content_dir.exists():
            for candidate in content_dir.rglob("*.mdx"):
                try:
                    rel = candidate.relative_to(content_dir).parts
                except ValueError:
                    continue
                if rel and rel[0] in LOCALE_DIR_NAMES:
                    continue  # nested locale subdir → not default-locale content
                if candidate.stem == article_slug:
                    return candidate
    else:
        # Non-default locale → search content-<loc>/ AND content/<loc>/ subtrees.
        locale_dir = repo / f"content-{locale}"
        if locale_dir.exists():
            for candidate in locale_dir.rglob("*.mdx"):
                if candidate.stem == article_slug:
                    return candidate
        content_locale_dir = repo / "content" / locale
        if content_locale_dir.exists():
            for candidate in content_locale_dir.rglob("*.mdx"):
                if candidate.stem == article_slug:
                    return candidate

    print(f"  ⚠️ Could not resolve MDX path for {site_slug}/{article_slug} (locale={locale})")
    return None


def resolve_target_path(site_slug: str, article_slug: str, target_locale: str, source_path: Path) -> Path:
    """Determine the target MDX file path for translated content."""
    site = SITES.get(site_slug)
    if not site:
        return BASE_DIR / f"{site_slug}/content-{target_locale}/{article_slug}.mdx"

    repo = site["repo_path"]

    if site["is_monorepo"]:
        # Mirror the source path structure but with target locale
        try:
            relative = source_path.relative_to(repo)
        except ValueError:
            relative = Path(source_path.name)
        parts = list(relative.parts)
        # Replace locale in path
        for i, part in enumerate(parts):
            if part in LOCALES:
                parts[i] = target_locale
                break
        target_path = repo / Path(*parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        return target_path
    else:
        # Legacy: content-<locale>/<slug>.mdx or content/<locale>/<slug>.mdx
        # Try to mirror subdirectory structure from source
        try:
            relative = source_path.relative_to(repo)
            parts = list(relative.parts)
            # Replace the first directory component (content or content-<locale>)
            if parts:
                if parts[0].startswith("content-"):
                    parts[0] = f"content-{target_locale}"
                elif parts[0] == "content" and len(parts) > 1:
                    # content/comparatifs/... -> content-en/comparatifs/...
                    parts[0] = f"content-{target_locale}"
                target_path = repo / Path(*parts)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                return target_path
        except ValueError:
            pass
        # Fallback flat structure
        target_dir = repo / f"content-{target_locale}"
        if not target_dir.exists():
            target_dir = repo / "content" / target_locale
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{article_slug}.mdx"


# ── Claude API ───────────────────────────────────────────────────────────

def get_claude_api_key() -> Optional[str]:
    return os.environ.get("ANTHROPIC_API_KEY")


def call_claude(prompt: str, max_tokens: int = 4000) -> str:
    """Call Anthropic Claude API via HTTP."""
    api_key = get_claude_api_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")

    import urllib.request
    import urllib.error

    # Read model from env, fallback to hardcoded default
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Claude API HTTP {e.code}: {body}")
    except Exception as e:
        raise RuntimeError(f"Claude API error: {e}")


# ── ASIN extraction and validation ─────────────────────────────────────────

ASIN_RE = re.compile(r"\b[A-Z0-9]{10}\b")
INVALID_ASINS = {
    "FRONTMATTE", "GREENGUARD", "ASINHERE", "ASIN-HERE",
    "ASIN", "PLACEHOLDER", "EXAMPLE", "XXXXXXXXXX",
    "AUFGRECHTE", "BESTCHOICE", "TOPPRODUCT", "RECOMMENDED",
}


def extract_asins(text: str) -> Set[str]:
    """Extract all potential ASINs from text."""
    candidates = ASIN_RE.findall(text)
    return {c for c in candidates if c not in INVALID_ASINS and any(d.isdigit() for d in c)}


def validate_asin_for_locale(asin: str, locale: str, dry_run: bool = False) -> Dict[str, Any]:
    """Validate an ASIN for a specific locale using asin_validator if available."""
    if dry_run:
        return {"valid": True, "asin": asin, "locale": locale, "source": "dry-run"}

    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from asin_validator import ASINValidator
        validator = ASINValidator(permissive=True)
        # Map locale to country code
        country_map = {"fr": "FR", "en": "US", "de": "DE", "es": "ES", "it": "IT", "uk": "UK", "ja": "JP"}
        country = country_map.get(locale, "FR")
        result = validator.validate(asin, country)
        result["locale"] = locale
        result["store"] = AMAZON_STORES.get(locale, {}).get("domain", "amazon.fr")
        return result
    except ImportError:
        # Fallback: format-only validation
        if re.match(r"^[A-Z0-9]{10}$", asin) and any(c.isdigit() for c in asin):
            return {"valid": True, "asin": asin, "locale": locale, "source": "format", "is_new": True}
        return {"valid": False, "asin": asin, "locale": locale, "source": "format", "error": "Invalid ASIN format"}


def find_asin_replacement(asin: str, source_locale: str, target_locale: str, product_name: str = "") -> Optional[str]:
    """Find an equivalent ASIN for a different locale."""
    # Many ASINs are global. First validate the same ASIN on target store.
    result = validate_asin_for_locale(asin, target_locale)
    if result.get("valid"):
        return asin

    # If invalid, we would need product search. For now, return None
    # TODO: Implement Apify search by product name on target store
    print(f"    ⚠️ ASIN {asin} invalid on {target_locale} store, no replacement found")
    return None


def validate_and_replace_asins(content: str, asins: Set[str], source_locale: str, target_locale: str, dry_run: bool = False) -> Tuple[str, Dict[str, Any]]:
    """Validate all ASINs for target locale and replace invalid ones."""
    store = AMAZON_STORES.get(target_locale, AMAZON_STORES["en"])
    replacements = {}
    validation_results = {}

    for asin in asins:
        result = validate_asin_for_locale(asin, target_locale, dry_run=dry_run)
        validation_results[asin] = result

        if not result.get("valid"):
            replacement = find_asin_replacement(asin, source_locale, target_locale)
            if replacement:
                replacements[asin] = replacement
                print(f"    🔄 Replaced ASIN {asin} → {replacement} for {store['domain']}")
            else:
                print(f"    ❌ ASIN {asin} invalid on {store['domain']} and no replacement found")
        else:
            print(f"    ✅ ASIN {asin} valid on {store['domain']}")

    # Apply replacements in content
    new_content = content
    for old, new in replacements.items():
        new_content = re.sub(rf"\b{old}\b", new, new_content)

    return new_content, {"replacements": replacements, "validation_results": validation_results}


# ── Translation prompt builder ───────────────────────────────────────────

def build_translation_prompt(
    fm: dict,
    body: str,
    source_locale: str,
    target_locale: str,
    site_slug: str,
    asins: Set[str],
    asin_validation: Dict[str, Any],
) -> str:
    """Build a comprehensive prompt for Claude to translate content."""
    source_store = AMAZON_STORES.get(source_locale, AMAZON_STORES["en"])
    target_store = AMAZON_STORES.get(target_locale, AMAZON_STORES["en"])

    locale_names = {
        "fr": "French", "en": "English", "de": "German",
        "es": "Spanish", "it": "Italian", "uk": "British English",
        "ja": "Japanese",
    }

    # Locale-specific instructions
    locale_instructions = {
        "fr": """
- Use formal "vous" to address readers
- French punctuation: space before : ! ?
- Currency: € with space (e.g., "199 €")
- Metric units (cm, kg, m²)
- Date format: DD/MM/YYYY
- Use French idioms naturally
- Flesch readability target: 50-70
""",
        "en": """
- Use American English (color, not colour)
- Currency: $ (e.g., "$199")
- Imperial units with metric in parentheses
- Date format: MM/DD/YYYY
- Flesch readability target: 60-70
""",
        "de": """
- Use formal "Sie" to address readers
- Currency: € with space (e.g., "199 €")
- Metric units (cm, kg, m²)
- Date format: DD.MM.YYYY
- Compound words are natural
- Flesch readability target: 50-70
""",
        "es": """
- Use informal "tú" to address readers
- Currency: € with space (e.g., "199 €")
- Metric units (cm, kg, m²)
- Date format: DD/MM/YYYY
- Flesch readability target: 50-70
""",
        "it": """
- Use informal "tu" to address readers
- Currency: € with space (e.g., "199 €")
- Metric units (cm, kg, m²)
- Date format: DD/MM/YYYY
- Flesch readability target: 50-70
""",
        "uk": """
- Use British English (colour, not color)
- Currency: £ (e.g., "£199")
- Metric units preferred
- Date format: DD/MM/YYYY
- Flesch readability target: 60-70
""",
        "ja": """
- Use polite form (desu/masu)
- Currency: ¥ (e.g., "¥19,900")
- Metric units (cm, kg, 畳 for room size)
- Date format: YYYY/MM/DD
- Flesch readability target: N/A (use simple, clear expressions)
""",
    }

    # Cultural reference adaptations
    cultural_adaptations = {
        ("en", "fr"): "Adapt cultural references: Black Friday → Vendredi Noir, inches → cm, lbs → kg, Fahrenheit → Celsius",
        ("fr", "en"): "Adapt cultural references: Vendredi Noir → Black Friday, cm → inches with metric in parens, kg → lbs, Celsius → Fahrenheit",
        ("en", "de"): "Adapt cultural references: inches → cm, lbs → kg, Fahrenheit → Celsius, use German product names where known",
        ("fr", "de"): "Adapt cultural references: use German conventions, formal address, metric units",
        ("en", "es"): "Adapt cultural references: inches → cm, lbs → kg, use Spanish product names where known",
        ("en", "it"): "Adapt cultural references: inches → cm, lbs → kg, use Italian product names where known",
        ("en", "ja"): "Adapt cultural references: use metric, polite forms, yen pricing, tatami for room sizes",
    }

    # Build ASIN instructions
    asin_instructions = []
    for asin in asins:
        result = asin_validation.get("validation_results", {}).get(asin, {})
        if result.get("valid"):
            asin_instructions.append(f"- Keep ASIN {asin} (valid on {target_store['domain']})")
        elif asin in asin_validation.get("replacements", {}):
            new_asin = asin_validation["replacements"][asin]
            asin_instructions.append(f"- Replace ASIN {asin} with {new_asin} on {target_store['domain']}")
        else:
            asin_instructions.append(f"- ASIN {asin} may be invalid on {target_store['domain']}; verify or replace with equivalent product")

    # Currency conversion guidance
    conversion_guidance = ""
    rate_key = (source_store["currency"], target_store["currency"])
    if rate_key in CURRENCY_RATES:
        rate = CURRENCY_RATES[rate_key]
        conversion_guidance = f"\n- Convert prices approximately: 1 {source_store['currency']} ≈ {rate} {target_store['currency']}\n"

    # Readability target
    readability_target = {
        "fr": "Flesch score 50-70 (simple French, short sentences 15-20 words)",
        "en": "Flesch score 60-70 (simple English, 8th-grade level)",
        "de": "Flesch score 50-70 (simple German, short sentences)",
        "es": "Flesch score 50-70 (simple Spanish, short sentences)",
        "it": "Flesch score 50-70 (simple Italian, short sentences)",
        "uk": "Flesch score 60-70 (simple British English)",
        "ja": "Simple, clear Japanese (desu/masu form, avoid complex kanji compounds)",
    }

    cultural_note = cultural_adaptations.get((source_locale, target_locale), "Adapt cultural references appropriately for the target market")

    prompt = f"""You are an expert SEO content translator for affiliate marketing. Translate the following article from {locale_names.get(source_locale, source_locale)} to {locale_names.get(target_locale, target_locale)}.

SITE: {SITES.get(site_slug, {}).get('name', site_slug)}
SOURCE LOCALE: {source_locale} ({source_store['domain']})
TARGET LOCALE: {target_locale} ({target_store['domain']})

ORIGINAL FRONTMATTER:
{json.dumps(fm, indent=2, ensure_ascii=False)}

ORIGINAL ARTICLE BODY:
---
{body}
---

CRITICAL TRANSLATION INSTRUCTIONS:

1. TRANSLATE ALL CONTENT
   - Translate every heading, paragraph, list item, and table cell
   - Preserve MDX formatting (## headings, **bold**, bullet lists, tables)
   - Keep comparison tables structurally identical

2. SEO METADATA (MUST BE IN TARGET LANGUAGE)
   - Title: {TITLE_MIN}-{TITLE_MAX} characters exactly
   - Meta description: {META_MIN}-{META_MAX} characters exactly
   - Include target keyword in first 100 words
   - Generate 3-5 target keywords in {locale_names.get(target_locale, target_locale)}

3. AMAZON LINKS AND ASINS
   - Replace all Amazon domain references: {source_store['domain']} → {target_store['domain']}
   - Use affiliate tag: {target_store['tag']}
{chr(10).join(asin_instructions) if asin_instructions else '   - Preserve all ASINs (they are mostly global)'}

4. CURRENCY AND PRICING
   - Replace {source_store['currency']} with {target_store['currency']}
{conversion_guidance}

5. CULTURAL ADAPTATION
   - {cultural_note}
   - Adapt examples to local brands/products where appropriate
   - Use local date formats and measurement units

6. LOCALE-SPECIFIC STYLE
{locale_instructions.get(target_locale, locale_instructions.get('en', ''))}

7. READABILITY
   - Target: {readability_target.get(target_locale, 'Simple language, short sentences')}
   - Use active voice
   - One idea per sentence
   - Short paragraphs (2-4 sentences)

8. STRUCTURE PRESERVATION
   - Keep the same number of H2 sections
   - Preserve comparison tables (translate content, keep structure)
   - Keep FAQ section if present
   - Preserve internal links (translate anchor text, keep slugs)

9. PRODUCT DATA
   - Translate product names if they have local equivalents
   - Keep brand names in original language (e.g., iRobot, Dyson)
   - Translate feature descriptions
   - Adapt price ranges to local market

OUTPUT FORMAT:
Return ONLY the complete translated MDX content with frontmatter and body.
The output must start with --- and contain valid frontmatter, then the translated body.
Do NOT wrap in markdown code fences.

QUALITY GATES (the output will be checked):
- Title length: {TITLE_MIN}-{TITLE_MAX} chars
- Meta description: {META_MIN}-{META_MAX} chars
- Plagiarism: completely original translation (not machine-translated sounding)
- Readability: {readability_target.get(target_locale, 'appropriate for locale')}
"""
    return prompt


# ── Quality validation ─────────────────────────────────────────────────────

def validate_translation_quality(fm: dict, body: str, target_locale: str) -> Dict[str, Any]:
    """Validate translated content against quality gates."""
    title = fm.get("title", "")
    meta = fm.get("meta_description", "") or fm.get("description", "")
    word_count = count_words(body)

    issues = []

    if len(title) < TITLE_MIN or len(title) > TITLE_MAX:
        issues.append(f"Title length {len(title)} not in {TITLE_MIN}-{TITLE_MAX}")

    if meta:
        if len(meta) < META_MIN or len(meta) > META_MAX:
            issues.append(f"Meta description length {len(meta)} not in {META_MIN}-{META_MAX}")

    # Check for remaining source-locale artifacts
    source_artifacts = {
        "fr": ["€", "le meilleur", "notre guide"],
        "en": ["$", "the best", "our guide", "buy now"],
        "de": ["€", "der beste", "unser guide"],
        "es": ["€", "el mejor", "nuestra guía"],
        "it": ["€", "il migliore", "la nostra guida"],
    }

    # Simple check: if target is not English, ensure no untranslated English paragraphs
    if target_locale != "en" and target_locale != "uk":
        # Check if large chunks are still English
        english_words = {"the", "and", "for", "with", "best", "guide", "review", "top"}
        body_lower = body.lower()
        english_count = sum(1 for w in english_words if f" {w} " in body_lower or f"\n{w} " in body_lower)
        if english_count > 10:
            issues.append(f"Possible untranslated content detected ({english_count} English words)")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "title_length": len(title),
        "meta_length": len(meta),
        "word_count": word_count,
    }


# ── Git helpers ────────────────────────────────────────────────────────────

def git_branch_exists(repo: Path, branch: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        return branch in result.stdout
    except subprocess.CalledProcessError:
        return False


def git_create_branch(repo: Path, branch: str, base: str = "main") -> bool:
    try:
        subprocess.run(["git", "checkout", "-b", branch, base], cwd=repo, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError:
        try:
            subprocess.run(["git", "checkout", "-b", branch, "master"], cwd=repo, capture_output=True, text=True, check=True)
            return True
        except subprocess.CalledProcessError:
            return False


def git_commit_file(repo: Path, file_path: Path, message: str, branch: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"  [DRY-RUN] Would git add + commit {file_path.name} on branch {branch}")
        return True
    try:
        subprocess.run(["git", "checkout", branch], cwd=repo, capture_output=True, text=True, check=False)
        subprocess.run(["git", "add", str(file_path)], cwd=repo, capture_output=True, text=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ Git commit failed: {e.stderr}")
        return False


def git_push_branch(repo: Path, branch: str, dry_run: bool = False) -> bool:
    """Push a local feature branch so agent-publisher can open/update a PR."""
    if dry_run:
        print(f"  [DRY-RUN] Would git push -u origin {branch}")
        return True
    try:
        subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        print(f"  ✅ Pushed branch: {branch}")
        return True
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        print(f"  ⚠️ Git push failed: {err[:400]}")
        return False


# ── Core processing ──────────────────────────────────────────────────────────

def process_translation_event(event: Dict[str, Any], dry_run: bool = False) -> bool:
    """Process a single content.translation_needed event."""
    payload = event.get("payload", {})
    # Syndication dispatcher uses `site` + `slug`; legacy events use site_slug + article_slug.
    site_slug = (payload.get("site_slug") or payload.get("site") or "").strip()
    article_slug = (payload.get("article_slug") or payload.get("slug") or "").strip()
    source_locale = payload.get("source_locale", "")
    target_locale = payload.get("target_locale", "")
    article_id = payload.get("article_id")

    if not site_slug or not article_slug:
        print(f"  ❌ Missing site_slug or article_slug in event {event.get('id')}")
        return False

    if not source_locale or not target_locale:
        print(f"  ❌ Missing source_locale or target_locale in event {event.get('id')}")
        return False

    if target_locale not in LOCALES:
        print(f"  ❌ Invalid target_locale: {target_locale}")
        return False

    print(f"\n🌐 Translating: {site_slug}/{article_slug}")
    print(f"   {source_locale} → {target_locale}")

    site = SITES.get(site_slug)
    if not site:
        print(f"  ⚠️ Unknown site: {site_slug}")
        return False

    # 1. Resolve source MDX path
    source_path = resolve_mdx_path(site_slug, article_slug, source_locale)
    if not source_path:
        print(f"  ❌ Source file not found: {site_slug}/{article_slug} ({source_locale})")
        return False

    # 2. Read source content
    try:
        original_content = source_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  ❌ Failed to read {source_path}: {e}")
        return False

    fm, body = parse_frontmatter(original_content)
    source_word_count = count_words(body)
    print(f"  📄 Source: {source_path}")
    print(f"  📊 Source word count: {source_word_count}")

    # 3. Extract ASINs
    asins = extract_asins(original_content)
    print(f"  🔍 Found {len(asins)} ASINs: {', '.join(asins) if asins else 'none'}")

    # 4. Validate ASINs for target locale
    if asins and not dry_run:
        validated_content, asin_validation = validate_and_replace_asins(
            original_content, asins, source_locale, target_locale, dry_run=dry_run
        )
    else:
        asin_validation = {"replacements": {}, "validation_results": {}}
        validated_content = original_content

    # Re-parse after ASIN replacement
    fm, body = parse_frontmatter(validated_content)

    # 5. Build translation prompt
    prompt = build_translation_prompt(fm, body, source_locale, target_locale, site_slug, asins, asin_validation)

    # 6. Call Claude API
    if dry_run:
        print(f"  [DRY-RUN] Would call Claude API (prompt length={len(prompt)} chars)")
        # Generate a mock translation for dry-run
        translated_fm = dict(fm)
        translated_fm["title"] = f"[DRY-RUN] Translated: {fm.get('title', '')[:50]}"
        translated_fm["description"] = f"[DRY-RUN] Translated meta for {target_locale}"
        translated_fm["locale"] = target_locale
        translated_body = f"[DRY-RUN] Translated body ({source_word_count} words) from {source_locale} to {target_locale}\n\n{body[:200]}..."
    else:
        print(f"  🤖 Calling Claude API for translation...")
        try:
            response = call_claude(prompt, max_tokens=8000)
            # Strip any accidental code fences
            response = re.sub(r"^```(?:mdx|markdown)?\s*\n", "", response)
            response = re.sub(r"\n```\s*$", "", response)

            # Parse the response
            translated_fm, translated_body = parse_frontmatter(response)

            # If Claude didn't return frontmatter, preserve original and use response as body
            if not translated_fm:
                translated_fm = dict(fm)
                translated_fm["locale"] = target_locale
                translated_body = response

        except Exception as e:
            print(f"  ❌ Claude translation failed: {e}")
            return False

    # 7. Ensure essential frontmatter fields
    translated_fm["locale"] = target_locale
    if "slug" not in translated_fm:
        translated_fm["slug"] = article_slug
    if "translationSlugs" not in translated_fm:
        translated_fm["translationSlugs"] = {}
    if isinstance(translated_fm.get("translationSlugs"), dict):
        translated_fm["translationSlugs"][source_locale] = article_slug
        translated_fm["translationSlugs"][target_locale] = article_slug

    # Update dates
    today = datetime.now().strftime("%Y-%m-%d")
    if "date" not in translated_fm and "datePublished" not in translated_fm and "publishedAt" not in translated_fm:
        translated_fm["date"] = today
    if "dateModified" in translated_fm:
        translated_fm["dateModified"] = today
    if "updatedAt" in translated_fm:
        translated_fm["updatedAt"] = today

    # 8. Quality validation
    quality = validate_translation_quality(translated_fm, translated_body, target_locale)
    if not quality["valid"]:
        print(f"  ⚠️ Quality issues detected:")
        for issue in quality["issues"]:
            print(f"    - {issue}")
    else:
        print(f"  ✅ Quality gates passed")

    print(f"  📊 Translated word count: {quality['word_count']}")
    print(f"  📏 Title: {quality['title_length']} chars")
    print(f"  📏 Meta: {quality['meta_length']} chars")

    # 9. Determine target path
    target_path = resolve_target_path(site_slug, article_slug, target_locale, source_path)
    print(f"  📝 Target: {target_path}")

    # 10. Assemble and write
    new_frontmatter = build_frontmatter(translated_fm)
    new_content = new_frontmatter + "\n\n" + translated_body.strip() + "\n"

    if dry_run:
        print(f"  [DRY-RUN] Would write {len(new_content)} chars to {target_path}")
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(new_content, encoding="utf-8")
        print(f"  ✅ Written: {target_path}")

    # 11. Git commit + push (publisher consumes seo.fix_applied with use_existing_branch)
    repo_path = site["repo_path"]
    branch_name = ""
    if repo_path and repo_path.exists():
        branch_name = f"content-translate/{site_slug}-{target_locale}-{article_slug[:30]}"
        if not git_branch_exists(repo_path, branch_name):
            git_create_branch(repo_path, branch_name)
        commit_msg = f"content: translate {article_slug} from {source_locale} to {target_locale}"
        git_commit_file(repo_path, target_path, commit_msg, branch_name, dry_run=dry_run)
        git_push_branch(repo_path, branch_name, dry_run=dry_run)
    else:
        print(f"  ⚠️ No git repo found for {site_slug}")

    # 12. Update DB if article_id provided
    if article_id and not dry_run:
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute(
                """UPDATE articles
                   SET translation_status = ?, translated_locales = COALESCE(translated_locales, '') || ? || ',',
                       updated_at = datetime('now')
                   WHERE article_id = ?""",
                ("translated", target_locale, article_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  ⚠️ DB update failed: {e}")

    # 13. Emit content.translated event
    emit_payload = {
        "article_id": article_id,
        "site_slug": site_slug,
        "article_slug": article_slug,
        "source_locale": source_locale,
        "target_locale": target_locale,
        "source_path": str(source_path.relative_to(BASE_DIR)),
        "target_path": str(target_path.relative_to(BASE_DIR)) if target_path else "",
        "word_count": quality["word_count"],
        "title_length": quality["title_length"],
        "meta_length": quality["meta_length"],
        "quality_valid": quality["valid"],
        "quality_issues": quality["issues"],
        "asins_found": list(asins),
        "asin_replacements": asin_validation.get("replacements", {}),
        "branch": branch_name,
    }
    emit_event("content.translated", emit_payload, priority=event.get("priority", 3), routing_key="agent.reviewer")

    # 14. Queue publisher: open PR from the translation branch (same machine / CI with repo access).
    if branch_name and target_path and repo_path and repo_path.exists():
        try:
            rel_file = str(target_path.relative_to(repo_path))
        except ValueError:
            rel_file = str(target_path.name)
        emit_event(
            "seo.fix_applied",
            {
                "site": site_slug,
                "fix_type": "translation",
                "files": [rel_file],
                "use_existing_branch": True,
                "branch": branch_name,
                "summary": f"i18n: {article_slug} ({source_locale}→{target_locale})",
                "translation": {
                    "source_locale": source_locale,
                    "target_locale": target_locale,
                    "article_slug": article_slug,
                },
            },
            priority=2,
            routing_key="agent.publisher",
            target_agent="agent-publisher",
        )

    return True


# ── Event lifecycle ────────────────────────────────────────────────────────

def list_inbox_events() -> List[Path]:
    if not INBOX_DIR.exists():
        return []
    files = sorted(
        [f for f in INBOX_DIR.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda p: p.stat().st_mtime,
    )
    return files


def consume_events(limit: int = 10, dry_run: bool = False) -> int:
    """Consume up to `limit` content.translation_needed events."""
    ensure_dirs()

    all_files = sorted(
        [f for f in INBOX_DIR.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda p: p.stat().st_mtime,
    )

    # Quick filter by filename pattern first
    candidate_files = [p for p in all_files if "content_translation" in p.name]

    # If not enough by filename, scan all
    if len(candidate_files) < limit:
        for p in all_files:
            if p in candidate_files:
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    ev = json.load(f)
                if ev.get("type") == "content.translation_needed":
                    candidate_files.append(p)
            except:
                continue
            if len(candidate_files) >= limit * 2:
                break

    processed = 0
    failed = 0

    for path in candidate_files:
        if processed >= limit:
            break
        event = read_event(path)
        if event is None:
            move_event(path, FAILED_DIR, dry_run=dry_run)
            continue

        event_type = event.get("type", "")

        if event_type == "content.translation_needed":
            proc_path = claim_inbox_json(path, dry_run=dry_run)
            if not proc_path:
                continue
            event["_file_path"] = str(proc_path)

            success = process_translation_event(event, dry_run=dry_run)

            if success:
                complete_claimed_event(event, dry_run=dry_run)
                processed += 1
            else:
                fail_claimed_event(event, dry_run=dry_run)
                failed += 1

    print(f"\n📊 Summary: {processed} processed, {failed} failed, {limit - processed - failed} remaining")
    return processed


# ── Test mode ──────────────────────────────────────────────────────────────

def run_test_mode(dry_run: bool = False) -> int:
    """Run a self-test with a sample translation event."""
    print("\n🧪 TEST MODE: Simulating translation event")
    print("=" * 60)

    # Use a real article if available
    test_site = "aspirateur"
    test_slug = "aspirateur-robot-maison-animaux-comparatif"
    test_source = "fr"
    test_target = "en"

    # Check if source exists
    source_path = resolve_mdx_path(test_site, test_slug, test_source)
    if not source_path:
        print(f"  ⚠️ Test article not found, using synthetic event")
        # Create a synthetic test event
        test_event = {
            "id": "test-" + str(uuid.uuid4())[:8],
            "type": "content.translation_needed",
            "priority": 3,
            "payload": {
                "site_slug": test_site,
                "article_slug": test_slug,
                "source_locale": test_source,
                "target_locale": test_target,
                "article_id": None,
            },
            "timestamp": now_iso(),
        }
    else:
        print(f"  📄 Found test article: {source_path}")
        test_event = {
            "id": "test-" + str(uuid.uuid4())[:8],
            "type": "content.translation_needed",
            "priority": 3,
            "payload": {
                "site_slug": test_site,
                "article_slug": test_slug,
                "source_locale": test_source,
                "target_locale": test_target,
                "article_id": 999999,
            },
            "timestamp": now_iso(),
        }

    print(f"\n  📝 Event payload:")
    print(f"     Site: {test_event['payload']['site_slug']}")
    print(f"     Slug: {test_event['payload']['article_slug']}")
    print(f"     Source: {test_event['payload']['source_locale']}")
    print(f"     Target: {test_event['payload']['target_locale']}")

    success = process_translation_event(test_event, dry_run=dry_run)

    if success:
        print("\n✅ TEST PASSED")
    else:
        print("\n❌ TEST FAILED")

    return 0 if success else 1


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent Translator — Hermes Event Consumer for Content Translation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --consume --limit 5 --dry-run
  %(prog)s --consume --limit 10
  %(prog)s --test
  %(prog)s --test --dry-run
""",
    )
    parser.add_argument("--consume", action="store_true", help="Consume pending content.translation_needed events")
    parser.add_argument("--limit", type=int, default=10, help="Max events to process (default: 10)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing files or calling API")
    parser.add_argument("--test", action="store_true", help="Run test mode with a sample translation")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="Path to SQLite DB")

    args = parser.parse_args()

    if args.test:
        return run_test_mode(dry_run=args.dry_run)

    if not args.consume:
        parser.print_help()
        return 0

    # Validate API key
    if not args.dry_run and not get_claude_api_key():
        print("❌ ANTHROPIC_API_KEY not set. Export it or use --dry-run.")
        return 1

    processed = consume_events(limit=args.limit, dry_run=args.dry_run)
    return 0 if processed >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
