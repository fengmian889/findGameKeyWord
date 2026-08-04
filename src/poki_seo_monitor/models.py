"""Immutable domain models for the Poki SEO monitor."""

from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class DiscoveredGame:
    url: str
    sources: tuple[str, ...]
    source_rank: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class GamePage:
    url: str
    slug: str
    name: str
    title: str
    description: str
    body: str
    categories: tuple[str, ...] = ()
    developer: str | None = None
    related_games: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "categories", tuple(self.categories))
        object.__setattr__(self, "related_games", tuple(self.related_games))


@dataclass(frozen=True)
class KeywordCandidate:
    phrase: str
    group: Literal["game_name", "category", "long_tail"]
    evidence: tuple[str, ...]
    verified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class SearchSignals:
    trend_7d: float | None = None
    trend_30d: float | None = None
    trend_90d: float | None = None
    rising_queries: tuple[str, ...] = ()
    autocomplete: tuple[str, ...] = ()
    competition: float | None = None
    errors: tuple[str, ...] = ()
    autocomplete_observed: bool = False
    rising_queries_observed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "rising_queries", tuple(self.rising_queries))
        object.__setattr__(self, "autocomplete", tuple(self.autocomplete))
        object.__setattr__(self, "errors", tuple(self.errors))
        for field in ("autocomplete_observed", "rising_queries_observed"):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be a bool")


@dataclass(frozen=True)
class Opportunity:
    keyword: KeywordCandidate
    signals: SearchSignals
    score: int
    confidence: float
    action: str


def to_dict(value: object) -> dict[str, Any]:
    """Convert a dataclass value, including nested dataclasses, to a dictionary."""
    return asdict(value)
