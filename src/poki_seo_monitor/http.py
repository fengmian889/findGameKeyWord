"""HTTP client policy for polite, resilient requests."""

from collections.abc import Mapping
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from typing import Protocol
from urllib.parse import urlsplit
from urllib3.util.retry import Retry


USER_AGENT = "PokiSEOResearchBot/0.1 (+repository contact)"


class ResponseLike(Protocol):
    content: bytes

    def raise_for_status(self) -> None: ...


class SessionLike(Protocol):
    def get(self, url: str, **kwargs: Any) -> ResponseLike: ...


def build_session() -> requests.Session:
    """Build a session with the monitor's retry and identification policy."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        backoff_max=8,
        respect_retry_after_header=False,
        status_forcelist=(429, 500, 502, 503, 504),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_bytes(session: SessionLike, url: str, max_bytes: int = 20 * 1024 * 1024) -> bytes:
    """Fetch a URL and return its content, raising for unsuccessful responses."""
    if urlsplit(url).scheme.lower() != "https":
        raise ValueError("Only HTTPS URLs are allowed")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    streamed = True
    try:
        response = session.get(url, timeout=(10, 30), stream=True)
    except TypeError:
        # Lightweight test doubles and older adapters may expose only the
        # original timeout-only protocol.
        streamed = False
        response = session.get(url, timeout=(10, 30))
    try:
        response.raise_for_status()
        headers = getattr(response, "headers", {})
        content_length = None
        if isinstance(headers, Mapping):
            content_length = next(
                (value for key, value in headers.items() if str(key).lower() == "content-length"),
                None,
            )
        if content_length is not None:
            try:
                declared = int(content_length)
            except (TypeError, ValueError):
                declared = -1
            if declared > max_bytes:
                raise ValueError(f"response exceeds {max_bytes} byte size limit")

        iterator = getattr(response, "iter_content", None)
        if streamed and callable(iterator):
            chunks: list[bytes] = []
            size = 0
            for chunk in iterator(chunk_size=min(64 * 1024, max_bytes + 1)):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"response exceeds {max_bytes} byte size limit")
                chunks.append(chunk)
            return b"".join(chunks)
        content = response.content
        if len(content) > max_bytes:
            raise ValueError(f"response exceeds {max_bytes} byte size limit")
        return content
    finally:
        close = getattr(response, "close", None)
        if streamed and callable(close):
            close()
