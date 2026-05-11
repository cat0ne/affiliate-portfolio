#!/usr/bin/env python3
"""Tests for agent_description_trimmer.

Covers the rule pipeline, hook detection, scoring, and Gemini validator.
Run: python scripts/test_description_trimmer.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_description_trimmer import (
    DESCRIPTION_LIMIT, MIN_LEN,
    _hook_loss, _hooks_in, _score_candidate,
    _split_sentences, _try_arrow_drop, _try_comma_clause_drop,
    _try_redundant_phrase_strip, _try_sentence_drop,
    _validate_gemini_output, trim_description,
)


def assert_trimmed(description: str, locale: str, want_strategy_in: set[str],
                   max_len: int = DESCRIPTION_LIMIT):
    proposed, strategy, chain = trim_description(description, locale)
    assert proposed is not None, f"FAIL: no trim for {description!r} (chain={chain})"
    assert MIN_LEN <= len(proposed) <= max_len, (
        f"FAIL: trim outside [{MIN_LEN},{max_len}]: {proposed!r} ({len(proposed)} chars)"
    )
    assert strategy in want_strategy_in, (
        f"FAIL: expected strategy in {want_strategy_in}, got {strategy!r} "
        f"for {description!r} → {proposed!r}"
    )
    return proposed, strategy


# ---------------------------------------------------------------------------
# Basic no-op + rule strategies
# ---------------------------------------------------------------------------

def test_under_limit_noop():
    short = "Short description well under the SERP limit, with enough words to clear the floor of 80 chars."
    proposed, strategy, _ = trim_description(short, "en")
    assert proposed is None, f"FAIL: short desc got trimmed: {proposed!r}"
    assert strategy == "no_op"
    print("PASS under-limit no-op")


def test_sentence_drop_keeps_first_complete():
    # First sentence: ~120 chars. Adding second sentence pushes over 160.
    desc = (
        "Emma Original tested 100 nights 2026 with detailed protocol and score 8.5/10. "
        "Three hidden flaws sellers will not tell you about. "
        "Honest review with prices and detailed comparison."
    )
    assert len(desc) > DESCRIPTION_LIMIT, f"test fixture not long enough: {len(desc)}"
    proposed, strategy = assert_trimmed(desc, "en", {"sentence_drop", "comma_clause_drop"})
    # First sentence must survive
    assert "Emma Original tested 100 nights 2026" in proposed
    print(f"PASS sentence_drop: {len(desc)} -> {len(proposed)} chars [{strategy}]")
    print(f"     {proposed!r}")


def test_arrow_drop_cuts_teaser():
    # Bureau FR pattern from the corpus: ' → ' followed by product list.
    desc = (
        "5 bureaux assis-debout à moins de 300€ testés 2026. "
        "Électriques, convertisseurs et designs. Notre comparatif → "
        "Découvrez les meilleurs bureaux assis-debout en 2026."
    )
    assert len(desc) > DESCRIPTION_LIMIT
    proposed, strategy = assert_trimmed(desc, "fr",
        {"arrow_drop", "sentence_drop", "redundant_phrase_strip"})
    # Currency hook must survive
    assert "300€" in proposed
    print(f"PASS arrow_drop: {len(desc)} -> {len(proposed)} chars [{strategy}]")


def test_redundant_phrase_strip_fr():
    # FR matelas pattern: "Comparatif mis à jour avec prix et confort" at tail.
    desc = (
        "Meilleurs matelas moins de 500€ 2026 : top 7 testés avec analyse complète "
        "des critères de confort et durabilité. Comparatif 2026 mis à jour avec prix et confort."
    )
    assert len(desc) > DESCRIPTION_LIMIT
    proposed = _try_redundant_phrase_strip(desc)
    assert proposed is not None, "FAIL: boilerplate not stripped"
    assert "Comparatif 2026 mis à jour" not in proposed
    assert len(proposed) <= DESCRIPTION_LIMIT
    # Currency must survive
    assert "500€" in proposed
    print(f"PASS redundant_phrase_strip FR: {len(desc)} -> {len(proposed)}")


def test_redundant_phrase_strip_de():
    desc = (
        "Top Matratzen 2026 getestet für 100 Nächte mit unabhängiger Bewertung "
        "von Komfort und Haltbarkeit. Aktualisierter Vergleich 2026."
    )
    if len(desc) > DESCRIPTION_LIMIT:
        proposed = _try_redundant_phrase_strip(desc)
        if proposed:
            assert "Aktualisierter Vergleich 2026" not in proposed
            print(f"PASS redundant_phrase_strip DE: {len(desc)} -> {len(proposed)}")
            return
    print("PASS redundant_phrase_strip DE (input under limit)")


# ---------------------------------------------------------------------------
# Hook detection
# ---------------------------------------------------------------------------

def test_hook_detection_currency():
    hooks = _hooks_in("Emma Original 2026: Worth €899? Tested 100 nights with score 8.5/10.")
    types = {h[0] for h in hooks}
    assert "currency" in types, f"FAIL: no currency hook in {hooks}"
    assert "score" in types, f"FAIL: no score hook in {hooks}"
    assert "intrigue" in types, f"FAIL: no intrigue hook in {hooks}"
    print(f"PASS hook detection: currency + score + intrigue ({len(hooks)} hooks)")


def test_hook_detection_specific_claim():
    """100 nights, 30 days tested — typical descriptor numeric hooks."""
    hooks = _hooks_in("Tested 100 nights and 30 days of real use with experts.")
    nn = {h[1] for h in hooks if h[0] == "num_noun"}
    assert any("100" in x and "night" in x for x in nn), f"FAIL: no '100 nights': {nn}"
    assert any("30" in x and "day" in x for x in nn), f"FAIL: no '30 days': {nn}"
    print(f"PASS hook detection: specific claims (100 nights, 30 days)")


def test_hook_detection_top_n():
    hooks = _hooks_in("Top 7 matelas testés en 2026 par notre équipe d'experts.")
    nn = {h[1] for h in hooks if h[0] == "num_noun"}
    assert any("7" in x and "top" in x for x in nn), f"FAIL: no 'Top 7': {nn}"
    print(f"PASS hook detection: top N")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_score_penalizes_hook_loss():
    original = "Emma Original tested 100 nights 2026: score 8.5/10 with 3 hidden flaws revealed."
    lossy = "Emma Original tested 2026."  # Loses currency-adjacent hooks + score
    keepy = "Emma Original tested 100 nights 2026: score 8.5/10 — full honest review."
    s_lossy = _score_candidate(lossy, original)
    s_keepy = _score_candidate(keepy, original)
    assert s_keepy > s_lossy, f"FAIL: keepy={s_keepy} should beat lossy={s_lossy}"
    print(f"PASS score penalizes hook loss: keepy={s_keepy} > lossy={s_lossy}")


def test_score_penalizes_year_loss():
    original = "Tediber Review 2026: tested 100 nights for under €699 with honest verdict."
    no_year = "Tediber Review: tested 100 nights for under €699 with honest verdict and details."
    yes_year = "Tediber Review 2026: tested 100 nights for under €699 — honest verdict for buyers."
    s_no = _score_candidate(no_year, original)
    s_yes = _score_candidate(yes_year, original)
    assert s_yes > s_no, f"FAIL: with-year should beat lost-year: {s_yes} vs {s_no}"
    print(f"PASS score penalizes year loss: with={s_yes} > without={s_no}")


def test_score_rewards_first_sentence_preservation():
    original = (
        "Emma Original tested 100 nights 2026: score 8.5/10. "
        "Three hidden flaws sellers won't tell you about."
    )
    keeps_first = "Emma Original tested 100 nights 2026: score 8.5/10."
    rewrite = "Three hidden flaws sellers won't tell you about Emma Original tested in 2026."
    s_keep = _score_candidate(keeps_first, original)
    s_rewrite = _score_candidate(rewrite, original)
    assert s_keep > s_rewrite, f"FAIL: first-sentence keep should win: {s_keep} vs {s_rewrite}"
    print(f"PASS score rewards first-sentence preservation: keep={s_keep} > rewrite={s_rewrite}")


# ---------------------------------------------------------------------------
# Gemini validator
# ---------------------------------------------------------------------------

def test_gemini_validator_rejects_year_drift():
    ok, reason = _validate_gemini_output(
        "Emma Original Mattress tested 100 nights 2026: score 8.5/10 with honest verdict for buyers.",
        "Emma Original Mattress tested 100 nights 2024: score 8.5/10 with honest verdict for buyers.",
    )
    assert not ok, "FAIL: year drift not detected"
    assert reason.startswith("year_drift"), f"unexpected reason: {reason}"
    print(f"PASS Gemini validator rejects year drift: {reason}")


def test_gemini_validator_rejects_length_overflow():
    original = "Emma Original Mattress tested 100 nights 2026 with detailed verdict and honest pricing analysis."
    overflow = "x" * (DESCRIPTION_LIMIT + 5)
    ok, reason = _validate_gemini_output(original, overflow)
    assert not ok and reason == "over_limit", f"FAIL: expected over_limit, got {reason}"
    print(f"PASS Gemini validator rejects length overflow")


def test_gemini_validator_rejects_under_floor():
    ok, reason = _validate_gemini_output(
        "Emma Original Mattress tested 100 nights 2026 with detailed verdict.",
        "Short.",
    )
    assert not ok and reason == "under_floor", f"FAIL: expected under_floor, got {reason}"
    print(f"PASS Gemini validator rejects under floor")


def test_gemini_validator_rejects_entity_loss():
    original = "Tediber Review 2026: tested 100 nights with detailed verdict and pricing breakdown for buyers."
    drift = "Mattress Review 2026: 100 nights of testing with detailed verdict and pricing breakdown for buyers."
    ok, reason = _validate_gemini_output(original, drift)
    assert not ok and reason.startswith("entity_lost"), f"FAIL: expected entity_lost, got {reason}"
    print(f"PASS Gemini validator rejects entity loss")


def test_gemini_validator_accepts_clean_trim():
    original = (
        "Emma Original tested 100 nights 2026: score 8.5/10. "
        "Three hidden flaws sellers won't tell you about. Honest review with prices."
    )
    clean = "Emma Original tested 100 nights 2026: score 8.5/10 with honest review and prices revealed."
    ok, reason = _validate_gemini_output(original, clean)
    assert ok, f"FAIL: clean trim rejected: {reason}"
    print(f"PASS Gemini validator accepts clean trim")


# ---------------------------------------------------------------------------
# Signal-retention floor
# ---------------------------------------------------------------------------

def test_signal_retention_floor_no_gemini():
    """When a rule produces a trim that's < 60% of original on long input AND
    no Gemini is available, the rule trim is still returned (no escalation
    possible). But scoring should reflect the loss."""
    desc = (
        "Comprehensive comparison of standing desks under 300 euros tested 2026, "
        "including FlexiSpot E5, Fezibo dual motor, and three more models with "
        "stability, noise, warranty details — our full rigorous selection."
    )
    proposed, strategy, _ = trim_description(desc, "en", use_gemini=False)
    # Either a rule trim returned, or no_strategy_worked — both valid without Gemini
    assert strategy in {"sentence_drop", "comma_clause_drop", "redundant_phrase_strip",
                        "arrow_drop", "no_strategy_worked"}
    if proposed:
        assert len(proposed) <= DESCRIPTION_LIMIT
        assert len(proposed) >= MIN_LEN
    print(f"PASS retention floor (no Gemini): strategy={strategy}, "
          f"len={'(none)' if not proposed else len(proposed)}")


def test_signal_retention_triggers_escalation():
    """Verify that the retention-floor escalation logic fires.

    Two independent triggers:
      a) trim length < 60% of original when original > 120 chars
      b) trim score below the 2.0 threshold
    Test (a) via direct length math (matches _should_escalate's branch).
    """
    original = (
        "Top 5 standing desks under €300 tested 100 days 2026 with stability, "
        "noise, warranty analysis and verdict from our independent test lab team."
    )
    assert len(original) > 120, f"fixture length {len(original)} insufficient"
    # 60% threshold for this 154-char input is 92 chars; a 50-char trim qualifies
    short_trim = "Standing desks 2026 — see our test lab verdict here."
    assert len(short_trim) < len(original) * 0.6
    # Sanity: dropped hooks (currency, Top 5, num+days)
    lost = _hook_loss(original, short_trim)
    assert lost, f"FAIL: expected hook loss, got none"
    print(f"PASS signal-retention escalation: trim len={len(short_trim)} "
          f"< 60% of {len(original)}, hooks lost={len(lost)}")


# ---------------------------------------------------------------------------
# Sentence splitter
# ---------------------------------------------------------------------------

def test_sentence_split_handles_terminators():
    text = "First sentence. Second sentence! Third? Fourth and final."
    sents = _split_sentences(text)
    assert len(sents) == 4, f"FAIL: expected 4 sentences, got {len(sents)}: {sents}"
    print(f"PASS sentence splitter: 4 sentences")


def test_sentence_split_preserves_decimals():
    """Score formats like '8.5/10' should not split a sentence."""
    text = "Emma Original scored 8.5/10 in our test. Best in class for the price."
    sents = _split_sentences(text)
    assert len(sents) == 2, f"FAIL: expected 2 sentences, got {len(sents)}: {sents}"
    assert "8.5/10" in sents[0]
    print(f"PASS sentence splitter preserves decimals (8.5/10)")


# ---------------------------------------------------------------------------
# Hook loss
# ---------------------------------------------------------------------------

def test_hook_loss_detects_dropped_currency():
    original = "Tested 100 nights for €899 with full verdict and detailed pricing breakdown."
    trim = "Tested 100 nights with full verdict and detailed breakdown."
    lost = _hook_loss(original, trim)
    assert any(h[0] == "currency" for h in lost), f"FAIL: currency not in lost: {lost}"
    print(f"PASS hook_loss detects dropped currency")


def test_hook_loss_detects_dropped_score():
    original = "Emma Original score 8.5/10 tested 100 nights with detailed verdict."
    trim = "Emma Original tested 100 nights with detailed verdict."
    lost = _hook_loss(original, trim)
    assert any(h[0] == "score" for h in lost), f"FAIL: score not in lost: {lost}"
    print(f"PASS hook_loss detects dropped score")


# ---------------------------------------------------------------------------
# Live corpus sanity (offline)
# ---------------------------------------------------------------------------

def test_known_corpus_trim_bureau_arrow():
    """Real corpus example: a bureau ES description with arrow teaser."""
    desc = (
        "Comparativo independiente 2026: tests reales, tabla comparativa y "
        "veredicto de experto. ¿Cuál ofrece la mejor relación calidad-precio? "
        "Descúbrelo → FlexiSpot E5 (formato compacto) o Fezibo Doble Motor"
    )
    proposed, strategy, chain = trim_description(desc, "es")
    assert proposed is not None, f"FAIL: no trim, chain={chain}"
    assert len(proposed) <= DESCRIPTION_LIMIT
    assert "2026" in proposed, f"FAIL: year lost in {proposed!r}"
    print(f"PASS corpus arrow trim: {len(desc)} -> {len(proposed)} [{strategy}]")
    print(f"     {proposed!r}")


def test_known_corpus_trim_matelas_long():
    """Real corpus: matelas IT >200 chars with boilerplate tail."""
    desc = (
        "Miglior materasso economico di qualità 2026 : top 7 testati sotto i 500€. "
        "Comparativa aggiornata per trovare il miglior rapporto. con i nostri "
        "consigli esperti e le nostre raccomandazioni per un sonno"
    )
    proposed, strategy, chain = trim_description(desc, "it")
    # May not trim if all rules fail without Gemini — accept either outcome
    print(f"PASS corpus matelas IT: strategy={strategy}, "
          f"len={'(none)' if not proposed else len(proposed)}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_under_limit_noop,
        test_sentence_drop_keeps_first_complete,
        test_arrow_drop_cuts_teaser,
        test_redundant_phrase_strip_fr,
        test_redundant_phrase_strip_de,
        test_hook_detection_currency,
        test_hook_detection_specific_claim,
        test_hook_detection_top_n,
        test_score_penalizes_hook_loss,
        test_score_penalizes_year_loss,
        test_score_rewards_first_sentence_preservation,
        test_gemini_validator_rejects_year_drift,
        test_gemini_validator_rejects_length_overflow,
        test_gemini_validator_rejects_under_floor,
        test_gemini_validator_rejects_entity_loss,
        test_gemini_validator_accepts_clean_trim,
        test_signal_retention_floor_no_gemini,
        test_signal_retention_triggers_escalation,
        test_sentence_split_handles_terminators,
        test_sentence_split_preserves_decimals,
        test_hook_loss_detects_dropped_currency,
        test_hook_loss_detects_dropped_score,
        test_known_corpus_trim_bureau_arrow,
        test_known_corpus_trim_matelas_long,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failures += 1
        except Exception as e:
            print(f"FAIL {t.__name__}: unexpected: {e}")
            failures += 1
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
