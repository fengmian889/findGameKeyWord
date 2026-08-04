from poki_seo_monitor.keywords import MAX_KEYWORDS_PER_GAME, generate_keywords
from poki_seo_monitor.models import GamePage


PAGE = GamePage(
    url="https://poki.com/en/g/alpha-game",
    slug="alpha-game",
    name="Alpha Game",
    title="Alpha Game - Play Online",
    description="Race cars online.",
    body="Drift a sports car and beat other racers with arrow key controls.",
    categories=("Car Games", "Racing Games"),
    related_games=("Beta Racer",),
)


def phrases(page: GamePage) -> list[str]:
    return [candidate.phrase for candidate in generate_keywords(page)]


def test_generates_three_evidence_backed_groups_without_duplicates() -> None:
    candidates = generate_keywords(PAGE)
    candidate_phrases = [candidate.phrase for candidate in candidates]

    assert "alpha game" in candidate_phrases
    assert "play alpha game online" in candidate_phrases
    assert "online car games" in candidate_phrases
    assert "how to play alpha game" in candidate_phrases
    assert len(candidate_phrases) == len({phrase.casefold() for phrase in candidate_phrases})
    assert {candidate.group for candidate in candidates} == {
        "game_name",
        "category",
        "long_tail",
    }
    assert next(item for item in candidates if item.phrase == "alpha game").evidence == ("h1",)
    assert next(item for item in candidates if item.phrase == "play alpha game online").evidence == (
        "h1",
        "online intent",
    )
    assert next(item for item in candidates if item.phrase == "online car games").evidence == (
        "category:Car Games",
    )
    assert next(item for item in candidates if item.phrase == "games like alpha game").evidence == (
        "related games",
        "related_game:beta racer",
    )
    assert next(item for item in candidates if item.phrase == "how to play alpha game").evidence == (
        "game instructions",
        "source:heuristic",
    )
    assert next(item for item in candidates if item.phrase == "alpha game controls").evidence == (
        "page controls",
        "source:heuristic",
    )
    assert next(item for item in candidates if item.phrase == "alpha game unblocked").evidence == (
        "access intent",
        "source:heuristic",
    )
    assert all(candidate.verified is False for candidate in candidates)


def test_normalizes_unicode_whitespace_and_edge_punctuation() -> None:
    page = GamePage(
        "url",
        "slug",
        "  “Goalheads.io’s\u00a0–\u00a04th   And  Goal 2026!!!”  ",
        "title",
        "",
        "",
        ("  Car\u00a0\u00a0Games!!! ",),
    )

    candidates = generate_keywords(page)

    assert candidates[0].phrase == "goalheads.io's - 4th and goal 2026"
    assert "online car games" in [candidate.phrase for candidate in candidates]
    assert all("  " not in candidate.phrase for candidate in candidates)
    assert all(candidate.phrase == candidate.phrase.strip() for candidate in candidates)
    assert all(not candidate.phrase.startswith(("-", "!", ".")) for candidate in candidates)
    assert all(not candidate.phrase.endswith(("-", "!", ".")) for candidate in candidates)


def test_prequalified_names_do_not_produce_repeated_intent_words() -> None:
    page = GamePage("url", "slug", "Play X Online", "title", "", "")

    assert phrases(page) == [
        "play x online",
        "x free",
        "how to play x",
        "x controls",
        "games like x",
        "x unblocked",
    ]


def test_free_fire_is_preserved_as_an_entity_without_a_duplicate_free_suffix() -> None:
    page = GamePage("url", "slug", "Free Fire", "title", "", "")

    candidate_phrases = phrases(page)

    assert "play free fire online" in candidate_phrases
    assert "how to play free fire" in candidate_phrases
    assert "free fire controls" in candidate_phrases
    assert "free fire free" not in candidate_phrases


def test_play_free_fire_without_terminal_wrapper_remains_intact() -> None:
    page = GamePage("url", "slug", "Play Free Fire", "title", "", "")

    candidate_phrases = phrases(page)

    assert "play free fire online" in candidate_phrases
    assert "how to play free fire" in candidate_phrases
    assert "play free fire controls" in candidate_phrases
    assert "games like play free fire" in candidate_phrases
    assert "play free fire free" not in candidate_phrases
    assert all("play play" not in phrase for phrase in candidate_phrases)


def test_play_free_fire_online_unwraps_its_combined_wrapper() -> None:
    page = GamePage("url", "slug", "Play Free Fire Online", "title", "", "")

    candidate_phrases = phrases(page)

    assert candidate_phrases[:3] == [
        "play free fire online",
        "how to play free fire",
        "free fire controls",
    ]
    assert "free fire free" not in candidate_phrases


