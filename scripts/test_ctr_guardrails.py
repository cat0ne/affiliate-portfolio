#!/usr/bin/env python3
"""Regression tests for agent_ctr_optimizer.py guardrails added 2026-05-11.

Reproduces the two confirmed failure modes:
  1. EN listicle title emitted on a FR comparatif page (bureau)
  2. Listicle template emitted on a single-product test page (matelas)

Run: python3 scripts/test_ctr_guardrails.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_ctr_optimizer import (
    _detect_content_type,
    _validate_variant_for_locale,
    propose_variants,
)


def test_locale_rejects_english_words_on_fr():
    cases = [
        ("Best Ergonomic Chairs Under €300 (2026) – 5 Tested & Ranked", "fr"),
        ("7 Best Emma Original for 2026 (Tested & Compared)", "fr"),
        ("Ultimate Mattress Buyer's Guide (2026, Tested)", "fr"),
    ]
    for title, locale in cases:
        ok, reason = _validate_variant_for_locale(title, locale)
        assert not ok, f"FAIL: {title!r} should be rejected on {locale}, got ok={ok}"
        assert reason.startswith("english_word_on_"), f"unexpected reason: {reason}"
    print("✓ locale rejects English words on FR pages")


def test_locale_allows_fr_on_fr():
    title = "7 meilleurs matelas mémoire de forme 2026 (testés & comparés)"
    ok, reason = _validate_variant_for_locale(title, "fr")
    assert ok, f"FAIL: FR title rejected: {reason}"
    print("✓ locale allows native FR title on FR page")


def test_locale_allows_en_on_en():
    title = "7 Best Instax Mini Cameras for 2026 (Tested & Compared)"
    ok, _ = _validate_variant_for_locale(title, "en")
    assert ok, "FAIL: EN title rejected on EN page"
    print("✓ locale allows EN title on EN page")


def test_content_type_detection_from_url():
    cases = [
        ("https://matelas-expert.fr/test/test-emma-original", "test"),
        ("https://bureau-expert.fr/guide/best-chair-2026", "guide"),
        ("https://pixinstant.com/en/comparatif/instax-vs-polaroid", "comparatif"),
    ]
    for url, expected in cases:
        got = _detect_content_type(None, url)
        assert got == expected, f"FAIL: {url} → {got!r}, expected {expected!r}"
    print("✓ content type inferred from URL")


def test_listicle_filtered_on_test_page():
    """Reproduce the matelas regression: test-emma-original got 'N Best…' title."""
    variants = propose_variants(
        current_title="Emma Original Avis 2026 : Notre Test Complet après 100 Nuits",
        slug="test-emma-original",
        page_path="https://matelas-expert.fr/test/test-emma-original/",
        locale="fr",
        content_type="test",
    )
    names = {v["name"] for v in variants}
    assert "number_year" not in names, (
        f"FAIL: listicle variant 'number_year' should be filtered on type=test, "
        f"got variants: {names}"
    )
    # Must still produce at least one valid alternative
    assert variants, "FAIL: propose_variants returned empty after filtering"
    print(f"✓ listicle filtered on test page ({len(variants)} variants survived)")


def test_listicle_allowed_on_comparatif():
    """Sanity check: comparatif pages CAN use listicle templates."""
    variants = propose_variants(
        current_title="Comparatif Aspirateurs Balai 2026",
        slug="meilleurs-aspirateurs-balai-2026",
        page_path="https://top-aspirateur.fr/comparatif/meilleurs-aspirateurs-balai-2026/",
        locale="fr",
        content_type="comparatif",
    )
    names = {v["name"] for v in variants}
    assert "number_year" in names, f"FAIL: listicle should survive on comparatif, got: {names}"
    print(f"✓ listicle preserved on comparatif page")


def test_listicle_filtered_on_avis_page():
    """Tediber EN avis page caught in live run 2026-05-11."""
    variants = propose_variants(
        current_title="Avis Tediber : notre opinion sur la marque française",
        slug="tediber-avis-test",
        page_path="https://matelas-expert.fr/en/avis/tediber-avis-test/",
        locale="en",
        content_type="avis",
    )
    names = {v["name"] for v in variants}
    assert "number_year" not in names, (
        f"FAIL: listicle should be filtered on type=avis, got: {names}"
    )
    print(f"✓ listicle filtered on avis page ({len(variants)} variants)")


def test_full_regression_bureau_case():
    """Bureau FR comparatif: even rule-based variants must be FR."""
    variants = propose_variants(
        current_title="Chaise Ergonomique < 300€ 2026 : 5 Testées | Notre Coup de Cœur",
        slug="meilleure-chaise-ergonomique-moins-300-euros",
        page_path="https://bureau-expert.fr/comparatif/meilleure-chaise-ergonomique-moins-300-euros/",
        locale="fr",
        content_type="comparatif",
    )
    for v in variants:
        ok, _ = _validate_variant_for_locale(v["title"], "fr")
        assert ok, f"FAIL: variant survived filter but contains EN words: {v['title']!r}"
    print(f"✓ bureau case clean ({len(variants)} variants, all FR)")


if __name__ == "__main__":
    tests = [
        test_locale_rejects_english_words_on_fr,
        test_locale_allows_fr_on_fr,
        test_locale_allows_en_on_en,
        test_content_type_detection_from_url,
        test_listicle_filtered_on_test_page,
        test_listicle_allowed_on_comparatif,
        test_listicle_filtered_on_avis_page,
        test_full_regression_bureau_case,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
            failures += 1
        except Exception as e:
            print(f"✗ {t.__name__}: unexpected error: {e}")
            failures += 1
    print(f"\n{'-' * 40}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
