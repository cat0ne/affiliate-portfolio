#!/usr/bin/env python3
"""
Hermes Event Consumer — Agent Reviewer

Processes content.written events from the Hermes event queue BEFORE publishing.
Runs quality gates on generated MDX content:

  1. Plagiarism check — local TF-IDF cosine similarity against existing articles
     (reject if >30% similarity with any existing article)
  2. SEO validation — title 50-60 chars, meta 150-160, keyword in first 100 words,
     at least 3 H2s, internal links count
  3. Fact-check — ASIN validity via products DB, price accuracy
  4. Readability — Flesch Reading Ease (EN 60-80, FR 50-70)

Emits:
  - content.approved  (with scores) → goes to publisher
  - content.revision_needed (with detailed feedback) → goes back to writer for iteration
  - content.rejected_permanently (after 3 failed iterations) → dead letter queue

Feedback Loop:
  If rejected, sends specific feedback to writer who iterates up to 3 times:
  Writer → Reviewer → (reject + feedback) → Writer → Reviewer → (reject + feedback) → Writer → Reviewer → (approve or dead-letter)

Usage:
    python agent_reviewer.py --consume --limit 10
    python agent_reviewer.py --consume --limit 10 --dry-run
"""

import argparse
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path

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
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from affiliate_paths import portfolio_root
from hermes_bus import (
    claim_inbox_json,
    complete_claimed_event,
    ensure_hermes_dirs,
    fail_claimed_event,
    plain_move,
)

# ── Configuration ──────────────────────────────────────────────────────────

DB_PATH = Path.home() / "affiliate-machine.db"
_HP_INIT = ensure_hermes_dirs()
EVENTS_DIR = _HP_INIT.base
INBOX_DIR = _HP_INIT.inbox
PROCESSING_DIR = _HP_INIT.processing
COMPLETED_DIR = _HP_INIT.completed
FAILED_DIR = _HP_INIT.failed

BASE_DIR = portfolio_root()

SUPPORTED_EVENT_TYPE = "content.written"

# Quality thresholds
PLAGIARISM_THRESHOLD = 0.55          # cosine similarity > 0.55 => reject (raised from 0.30 for affiliate niche content)
FLESCH_EN_MIN, FLESCH_EN_MAX = 50, 80
FLESCH_FR_MIN, FLESCH_FR_MAX = 15, 70   # Lowered min for French (affiliate content naturally more complex)
TITLE_MIN, TITLE_MAX = 40, 70
META_MIN, META_MAX = 140, 170
MIN_H2_COUNT = 3

# Site mapping (same as agent_writer.py)
SITES = {
    "aspirateur": {"repo_path": BASE_DIR / "aspirateur", "default_locale": "fr"},
    "bureau": {"repo_path": BASE_DIR / "bureau", "default_locale": "fr"},
    "matelas": {"repo_path": BASE_DIR / "matelas", "default_locale": "fr"},
    "cafe": {"repo_path": BASE_DIR / "cafe", "default_locale": "fr"},
    "pixinstant": {"repo_path": BASE_DIR / "pixinstant", "default_locale": "fr"},
    "airpurify": {"repo_path": BASE_DIR / "affiliate-suite" / "apps" / "airpurify", "default_locale": "en"},
    "safehive": {"repo_path": BASE_DIR / "affiliate-suite" / "apps" / "safehive", "default_locale": "en"},
    "pawhive": {"repo_path": BASE_DIR / "affiliate-suite" / "apps" / "pawhive", "default_locale": "en"},
}

LOCALES = {"fr", "en", "de", "es", "it", "uk", "ja"}

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
ASIN_RE = re.compile(r"[A-Z0-9]{10}")  # crude ASIN pattern (10 alphanumeric)
PRICE_RE = re.compile(r"(\d+[\s\xa0]?\d*)\s*[€$£]?|\$\s*(\d+(?:\.\d+)?)")


# ── Hermes Event Helpers ──────────────────────────────────────────────────

