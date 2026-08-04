"""Poki game URL validation and canonicalization."""

from urllib.parse import unquote, urljoin, urlsplit


_BASE_URL = "https://poki.com/"
_ALLOWED_HOSTS = {"poki.com", "www.poki.com"}


def canonical_game_url(raw: str) -> str | None:
    """Return a canonical Poki English game URL, or ``None`` when invalid."""
    try:
        parsed = urlsplit(urljoin(_BASE_URL, raw.strip()))
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    default_port = 443 if parsed.scheme == "https" else 80
    if port not in {None, default_port}:
        return None

    path = parsed.path.rstrip("/").lower()
    segments = path.split("/")
    if len(segments) != 4 or segments[:3] != ["", "en", "g"] or not segments[3]:
        return None

    slug = segments[3]
    decoded_slug = unquote(slug)
    if (
        "/" in decoded_slug
        or "\\" in decoded_slug
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded_slug)
    ):
        return None
    return f"https://poki.com/en/g/{slug}"
