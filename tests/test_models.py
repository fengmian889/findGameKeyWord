from dataclasses import FrozenInstanceError
from typing import get_type_hints

import pytest

from poki_seo_monitor.models import (
    DiscoveredGame,
    GamePage,
    KeywordCandidate,
    Opportunity,
    SearchSignals,
    to_dict,
)


def test_search_signal_collections_are_string_tuples() -> None:
    hints = get_type_hints(SearchSignals)

    assert hints["rising_queries"] == tuple[str, ...]
    assert hints["autocomplete"] == tuple[str, ...]
    assert hints["errors"] == tuple[str, ...]
    assert hints["autocomplete_observed"] is bool
    assert hints["rising_queries_observed"] is bool


def test_to_dict_serializes_nested_dataclasses() -> None:
    opportunity = Opportunity(
        keyword=KeywordCandidate(
            phrase="goal heads", group="game_name", evidence=("page",), verified=True
        ),
        signals=SearchSignals(trend_7d=2.5, rising_queries=("goal heads game",)),
        score=87,
        confidence=0.9,
        action="publish",
    )

    assert to_dict(opportunity) == {
        "keyword": {
            "phrase": "goal heads",
            "group": "game_name",
            "evidence": ("page",),
            "verified": True,
        },
        "signals": {
            "trend_7d": 2.5,
            "trend_30d": None,
            "trend_90d": None,
            "rising_queries": ("goal heads game",),
            "autocomplete": (),
            "competition": None,
            "errors": (),
            "autocomplete_observed": False,
            "rising_queries_observed": False,
        },
        "score": 87,
        "confidence": 0.9,
        "action": "publish",
    }


def test_models_are_frozen() -> None:
    candidate = KeywordCandidate("goal heads", "game_name", ("page",))

    with pytest.raises(FrozenInstanceError):
        candidate.verified = True  # type: ignore[misc]


def test_models_detach_tuple_fields_from_mutable_inputs() -> None:
    sources = ["sitemap"]
    categories = ["action"]
    related_games = ["related"]
    evidence = ["page"]
    rising_queries = ["rising"]
    autocomplete = ["auto"]
    errors = ["error"]
    discovered = DiscoveredGame("https://poki.com/en/g/game", sources)
    page = GamePage(
        "https://poki.com/en/g/game",
        "game",
        "Game",
        "Game title",
        "Game description",
        "Game body",
        categories,
        related_games=related_games,
    )
    candidate = KeywordCandidate("game", "game_name", evidence)
    signals = SearchSignals(
        rising_queries=rising_queries, autocomplete=autocomplete, errors=errors
    )

    for values in (
        sources,
        categories,
        related_games,
        evidence,
        rising_queries,
        autocomplete,
        errors,
    ):
        values.append("mutated")

    assert discovered.sources == ("sitemap",)
    assert page.categories == ("action",)
    assert page.related_games == ("related",)
    assert candidate.evidence == ("page",)
    assert signals.rising_queries == ("rising",)
    assert signals.autocomplete == ("auto",)
    assert signals.errors == ("error",)


@pytest.mark.parametrize("field", ["autocomplete_observed", "rising_queries_observed"])
def test_search_signal_observation_flags_require_exact_booleans(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        SearchSignals(**{field: 1})  # type: ignore[arg-type]
