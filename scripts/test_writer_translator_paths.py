#!/usr/bin/env python3
"""Regression tests for resolve_mdx_path multi-content-dir support.

Covers both agent_writer.resolve_mdx_path and agent_translator.resolve_mdx_path.

Live filesystem state verified 2026-05-11 (same fixtures as test_mdx_lookup.py):
  - matelas/bureau/cafe:  parallel layout (content/ + content-<loc>/)
  - aspirateur (mixed):   content/ + content-en/ + content/es/
  - pixinstant:           nested layout (content/ + content/<loc>/)

Before the fix, the terminal `repo.rglob("*.mdx")` fallback could return a
sibling-locale file (e.g. a FR article when an EN slug was requested), which
the writer/translator would then read + overwrite with wrong-language content.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_writer import BASE_DIR as WRITER_BASE_DIR
from agent_writer import resolve_mdx_path as writer_resolve
from agent_translator import resolve_mdx_path as translator_resolve


# (label, site_slug, article_slug, locale, expected_relative_or_None)
CASES = [
    (
        "matelas EN parallel layout → content-en/, not content/",
        "matelas",
        "test-emma-original",
        "en",
        "matelas/content-en/tests/test-emma-original.mdx",
    ),
    (
        "matelas FR default locale → content/, not content-en/",
        "matelas",
        "test-emma-original",
        "fr",
        "matelas/content/tests/test-emma-original.mdx",
    ),
    (
        "matelas EN avis-slug actually lives in content-en/tests/",
        "matelas",
        "tediber-avis-test",
        "en",
        "matelas/content-en/tests/tediber-avis-test.mdx",
    ),
    (
        "aspirateur ES nested layout (content/es/)",
        "aspirateur",
        "mejores-aspiradoras-sin-cable-2026",
        "es",
        "aspirateur/content/es/comparatifs/mejores-aspiradoras-sin-cable-2026.mdx",
    ),
    (
        "pixinstant EN nested layout (content/en/)",
        "pixinstant",
        "test-polaroid-go-gen2",
        "en",
        "pixinstant/content/en/tests/test-polaroid-go-gen2.mdx",
    ),
    (
        "nonexistent slug returns None (no whole-repo rglob fallback)",
        "matelas",
        "this-slug-does-not-exist-anywhere-2026-05-11",
        "en",
        None,
    ),
]


def _check(label: str, fn, site: str, slug: str, locale: str, expected: str | None) -> bool:
    expected_abs = (WRITER_BASE_DIR / expected).resolve() if expected else None
    got = fn(site, slug, locale)
    got_abs = got.resolve() if got else None
    ok = got_abs == expected_abs
    marker = "✓" if ok else "✗"
    print(f"  {marker} {label}")
    if not ok:
        print(f"      expected: {expected_abs}")
        print(f"      got:      {got_abs}")
    return ok


def test_writer_resolves_correct_locale_parallel() -> bool:
    """matelas EN slug must resolve to content-en/, not content/."""
    return _check(
        "matelas EN slug → content-en/ (parallel layout)",
        writer_resolve,
        "matelas", "test-emma-original", "en",
        "matelas/content-en/tests/test-emma-original.mdx",
    )


def test_writer_resolves_correct_locale_nested() -> bool:
    """pixinstant EN slug must resolve to content/en/."""
    return _check(
        "pixinstant EN slug → content/en/ (nested layout)",
        writer_resolve,
        "pixinstant", "test-polaroid-go-gen2", "en",
        "pixinstant/content/en/tests/test-polaroid-go-gen2.mdx",
    )


def test_writer_default_locale_excludes_locale_subdirs() -> bool:
    """matelas FR slug must resolve under content/<not en|de|es|it|uk>/."""
    got = writer_resolve("matelas", "test-emma-original", "fr")
    expected = (WRITER_BASE_DIR / "matelas/content/tests/test-emma-original.mdx").resolve()
    ok_path = got and got.resolve() == expected
    # Defensive: also verify the returned path isn't under a locale subdir.
    ok_not_locale = True
    if got:
        try:
            rel = got.resolve().relative_to((WRITER_BASE_DIR / "matelas/content").resolve()).parts
            if rel and rel[0] in {"en", "de", "es", "it", "uk", "ja"}:
                ok_not_locale = False
        except ValueError:
            pass
    ok = bool(ok_path and ok_not_locale)
    marker = "✓" if ok else "✗"
    print(f"  {marker} matelas FR default-locale resolves outside content/<locale>/")
    if not ok:
        print(f"      got: {got}")
    return ok


def test_writer_default_locale_no_cross_locale_leak() -> bool:
    """matelas FR on a slug shared between FR and EN must return the FR file, never content-en/."""
    got = writer_resolve("matelas", "tediber-avis-test", "fr")
    if got is None:
        print(f"  ✗ matelas FR resolve returned None unexpectedly")
        return False
    got_str = str(got.resolve())
    # Must be the FR file, not content-en/ sibling.
    ok = "/matelas/content/tests/tediber-avis-test.mdx" in got_str and "content-en" not in got_str
    marker = "✓" if ok else "✗"
    print(f"  {marker} matelas FR resolve on shared slug → content/ not content-en/")
    if not ok:
        print(f"      got: {got_str}")
    return ok


def test_writer_pixinstant_default_no_nested_locale_leak() -> bool:
    """pixinstant FR on a slug shared between FR and EN must NOT return content/en/."""
    got = writer_resolve("pixinstant", "test-polaroid-go-gen2", "fr")
    if got is None:
        print(f"  ✗ pixinstant FR resolve returned None unexpectedly")
        return False
    got_str = str(got.resolve())
    # Must NOT be under content/en/.
    try:
        rel = got.resolve().relative_to((WRITER_BASE_DIR / "pixinstant/content").resolve()).parts
        first_seg = rel[0] if rel else ""
    except ValueError:
        first_seg = ""
    ok = first_seg not in {"en", "de", "es", "it", "uk", "ja"}
    marker = "✓" if ok else "✗"
    print(f"  {marker} pixinstant FR resolve on shared slug → not under content/<locale>/")
    if not ok:
        print(f"      got: {got_str}")
    return ok


def test_translator_existing_file_correct_locale() -> bool:
    """Translator must resolve EN source to content-en/ on matelas (parallel)."""
    return _check(
        "translator: matelas EN → content-en/",
        translator_resolve,
        "matelas", "test-emma-original", "en",
        "matelas/content-en/tests/test-emma-original.mdx",
    )


def test_translator_nested_layout_correct_locale() -> bool:
    """Translator must resolve EN source to content/en/ on pixinstant (nested)."""
    return _check(
        "translator: pixinstant EN → content/en/",
        translator_resolve,
        "pixinstant", "test-polaroid-go-gen2", "en",
        "pixinstant/content/en/tests/test-polaroid-go-gen2.mdx",
    )


def test_translator_default_locale_no_cross_locale_leak() -> bool:
    """Translator FR on a shared slug must return content/ not content-en/ (avoids translating EN-as-FR)."""
    got = translator_resolve("matelas", "tediber-avis-test", "fr")
    if got is None:
        print(f"  ✗ translator: matelas FR resolve returned None unexpectedly")
        return False
    got_str = str(got.resolve())
    ok = "/matelas/content/tests/tediber-avis-test.mdx" in got_str and "content-en" not in got_str
    marker = "✓" if ok else "✗"
    print(f"  {marker} translator: matelas FR resolve → content/ not content-en/")
    if not ok:
        print(f"      got: {got_str}")
    return ok


def test_translator_nonexistent_slug_returns_none() -> bool:
    """Translator on a slug that doesn't exist anywhere must return None."""
    got = translator_resolve("matelas", "this-slug-does-not-exist-anywhere-2026-05-11", "en")
    ok = got is None
    marker = "✓" if ok else "✗"
    print(f"  {marker} translator: nonexistent slug → None")
    if not ok:
        print(f"      got: {got}")
    return ok