def test_terminal_online_is_preserved_without_a_leading_play_wrapper() -> None:
    page = GamePage("url", "slug", "Nights Online", "title", "", "")

    assert "how to play nights online" in phrases(page)
    assert "nights online controls" in phrases(page)


def test_play_x_keeps_its_entity_without_doubling_play_in_templates() -> None:
    page = GamePage("url", "slug", "Play X", "title", "", "")

    candidate_phrases = phrases(page)

    assert "play x online" in candidate_phrases
    assert "how to play x" in candidate_phrases
    assert "play x controls" in candidate_phrases
    assert "games like play x" in candidate_phrases
    assert all("play play" not in phrase for phrase in candidate_phrases)


def test_play_together_keeps_brand_tokens_without_doubling_play() -> None:
    page = GamePage("url", "slug", "Play Together", "title", "", "")

    candidate_phrases = phrases(page)

    assert "play together online" in candidate_phrases
    assert "how to play together" in candidate_phrases
    assert "play together controls" in candidate_phrases
    assert "games like play together" in candidate_phrases
    assert all("play play" not in phrase for phrase in candidate_phrases)


def test_freestyle_does_not_suppress_the_free_modifier() -> None:
    page = GamePage("url", "slug", "Freestyle Rider", "title", "", "")

    assert "freestyle rider free" in phrases(page)


def test_deduplicates_categories_and_adds_only_present_whole_word_mechanics() -> None:
    page = GamePage(
        "url",
        "slug",
        "Alpha",
        "title",
        "A multiplayer puzzle where you dress up.",
        "Drifting through emergency rooms.",
        ("Car Games", " car games ", "Online Car Games"),
    )

    candidates = generate_keywords(page)
    category = [candidate for candidate in candidates if candidate.group == "category"]

    assert [candidate.phrase for candidate in category] == [
        "online car games",
        "online multiplayer games",
        "puzzle games",
        "dress up games",
        "online drifting games",
    ]
    assert [candidate.evidence for candidate in category] == [
        ("category:Car Games",),
        ("description:multiplayer",),
        ("description:puzzle",),
        ("description:dress up",),
        ("body:drifting",),
    ]
    assert "merge games" not in phrases(page)


def test_uses_similarity_intent_when_no_related_games_are_available() -> None:
    page = GamePage("url", "slug", "Alpha", "title", "", "")

    games_like = next(candidate for candidate in generate_keywords(page) if candidate.phrase == "games like alpha")

    assert games_like.evidence == ("similarity intent", "source:heuristic")


def test_mechanic_provenance_prefers_description_when_term_is_in_both_fields() -> None:
    page = GamePage(
        "url",
        "slug",
        "Alpha",
        "title",
        "A drift challenge.",
        "Drift through every level.",
    )

    mechanic = next(
        candidate
        for candidate in generate_keywords(page)
        if candidate.phrase == "online drifting games"
    )

    assert mechanic.evidence == ("description:drift",)


def test_does_not_infer_mechanics_that_are_absent_from_page_copy() -> None:
    page = GamePage(
        "url",
        "slug",
        "Alpha",
        "title",
        "A relaxing experience.",
        "Collect stars and enjoy the music.",
    )

    assert [candidate for candidate in generate_keywords(page) if candidate.group == "category"] == []


def test_limits_mechanic_derived_phrases_to_five() -> None:
    page = GamePage(
        "url",
        "slug",
        "Alpha",
        "title",
        "multiplayer puzzle dress up soccer shooting idle platformer",
        "drift merge football shooter platform",
    )

    mechanics = [candidate for candidate in generate_keywords(page) if candidate.group == "category"]

    assert len(mechanics) == 5
    assert [candidate.phrase for candidate in mechanics] == [
        "online multiplayer games",
        "puzzle games",
        "dress up games",
        "online soccer games",
        "online shooting games",
    ]


def test_caps_deterministically_without_losing_the_three_groups() -> None:
    categories = tuple(f"Category {number} Games" for number in range(30))
    page = GamePage(
        "url",
        "slug",
        "Alpha",
        "title",
        "multiplayer puzzle shooting idle platformer",
        "drift merge dress up soccer football",
        categories,
        related_games=("Beta",),
    )

    first = generate_keywords(page)
    second = generate_keywords(page)

    assert len(first) == MAX_KEYWORDS_PER_GAME
    assert first == second
    assert first[0].phrase == "alpha"
    assert {candidate.group for candidate in first} == {"game_name", "category", "long_tail"}
    assert [candidate.phrase for candidate in first[-4:]] == [
        "how to play alpha",
        "alpha controls",
        "games like alpha",
        "alpha unblocked",
    ]
