#!/usr/bin/env python3
"""Apply queued title/description trims to MDX files.

Generalises the cohort-3 and cohort-4 throwaway scripts. Reads proposals from
`reports/agent_queues/{title,description}_trim_proposed/{date}.json`, ranks
them by current-week GSC impressions (from the audit JSON), filters out slugs
already applied in previous cohorts (parsed from `reports/ctr-experiments.md`),
and applies the trims as surgical find/replace edits — with strict
sanity-checks so a quote drift never causes a partial edit.

Usage::

    python3 scripts/apply_trim_queue.py \\
      --queue title \\
      --date 2026-05-11 \\
      --top-n 15 \\
      --min-impressions 20 \\
      --audit reports/gsc-full-audit-2026-05-10.json \\
      --exclude-applied reports/ctr-experiments.md \\
      --exclude-sites WeLoveInstant,Brewmance \\
      --dry-run

Edge cases handled:
- title quote styles: double, single, **unquoted** (replacement always emits
  double-quoted output for unquoted matches)
- description block scalars (``description: >-``, ``>``, ``|``, ``|-``):
  skipped with a warning — multi-line scalars are not safely editable with a
  single-line find/replace.
- queue current_title/description doesn't match file verbatim → reported as
  ``mismatch``, file untouched.
- title/description line appearing more than once → reported as ``ambiguous``.
- audit JSON missing pages mentioned by the queue → ranked last (pos=99,
  impr=0) so ``--top-n`` doesn't accidentally promote a zero-traffic page.

Exit codes: 0 on success (even if some entries failed individually), 1 on
catastrophic error (missing inputs, malformed JSON).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

DEFAULT_AUDIT = "reports/gsc-full-audit-2026-05-10.json"
DEFAULT_EXCLUDE_APPLIED = "reports/ctr-experiments.md"
KNOWN_LOCALES = {"en", "de", "es", "it", "uk", "nl", "pt"}
BLOCK_SCALAR_INDICATORS = (">-", ">", "|", "|-")


# ---------------------------------------------------------------------------
# Cohort markdown parser
# ---------------------------------------------------------------------------

# Backticked path-like tokens. Accepts `/en/foo/bar/`, `/comparatif/x/`,
# `/comparatif/x/ (FR)` (parenthetical suffix is allowed and discarded), with
# or without trailing slash, optional `#anchor`.
_BACKTICK_PATH_RE = re.compile(r"`(/[^`\s]+)`")


def parse_applied_slugs(md_path: Path) -> set[str]:
    """Parse a cohort markdown log and return the set of applied slugs.

    The cohort markdown has multiple tables with slightly different schemas
    (cohort 1, 2, 3, 4 each have a different column layout). To stay robust
    we don't parse columns — we extract every backticked path token, strip
    anchors and trailing slashes, and take the last non-empty segment.

    Returns an empty set if the file is missing (with a stderr warning so
    callers don't silently apply over an already-cohorted slug).
    """
    if not md_path.exists():
        print(f"warn: --exclude-applied file not found: {md_path}", file=sys.stderr)
        return set()
    text = md_path.read_text(encoding="utf-8")
    slugs: set[str] = set()
    for match in _BACKTICK_PATH_RE.finditer(text):
        raw = match.group(1)
        # strip anchor
        raw = raw.split("#", 1)[0]
        # strip query
        raw = raw.split("?", 1)[0]
        # normalise trailing slash
        raw = raw.rstrip("/")
        if not raw:
            continue
        segments = [s for s in raw.split("/") if s]
        if not segments:
            continue
        slug = segments[-1]
        # cohort markdown sometimes has trailing parenthetical like ` (FR)`
        # that ends up outside the backticks — we don't need to handle that
        # here because backticks already bound the token.
        slugs.add(slug)
    return slugs


# ---------------------------------------------------------------------------
# URL → (slug, locale) extraction
# ---------------------------------------------------------------------------


def extract_slug_locale(url: str) -> tuple[str, str]:
    """Return (slug, locale) for a GSC page URL.

    Locale defaults to ``fr`` (matches the default-locale-no-prefix convention
    used on matelas/bureau/cafe/aspirateur/pixinstant).
    """
    base = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    parts = [p for p in base.split("/") if p]
    locale = "fr"
    for seg in parts:
        if seg in KNOWN_LOCALES:
            locale = seg
            break
    slug = parts[-1] if parts else ""
    return slug, locale


# ---------------------------------------------------------------------------
# Audit indexing
# ---------------------------------------------------------------------------


@dataclass
class AuditEntry:
    site: str
    url: str
    impressions: int
    position: float


def index_audit(audit: dict, exclude_sites: set[str]) -> dict[tuple[str, str], AuditEntry]:
    """Index audit pages by (slug, locale).

    When the same (slug, locale) appears under multiple URL variants we keep
    the highest-impression entry.
    """
    index: dict[tuple[str, str], AuditEntry] = {}
    for site in audit.get("sites", []) or []:
        name = site.get("name") or ""
        if name in exclude_sites:
            continue
        for page in site.get("current", {}).get("pages", []) or []:
            url = page.get("page")
            if not url:
                continue
            slug, locale = extract_slug_locale(url)
            if not slug:
                continue
            key = (slug, locale)
            entry = AuditEntry(
                site=name,
                url=url,
                impressions=int(page.get("impressions") or 0),
                position=float(page.get("position") or 0.0),
            )
            cur = index.get(key)
            if cur is None or entry.impressions > cur.impressions:
                index[key] = entry
    return index


# ---------------------------------------------------------------------------
# Candidate ranking
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    proposal: dict
    slug: str
    locale: str
    impressions: int
    position: float
    site: str
    excluded_reason: str | None = None


def _proposal_slug(proposal: dict) -> str:
    return Path(proposal.get("mdx_path", "")).stem


def build_candidates(
    proposals: Iterable[dict],
    audit_index: dict[tuple[str, str], AuditEntry],
    *,
    queue: str,
    excluded_slugs: set[str],
    excluded_sites: set[str],  # audit names: Brewmance, WeLoveInstant
    excluded_site_keys: set[str],  # queue-side names: brewmance, weloveinstant
    allowed_strategies: set[str] | None,
    min_impressions: int,
) -> list[Candidate]:
    proposed_field = "proposed_title" if queue == "title" else "proposed_description"
    candidates: list[Candidate] = []
    for p in proposals:
        if not p.get(proposed_field):
            continue
        strategy = p.get("strategy", "")
        if strategy == "no_strategy_worked":
            continue
        if allowed_strategies is not None and strategy not in allowed_strategies:
            continue
        slug = _proposal_slug(p)
        if not slug:
            continue
        locale = p.get("locale") or "fr"
        site_key = (p.get("site") or "").lower()
        if site_key in excluded_site_keys:
            continue
        audit = audit_index.get((slug, locale))
        impr = audit.impressions if audit else 0
        pos = audit.position if audit else 99.0
        site_name = audit.site if audit else (p.get("site") or "?")
        if site_name in excluded_sites:
            continue
        excluded_reason = None
        if slug in excluded_slugs:
            excluded_reason = "already-applied"
        elif impr < min_impressions:
            excluded_reason = f"impr<{min_impressions}"
        candidates.append(
            Candidate(
                proposal=p,
                slug=slug,
                locale=locale,
                impressions=impr,
                position=pos,
                site=site_name,
                excluded_reason=excluded_reason,
            )
        )

    # Dedupe by (slug, locale, mdx_path): same mdx may appear once per locale
    # already, but a safety net never hurts. Keep highest-impression.
    keyed: dict[tuple[str, str, str], Candidate] = {}
    for c in candidates:
        key = (c.slug, c.locale, c.proposal.get("mdx_path", ""))
        cur = keyed.get(key)
        if cur is None or c.impressions > cur.impressions:
            keyed[key] = c
    # Sort: include-then-exclude, by impressions desc, then position asc
    return sorted(
        keyed.values(),
        key=lambda c: (c.excluded_reason is not None, -c.impressions, c.position),
    )


# ---------------------------------------------------------------------------
# YAML frontmatter edit
# ---------------------------------------------------------------------------


@dataclass
class EditResult:
    ok: bool
    reason: str = ""  # error reason if not ok
    warning: str = ""  # non-fatal note (e.g. nothing to do in dry-run)


# Per-style matchers. Each returns (matched_line, new_line) or None.
def _build_title_replacement(text: str, cur: str, new: str) -> tuple[str, str] | tuple[None, str]:
    """Find the title line and build its replacement.

    Returns (matched, replacement) on success; (None, reason) on failure.
    """
    return _build_frontmatter_replacement(text, "title", cur, new)


def _build_description_replacement(
    text: str, cur: str, new: str
) -> tuple[str, str] | tuple[None, str]:
    return _build_frontmatter_replacement(text, "description", cur, new)


def _is_block_scalar(text: str, field: str) -> bool:
    """True if the field uses a YAML block scalar (multi-line)."""
    # Match e.g. `description: >-` at start of a line.
    pattern = re.compile(rf"(?m)^{re.escape(field)}:\s*([>|][-+]?)\s*$")
    return pattern.search(text) is not None


def _build_frontmatter_replacement(
    text: str, field: str, cur: str, new: str
) -> tuple[str, str] | tuple[None, str]:
    """Find the field line and build its replacement.

    Tries double-quoted, single-quoted, then unquoted. Returns (matched_line,
    replacement_line) on success. On failure returns (None, reason).
    """
    if _is_block_scalar(text, field):
        return None, "block-scalar"

    # 1. Double-quoted: `field: "..."`
    dbl = f'{field}: "{cur}"'
    if dbl in text:
        if text.count(dbl) > 1:
            return None, f"ambiguous-double ({text.count(dbl)}x)"
        esc = new.replace("\\", "\\\\").replace('"', '\\"')
        return dbl, f'{field}: "{esc}"'

    # 2. Single-quoted: `field: '...'`
    sgl = f"{field}: '{cur}'"
    if sgl in text:
        if text.count(sgl) > 1:
            return None, f"ambiguous-single ({text.count(sgl)}x)"
        # YAML single-quote escape doubles the quote character
        if "'" in new:
            esc = new.replace("'", "''")
            return sgl, f"{field}: '{esc}'"
        return sgl, f"{field}: '{new}'"

    # 3. Unquoted: match the WHOLE line. Anchor on \n so we don't catch a
    # substring of a quoted line.
    unquoted_re = re.compile(
        rf"(?m)^{re.escape(field)}:\s+{re.escape(cur)}\s*$"
    )
    matches = unquoted_re.findall(text)
    if matches:
        if len(matches) > 1:
            return None, f"ambiguous-unquoted ({len(matches)}x)"
        # The actual matched line — re-find to get exact whitespace
        m = unquoted_re.search(text)
        assert m is not None
        matched_line = m.group(0)
        # Always promote to double-quoted on replacement
        esc = new.replace("\\", "\\\\").replace('"', '\\"')
        return matched_line, f'{field}: "{esc}"'

    return None, "mismatch"


def apply_edit(path: Path, field: str, cur: str, new: str, *, dry_run: bool) -> EditResult:
    if not path.exists():
        return EditResult(False, "file-missing")
    text = path.read_text(encoding="utf-8")
    matched, replacement_or_reason = _build_frontmatter_replacement(text, field, cur, new)
    if matched is None:
        return EditResult(False, replacement_or_reason)
    if dry_run:
        return EditResult(True)
    new_text = text.replace(matched, replacement_or_reason, 1)
    if new_text == text:
        return EditResult(False, "noop-after-replace")
    path.write_text(new_text, encoding="utf-8")
    return EditResult(True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent


def _short_path(p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(REPO_ROOT))
    except (ValueError, OSError):
        return p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply queued title/description trims to MDX files.",
    )
    parser.add_argument("--queue", choices=("title", "description"), required=True)
    parser.add_argument(
        "--date",
        default=None,
        help="Queue date (default: latest file in the queue dir).",
    )
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument("--min-impressions", type=int, default=20)
    parser.add_argument(
        "--audit",
        default=str(REPO_ROOT / DEFAULT_AUDIT),
        help=f"Path to gsc-full-audit JSON (default: {DEFAULT_AUDIT}).",
    )
    parser.add_argument(
        "--exclude-applied",
        default=str(REPO_ROOT / DEFAULT_EXCLUDE_APPLIED),
        help=f"Cohort markdown to skip already-applied slugs (default: {DEFAULT_EXCLUDE_APPLIED}).",
    )
    parser.add_argument(
        "--exclude-sites",
        default="",
        help="Comma-separated audit-site names to skip (e.g. Brewmance,WeLoveInstant).",
    )
    parser.add_argument(
        "--strategies",
        default=None,
        help=(
            "Comma-separated strategies to allow. "
            "Default: all proposals with a non-null proposed_* (i.e. exclude no_strategy_worked)."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--queue-dir", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # Resolve queue file
    queue_dir = Path(args.queue_dir) if args.queue_dir else (
        REPO_ROOT / "reports" / "agent_queues" / f"{args.queue}_trim_proposed"
    )
    if not queue_dir.is_dir():
        print(f"error: queue dir not found: {queue_dir}", file=sys.stderr)
        return 1
    if args.date:
        queue_file = queue_dir / f"{args.date}.json"
    else:
        candidates = sorted(queue_dir.glob("*.json"))
        if not candidates:
            print(f"error: no queue files in {queue_dir}", file=sys.stderr)
            return 1
        queue_file = candidates[-1]
    if not queue_file.exists():
        print(f"error: queue file not found: {queue_file}", file=sys.stderr)
        return 1

    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    proposals = queue.get("proposals") or []

    # Load audit
    audit_path = Path(args.audit)
    if not audit_path.is_absolute():
        audit_path = REPO_ROOT / audit_path
    if not audit_path.exists():
        print(f"error: audit file not found: {audit_path}", file=sys.stderr)
        return 1
    audit = json.loads(audit_path.read_text(encoding="utf-8"))

    # Exclusions
    excluded_audit_sites: set[str] = set()
    excluded_queue_sites: set[str] = set()
    for s in (args.exclude_sites or "").split(","):
        s = s.strip()
        if not s:
            continue
        excluded_audit_sites.add(s)
        excluded_queue_sites.add(s.lower())

    excluded_applied = (
        parse_applied_slugs(Path(args.exclude_applied)) if args.exclude_applied else set()
    )

    audit_index = index_audit(audit, excluded_audit_sites)

    allowed_strategies: set[str] | None = None
    if args.strategies:
        allowed_strategies = {s.strip() for s in args.strategies.split(",") if s.strip()}

    candidates = build_candidates(
        proposals,
        audit_index,
        queue=args.queue,
        excluded_slugs=excluded_applied,
        excluded_sites=excluded_audit_sites,
        excluded_site_keys=excluded_queue_sites,
        allowed_strategies=allowed_strategies,
        min_impressions=args.min_impressions,
    )

    # Split: includes (no exclusion) vs excludes (filtered out by flag/threshold)
    includes = [c for c in candidates if c.excluded_reason is None][: args.top_n]
    excludes = [c for c in candidates if c.excluded_reason is not None]

    field = "title" if args.queue == "title" else "description"
    cur_field = f"current_{field}"
    new_field = f"proposed_{field}"

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"[{mode}] queue={args.queue} date={queue_file.stem} top_n={args.top_n} "
          f"min_impressions={args.min_impressions}")
    print(f"[{mode}] candidates: {len(includes)} to apply, {len(excludes)} excluded by filters")
    print()

    applied_count = 0
    failed_count = 0
    skipped_count = len(excludes)
    failures: list[tuple[str, str]] = []

    for i, c in enumerate(includes, 1):
        p = c.proposal
        mdx = Path(p.get("mdx_path", ""))
        cur = p.get(cur_field) or ""
        new = p.get(new_field) or ""
        short = _short_path(str(mdx))
        head = (
            f"{i:>2}. [{c.site:<14} {c.locale:<3} {p.get('strategy',''):<14} "
            f"impr={c.impressions:>4} pos={c.position:>5.1f}] {short}"
        )
        if args.dry_run:
            print(head)
            print(f"     cur: {cur}")
            print(f"     new: {new}")
            # Still run the matcher in dry-run so we surface mismatches early.
            res = apply_edit(mdx, field, cur, new, dry_run=True)
            if not res.ok:
                print(f"     [would FAIL: {res.reason}]")
                failed_count += 1
            continue
        res = apply_edit(mdx, field, cur, new, dry_run=False)
        if res.ok:
            applied_count += 1
            print(f"OK {head}")
            print(f"     {cur}")
            print(f"  -> {new}")
        else:
            failed_count += 1
            failures.append((short, res.reason))
            print(f"XX {head}")
            print(f"     reason: {res.reason}")

    if args.dry_run:
        print()
        print(f"[DRY-RUN] would apply: {len(includes) - failed_count}, "
              f"would fail: {failed_count}, "
              f"excluded by filters: {skipped_count}")
        return 0

    print()
    print(f"Applied: {applied_count}, Failed: {failed_count}, "
          f"Skipped (excluded): {skipped_count}")
    if failures:
        print("\nFailures:")
        for path, reason in failures:
            print(f"  - {path}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
