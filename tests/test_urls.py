import pytest

import poki_seo_monitor.urls as urls
from poki_seo_monitor.urls import canonical_game_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://poki.com/en/g/GoalHeads.io?ref=new",
            "https://poki.com/en/g/goalheads.io",
        ),
        (
            "https://www.poki.com/en/g/test-game/",
            "https://poki.com/en/g/test-game",
        ),
        ("https://poki.com/EN/G/Test-Game", "https://poki.com/en/g/test-game"),
        ("/en/g/test-game", "https://poki.com/en/g/test-game"),
        (
            "  https://poki.com/en/g/test-game?source=homepage  ",
            "https://poki.com/en/g/test-game",
        ),
    ],
)
def test_canonical_game_url_normalizes_valid_game_urls(
    raw: str, expected: str
) -> None:
    assert canonical_game_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "/en/new",
        "/en/g",
        "/en/g/",
        "https://evil.example/en/g/test-game",
        "https://poki.com/zh/g/x",
        "https://poki.com/en/g/test-game/related",
        "https://[::1/en/g/x",
        "https://user:pass@poki.com/en/g/x",
        "https://poki.com:99999/en/g/x",
        "https://poki.com/en/g/test%0Agame",
        "https://poki.com/en/g/test%2Fgame",
        "https://poki.com/en/g/test%5Cgame",
        "mailto:person@example.com",
        "javascript:alert(1)",
        "ftp://poki.com/en/g/test-game",
    ],
)
def test_canonical_game_url_rejects_non_game_or_unsafe_urls(raw: str) -> None:
    assert canonical_game_url(raw) is None


def test_canonical_game_url_strips_input_before_url_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved_inputs: list[str] = []
    real_urljoin = urls.urljoin

    def capture_urljoin(base: str, raw: str) -> str:
        resolved_inputs.append(raw)
        return real_urljoin(base, raw)

    monkeypatch.setattr(urls, "urljoin", capture_urljoin)

    canonical_game_url("  /en/g/test-game  ")

    assert resolved_inputs == ["/en/g/test-game"]
