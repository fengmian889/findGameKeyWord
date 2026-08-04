import math
from decimal import Decimal
from fractions import Fraction

import pytest

from poki_seo_monitor.models import KeywordCandidate, SearchSignals
from poki_seo_monitor.scoring import _clamp, _round_score, score_opportunity


KEYWORD = KeywordCandidate("alpha game", "game_name", ("h1",), verified=True)


def test_complete_fast_rising_signal_is_high_priority() -> None:
    result = score_opportunity(
        KEYWORD,
        SearchSignals(80, 60, 30, ("alpha tips",), ("alpha game controls",), 0.2),
        freshness=1.0,
        expansion=0.8,
    )

    assert result.score >= 75
    assert result.confidence == 1.0
    assert result.action == "immediate"


def test_missing_trends_reduces_confidence_not_score_to_zero() -> None:
    result = score_opportunity(
        KEYWORD,
        SearchSignals(autocomplete=("alpha controls",), errors=("trends:limited",)),
        freshness=1.0,
        expansion=0.5,
    )

    assert result.score > 0
    assert result.confidence < 0.7
    assert result.action in {"watch", "hold"}


@pytest.mark.parametrize(
    ("signals", "freshness", "expansion", "expected_score", "expected_action"),
    [
        (SearchSignals(trend_7d=0, competition=0.05, autocomplete=("query",)), 0.0, 0.0, 34, "ignore"),
        (SearchSignals(trend_7d=0, competition=0.0, autocomplete=("query",)), 0.0, 0.0, 35, "hold"),
        (SearchSignals(trend_7d=0, competition=0.0, autocomplete=("query",)), 0.76, 0.0, 54, "hold"),
        (SearchSignals(trend_7d=0, competition=0.0, autocomplete=("query",)), 0.8, 0.0, 55, "watch"),
        (SearchSignals(trend_7d=17, competition=0.0, autocomplete=("query",)), 1.0, 0.9, 74, "watch"),
        (SearchSignals(trend_7d=17, competition=0.0, autocomplete=("query",), autocomplete_observed=True, rising_queries_observed=True), 1.0, 1.0, 75, "immediate"),
    ],
)
def test_action_boundaries_follow_the_documented_thresholds(
    signals: SearchSignals,
    freshness: float,
    expansion: float,
    expected_score: int,
    expected_action: str,
) -> None:
    result = score_opportunity(
        KeywordCandidate("plain game", "game_name", ("h1",)),
        signals,
        freshness=freshness,
        expansion=expansion,
    )

    assert result.score == expected_score
    assert result.action == expected_action


@pytest.mark.parametrize(
    ("signals", "freshness", "expansion", "expected_contribution"),
    [
        (SearchSignals(trend_7d=100, competition=1.0), 0.0, 0.0, 30),
        (SearchSignals(trend_7d=0, competition=1.0), 1.0, 0.0, 25),
        (SearchSignals(trend_7d=0, competition=0.0), 0.0, 0.0, 20),
        (SearchSignals(trend_7d=0, competition=1.0), 0.0, 1.0, 10),
    ],
)
def test_each_weight_can_independently_contribute_its_full_value(
    signals: SearchSignals, freshness: float, expansion: float, expected_contribution: int
) -> None:
    keyword = KeywordCandidate("plain game", "game_name", ("h1",))
    baseline = score_opportunity(
        keyword,
        SearchSignals(trend_7d=0, competition=1.0),
        freshness=0.0,
        expansion=0.0,
    )
    result = score_opportunity(
        keyword,
        signals,
        freshness=freshness,
        expansion=expansion,
    )

    assert result.score - baseline.score == expected_contribution


def test_trend_growth_uses_equal_positive_short_and_long_bonuses() -> None:
    result = score_opportunity(
        KeywordCandidate("plain game", "game_name", ("h1",)),
        SearchSignals(trend_7d=60, trend_30d=40, trend_90d=20, competition=1.0),
        freshness=0.0,
        expansion=0.0,
    )

    # trend = .60 + ((.60 - .40) + (.60 - .20)) / 2 = .90
    assert result.score == 31


def test_trend_declines_do_not_reduce_the_base_strength() -> None:
    declining = score_opportunity(
        KeywordCandidate("plain game", "game_name", ("h1",)),
        SearchSignals(trend_7d=60, trend_30d=80, trend_90d=100, competition=1.0),
        freshness=0.0,
        expansion=0.0,
    )
    baseline = score_opportunity(
        KeywordCandidate("plain game", "game_name", ("h1",)),
        SearchSignals(trend_7d=60, competition=1.0),
        freshness=0.0,
        expansion=0.0,
    )

    assert declining.score == baseline.score == 22


def test_confidence_counts_only_the_six_observed_evidence_slots() -> None:
    result = score_opportunity(
        KEYWORD,
        SearchSignals(
            trend_7d=50,
            trend_90d=30,
            autocomplete=("alpha controls",),
            errors=("serp:limited",),
        ),
        freshness=0.0,
        expansion=0.0,
    )

    assert result.confidence == 0.5


