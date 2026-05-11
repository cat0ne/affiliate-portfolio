#!/usr/bin/env python3
"""Measure the CTR-experiment cohort at T+14 / T+28 vs the pre-change baseline.

Reads:
    --baseline   pre-change GSC full-audit JSON (snapshot taken on 2026-05-10)
    --current    post-change GSC full-audit JSON
    --cohort     reports/ctr-experiments.md (21 URLs across two tables)

Writes a markdown report to --out (default
reports/ctr-cohort-measurement-{date}.md) containing:

    * per-URL Δclicks / Δimpressions / ΔCTR (abs and %) / Δposition
    * rank-controlled ΔCTR using the Backlinko Q4 2025 position-CTR curve
      imported from scripts/agent_ctr_optimizer.py:EXPECTED_CTR_BY_POSITION
    * pass/fail vs the +15% relative-lift kill criterion
    * cohort-wide median rank-controlled ΔCTR, N pass / N fail, breakdown
      by change_type (CTR-variant / trim-rule / trim-gemini)

Notes on units (silent-bug bait):
    GSC JSON `ctr` is **percent** (e.g. 8.33 means 8.33%). The Backlinko
    curve is **fraction** (e.g. 0.080). We convert GSC ctr to fraction at
    parse time and work in fractions everywhere internally; the markdown
    report displays percent for human readability.

Pass/fail interpretation:
    The cohort markdown says "median rank-controlled ΔCTR ≥ +15%". With
    baselines in the 1-10% CTR range, 15 percentage points is unrealistic;
    we interpret this as +15% **relative** lift over baseline CTR
    (rank_controlled_dCTR / baseline_ctr >= 0.15). Reported as both
    absolute (percentage points) and relative for transparency.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Import EXPECTED_CTR_BY_POSITION from the optimizer. Import has filesystem
# side effects (creates Hermes dirs); idempotent and harmless here.
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agent_ctr_optimizer import EXPECTED_CTR_BY_POSITION  # noqa: E402

# Site slug (used in cohort markdown) -> GSC audit site name (the `name`
# field in the JSON `sites` array).
SITE_SLUG_TO_NAME: dict[str, str] = {
    "aspirateur": "Top-Aspirateur",
    "bureau": "Bureau-Expert",
    "matelas": "Matelas",
    "cafe": "Brewmance",
    "pixinstant": "PixInstant",
}

KILL_CRITERION_RELATIVE = 0.15  # +15% relative lift over baseline CTR
EXPECTED_COHORT_SIZE = 21


# ---------------------------------------------------------------------------
# Position curve helper (do not import the underscored helper from the
# optimizer; reimplement so the contract is local and testable).
# ---------------------------------------------------------------------------

def expected_ctr(position: float) -> float:
    """Backlinko Q4 2025 expected CTR for a given SERP position.

    Positions are floats in GSC JSON (e.g. 6.6). Round to int, then look up
    or fall back to the next-higher known bucket. Out-of-range returns the
    deepest bucket's value.
    """
    if position is None or position <= 0:
        return 0.0
    pos_int = max(1, int(round(position)))
    if pos_int in EXPECTED_CTR_BY_POSITION:
        return EXPECTED_CTR_BY_POSITION[pos_int]
    keys = sorted(EXPECTED_CTR_BY_POSITION.keys())
    for k in keys:
        if k >= pos_int:
            return EXPECTED_CTR_BY_POSITION[k]
    return EXPECTED_CTR_BY_POSITION[max(keys)]


# ---------------------------------------------------------------------------
# Cohort markdown parser
# ---------------------------------------------------------------------------

@dataclass
class CohortEntry:
    site_slug: str           # e.g. "matelas"
    path: str                # e.g. "/en/avis/tediber/"
    change_type_full: str    # e.g. "trim-rule (pipe_drop)"
    change_type_base: str    # one of CTR-variant | trim-rule | trim-gemini
    pre_impr_7d: Optional[float]
    pre_position: Optional[float]
    old_title: str
    new_title: str
    cohort_label: str        # "cohort 1" or "cohort 2"


# Pull the first backticked token from a cell (the path).
_PATH_RE = re.compile(r"`([^`]+)`")
# Trailing " (FR)" / " (EN)" / etc. annotation after the backticks — already
# excluded by parsing only the backticked content. Kept here as documentation.

# Change type base detection — order matters: CTR-variant before trim-* so
# "CTR-variant" doesn't get confused with anything else.
_CHANGE_TYPE_PATTERNS = [
    ("CTR-variant", re.compile(r"\bCTR-variant\b", re.IGNORECASE)),
    ("trim-rule", re.compile(r"\btrim-rule\b", re.IGNORECASE)),
    ("trim-gemini", re.compile(r"\btrim-gemini\b", re.IGNORECASE)),
]


def _normalize_change_type(raw: str) -> str:
    for base, pat in _CHANGE_TYPE_PATTERNS:
        if pat.search(raw):
            return base
    return raw.strip() or "unknown"


def _parse_float(cell: str) -> Optional[float]:
    """Pull the first float out of a cell. Returns None if nothing parseable."""
    m = re.search(r"[-+]?\d*\.?\d+", cell)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _first_backtick(cell: str) -> str:
    m = _PATH_RE.search(cell)
    return m.group(1).strip() if m else cell.strip()


def parse_cohort_markdown(path: Path) -> list[CohortEntry]:
    """Parse both cohort tables out of reports/ctr-experiments.md.

    The tables share the column structure:
        | Site | Page | Change type | Pre impr/7d | Pre pos | Old title (len) | New title (len) |
    """
    text = path.read_text(encoding="utf-8")
    entries: list[CohortEntry] = []
    cohort_label = ""
    in_table = False
    header_seen = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        # Track which cohort heading we're under. Note: "cohort 2026" also
        # contains the substring "cohort 2"; match parenthetical explicitly.
        if line.startswith("## Cohort 2026-05-11"):
            if re.search(r"\(cohort\s*2\b", line, re.IGNORECASE):
                cohort_label = "cohort 2"
            else:
                cohort_label = "cohort 1"
            in_table = False
            header_seen = False
            continue

        # Any other H2 ends the current table region.
        if line.startswith("## ") and not line.startswith("## Cohort"):
            in_table = False
            header_seen = False
            continue

        if not cohort_label:
            continue

        if not line.startswith("|"):
            # Blank line inside a cohort section is fine, but signals
            # end-of-table when we were in one.
            if in_table and not line.strip():
                in_table = False
                header_seen = False
            continue

        # Pipe-delimited row.
        cells = [c.strip() for c in line.strip().strip("|").split("|")]

        # Skip the header row.
        if not header_seen and "Site" in cells[0]:
            header_seen = True
            continue
        # Skip the separator row (--- | --- | ...).
        if cells[0].startswith("-") or set(cells[0]) <= set("- "):
            in_table = True
            continue

        if len(cells) < 7:
            continue

        site_slug = cells[0].strip()
        path_cell = cells[1]
        change_type_full = cells[2]
        pre_impr_cell = cells[3]
        pre_pos_cell = cells[4]
        old_title_cell = cells[5]
        new_title_cell = cells[6]

        cohort_path = _first_backtick(path_cell)
        old_title = _first_backtick(old_title_cell)
        new_title = _first_backtick(new_title_cell)

        entries.append(CohortEntry(
            site_slug=site_slug,
            path=cohort_path,
            change_type_full=change_type_full,
            change_type_base=_normalize_change_type(change_type_full),
            pre_impr_7d=_parse_float(pre_impr_cell),
            pre_position=_parse_float(pre_pos_cell),
            old_title=old_title,
            new_title=new_title,
            cohort_label=cohort_label,
        ))

    return entries


# ---------------------------------------------------------------------------
# GSC audit reader
# ---------------------------------------------------------------------------

@dataclass
class PageMetrics:
    clicks: int
    impressions: int
    ctr: float       # FRACTION (e.g. 0.0833), normalized from JSON percent
    position: float


def _norm_path(url_or_path: str) -> str:
    """Return the path portion of a URL with a single trailing slash semantic.

    Both full URLs (https://...) and bare paths are accepted. Querystring &
    fragment are stripped. Trailing slash is preserved as-is so we can match
    cohort entries that include or omit it consistently.
    """
    if not url_or_path:
        return ""
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        p = urlparse(url_or_path).path
    else:
        p = url_or_path.split("#", 1)[0].split("?", 1)[0]
    return p or "/"


def _paths_equal(a: str, b: str) -> bool:
    """Compare two paths tolerating a single trailing slash difference."""
    if a == b:
        return True
    return a.rstrip("/") == b.rstrip("/")


def load_gsc_audit(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def lookup_page(audit: dict[str, Any], site_slug: str, cohort_path: str) -> Optional[PageMetrics]:
    """Find a cohort page in the GSC audit JSON and return normalized metrics.

    Looks first in the site's `current.pages` (full top-pages list); falls
    back to the top-level `low_ctr_pages` if not found.
    """
    site_name = SITE_SLUG_TO_NAME.get(site_slug)
    if not site_name:
        return None

    sites_by_name = {s.get("name"): s for s in audit.get("sites", [])}
    site_obj = sites_by_name.get(site_name)

    # 1. Primary lookup: site.current.pages
    if site_obj:
        for page in site_obj.get("current", {}).get("pages", []) or []:
            page_path = _norm_path(page.get("page", ""))
            if not _paths_equal(page_path, cohort_path):
                continue
            # Skip anchored variants (`page#section`) — they shouldn't have
            # a clean path equal to cohort_path anyway after _norm_path, but
            # double check.
            if "#" in (page.get("page") or ""):
                continue
            return PageMetrics(
                clicks=int(page.get("clicks") or 0),
                impressions=int(page.get("impressions") or 0),
                # GSC JSON `ctr` is percent — convert to fraction.
                ctr=float(page.get("ctr") or 0) / 100.0,
                position=float(page.get("position") or 0),
            )

    # 2. Fallback: top-level low_ctr_pages (no clicks/ctr there — derive 0)
    for lcp in audit.get("low_ctr_pages", []) or []:
        if lcp.get("site") != site_name:
            continue
        page_path = _norm_path(lcp.get("page", ""))
        if _paths_equal(page_path, cohort_path):
            impressions = int(lcp.get("impressions") or 0)
            return PageMetrics(
                clicks=0,
                impressions=impressions,
                ctr=0.0,
                position=float(lcp.get("position") or 0),
            )

    return None


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

@dataclass
class RowResult:
    entry: CohortEntry
    baseline: Optional[PageMetrics]
    current: Optional[PageMetrics]
    missing: bool = False

    # Computed fields (only meaningful when both baseline and current exist)
    d_clicks: Optional[int] = None
    d_impressions: Optional[int] = None
    d_ctr_abs: Optional[float] = None        # fraction points (e.g. 0.012 = 1.2pp)
    d_ctr_relative: Optional[float] = None   # relative to baseline_ctr
    d_position: Optional[float] = None
    rank_controlled_d_ctr: Optional[float] = None       # absolute fraction
    rank_controlled_d_ctr_rel: Optional[float] = None   # relative to baseline_ctr
    pass_threshold: Optional[bool] = None    # vs +15% relative lift
    notes: list[str] = field(default_factory=list)


def compute_row(entry: CohortEntry, baseline: Optional[PageMetrics], current: Optional[PageMetrics]) -> RowResult:
    row = RowResult(entry=entry, baseline=baseline, current=current)

    if baseline is None:
        # Baseline missing — we can't measure. Flag separately from MISSING
        # (which is reserved for current=None per the spec).
        row.notes.append("NO-BASELINE")
        # Per spec, MISSING flag only fires on current=None.
        if current is None:
            row.missing = True
            row.notes.append("MISSING")
        return row

    if current is None:
        row.missing = True
        row.notes.append("MISSING")
        return row

    row.d_clicks = current.clicks - baseline.clicks
    row.d_impressions = current.impressions - baseline.impressions
    row.d_ctr_abs = current.ctr - baseline.ctr
    row.d_position = current.position - baseline.position

    if current.impressions == 0:
        row.notes.append("ZERO-CURRENT-IMPR")

    # Rank-controlled ΔCTR
    expected_baseline = expected_ctr(baseline.position)
    expected_current = expected_ctr(current.position)
    expected_delta = expected_current - expected_baseline
    row.rank_controlled_d_ctr = row.d_ctr_abs - expected_delta

    # Relative lifts (vs baseline CTR)
    if baseline.ctr > 0:
        row.d_ctr_relative = row.d_ctr_abs / baseline.ctr
        row.rank_controlled_d_ctr_rel = row.rank_controlled_d_ctr / baseline.ctr
        row.pass_threshold = row.rank_controlled_d_ctr_rel >= KILL_CRITERION_RELATIVE
    else:
        # Baseline CTR == 0 — can't compute relative lift; exclude from
        # pass/fail and from the median calculation.
        row.notes.append("ZERO-BASELINE-CTR")

    return row


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _fmt_float(v: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}{suffix}"


def _fmt_pct(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{digits}f}%"


def _fmt_int(v: Optional[int]) -> str:
    if v is None:
        return "—"
    return f"{v:+d}" if v != 0 else "0"


def render_report(
    rows: list[RowResult],
    baseline_path: Path,
    current_path: Path,
    cohort_path: Path,
) -> str:
    lines: list[str] = []
    lines.append("# CTR cohort measurement")
    lines.append("")
    lines.append(f"- Baseline: `{baseline_path}`")
    lines.append(f"- Current:  `{current_path}`")
    lines.append(f"- Cohort:   `{cohort_path}`")
    lines.append(f"- Kill criterion: rank-controlled ΔCTR ≥ +{int(KILL_CRITERION_RELATIVE * 100)}% relative to baseline CTR")
    lines.append("")
    lines.append("## Per-URL results")
    lines.append("")
    lines.append(
        "| Site | Path | Change | Base clicks | Cur clicks | Δclicks | "
        "Base impr | Cur impr | Δimpr | Base CTR | Cur CTR | ΔCTR (pp) | "
        "ΔCTR (rel) | Base pos | Cur pos | Δpos | Rank-ctl ΔCTR (pp) | "
        "Rank-ctl ΔCTR (rel) | Pass? | Notes |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for r in rows:
        e = r.entry
        b = r.baseline
        c = r.current
        base_clicks = b.clicks if b else None
        cur_clicks = c.clicks if c else None
        base_impr = b.impressions if b else None
        cur_impr = c.impressions if c else None
        base_ctr = b.ctr if b else None
        cur_ctr = c.ctr if c else None
        base_pos = b.position if b else None
        cur_pos = c.position if c else None

        if r.pass_threshold is None:
            pass_str = "N/A"
        else:
            pass_str = "PASS" if r.pass_threshold else "FAIL"

        d_ctr_abs_pp = r.d_ctr_abs * 100 if r.d_ctr_abs is not None else None
        rank_ctl_pp = r.rank_controlled_d_ctr * 100 if r.rank_controlled_d_ctr is not None else None

        lines.append(
            "| {site} | `{path}` | {change} | {bc} | {cc} | {dc} | "
            "{bi} | {ci} | {di} | {bctr} | {cctr} | {dctr_pp} | "
            "{dctr_rel} | {bp} | {cp} | {dp} | {rctl_pp} | {rctl_rel} | {p} | {n} |".format(
                site=e.site_slug,
                path=e.path,
                change=e.change_type_full,
                bc=("—" if base_clicks is None else base_clicks),
                cc=("—" if cur_clicks is None else cur_clicks),
                dc=_fmt_int(r.d_clicks),
                bi=("—" if base_impr is None else base_impr),
                ci=("—" if cur_impr is None else cur_impr),
                di=_fmt_int(r.d_impressions),
                bctr=_fmt_pct(base_ctr),
                cctr=_fmt_pct(cur_ctr),
                dctr_pp=_fmt_float(d_ctr_abs_pp, 2, "pp"),
                dctr_rel=_fmt_pct(r.d_ctr_relative),
                bp=_fmt_float(base_pos, 1),
                cp=_fmt_float(cur_pos, 1),
                dp=_fmt_float(r.d_position, 1),
                rctl_pp=_fmt_float(rank_ctl_pp, 2, "pp"),
                rctl_rel=_fmt_pct(r.rank_controlled_d_ctr_rel),
                p=pass_str,
                n=", ".join(r.notes) if r.notes else "",
            )
        )

    # Aggregates
    rel_lifts = [r.rank_controlled_d_ctr_rel for r in rows if r.rank_controlled_d_ctr_rel is not None]
    n_pass = sum(1 for r in rows if r.pass_threshold is True)
    n_fail = sum(1 for r in rows if r.pass_threshold is False)
    n_na = len(rows) - n_pass - n_fail
    n_missing = sum(1 for r in rows if r.missing)

    lines.append("")
    lines.append("## Cohort aggregates")
    lines.append("")
    lines.append(f"- N total: **{len(rows)}**")
    lines.append(f"- N pass (≥ +{int(KILL_CRITERION_RELATIVE * 100)}% rank-controlled): **{n_pass}**")
    lines.append(f"- N fail: **{n_fail}**")
    lines.append(f"- N N/A (no baseline CTR / missing): **{n_na}** (incl. {n_missing} MISSING)")
    if rel_lifts:
        med_rel = median(rel_lifts)
        lines.append(f"- Median rank-controlled ΔCTR (relative): **{med_rel * 100:+.2f}%**")
        verdict = "PASS" if med_rel >= KILL_CRITERION_RELATIVE else "FAIL"
        lines.append(f"- Kill-criterion verdict (median ≥ +{int(KILL_CRITERION_RELATIVE * 100)}%): **{verdict}**")
    else:
        lines.append("- Median rank-controlled ΔCTR: **N/A** (no measurable rows)")

    # Per-change-type breakdown
    lines.append("")
    lines.append("## Per change-type breakdown")
    lines.append("")
    lines.append("| Change type | N | N pass | N fail | Median rank-ctl ΔCTR (rel) |")
    lines.append("|---|---|---|---|---|")
    for ct_base in ["CTR-variant", "trim-rule", "trim-gemini"]:
        subset = [r for r in rows if r.entry.change_type_base == ct_base]
        if not subset:
            continue
        sub_lifts = [r.rank_controlled_d_ctr_rel for r in subset if r.rank_controlled_d_ctr_rel is not None]
        sub_pass = sum(1 for r in subset if r.pass_threshold is True)
        sub_fail = sum(1 for r in subset if r.pass_threshold is False)
        med_str = f"{median(sub_lifts) * 100:+.2f}%" if sub_lifts else "—"
        lines.append(f"| {ct_base} | {len(subset)} | {sub_pass} | {sub_fail} | {med_str} |")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_out_path() -> Path:
    today = date.today().isoformat()
    return Path("/Users/gho/Documents/affiliation-sites/reports") / f"ctr-cohort-measurement-{today}.md"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Measure CTR-experiment cohort vs baseline.")
    p.add_argument("--baseline", type=Path,
                   default=Path("/Users/gho/Documents/affiliation-sites/reports/gsc-full-audit-2026-05-10.json"),
                   help="Pre-change GSC full-audit JSON.")
    p.add_argument("--current", type=Path,
                   help="Post-change GSC full-audit JSON (required unless --dry-run).")
    p.add_argument("--cohort", type=Path,
                   default=Path("/Users/gho/Documents/affiliation-sites/reports/ctr-experiments.md"),
                   help="Cohort markdown.")
    p.add_argument("--out", type=Path, default=None,
                   help="Output report path (default reports/ctr-cohort-measurement-{today}.md).")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse the cohort and print URLs without computing diffs.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    cohort = parse_cohort_markdown(args.cohort)

    if len(cohort) != EXPECTED_COHORT_SIZE:
        print(
            f"WARN: parsed {len(cohort)} cohort entries (expected {EXPECTED_COHORT_SIZE}).",
            file=sys.stderr,
        )

    if args.dry_run:
        print(f"# Parsed cohort entries: {len(cohort)}")
        for i, e in enumerate(cohort, 1):
            print(f"{i:>2}. [{e.cohort_label}] {e.site_slug:<11} {e.change_type_base:<11} {e.path}")
        # Also break down by change_type for sanity.
        by_ct: dict[str, int] = {}
        for e in cohort:
            by_ct[e.change_type_base] = by_ct.get(e.change_type_base, 0) + 1
        print("\n# By change_type:")
        for k, v in sorted(by_ct.items()):
            print(f"  {k}: {v}")
        return 0

    if args.current is None:
        print("ERROR: --current is required (unless --dry-run).", file=sys.stderr)
        return 2

    out_path = args.out or _default_out_path()

    baseline_audit = load_gsc_audit(args.baseline)
    current_audit = load_gsc_audit(args.current)

    rows: list[RowResult] = []
    for entry in cohort:
        b = lookup_page(baseline_audit, entry.site_slug, entry.path)
        c = lookup_page(current_audit, entry.site_slug, entry.path)
        rows.append(compute_row(entry, b, c))

    report = render_report(rows, args.baseline, args.current, args.cohort)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"Wrote {out_path} ({len(rows)} rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
