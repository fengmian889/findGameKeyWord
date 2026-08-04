from pathlib import Path

import pytest

from poki_seo_monitor.extractor import PageStructureError, extract_game_page


FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://poki.com/en/g/alpha-game"


def test_extract_game_page_returns_core_content_from_fixture() -> None:
    page = extract_game_page(URL, (FIXTURES / "game-page.html").read_text(encoding="utf-8"))

    assert page.name == "Alpha Game"
    assert page.slug == "alpha-game"
    assert page.title == "Alpha Game - Play Online"
    assert page.description == "Drive fast in Alpha Game."
    assert page.categories == ("Car Games",)
    assert page.developer == "Example Studio"
    assert page.related_games == ("Beta Game",)
    assert "ignore me" not in page.body
    assert "Ignored fallback" not in page.body


def test_extract_game_page_requires_a_nonblank_h1() -> None:
    with pytest.raises(PageStructureError, match="missing h1"):
        extract_game_page(URL, "<html><body><main><h1> </h1></main></body></html>")


def test_extract_game_page_requires_h1_inside_selected_main_content() -> None:
    with pytest.raises(PageStructureError, match="missing h1"):
        extract_game_page(URL, "<body><h1>Outside title</h1><main><p>No title</p></main></body>")


def test_extract_game_page_falls_back_to_name_for_missing_title_and_description() -> None:
    page = extract_game_page(URL, "<body><h1>Alpha Game</h1><p>Text</p></body>")

    assert page.title == "Alpha Game"
    assert page.description == ""


def test_extract_game_page_requires_body_when_no_main_is_present() -> None:
    with pytest.raises(PageStructureError, match="missing body"):
        extract_game_page(URL, "<html><head><h1>Alpha Game</h1></head></html>")


def test_extract_game_page_deduplicates_valid_categories_and_related_games() -> None:
    html = """
    <body><main><h1>Alpha Game</h1>
      <a href="/en/car">Car Games</a><a href="/en/car/">Car Games</a>
      <a href="/en/g/beta-game">Beta Game</a>
      <a href="https://www.poki.com/en/g/beta-game?x=1">Beta Game</a>
      <a href="/zh/g/gamma">Gamma Game</a><a href="https://evil.test/en/g/no">No Game</a>
      <a href="/en/g/alpha-game">Alpha Game</a>
    </main></body>"""

    page = extract_game_page(URL, html)

    assert page.categories == ("Car Games",)
    assert page.related_games == ("Beta Game",)


def test_extract_game_page_excludes_hidden_text_and_hidden_anchor_label_parts() -> None:
    html = """
    <body><h1>Alpha Game</h1><p>Visible text</p>
      <p hidden>Hidden attribute</p><p aria-hidden="TRUE">Hidden aria</p>
      <p style="display: none">Hidden display</p><p style="visibility : hidden">Hidden visibility</p>
      <p class="hidden">Hidden class</p>
      <a href="/en/g/beta-game">Beta<span hidden> Secret</span> Game</a>
    </body>"""

    page = extract_game_page(URL, html)

    assert page.body == "Alpha Game Visible text Beta Game"
    assert page.related_games == ("Beta Game",)


def test_extract_game_page_accepts_only_single_segment_category_paths() -> None:
    html = """
    <body><h1>Alpha Game</h1>
      <a href="/en/car">Car Games</a><a href="/en/car/racing">Racing Games</a>
      <a href="/en/car?filter=x">Query Games</a><a href="/en/car#top">Fragment Games</a>
      <a href="/en/g/beta-game">Beta Games</a><a href="/en/new">New Games</a>
    </body>"""

    assert extract_game_page(URL, html).categories == ("Car Games",)


def test_extract_game_page_handles_developer_absence_and_punctuation_boundary() -> None:
    absent = extract_game_page(URL, "<body><h1>Alpha Game</h1><p>A game.</p></body>")
    present = extract_game_page(
        URL,
        "<body><h1>Alpha Game</h1><p>Developer: Example Studio. <a href='/en/car'>Car Games</a></p></body>",
    )

    assert absent.developer is None
    assert present.developer == "Example Studio"


def test_extract_game_page_reads_developer_from_split_markup_and_keeps_dotted_names() -> None:
    page = extract_game_page(
        URL,
        "<body><h1>Alpha Game</h1><p>Developer: <a href='/en/example'>Example Studio Inc.</a></p></body>",
    )

    assert page.developer == "Example Studio Inc."


def test_extract_game_page_does_not_treat_prose_developer_mentions_as_a_label() -> None:
    page = extract_game_page(URL, "<body><h1>Alpha Game</h1><p>Meet the Developer: Example Studio</p></body>")

    assert page.developer is None


def test_extract_game_page_accepts_relative_and_canonical_related_links() -> None:
    html = """
    <body><h1>Alpha Game</h1>
      <a href="/en/g/beta-game">Beta Game</a>
      <a href="https://poki.com/en/g/gamma-game">Gamma Game</a>
    </body>"""

    assert extract_game_page(URL, html).related_games == ("Beta Game", "Gamma Game")


def test_extract_game_page_excludes_body_fallback_structural_regions() -> None:
    html = """
    <body>
      <header><h1>Site title</h1></header>
      <nav><a href="/en/car">Nav Games</a><a href="/en/g/nav-game">Nav Game</a></nav>
      <article><h1>Alpha Game</h1><p>Article copy</p>
        <a href="/en/car">Car Games</a><a href="/en/g/beta-game">Beta Game</a>
      </article>
      <footer><p>Developer: Footer Studio</p><a href="/en/g/footer-game">Footer Game</a></footer>
      <aside><a href="/en/g/aside-game">Aside Game</a></aside>
    </body>"""

    page = extract_game_page(URL, html)

    assert page.body == "Alpha Game Article copy Car Games Beta Game"
    assert page.categories == ("Car Games",)
    assert page.related_games == ("Beta Game",)
    assert page.developer is None
