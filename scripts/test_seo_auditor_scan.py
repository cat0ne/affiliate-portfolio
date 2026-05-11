#!/usr/bin/env python3
"""Regression tests for agent_seo_auditor.find_mdx_files and the auto-fix path.

Locks in the multi-content-dir bug-fix shipped 2026-05-11:
  - find_mdx_files now returns (path, locale, content_type) tuples
  - apply_fix(add_canonical) uses the locale tag to build a URL that matches
    the file's locale subtree (no more cross-locale leak on parallel-layout
    sites or nested-layout sites)

Before this fix, a file under ``content-en/tests/foo.mdx`` could be
auto-fixed with ``canonical: https://www.<site>/foo`` — the FR URL on the EN
file. After: ``canonical: https://www.<site>/en/test/foo``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# ── Test helpers ──────────────────────────────────────────────────────────────

def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _make_parallel_site(root: Path) -> Path:
    """Create a parallel-layout site (matelas-style):
      content/tests/test-foo.mdx              → fr
      content-en/tests/test-foo.mdx           → en
      content-de/guides/guide-bar.mdx         → de
      content/data/fixture.mdx                → skipped (data dir)
    """
    site = root / "siteA"
    _write(site / "content" / "tests" / "test-foo.mdx",
           "---\nslug: test-foo\n---\n# Foo\n")
    _write(site / "content-en" / "tests" / "test-foo.mdx",
           "---\nslug: test-foo\n---\n# Foo EN\n")
    _write(site / "content-de" / "guides" / "guide-bar.mdx",
           "---\nslug: guide-bar\n---\n# Bar\n")
    _write(site / "content" / "data" / "fixture.mdx",
           "---\nslug: fixture\n---\n")
    return site


def _make_nested_site(root: Path) -> Path:
    """Create a nested-layout site (pixinstant-style):
      content/comparatifs/baz.mdx             → fr (default)
      content/en/comparatifs/baz.mdx          → en
      content/de/guides/qux.mdx               → de
      content/pages/_root.mdx                 → skipped (pages dir)
    """
    site = root / "siteB"
    _write(site / "content" / "comparatifs" / "baz.mdx",
           "---\nslug: baz\n---\n# Baz\n")
    _write(site / "content" / "en" / "comparatifs" / "baz.mdx",
           "---\nslug: baz\n---\n# Baz EN\n")
    _write(site / "content" / "de" / "guides" / "qux.mdx",
           "---\nslug: qux\n---\n# Qux\n")
    _write(site / "content" / "pages" / "_root.mdx", "---\n---\n")
    return site


def _patch_sites(monkeypatch_dict: dict) -> None:
    """Patch agent_seo_auditor.SITES in place with synthetic site entries."""
    import agent_seo_auditor as mod
    # Save & overwrite — restored by the test harness's try/finally.
    mod._ORIGINAL_SITES = getattr(mod, "_ORIGINAL_SITES", dict(mod.SITES))
    mod.SITES.clear()
    mod.SITES.update(monkeypatch_dict)


def _restore_sites() -> None:
    import agent_seo_auditor as mod
    if hasattr(mod, "_ORIGINAL_SITES"):
        mod.SITES.clear()
        mod.SITES.update(mod._ORIGINAL_SITES)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_parallel_layout_enumeration():
    """find_mdx_files returns (path, locale, type) triples for parallel layout."""
    import agent_seo_auditor as mod

    with tempfile.TemporaryDirectory() as tmp:
        site = _make_parallel_site(Path(tmp))
        _patch_sites({
            "siteA": {"domain": "siteA.test", "default_locale": "fr", "repo_path": site}
        })
        try:
            entries = mod.find_mdx_files("siteA")
            paths_by_locale: dict[str, list[Path]] = {}
            for p, loc, _ct in entries:
                paths_by_locale.setdefault(loc, []).append(p)

            # data/ dir must be skipped
            for p, _, _ in entries:
                assert "data" not in p.parts, f"FAIL: data fixture leaked: {p}"

            assert set(paths_by_locale) == {"fr", "en", "de"}, (
                f"FAIL: locales = {set(paths_by_locale)}, want fr/en/de"
            )
            assert len(paths_by_locale["fr"]) == 1
            assert len(paths_by_locale["en"]) == 1
            assert len(paths_by_locale["de"]) == 1

            # Verify locale tag matches the path's parent content dir
            for p, loc, _ in entries:
                if "content-en" in p.parts:
                    assert loc == "en", f"FAIL: {p} tagged {loc}, want en"
                if "content-de" in p.parts:
                    assert loc == "de", f"FAIL: {p} tagged {loc}, want de"
                if "content" in p.parts and "content-en" not in p.parts and "content-de" not in p.parts:
                    assert loc == "fr", f"FAIL: {p} tagged {loc}, want fr"
            print("PASS parallel layout enumeration")
        finally:
            _restore_sites()


def test_nested_layout_enumeration():
    """find_mdx_files handles nested layout (content/en/, content/de/)."""
    import agent_seo_auditor as mod

    with tempfile.TemporaryDirectory() as tmp:
        site = _make_nested_site(Path(tmp))
        _patch_sites({
            "siteB": {"domain": "siteB.test", "default_locale": "fr", "repo_path": site}
        })
        try:
            entries = mod.find_mdx_files("siteB")
            by_locale: dict[str, list[Path]] = {}
            for p, loc, _ct in entries:
                by_locale.setdefault(loc, []).append(p)

            # pages/ dir must be skipped
            for p, _, _ in entries:
                assert "pages" not in p.parts, f"FAIL: pages dir leaked: {p}"

            assert set(by_locale) == {"fr", "en", "de"}, (
                f"FAIL: locales = {set(by_locale)}, want fr/en/de"
            )
            assert len(by_locale["fr"]) == 1
            assert len(by_locale["en"]) == 1
            assert len(by_locale["de"]) == 1

            # Crucially: the EN file is content/en/... not content-en/...
            en_path = by_locale["en"][0]
            assert "en" in en_path.parts and "content-en" not in en_path.parts, (
                f"FAIL: nested EN path detection wrong: {en_path}"
            )
            print("PASS nested layout enumeration")
        finally:
            _restore_sites()


def test_default_locale_excludes_nested_locale_subtrees():
    """A file at content/en/foo.mdx must NOT be tagged as the site's default locale."""
    import agent_seo_auditor as mod

    with tempfile.TemporaryDirectory() as tmp:
        site = _make_nested_site(Path(tmp))
        _patch_sites({
            "siteB": {"domain": "siteB.test", "default_locale": "fr", "repo_path": site}
        })
        try:
            entries = mod.find_mdx_files("siteB")
            for p, loc, _ in entries:
                # Any path with 'en' as a leading segment must be tagged 'en', not 'fr'
                rel = p.relative_to(site)
                if len(rel.parts) >= 2 and rel.parts[0] == "content" and rel.parts[1] == "en":
                    assert loc == "en", f"FAIL: {rel} tagged {loc}, want en"
            print("PASS default-locale enumeration excludes nested locale subtrees")
        finally:
            _restore_sites()