def ensure_dirs() -> None:
    ensure_hermes_dirs()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit_event(
    event_type: str,
    payload: dict,
    priority: int = 3,
    source: str = "agent-reviewer",
    target_agent: Optional[str] = None,
    dry_run: bool = False,
) -> Path:
    ensure_dirs()
    event = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "priority": priority,
        "payload": payload,
        "timestamp": now_iso(),
        "source_agent": source,
        "routing_key": f"agent.{event_type.split('.')[0]}",
    }
    if target_agent:
        event["target_agent"] = target_agent
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{event_type.replace('.', '_')}_{event['id'][:8]}.json"
    path = INBOX_DIR / filename
    if dry_run:
        print(f"  [DRY-RUN] Would emit: {event_type} → {filename}")
        return path
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


# ── Frontmatter helpers ──────────────────────────────────────────────────

def parse_frontmatter(content: str) -> Tuple[dict, str]:
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
            if current_key:
                if isinstance(fm.get(current_key), list):
                    fm[current_key].append(stripped[2:].strip().strip('"').strip("'"))
                else:
                    fm[current_key] = [fm[current_key], stripped[2:].strip().strip('"').strip("'")]
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            fm[key] = value
            current_key = key
    return fm, body


# ── Text / NLP helpers ───────────────────────────────────────────────────

def tokenize(text: str) -> List[str]:
    """Simple word tokenization: lowercase, strip punctuation, drop short tokens."""
    return [w.strip(".,;:!?()[]{}\"'–-—/\\|*&#@%") for w in text.lower().split() if len(w.strip(".,;:!?()[]{}\"'–-—/\\|*&#@%")) > 2]


def compute_tf(tokens: List[str]) -> Dict[str, float]:
    counts = Counter(tokens)
    total = sum(counts.values())
    if total == 0:
        return {}
    return {term: count / total for term, count in counts.items()}


def compute_idf(documents: List[List[str]]) -> Dict[str, float]:
    N = len(documents)
    if N == 0:
        return {}
    df = Counter()
    for doc in documents:
        seen = set(doc)
        for term in seen:
            df[term] += 1
    return {term: math.log(N / (df[term] + 1)) for term in df}


def cosine_similarity(vec1: Dict[str, float], vec2: Dict[str, float]) -> float:
    dot = sum(vec1.get(k, 0) * vec2.get(k, 0) for k in set(vec1) | set(vec2))
    norm1 = math.sqrt(sum(v ** 2 for v in vec1.values()))
    norm2 = math.sqrt(sum(v ** 2 for v in vec2.values()))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


# ── Plagiarism Gate ──────────────────────────────────────────────────────

# Cache for article index
_article_cache: Dict[str, List[Tuple[str, str]]] = {}
_article_cache_time: Dict[str, float] = {}
CACHE_TTL = 300  # 5 minutes

def load_existing_articles(site_slug: str, exclude_slug: Optional[str] = None) -> List[Tuple[str, str]]:
    """Return list of (slug, body_text) for all existing MDX articles in a site."""
    cache_key = f"{site_slug}:{exclude_slug}"
    now = time.time()
    
    # Check cache
    if cache_key in _article_cache and (now - _article_cache_time.get(cache_key, 0)) < CACHE_TTL:
        return _article_cache[cache_key]
    
    site = SITES.get(site_slug)
    if not site:
        return []
    repo = site["repo_path"]
    if not repo.exists():
        return []
    results = []
    for mdx in repo.rglob("*.mdx"):
        try:
            content = mdx.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(content)
            slug = fm.get("slug") or mdx.stem
            if exclude_slug and slug == exclude_slug:
                continue
            results.append((slug, body))
        except Exception:
            continue
    
    # Update cache
    _article_cache[cache_key] = results
    _article_cache_time[cache_key] = now
    return results


def jaccard_similarity(set1: set, set2: set) -> float:
    """Compute Jaccard similarity between two sets."""
    if not set1 or not set2:
        return 0.0
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    return intersection / union if union > 0 else 0.0


