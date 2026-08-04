"""Parsing and consolidation of Poki game discovery sources."""

import gzip
from io import BytesIO
from collections.abc import Iterable
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

from bs4 import BeautifulSoup
from defusedxml import ElementTree

from .models import DiscoveredGame
from .urls import canonical_game_url


_SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SITEMAP = f"{{{_SITEMAP_NAMESPACE}}}"
_SOURCE_ORDER = ("new_games", "sitemap")
MAX_XML_BYTES = 20 * 1024 * 1024


def _xml(data: bytes) -> Element:
    """Decompress a gzip sitemap when needed and parse its XML document."""
    if len(data) > MAX_XML_BYTES:
        raise ValueError("Sitemap XML exceeds the 20 MiB size limit")
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=BytesIO(data)) as archive:
            data = archive.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise ValueError("Sitemap XML exceeds the 20 MiB size limit")
    return ElementTree.fromstring(
        data,
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )


def _canonical_sitemap_url(raw: str) -> str | None:
    """Return a safe Poki sitemap URL, or ``None`` when the location is invalid."""
    try:
        parsed = urlsplit(raw.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"poki.com", "www.poki.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/en/sitemaps/")
    ):
        return None
    return f"https://poki.com{parsed.path}"


def parse_sitemap_index(data: bytes) -> list[str]:
    """Return nonblank sitemap locations in their document order."""
    root = _xml(data)
    return [
        url
        for sitemap in root.findall(f"{_SITEMAP}sitemap")
        if (location := sitemap.findtext(f"{_SITEMAP}loc"))
        and (url := _canonical_sitemap_url(location))
    ]


def parse_urlset(data: bytes) -> list[DiscoveredGame]:
    """Extract unique, canonical Poki games from a sitemap urlset."""
    root = _xml(data)
    discovered: list[DiscoveredGame] = []
    seen: set[str] = set()
    for entry in root.findall(f"{_SITEMAP}url"):
        location = entry.findtext(f"{_SITEMAP}loc")
        if not location:
            continue
        url = canonical_game_url(location)
        if url is not None and url not in seen:
            seen.add(url)
            discovered.append(DiscoveredGame(url, ("sitemap",)))
    return discovered


def parse_new_games(html: str) -> list[DiscoveredGame]:
    """Extract unique canonical game links from the new-games page."""
    soup = BeautifulSoup(html, "lxml")
    discovered: list[DiscoveredGame] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        url = canonical_game_url(href)
        if url is not None and url not in seen:
            seen.add(url)
            discovered.append(DiscoveredGame(url, ("new_games",), len(discovered) + 1))
    return discovered


def merge_discoveries(*groups: Iterable[DiscoveredGame]) -> list[DiscoveredGame]:
    """Merge discoveries by URL with stable source order and rank priority."""
    merged: dict[str, tuple[set[str], int | None]] = {}
    for group in groups:
        for game in group:
            sources, rank = merged.get(game.url, (set(), None))
            sources.update(game.sources)
            if game.source_rank is not None and (rank is None or game.source_rank < rank):
                rank = game.source_rank
            merged[game.url] = (sources, rank)

    games = [
        DiscoveredGame(
            url,
            tuple(source for source in _SOURCE_ORDER if source in sources),
            rank,
        )
        for url, (sources, rank) in merged.items()
    ]
    return sorted(
        games,
        key=lambda game: (
            game.source_rank is None,
            game.source_rank if game.source_rank is not None else 0,
            game.url,
        ),
    )