def test_duplicate_slug_locale_tags_distinct():
    """Same slug in content/ AND content-en/ → each returned with its own locale."""
    import agent_seo_auditor as mod

    with tempfile.TemporaryDirectory() as tmp:
        site = _make_parallel_site(Path(tmp))
        _patch_sites({
            "siteA": {"domain": "siteA.test", "default_locale": "fr", "repo_path": site}
        })
        try:
            entries = mod.find_mdx_files("siteA")
            # Both files have slug "test-foo" — verify locale tags differ
            test_foo = [(p, loc, ct) for p, loc, ct in entries if p.stem == "test-foo"]
            assert len(test_foo) == 2, f"FAIL: expected 2 test-foo entries, got {len(test_foo)}"
            locales = {loc for _, loc, _ in test_foo}
            assert locales == {"fr", "en"}, f"FAIL: locales = {locales}, want fr/en"
            # Content-type 'test' should be detected for both
            for _, _, ct in test_foo:
                assert ct == "test", f"FAIL: content_type = {ct!r}, want 'test'"
            print("PASS duplicate slugs carry distinct locale tags")
        finally:
            _restore_sites()


def test_autofix_canonical_writes_to_correct_locale_url():
    """The smoking-gun test: auto-fix add_canonical for the EN file must
    produce an EN URL, not the default-locale URL.

    Synthetic case from the audit:
      matelas-like site has content/tests/test-foo.mdx AND
      content-en/tests/test-foo.mdx. Auto-fix on the EN issue must write
      ``https://www.<domain>/en/test/test-foo`` into the EN file's canonical.
    """
    import agent_seo_auditor as mod

    with tempfile.TemporaryDirectory() as tmp:
        site = _make_parallel_site(Path(tmp))
        _patch_sites({
            "siteA": {"domain": "siteA.test", "default_locale": "fr", "repo_path": site}
        })
        try:
            entries = mod.find_mdx_files("siteA")
            # Find the EN and FR test-foo entries
            en_entry = next((e for e in entries if e[0].stem == "test-foo" and e[1] == "en"), None)
            fr_entry = next((e for e in entries if e[0].stem == "test-foo" and e[1] == "fr"), None)
            assert en_entry and fr_entry, "FAIL: fixture missing EN or FR test-foo"

            en_path, en_locale, en_ct = en_entry
            fr_path, fr_locale, fr_ct = fr_entry

            # Build the issue the auditor would emit, with locale tagging
            en_issue = {
                "type": "missing_canonical",
                "severity": "warning",
                "description": "no canonical",
                "auto_fixable": True,
                "fix_type": "add_canonical",
                "site_slug": "siteA",
                "locale": en_locale,
                "content_type": en_ct,
            }
            fr_issue = dict(en_issue)
            fr_issue["locale"] = fr_locale
            fr_issue["content_type"] = fr_ct

            # Apply auto-fix to each file
            ok_en = mod.apply_fix(en_path, en_issue, dry_run=False)
            ok_fr = mod.apply_fix(fr_path, fr_issue, dry_run=False)
            assert ok_en and ok_fr, "FAIL: apply_fix returned False"

            en_after = en_path.read_text(encoding="utf-8")
            fr_after = fr_path.read_text(encoding="utf-8")

            # The crucial assertion: EN file canonical contains /en/
            assert "https://www.siteA.test/en/test/test-foo" in en_after, (
                f"FAIL: EN canonical wrong-locale write. Got:\n{en_after}"
            )
            # And the FR file canonical does NOT contain /en/
            assert "/en/" not in fr_after, (
                f"FAIL: FR canonical leaked /en/. Got:\n{fr_after}"
            )
            assert "https://www.siteA.test/test/test-foo" in fr_after, (
                f"FAIL: FR canonical missing or wrong. Got:\n{fr_after}"
            )
            print("PASS auto-fix add_canonical writes locale-correct URL on both files")
        finally:
            _restore_sites()


def test_repo_missing_returns_empty():
    """Missing repo_path → empty list (no crash)."""
    import agent_seo_auditor as mod
    _patch_sites({
        "ghost": {"domain": "x.test", "default_locale": "fr", "repo_path": Path("/nonexistent/zzz")}
    })
    try:
        assert mod.find_mdx_files("ghost") == []
        assert mod.find_mdx_files("unknown-site") == []
        print("PASS missing repo / unknown site → empty list")
    finally:
        _restore_sites()


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_parallel_layout_enumeration,
        test_nested_layout_enumeration,
        test_default_locale_excludes_nested_locale_subtrees,
        test_duplicate_slug_locale_tags_distinct,
        test_autofix_canonical_writes_to_correct_locale_url,
        test_repo_missing_returns_empty,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failures += 1
        except Exception as e:
            import traceback
            print(f"FAIL {t.__name__}: unexpected: {e}")
            traceback.print_exc()
            failures += 1
    total = len(tests)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