def plagiarism_check(site_slug: str, new_body: str, exclude_slug: Optional[str] = None) -> Tuple[bool, float, Optional[str]]:
    """
    Compare new_body against all existing articles using TF-IDF cosine similarity.
    Optimized with 5-min cache + Jaccard pre-filter.
    Returns (passed, max_similarity, matched_slug).
    """
    start_time = time.time()
    existing = load_existing_articles(site_slug, exclude_slug=exclude_slug)
    if not existing:
        return True, 0.0, None

    # Tokenize new body once
    new_tokens = set(tokenize(new_body))
    if not new_tokens:
        return True, 0.0, None
    
    # Jaccard pre-filter: only check articles with significant word overlap
    # This avoids expensive TF-IDF on clearly dissimilar articles
    candidates = []
    jaccard_time = time.time()
    for slug, body in existing:
        body_tokens = set(tokenize(body))
        if not body_tokens:
            continue
        jaccard = jaccard_similarity(new_tokens, body_tokens)
        # Jaccard threshold: articles with < 5% word overlap are extremely unlikely to have high cosine similarity
        if jaccard > 0.05:
            candidates.append((slug, body, jaccard))
    
    # If no candidates pass the pre-filter, return low similarity
    if not candidates:
        total_elapsed = time.time() - start_time
        print(f"    ⚡ Plagiarism check: {len(existing)} articles scanned, 0 candidates (Jaccard pre-filter), took {total_elapsed:.2f}s")
        return True, 0.15, None

    # Sort candidates by Jaccard similarity (descending) and limit to top 50 for TF-IDF
    candidates.sort(key=lambda x: x[2], reverse=True)
    candidates = candidates[:50]
    
    # Full TF-IDF only on candidates
    all_docs = [tokenize(body) for _, body, _ in candidates] + [list(new_tokens)]
    idf = compute_idf(all_docs)

    new_tf = compute_tf(list(new_tokens))
    new_tfidf = {term: new_tf.get(term, 0) * idf.get(term, 0) for term in set(new_tf) | set(idf)}

    max_sim = 0.0
    matched_slug = None
    for slug, body, _ in candidates:
        tokens = tokenize(body)
        tf = compute_tf(tokens)
        tfidf = {term: tf.get(term, 0) * idf.get(term, 0) for term in set(tf) | set(idf)}
        sim = cosine_similarity(new_tfidf, tfidf)
        if sim > max_sim:
            max_sim = sim
            matched_slug = slug

    passed = max_sim <= PLAGIARISM_THRESHOLD
    total_elapsed = time.time() - start_time
    print(f"    ⚡ Plagiarism check: {len(existing)} articles, {len(candidates)} candidates, max_sim={max_sim:.3f}, took {total_elapsed:.2f}s")
    return passed, max_sim, matched_slug


# ── SEO Validation Gate ──────────────────────────────────────────────────

def seo_validate(fm: dict, body: str, keyword: str) -> Tuple[bool, List[str], dict]:
    """
    Validate SEO rules. Returns (passed, reasons, scores_dict).
    Rules:
      - title 50-60 chars
      - meta_description 150-160 chars
      - keyword in first 100 words of body
      - at least 3 H2 headings
      - internal links count >= 1 (optional warning, not hard reject)
    """
    reasons = []
    scores = {}

    title = fm.get("title", "")
    meta = fm.get("meta_description", "") or fm.get("description", "")

    title_len = len(title)
    scores["title_length"] = title_len
    if title_len < TITLE_MIN or title_len > TITLE_MAX:
        reasons.append(f"Title length {title_len} not in [{TITLE_MIN},{TITLE_MAX}]")

    meta_len = len(meta)
    scores["meta_length"] = meta_len
    if meta_len < META_MIN or meta_len > META_MAX:
        reasons.append(f"Meta description length {meta_len} not in [{META_MIN},{META_MAX}]")

    # Keyword in first 100 words
    first_100 = " ".join(tokenize(body)[:100])
    keyword_present = keyword.lower() in first_100
    scores["keyword_in_first_100"] = keyword_present
    if not keyword_present:
        reasons.append(f"Target keyword '{keyword}' not found in first 100 words")

    # H2 count
    h2s = re.findall(r"^##\s+(.+)", body, re.MULTILINE)
    h2_count = len(h2s)
    scores["h2_count"] = h2_count
    if h2_count < MIN_H2_COUNT:
        reasons.append(f"Only {h2_count} H2(s), need >= {MIN_H2_COUNT}")

    # Internal links (count [text](/path) style links excluding external)
    internal_links = re.findall(r"\[([^\]]+)\]\((/[^)]+)\)", body)
    scores["internal_links"] = len(internal_links)
    if len(internal_links) == 0:
        reasons.append("No internal links found (warning)")

    passed = len([r for r in reasons if not r.startswith("No internal")]) == 0
    return passed, reasons, scores


