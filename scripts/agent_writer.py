#!/usr/bin/env python3
"""
Agent Writer — Hermes Event Consumer for Content Generation & Refresh

Consumes content.refresh_needed events from the Hermes inbox (env paths via ``scripts/hermes_bus.py``).
For each event:
  1. Reads event payload (article slug, site, locale)
  2. Resolves the MDX file path from the repo
  3. Reads existing frontmatter and body
  4. Generates expanded/revised MDX content via Anthropic Claude API
  5. Writes updated MDX to repo (preserves frontmatter schema)
  6. Commits to a git branch
  7. Emits content.written event

Handles:
  - Thin content expansion (target 1200+ words)
  - Stale content refresh
  - Frontmatter schema preservation

Usage:
    python3 agent_writer.py --consume [--limit N] [--dry-run]
    python3 agent_writer.py --consume --limit 5 --dry-run
    python3 agent_writer.py --consume --limit 10
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from affiliate_paths import portfolio_root
from hermes_bus import (
    claim_inbox_json,
    complete_claimed_event,
    ensure_hermes_dirs,
    fail_claimed_event,
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
    "slug", "title", "meta_description", "date", "og_image", "thumbnail",
    "angle", "target_keyword",
    # Also accept common variants used across sites
    "description", "datePublished", "dateModified", "publishedAt", "updatedAt",
    "image", "author", "authorId", "category", "type", "locale",
    "hreflangGroup", "topPick", "priceRange", "verdict", "products",
    "keywords", "criteria", "faq", "targetAudience", "budgetRanges",
}

THIN_CONTENT_TARGET = 1200  # words
MAX_RETRIES = 3

SITES = {
    "aspirateur": {
        "name": "Aspirateur",
        "repo_path": BASE_DIR / "aspirateur",
        "is_monorepo": False,
        "default_locale": "fr",
    },
    "bureau": {
        "name": "Bureau",
        "repo_path": BASE_DIR / "bureau",
        "is_monorepo": False,
        "default_locale": "fr",
    },
    "matelas": {
        "name": "Matelas",
        "repo_path": BASE_DIR / "matelas",
        "is_monorepo": False,
        "default_locale": "fr",
    },
    "cafe": {
        "name": "Cafe",
        "repo_path": BASE_DIR / "cafe",
        "is_monorepo": False,
        "default_locale": "fr",
    },
    "pixinstant": {
        "name": "PixInstant",
        "repo_path": BASE_DIR / "pixinstant",
        "is_monorepo": False,
        "default_locale": "fr",
    },
    "airpurify": {
        "name": "AirPurify",
        "repo_path": BASE_DIR / "affiliate-suite" / "apps" / "airpurify",
        "is_monorepo": True,
        "default_locale": "en",
    },
    "safehive": {
        "name": "SafeHive",
        "repo_path": BASE_DIR / "affiliate-suite" / "apps" / "safehive",
        "is_monorepo": True,
        "default_locale": "en",
    },
    "pawhive": {
        "name": "PawHive",
        "repo_path": BASE_DIR / "affiliate-suite" / "apps" / "pawhive",
        "is_monorepo": True,
        "default_locale": "en",
    },
}

LOCALES = {"fr", "en", "de", "es", "it", "uk", "ja"}


def ensure_dirs() -> None:
    ensure_hermes_dirs()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit_event(event_type: str, payload: dict, priority: int = 3, routing_key: str = "agent.publisher") -> Path:
    """Emit a follow-up event to the Hermes event bus."""
    ensure_dirs()
    event = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "priority": priority,
        "payload": payload,
        "timestamp": now_iso(),
        "source_agent": "agent-writer",
        "routing_key": routing_key,
    }
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


# ── Frontmatter helpers ──────────────────────────────────────────────────

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
    # Simple key: value parsing (handles flat scalars and lists)
    current_key = None
    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Detect list items
        if stripped.startswith("- "):
            if current_key:
                if isinstance(fm.get(current_key), list):
                    fm[current_key].append(stripped[2:].strip().strip('"').strip("'"))
                else:
                    # Convert scalar to list
                    fm[current_key] = [fm[current_key], stripped[2:].strip().strip('"').strip("'")]
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
            # Quote if contains special chars
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
    """Resolve the MDX file path for an existing article given site and slug.

    Locale-aware: on parallel-layout sites (matelas/bureau/cafe — `content/`
    default + `content-<loc>/`) and nested-layout sites (pixinstant — `content/`
    + `content/<loc>/`), we MUST never return a path from a sibling locale or
    we'll overwrite the wrong-language file.

    Algorithm:
      - Default locale → search only under `content/` excluding `content/<loc>/`
        subtrees.
      - Non-default locale → search `content-<loc>/**` and `content/<loc>/**`.
      - Monorepo (airpurify/safehive/pawhive) → search `content/**/<loc>/**`
        scoped to the explicit locale only.

    Returns None if no matching file exists in the requested locale (callers
    then bail rather than reading + overwriting cross-locale content).
    """
    site = SITES.get(site_slug)
    if not site:
        print(f"  ⚠️ Unknown site: {site_slug}")
        return None

    repo = site["repo_path"]
    if not repo.exists():
        print(f"  ⚠️ Repo not found: {repo}")
        return None

    # Normalize locale hint
    locale = locale_hint or site["default_locale"]
    # Some events pass locale as "content-de" or "content"; normalize
    locale = locale.replace("content-", "").replace("content", site["default_locale"])
    if locale not in LOCALES:
        locale = site["default_locale"]

    default_locale = site["default_locale"]

    # Search patterns (ordered fast-path: explicit known locations first).
    search_patterns: List[Path] = []

    if site["is_monorepo"]:
        # Monorepo: content/<type>/<locale>/<slug>.mdx
        for subdir in ["reviews", "guides", "pillars", "data", "comparatifs", "articles", "tests", "avis", "pages"]:
            search_patterns.append(repo / "content" / subdir / locale / f"{article_slug}.mdx")
            # Also check deploy copy if present
            search_patterns.append(repo / "deploy" / "content" / subdir / locale / f"{article_slug}.mdx")
        # Also try flat content/<locale>/
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


# ── Claude API ───────────────────────────────────────────────────────────

def get_claude_api_key() -> Optional[str]:
    return os.environ.get("ANTHROPIC_API_KEY")


def call_claude(prompt: str, max_tokens: int = 4000, timeout: int = 180) -> str:
    """Call Anthropic Claude API via HTTP with cost tracking."""
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

    start = time.time()
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            response_text = data["content"][0]["text"]
            duration = round(time.time() - start, 2)
            
            # Track cost
            try:
                from claude_cost_tracker import log_call
                log_call(prompt, response_text, metadata={"agent": "writer", "duration_sec": duration})
            except ImportError:
                pass
            
            return response_text
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Claude API HTTP {e.code}: {body}")
    except Exception as e:
        raise RuntimeError(f"Claude API error: {e}")


def call_claude_parallel(prompts: list, max_tokens: int = 4000, timeout: int = 180, max_workers: int = 3) -> list:
    """
    Call Claude API in parallel for multiple prompts.
    
    Args:
        prompts: List of (index, prompt) tuples or just list of prompts
        max_tokens: Max tokens per call
        timeout: Timeout per call
        max_workers: Number of concurrent API calls (default 3)
    
    Returns:
        List of (index, result_or_exception) tuples
    """
    results = [None] * len(prompts)
    
    def _call_single(idx_prompt):
        idx, prompt = idx_prompt
        try:
            result = call_claude(prompt, max_tokens=max_tokens, timeout=timeout)
            return (idx, result)
        except Exception as e:
            return (idx, e)
    
    start = time.time()
    print(f"  🚀 Parallel Claude API calls: {len(prompts)} articles, {max_workers} concurrent workers")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_call_single, (i, p)): i for i, p in enumerate(prompts)}
        
        for future in concurrent.futures.as_completed(futures):
            idx, result = future.result()
            results[idx] = result
            elapsed = round(time.time() - start, 1)
            print(f"    ✅ Article {idx+1}/{len(prompts)} complete ({elapsed}s elapsed)")
    
    total_time = round(time.time() - start, 1)
    print(f"  🏁 All {len(prompts)} articles complete in {total_time}s (vs ~{len(prompts)*90}s sequential)")
    
    return results


def build_generation_prompt(fm: dict, body: str, reason: str, word_count: int, locale: str, site_name: str, feedback: list = None, iteration: int = 0) -> str:
    """Build a prompt for Claude to generate expanded/refreshed MDX body.
    
    If feedback is provided (from reviewer rejection), includes critic feedback
    for the writer to fix specific issues.
    """
    title = fm.get("title", "")
    keyword = fm.get("target_keyword", "") or fm.get("keyword", "") or ""
    angle = fm.get("angle", "")
    meta_desc = fm.get("meta_description", "") or fm.get("description", "")

    instructions = []
    if reason == "thin" or word_count < THIN_CONTENT_TARGET:
        instructions.append(
            f"The current article is only {word_count} words. Expand it to at least {THIN_CONTENT_TARGET} words. "
            "Add depth: more detailed explanations, practical examples, buyer considerations, and a comprehensive FAQ section."
        )
    elif reason == "stale":
        instructions.append(
            "The article is stale (has not been refreshed in 90+ days). Update it with current 2026 information, "
            "refresh any outdated claims, improve clarity, and ensure the content remains authoritative."
        )
    else:
        instructions.append(
            "Refresh and improve the article. Expand where thin, update where stale, and ensure strong SEO coverage."
        )

    # Add critic feedback section if this is a revision
    feedback_section = ""
    if feedback and iteration > 0:
        feedback_section = f"""

