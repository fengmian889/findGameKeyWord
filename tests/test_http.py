import pytest
import requests

from poki_seo_monitor.http import build_session, get_bytes


def test_build_session_sets_user_agent_and_retry_policy() -> None:
    session = build_session()

    assert session.headers["User-Agent"] == "PokiSEOResearchBot/0.1 (+repository contact)"
    retry = session.get_adapter("https://poki.com").max_retries
    assert retry.total == retry.connect == retry.read == 3
    assert retry.backoff_factor == 0.8
    assert retry.backoff_max == 8
    assert retry.respect_retry_after_header is False
    assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}


class FakeResponse:
    content = b"game page"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, tuple[int, int]]] = []

    def get(self, url: str, *, timeout: tuple[int, int]) -> FakeResponse:
        self.calls.append((url, timeout))
        return self.response


def test_get_bytes_returns_successful_response_content() -> None:
    session = FakeSession(FakeResponse())

    assert get_bytes(session, "https://poki.com/en/g/test-game") == b"game page"
    assert session.calls == [("https://poki.com/en/g/test-game", (10, 30))]


def test_get_bytes_propagates_http_errors() -> None:
    error = requests.HTTPError("not found")

    with pytest.raises(requests.HTTPError, match="not found"):
        get_bytes(FakeSession(FakeResponse(error)), "https://poki.com/en/g/missing")


@pytest.mark.parametrize("url", ["http://poki.com/en/g/test-game", "ftp://poki.com/game"])
def test_get_bytes_rejects_non_https_urls_without_requesting(url: str) -> None:
    session = FakeSession(FakeResponse())

    with pytest.raises(ValueError, match="HTTPS"):
        get_bytes(session, url)

    assert session.calls == []


class StreamingResponse(FakeResponse):
    def __init__(self, chunks: list[bytes], content_length: str | None = None) -> None:
        super().__init__()
        self.content = b"unused"
        self.headers = {} if content_length is None else {"Content-Length": content_length}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int):
        assert chunk_size > 0
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class StreamingSession:
    def __init__(self, response: StreamingResponse) -> None:
        self.response = response
        self.kwargs = None

    def get(self, url: str, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_get_bytes_streams_and_closes_response() -> None:
    response = StreamingResponse([b"ab", b"", b"cd"])
    session = StreamingSession(response)

    assert get_bytes(session, "https://poki.com/x", max_bytes=4) == b"abcd"
    assert session.kwargs == {"timeout": (10, 30), "stream": True}
    assert response.closed is True


def test_get_bytes_rejects_content_length_before_body() -> None:
    response = StreamingResponse([b"never"], "5")

    with pytest.raises(ValueError, match="size limit"):
        get_bytes(StreamingSession(response), "https://poki.com/x", max_bytes=4)


def test_get_bytes_rejects_actual_streamed_size() -> None:
    with pytest.raises(ValueError, match="size limit"):
        get_bytes(
            StreamingSession(StreamingResponse([b"abcd", b"e"])),
            "https://poki.com/x",
            max_bytes=4,
        )
