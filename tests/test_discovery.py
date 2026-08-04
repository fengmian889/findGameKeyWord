import gzip
from xml.etree import ElementTree
from pathlib import Path

import pytest

import poki_seo_monitor.discovery as discovery
from poki_seo_monitor.discovery import (
    MAX_XML_BYTES,
    merge_discoveries,
    parse_new_games,
    parse_sitemap_index,
    parse_urlset,
)
from poki_seo_monitor.models import DiscoveredGame


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_sitemap_index_returns_namespaced_locations_in_document_order() -> None:
    data = (FIXTURES / "sitemap-index.xml").read_bytes()

    assert parse_sitemap_index(data) == ["https://poki.com/en/sitemaps/games-1.xml"]


def test_parse_sitemap_index_skips_blank_locations() -> None:
    data = b"""<sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
      <sitemap><loc>  </loc></sitemap><sitemap><loc> https://poki.com/en/sitemaps/a.xml </loc></sitemap>
    </sitemapindex>"""

    assert parse_sitemap_index(data) == ["https://poki.com/en/sitemaps/a.xml"]


def test_parse_sitemap_index_keeps_only_safe_poki_sitemap_locations() -> None:
    data = b"""<sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
      <sitemap><loc>https://www.poki.com/en/sitemaps/valid.xml</loc></sitemap>
      <sitemap><loc>https://evil.example/en/sitemaps/foreign.xml</loc></sitemap>
      <sitemap><loc>https://poki.com/en/sitemaps/query.xml?next=x</loc></sitemap>
      <sitemap><loc>https://poki.com:444/en/sitemaps/port.xml</loc></sitemap>
      <sitemap><loc>https://user@poki.com/en/sitemaps/user.xml</loc></sitemap>
      <sitemap><loc>https://poki.com/sitemaps/outside.xml</loc></sitemap>
    </sitemapindex>"""

    assert parse_sitemap_index(data) == ["https://poki.com/en/sitemaps/valid.xml"]


def test_parse_sitemap_index_accepts_gzip() -> None:
    data = gzip.compress((FIXTURES / "sitemap-index.xml").read_bytes())

    assert parse_sitemap_index(data) == ["https://poki.com/en/sitemaps/games-1.xml"]


def test_parse_urlset_returns_canonical_games_only() -> None:
    data = (FIXTURES / "games-sitemap.xml").read_bytes()

    assert parse_urlset(data) == [
        DiscoveredGame("https://poki.com/en/g/alpha-game", ("sitemap",)),
    ]


def test_parse_urlset_accepts_gzip_and_deduplicates_canonical_urls() -> None:
    data = gzip.compress(b"""<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
      <url><loc>https://poki.com/en/g/alpha-game?source=one</loc></url>
      <url><loc>https://poki.com/en/g/alpha-game/</loc></url>
      <url><loc>https://poki.com/en/g/beta-game</loc></url>
    </urlset>""")

    assert parse_urlset(data) == [
        DiscoveredGame("https://poki.com/en/g/alpha-game", ("sitemap",)),
        DiscoveredGame("https://poki.com/en/g/beta-game", ("sitemap",)),
    ]


def test_parse_new_games_returns_ranked_unique_canonical_game_links() -> None:
    html = (FIXTURES / "new-games.html").read_text()

    assert parse_new_games(html) == [
        DiscoveredGame("https://poki.com/en/g/beta-game", ("new_games",), 1),
        DiscoveredGame("https://poki.com/en/g/alpha-game", ("new_games",), 2),
    ]


def test_parse_new_games_ignores_invalid_and_duplicate_links_when_ranking() -> None:
    html = """<a href=\"/en/new\">New</a><a>Missing</a>
    <a href=\"/en/g/alpha\">Alpha</a><a href=\"/en/g/alpha?duplicate\">Again</a>
    <a href=\"/en/g/beta\">Beta</a>"""

    assert parse_new_games(html) == [
        DiscoveredGame("https://poki.com/en/g/alpha", ("new_games",), 1),
        DiscoveredGame("https://poki.com/en/g/beta", ("new_games",), 2),
    ]