def test_aspirateur_es_nested_layout() -> bool:
    """Aspirateur mixed layout: ES slug lives in content/es/ (nested), not content-es/."""
    got = writer_resolve("aspirateur", "mejores-aspiradoras-sin-cable-2026", "es")
    expected = (WRITER_BASE_DIR / "aspirateur/content/es/comparatifs/mejores-aspiradoras-sin-cable-2026.mdx").resolve()
    ok = got is not None and got.resolve() == expected
    marker = "✓" if ok else "✗"
    print(f"  {marker} aspirateur ES → content/es/ (nested branch of mixed layout)")
    if not ok:
        print(f"      expected: {expected}")
        print(f"      got:      {got}")
    return ok


def test_isolated_no_rglob_fallback_leak() -> bool:
    """Synthetic fixture: EN-only slug under content-en/, FR resolve must return None.

    This proves the killed `repo.rglob("*.mdx")` cross-locale fallback doesn't
    silently return content-en/<slug>.mdx when locale=fr is requested.
    """
    import tempfile
    from unittest import mock

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "fakesite"
        (repo / "content" / "tests").mkdir(parents=True)
        (repo / "content-en" / "tests").mkdir(parents=True)
        # Slug exists ONLY in content-en/, NOT in content/
        (repo / "content-en" / "tests" / "en-only-product.mdx").write_text("# en", encoding="utf-8")

        from agent_writer import SITES as W_SITES
        from agent_translator import SITES as T_SITES

        fake_cfg = {
            "name": "FakeSite",
            "repo_path": repo,
            "is_monorepo": False,
            "default_locale": "fr",
        }

        W_SITES["__fake__"] = fake_cfg
        T_SITES["__fake__"] = dict(fake_cfg, secondary_locales=["en"], strategy="primary_first")
        try:
            w_fr = writer_resolve("__fake__", "en-only-product", "fr")
            w_en = writer_resolve("__fake__", "en-only-product", "en")
            t_fr = translator_resolve("__fake__", "en-only-product", "fr")
            t_en = translator_resolve("__fake__", "en-only-product", "en")
        finally:
            W_SITES.pop("__fake__", None)
            T_SITES.pop("__fake__", None)

        ok_w_fr = w_fr is None
        ok_w_en = w_en is not None and "content-en" in str(w_en)
        ok_t_fr = t_fr is None
        ok_t_en = t_en is not None and "content-en" in str(t_en)
        ok = ok_w_fr and ok_w_en and ok_t_fr and ok_t_en
        marker = "✓" if ok else "✗"
        print(f"  {marker} isolated fixture: EN-only slug → FR=None, EN=content-en/ (writer+translator)")
        if not ok:
            print(f"      writer FR: {w_fr} (want None)  EN: {w_en} (want content-en/)")
            print(f"      translator FR: {t_fr} (want None)  EN: {t_en} (want content-en/)")
        return ok


TESTS = [
    test_writer_resolves_correct_locale_parallel,
    test_writer_resolves_correct_locale_nested,
    test_writer_default_locale_excludes_locale_subdirs,
    test_writer_default_locale_no_cross_locale_leak,
    test_writer_pixinstant_default_no_nested_locale_leak,
    test_translator_existing_file_correct_locale,
    test_translator_nested_layout_correct_locale,
    test_translator_default_locale_no_cross_locale_leak,
    test_translator_nonexistent_slug_returns_none,
    test_aspirateur_es_nested_layout,
    test_isolated_no_rglob_fallback_leak,
]


def run() -> int:
    print("=" * 70)
    print("Writer/Translator resolve_mdx_path locale-safety tests")
    print("=" * 70)
    print()
    passes = 0
    for t in TESTS:
        try:
            if t():
                passes += 1
        except Exception as exc:
            print(f"  ✗ {t.__name__} raised: {exc}")
    print()
    print(f"{passes}/{len(TESTS)} passed")
    return 0 if passes == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(run())