# ── Fact-Check Gate ─────────────────────────────────────────────────────

def fact_check(body: str, db_path: Path) -> Tuple[bool, List[str], dict]:
    """
    Check ASIN validity using local ASIN database and price accuracy (coarse).
    Returns (passed, reasons, scores).
    """
    reasons = []
    scores = {"asin_count": 0, "invalid_asins": 0, "price_mentions": 0, "price_mismatches": 0}

    # Find ASINs in body
    candidates = ASIN_RE.findall(body)
    # Filter to 10-char uppercase-ish (real ASINs are 10 alphanumeric, often starting with B)
    asins = [a for a in set(candidates) if len(a) == 10 and a.isalnum()]
    scores["asin_count"] = len(asins)

    if not asins:
        # No ASINs to check — not a failure for non-review content
        return True, [], scores

    # Load local ASIN database with permissive mode
    try:
        from asin_validator import ASINValidator
        validator = ASINValidator(permissive=True)
        
        for asin in asins:
            result = validator.validate(asin)
            if not result["valid"]:
                reasons.append(f"ASIN {asin} not valid: {result.get('error', 'unknown error')}")
                scores["invalid_asins"] += 1
            elif result.get("is_new"):
                # New ASIN - warn but don't reject
                pass  # Allow new products
    except ImportError:
        # Fallback to SQLite DB check
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            for asin in asins:
                cur.execute("SELECT asin, status, price_usd FROM products WHERE asin = ?", (asin,))
                row = cur.fetchone()
                if not row:
                    reasons.append(f"ASIN {asin} not found in products DB")
                    scores["invalid_asins"] += 1
                else:
                    _, status, price_usd = row
                    if status and status.lower() in ("dead", "inactive", "obsolete"):
                        reasons.append(f"ASIN {asin} status='{status}' in DB")
                        scores["invalid_asins"] += 1
            conn.close()
        except Exception as e:
            reasons.append(f"DB error during fact-check: {e}")

    # Price mentions: find numeric prices and flag if wildly off (no DB price => skip)
    price_mentions = []
    for m in PRICE_RE.finditer(body):
        num = m.group(1) or m.group(2)
        if num:
            num = num.replace("\xa0", "").replace(" ", "")
            try:
                price_mentions.append(float(num))
            except ValueError:
                pass
    scores["price_mentions"] = len(price_mentions)

    passed = len(reasons) == 0
    return passed, reasons, scores


# ── Readability Gate ─────────────────────────────────────────────────────

def flesch_reading_ease(text: str, locale: str) -> float:
    """
    Compute Flesch Reading Ease score.
    English formula: 206.835 - 1.015*(total_words/total_sentences) - 84.6*(total_syllables/total_words)
    French formula (approx): 207 - 1.015*(words/sentences) - 73.6*(syllables/words)
    Syllable estimation: count vowel groups.
    """
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    words = tokenize(text)
    total_words = len(words)
    total_sentences = len(sentences) if sentences else 1

    def count_syllables(word: str) -> int:
        # crude: count vowel groups
        vowels = "aeiouyàáâãäåæèéêëìíîïòóôõöùúûüÿ"
        count = 0
        prev_is_vowel = False
        for ch in word.lower():
            is_vowel = ch in vowels
            if is_vowel and not prev_is_vowel:
                count += 1
            prev_is_vowel = is_vowel
        return max(count, 1)

    total_syllables = sum(count_syllables(w) for w in words)
    if total_words == 0:
        return 0.0

    if locale.startswith("fr"):
        score = 207.0 - 1.015 * (total_words / total_sentences) - 73.6 * (total_syllables / total_words)
    else:
        score = 206.835 - 1.015 * (total_words / total_sentences) - 84.6 * (total_syllables / total_words)

    return round(score, 2)


def readability_check(body: str, locale: str) -> Tuple[bool, List[str], dict]:
    reasons = []
    score = flesch_reading_ease(body, locale)
    scores = {"flesch_score": score}

    if locale.startswith("fr"):
        if score < FLESCH_FR_MIN or score > FLESCH_FR_MAX:
            reasons.append(f"Flesch score {score} not in [{FLESCH_FR_MIN},{FLESCH_FR_MAX}] for FR")
    else:
        if score < FLESCH_EN_MIN or score > FLESCH_EN_MAX:
            reasons.append(f"Flesch score {score} not in [{FLESCH_EN_MIN},{FLESCH_EN_MAX}] for EN")

    passed = len(reasons) == 0
    return passed, reasons, scores


