"""Trend Opportunity Score calculation."""

import math
from numbers import Number

from .models import KeywordCandidate, Opportunity, SearchSignals


WEIGHT_TREND = 30
WEIGHT_FRESHNESS = 25
WEIGHT_COMPETITION_GAP = 20
WEIGHT_INTENT = 15
WEIGHT_EXPANSION = 10

MISSING_TREND_PRIOR = 0.35
MISSING_COMPETITION_PRIOR = 0.5
PROVIDER_INTENT_PRIOR = 1.0
VERIFIED_INTENT_PRIOR = 0.6
UNVERIFIED_INTENT_PRIOR = 0.25

IMMEDIATE_SCORE_THRESHOLD = 75
WATCH_SCORE_THRESHOLD = 55
HOLD_SCORE_THRESHOLD = 35
LOW_CONFIDENCE_THRESHOLD = 0.34
MEDIUM_CONFIDENCE_THRESHOLD = 0.67
CONFIDENCE_EVIDENCE_SLOTS = 6

_ACTION_TIERS = ("ignore", "hold", "watch", "immediate")


def _clamp(value: float) -> float:
    """Return a finite real value limited to the inclusive 0.0--1.0 range."""
    return max(0.0, min(1.0, _finite_real(value)))


def _finite_real(value: object) -> float:
    """Validate a scalar score input without coercing booleans to numbers."""
    if isinstance(value, bool) or not isinstance(value, Number):
        raise TypeError("score inputs must be real numbers, not booleans")
    try:
        numeric = float(value)
    except OverflowError as error:
        raise ValueError("score inputs must be finite") from error
    except (TypeError, ValueError) as error:
        raise TypeError("score inputs must be real numbers, not booleans") from error
    if not math.isfinite(numeric):
        raise ValueError("score inputs must be finite")
    return numeric


def _normalized_trend(value: float | None) -> float | None:
    """Validate a Trends value and normalize its documented 0--100 scale."""
    if value is None:
        return None
    return _clamp(_finite_real(value) / 100.0)


def _action(score: int) -> str:
    if score >= IMMEDIATE_SCORE_THRESHOLD:
        return "immediate"
    if score >= WATCH_SCORE_THRESHOLD:
        return "watch"
    if score >= HOLD_SCORE_THRESHOLD:
        return "hold"
    return "ignore"


def _round_score(raw_score: float) -> int:
    """Round a nonnegative score with .5 values always rounded upward."""
    return math.floor(raw_score + 0.5)


def _confidence_gated_action(score: int, confidence: float) -> str:
    """Cap the score action by evidence coverage without ever promoting it."""
    score_tier = _ACTION_TIERS.index(_action(score))
    confidence_tier = (
        _ACTION_TIERS.index("hold")
        if confidence < LOW_CONFIDENCE_THRESHOLD
        else _ACTION_TIERS.index("watch")
        if confidence < MEDIUM_CONFIDENCE_THRESHOLD
        else _ACTION_TIERS.index("immediate")
    )
    return _ACTION_TIERS[min(score_tier, confidence_tier)]


def score_opportunity(
    keyword: KeywordCandidate,
    signals: SearchSignals,
    freshness: float,
    expansion: float,
) -> Opportunity:
    """Score an immutable keyword opportunity from normalized evidence.

    The score is ``round_half_up(30*trend + 25*freshness + 20*competition_gap
    + 15*intent + 10*expansion)``.  Trend starts with the clamped 7-day
    value.  When present, its positive short and long bonuses are the average
    of ``max(0, trend_7d - trend_30d)`` and
    ``max(0, trend_7d - trend_90d)`` after each value is normalized to 0--1;
    the resulting trend is capped at 1.0.  A missing 7-day value uses 0.35.
    Confidence gates the recommendation: below .34 the action is at most
    ``hold``; below .67 it is at most ``watch``.
    """
    trend_7d = _normalized_trend(signals.trend_7d)
    trend_30d = _normalized_trend(signals.trend_30d)
    trend_90d = _normalized_trend(signals.trend_90d)

    if trend_7d is None:
        trend = MISSING_TREND_PRIOR
    else:
        short_growth = 0.0 if trend_30d is None else max(0.0, trend_7d - trend_30d)
        long_growth = 0.0 if trend_90d is None else max(0.0, trend_7d - trend_90d)
        trend = _clamp(trend_7d + (short_growth + long_growth) / 2.0)

    competition_gap = (
        MISSING_COMPETITION_PRIOR
        if signals.competition is None
        else 1.0 - _clamp(signals.competition)
    )
    intent = (
        PROVIDER_INTENT_PRIOR
        if signals.autocomplete or signals.rising_queries
        else VERIFIED_INTENT_PRIOR
        if keyword.verified
        else UNVERIFIED_INTENT_PRIOR
    )
    score = _round_score(
        WEIGHT_TREND * trend
        + WEIGHT_FRESHNESS * _clamp(freshness)
        + WEIGHT_COMPETITION_GAP * competition_gap
        + WEIGHT_INTENT * intent
        + WEIGHT_EXPANSION * _clamp(expansion)
    )
    observed = sum(
        value is not None
        for value in (
            signals.trend_7d,
            signals.trend_30d,
            signals.trend_90d,
            signals.competition,
        )
    )
    autocomplete_available = bool(signals.autocomplete) or signals.autocomplete_observed
    rising_queries_available = (
        bool(signals.rising_queries) or signals.rising_queries_observed
    )
    confidence = round(
        (observed + autocomplete_available + rising_queries_available)
        / CONFIDENCE_EVIDENCE_SLOTS,
        2,
    )
    return Opportunity(keyword, signals, score, confidence, _confidence_gated_action(score, confidence))
