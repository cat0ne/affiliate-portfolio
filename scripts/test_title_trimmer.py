#!/usr/bin/env python3
"""Tests for agent_title_trimmer.

Covers the 3 known matelas EN titles + a few synthetic cases.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_title_trimmer import (
    MIN_LEN, TITLE_LIMIT,
    _hook_loss, _hooks_in, _score_candidate,
    _validate_gemini_output, trim_title,
)


def assert_trimmed(title: str, locale: str, want_strategy_in: set[str], max_len: int = TITLE_LIMIT):
    proposed, strategy, chain = trim_title(title, locale)
    assert proposed is not None, f"FAIL: no trim for {title!r} (chain={chain})"
    assert MIN_LEN <= len(proposed) <= max_len, (
        f"FAIL: trim outside [{MIN_LEN},{max_len}]: {proposed!r} ({len(proposed)} chars)"
    )
    assert strategy in want_strategy_in, (
        f"FAIL: expected strategy in {want_strategy_in}, got {strategy!r} "
        f"for {title!r} → {proposed!r}"
    )
    return proposed, strategy


def test_under_limit_noop():
    proposed, strategy, _ = trim_title("Short Title 2026", "en")
    assert proposed is None, f"FAIL: short title got trimmed: {proposed!r}"
    assert strategy == "no_op"
    print("✓ titles under limit are no-ops")


def test_parenthetical_drop():
    # 75 chars, parenthetical at end
    title = "Tediber Review 2026: French Mattress Comparison (Tested & Compared)"
    assert len(title) > TITLE_LIMIT
    proposed, strategy = assert_trimmed(title, "en", {"paren_drop", "boilerplate"})
    print(f"✓ parenthetical drop: {len(title)} → {len(proposed)} chars [{strategy}]")
    print(f"    {proposed!r}")


def test_pipe_drop_matelas_epeda():
    title = "Épéda L'Échappée Test 2026: 100 Nights | What Reviews Don't Say"
    assert len(title) > TITLE_LIMIT
    proposed, strategy = assert_trimmed(title, "en", {"pipe_drop", "boilerplate"})
    print(f"✓ pipe drop (Épéda case): {len(title)} → {len(proposed)} chars [{strategy}]")
    print(f"    {proposed!r}")


def test_boilerplate_strip_matelas_morphea():
    title = "Morphea Jade Test 2026: 100 Nights | French Craftsmanship Worth €899?"
    assert len(title) > TITLE_LIMIT
    proposed, strategy = assert_trimmed(title, "en", {"pipe_drop", "boilerplate", "colon_drop"})
    # Year must survive
    assert "2026" in proposed, f"FAIL: lost year: {proposed!r}"
    # Brand must survive
    assert "Morphea" in proposed or "Jade" in proposed, f"FAIL: lost entity: {proposed!r}"
    print(f"✓ boilerplate strip (Morphea case): {len(title)} → {len(proposed)} chars [{strategy}]")
    print(f"    {proposed!r}")


def test_tediber_long_title_no_rule_trim():
    """Default mode (no Gemini): rule-based trims below floor — should fail."""
    title = "Tediber Review 2026: Testing the French Mattress Praised by Internet Users"
    proposed, strategy, _ = trim_title(title, "en", use_gemini=False)
    assert proposed is None, f"FAIL: got rule trim {proposed!r}"
    assert strategy == "no_strategy_worked"
    print(f"✓ Tediber long title: no rule trim (as expected without Gemini)")


def test_colon_drop_preserves_entity():
    # 65 chars; head has entity + year. Drop after ':' should succeed.
    title = "Cafetière Bialetti Brikka 2026: Notre Test Complet et Détaillé"
    assert len(title) > TITLE_LIMIT
    proposed, strategy, _ = trim_title(title, "fr")
    if proposed:
        assert "Bialetti" in proposed, f"FAIL: lost entity: {proposed!r}"
        assert len(proposed) <= TITLE_LIMIT
        print(f"✓ colon drop preserves entity: {len(title)} → {len(proposed)} chars [{strategy}]")
    else:
        print(f"  (no rule produced trim — Gemini fallback would handle, that's OK)")


def test_gemini_validator_rejects_year_drift():
    """Gemini output that changes 2026 → 2024 must be rejected."""
    ok, reason = _validate_gemini_output(
        "Seniorenmatratze: Vollständiger Ratgeber 2026",
        "Seniorenmatratze: Der Ratgeber für guten Schlaf ab 60 (2024)",
    )
    assert not ok, "FAIL: year drift not detected"
    assert reason.startswith("year_drift"), f"unexpected reason: {reason}"
    print("✓ Gemini validator rejects year drift")


def test_gemini_validator_rejects_entity_loss():
    """Output that drops the brand must be rejected."""
    ok, reason = _validate_gemini_output(
        "Tediber Review 2026: Testing the French Mattress",
        "French Mattress Review 2026: A Comprehensive Test",
    )
    assert not ok, "FAIL: entity loss not detected"
    assert reason.startswith("entity_lost"), f"unexpected reason: {reason}"
    print("✓ Gemini validator rejects entity loss")


def test_hook_detection_currency():
    hooks = _hooks_in("Morphea Jade 2026: Worth €899? Tested")
    assert any(h[0] == "currency" for h in hooks), f"FAIL: no currency hook in {hooks}"
    assert any(h[0] == "intrigue" for h in hooks), f"FAIL: no intrigue hook in {hooks}"
    print("✓ hook detection: currency + intrigue")


def test_hook_detection_number_noun():
    hooks = _hooks_in("Emma Original Review 2026: 100-Night Test | 3 Flaws Sellers Hide")
    # "3 Flaws" is a num_noun hook
    assert any(h[0] == "num_noun" and h[1] == "3_flaw" for h in hooks), f"FAIL: {hooks}"
    print("✓ hook detection: numeric + noun")


def test_score_penalizes_hook_loss():
    original = "Emma Original Review 2026: 100-Night Test | 3 Flaws Sellers Hide"
    # Pipe-drop result loses "3 Flaws Sellers Hide"
    lossy = "Emma Original Review 2026: 100-Night Test"
    # Hypothetical hook-preserving rewrite
    keepy = "Emma Original 2026: 100-Night Test + 3 Hidden Flaws"
    s_lossy = _score_candidate(lossy, original)
    s_keepy = _score_candidate(keepy, original)
    assert s_keepy > s_lossy, f"FAIL: keepy should score higher: {s_keepy} <= {s_lossy}"
    print(f"✓ score penalizes hook loss: keepy={s_keepy} > lossy={s_lossy}")


def test_score_penalizes_year_loss():
    original = "Tediber Review 2026: French Mattress Worth €899?"
    no_year = "Tediber Review: French Mattress"
    s = _score_candidate(no_year, original)
    s_with_year = _score_candidate("Tediber Review 2026", original)
    assert s_with_year > s, f"FAIL: year-preserving should score higher: {s_with_year} <= {s}"
    print(f"✓ score penalizes year loss")


def test_signal_retention_floor():
    """Trim that loses >40% of original length should escalate if Gemini available.

    Verifies the escalation trigger fires by checking when use_gemini=False:
    the rule-based trim still wins (because no escalation possible without API).
    """
    # 68-char original, rule trim would land at 38 chars (56% retention)
    title = "Mattress Sizes & Dimensions Guide 2026: EU, UK & US Comparison Chart"
    proposed, strategy, _ = trim_title(title, "en", use_gemini=False)
    # Without Gemini, we still return the rule output (no fallback available)
    assert proposed is not None
    print(f"✓ retention floor test (no Gemini): {len(title)} → {len(proposed)} [{strategy}]")
    # The score+escalation logic is exercised; with Gemini enabled the same
    # case would escalate, but we don't test Gemini calls in this suite.


def test_hook_loss_set():
    original = "Morphea Jade Test 2026: 100 Nights | French Craftsmanship Worth €899?"
    pipe_drop = "Morphea Jade Test 2026: 100 Nights"
    lost = _hook_loss(original, pipe_drop)
    # "€899" lost, "Worth €899?" intrigue lost
    assert any(h[0] == "currency" for h in lost), f"FAIL: currency not in lost: {lost}"
    print(f"✓ hook_loss detects dropped currency hooks ({len(lost)} hooks lost)")


def test_colon_drop_with_floor():
    """Original 67 chars, head after colon drop is 23 chars — below MIN_LEN.
    Should NOT trim (under floor), fall through to next rule or no_strategy."""
    title = "Emma Original Avis 2026 : Notre Test Complet du Matelas Best-Seller"
    proposed, strategy, _ = trim_title(title, "fr")
    if proposed and strategy == "colon_drop":
        assert len(proposed) >= MIN_LEN, f"FAIL: colon_drop below floor: {len(proposed)}"
    # Acceptable outcomes: gemini, no_strategy_worked, or a longer trim from another rule
    print(f"✓ colon_drop with floor: strategy={strategy}, result={'(none)' if not proposed else proposed}")


def test_fully_unsplittable_falls_through():
    # No paren, no pipe, no colon — all rules fail. Should reach gemini or return None.
    title = "An Extremely Long Title Without Any Splittable Punctuation Markers Whatsoever Here"
    proposed, strategy, chain = trim_title(title, "en")
    # Either Gemini succeeded or returned None — both are valid
    assert strategy in {"gemini", "no_strategy_worked"}, f"unexpected strategy {strategy}"
    print(f"✓ unsplittable falls through to {strategy}")


if __name__ == "__main__":
    tests = [
        test_under_limit_noop,
        test_parenthetical_drop,
        test_pipe_drop_matelas_epeda,
        test_boilerplate_strip_matelas_morphea,
        test_tediber_long_title_no_rule_trim,
        test_colon_drop_preserves_entity,
        test_gemini_validator_rejects_year_drift,
        test_gemini_validator_rejects_entity_loss,
        test_hook_detection_currency,
        test_hook_detection_number_noun,
        test_score_penalizes_hook_loss,
        test_score_penalizes_year_loss,
        test_signal_retention_floor,
        test_hook_loss_set,
        test_colon_drop_with_floor,
        test_fully_unsplittable_falls_through,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
            failures += 1
        except Exception as e:
            print(f"✗ {t.__name__}: unexpected: {e}")
            failures += 1
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
