#!/usr/bin/env python3
"""Agent: Title Trimmer — finds MDX titles >60 chars and proposes trims.

Why
---
Google truncates SERP titles at ~60 chars (mobile slightly less). A 75-char
title still ranks at position 4 but the value prop after char 60 is invisible
to users — they see "Tediber Review 2026: Testing the French Mat..." and
click less than a tighter title would earn. The existing CTR optimizer
(scripts/agent_ctr_optimizer.py) replaces titles wholesale via pattern
templates; that's the wrong tool when the existing title is good content
that just needs trimming.

Strategy
--------
Rule-based pipeline first (no LLM cost for the easy 80%):
  1. Drop trailing parenthetical "(...)" if removing it gets ≤60.
  2. Drop trailing clause after last `|`.
  3. Drop trailing clause after last `:` (only if the head still contains
     the entity name / year — measured by retention of first 3 tokens).
  4. Strip known boilerplate suffixes ("What Reviews Don't Say",
     "Praised by Internet Users", "Notre Coup de Cœur", ...).
  5. If still >60 and GEMINI_API_KEY is set, fall back to Gemini
     3.1-flash-lite to produce a single trimmed candidate.

Output
------
Queue-only at reports/agent_queues/title_trim_proposed/{date}.json.
No Hermes events. Human reviews before applying.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

# Reuse the path + frontmatter helpers from the CTR optimizer (same repo).
sys.path.insert(0, str(Path(__file__).parent))
from affiliate_paths import portfolio_root  # noqa: E402

AGENT_NAME = "agent-title-trimmer"

# Google SERP visible title length. Mobile is slightly less; 60 is a safe target.
TITLE_LIMIT = 60
# Floor: don't trim below this. A 19-char trim wastes SERP real estate.
# When multiple rules produce candidates, we pick the longest acceptable one.
MIN_LEN = 30

BASE_DIR = portfolio_root()
REPORTS_DIR = BASE_DIR / "reports"
QUEUE_DIR = REPORTS_DIR / "agent_queues" / "title_trim_proposed"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

# Sites with content dirs we should scan. Skip WeLoveInstant (out of scope).
SITES = ["matelas", "bureau", "cafe", "aspirateur", "pixinstant"]

# Frontmatter parser (matches the same regex used in agent_ctr_optimizer)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
TITLE_LINE_RE = re.compile(r'^title:\s*["\']?(.*?)["\']?\s*$', re.MULTILINE)

# Boilerplate suffixes we can safely strip — order matters; longest first.
# Each entry is (regex, locale) for documentation only; pattern is what's applied.
BOILERPLATE_SUFFIXES = [
    r"\s*\|\s*What Reviews Don'?t Say\s*$",
    r"\s*\|\s*Praised by Internet Users\s*$",
    r"\s*\|\s*Tested\s*\d+\s*Nights?\s*$",
    r"\s*\|\s*Notre Coup de Cœur\s*$",
    r"\s*\|\s*Ce que les Avis Clients Cachent\s*$",
    r"\s*:\s*Testing the French Mattress\s+.*$",
    r"\s*\(\s*Tested\s*&\s*Compared\s*\)\s*$",
    r"\s*\(\s*Tested\s+\d+\s+Nights?\s*\)\s*$",
]

GEMINI_MODEL = os.environ.get("GEMINI_TRIM_MODEL", "gemini-3.1-flash-lite-preview")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


@dataclass
class TitleProposal:
    site: str
    mdx_path: str
    locale: str
    content_type: str
    current_title: str
    current_length: int
    proposed_title: Optional[str]
    proposed_length: Optional[int]
    strategy: str  # "paren_drop" | "pipe_drop" | "colon_drop" | "boilerplate" | "gemini" | "manual"
    rule_chain: list[str]


# ---------------------------------------------------------------------------
# MDX scanning
# ---------------------------------------------------------------------------

def _detect_locale_from_path(rel_path: Path, site_default: str = "fr") -> str:
    """Infer locale from a file path under the site repo.

    Handles both layouts:
      - parallel:  content-en/<...>  → en
      - nested:    content/en/<...>  → en
      - default:   content/<not-locale>/<...>  → site default
    """
    parts = rel_path.parts
    LOCALES = {"en", "de", "es", "it", "uk"}
    if not parts:
        return site_default
    first = parts[0]
    if first.startswith("content-"):
        suffix = first[len("content-"):]
        return suffix if suffix in LOCALES else site_default
    if first == "content" and len(parts) >= 2 and parts[1] in LOCALES:
        return parts[1]
    return site_default


def _read_title(mdx_path: Path) -> tuple[Optional[str], dict[str, str]]:
    try:
        text = mdx_path.read_text(encoding="utf-8")
    except OSError:
        return None, {}
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, {}
    fm_text = m.group(1)
    title_match = TITLE_LINE_RE.search(fm_text)
    title = title_match.group(1).strip() if title_match else None
    # Cheap metadata for context
    meta: dict[str, str] = {}
    for key in ("type", "slug", "datePublished"):
        match = re.search(rf'^{key}:\s*["\']?(.*?)["\']?\s*$', fm_text, re.MULTILINE)
        if match:
            meta[key] = match.group(1).strip()
    return title, meta


def scan_site(site_slug: str) -> list[tuple[Path, str, str, dict[str, str]]]:
    """Return [(mdx_path, locale, title, meta)] for every MDX with a title."""
    repo = BASE_DIR / site_slug
    if not repo.is_dir():
        return []
    out = []
    # All content dirs: content/, content-en/, content-de/, content-es/, content-it/, content-uk/
    content_roots = sorted({p for p in repo.glob("content*") if p.is_dir()})
    for root in content_roots:
        for mdx in root.rglob("*.mdx"):
            # Skip drafts, data fixtures, pages index
            if any(part in {"data", "pages"} for part in mdx.parts):
                continue
            title, meta = _read_title(mdx)
            if not title:
                continue
            rel = mdx.relative_to(repo)
            locale = _detect_locale_from_path(rel)
            out.append((mdx, locale, title, meta))
    return out


# ---------------------------------------------------------------------------
# Trimming pipeline
# ---------------------------------------------------------------------------

def _acceptable(candidate: str) -> bool:
    """Trim must land in [MIN_LEN, TITLE_LIMIT]."""
    return candidate and MIN_LEN <= len(candidate) <= TITLE_LIMIT


def _try_paren_drop(title: str) -> Optional[str]:
    m = re.search(r"\s*\([^()]*\)\s*$", title)
    if not m:
        return None
    candidate = title[: m.start()].rstrip()
    return candidate if _acceptable(candidate) else None


def _try_pipe_drop(title: str) -> Optional[str]:
    if "|" not in title:
        return None
    head = title.rsplit("|", 1)[0].rstrip()
    return head if _acceptable(head) else None


def _try_colon_drop(title: str) -> Optional[str]:
    """Drop content after last ':' BUT only if the head still has signal.

    Heuristic: keep if first 3 alphanumeric tokens of original survive in head.
    Floor at MIN_LEN — a 23-char trim from 67-char original wastes SERP space
    that could carry the value prop.
    """
    if ":" not in title:
        return None
    head = title.rsplit(":", 1)[0].rstrip()
    if not _acceptable(head):
        return None
    orig_tokens = re.findall(r"\w+", title)[:3]
    head_tokens = re.findall(r"\w+", head)
    if not all(t in head_tokens for t in orig_tokens):
        return None
    return head


def _try_boilerplate_strip(title: str) -> Optional[str]:
    candidate = title
    for pattern in BOILERPLATE_SUFFIXES:
        candidate = re.sub(pattern, "", candidate, flags=re.IGNORECASE).rstrip()
    if candidate != title and _acceptable(candidate):
        return candidate
    return None


def _validate_gemini_output(original: str, candidate: str) -> tuple[bool, str]:
    """Reject candidates that hallucinate years or drop the entity.

    Live run on 2026-05-11 observed Gemini rewriting `Seniorenmatratze ...
    2026` to `... (2024)`. Treat that as a hallucination and reject.
    """
    if not candidate:
        return False, "empty"
    if len(candidate) > TITLE_LIMIT:
        return False, "over_limit"
    if len(candidate) < MIN_LEN:
        return False, "under_floor"
    # Year preservation: any 4-digit year in input must survive in output.
    in_years = set(re.findall(r"\b(20\d{2})\b", original))
    out_years = set(re.findall(r"\b(20\d{2})\b", candidate))
    if in_years and not in_years & out_years:
        return False, f"year_drift:{in_years}->{out_years}"
    # Entity preservation: at least the first alphabetic token of original must survive
    # (case-insensitive). This catches "Tediber Review" → "Mattress Review" type drift.
    in_tokens = re.findall(r"[A-Za-zÀ-ÿ]+", original)
    if in_tokens:
        first = in_tokens[0]
        if first.lower() not in candidate.lower():
            return False, f"entity_lost:{first}"
    return True, ""


def _try_gemini_trim(title: str, locale: str) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None
    try:
        import urllib.request
    except Exception:
        return None
    locale_names = {"fr": "French", "en": "English", "de": "German", "es": "Spanish", "it": "Italian"}
    lang = locale_names.get(locale, "English")
    in_years = re.findall(r"\b(20\d{2})\b", title)
    year_constraint = (
        f" The year {in_years[0]} MUST appear verbatim — do not change or omit it."
        if in_years else ""
    )
    prompt = (
        f"Rewrite this SEO title to be at most {TITLE_LIMIT} characters while "
        f"preserving the brand/entity name and the core value proposition. "
        f"Stay in {lang}.{year_constraint} Reply with ONLY the rewritten title, "
        f"no quotes or explanation.\n\nOriginal: {title}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 100, "temperature": 0.3},
    }).encode("utf-8")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cands = data.get("candidates", [])
        if not cands:
            return None
        parts = cands[0].get("content", {}).get("parts", [])
        if not parts:
            return None
        text = parts[0].get("text", "").strip().strip('"\'')
        ok, reason = _validate_gemini_output(title, text)
        if not ok:
            print(f"  ⚠️  Gemini output rejected ({reason}): {text!r}", file=sys.stderr)
            return None
        return text
    except Exception as exc:
        print(f"  ⚠️  Gemini trim failed: {exc}", file=sys.stderr)
        return None


def trim_title(
    title: str,
    locale: str,
    use_gemini: bool = False,
) -> tuple[Optional[str], str, list[str]]:
    """Return (proposed, strategy, rule_chain).

    Try all rule-based strategies, return the LONGEST acceptable candidate
    (preserves the most signal). Gemini fallback only when use_gemini=True
    and no rule produces an in-range trim — opt-in to avoid API spend.
    """
    if len(title) <= TITLE_LIMIT:
        return None, "no_op", []

    chain: list[str] = []
    candidates: list[tuple[str, str]] = []

    for name, fn in [
        ("boilerplate", _try_boilerplate_strip),
        ("paren_drop", _try_paren_drop),
        ("pipe_drop", _try_pipe_drop),
        ("colon_drop", _try_colon_drop),
    ]:
        chain.append(name)
        result = fn(title)
        if result:
            candidates.append((name, result))

    if candidates:
        # Longest acceptable trim wins — preserves the most SERP-visible signal.
        candidates.sort(key=lambda x: -len(x[1]))
        strategy, result = candidates[0]
        return result, strategy, chain

    if use_gemini:
        chain.append("gemini")
        g = _try_gemini_trim(title, locale)
        if g:
            return g, "gemini", chain

    return None, "no_strategy_worked", chain


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(
    sites: Optional[list[str]] = None,
    dry_run: bool = False,
    use_gemini: bool = False,
) -> Path:
    sites = sites or SITES
    proposals: list[TitleProposal] = []
    over_limit_count = 0
    scanned = 0
    for site in sites:
        entries = scan_site(site)
        scanned += len(entries)
        for mdx, locale, title, meta in entries:
            if len(title) <= TITLE_LIMIT:
                continue
            over_limit_count += 1
            proposed, strategy, chain = trim_title(title, locale, use_gemini=use_gemini)
            proposals.append(TitleProposal(
                site=site,
                mdx_path=str(mdx),
                locale=locale,
                content_type=meta.get("type", ""),
                current_title=title,
                current_length=len(title),
                proposed_title=proposed,
                proposed_length=len(proposed) if proposed else None,
                strategy=strategy,
                rule_chain=chain,
            ))

    print(f"Scanned {scanned} MDX files across {len(sites)} sites")
    print(f"Found {over_limit_count} titles > {TITLE_LIMIT} chars")
    by_strategy: dict[str, int] = {}
    for p in proposals:
        by_strategy[p.strategy] = by_strategy.get(p.strategy, 0) + 1
    for strat, n in sorted(by_strategy.items(), key=lambda x: -x[1]):
        print(f"  {strat}: {n}")

    today = date.today().isoformat()
    out_path = QUEUE_DIR / f"{today}.json"
    if not dry_run:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "agent": AGENT_NAME,
            "title_limit": TITLE_LIMIT,
            "sites_scanned": sites,
            "total_scanned": scanned,
            "total_over_limit": over_limit_count,
            "proposals": [p.__dict__ for p in proposals],
        }
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  📥 Queued: {out_path}")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=AGENT_NAME)
    parser.add_argument("--site", type=str, help="Single site (default: all 5)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--gemini-fallback",
        action="store_true",
        help=f"Use {GEMINI_MODEL} for titles no rule can trim. Costs API.",
    )
    args = parser.parse_args()
    sites = [args.site] if args.site else None
    run(sites=sites, dry_run=args.dry_run, use_gemini=args.gemini_fallback)
    return 0


if __name__ == "__main__":
    sys.exit(main())
