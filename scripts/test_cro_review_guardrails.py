#!/usr/bin/env python3
"""Regression tests for CRO review guardrails.

Run: python3 scripts/test_cro_review_guardrails.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_cro_optimizer import select_relevant_events


def test_meta_variants_stay_pending_without_apply_flag():
    events = [
        (Path("meta.json"), {"type": "cro.meta_variant_proposed", "target_agent": "agent-cro-optimizer"}),
        (Path("asin.json"), {"type": "price.asin_invalid", "target_agent": "agent-cro-optimizer"}),
    ]
    selected, pending_meta = select_relevant_events(events, apply_meta_variants=False)
    assert [event.get("type") for _, event in selected] == ["price.asin_invalid"], selected
    assert [event.get("type") for _, event in pending_meta] == ["cro.meta_variant_proposed"], pending_meta
    print("✓ meta variants stay pending unless --apply-meta-variants is set")


def test_meta_variants_apply_only_with_explicit_flag():
    events = [
        (Path("meta.json"), {"type": "cro.meta_variant_proposed", "target_agent": "agent-cro-optimizer"}),
        (Path("locale.json"), {"type": "cro.asin_missing_locale"}),
    ]
    selected, pending_meta = select_relevant_events(events, apply_meta_variants=True)
    assert [event.get("type") for _, event in selected] == [
        "cro.meta_variant_proposed",
        "cro.asin_missing_locale",
    ], selected
    assert pending_meta == [], pending_meta
    print("✓ meta variants are eligible only with the explicit apply flag")


if __name__ == "__main__":
    tests = [
        test_meta_variants_stay_pending_without_apply_flag,
        test_meta_variants_apply_only_with_explicit_flag,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            print(f"✗ {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:
            print(f"✗ {test.__name__}: unexpected error: {exc}")
            failures += 1
    print(f"\n{'-' * 40}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
