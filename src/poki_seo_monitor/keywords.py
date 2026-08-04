"""Evidence-backed keyword candidate generation for extracted game pages."""

import re
import unicodedata
from typing import Literal

from .models import GamePage, KeywordCandidate


# The fixed bound keeps downstream signal collection deliberately conservative.
# When categories exceed the bound, game-name and long-tail candidates are retained
# first so a valid page still has representation in all three keyword groups.
MAX_KEYWORDS_PER_GAME = 20
KeywordGroup = Literal["game_name", "category", "long_tail"]

_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\uff07": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)
_LEADING_EDGE_PUNCTUATION = re.compile(r"^[^\w\s]+(?:\s+)?")
_TRAILING_EDGE_PUNCTUATION = re.compile(r"(?:\s+)?[^\w\s]+$")
_WHITESPACE = re.compile(r"\s+")

_MECHANICS: tuple[tuple[str, str], ...] = (
    ("drift", "online drifting games"),
    ("drifting", "online drifting games"),
    ("multiplayer", "online multiplayer games"),
    ("merge", "merge games"),
    ("puzzle", "puzzle games"),
    ("dress up", "dress up games"),
    ("soccer", "online soccer games"),
    ("football", "online soccer games"),
    ("shooting", "online shooting games"),
    ("shooter", "online shooting games"),
    ("idle", "idle games"),
    ("platform", "platform games"),
    ("platformer", "platform games"),
)
_MECHANIC_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (
        term,
        phrase,
        re.compile(
            r"(?<!\w)" + re.escape(term).replace(r"\ ", r"\s+") + r"(?!\w)"
        ),
    )
    for term, phrase in _MECHANICS
)


def _clean(value: str) -> str:
    """Return a readable, comparison-ready phrase without erasing meaningful text."""
    normalized = unicodedata.normalize("NFC", value).translate(_TRANSLATION).lower()
    normalized = _WHITESPACE.sub(" ", normalized.strip())
    normalized = _LEADING_EDGE_PUNCTUATION.sub("", normalized)
    normalized = _TRAILING_EDGE_PUNCTUATION.sub("", normalized)
    return _WHITESPACE.sub(" ", normalized.strip())


def _is_phrase(value: str) -> bool:
    return bool(value) and any(character.isalnum() for character in value)


def _candidate(
    phrase: str, group: KeywordGroup, evidence: tuple[str, ...]
) -> KeywordCandidate | None:
    cleaned = _clean(phrase)
    if not _is_phrase(cleaned):
        return None
    return KeywordCandidate(cleaned, group, evidence)


def _unique(candidates: list[KeywordCandidate]) -> list[KeywordCandidate]:
    """Deduplicate case-insensitively while retaining first evidence and group."""
    unique: list[KeywordCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = unicodedata.normalize("NFC", candidate.phrase).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _base_entity(name: str) -> str:
    """Unwrap only an unambiguous ``Play <entity> Online/Free`` H1 wrapper."""
    words = name.split()
    if len(words) >= 3 and words[0] == "play" and words[-1] in {"online", "free"}:
        interior = " ".join(words[1:-1])
        if _is_phrase(interior):
            return interior
    return name


def _game_name_candidates(name: str, base_entity: str) -> list[KeywordCandidate]:
    if not _is_phrase(name) or not _is_phrase(base_entity):
        return []

    candidates = [_candidate(name, "game_name", ("h1",))]
    base_words = base_entity.split()
    play_online_phrase = (
        f"{base_entity} online"
        if base_words[0] == "play"
        else f"play {base_entity} online"
    )
    if (
        play_online_phrase != name
        and base_words[-1] != "online"
    ):
        candidates.append(
            _candidate(play_online_phrase, "game_name", ("h1", "online intent"))
        )
    if "free" not in base_words:
        candidates.append(
            _candidate(f"{base_entity} free", "game_name", ("h1", "free intent"))
        )
    return [candidate for candidate in candidates if candidate is not None]


def _category_candidates(page: GamePage) -> list[KeywordCandidate]:
    candidates: list[KeywordCandidate] = []
    seen_categories: set[str] = set()
    for original in page.categories:
        category = _clean(original)
        key = category.casefold()
        if not _is_phrase(category) or key in seen_categories:
            continue
        seen_categories.add(key)
        phrase = category if category.split()[0] == "online" else f"online {category}"
        candidate = _candidate(phrase, "category", (f"category:{original}",))
        if candidate is not None:
            candidates.append(candidate)
    candidates.extend(_mechanic_candidates(page))
    return candidates


def _mechanic_candidates(page: GamePage) -> list[KeywordCandidate]:
    sources = (("description", _clean(page.description)), ("body", _clean(page.body)))
    matches: list[tuple[int, int, int, str, str, str]] = []
    matched_terms: set[str] = set()
    for source_index, (source_name, source) in enumerate(sources):
        for mechanic_index, (term, phrase, pattern) in enumerate(_MECHANIC_PATTERNS):
            if term in matched_terms:
                continue
            if match := pattern.search(source):
                matched_terms.add(term)
                matches.append(
                    (source_index, match.start(), mechanic_index, source_name, term, phrase)
                )

    candidates: list[KeywordCandidate] = []
    seen_phrases: set[str] = set()
    for _, _, _, source_name, term, phrase in sorted(matches):
        candidate = _candidate(phrase, "category", (f"{source_name}:{term}",))
        if candidate is not None and candidate.phrase.casefold() not in seen_phrases:
            seen_phrases.add(candidate.phrase.casefold())
            candidates.append(candidate)
            if len(candidates) == 5:
                break
    return candidates


def _long_tail_candidates(
    base_entity: str, related_games: tuple[str, ...]
) -> list[KeywordCandidate]:
    if not _is_phrase(base_entity):
        return []
    related_names = [
        name
        for related_game in related_games
        if _is_phrase(name := _clean(related_game))
    ]
    similarity_evidence = (
        ("related games", f"related_game:{related_names[0]}")
        if related_names
        else ("similarity intent", "source:heuristic")
    )
    how_to_phrase = (
        f"how to {base_entity}"
        if base_entity.startswith("play ")
        else f"how to play {base_entity}"
    )
    values = (
        (how_to_phrase, ("game instructions", "source:heuristic")),
        (f"{base_entity} controls", ("page controls", "source:heuristic")),
        (f"games like {base_entity}", similarity_evidence),
        (f"{base_entity} unblocked", ("access intent", "source:heuristic")),
    )
    return [
        candidate
        for phrase, evidence in values
        if (candidate := _candidate(phrase, "long_tail", evidence)) is not None
    ]


def generate_keywords(page: GamePage) -> list[KeywordCandidate]:
    """Generate at most :data:`MAX_KEYWORDS_PER_GAME` unverified candidates."""
    name = _clean(page.name)
    base_entity = _base_entity(name)
    game_names = _unique(_game_name_candidates(name, base_entity))
    long_tails = _unique(_long_tail_candidates(base_entity, page.related_games))
    categories = _unique(_category_candidates(page))

    occupied = {candidate.phrase.casefold() for candidate in game_names}
    categories = [candidate for candidate in categories if candidate.phrase.casefold() not in occupied]
    occupied.update(candidate.phrase.casefold() for candidate in categories)
    long_tails = [candidate for candidate in long_tails if candidate.phrase.casefold() not in occupied]

    category_capacity = max(0, MAX_KEYWORDS_PER_GAME - len(game_names) - len(long_tails))
    return game_names + categories[:category_capacity] + long_tails