def test_successful_empty_provider_responses_increase_confidence() -> None:
    result = score_opportunity(
        KEYWORD,
        SearchSignals(autocomplete_observed=True, rising_queries_observed=True),
        freshness=0.0,
        expansion=0.0,
    )

    assert result.confidence == 0.33


def test_unknown_keyword_keeps_neutral_priors_but_is_held_for_low_confidence() -> None:
    unknown = score_opportunity(KEYWORD, SearchSignals(), freshness=1.0, expansion=1.0)
    known_poor = score_opportunity(
        KeywordCandidate("alpha game", "game_name", ("h1",)),
        SearchSignals(
            trend_7d=0,
            trend_30d=0,
            trend_90d=0,
            competition=1.0,
            autocomplete_observed=True,
            rising_queries_observed=True,
        ),
        freshness=0.0,
        expansion=0.0,
    )

    assert unknown.score == 65
    assert unknown.confidence == 0.0
    assert unknown.action == "hold"
    assert known_poor.score == 4
    assert known_poor.confidence == 1.0
    assert known_poor.action == "ignore"


def test_confidence_gates_cap_high_raw_scores_without_promoting_lower_tiers() -> None:
    low_confidence = score_opportunity(
        KEYWORD, SearchSignals(trend_7d=100), freshness=1.0, expansion=1.0
    )
    partial_confidence = score_opportunity(
        KEYWORD,
        SearchSignals(trend_7d=100, trend_30d=100, competition=0.0),
        freshness=1.0,
        expansion=1.0,
    )

    assert low_confidence.score >= 75
    assert low_confidence.confidence < 0.34
    assert low_confidence.action == "hold"
    assert partial_confidence.score >= 75
    assert partial_confidence.confidence == 0.5
    assert partial_confidence.action == "watch"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(34.49, 34), (34.5, 35), (54.49, 54), (54.5, 55), (74.49, 74), (74.5, 75)],
)
def test_scores_round_half_up_at_action_boundaries(raw: float, expected: int) -> None:
    assert _round_score(raw) == expected


def test_verified_keyword_is_a_partial_intent_match_without_provider_lists() -> None:
    verified = score_opportunity(
        KEYWORD,
        SearchSignals(trend_7d=0, competition=1.0),
        freshness=0.0,
        expansion=0.0,
    )
    unverified = score_opportunity(
        KeywordCandidate("alpha game", "game_name", ("h1",)),
        SearchSignals(trend_7d=0, competition=1.0),
        freshness=0.0,
        expansion=0.0,
    )
    provider_supported = score_opportunity(
        KeywordCandidate("alpha game", "game_name", ("h1",)),
        SearchSignals(trend_7d=0, competition=1.0, autocomplete=("alpha game",)),
        freshness=0.0,
        expansion=0.0,
    )

    assert verified.score == 9
    assert unverified.score == 4
    assert provider_supported.score == 15
    assert verified.confidence == unverified.confidence == 0.33


def test_values_are_clamped_before_scoring() -> None:
    result = score_opportunity(
        KEYWORD,
        SearchSignals(trend_7d=200, trend_30d=-50, trend_90d=-10, competition=-2),
        freshness=4,
        expansion=-3,
    )

    assert result.score == 84


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "0.5"])
def test_clamp_rejects_booleans_nonfinite_and_nonnumeric_values(value: object) -> None:
    expected = TypeError if isinstance(value, (bool, str)) else ValueError

    with pytest.raises(expected):
        _clamp(value)  # type: ignore[arg-type]


def test_clamp_accepts_finite_decimal_numbers() -> None:
    assert _clamp(Decimal("0.5")) == 0.5  # type: ignore[arg-type]


def test_clamp_rejects_huge_fraction_without_an_overflow_leak() -> None:
    with pytest.raises(ValueError, match="finite"):
        _clamp(Fraction(10**10000, 1))  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, float("nan"), -float("inf"), "50"])
def test_trends_and_competition_reject_invalid_values(value: object) -> None:
    signals = SearchSignals(trend_7d=value, competition=value)  # type: ignore[arg-type]

    with pytest.raises((TypeError, ValueError)):
        score_opportunity(KEYWORD, signals, freshness=0.5, expansion=0.5)


def test_boolean_trend_is_rejected_before_normalization() -> None:
    with pytest.raises(TypeError):
        score_opportunity(
            KEYWORD,
            SearchSignals(trend_7d=True),  # type: ignore[arg-type]
            freshness=0.5,
            expansion=0.5,
        )


def test_scoring_is_deterministic_and_does_not_mutate_its_inputs() -> None:
    keyword = KeywordCandidate("alpha game", "game_name", ["h1"], verified=True)
    signals = SearchSignals(80, 60, 30, ["rise"], ["auto"], 0.2)
    before_keyword = keyword
    before_signals = signals

    first = score_opportunity(keyword, signals, freshness=1.0, expansion=0.8)
    second = score_opportunity(keyword, signals, freshness=1.0, expansion=0.8)

    assert first == second
    assert first.keyword is keyword
    assert first.signals is signals
    assert keyword == before_keyword
    assert signals == before_signals
