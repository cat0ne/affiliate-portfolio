#!/usr/bin/env python3
"""Agent: Description Trimmer — finds MDX descriptions >160 chars and proposes trims.

Why
---
Google truncates SERP meta descriptions at ~155-160 chars (mobile slightly less).
A 240-char description still gets indexed, but only the first ~160 are shown;
worse, Google often truncates mid-word (`...with prices and pro` →
`...with prices and pro...`) which hurts CTR. The existing CTR optimizer
(scripts/agent_ctr_optimizer.py) rewrites whole meta blocks from templates;
that's overkill when the existing description is already well-targeted copy
that simply runs long.

Strategy
--------
Rule-based pipeline first (no LLM cost for the easy ~80%):
  1. Strip known boilerplate phrases harvested from the actual corpus
     ("Updated 2026 comparison", "Ce que les avis clients ne disent pas",
     etc., in all 5 supported locales).
  2. Drop trailing arrow clauses (` → ...`) — the corpus shows arrow is the
     dominant join character in over-limit descriptions.
  3. Drop trailing sentence(s) after the first 1-2 if they push us over.
  4. Drop trailing comma-delimited clause when arrow + sentence drops fail.
  5. If still >160 and `--gemini-fallback` is set, escalate to Gemini
     3.1-flash-lite with explicit constraints (keep prices, first sentence,
     specific testing claims).

Same self-critic loop as `agent_title_trimmer`: candidates are scored and the
best is returned, with Gemini escalation on hook loss / signal-retention floor.

Output
------
Queue-only at reports/agent_queues/description_trim_proposed/{date}.json.
No Hermes events. Human reviews before applying.

Design notes
------------
- `_validate_gemini_output` is re-implemented (not imported) because the
  description-specific check adds a first-sentence preservation requirement
  that the title version doesn't have.
- `_hooks_in` is extended for description-typical signals: "score X/10",
  "top N", "tested N nights/days".
- Only quoted descriptions are parsed (`description: "..."` or `'...'`).
  Folded YAML scalars (`description: >-`) are not present in the corpus
  (verified: 0 hits across all 5 sites on 2026-05-11); if added later
  they will be silently skipped, which is fine since the agent is queue-only.
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

sys.path.insert(0, str(Path(__file__).parent))
from affiliate_paths import portfolio_root  # noqa: E402

AGENT_NAME = "agent-description-trimmer"

# Google SERP visible meta description length. Mobile is slightly less; 160 is
# the conventional safe target.
DESCRIPTION_LIMIT = 160
# Floor: a description below 80 chars looks thin and Google often appends
# random page text to fill SERP real estate. Don't trim below.
MIN_LEN = 80

BASE_DIR = portfolio_root()
REPORTS_DIR = BASE_DIR / "reports"
QUEUE_DIR = REPORTS_DIR / "agent_queues" / "description_trim_proposed"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

SITES = ["matelas", "bureau", "cafe", "aspirateur", "pixinstant"]

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
# Descriptions in this corpus are always quoted (verified on 2026-05-11).
# Require the quote — unquoted matches would slurp multi-line YAML structure.
DESCRIPTION_LINE_RE = re.compile(
    r'^description:\s*(["\'])(.*?)\1\s*$',
    re.MULTILINE,
)

# Boilerplate phrases harvested from the actual corpus (top 40 recurring
# fragments at >=3 occurrences). Ordered longest-first so the longest match
# is removed first. Patterns are case-insensitive and anchored with optional
# surrounding whitespace + trailing punctuation.
BOILERPLATE_PHRASES = [
    # EN
    r"\s*Updated\s+comparison\s+with\s+prices,\s+comfort\s+and\s+durability\.?\s*",
    r"\s*Updated\s+comparison\s+with\s+prices\s+and\s+comfort\.?\s*",
    r"\s*Updated\s+comparison\s+with\s+prices\.?\s*",
    r"\s*Updated\s+202\d\s+comparison\.?\s*",
    r"\s*What\s+customer\s+reviews\s+don'?t\s+say\.?\s*",
    r"\s*What\s+customer\s+reviews\s+don'?t\s+tell\s+you\.?\s*",
    r"\s*What\s+reviews\s+don'?t\s+say\.?\s*",
    # FR
    r"\s*Comparatif\s+202\d\s+mis\s+à\s+jour\s+avec\s+prix,\s+confort\s+et\s+durabilité\.?\s*",
    r"\s*Comparatif\s+202\d\s+mis\s+à\s+jour\s+avec\s+prix\s+et\s+confort\.?\s*",
    r"\s*Comparatif\s+202\d\s+mis\s+à\s+jour\.?\s*",
    r"\s*Comparatif\s+mis\s+à\s+jour\s+avec\s+prix,\s+confort\s+et\s+durabilité\.?\s*",
    r"\s*Comparatif\s+mis\s+à\s+jour\s+avec\s+prix\s+et\s+confort\.?\s*",
    r"\s*Comparatif\s+mis\s+à\s+jour\.?\s*",
    r"\s*Ce\s+que\s+les\s+avis\s+clients\s+ne\s+disent\s+pas\.?\s*",
    # ES
    r"\s*Comparativa\s+202\d\s+actualizada\.?\s*",
    r"\s*Comparativa\s+actualizada\.?\s*",
    r"\s*Lo\s+que\s+las\s+opiniones\s+de\s+clientes\s+no\s+te\s+dicen\.?\s*",
    # IT
    r"\s*Comparativo\s+202\d\s+aggiornato\.?\s*",
    r"\s*Comparativo\s+aggiornato\.?\s*",
    r"\s*Confronto\s+aggiornato\s+con\s+prezzi\s+e\s+comfort\.?\s*",
    r"\s*Quello\s+che\s+le\s+recensioni\s+dei\s+clienti\s+non\s+dicono\.?\s*",
    # DE
    r"\s*Aktualisierter\s+Vergleich\s+202\d\.?\s*",
    r"\s*Aktualisierter\s+Vergleich\s+mit\s+Preisen,\s+Komfort\s+und\s+Haltbarkeit\.?\s*",
    r"\s*Aktualisierter\s+Vergleich\s+mit\s+Preisen\s+und\s+Sicherheitstipps\.?\s*",
    r"\s*Aktualisierter\s+Vergleich\.?\s*",
    r"\s*Vergleich\s+202\d\s+aktualisiert\.?\s*",
    r"\s*Was\s+Kundenbewertungen\s+(nicht\s+verraten|verschweigen)\.?\s*",
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
class DescriptionProposal:
    site: str
    mdx_path: str
    locale: str
    content_type: str
    current_description: str
    current_length: int
    proposed_description: Optional[str]
    proposed_length: Optional[int]
    strategy: str
    rule_chain: list[str]


# ---------------------------------------------------------------------------
# MDX scanning
# ---------------------------------------------------------------------------

LOCALES = {"en", "de", "es", "it", "uk"}


def _detect_locale_from_path(rel_path: Path, site_default: str = "fr") -> str:
    """Infer locale from a file path under the site repo.

    Mirrors `agent_title_trimmer._detect_locale_from_path` — handles both
    parallel (`content-en/`) and nested (`content/en/`) layouts.
    """
    parts = rel_path.parts
    if not parts:
        return site_default
    first = parts[0]
    if first.startswith("content-"):
        suffix = first[len("content-"):]
        return suffix if suffix in LOCALES else site_default
    if first == "content" and len(parts) >= 2 and parts[1] in LOCALES:
        return parts[1]
    return site_default


def _read_description(mdx_path: Path) -> tuple[Optional[str], dict[str, str]]:
    try:
        text = mdx_path.read_text(encoding="utf-8")
    except OSError:
        return None, {}
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, {}
    fm_text = m.group(1)
    desc_match = DESCRIPTION_LINE_RE.search(fm_text)
    description = desc_match.group(2).strip() if desc_match else None
    meta: dict[str, str] = {}
    for key in ("type", "slug", "title", "datePublished"):
        match = re.search(rf'^{key}:\s*["\']?(.*?)["\']?\s*$', fm_text, re.MULTILINE)
        if match:
            meta[key] = match.group(1).strip()
    return description, meta


def scan_site(site_slug: str) -> list[tuple[Path, str, str, dict[str, str]]]:
    """Return [(mdx_path, locale, description, meta)] for every MDX with a description."""
    repo = BASE_DIR / site_slug
    if not repo.is_dir():
        return []
    out = []
    content_roots = sorted({p for p in repo.glob("content*") if p.is_dir()})
    for root in content_roots:
        for mdx in root.rglob("*.mdx"):
            # Skip drafts, data fixtures, pages index, and any stray node_modules
            if any(part in {"data", "pages", "node_modules"} for part in mdx.parts):
                continue
            description, meta = _read_description(mdx)
            if not description:
                continue
            rel = mdx.relative_to(repo)
            locale = _detect_locale_from_path(rel)
            out.append((mdx, locale, description, meta))
    return out


# ---------------------------------------------------------------------------
# Trimming pipeline
# ---------------------------------------------------------------------------

def _acceptable(candidate: str) -> bool:
    """Trim must land in [MIN_LEN, DESCRIPTION_LIMIT]."""
    return bool(candidate) and MIN_LEN <= len(candidate) <= DESCRIPTION_LIMIT


# Sentence splitter: split on `. `, `? `, `! ` followed by a capital letter
# OR end of string. Conservative: leaves abbreviations (Mr., Dr., etc.) alone
# because the next char wouldn't be a capital + space pattern. SEO copy in
# the corpus doesn't use abbreviations in descriptions, so false-positive
# risk is negligible.
SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z€$£0-9])')


def _split_sentences(text: str) -> list[str]:
    """Split a description into sentences preserving terminal punctuation."""
    return [s.strip() for s in SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _try_redundant_phrase_strip(description: str) -> Optional[str]:
    candidate = description
    for pattern in BOILERPLATE_PHRASES:
        candidate = re.sub(pattern, " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s{2,}", " ", candidate).strip(" .")
    # After stripping mid-string boilerplate, fragments may dangle as a
    # lowercase continuation after a period (e.g. "...testés. qualite avec...").
    # Split on any `. ` boundary here (not just before capitals, like the
    # main splitter), then keep only sentences that begin uppercase/digit/$€£.
    raw_parts = re.split(r"(?<=[.!?])\s+", candidate)
    clean_sentences = [
        s.strip() for s in raw_parts
        if s.strip() and (s.strip()[0].isupper() or s.strip()[0].isdigit() or s.strip()[0] in "€$£")
    ]
    if clean_sentences:
        candidate = " ".join(clean_sentences).strip()
    if candidate and not candidate.endswith((".", "!", "?")):
        candidate += "."
    if candidate != description and _acceptable(candidate):
        return candidate
    return None


def _try_arrow_drop(description: str) -> Optional[str]:
    """Cut at last ` → ` separator. Corpus shows arrow is the dominant join
    char on over-limit descriptions (FlexiSpot teasers, "Notre sélection →")."""
    if " → " not in description:
        return None
    head = description.rsplit(" → ", 1)[0].rstrip(" .,;:")
    if head and not head.endswith((".", "!", "?")):
        head += "."
    return head if _acceptable(head) else None


def _try_sentence_drop(description: str) -> Optional[str]:
    """Keep the first N complete sentences whose combined length is <= limit.

    Greedy: takes as many leading sentences as fit, then stops. If even the
    first sentence overruns or trimming yields the same text, fail.
    """
    sentences = _split_sentences(description)
    if len(sentences) < 2:
        return None
    kept: list[str] = []
    total = 0
    for s in sentences:
        # +1 for the space separator (except first)
        add = len(s) + (1 if kept else 0)
        if total + add > DESCRIPTION_LIMIT:
            break
        kept.append(s)
        total += add
    if not kept or len(kept) == len(sentences):
        return None
    candidate = " ".join(kept).strip()
    return candidate if _acceptable(candidate) else None


def _try_comma_clause_drop(description: str) -> Optional[str]:
    """Drop trailing comma-delimited clauses one at a time until under limit.

    Only operates on the first sentence (anything beyond is sentence_drop
    territory). Floor: stop if drops would push below MIN_LEN.
    """
    sentences = _split_sentences(description)
    if not sentences:
        return None
    first = sentences[0]
    if "," not in first:
        return None
    parts = [p.strip() for p in first.split(",")]
    # Strip clauses from the tail
    while len(parts) > 1:
        parts.pop()
        candidate_first = ", ".join(parts).rstrip(" .,;:")
        if not candidate_first.endswith((".", "!", "?")):
            candidate_first += "."
        # Re-attach following sentences only if they still fit
        rest = sentences[1:]
        candidate = candidate_first
        for s in rest:
            if len(candidate) + 1 + len(s) > DESCRIPTION_LIMIT:
                break
            candidate = candidate + " " + s
        if _acceptable(candidate):
            return candidate
        if len(candidate) < MIN_LEN:
            return None
    return None


# ---------------------------------------------------------------------------
# Hook detection — extends title hooks with description-typical signals
# ---------------------------------------------------------------------------

HOOK_CURRENCY_RE = re.compile(r"[€$£]\s*\d+")
# Numeric-noun hooks tuned for descriptions: scores, tested durations, top N,
# product counts, etc.
HOOK_NUMBER_NOUN_RE = re.compile(
    r"\b(\d+(?:[\.,]\d+)?)\s*("
    # Test duration claims
    r"nights?|nuits?|n[äa]chte?|notti|noches?|"
    r"days?|jours?|tage?|giorni|d[íi]as?|"
    # Score formats like "8.5/10"
    r"/\s*10|"
    # Generic count + noun
    r"flaws?|hidden|reasons?|steps?|tested|tips?|ways?|real|best|"
    r"differences?|defauts?|defects?|pi[èe]ges?|erreurs?|astuces?|conseils?|"
    r"verdades?|errori|trucchi|"
    r"models?|mod[èe]les?|modelli|modelos?|modelle?|"
    r"products?|produits?|prodotti|productos?|produkte?"
    r")\b",
    re.IGNORECASE,
)
# Top-N markers: noun-then-number ("Top 7", "Top 5"). Distinct regex because
# the order is inverted from HOOK_NUMBER_NOUN_RE.
HOOK_TOP_N_RE = re.compile(r"\b(top|meilleurs?|migliori|mejores|beste[nr]?)\s+(\d+)\b", re.IGNORECASE)
# Score patterns: "score 8.5/10", "9.2/10"
HOOK_SCORE_RE = re.compile(r"\b\d+(?:[\.,]\d+)?\s*/\s*10\b")
# Intrigue cues common in descriptions
HOOK_INTRIGUE_RE = re.compile(
    r"\b("
    r"worth\s+[€$£]?\s*\d+|"
    r"cheaper\s+than|"
    r"hidden|revealed|truth|honest|"
    r"que\s+les\s+avis|cachent?|"
    r"buy|shop|price|where\s+to\s+buy|money[-\s]?saving|"
    r"acheter|comprar|kaufen|compra"
    r")\b",
    re.IGNORECASE,
)


def _hooks_in(text: str) -> set:
    """Return the set of hook tuples present in text."""
    if not text:
        return set()
    hooks: set = set()
    for m in HOOK_CURRENCY_RE.finditer(text):
        hooks.add(("currency", m.group(0).replace(" ", "").lower()))
    for m in HOOK_SCORE_RE.finditer(text):
        hooks.add(("score", m.group(0).replace(" ", "").lower()))
    for m in HOOK_NUMBER_NOUN_RE.finditer(text):
        count, noun = m.group(1), m.group(2).lower().rstrip("s").rstrip()
        # Skip scores already captured
        if noun.startswith("/"):
            continue
        hooks.add(("num_noun", f"{count}_{noun}"))
    for m in HOOK_TOP_N_RE.finditer(text):
        noun, count = m.group(1).lower().rstrip("s"), m.group(2)
        hooks.add(("num_noun", f"{count}_{noun}"))
    for m in HOOK_INTRIGUE_RE.finditer(text):
        hooks.add(("intrigue", m.group(0).lower().strip()))
    return hooks


def _hook_loss(original: str, candidate: str) -> set:
    return _hooks_in(original) - _hooks_in(candidate)


def _score_candidate(candidate: str, original: str) -> float:
    """Score a trim candidate. Higher = better."""
    if not candidate:
        return -100.0
    score = 0.0
    n = len(candidate)
    # Prefer trims that use available real estate (closer to limit = more signal)
    score += (n / DESCRIPTION_LIMIT) * 4.0
    # Year preservation
    orig_years = set(re.findall(r"\b20\d{2}\b", original))
    cand_years = set(re.findall(r"\b20\d{2}\b", candidate))
    if orig_years and orig_years.issubset(cand_years):
        score += 2.0
    elif orig_years and not cand_years:
        score -= 3.0
    # Hook preservation
    lost = _hook_loss(original, candidate)
    for h_type, _ in lost:
        if h_type == "currency":
            score -= 3.0
        elif h_type == "score":
            score -= 3.0
        elif h_type == "num_noun":
            score -= 2.0
        else:
            score -= 1.0
    # Bonus: first sentence preserved (heuristic — first 40 chars of original
    # appear at start of candidate, allowing for boilerplate strip at tail).
    orig_first = _split_sentences(original)
    cand_first = _split_sentences(candidate)
    if orig_first and cand_first and orig_first[0] == cand_first[0]:
        score += 1.5
    return round(score, 2)


def _validate_gemini_output(original: str, candidate: str) -> tuple[bool, str]:
    """Reject candidates that hallucinate years, drop the entity, exceed
    length, or replace the first sentence.

    Re-implemented (not imported from agent_title_trimmer) because the
    description check adds a first-sentence preservation requirement that's
    specific to descriptions.
    """
    if not candidate:
        return False, "empty"
    if len(candidate) > DESCRIPTION_LIMIT:
        return False, "over_limit"
    if len(candidate) < MIN_LEN:
        return False, "under_floor"
    # Year preservation: any 4-digit year in input must survive in output.
    in_years = set(re.findall(r"\b(20\d{2})\b", original))
    out_years = set(re.findall(r"\b(20\d{2})\b", candidate))
    if in_years and not in_years & out_years:
        return False, f"year_drift:{in_years}->{out_years}"
    # Entity preservation: first alphabetic token of original must survive.
    in_tokens = re.findall(r"[A-Za-zÀ-ÿ]+", original)
    if in_tokens:
        first = in_tokens[0]
        if first.lower() not in candidate.lower():
            return False, f"entity_lost:{first}"
    # First-sentence subject preservation: the first 3 alphanumeric tokens of
    # the original's first sentence should be present (case-insensitive).
    orig_sentences = _split_sentences(original)
    if orig_sentences:
        first_tokens = re.findall(r"\w+", orig_sentences[0])[:3]
        cand_low = candidate.lower()
        missing = [t for t in first_tokens if t.lower() not in cand_low]
        if first_tokens and len(missing) > 1:
            return False, f"first_sentence_drift:{missing}"
    return True, ""


def _try_gemini_trim(
    description: str,
    locale: str,
    hooks_to_keep: Optional[set] = None,
) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None
    try:
        import urllib.request
    except Exception:
        return None
    locale_names = {"fr": "French", "en": "English", "de": "German",
                    "es": "Spanish", "it": "Italian", "uk": "English"}
    lang = locale_names.get(locale, "English")
    in_years = re.findall(r"\b(20\d{2})\b", description)
    year_constraint = (
        f" The year {in_years[0]} MUST appear verbatim — do not change or omit it."
        if in_years else ""
    )
    hook_phrases: list[str] = []
    if hooks_to_keep:
        for m in HOOK_CURRENCY_RE.finditer(description):
            hook_phrases.append(m.group(0).strip())
        for m in HOOK_SCORE_RE.finditer(description):
            hook_phrases.append(m.group(0).strip())
        for m in HOOK_NUMBER_NOUN_RE.finditer(description):
            hook_phrases.append(m.group(0).strip())
        for m in HOOK_TOP_N_RE.finditer(description):
            hook_phrases.append(m.group(0).strip())
    hook_constraint = ""
    if hook_phrases:
        joined = ", ".join(f'"{h}"' for h in hook_phrases[:5])
        hook_constraint = (
            f" The following claims MUST be preserved verbatim or with minor "
            f"inflection: {joined}."
        )
    prompt = (
        f"Rewrite this SEO meta description to be at most {DESCRIPTION_LIMIT} "
        f"characters while preserving the brand/entity name, the first complete "
        f"sentence, any prices, and any specific testing claims (e.g. '100 nights', "
        f"'score X/10'). Stay in {lang}.{year_constraint}{hook_constraint} "
        f"Reply with ONLY the rewritten description, no quotes or explanation.\n\n"
        f"Original: {description}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0.3},
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
        ok, reason = _validate_gemini_output(description, text)
        if not ok:
            print(f"  ⚠️  Gemini output rejected ({reason}): {text!r}", file=sys.stderr)
            return None
        return text
    except Exception as exc:
        print(f"  ⚠️  Gemini trim failed: {exc}", file=sys.stderr)
        return None


def trim_description(
    description: str,
    locale: str,
    use_gemini: bool = False,
) -> tuple[Optional[str], str, list[str]]:
    """Return (proposed, strategy, rule_chain).

    Self-critic pipeline mirroring `agent_title_trimmer.trim_title`:
      1. Generate all rule-based candidates.
      2. Score each by length proximity to DESCRIPTION_LIMIT minus penalties
         for dropped hooks (currency, score, num+noun) and dropped years.
      3. If best rule loses hooks / drifts year / retains <60% of original
         AND use_gemini=True, also ask Gemini for a candidate with explicit
         hook + first-sentence preservation, then return whichever scores
         higher.
      4. If no rule produces an acceptable trim, try Gemini outright.
    """
    if len(description) <= DESCRIPTION_LIMIT:
        return None, "no_op", []

    chain: list[str] = []
    rule_candidates: list[tuple[str, str, float]] = []

    for name, fn in [
        ("redundant_phrase_strip", _try_redundant_phrase_strip),
        ("arrow_drop", _try_arrow_drop),
        ("sentence_drop", _try_sentence_drop),
        ("comma_clause_drop", _try_comma_clause_drop),
    ]:
        chain.append(name)
        result = fn(description)
        if result:
            rule_candidates.append((name, result, _score_candidate(result, description)))

    best_rule = max(rule_candidates, key=lambda x: x[2]) if rule_candidates else None

    def _should_escalate(cand_text: str) -> bool:
        if _hook_loss(description, cand_text):
            return True
        orig_years = set(re.findall(r"\b20\d{2}\b", description))
        cand_years = set(re.findall(r"\b20\d{2}\b", cand_text))
        if orig_years and not orig_years & cand_years:
            return True
        # Signal-retention floor: trim retaining < 60% of original length when
        # original is long means we're throwing away too much. Escalate.
        if len(description) > 120 and len(cand_text) < len(description) * 0.6:
            return True
        return _score_candidate(cand_text, description) < 2.0

    should_try_gemini = use_gemini and (
        best_rule is None or _should_escalate(best_rule[1])
    )

    gemini_candidate: Optional[tuple[str, float]] = None
    if should_try_gemini:
        chain.append("gemini")
        lost_in_best = (
            _hook_loss(description, best_rule[1]) if best_rule else _hooks_in(description)
        )
        g = _try_gemini_trim(description, locale, hooks_to_keep=lost_in_best)
        if g:
            gemini_candidate = (g, _score_candidate(g, description))

    if best_rule and gemini_candidate:
        if gemini_candidate[1] > best_rule[2]:
            return gemini_candidate[0], "gemini", chain
        return best_rule[1], best_rule[0], chain
    if best_rule:
        return best_rule[1], best_rule[0], chain
    if gemini_candidate:
        return gemini_candidate[0], "gemini", chain
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
    proposals: list[DescriptionProposal] = []
    over_limit_count = 0
    scanned = 0
    for site in sites:
        entries = scan_site(site)
        scanned += len(entries)
        for mdx, locale, description, meta in entries:
            if len(description) <= DESCRIPTION_LIMIT:
                continue
            over_limit_count += 1
            proposed, strategy, chain = trim_description(
                description, locale, use_gemini=use_gemini,
            )
            proposals.append(DescriptionProposal(
                site=site,
                mdx_path=str(mdx),
                locale=locale,
                content_type=meta.get("type", ""),
                current_description=description,
                current_length=len(description),
                proposed_description=proposed,
                proposed_length=len(proposed) if proposed else None,
                strategy=strategy,
                rule_chain=chain,
            ))

    print(f"Scanned {scanned} MDX files across {len(sites)} sites")
    print(f"Found {over_limit_count} descriptions > {DESCRIPTION_LIMIT} chars")
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
            "description_limit": DESCRIPTION_LIMIT,
            "min_length": MIN_LEN,
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
        help=f"Use {GEMINI_MODEL} for descriptions no rule can trim. Costs API.",
    )
    args = parser.parse_args()
    sites = [args.site] if args.site else None
    run(sites=sites, dry_run=args.dry_run, use_gemini=args.gemini_fallback)
    return 0


if __name__ == "__main__":
    sys.exit(main())