def test_merge_discoveries_unions_sources_and_sorts_ranked_then_unranked() -> None:
    sitemap = [
        DiscoveredGame("https://poki.com/en/g/alpha", ("sitemap",)),
        DiscoveredGame("https://poki.com/en/g/zeta", ("sitemap",)),
    ]
    new_games = [
        DiscoveredGame("https://poki.com/en/g/beta", ("new_games",), 2),
        DiscoveredGame("https://poki.com/en/g/alpha", ("new_games",), 1),
    ]

    assert merge_discoveries(sitemap, new_games) == [
        DiscoveredGame("https://poki.com/en/g/alpha", ("new_games", "sitemap"), 1),
        DiscoveredGame("https://poki.com/en/g/beta", ("new_games",), 2),
        DiscoveredGame("https://poki.com/en/g/zeta", ("sitemap",)),
    ]
    assert sitemap[0] == DiscoveredGame("https://poki.com/en/g/alpha", ("sitemap",))


def test_merge_discoveries_uses_minimum_rank_and_handles_empty_groups() -> None:
    assert merge_discoveries(
        [],
        [DiscoveredGame("https://poki.com/en/g/alpha", ("sitemap",), 5)],
        [DiscoveredGame("https://poki.com/en/g/alpha", ("new_games",), 2)],
    ) == [
        DiscoveredGame("https://poki.com/en/g/alpha", ("new_games", "sitemap"), 2),
    ]
    assert merge_discoveries([], []) == []


def test_parse_sitemap_xml_rejects_malformed_documents() -> None:
    with pytest.raises(ElementTree.ParseError):
        parse_urlset(b"<urlset><url><loc>https://poki.com/en/g/alpha</loc></urlset>")


def test_parse_sitemap_xml_rejects_dtd_and_entity_declarations() -> None:
    data = b"""<!DOCTYPE urlset [<!ENTITY game "https://poki.com/en/g/alpha">]>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&game;</loc></url></urlset>"""

    with pytest.raises(ValueError, match="DTDForbidden"):
        parse_urlset(data)


def test_parse_sitemap_xml_rejects_utf16_dtd_and_entity_declarations() -> None:
    data = """<?xml version="1.0" encoding="UTF-16"?>
    <!DOCTYPE urlset [<!ENTITY game "https://poki.com/en/g/alpha">]>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&game;</loc></url></urlset>""".encode(
        "utf-16"
    )

    with pytest.raises(ValueError):
        parse_urlset(data)


def test_parse_sitemap_xml_allows_doctype_text_inside_comment_or_cdata() -> None:
    data = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <!-- literal <!DOCTYPE is harmless here> -->
      <url><loc><![CDATA[https://poki.com/en/g/alpha]]></loc></url>
    </urlset>"""

    assert parse_urlset(data) == [
        DiscoveredGame("https://poki.com/en/g/alpha", ("sitemap",)),
    ]


def test_parse_sitemap_xml_rejects_invalid_gzip() -> None:
    with pytest.raises(gzip.BadGzipFile):
        parse_urlset(b"\x1f\x8bnot-a-gzip-stream")


def test_parse_sitemap_xml_rejects_oversized_compressed_input_before_decompression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery, "MAX_XML_BYTES", 10)

    with pytest.raises(ValueError, match="20 MiB"):
        parse_urlset(gzip.compress(b""))


@pytest.mark.parametrize("data", [b" " * (MAX_XML_BYTES + 1), gzip.compress(b" " * (MAX_XML_BYTES + 1))])
def test_parse_sitemap_xml_rejects_oversized_documents(data: bytes) -> None:
    with pytest.raises(ValueError, match="20 MiB"):
        parse_urlset(data)