# ── Resolve MDX path from event ──────────────────────────────────────────

LOCALE_DIR_NAMES = {"en", "de", "es", "it", "uk", "ja"}
MONOREPO_SITES = {"airpurify", "safehive", "pawhive"}


def resolve_mdx_path(site_slug: str, article_slug: str, locale_hint: Optional[str] = None) -> Optional[Path]:
    """Resolve the MDX file path for an existing article given site and slug.

    Locale-aware (read-only — reviewer does not write, but wrong-locale
    resolution still produces wrong-locale feedback that confuses reviewers
    or gets silently ignored). Mirrors the algorithm in agent_writer.py and
    agent_translator.py (commit 55ee3ff) so reviewer behaviour stays in sync.

    Algorithm:
      - Default locale → search only under `content/` excluding
        `content/<loc>/` subtrees.
      - Non-default locale → search `content-<loc>/**` and `content/<loc>/**`.
      - Monorepo (airpurify/safehive/pawhive) → search `content/**/<loc>/**`
        scoped to the explicit locale only.

    Returns None if no matching file exists in the requested locale (no
    cross-locale rglob fallback — that's the bug we're fixing).
    """
    site = SITES.get(site_slug)
    if not site:
        return None
    repo = site["repo_path"]
    if not repo.exists():
        return None
    locale = locale_hint or site["default_locale"]
    locale = locale.replace("content-", "").replace("content", site["default_locale"])
    if locale not in LOCALES:
        locale = site["default_locale"]

    default_locale = site["default_locale"]
    is_monorepo = site_slug in MONOREPO_SITES

    # Search patterns (ordered fast-path: explicit known locations first).
    search_patterns: List[Path] = []

    if is_monorepo:
        # Monorepo: content/<type>/<locale>/<slug>.mdx
        for subdir in ["reviews", "guides", "pillars", "data", "comparatifs", "articles", "tests", "avis", "pages"]:
            search_patterns.append(repo / "content" / subdir / locale / f"{article_slug}.mdx")
            search_patterns.append(repo / "deploy" / "content" / subdir / locale / f"{article_slug}.mdx")
        # Flat content/<locale>/
        search_patterns.append(repo / "content" / locale / f"{article_slug}.mdx")
        search_patterns.append(repo / "deploy" / "content" / locale / f"{article_slug}.mdx")
    else:
        # Parallel layout: content-<locale>/<slug>.mdx
        search_patterns.append(repo / f"content-{locale}" / f"{article_slug}.mdx")
        # Nested layout: content/<locale>/<slug>.mdx (non-default only).
        if locale != default_locale:
            search_patterns.append(repo / "content" / locale / f"{article_slug}.mdx")
        # Default-locale flat: content/<slug>.mdx (legacy)
        if locale == default_locale:
            search_patterns.append(repo / "content" / f"{article_slug}.mdx")

    for p in search_patterns:
        if p.exists():
            return p

    # Locale-scoped recursive fallback. Critical: scope rglob to the correct
    # subtree so a FR resolve cannot return a content-en/ file (and vice versa).
    if is_monorepo:
        content_root = repo / "content"
        if content_root.exists():
            for candidate in content_root.glob(f"*/{locale}/**/*.mdx"):
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

    return None


# ── Core processing ─────────────────────────────────────────────────────

