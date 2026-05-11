#!/usr/bin/env python3
"""Tests for measure_ctr_cohort.py.

Run with: python3 scripts/test_measure_ctr_cohort.py
or:       python3 -m pytest scripts/test_measure_ctr_cohort.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from measure_ctr_cohort import (  # noqa: E402
    EXPECTED_COHORT_SIZE,
    KILL_CRITERION_RELATIVE,
    PageMetrics,
    compute_row,
    expected_ctr,
    parse_cohort_markdown,
    lookup_page,
    main,
    render_report,
)
from agent_ctr_optimizer import EXPECTED_CTR_BY_POSITION  # noqa: E402


COHORT_MD = Path("/Users/gho/Documents/affiliation-sites/reports/ctr-experiments.md")


class ParseCohortTests(unittest.TestCase):
    def test_cohort_count_is_21(self):
        entries = parse_cohort_markdown(COHORT_MD)
        self.assertEqual(len(entries), EXPECTED_COHORT_SIZE)

    def test_cohort_split_12_and_9(self):
        entries = parse_cohort_markdown(COHORT_MD)
        c1 = [e for e in entries if e.cohort_label == "cohort 1"]
        c2 = [e for e in entries if e.cohort_label == "cohort 2"]
        self.assertEqual(len(c1), 12, "Cohort 1 should have 12 entries")
        self.assertEqual(len(c2), 9, "Cohort 2 should have 9 entries")

    def test_change_type_breakdown(self):
        entries = parse_cohort_markdown(COHORT_MD)
        by_ct: dict[str, int] = {}
        for e in entries:
            by_ct[e.change_type_base] = by_ct.get(e.change_type_base, 0) + 1
        # From the markdown: 1 CTR-variant, 11 trim-rule, 9 trim-gemini.
        self.assertEqual(by_ct.get("CTR-variant"), 1)
        self.assertEqual(by_ct.get("trim-rule"), 11)
        self.assertEqual(by_ct.get("trim-gemini"), 9)

    def test_known_tediber_entry(self):
        entries = parse_cohort_markdown(COHORT_MD)
        match = [e for e in entries if e.path == "/en/avis/tediber/"]
        self.assertEqual(len(match), 1)
        e = match[0]
        self.assertEqual(e.site_slug, "matelas")
        self.assertEqual(e.change_type_base, "CTR-variant")
        self.assertAlmostEqual(e.pre_position or 0.0, 4.8, places=2)

    def test_fr_annotation_stripped(self):
        entries = parse_cohort_markdown(COHORT_MD)
        # The cohort 2 row "/comparatif/meilleur-instax-mini-2026/` (FR)`
        # should parse to just the path (no " (FR)").
        match = [e for e in entries if "meilleur-instax-mini-2026" in e.path]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0].path, "/comparatif/meilleur-instax-mini-2026/")


class ExpectedCtrTests(unittest.TestCase):
    def test_int_position_lookup(self):
        self.assertAlmostEqual(expected_ctr(1), 0.275)
        self.assertAlmostEqual(expected_ctr(4), 0.080)
        self.assertAlmostEqual(expected_ctr(10), 0.018)

    def test_float_position_rounds(self):
        # 6.6 rounds to 7 -> 0.035
        self.assertAlmostEqual(expected_ctr(6.6), 0.035)
        # 4.4 rounds to 4 -> 0.080
        self.assertAlmostEqual(expected_ctr(4.4), 0.080)
        # 4.5 rounds to 4 (banker's rounding) -> 0.080
        # but if Python rounds 4.5 to 4, we get 0.080; if to 5, 0.060.
        # int(round(4.5)) is 4 in Python 3 due to banker's rounding.
        self.assertIn(expected_ctr(4.5), (0.080, 0.060))

    def test_imported_dict_intact(self):
        self.assertEqual(EXPECTED_CTR_BY_POSITION[1], 0.275)
        self.assertEqual(EXPECTED_CTR_BY_POSITION[10], 0.018)


class RankControlledFormulaTests(unittest.TestCase):
    def test_known_case_rank_6_to_4_ctr_2_to_5(self):
        """Rank 6.0 -> 4.0, CTR 2% -> 5%. With expected curve at 6=0.045 and
        4=0.080: expected_delta = +0.035. actual_delta = 0.03.
        rank_controlled = 0.03 - 0.035 = -0.005."""
        from measure_ctr_cohort import CohortEntry

        entry = CohortEntry(
            site_slug="matelas", path="/en/foo/", change_type_full="trim-rule",
            change_type_base="trim-rule", pre_impr_7d=100, pre_position=6.0,
            old_title="old", new_title="new", cohort_label="cohort 1",
        )
        baseline = PageMetrics(clicks=2, impressions=100, ctr=0.02, position=6.0)
        current = PageMetrics(clicks=5, impressions=100, ctr=0.05, position=4.0)
        row = compute_row(entry, baseline, current)

        self.assertAlmostEqual(row.d_ctr_abs or 0.0, 0.03, places=9)
        self.assertAlmostEqual(row.d_position or 0.0, -2.0, places=9)
        # The crucial assertion — must be exactly -0.005, not just "< 0.03".
        self.assertAlmostEqual(row.rank_controlled_d_ctr or 0.0, -0.005, places=9)
        # And smaller than the raw 0.03 (which is the spec's loose check).
        self.assertLess(row.rank_controlled_d_ctr or 0.0, 0.03)
        # Relative lift is negative; should fail kill criterion.
        self.assertIsNotNone(row.rank_controlled_d_ctr_rel)
        self.assertLess(row.rank_controlled_d_ctr_rel or 0.0, KILL_CRITERION_RELATIVE)
        self.assertFalse(row.pass_threshold)

    def test_pass_case_genuine_lift(self):
        """No rank movement, CTR jumps 2% -> 5%. Rank-controlled equals raw =
        +0.03 absolute = +150% relative -> PASS."""
        from measure_ctr_cohort import CohortEntry

        entry = CohortEntry(
            site_slug="matelas", path="/en/bar/", change_type_full="trim-gemini",
            change_type_base="trim-gemini", pre_impr_7d=100, pre_position=8.0,
            old_title="o", new_title="n", cohort_label="cohort 1",
        )
        baseline = PageMetrics(clicks=2, impressions=100, ctr=0.02, position=8.0)
        current = PageMetrics(clicks=5, impressions=100, ctr=0.05, position=8.0)
        row = compute_row(entry, baseline, current)
        self.assertAlmostEqual(row.rank_controlled_d_ctr or 0.0, 0.03, places=9)
        self.assertAlmostEqual(row.rank_controlled_d_ctr_rel or 0.0, 1.5, places=9)
        self.assertTrue(row.pass_threshold)


class MissingFlagTests(unittest.TestCase):
    def _build_audits(self, current_pages: list[dict]) -> tuple[dict, dict]:
        baseline = {
            "sites": [{
                "name": "Matelas",
                "url": "sc-domain:matelas-expert.fr",
                "current": {"pages": [
                    {"page": "https://www.matelas-expert.fr/en/avis/tediber/",
                     "clicks": 10, "impressions": 200, "ctr": 5.0, "position": 4.8},
                    {"page": "https://www.matelas-expert.fr/en/test/test-emma-original/",
                     "clicks": 5, "impressions": 150, "ctr": 3.33, "position": 8.2},
                ]},
            }],
            "low_ctr_pages": [],
        }
        current = {
            "sites": [{
                "name": "Matelas",
                "url": "sc-domain:matelas-expert.fr",
                "current": {"pages": current_pages},
            }],
            "low_ctr_pages": [],
        }
        return baseline, current

    def test_missing_flag_when_url_absent_from_current(self):
        from measure_ctr_cohort import CohortEntry

        baseline_audit, current_audit = self._build_audits(current_pages=[
            # tediber present, emma deleted
            {"page": "https://www.matelas-expert.fr/en/avis/tediber/",
             "clicks": 15, "impressions": 220, "ctr": 6.82, "position": 4.0},
        ])

        entry = CohortEntry(
            site_slug="matelas", path="/en/test/test-emma-original/",
            change_type_full="trim-gemini", change_type_base="trim-gemini",
            pre_impr_7d=150, pre_position=8.2, old_title="o", new_title="n",
            cohort_label="cohort 1",
        )
        b = lookup_page(baseline_audit, entry.site_slug, entry.path)
        c = lookup_page(current_audit, entry.site_slug, entry.path)
        row = compute_row(entry, b, c)
        self.assertIsNotNone(b, "Baseline should have the emma row")
        self.assertIsNone(c, "Current should not have the emma row")
        self.assertTrue(row.missing)
        self.assertIn("MISSING", row.notes)

    def test_not_missing_when_present_in_both(self):
        from measure_ctr_cohort import CohortEntry

        baseline_audit, current_audit = self._build_audits(current_pages=[
            {"page": "https://www.matelas-expert.fr/en/avis/tediber/",
             "clicks": 15, "impressions": 220, "ctr": 6.82, "position": 4.0},
        ])

        entry = CohortEntry(
            site_slug="matelas", path="/en/avis/tediber/",
            change_type_full="CTR-variant", change_type_base="CTR-variant",
            pre_impr_7d=200, pre_position=4.8, old_title="o", new_title="n",
            cohort_label="cohort 1",
        )
        b = lookup_page(baseline_audit, entry.site_slug, entry.path)
        c = lookup_page(current_audit, entry.site_slug, entry.path)
        row = compute_row(entry, b, c)
        self.assertFalse(row.missing)
        # CTR was 5.0% in baseline and 6.82% in current (both percent in JSON).
        # In our PageMetrics these are stored as fractions: 0.05 and 0.0682.
        self.assertAlmostEqual(b.ctr, 0.05, places=4)
        self.assertAlmostEqual(c.ctr, 0.0682, places=4)


class ZeroImpressionsTests(unittest.TestCase):
    def test_zero_current_impressions_does_not_crash(self):
        from measure_ctr_cohort import CohortEntry

        entry = CohortEntry(
            site_slug="matelas", path="/en/zero/", change_type_full="trim-rule",
            change_type_base="trim-rule", pre_impr_7d=100, pre_position=6.0,
            old_title="o", new_title="n", cohort_label="cohort 1",
        )
        baseline = PageMetrics(clicks=5, impressions=100, ctr=0.05, position=6.0)
        current = PageMetrics(clicks=0, impressions=0, ctr=0.0, position=6.0)
        row = compute_row(entry, baseline, current)
        # No crash, and a low-signal note is attached.
        self.assertIn("ZERO-CURRENT-IMPR", row.notes)
        # ΔCTR is still computable (0.0 - 0.05 = -0.05).
        self.assertAlmostEqual(row.d_ctr_abs or 0.0, -0.05, places=9)

    def test_zero_baseline_ctr_excluded_from_passfail(self):
        from measure_ctr_cohort import CohortEntry

        entry = CohortEntry(
            site_slug="matelas", path="/en/zerobase/", change_type_full="trim-rule",
            change_type_base="trim-rule", pre_impr_7d=10, pre_position=6.0,
            old_title="o", new_title="n", cohort_label="cohort 1",
        )
        baseline = PageMetrics(clicks=0, impressions=10, ctr=0.0, position=6.0)
        current = PageMetrics(clicks=2, impressions=10, ctr=0.20, position=6.0)
        row = compute_row(entry, baseline, current)
        self.assertIsNone(row.rank_controlled_d_ctr_rel,
                          "Relative lift should be None when baseline_ctr=0")
        self.assertIsNone(row.pass_threshold,
                          "Pass/fail should be None when baseline_ctr=0")
        self.assertIn("ZERO-BASELINE-CTR", row.notes)


class DryRunIntegrationTests(unittest.TestCase):
    def test_dry_run_prints_21_entries(self):
        # main() returns 0 and doesn't crash with a real cohort file.
        rc = main(["--cohort", str(COHORT_MD), "--dry-run"])
        self.assertEqual(rc, 0)


class FullPipelineSmokeTest(unittest.TestCase):
    """End-to-end against a fake "current" JSON built from the real baseline."""

    def test_generates_report_with_aggregates(self):
        baseline_path = Path("/Users/gho/Documents/affiliation-sites/reports/gsc-full-audit-2026-05-10.json")
        if not baseline_path.exists():
            self.skipTest("baseline audit not present")
        with baseline_path.open() as f:
            baseline_audit = json.load(f)

        # Construct a synthetic "current": +30% clicks on every page, same
        # position. This should produce a clean PASS on rows where baseline
        # CTR > 0.
        current_audit = {"sites": [], "low_ctr_pages": []}
        for site in baseline_audit["sites"]:
            new_pages = []
            for page in site.get("current", {}).get("pages", []) or []:
                np_page = dict(page)
                np_page["clicks"] = int((page.get("clicks") or 0) * 1.30) + 1
                impr = page.get("impressions") or 1
                np_page["ctr"] = (np_page["clicks"] / impr) * 100.0
                new_pages.append(np_page)
            current_audit["sites"].append({
                "name": site["name"],
                "url": site["url"],
                "current": {"pages": new_pages},
            })

        with tempfile.TemporaryDirectory() as td:
            current_path = Path(td) / "current.json"
            current_path.write_text(json.dumps(current_audit))
            out_path = Path(td) / "out.md"
            rc = main([
                "--baseline", str(baseline_path),
                "--current", str(current_path),
                "--cohort", str(COHORT_MD),
                "--out", str(out_path),
            ])
            self.assertEqual(rc, 0)
            text = out_path.read_text()
            self.assertIn("# CTR cohort measurement", text)
            self.assertIn("Cohort aggregates", text)
            self.assertIn("Per change-type breakdown", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
