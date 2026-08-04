"""Extraction of useful SEO fields from Poki game pages."""

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import GamePage
from .urls import canonical_game_url


class PageStructureError(ValueError):
    """Raised when a page lacks the minimum structure needed for extraction."""


_BASE_URL = "https://poki.com/"
_IGNORED_TAGS = {"script", "style", "noscript", "template"}
_BODY_STRUCTURAL_TAGS = {"nav", "header", "footer", "aside"}
_NON_CATEGORY_ROUTES = {"new"}
_DEVELOPER_PATTERN = re.compile(r"^Developer\s*:\s*(\S(?:.*\S)?)$", re.IGNORECASE)
_DEVELOPER_BOUNDARY = re.compile(r"(?:[!?]|\.)\s+|\s+[|·•—–]\s+")
_HIDDEN_STYLE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.IGNORECASE)


def extract_game_page(url: str, html: str) -> GamePage:
    """Parse a Poki game page without relying on its client-side application data."""
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main")
    content = main or (soup.body if re.search(r"<body\b", html, re.IGNORECASE) else None)
    if content is None:
        raise PageStructureError("missing body")
    name = next(
        (
            heading_text
            for h1 in content.find_all("h1")
            if (heading_text := _visible_text(h1, content))
        ),
        "",
    )
    if not name:
        raise PageStructureError("missing h1")

    title_tag = soup.title
    title = _text(title_tag) if title_tag is not None else name
    description_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = (description_tag.get("content") or "").strip() if description_tag else ""

    canonical_current_url = canonical_game_url(url)
    return GamePage(
        url=url,
        slug=_slug(url),
        name=name,
        title=title or name,
        description=description,
        body=_visible_text(content, content),
        categories=tuple(_categories(content)),
        developer=_developer(content),
        related_games=tuple(_related_games(content, name, canonical_current_url)),
    )


def _text(tag: Tag) -> str:
    return " ".join(tag.stripped_strings)


def _visible_text(tag: Tag, content_root: Tag | None = None) -> str:
    root = content_root or tag
    strings = (
        string.strip()
        for string in tag.strings
        if string.strip() and not _has_excluded_ancestor(string, root)
    )
    return " ".join(strings)


def _has_excluded_ancestor(string: NavigableString, content_root: Tag) -> bool:
    return any(_is_excluded(tag, content_root) for tag in string.parents if isinstance(tag, Tag))


def _is_excluded(tag: Tag, content_root: Tag) -> bool:
    return _is_hidden(tag) or (content_root.name == "body" and tag.name in _BODY_STRUCTURAL_TAGS)


def _is_hidden(tag: Tag) -> bool:
    classes = tag.get("class", [])
    style = tag.get("style", "")
    return (
        tag.name in _IGNORED_TAGS
        or tag.has_attr("hidden")
        or str(tag.get("aria-hidden", "")).strip().lower() == "true"
        or "hidden" in classes
        or bool(_HIDDEN_STYLE.search(str(style)))
    )


def _visible_label(anchor: Tag, content_root: Tag) -> str:
    return _visible_text(anchor, content_root)


def _slug(url: str) -> str:
    return urlsplit(url).path.rstrip("/").split("/")[-1]


def _categories(content: Tag) -> list[str]:
    categories: list[str] = []
    seen: set[str] = set()
    for anchor in content.find_all("a", href=True):
        if _is_excluded(anchor, content):
            continue
        label = _visible_label(anchor, content)
        if label.endswith("Games") and label not in seen and _is_category_url(anchor["href"]):
            seen.add(label)
            categories.append(label)
    return categories


def _is_category_url(href: str) -> bool:
    try:
        parsed = urlsplit(urljoin(_BASE_URL, href.strip()))
        default_port = 443 if parsed.scheme == "https" else 80
        segments = [segment for segment in parsed.path.split("/") if segment]
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"poki.com", "www.poki.com"}
            and parsed.port in {None, default_port}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and len(segments) == 2
            and segments[0].lower() == "en"
            and segments[1].lower() != "g"
            and segments[1].lower() not in _NON_CATEGORY_ROUTES
        )
    except ValueError:
        return False


def _related_games(content: Tag, name: str, current_url: str | None) -> list[str]:
    games: list[str] = []
    seen: set[str] = set()
    for anchor in content.find_all("a", href=True):
        if _is_excluded(anchor, content):
            continue
        game_url = canonical_game_url(anchor["href"])
        label = _visible_label(anchor, content)
        if (
            game_url is not None
            and game_url != current_url
            and label
            and label != name
            and game_url not in seen
        ):
            seen.add(game_url)
            games.append(label)
    return games


def _developer(content: Tag) -> str | None:
    for container in content.find_all(["p", "li", "dd"]):
        if _is_excluded(container, content):
            continue
        value = _developer_value(_visible_text(container, content))
        if value:
            return value
    for string in content.find_all(string=True, recursive=False):
        if not string.strip() or _has_excluded_ancestor(string, content):
            continue
        value = _developer_value(string.strip())
        if value:
            return value
    return None


def _developer_value(text: str) -> str | None:
    match = _DEVELOPER_PATTERN.match(text)
    if not match:
        return None
    value = _DEVELOPER_BOUNDARY.split(match.group(1), maxsplit=1)[0].strip()
    return value or None