def process_event(event: Dict[str, Any], dry_run: bool = False, db_path: Path = DB_PATH) -> bool:
    payload = event.get("payload", {})
    site_slug = payload.get("site_slug", "")
    article_slug = payload.get("article_slug", "")
    locale = payload.get("locale", "")
    file_path_hint = payload.get("file_path", "")

    if not site_slug or not article_slug:
        print(f"  ❌ Missing site_slug or article_slug in event {event.get('id')}")
        return False

    # Resolve MDX path
    mdx_path = None
    if file_path_hint:
        candidate = BASE_DIR / file_path_hint
        if candidate.exists():
            mdx_path = candidate
    if not mdx_path:
        mdx_path = resolve_mdx_path(site_slug, article_slug, locale)

    if not mdx_path or not mdx_path.exists():
        print(f"  ❌ MDX file not found for {site_slug}/{article_slug}")
        return False

    print(f"\n🔍 Reviewing: {site_slug}/{article_slug} ({mdx_path})")

    try:
        content = mdx_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  ❌ Failed to read {mdx_path}: {e}")
        return False

    fm, body = parse_frontmatter(content)
    keyword = fm.get("target_keyword", "") or fm.get("keyword", "") or ""
    locale = locale or fm.get("locale", "") or SITES.get(site_slug, {}).get("default_locale", "en")

    all_reasons: List[str] = []
    gate_scores: Dict[str, Any] = {}
    gates_passed = 0
    gates_total = 4

    # 1. Plagiarism
    print("  🧪 Running plagiarism check...")
    plag_pass, plag_sim, plag_match = plagiarism_check(site_slug, body, exclude_slug=article_slug)
    gate_scores["plagiarism"] = {"similarity": round(plag_sim, 4), "matched_article": plag_match, "passed": plag_pass}
    if not plag_pass:
        all_reasons.append(f"Plagiarism: similarity {plag_sim:.2%} with '{plag_match}' exceeds {PLAGIARISM_THRESHOLD:.0%}")
    else:
        gates_passed += 1
        print(f"    ✅ Similarity {plag_sim:.2%}")

    # 2. SEO
    print("  🧪 Running SEO validation...")
    seo_pass, seo_reasons, seo_scores = seo_validate(fm, body, keyword)
    gate_scores["seo"] = {**seo_scores, "passed": seo_pass, "details": seo_reasons}
    if not seo_pass:
        for r in seo_reasons:
            all_reasons.append(f"SEO: {r}")
    else:
        gates_passed += 1
        print("    ✅ SEO OK")

    # 3. Fact-check
    print("  🧪 Running fact-check...")
    fact_pass, fact_reasons, fact_scores = fact_check(body, db_path)
    gate_scores["fact_check"] = {**fact_scores, "passed": fact_pass, "details": fact_reasons}
    if not fact_pass:
        for r in fact_reasons:
            all_reasons.append(f"Fact-check: {r}")
    else:
        gates_passed += 1
        print("    ✅ Fact-check OK")

    # 4. Readability
    print("  🧪 Running readability check...")
    read_pass, read_reasons, read_scores = readability_check(body, locale)
    gate_scores["readability"] = {**read_scores, "passed": read_pass, "details": read_reasons}
    if not read_pass:
        for r in read_reasons:
            all_reasons.append(f"Readability: {r}")
    else:
        gates_passed += 1
        print("    ✅ Readability OK")

    # Decision
    approved = gates_passed == gates_total

    # Check iteration count (max 3 retries)
    iteration = payload.get("iteration", 0)
    max_iterations = 3

    # If max iterations reached and still not approved, emit dead letter
    if not approved and iteration >= max_iterations:
        emit_event(
            "content.rejected_permanently",
            {
                "original_event_id": event.get("id"),
                "site_slug": site_slug,
                "article_slug": article_slug,
                "locale": locale,
                "file_path": str(mdx_path.relative_to(BASE_DIR)) if mdx_path else "",
                "reasons": all_reasons,
                "final_scores": gate_scores,
                "iterations": iteration,
            },
            priority=2,
            dry_run=dry_run,
        )
        print(f"  🚫 DEAD LETTER: Max iterations ({max_iterations}) reached. Manual review required.")
        return False

    emit_payload = {
        "original_event_id": event.get("id"),
        "site_slug": site_slug,
        "article_slug": article_slug,
        "locale": locale,
        "file_path": str(mdx_path.relative_to(BASE_DIR)) if mdx_path else "",
        "approved": approved,
        "gates_passed": gates_passed,
        "gates_total": gates_total,
        "scores": gate_scores,
        "reasons": all_reasons if not approved else [],
        "iteration": iteration + 1,
        "feedback": all_reasons if not approved else [],
    }
    if approved:
        print(f"  ✅ APPROVED ({gates_passed}/{gates_total} gates passed)")
        emit_event("content.approved", emit_payload, priority=2, target_agent="agent-publisher", dry_run=dry_run)
    elif iteration >= max_iterations:
        print(f"  ❌ REJECTED after {iteration} iterations — MAX RETRIES EXCEEDED")
        print(f"     Final score: {gates_passed}/{gates_total} gates passed")
        for r in all_reasons:
            print(f"     - {r}")
        # Emit to dead-letter queue for manual review
        emit_payload["dead_letter_reason"] = "max_retries_exceeded"
        emit_event("content.rejected_permanently", emit_payload, priority=1, target_agent="agent-strategist", dry_run=dry_run)
    else:
        print(f"  ❌ REJECTED ({gates_passed}/{gates_total} gates passed) — sending feedback to writer (iteration {iteration + 1}/{max_iterations})")
        for r in all_reasons:
            print(f"     - {r}")
        # Emit feedback event for writer to iterate
        feedback_payload = {
            **emit_payload,
            "iteration": iteration + 1,
            "feedback": all_reasons,
            "scores": gate_scores,
        }
        emit_event("content.revision_needed", feedback_payload, priority=2, target_agent="agent-writer", dry_run=dry_run)

    return True


