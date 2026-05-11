#!/usr/bin/env python3
"""Tests for scripts/apply_trim_queue.py.

Covers:
- cohort markdown parser handles all 4 cohort table styles in ctr-experiments.md
- URL → (slug, locale) handles trailing slashes / anchors / multi-locale paths
- title quote styles: double, single, unquoted (+ unicode round-trip)
- description block scalar (``>-``) is detected and skipped
- ambiguous matches (line appears twice) are reported as ambiguous, not applied
- audit-missing pages are ranked last
- ``--dry-run`` does not modify any file (mtime + sha256 unchanged)
- ``--exclude-applied`` filters out cohorted slugs
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "apply_trim_queue.py"
sys.path.insert(0, str(HERE))

import apply_trim_queue as atq  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Pure unit tests (no filesystem fixtures)
# ---------------------------------------------------------------------------


class TestSlugLocale(unittest.TestCase):
    def test_default_locale_fr(self):
        slug, locale = atq.extract_slug_locale("https://www.matelas-expert.fr/test/test-emma-original/")
        self.assertEqual(slug, "test-emma-original")
        self.assertEqual(locale, "fr")

    def test_en_prefix(self):
        slug, locale = atq.extract_slug_locale("https://www.pixinstant.com/en/comparatif/instax-mini-12-vs-mini-11/")
        self.assertEqual(slug, "instax-mini-12-vs-mini-11")
        self.assertEqual(locale, "en")

    def test_anchor_stripped(self):
        slug, locale = atq.extract_slug_locale("https://x.com/en/guide/foo/#section")
        self.assertEqual(slug, "foo")
        self.assertEqual(locale, "en")

    def test_query_stripped(self):
        slug, _ = atq.extract_slug_locale("https://x.com/de/test/bar?ref=1")
        self.assertEqual(slug, "bar")

    def test_no_trailing_slash(self):
        slug, locale = atq.extract_slug_locale("https://x.com/es/test/baz")
        self.assertEqual(slug, "baz")
        self.assertEqual(locale, "es")


# ---------------------------------------------------------------------------
# Cohort markdown parsing
# ---------------------------------------------------------------------------


COHORT_MD = """\
# CTR title experiments log

## Cohort 2026-05-11 (n=12)

| Site | Page | Change type | Pre impr/7d | Pre pos | Old title (len) | New title (len) |
|---|---|---|---|---|---|---|
| matelas | `/en/avis/tediber/` | CTR-variant | 64 | 4.8 | Old | New |
| pixinstant | `/en/comparatif/instax-mini-12-vs-mini-11/` | trim-rule | 374 | 9.5 | Old | New |

## Cohort 2026-05-11 (cohort 2, n=9)

| Site | Page | Change type | Pre impr/7d | Pre pos | Old | New |
|---|---|---|---|---|---|---|
| aspirateur | `/en/guide/best-robot-vacuum-under-300-2026/` | trim-rule | 63 | 7.7 | Old | New |
| pixinstant | `/comparatif/meilleur-instax-mini-2026/` (FR) | trim-rule | 39 | 9.5 | Old | New |

## Cohort 2026-05-11 (cohort 3, n=21) — locale extension

| Site | Page | Change type | Locale | Old len | New len |
|---|---|---|---|---|---|
| matelas | `/de/test/test-emma-original/` | trim-gemini | de | 71 | 60 |
| pixinstant | `/it/comparatif/meilleur-instax-mini-2026/` | trim-rule | it | 61 | 48 |

## Cohort 2026-05-11 (cohort 4, n=15) — high-impression sweep