⚠️ CRITIC FEEDBACK (Revision {iteration}/3):
The previous version was REJECTED by the quality reviewer. You MUST fix these specific issues:

"""
        for i, item in enumerate(feedback, 1):
            feedback_section += f"{i}. {item}\n"
        
        feedback_section += """
IMPORTANT: Address EACH feedback item above explicitly. The reviewer will re-check all gates.
- If plagiarism is too high: rewrite sentences with different structure and vocabulary
- If SEO issues: fix title length, meta description, add keyword in first 100 words
- If fact-check failed: verify all ASINs and prices are correct
- If readability is poor: use shorter sentences, simpler words, more paragraph breaks
"""

    # Anti-plagiarism instructions for similar topics
    anti_plagiarism = """
CRITICAL - AVOID PLAGIARISM:
This site has multiple articles about similar products. You MUST write with a UNIQUE voice and structure:
- Use different sentence structures and vocabulary from the original
- Change the order of information presentation
- Use different examples, comparisons, and analogies
- Vary paragraph length and structure
- Use distinct transition words and phrases
- Avoid copying any sentence longer than 5 words from the original
"""

    # Readability instructions based on locale
    readability_map = {
        "fr": "Use simple French: short sentences (15-20 words max), common vocabulary, active voice. Target Flesch score 50-70. Avoid complex subordinate clauses.",
        "en": "Use simple English: short sentences, common words, active voice. Target Flesch score 60-70. Write at 8th-grade reading level.",
        "de": "Use simple German: short sentences, common vocabulary. Target Flesch score 50-70.",
        "es": "Use simple Spanish: short sentences, common vocabulary. Target Flesch score 50-70.",
        "it": "Use simple Italian: short sentences, common vocabulary. Target Flesch score 50-70.",
    }
    readability_instructions = readability_map.get(locale, readability_map.get("en", "Use simple language: short sentences, common vocabulary, active voice."))

    prompt = f"""You are an expert SEO content writer for an affiliate site. Write in the same language as the original content (locale: {locale}).