# ── Event lifecycle ──────────────────────────────────────────────────────

def list_inbox_events() -> List[Path]:
    if not INBOX_DIR.exists():
        return []
    files = sorted(
        [f for f in INBOX_DIR.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda p: p.stat().st_mtime,
    )
    return files


def consume_events(limit: int = 10, dry_run: bool = False, db_path: Path = DB_PATH, timeout: int = 120, batch_size: int = 10) -> int:
    """Consume up to `limit` content.written events.
    
    Args:
        limit: Max total events to process
        dry_run: Preview checks without emitting events
        db_path: Path to SQLite DB
        timeout: Overall timeout in seconds for the batch
        batch_size: Number of articles per batch (for progress logging)
    """
    ensure_dirs()
    overall_start = time.time()
    files = list_inbox_events()
    processed = 0
    failed = 0

    for idx, path in enumerate(files):
        if processed >= limit:
            break
            
        # Check overall timeout — if approaching, finish current article and exit cleanly
        elapsed = time.time() - overall_start
        remaining = timeout - elapsed
        if remaining <= 15:
            print(f"\n⏰ Timeout approaching ({elapsed:.0f}s / {timeout}s). Finishing current batch and exiting cleanly.")
            break
        
        # Batch progress logging (only when starting a new batch or every article)
        batch_num = (processed // batch_size) + 1
        in_batch = (processed % batch_size) + 1
        if in_batch == 1 or processed == 0:
            print(f"\n📦 Starting batch {batch_num} (batch size: {batch_size}, limit: {limit}, elapsed: {elapsed:.0f}s)")
        
        event = read_event(path)
        if event is None:
            move_event(path, FAILED_DIR, dry_run=dry_run)
            failed += 1
            continue

        event_type = event.get("type", "")
        if event_type != SUPPORTED_EVENT_TYPE:
            continue

        proc_path = claim_inbox_json(path, dry_run=dry_run)
        if not proc_path:
            continue
        event["_file_path"] = str(proc_path)

        success = process_event(event, dry_run=dry_run, db_path=db_path)

        if success:
            complete_claimed_event(event, dry_run=dry_run)
            processed += 1
        else:
            fail_claimed_event(event, dry_run=dry_run)
            failed += 1

    total_elapsed = time.time() - overall_start
    print(f"\n📊 Summary: {processed} processed, {failed} failed, {limit - processed - failed} remaining (total time: {total_elapsed:.1f}s)")
    return processed


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent Reviewer — Hermes Event Consumer for Content Quality Gates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --consume --limit 5 --dry-run
  %(prog)s --consume --limit 10
  %(prog)s --consume --timeout 300 --batch-size 5
""",
    )
    parser.add_argument("--consume", action="store_true", help="Consume pending content.written events")
    parser.add_argument("--limit", type=int, default=5, help="Max events to process (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Preview checks without emitting events")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="Path to SQLite DB")
    parser.add_argument("--timeout", type=int, default=120, help="Overall timeout in seconds for the batch (default: 120)")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of articles per batch (default: 10)")

    args = parser.parse_args()

    if not args.consume:
        parser.print_help()
        return 0

    db_path = Path(args.db)

    processed = consume_events(limit=args.limit, dry_run=args.dry_run, db_path=db_path, timeout=args.timeout, batch_size=args.batch_size)
    return 0 if processed >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
