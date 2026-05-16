#!/usr/bin/env python3
"""
Agent CTR Optimizer — finds low-CTR pages and proposes meta title/description
variants for the CRO Optimizer to apply.

Why this exists
---------------
PostHog feature flags are referenced in DEPLOYMENT_CHECKLIST but
NEXT_PUBLIC_POSTHOG_KEY isn't set in production (verified in Playwright
console logs), so a true client-side A/B framework would mean shipping
new client code to 5 sites — high-risk.

Instead, this agent runs the *server-side iteration loop* that actually
moves the needle on most affiliation sites:

  1. For each site, find pages with **high impressions but low CTR vs
     position** (Google's "average CTR by position" curve from
     Backlinko Q4 2025 — adjusted per site).
  2. Generate 3 meta-title variants using proven CTR patterns:
       - **Number + Year** ("7 Best Foo Mattresses for 2026")
       - **Question / Pain** ("Which Foo Mattress Is Worth It in 2026?")
       - **Power word + Trust** ("Ultimate Foo Buyer's Guide (Tested 2026)")
     Plus 1 LLM-generated variant via Anthropic Claude (when
     ANTHROPIC_API_KEY is set).
  3. Score variants vs the current title using:
       - title length (50–60 chars optimal)
       - presence of year, number, power word
       - SERP-pattern fit for query intent
     and queue the best recommendation for human review by default.
     Only emit `cro.meta_variant_proposed` events when `--apply` is
     explicitly set for a reviewed batch.
  4. After 14 days, harvest GSC CTR for any page whose meta was rewritten
     (looked up via `page_metrics.history`); emit `cro.meta_variant_winner`
     events with measured CTR delta so the strategist can multiply the
     pattern across the rest of the site.

Outputs
-------
* `reports/ctr-opportunities-{date}.md`
* `reports/agent_queues/ctr_proposed/{date}.json`
* Events:
    - `cro.meta_variant_proposed` only with `--apply`
    - `cro.meta_variant_winner`   per measured uplift → Strategist
    - `ctr_optimizer.run_completed` → Analytics

Usage
-----
    python3 agent_ctr_optimizer.py --daily --max 10
    python3 agent_ctr_optimizer.py --daily --max 10 --apply
    python3 agent_ctr_optimizer.py --site matelas --min-impressions 500
    python3 agent_ctr_optimizer.py --measure-winners
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
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

# ---------------------------------------------------------------------------
# .env
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

AGENT_NAME = "agent-ctr-optimizer"

CURRENT_YEAR = datetime.now(timezone.utc).year

# Backlinko / Sistrix Q4 2025 average CTR by SERP position (mobile, FR/EN
# blended). Used to compute "expected CTR" for a given position so we know
# which pages are punching below their weight.
EXPECTED_CTR_BY_POSITION: dict[int, float] = {
    1: 0.275, 2: 0.157, 3: 0.110, 4: 0.080, 5: 0.060,
    6: 0.045, 7: 0.035, 8: 0.028, 9: 0.022, 10: 0.018,
    11: 0.014, 12: 0.012, 13: 0.010, 14: 0.009, 15: 0.008,
    20: 0.005, 30: 0.002, 50: 0.001,
}

POWER_WORDS_FR = (
    "ultime", "complet", "exclusif", "vérité", "secrets", "puissant",
    "incroyable", "essentiel", "définitif", "professionnel", "optimal",
)
POWER_WORDS_EN = (
    "ultimate", "complete", "essential", "definitive", "honest", "real",
    "tested", "expert", "best", "proven",
)

SITES: dict[str, dict[str, str]] = {
    "aspirateur": {"domain": "top-aspirateur.fr", "default_locale": "fr"},
    "bureau":     {"domain": "bureau-expert.fr",  "default_locale": "fr"},
    "matelas":    {"domain": "matelas-expert.fr", "default_locale": "fr"},
    "cafe":       {"domain": "brewmance.fr",      "default_locale": "fr"},
    "pixinstant": {"domain": "pixinstant.com",    "default_locale": "fr"},
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def find_low_ctr_pages(site_slug: str, min_impressions: int = 200, top_n: int = 50) -> list[dict[str, Any]]:
    """Aggregate by page from `gsc_page_daily` (preferred) or legacy `page_metrics`."""
    with _conn() as conn:
        q_gsc = """
            SELECT page_path,
                   SUM(impressions) AS impressions,
                   SUM(clicks)      AS clicks,
                   AVG(position)    AS position
            FROM gsc_page_daily
            WHERE site_slug = ? AND impressions IS NOT NULL
            GROUP BY page_path
            HAVING SUM(impressions) >= ?
            ORDER BY SUM(impressions) DESC
            LIMIT ?
        """
        q_legacy = """
            SELECT page_path,
                   SUM(impressions) AS impressions,
                   SUM(clicks)      AS clicks,
                   AVG(position)    AS position
            FROM page_metrics
            WHERE site_slug = ? AND impressions IS NOT NULL
            GROUP BY page_path
            HAVING SUM(impressions) >= ?
            ORDER BY SUM(impressions) DESC
            LIMIT ?
        """
        if _has_table(conn, "gsc_page_daily"):
            chk = conn.execute(
                "SELECT 1 FROM gsc_page_daily WHERE site_slug = ? LIMIT 1",
                (site_slug,),
            ).fetchone()
            if chk:
                rows = conn.execute(
                    q_gsc, (site_slug, min_impressions, top_n * 3)
                ).fetchall()
            else:
                rows = []
        else:
            rows = []
        if not rows:
            try:
                rows = conn.execute(
                    q_legacy, (site_slug, min_impressions, top_n * 3)
                ).fetchall()
            except sqlite3.Error:
                rows = []

    out: list[dict[str, Any]] = []
    for r in rows:
        impressions = int(r["impressions"] or 0)
        clicks = int(r["clicks"] or 0)
        position = float(r["position"] or 0)
        if impressions == 0:
            continue
        actual_ctr = clicks / impressions
        expected = _expected_ctr(position)
        ratio = actual_ctr / expected if expected > 0 else 1.0
        # Lift opportunity = clicks we'd gain if we hit expected CTR
        opportunity_clicks = max(0, int(impressions * expected) - clicks)
        if ratio < 0.7 and opportunity_clicks >= 5:
            out.append({
                "page_path": r["page_path"],
                "impressions": impressions,
                "clicks": clicks,
                "actual_ctr": round(actual_ctr, 4),
                "position": round(position, 1),
                "expected_ctr": round(expected, 4),
                "ctr_ratio": round(ratio, 2),
                "opportunity_clicks": opportunity_clicks,
            })
    out.sort(key=lambda x: -x["opportunity_clicks"])
    return out[:top_n]


def _expected_ctr(position: float) -> float:
    if position <= 0:
        return 0.0
    pos_int = max(1, int(round(position)))
    if pos_int in EXPECTED_CTR_BY_POSITION:
        return EXPECTED_CTR_BY_POSITION[pos_int]
    # Interpolate to nearest known buckets.
    keys = sorted(EXPECTED_CTR_BY_POSITION.keys())
    for k in keys:
        if k >= pos_int:
            return EXPECTED_CTR_BY_POSITION[k]
    return EXPECTED_CTR_BY_POSITION[max(keys)]


# ---------------------------------------------------------------------------
# Frontmatter reader (no yaml dep needed for the simple case)
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _slug_from_url(url: str) -> str:
    if not url:
        return ""
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return path.split("/")[-1] if path else ""


LOCALE_DIR_NAMES = {"en", "de", "es", "it", "uk"}


def _find_mdx_for_url(site_slug: str, page_url: str) -> Optional[Path]:
    """Locate the MDX file backing a URL across both content-dir layouts.

    Site conventions vary:
      - matelas / bureau / cafe:        content/ (default) + content-<loc>/
      - aspirateur (mixed):             content/ + content-en/ + content/es/
      - pixinstant:                     content/ (default) + content/<loc>/

    Algorithm:
      1. Detect locale from URL.
      2. Default locale → search content/**/<slug>.mdx EXCLUDING locale-named
         subdirs (so /test/ on a FR site doesn't pick up content/en/...).
      3. Non-default locale → search content-<loc>/** and content/<loc>/**.
      4. Tiebreaker: prefer a match whose directory name matches the URL's
         content-type segment ('test', 'avis', 'comparatif', 'guide'). The
         URL segment can diverge from the on-disk dir (e.g. /en/avis/<slug>
         lives in content-en/tests/), so this is a soft preference only.
    """
    slug = _slug_from_url(page_url)
    if not slug:
        return None
    site = SITES.get(site_slug)
    if not site:
        return None
    repo = BASE_DIR / site_slug
    site_default = site.get("default_locale", "fr")
    locale = _detect_locale_from_url(page_url, site_default=site_default)

    candidates: list[Path] = []
    if locale == site_default:
        # Default-locale: content/**/<slug>.mdx but skip content/<locale>/ subtrees.
        for mdx in repo.glob(f"content/**/{slug}.mdx"):
            try:
                rel = mdx.relative_to(repo / "content").parts
            except ValueError:
                continue
            if rel and rel[0] in LOCALE_DIR_NAMES:
                continue  # nested locale subdir on a default-locale URL → skip
            candidates.append(mdx)
    else:
        # Parallel layout: content-<loc>/
        candidates.extend(repo.glob(f"content-{locale}/**/{slug}.mdx"))
        # Nested layout: content/<loc>/
        candidates.extend(repo.glob(f"content/{locale}/**/{slug}.mdx"))

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Soft tiebreaker: URL content-type segment match.
    path_segments = [p for p in page_url.lower().split("/") if p]
    url_type = next(
        (s for s in path_segments if s in {"test", "avis", "comparatif", "comparativo", "guide", "blog"}),
        None,
    )
    if url_type:
        # Match on directory name (allow plural: 'tests/', 'comparatifs/').
        for c in candidates:
            parts = [p.lower() for p in c.parts]
            if any(p == url_type or p.rstrip("s") == url_type for p in parts):
                return c
    return candidates[0]


def _read_meta(mdx_path: Path) -> dict[str, str]:
    try:
        text = mdx_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm: dict[str, str] = {}
    for line in m.group(1).split("\n"):
        if ":" not in line or line.startswith(("  ", "\t", "-")):
            continue
        key, val = line.split(":", 1)
        fm[key.strip()] = val.strip().strip("\"'")
    return fm


# ---------------------------------------------------------------------------
# Guardrails (added 2026-05-11 after EN-template regressions on FR pages)
# ---------------------------------------------------------------------------

# Words that should NEVER appear in non-EN titles. If a variant for a FR/DE/ES/IT
# page contains any of these, reject it — the LLM (or rule generator) drifted.
EN_ONLY_TOKENS = (
    "best", "tested", "ranked", "compared", "worth it", "honest", "ultimate",
    "buyer's", "buyers guide", "expert", "proven", "definitive",
)

# Content types where a listicle-style title ("7 Best …", "Top 5 …") is wrong:
# single-product test/avis pages and how-to guides aren't lists.
# `avis` added 2026-05-11 after live run produced "7 Best Tediber for 2026" on
# the Tediber EN avis page despite type-from-URL stamping `avis`.
NON_LISTICLE_CONTENT_TYPES = {"test", "guide", "blog", "avis"}

# Variant strategies that produce listicle-shape titles.
LISTICLE_STRATEGIES = {"number_year"}


def _validate_variant_for_locale(title: str, locale: str) -> tuple[bool, str]:
    """Return (ok, reason). False means reject this variant."""
    if not title:
        return False, "empty"
    if locale != "en":
        low = title.lower()
        for tok in EN_ONLY_TOKENS:
            if tok in low:
                return False, f"english_word_on_{locale}_page:{tok}"
    return True, ""


def _detect_content_type(mdx_path: Optional[Path], page_url: str) -> str:
    """Read `type:` from frontmatter; fall back to URL segment inference."""
    if mdx_path:
        meta = _read_meta(mdx_path)
        t = (meta.get("type") or "").strip().lower()
        if t:
            return t
    parts = [p for p in page_url.lower().split("/") if p]
    for p in parts:
        if p in {"test", "guide", "comparatif", "blog", "avis"}:
            return p
    return ""


# ---------------------------------------------------------------------------
# Variant generators (rule-based; LLM optional)
# ---------------------------------------------------------------------------

def variant_number_year(keyword: str, locale: str = "fr") -> str:
    n = 7
    if locale == "en":
        return f"{n} Best {keyword.title()} for {CURRENT_YEAR} (Tested & Compared)"
    if locale == "de":
        return f"Die {n} besten {keyword} {CURRENT_YEAR} im Vergleich"
    if locale == "es":
        return f"Los {n} mejores {keyword} {CURRENT_YEAR} (probados y comparados)"
    if locale == "it":
        return f"I {n} migliori {keyword} {CURRENT_YEAR} a confronto"
    return f"{n} meilleurs {keyword} {CURRENT_YEAR} (testés & comparés)"


def variant_question(keyword: str, locale: str = "fr") -> str:
    if locale == "en":
        return f"Which {keyword} Is Worth It in {CURRENT_YEAR}? Honest Review"
    if locale == "de":
        return f"Welcher {keyword} lohnt sich {CURRENT_YEAR}? Ehrlicher Test"
    if locale == "es":
        return f"¿Cuál es el mejor {keyword} en {CURRENT_YEAR}? Análisis honesto"
    if locale == "it":
        return f"Qual è il miglior {keyword} nel {CURRENT_YEAR}? Recensione onesta"
    return f"Quel {keyword} choisir en {CURRENT_YEAR} ? Avis honnête"


def variant_power_word(keyword: str, locale: str = "fr") -> str:
    if locale == "en":
        return f"Ultimate {keyword.title()} Buyer's Guide ({CURRENT_YEAR}, Tested)"
    if locale == "de":
        return f"Der ultimative {keyword}-Kaufratgeber {CURRENT_YEAR}"
    if locale == "es":
        return f"Guía definitiva de {keyword} {CURRENT_YEAR} (probado)"
    if locale == "it":
        return f"La guida definitiva al {keyword} {CURRENT_YEAR} (testato)"
    return f"Guide d'achat ultime {keyword} {CURRENT_YEAR} (testé)"


def llm_variant(current_title: str, page_path: str, locale: str = "fr") -> Optional[str]:
    """Generate one variant via Anthropic. Skipped silently when key missing."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import urllib.request
    except Exception:
        return None
    prompt = (
        f"Rewrite this affiliation-site SEO title to maximise CTR while staying"
        f" truthful and ≤60 characters. Reply with only the rewritten title.\n"
        f"Current title: {current_title}\nURL: {page_path}\nLocale: {locale}\n"
        f"Year: {CURRENT_YEAR}"
    )
    body = json.dumps({
        "model": os.environ.get("CTR_LLM_MODEL", "claude-opus-4-5"),
        "max_tokens": 100,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        blocks = data.get("content", [])
        if blocks and isinstance(blocks, list):
            text = blocks[0].get("text", "").strip().strip('"\'')
            return text if text else None
    except Exception as exc:
        print(f"  ⚠️  LLM variant failed: {exc}")
    return None


def score_variant(variant: str, locale: str = "fr") -> float:
    """0–10 heuristic score: length sweet spot, year, number, power word."""
    if not variant:
        return 0.0
    score = 5.0
    n = len(variant)
    if 50 <= n <= 60:
        score += 1.5
    elif 45 <= n <= 65:
        score += 0.5
    elif n > 75 or n < 30:
        score -= 1.5
    if str(CURRENT_YEAR) in variant or str(CURRENT_YEAR + 1) in variant:
        score += 1.0
    if re.search(r"\b\d{1,2}\b", variant):
        score += 1.0
    pw = POWER_WORDS_EN if locale == "en" else POWER_WORDS_FR
    if any(w in variant.lower() for w in pw):
        score += 1.0
    if "?" in variant or "(" in variant:
        score += 0.5
    return round(min(10.0, max(0.0, score)), 2)


_FILLER_TOKENS = {
    "meilleur", "meilleure", "meilleurs", "meilleures",
    "best", "top", "the", "le", "la", "les", "un", "une", "vs",
    "test", "comparatif", "guide", "avis",
    str(CURRENT_YEAR), str(CURRENT_YEAR - 1), str(CURRENT_YEAR + 1),
}


def keyword_from_slug(slug: str) -> str:
    if not slug:
        return ""
    parts = [p for p in slug.split("-") if p and p.lower() not in _FILLER_TOKENS]
    return " ".join(parts[:5]).strip() or slug.replace("-", " ")


def propose_variants(
    current_title: str,
    slug: str,
    page_path: str,
    locale: str = "fr",
    content_type: str = "",
) -> list[dict[str, Any]]:
    keyword = keyword_from_slug(slug)
    variants = [
        {"name": "number_year", "title": variant_number_year(keyword, locale)},
        {"name": "question",    "title": variant_question(keyword, locale)},
        {"name": "power_word",  "title": variant_power_word(keyword, locale)},
    ]
    llm_v = llm_variant(current_title, page_path, locale)
    if llm_v:
        variants.append({"name": "llm", "title": llm_v})

    # Guardrail 1: locale validity — drop variants with English-only tokens
    # on non-EN pages (LLM is the most common offender here).
    filtered: list[dict[str, Any]] = []
    for v in variants:
        ok, reason = _validate_variant_for_locale(v["title"], locale)
        if not ok:
            v["rejected"] = reason
            continue
        filtered.append(v)

    # Guardrail 2: content-type — listicle templates ("7 Best X") on a
    # single-product test ("Emma Original Avis") read as nonsense to users
    # and tank CTR further. Drop them.
    if content_type in NON_LISTICLE_CONTENT_TYPES:
        filtered = [v for v in filtered if v["name"] not in LISTICLE_STRATEGIES]

    for v in filtered:
        v["score"] = score_variant(v["title"], locale)
        v["length"] = len(v["title"])
    filtered.sort(key=lambda v: -v["score"])
    return filtered


# ---------------------------------------------------------------------------
# Hermes events
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
# Site driver
# ---------------------------------------------------------------------------

def _detect_locale_from_url(page_url: str, site_default: str = "fr") -> str:
    """Walk the URL path looking for an explicit /xx/ locale prefix.

    Important: only match locale prefixes that appear as path SEGMENTS
    (between slashes) at the very start, never substrings inside slugs.
    Otherwise URLs containing 'en' (e.g. /comparatif/aspirateur-balai-puissant)
    get mis-classified as English."""
    if not page_url:
        return site_default
    try:
        from urllib.parse import urlparse
        path = urlparse(page_url).path
    except Exception:
        path = page_url
    parts = [p for p in path.lower().split("/") if p]
    if parts and parts[0] in {"en", "de", "es", "it", "uk"}:
        return parts[0]
    return site_default


def run_site(
    site_slug: str,
    min_impressions: int,
    max_proposals: int,
    dry_run: bool,
    apply: bool = False,
) -> dict[str, Any]:
    candidates = find_low_ctr_pages(site_slug, min_impressions=min_impressions, top_n=max_proposals * 2)
    print(f"  → {site_slug}: {len(candidates)} low-CTR candidate(s)")
    proposals: list[dict[str, Any]] = []
    skipped_no_mdx = 0
    for c in candidates[:max_proposals]:
        mdx = _find_mdx_for_url(site_slug, c["page_path"])
        if mdx is None:
            # No MDX backs this URL — likely a homepage (`/en/`), category index,
            # or other framework-rendered page. The slug-as-title fallback used
            # to emit nonsense proposals like `current_title="en"` → variant
            # `"7 Best En for 2026 (Tested & Compared)"`. Skip cleanly.
            skipped_no_mdx += 1
            continue
        meta = _read_meta(mdx)
        current_title = meta.get("title", "")
        if not current_title:
            # MDX exists but title couldn't be parsed (rare malformed frontmatter)
            # — still skip rather than fall back to slug-as-title, since the slug
            # rarely makes a good comparison baseline for variant scoring.
            skipped_no_mdx += 1
            continue
        slug = _slug_from_url(c["page_path"])
        site_default = SITES.get(site_slug, {}).get("default_locale", "fr")
        locale = _detect_locale_from_url(c["page_path"], site_default=site_default)
        content_type = _detect_content_type(mdx, c["page_path"])
        variants = propose_variants(
            current_title, slug, c["page_path"],
            locale=locale, content_type=content_type,
        )
        current_score = score_variant(current_title, locale)
        winner = next((v for v in variants if v["score"] > current_score + 0.5), None)
        proposal = {
            "site": site_slug,
            "page_path": c["page_path"],
            "slug": slug,
            "locale": locale,
            "content_type": content_type,
            "mdx_path": str(mdx) if mdx else None,
            "current_title": current_title,
            "current_title_score": current_score,
            "current_ctr": c["actual_ctr"],
            "expected_ctr": c["expected_ctr"],
            "position": c["position"],
            "impressions": c["impressions"],
            "opportunity_clicks": c["opportunity_clicks"],
            "variants": variants,
            "recommended": winner,
        }
        proposals.append(proposal)
        # Guardrail 3: only emit Hermes events when --apply is explicitly set.
        # Otherwise proposals stay in the queue file for human review.
        if winner and apply and not dry_run:
            emit_event(
                "cro.meta_variant_proposed",
                {
                    "site": site_slug,
                    "page_path": c["page_path"],
                    "mdx_path": str(mdx) if mdx else None,
                    "current_title": current_title,
                    "proposed_title": winner["title"],
                    "variant_strategy": winner["name"],
                    "score_uplift": round(winner["score"] - current_score, 2),
                    "impressions": c["impressions"],
                    "current_ctr": c["actual_ctr"],
                    "expected_ctr": c["expected_ctr"],
                    "opportunity_clicks": c["opportunity_clicks"],
                    "all_variants": variants,
                },
                priority=3,
                target_agent="agent-cro-optimizer",
            )
    if skipped_no_mdx:
        print(f"     ({skipped_no_mdx} candidate(s) skipped — no MDX backing the URL)")
    return {
        "site": site_slug,
        "candidates": len(candidates),
        "proposals": proposals,
        "skipped_no_mdx": skipped_no_mdx,
    }


def write_report(per_site: dict[str, dict[str, Any]], dry_run: bool, apply: bool) -> Path:
    today = date.today().isoformat()
    out = REPORTS_DIR / f"ctr-opportunities-{today}.md"
    if dry_run:
        mode_note = "DRY-RUN — proposals scored for review; no Hermes proposal events emitted."
    elif apply:
        mode_note = "APPLY MODE — reviewed winners emitted as `cro.meta_variant_proposed` events."
    else:
        mode_note = "QUEUE-ONLY — proposals saved for human review; no Hermes proposal events emitted."
    lines = [
        f"# CTR optimization opportunities — {today}",
        "",
        f"_Generated by `{AGENT_NAME}`. {mode_note}_",
        "",
    ]
    for site, data in per_site.items():
        lines.append(f"## {site} — {len(data.get('proposals', []))} proposals")
        lines.append("")
        for p in data.get("proposals", []):
            lines.append(f"### `{p['slug']}` [{p['locale']}] (position {p['position']}, {p['impressions']:,} impressions)")
            lines.append(f"- URL: `{p['page_path']}`")
            lines.append(f"- Current CTR: **{p['current_ctr']*100:.2f}%** vs expected **{p['expected_ctr']*100:.2f}%** "
                         f"(opportunity: **+{p['opportunity_clicks']} clicks/period**)")
            lines.append(f"- Current title: `{p['current_title']}` (score {p['current_title_score']})")
            lines.append("- Variants:")
            for v in p["variants"]:
                marker = " ← **RECOMMENDED**" if p.get("recommended") and v["title"] == p["recommended"]["title"] else ""
                lines.append(f"  - [{v['name']}, score {v['score']}, {v['length']} chars]{marker} `{v['title']}`")
            lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✅ Report saved: {out}")
    return out


def _write_proposal_queue(per_site: dict[str, dict[str, Any]], apply: bool) -> Path:
    """Persist proposals to a queue file for human review (Phase 1 guardrail)."""
    queue_dir = REPORTS_DIR / "agent_queues" / "ctr_proposed"
    queue_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    out = queue_dir / f"{today}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "agent": AGENT_NAME,
        "mode": "apply" if apply else "queue_only",
        "review_required": not apply,
        "sites": {s: data.get("proposals", []) for s, data in per_site.items()},
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  📥 Proposals queued for review: {out}")
    return out


def run_daily(
    sites: Optional[list[str]] = None,
    min_impressions: int = 200,
    max_proposals: int = 10,
    dry_run: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    sites = sites or list(SITES.keys())
    per_site: dict[str, dict[str, Any]] = {}
    total_proposals = 0
    for s in sites:
        result = run_site(s, min_impressions, max_proposals, dry_run, apply=apply)
        per_site[s] = result
        total_proposals += len(result.get("proposals", []))
    out_path = write_report(per_site, dry_run, apply)
    queue_path = _write_proposal_queue(per_site, apply)
    if not dry_run:
        emit_event(
            "ctr_optimizer.run_completed",
            {
                "report_path": str(out_path),
                "queue_path": str(queue_path),
                "sites": sites,
                "proposals": total_proposals,
                "applied": apply,
            },
            priority=4,
            target_agent="agent-analytics",
        )
    return {"per_site": per_site, "report_path": str(out_path), "queue_path": str(queue_path)}


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------

CONSUMED_TYPES = {"ctr_optimizer.run_requested", "analytics.weekly_report"}


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
        try:
            run_daily(dry_run=dry_run)
            complete_claimed_event(event, dry_run=dry_run)
        except Exception as exc:
            print(f"  ❌ {AGENT_NAME} failed on {path.name}: {exc}")
            fail_claimed_event(event, dry_run=dry_run)
        handled += 1
    print(f"[SUMMARY] {AGENT_NAME} handled {handled} event(s).")
    return handled


def main() -> int:
    parser = argparse.ArgumentParser(description=AGENT_NAME)
    parser.add_argument("--consume", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--site", type=str)
    parser.add_argument("--min-impressions", type=int, default=200)
    parser.add_argument("--max", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Emit cro.meta_variant_proposed events. Default: queue-only (human-in-loop).",
    )
    args = parser.parse_args()
    if args.consume:
        consume(limit=args.limit, dry_run=args.dry_run)
        return 0
    sites = [args.site] if args.site else None
    if args.daily or args.site:
        run_daily(
            sites=sites,
            min_impressions=args.min_impressions,
            max_proposals=args.max,
            dry_run=args.dry_run,
            apply=args.apply,
        )
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