Site: {site_name}
Title: {title}
Target Keyword: {keyword}
Angle: {angle}
Meta Description: {meta_desc}

Original article body (MDX format):

---
{body}
---

Instructions:
{chr(10).join(instructions)}{feedback_section}

{anti_plagiarism}

READABILITY REQUIREMENTS:
{readability_instructions}

SEO REQUIREMENTS:
- Title must be 50-60 characters (current: {len(title)} chars)
- Meta description must be 150-160 characters (current: {len(meta_desc)} chars)
- Include target keyword in first 100 words
- Use H2 headings every 200-300 words
- Add 2-3 internal links to related articles (use [anchor text](/slug) format)
- Include at least 1 comparison table with product specs

PRODUCT DATA REQUIREMENTS:
- Only use real, valid Amazon ASINs (10-character alphanumeric, e.g., B08N5WRWNW)
- NEVER use placeholder text like "FRONTMATTE" or "ASIN-HERE"
- Include current prices with € symbol for EU markets
- Mention 3-5 specific products with brand names

Requirements:
- Return ONLY the article body (no frontmatter, no markdown code fences).
- Use proper MDX formatting: headings with ##, bullet lists, bold for emphasis.
- Preserve any existing product references, ASINs, and comparison tables.
- Add a clear H2 FAQ section near the end if not already present.
- Ensure the tone is helpful, authoritative, and conversion-oriented.
- Write in the SAME LANGUAGE as the original content.
- Generate a meta description at the end: <!-- meta-description: YOUR 150-160 CHAR DESC HERE -->
"""
    return prompt


# ── Git helpers ──────────────────────────────────────────────────────────

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
        # Try master fallback
        try:
            subprocess.run(["git", "checkout", "-b", branch, "master"], cwd=repo, capture_output=True, text=True, check=True)
            return True
        except subprocess.CalledProcessError:
            return False


def git_commit_file(repo: Path, file_path: Path, message: str, branch: str, dry_run: bool = False) -> bool:
    """Stage and commit a single file on the given branch."""
    if dry_run:
        print(f"  [DRY-RUN] Would git add + commit {file_path.name} on branch {branch}")
        return True
    try:
        # Ensure we are on the branch
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


# ── Database helpers ───────────────────────────────────────────────────────

def update_article_refresh(db_path: Path, article_id: int, word_count: int, dry_run: bool = False) -> None:
    if dry_run:
        print(f"  [DRY-RUN] Would update article {article_id}: word_count={word_count}, last_refreshed=now")
        return
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute(
            """UPDATE articles
               SET word_count = ?, last_refreshed = datetime('now'), thin_content_flag = ?, updated_at = datetime('now')
               WHERE article_id = ?""",
            (word_count, 0 if word_count >= 800 else 1, article_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠️ DB update failed: {e}")


# ── Core processing ───────────────────────────────────────────────────────

def process_event(event: Dict[str, Any], dry_run: bool = False, timeout: int = 600) -> bool:
    """Process a single content.refresh_needed or content.revision_needed event."""
    payload = event.get("payload", {})
    site_slug = (payload.get("site_slug") or payload.get("site") or "").strip()
    article_slug = (payload.get("article_slug") or payload.get("slug") or "").strip()
    # Aggregated decay payload (content_decay_agent): take highest-priority first article
    if (not site_slug or not article_slug) and payload.get("articles"):
        first = payload["articles"][0]
        site_slug = (first.get("site_slug") or first.get("site") or "").strip()
        article_slug = (first.get("article_slug") or first.get("slug") or "").strip()
    article_id = payload.get("article_id")
    title = payload.get("title", "")
    reason = payload.get("reason", "")
    locale_hint = payload.get("locale", "")
    
    # Check if this is a revision event (from reviewer feedback)
    event_type = event.get("type", "")
    feedback = payload.get("feedback", [])
    iteration = payload.get("iteration", 0)
    is_revision = event_type == "content.revision_needed" or (feedback and iteration > 0)

    if not site_slug or not article_slug:
        print(f"  ❌ Missing site_slug or article_slug in event {event.get('id')}")
        return False

    if is_revision:
        print(f"\n📝 Processing REVISION {iteration}/3: {site_slug}/{article_slug}")
        print(f"  📋 Feedback from reviewer:")
        for i, item in enumerate(feedback, 1):
            print(f"    {i}. {item}")
    else:
        print(f"\n📝 Processing: {site_slug}/{article_slug} (reason={reason})")

    # 1. Resolve MDX path
    mdx_path = resolve_mdx_path(site_slug, article_slug, locale_hint)
    if not mdx_path:
        return False

    # 2. Read existing content
    try:
        original_content = mdx_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  ❌ Failed to read {mdx_path}: {e}")
        return False

    fm, body = parse_frontmatter(original_content)
    original_word_count = count_words(body)
    print(f"  📄 File: {mdx_path}")
    print(f"  📊 Current word count: {original_word_count}")

    # 3. Determine locale
    locale = locale_hint or fm.get("locale", "")
    if not locale or locale not in LOCALES:
        locale = SITES.get(site_slug, {}).get("default_locale", "fr")
    locale = locale.replace("content-", "").replace("content", SITES.get(site_slug, {}).get("default_locale", "fr"))

    # 4. Generate with Claude (with feedback if revision)
    site_name = SITES.get(site_slug, {}).get("name", site_slug)
    
    # Get locale-specific instructions
    try:
        from locale_manager import LocaleManager
        locale_manager = LocaleManager()
        locale_additions = locale_manager.get_locale_specific_prompt_additions(locale)
    except ImportError:
        locale_additions = ""
    
    # Check timeout before starting generation
    start_time = time.time()
    
    # Calculate remaining time for this article
    remaining_time = timeout - (time.time() - start_time)
    if remaining_time < 60:
        print(f"  ⚠️ Timeout approaching ({remaining_time:.0f}s left), skipping article")
        return False
    
    # Try structured output first (with Pydantic schema enforcement)
    use_structured = True
    
    if use_structured:
        try:
            from article_schema import build_structured_prompt, parse_article_output, article_to_mdx
            
            base_prompt = build_generation_prompt(fm, body, reason, original_word_count, locale, site_name, feedback=feedback, iteration=iteration)
            # Add locale-specific instructions to prompt
            if locale_additions:
                base_prompt += f"\n\nLOCALE-SPECIFIC REQUIREMENTS ({locale}):\n{locale_additions}\n"
            structured_prompt = build_structured_prompt(base_prompt, locale)
            
            if dry_run:
                print(f"  [DRY-RUN] Would call Claude API with structured output (prompt length={len(structured_prompt)} chars)")
                new_body = body
            else:
                print(f"  🤖 Calling Claude API with structured output...")
                try:
                    json_response = call_claude(structured_prompt, max_tokens=8000, timeout=min(180, timeout))
                    article = parse_article_output(json_response)
                    
                    # Convert to MDX with proper frontmatter
                    new_content = article_to_mdx(article, fm)
                    
                    # Extract body for word count
                    _, new_body = parse_frontmatter(new_content)
                    
                    # Update frontmatter fields from article
                    updated_fm = dict(fm)
                    updated_fm["title"] = article.seo.title
                    updated_fm["description"] = article.seo.meta_description
                    if article.seo.keywords:
                        updated_fm["keywords"] = article.seo.keywords
                    
                    # Update products
                    if article.products:
                        updated_fm["products"] = [p.asin for p in article.products if p.asin]
                        if article.products[0].asin:
                            updated_fm["topPick"] = article.products[0].asin
                    
                    # Update dates
                    if "date" not in updated_fm and "datePublished" not in updated_fm and "publishedAt" not in updated_fm:
                        updated_fm["date"] = datetime.now().strftime("%Y-%m-%d")
                    if "dateModified" in updated_fm:
                        updated_fm["dateModified"] = datetime.now().strftime("%Y-%m-%d")
                    if "updatedAt" in updated_fm:
                        updated_fm["updatedAt"] = datetime.now().strftime("%Y-%m-%d")
                    
                    # Rebuild content with updated frontmatter
                    new_frontmatter = build_frontmatter(updated_fm)
                    new_content = new_frontmatter + "\n\n" + new_body.strip() + "\n"
                    
                    print(f"  ✅ Structured output: title={len(article.seo.title)} chars, meta={len(article.seo.meta_description)} chars")
                    
                except Exception as e:
                    print(f"  ⚠️ Structured output failed: {e}")
                    print(f"  🔄 Falling back to free-form generation...")
                    use_structured = False
        except ImportError:
            print(f"  ⚠️ article_schema not available, using free-form generation")
            use_structured = False
    
    # Fallback to free-form generation
    if not use_structured:
        prompt = build_generation_prompt(fm, body, reason, original_word_count, locale, site_name, feedback=feedback, iteration=iteration)
        # Add locale-specific instructions
        if locale_additions:
            prompt += f"\n\nLOCALE-SPECIFIC REQUIREMENTS ({locale}):\n{locale_additions}\n"
        
        if dry_run:
            print(f"  [DRY-RUN] Would call Claude API (prompt length={len(prompt)} chars)")
            new_body = body
        else:
            print(f"  🤖 Calling Claude API...")
            try:
                new_body = call_claude(prompt, max_tokens=6000, timeout=min(180, timeout))
                # Strip any accidental code fences
                new_body = re.sub(r"^```(?:mdx|markdown)?\s*\n", "", new_body)
                new_body = re.sub(r"\n```\s*$", "", new_body)
            except Exception as e:
                print(f"  ❌ Claude generation failed: {e}")
                return False
        
        # 5. Update frontmatter (free-form path)
        updated_fm = dict(fm)
        if "slug" not in updated_fm and "slug" in FRONTMATTER_SCHEMA:
            updated_fm["slug"] = article_slug
        if "date" not in updated_fm and "datePublished" not in updated_fm and "publishedAt" not in updated_fm:
            updated_fm["date"] = datetime.now().strftime("%Y-%m-%d")
        if "dateModified" in updated_fm:
            updated_fm["dateModified"] = datetime.now().strftime("%Y-%m-%d")
        if "updatedAt" in updated_fm:
            updated_fm["updatedAt"] = datetime.now().strftime("%Y-%m-%d")
        
        # 6. Assemble new content
        new_frontmatter = build_frontmatter(updated_fm)
        new_content = new_frontmatter + "\n\n" + new_body.strip() + "\n"

    new_word_count = count_words(new_body)
    elapsed = time.time() - start_time
    print(f"  📊 New word count: {new_word_count} (generated in {elapsed:.1f}s)")

    # 7. Write file
    if dry_run:
        content_len = len(new_content) if 'new_content' in locals() else len(new_body)
        print(f"  [DRY-RUN] Would write {content_len} chars to {mdx_path}")
    else:
        if 'new_content' not in locals():
            new_content = new_body
        mdx_path.write_text(new_content, encoding="utf-8")
        print(f"  ✅ Written: {mdx_path}")

    # 8. Git commit
    repo_path = SITES.get(site_slug, {}).get("repo_path")
    if repo_path and repo_path.exists():
        branch_name = f"content-refresh/{site_slug}-{article_slug[:30]}"
        if not git_branch_exists(repo_path, branch_name):
            git_create_branch(repo_path, branch_name)
        commit_msg = f"content: refresh {article_slug} ({reason}, {original_word_count}→{new_word_count} words)"
        git_commit_file(repo_path, mdx_path, commit_msg, branch_name, dry_run=dry_run)
    else:
        print(f"  ⚠️ No git repo found for {site_slug}")

    # 9. Update DB
    if article_id:
        update_article_refresh(DB_PATH, article_id, new_word_count, dry_run=dry_run)

    # 10. Emit content.written event (include iteration for reviewer tracking)
    emit_payload = {
        "article_id": article_id,
        "site_slug": site_slug,
        "article_slug": article_slug,
        "locale": locale,
        "reason": reason,
        "original_word_count": original_word_count,
        "new_word_count": new_word_count,
        "file_path": str(mdx_path.relative_to(BASE_DIR)) if mdx_path else "",
        "branch": branch_name if repo_path else "",
        "iteration": iteration,
    }
    emit_event("content.written", emit_payload, priority=event.get("priority", 3), routing_key="agent.publisher")

    return True


def process_events_parallel(events: list, dry_run: bool = False, timeout: int = 600, max_workers: int = 3) -> list:
    """
    Process multiple content events in parallel using concurrent Claude API calls.
    
    This is the parallel version of process_event() for batch processing.
    Each event is prepared individually, then all Claude calls are made concurrently.
    """
    if not events:
        return []
    
    print(f"\n{'='*60}")
    print(f"🚀 PARALLEL BATCH PROCESSING: {len(events)} articles")
    print(f"{'='*60}")
    
    # Phase 1: Prepare all prompts (sequential, fast)
    prompts_data = []
    for event in events:
        payload = event.get("payload", {})
        site_slug = payload.get("site_slug", "")
        article_slug = payload.get("article_slug", "")
        locale_hint = payload.get("locale", "")
        reason = payload.get("reason", "")
        feedback = payload.get("feedback", [])
        iteration = payload.get("iteration", 0)
        
        # Resolve path and read content
        mdx_path = resolve_mdx_path(site_slug, article_slug, locale_hint)
        if not mdx_path:
            continue
            
        try:
            original_content = mdx_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  ❌ Failed to read {mdx_path}: {e}")
            continue
        
        fm, body = parse_frontmatter(original_content)
        original_word_count = count_words(body)
        
        # Determine locale
        locale = locale_hint or fm.get("locale", "")
        if not locale or locale not in LOCALES:
            locale = SITES.get(site_slug, {}).get("default_locale", "fr")
        locale = locale.replace("content-", "").replace("content", SITES.get(site_slug, {}).get("default_locale", "fr"))
        
        site_name = SITES.get(site_slug, {}).get("name", site_slug)
        
        # Get locale-specific instructions
        try:
            from locale_manager import LocaleManager
            locale_manager = LocaleManager()
            locale_additions = locale_manager.get_locale_specific_prompt_additions(locale)
        except ImportError:
            locale_additions = ""
        
        # Build prompt
        base_prompt = build_generation_prompt(fm, body, reason, original_word_count, locale, site_name, feedback=feedback, iteration=iteration)
        if locale_additions:
            base_prompt += f"\n\nLOCALE-SPECIFIC REQUIREMENTS ({locale}):\n{locale_additions}\n"
        
        # Try structured prompt
        try:
            from article_schema import build_structured_prompt
            prompt = build_structured_prompt(base_prompt, locale)
            use_structured = True
        except ImportError:
            prompt = base_prompt
            use_structured = False
        
        prompts_data.append({
            "event": event,
            "mdx_path": mdx_path,
            "fm": fm,
            "body": body,
            "original_word_count": original_word_count,
            "locale": locale,
            "site_slug": site_slug,
            "article_slug": article_slug,
            "article_id": payload.get("article_id"),
            "title": payload.get("title", ""),
            "reason": reason,
            "iteration": iteration,
            "prompt": prompt,
            "use_structured": use_structured,
        })
    
    if not prompts_data:
        print("  ⚠️ No valid events to process")
        return []
    
    print(f"  📋 Prepared {len(prompts_data)} prompts")
    
    # Phase 2: Call Claude API in parallel (the slow part)
    prompts = [d["prompt"] for d in prompts_data]
    parallel_results = call_claude_parallel(prompts, max_tokens=8000, timeout=180, max_workers=max_workers)
    
    # Phase 3: Process results and write files (sequential, fast)
    results = []
    for i, (data, result) in enumerate(zip(prompts_data, parallel_results)):
        event = data["event"]
        mdx_path = data["mdx_path"]
        fm = data["fm"]
        body = data["body"]
        original_word_count = data["original_word_count"]
        locale = data["locale"]
        site_slug = data["site_slug"]
        article_slug = data["article_slug"]
        article_id = data["article_id"]
        title = data["title"]
        reason = data["reason"]
        iteration = data["iteration"]
        use_structured = data["use_structured"]
        
        print(f"\n📝 Processing result {i+1}/{len(prompts_data)}: {site_slug}/{article_slug}")
        
        if isinstance(result, Exception):
            print(f"  ❌ Claude generation failed: {result}")
            results.append(False)
            continue
        
        if dry_run:
            print(f"  [DRY-RUN] Would process result ({len(result)} chars)")
            results.append(True)
            continue
        
        # Parse result
        try:
            if use_structured:
                from article_schema import parse_article_output, article_to_mdx
                article = parse_article_output(result)
                new_content = article_to_mdx(article, fm)
                _, new_body = parse_frontmatter(new_content)
                
                # Update frontmatter
                updated_fm = dict(fm)
                updated_fm["title"] = article.seo.title
                updated_fm["description"] = article.seo.meta_description
                if article.seo.keywords:
                    updated_fm["keywords"] = article.seo.keywords
                if article.products:
                    updated_fm["products"] = [p.asin for p in article.products if p.asin]
                    if article.products[0].asin:
                        updated_fm["topPick"] = article.products[0].asin
            else:
                # Free-form
                new_body = result
                new_body = re.sub(r"^```(?:mdx|markdown)?\s*\n", "", new_body)
                new_body = re.sub(r"\n```\s*$", "", new_body)
                
                updated_fm = dict(fm)
                if "slug" not in updated_fm and "slug" in FRONTMATTER_SCHEMA:
                    updated_fm["slug"] = article_slug
            
            # Update dates
            if "date" not in updated_fm and "datePublished" not in updated_fm and "publishedAt" not in updated_fm:
                updated_fm["date"] = datetime.now().strftime("%Y-%m-%d")
            if "dateModified" in updated_fm:
                updated_fm["dateModified"] = datetime.now().strftime("%Y-%m-%d")
            if "updatedAt" in updated_fm:
                updated_fm["updatedAt"] = datetime.now().strftime("%Y-%m-%d")
            
            # Assemble content
            new_frontmatter = build_frontmatter(updated_fm)
            new_content = new_frontmatter + "\n\n" + new_body.strip() + "\n"
            new_word_count = count_words(new_body)
            
            print(f"  📊 New word count: {new_word_count}")
            
            # Write file
            mdx_path.write_text(new_content, encoding="utf-8")
            print(f"  ✅ Written: {mdx_path}")
            
            # Git commit
            repo_path = SITES.get(site_slug, {}).get("repo_path")
            if repo_path and repo_path.exists():
                branch_name = f"content-refresh/{site_slug}-{article_slug[:30]}"
                if not git_branch_exists(repo_path, branch_name):
                    git_create_branch(repo_path, branch_name)
                commit_msg = f"content: refresh {article_slug} ({reason}, {original_word_count}→{new_word_count} words)"
                git_commit_file(repo_path, mdx_path, commit_msg, branch_name, dry_run=dry_run)
            
            # Update DB
            if article_id:
                update_article_refresh(DB_PATH, article_id, new_word_count, dry_run=dry_run)
            
            # Emit event
            emit_payload = {
                "article_id": article_id,
                "site_slug": site_slug,
                "article_slug": article_slug,
                "locale": locale,
                "reason": reason,
                "original_word_count": original_word_count,
                "new_word_count": new_word_count,
                "file_path": str(mdx_path.relative_to(BASE_DIR)) if mdx_path else "",
                "branch": branch_name if repo_path else "",
                "iteration": iteration,
            }
            emit_event("content.written", emit_payload, priority=event.get("priority", 3), routing_key="agent.publisher")
            
            results.append(True)
            
        except Exception as e:
            print(f"  ❌ Failed to process result: {e}")
            results.append(False)
    
    return results


# ── Event lifecycle ──────────────────────────────────────────────────────

def list_inbox_events() -> List[Path]:
    if not INBOX_DIR.exists():
        return []
    # Sort by modification time (oldest first) so revision events created later
    # are processed after their original events
    files = sorted(
        [f for f in INBOX_DIR.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda p: p.stat().st_mtime,
    )
    return files


def consume_events(limit: int = 5, dry_run: bool = False, timeout: int = 600, batch_size: int = 5, parallel: bool = False, workers: int = 3) -> int:
    """Consume up to `limit` content.refresh_needed or content.revision_needed events.
    
    Priority: content.revision_needed events are processed first (they need immediate
    attention to complete the feedback loop with the reviewer).
    
    Args:
        limit: Max total events to process
        dry_run: Preview changes without writing files or calling API
        timeout: Overall timeout in seconds for the batch
        batch_size: Number of articles to process per batch (for progress logging)
        parallel: Use parallel Claude API calls for faster processing
        workers: Number of concurrent API workers when parallel=True
    """
    ensure_dirs()
    overall_start = time.time()
    
    # Fast scan: only read files with matching names or types
    all_files = sorted(
        [f for f in INBOX_DIR.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda p: p.stat().st_mtime,
    )
    
    # Quick filter by filename pattern first (fast)
    candidate_files = [p for p in all_files if "content_refresh" in p.name or "content_revision" in p.name]
    
    # If not enough by filename, scan all (but only read type field)
    if len(candidate_files) < limit:
        for p in all_files:
            if p in candidate_files:
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    ev = json.load(f)
                if ev.get("type") in ("content.refresh_needed", "content.revision_needed"):
                    candidate_files.append(p)
            except:
                continue
            if len(candidate_files) >= limit * 2:
                break
    
    # Sort: revision events first
    revision_files = []
    regular_files = []
    for path in candidate_files:
        try:
            event = read_event(path)
            if event and event.get("type") == "content.revision_needed":
                revision_files.append(path)
            elif event and event.get("type") == "content.refresh_needed":
                regular_files.append(path)
        except:
            pass
    
    all_files = revision_files + regular_files
    
    processed = 0
    failed = 0
    
    # Parallel mode: process in batches with concurrent API calls
    if parallel and not dry_run:
        print(f"\n🚀 PARALLEL MODE: Processing up to {limit} articles with {workers} concurrent workers")
        
        while processed < limit and all_files:
            # Check overall timeout
            elapsed = time.time() - overall_start
            remaining = timeout - elapsed
            if remaining <= 30:
                print(f"\n⏰ Timeout approaching ({elapsed:.0f}s / {timeout}s). Exiting.")
                break
            
            # Take next batch
            batch_size_actual = min(batch_size, limit - processed, len(all_files))
            batch_files = all_files[:batch_size_actual]
            all_files = all_files[batch_size_actual:]
            
            # Load events
            batch_events = []
            batch_paths = []
            for path in batch_files:
                event = read_event(path)
                if event:
                    batch_events.append(event)
                    batch_paths.append(path)
            
            if not batch_events:
                continue
            
            # Atomic claim inbox → processing
            proc_paths: list[Path] = []
            claimed_events: list[Dict[str, Any]] = []
            for path, event in zip(batch_paths, batch_events):
                proc_path = claim_inbox_json(path, dry_run=False)
                if not proc_path:
                    continue
                event["_file_path"] = str(proc_path)
                proc_paths.append(proc_path)
                claimed_events.append(event)

            if not claimed_events:
                continue

            # Process batch in parallel
            print(f"\n📦 Parallel batch: {len(claimed_events)} articles (total processed: {processed}/{limit})")
            results = process_events_parallel(claimed_events, dry_run=dry_run, timeout=int(remaining), max_workers=workers)

            # Move events based on results (clear locks via hermes_bus)
            for success, ev in zip(results, claimed_events):
                if success:
                    complete_claimed_event(ev, dry_run=False)
                    processed += 1
                else:
                    fail_claimed_event(ev, dry_run=False)
                    failed += 1
        
        total_elapsed = time.time() - overall_start
        print(f"\n📊 Summary: {processed} processed, {failed} failed, {limit - processed - failed} remaining (total time: {total_elapsed:.1f}s)")
        return processed
    
    # Sequential mode (original behavior)
    for idx, path in enumerate(all_files):
        if processed >= limit:
            break
            
        # Check overall timeout
        elapsed = time.time() - overall_start
        remaining = timeout - elapsed
        if remaining <= 30:
            print(f"\n⏰ Timeout approaching ({elapsed:.0f}s / {timeout}s). Finishing current batch and exiting cleanly.")
            break
        
        # Progress logging every article
        print(f"\n📦 Batch progress: {processed + 1}/{limit} (elapsed: {elapsed:.0f}s, remaining: {remaining:.0f}s)")
        
        event = read_event(path)
        if event is None:
            move_event(path, FAILED_DIR, dry_run=dry_run)
            continue

        event_type = event.get("type", "")

        if event_type in ("content.refresh_needed", "content.revision_needed"):
            proc_path = claim_inbox_json(path, dry_run=dry_run)
            if not proc_path:
                continue
            event["_file_path"] = str(proc_path)

            # Per-article timeout
            article_timeout = max(30, int(remaining - 30))
            success = process_event(event, dry_run=dry_run, timeout=article_timeout)

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
        description="Agent Writer — Hermes Event Consumer for Content Generation & Refresh",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --consume --limit 5 --dry-run
  %(prog)s --consume --limit 10
  %(prog)s --consume --timeout 300 --batch-size 3
""",
    )
    parser.add_argument("--consume", action="store_true", help="Consume pending content.refresh_needed events")
    parser.add_argument("--limit", type=int, default=5, help="Max events to process (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing files or calling API")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="Path to SQLite DB")
    parser.add_argument("--timeout", type=int, default=600, help="Overall timeout in seconds for the batch (default: 600)")
    parser.add_argument("--batch-size", type=int, default=5, help="Number of articles per batch (default: 5)")
    parser.add_argument("--parallel", action="store_true", help="Use parallel Claude API calls (3 concurrent workers)")
    parser.add_argument("--workers", type=int, default=3, help="Number of parallel API workers (default: 3)")

    args = parser.parse_args()

    if not args.consume:
        parser.print_help()
        return 0

    # Validate API key
    if not args.dry_run and not get_claude_api_key():
        print("❌ ANTHROPIC_API_KEY not set. Export it or use --dry-run.")
        return 1

    processed = consume_events(limit=args.limit, dry_run=args.dry_run, timeout=args.timeout, batch_size=args.batch_size, parallel=args.parallel, workers=args.workers)
    return 0 if processed >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