| Site | Page | Change type | Locale | Old | New | Impr |
|---|---|---|---|---|---|---|
| bureau | `/en/comparatif/meilleure-chaise-ergonomique-moins-300-euros/` | trim-rule | en | 63 | 47 | 183 |
| matelas | `/en/test/tediber-avis-test/` | trim-gemini | en | 74 | 52 | 32 |
"""


class TestCohortMarkdownParser(unittest.TestCase):
    def test_all_four_cohort_tables(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fp:
            fp.write(COHORT_MD)
            path = Path(fp.name)
        try:
            slugs = atq.parse_applied_slugs(path)
            # cohort 1
            self.assertIn("tediber", slugs)
            self.assertIn("instax-mini-12-vs-mini-11", slugs)
            # cohort 2 — note "meilleur-instax-mini-2026" appears in both
            # cohort 2 (FR) and cohort 3 (IT) — that's fine, it's a set.
            self.assertIn("best-robot-vacuum-under-300-2026", slugs)
            self.assertIn("meilleur-instax-mini-2026", slugs)
            # cohort 3
            self.assertIn("test-emma-original", slugs)
            # cohort 4
            self.assertIn("meilleure-chaise-ergonomique-moins-300-euros", slugs)
            self.assertIn("tediber-avis-test", slugs)
        finally:
            path.unlink()

    def test_missing_file_returns_empty_set(self):
        slugs = atq.parse_applied_slugs(Path("/nonexistent/path.md"))
        self.assertEqual(slugs, set())


# ---------------------------------------------------------------------------
# YAML edit logic
# ---------------------------------------------------------------------------


class TestFrontmatterReplacement(unittest.TestCase):
    def test_double_quoted(self):
        text = '---\ntitle: "Hello World"\nslug: x\n---\n'
        matched, repl = atq._build_frontmatter_replacement(text, "title", "Hello World", "Hi")
        self.assertEqual(matched, 'title: "Hello World"')
        self.assertEqual(repl, 'title: "Hi"')

    def test_single_quoted(self):
        text = "---\ntitle: 'Hello World'\nslug: x\n---\n"
        matched, repl = atq._build_frontmatter_replacement(text, "title", "Hello World", "Hi")
        self.assertEqual(matched, "title: 'Hello World'")
        self.assertEqual(repl, "title: 'Hi'")

    def test_single_quoted_with_apostrophe_in_new(self):
        text = "---\ntitle: 'A B C'\n---\n"
        matched, repl = atq._build_frontmatter_replacement(text, "title", "A B C", "It's good")
        self.assertEqual(matched, "title: 'A B C'")
        self.assertEqual(repl, "title: 'It''s good'")  # YAML escape

    def test_unquoted_title_promotes_to_double_quoted(self):
        text = "---\ntitle: Bare Title Here\nslug: x\n---\n"
        matched, repl = atq._build_frontmatter_replacement(text, "title", "Bare Title Here", "Trimmed")
        self.assertEqual(matched, "title: Bare Title Here")
        self.assertEqual(repl, 'title: "Trimmed"')

    def test_unquoted_does_not_match_substring_of_quoted(self):
        # The quoted line contains `Bare Title` as substring — must NOT match
        # the unquoted regex (which is anchored on line boundaries).
        text = '---\ntitle: "Bare Title Here Longer"\n---\n'
        matched, _ = atq._build_frontmatter_replacement(text, "title", "Bare Title Here", "x")
        self.assertIsNone(matched)

    def test_mismatch(self):
        text = '---\ntitle: "Foo"\n---\n'
        matched, reason = atq._build_frontmatter_replacement(text, "title", "Bar", "x")
        self.assertIsNone(matched)
        self.assertEqual(reason, "mismatch")

    def test_ambiguous_duplicate_lines(self):
        text = '---\ntitle: "X"\ndescription: y\n---\ntitle: "X"\n'
        matched, reason = atq._build_frontmatter_replacement(text, "title", "X", "Z")
        self.assertIsNone(matched)
        self.assertTrue(reason.startswith("ambiguous"))

    def test_block_scalar_description_skipped(self):
        text = "---\ntitle: \"T\"\ndescription: >-\n  multi line\n  description text\nslug: x\n---\n"
        matched, reason = atq._build_frontmatter_replacement(text, "description", "anything", "x")
        self.assertIsNone(matched)
        self.assertEqual(reason, "block-scalar")

    def test_block_scalar_pipe(self):
        text = "---\ndescription: |-\n  multi line\n---\n"
        matched, reason = atq._build_frontmatter_replacement(text, "description", "x", "y")
        self.assertIsNone(matched)
        self.assertEqual(reason, "block-scalar")

    def test_unicode_round_trip(self):
        text = '---\ntitle: "Épéda L\'Échappée Test 2026"\n---\n'
        matched, repl = atq._build_frontmatter_replacement(
            text, "title", "Épéda L'Échappée Test 2026", "Épéda Test 2026 (Caffè edition)"
        )
        self.assertIsNotNone(matched)
        self.assertIn("Caffè", repl)
        self.assertIn("Épéda", repl)

    def test_double_quote_in_new_is_escaped(self):
        text = '---\ntitle: "A"\n---\n'
        matched, repl = atq._build_frontmatter_replacement(text, "title", "A", 'He said "hi"')
        self.assertEqual(matched, 'title: "A"')
        self.assertEqual(repl, 'title: "He said \\"hi\\""')


# ---------------------------------------------------------------------------
# apply_edit (filesystem) — dry-run must not modify
# ---------------------------------------------------------------------------


class TestApplyEdit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_dry_run_does_not_modify(self):
        f = self.tmp / "post.mdx"
        f.write_text('---\ntitle: "Hello"\n---\nbody\n', encoding="utf-8")
        before_sha = _sha256(f)
        before_mtime = f.stat().st_mtime_ns
        res = atq.apply_edit(f, "title", "Hello", "Hi", dry_run=True)
        self.assertTrue(res.ok)
        self.assertEqual(_sha256(f), before_sha)
        self.assertEqual(f.stat().st_mtime_ns, before_mtime)

    def test_live_writes(self):
        f = self.tmp / "post.mdx"
        f.write_text('---\ntitle: "Hello"\n---\n', encoding="utf-8")
        res = atq.apply_edit(f, "title", "Hello", "Hi", dry_run=False)
        self.assertTrue(res.ok)
        self.assertIn('title: "Hi"', f.read_text(encoding="utf-8"))

    def test_block_scalar_skipped_no_write(self):
        f = self.tmp / "post.mdx"
        original = '---\ntitle: "T"\ndescription: >-\n  long\n  text\n---\n'
        f.write_text(original, encoding="utf-8")
        before_sha = _sha256(f)
        res = atq.apply_edit(f, "description", "long text", "short", dry_run=False)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "block-scalar")
        self.assertEqual(_sha256(f), before_sha)

    def test_unquoted_title_promoted_to_double(self):
        f = self.tmp / "post.mdx"
        f.write_text("---\ntitle: Bare Title Here\nslug: x\n---\n", encoding="utf-8")
        res = atq.apply_edit(f, "title", "Bare Title Here", "Trimmed", dry_run=False)
        self.assertTrue(res.ok)
        content = f.read_text(encoding="utf-8")
        self.assertIn('title: "Trimmed"', content)
        self.assertNotIn("title: Bare Title Here", content)


# ---------------------------------------------------------------------------
# Candidate ranking
# ---------------------------------------------------------------------------


class TestBuildCandidates(unittest.TestCase):
    def test_audit_missing_ranks_last(self):
        proposals = [
            {
                "site": "matelas",
                "mdx_path": "/x/foo.mdx",
                "locale": "en",
                "current_title": "A",
                "proposed_title": "B",
                "strategy": "pipe_drop",
            },
            {
                "site": "matelas",
                "mdx_path": "/x/bar.mdx",
                "locale": "en",
                "current_title": "C",
                "proposed_title": "D",
                "strategy": "pipe_drop",
            },
        ]
        audit_index = {
            ("bar", "en"): atq.AuditEntry(site="Matelas", url="x", impressions=100, position=5.0),
            # foo is missing — should rank last with impr=0 pos=99
        }
        out = atq.build_candidates(
            proposals,
            audit_index,
            queue="title",
            excluded_slugs=set(),
            excluded_sites=set(),
            excluded_site_keys=set(),
            allowed_strategies=None,
            min_impressions=0,  # admit everything so we test ordering
        )
        # bar should be first (impr=100), foo last
        self.assertEqual(out[0].slug, "bar")
        self.assertEqual(out[1].slug, "foo")
        self.assertEqual(out[1].impressions, 0)
        self.assertEqual(out[1].position, 99.0)

    def test_excluded_slugs_marked(self):
        proposals = [
            {
                "site": "matelas",
                "mdx_path": "/x/already-done.mdx",
                "locale": "en",
                "current_title": "A",
                "proposed_title": "B",
                "strategy": "pipe_drop",
            },
        ]
        audit_index = {
            ("already-done", "en"): atq.AuditEntry(
                site="Matelas", url="x", impressions=500, position=4.0
            ),
        }
        out = atq.build_candidates(
            proposals,
            audit_index,
            queue="title",
            excluded_slugs={"already-done"},
            excluded_sites=set(),
            excluded_site_keys=set(),
            allowed_strategies=None,
            min_impressions=0,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].excluded_reason, "already-applied")

    def test_no_strategy_worked_dropped(self):
        proposals = [
            {
                "site": "matelas",
                "mdx_path": "/x/foo.mdx",
                "locale": "en",
                "current_title": "A",
                "proposed_title": None,
                "strategy": "no_strategy_worked",
            },
        ]
        out = atq.build_candidates(
            proposals,
            {},
            queue="title",
            excluded_slugs=set(),
            excluded_sites=set(),
            excluded_site_keys=set(),
            allowed_strategies=None,
            min_impressions=0,
        )
        self.assertEqual(out, [])

    def test_strategy_filter(self):
        proposals = [
            {
                "site": "matelas",
                "mdx_path": "/x/a.mdx",
                "locale": "en",
                "current_title": "X",
                "proposed_title": "Y",
                "strategy": "gemini",
            },
            {
                "site": "matelas",
                "mdx_path": "/x/b.mdx",
                "locale": "en",
                "current_title": "X",
                "proposed_title": "Y",
                "strategy": "pipe_drop",
            },
        ]
        out = atq.build_candidates(
            proposals, {},
            queue="title",
            excluded_slugs=set(),
            excluded_sites=set(),
            excluded_site_keys=set(),
            allowed_strategies={"pipe_drop"},
            min_impressions=0,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].proposal["strategy"], "pipe_drop")


# ---------------------------------------------------------------------------
# Integration: dry-run end-to-end against minimal fixtures
# ---------------------------------------------------------------------------


class TestDryRunIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Synthesize mdx
        mdx_dir = self.tmp / "site" / "content"
        mdx_dir.mkdir(parents=True)
        self.mdx = mdx_dir / "alpha.mdx"
        self.mdx.write_text(
            '---\ntitle: "Alpha Long Title 2026: Buyer Guide"\n---\nbody\n',
            encoding="utf-8",
        )
        # Queue
        queue_dir = self.tmp / "reports" / "agent_queues" / "title_trim_proposed"
        queue_dir.mkdir(parents=True)
        queue = {
            "proposals": [
                {
                    "site": "matelas",
                    "mdx_path": str(self.mdx),
                    "locale": "en",
                    "current_title": "Alpha Long Title 2026: Buyer Guide",
                    "proposed_title": "Alpha 2026: Buyer Guide",
                    "strategy": "pipe_drop",
                }
            ]
        }
        (queue_dir / "2026-05-11.json").write_text(json.dumps(queue), encoding="utf-8")
        # Audit
        audit = {
            "sites": [
                {
                    "name": "Matelas",
                    "current": {
                        "pages": [
                            {
                                "page": f"https://x.com/en/test/alpha/",
                                "impressions": 200,
                                "position": 7.5,
                            }
                        ]
                    },
                }
            ]
        }
        self.audit_path = self.tmp / "audit.json"
        self.audit_path.write_text(json.dumps(audit), encoding="utf-8")
        # Empty cohort md
        self.cohort = self.tmp / "ctr-experiments.md"
        self.cohort.write_text("# Empty\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_dry_run_does_not_modify_mdx(self):
        before = _sha256(self.mdx)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--queue", "title",
                "--date", "2026-05-11",
                "--audit", str(self.audit_path),
                "--exclude-applied", str(self.cohort),
                "--top-n", "5",
                "--min-impressions", "0",
                "--dry-run",
                "--queue-dir", str(self.tmp / "reports" / "agent_queues" / "title_trim_proposed"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_sha256(self.mdx), before)
        self.assertIn("DRY-RUN", result.stdout)
        self.assertIn("alpha.mdx", result.stdout)

    def test_exclude_applied_filters(self):
        # Mark alpha as applied
        self.cohort.write_text(
            "## Cohort\n| s | `/en/test/alpha/` | x | 1 |\n", encoding="utf-8"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--queue", "title",
                "--date", "2026-05-11",
                "--audit", str(self.audit_path),
                "--exclude-applied", str(self.cohort),
                "--top-n", "5",
                "--min-impressions", "0",
                "--dry-run",
                "--queue-dir", str(self.tmp / "reports" / "agent_queues" / "title_trim_proposed"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # alpha should be excluded → 0 candidates to apply, 1 excluded
        self.assertIn("0 to apply", result.stdout)
        self.assertIn("1 excluded", result.stdout)


if __name__ == "__main__":
    unittest.main()
