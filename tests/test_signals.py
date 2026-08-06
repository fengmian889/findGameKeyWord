import math
import sys

import pytest

from poki_seo_monitor.models import SearchSignals
import poki_seo_monitor.signals as signals_module
from poki_seo_monitor.signals import (
    AutocompleteProvider,
    GoogleTrendsProvider,
    SerpApiTrendsProvider,
    SerpCompetitionProvider,
    collect_signals,
    parse_serp_hosts,
)


def test_default_autocomplete_request_uses_google_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str], tuple[int, int]]] = []
    status_called = False
    session_built = False

    class Response:
        def raise_for_status(self) -> None:
            nonlocal status_called
            status_called = True

        def json(self) -> list[object]:
            return ["goal heads", ["goal heads game"]]

    class Session:
        def get(
            self,
            url: str,
            *,
            params: dict[str, str],
            timeout: tuple[int, int],
            allow_redirects: bool,
        ) -> Response:
            assert allow_redirects is False
            calls.append((url, params, timeout))
            return Response()

    def fake_build_session() -> Session:
        nonlocal session_built
        session_built = True
        return Session()

    monkeypatch.setattr(signals_module, "build_session", fake_build_session)

    assert AutocompleteProvider().suggest("goal heads") == ("goal heads game",)
    assert session_built is True
    assert calls == [
        (
            "https://suggestqueries.google.com/complete/search",
            {"client": "firefox", "q": "goal heads", "hl": "en"},
            (10, 20),
        )
    ]
    assert status_called is True


def test_default_autocomplete_caches_and_paces_uncached_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    class Response:
        def __init__(self, keyword: str) -> None:
            self._keyword = keyword

        def raise_for_status(self) -> None:
            pass

        def json(self) -> list[object]:
            return [self._keyword, [f"{self._keyword} game"]]

    class Session:
        def get(self, url: str, *, params: dict[str, str], **kwargs: object) -> Response:
            calls.append(params["q"])
            return Response(params["q"])

    monkeypatch.setattr(signals_module, "build_session", Session)
    provider = AutocompleteProvider(monotonic=lambda: 0.0, sleep=sleeps.append)

    assert provider.suggest("Goal Heads") == ("goal heads game",)
    assert provider.suggest(" goal\u00a0heads ") == ("goal heads game",)
    assert provider.suggest("other") == ("other game",)
    assert calls == ["Goal Heads", "other"]
    assert sleeps == [0.25]


def test_default_autocomplete_shares_cache_pacing_and_expires_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    calls: list[str] = []
    sleeps: list[float] = []

    class Response:
        def __init__(self, keyword: str) -> None:
            self._keyword = keyword

        def raise_for_status(self) -> None:
            pass

        def json(self) -> list[object]:
            return [self._keyword, [f"{self._keyword} game"]]

    class Session:
        def get(self, url: str, *, params: dict[str, str], **kwargs: object) -> Response:
            calls.append(params["q"])
            return Response(params["q"])

    def clock() -> float:
        return now

    monkeypatch.setattr(signals_module, "build_session", Session)
    signals_module._reset_default_http_state(monotonic=clock, sleep=sleeps.append)
    try:
        first = AutocompleteProvider()
        second = AutocompleteProvider()

        assert first.suggest("Goal Heads") == ("goal heads game",)
        assert second.suggest(" goal\u00a0heads ") == ("goal heads game",)
        assert second.suggest("other") == ("other game",)
        assert calls == ["Goal Heads", "other"]
        assert sleeps == [0.25]

        now = 6 * 60 * 60
        assert second.suggest("goal heads") == ("goal heads game",)
        assert calls == ["Goal Heads", "other", "goal heads"]
    finally:
        signals_module._reset_default_http_state()


def test_autocomplete_filters_deduplicates_and_excludes_the_keyword() -> None:
    provider = AutocompleteProvider(
        fetch=lambda keyword: [
            keyword,
            ["Goal Heads", "Goal Heads game", "goal heads game", "Another game"],
        ]
    )

    assert provider.suggest("goal heads") == ("goal heads game",)


def test_injected_autocomplete_can_enable_production_pacing_without_losing_cache() -> None:
    calls: list[str] = []
    sleeps: list[float] = []
    provider = AutocompleteProvider(
        fetch=lambda keyword: calls.append(keyword) or [keyword, [f"{keyword} game"]],
        pace_custom=True,
        monotonic=lambda: 0.0,
        sleep=sleeps.append,
    )

    provider.suggest("alpha")
    provider.suggest(" alpha ")
    provider.suggest("beta")

    assert calls == ["alpha", "beta"]
    assert sleeps == [0.25]


def test_autocomplete_normalizes_unicode_whitespace() -> None:
    provider = AutocompleteProvider(
        fetch=lambda keyword: [keyword, ["  Goal\u00a0\u00a0Heads\tgame  ", "GOAL HEADS GAME"]]
    )

    assert provider.suggest("goal heads") == ("goal heads game",)


def test_autocomplete_rejects_malformed_payload() -> None:
    provider = AutocompleteProvider(fetch=lambda keyword: {"suggestions": []})

    with pytest.raises(ValueError, match="payload"):
        provider.suggest("goal heads")


def test_trends_maps_calendar_windows_and_breakout_queries() -> None:
    calls: list[tuple[str, str, str]] = []

    def explore(keyword: str, *, geo: str, timeframe: str) -> dict[str, object]:
        calls.append((keyword, geo, timeframe))
        return {
            "interest_over_time": [
                {"date": "2025-01-01", "value": 10},
                {"date": "2026-03-25", "value": 30},
                {"date": "2026-03-30T00:00:00Z", "value": [70]},
            ],
            "related_queries": {"rising": [{"query": "Goal Heads 2", "value": "Breakout"}]},
        }

    provider = GoogleTrendsProvider(
        explore=explore
    )

    signals = provider.research("goal heads", "US")

    assert signals.trend_7d == 50.0
    assert signals.trend_30d == 50.0
    assert signals.trend_90d == 50.0
    assert signals.rising_queries == ("goal heads 2",)
    assert signals.rising_queries_observed is True
    assert calls == [("goal heads", "US", "today 3-m")]


def test_trends_uses_last_points_when_dates_are_unavailable() -> None:
    provider = GoogleTrendsProvider(
        explore=lambda keyword, **kwargs: {
            "interest_over_time": [{"value": value} for value in range(1, 11)],
            "related_queries": {"rising": []},
        }
    )

    signals = provider.research("goal heads", "US")

    assert signals.trend_7d == 7.0
    assert signals.trend_30d == 5.5
    assert signals.trend_90d == 5.5


def test_trends_uses_value_order_fallback_when_any_accepted_point_lacks_a_date() -> None:
    provider = GoogleTrendsProvider(
        explore=lambda keyword, **kwargs: {
            "interest_over_time": [
                {"date": "2026-03-30", "value": 100},
                {"value": 0},
            ],
            "related_queries": {"rising": []},
        }
    )

    assert provider.research("goal heads", "US").trend_7d == 50.0


def test_trends_calendar_windows_include_the_exact_seven_day_boundary() -> None:
    provider = GoogleTrendsProvider(
        explore=lambda keyword, **kwargs: {
            "interest_over_time": [
                {"date": "2026-03-22", "value": 10},
                {"date": "2026-03-23", "value": 90},
                {"date": "2026-03-30", "value": 100},
            ],
            "related_queries": {"rising": []},
        }
    )

    assert provider.research("goal heads", "US").trend_7d == 95.0


def test_trends_deadline_runner_times_out_without_calling_a_live_browser() -> None:
    deadlines: list[float] = []

    def runner(operation, deadline_seconds: float):
        deadlines.append(deadline_seconds)
        raise TimeoutError("deadline exceeded")

    provider = GoogleTrendsProvider(
        explore=lambda keyword, **kwargs: pytest.fail("explore should be held by the runner"),
        deadline_seconds=1.5,
        deadline_runner=runner,
    )

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        provider.research("goal heads", "US")
    assert deadlines == [1.5]


def test_default_trends_uses_the_process_runner_without_requiring_picklable_callables() -> None:
    calls: list[tuple[str, str, str, float]] = []

    def process_runner(keyword: str, geo: str, timeframe: str, deadline_seconds: float):
        calls.append((keyword, geo, timeframe, deadline_seconds))
        return {"interest_over_time": [], "related_queries": {"rising": []}}

    provider = GoogleTrendsProvider(process_runner=process_runner)

    result = provider.research("goal heads", "US")

    assert result.rising_queries == ()
    assert result.rising_queries_observed is True
    assert calls == [("goal heads", "US", "today 3-m", 90.0)]


def test_default_trends_process_timeout_terminates_then_kills_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Receiver:
        def poll(self, timeout: float) -> bool:
            events.append(("poll", timeout))
            return False

        def close(self) -> None:
            events.append("receiver-close")

    class Sender:
        def close(self) -> None:
            events.append("sender-close")

    class Process:
        pid = 4321

        def __init__(self) -> None:
            self.alive = True

        def start(self) -> None:
            events.append("start")

        def join(self, timeout: float) -> None:
            events.append(("join", timeout))

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            events.append("terminate")

        def kill(self) -> None:
            events.append("kill")
            self.alive = False

    process = Process()

    class Context:
        def Pipe(self, duplex: bool):
            assert duplex is False
            return Receiver(), Sender()

        def Process(self, **kwargs: object):
            return process

    monkeypatch.setattr(signals_module, "_trends_process_context", lambda: Context())
    monkeypatch.setattr(
        signals_module,
        "_signal_process_group",
        lambda process, signal_number: events.append(("group", signal_number)),
    )

    with pytest.raises(TimeoutError, match="exceeded"):
        signals_module._run_default_trends_process("goal heads", "US", "today 3-m", 0.5)

    assert "terminate" in events
    assert "kill" in events
    assert process.is_alive() is False
    assert "receiver-close" in events
    assert "sender-close" in events


def test_trends_ignores_invalid_and_nonfinite_values() -> None:
    provider = GoogleTrendsProvider(
        explore=lambda keyword, **kwargs: {
            "interest_over_time": [
                {"value": True},
                {"value": -1},
                {"value": 101},
                {"value": math.inf},
                {"value": "nan"},
            ],
            "related_queries": {"rising": []},
        }
    )

    assert provider.research("goal heads", "US").trend_7d is None


def test_trends_deduplicates_and_caps_rising_queries() -> None:
    rising = [{"query": f"Query {number}"} for number in range(25)]
    rising[3] = {"query": " query 1 "}
    rising.append({"query": 3})
    provider = GoogleTrendsProvider(
        explore=lambda keyword, **kwargs: {
            "interest_over_time": [],
            "related_queries": {"rising": rising},
        }
    )

    assert provider.research("goal heads", "US").rising_queries == tuple(
        f"query {number}" for number in range(21) if number != 3
    )


def test_collect_signals_degrades_when_trends_fail() -> None:
    class BrokenTrends:
        def research(self, keyword: str, geo: str):
            raise RuntimeError("trend service unavailable")

    signals = collect_signals(
        "goal heads",
        "US",
        BrokenTrends(),
        AutocompleteProvider(fetch=lambda keyword: [keyword, ["goal heads game"]]),
    )

    assert signals.autocomplete == ("goal heads game",)
    assert signals.trend_7d is None
    assert signals.rising_queries_observed is False
    assert signals.autocomplete_observed is True
    assert signals.errors == ("trends: RuntimeError: trend service unavailable",)


def test_collect_signals_keeps_partial_trends_and_provider_errors() -> None:
    class PartialTrends:
        def research(self, keyword: str, geo: str) -> SearchSignals:
            return SearchSignals(
                trend_7d=75.0,
                errors=(
                    "trends: RuntimeError: SerpAPI RELATED_QUERIES unavailable",
                ),
            )

    result = collect_signals(
        "goal heads",
        "US",
        PartialTrends(),
        AutocompleteProvider(fetch=lambda keyword: [keyword, []]),
    )

    assert result.trend_7d == 75.0
    assert result.errors == (
        "trends: RuntimeError: SerpAPI RELATED_QUERIES unavailable",
    )


def test_collect_signals_skips_disabled_providers_without_an_error() -> None:
    class MustNotRun:
        def research(self, keyword: str, geo: str):
            raise AssertionError("should not run")

        def competition(self, keyword: str):
            raise AssertionError("should not run")

    signals = collect_signals(
        "goal heads",
        "US",
        MustNotRun(),
        AutocompleteProvider(fetch=lambda keyword: [keyword, []]),
        include_trends=False,
        serp=MustNotRun(),
        include_serp=False,
    )

    assert signals.errors == ()
    assert signals.rising_queries_observed is False
    assert signals.autocomplete_observed is True


def test_collect_signals_marks_disabled_autocomplete_as_unobserved() -> None:
    class MustNotRun:
        def research(self, keyword: str, geo: str):
            raise AssertionError("should not run")

        def suggest(self, keyword: str):
            raise AssertionError("should not run")

    signals = collect_signals(
        "goal heads",
        "US",
        MustNotRun(),
        MustNotRun(),
        include_trends=False,
        include_autocomplete=False,
    )

    assert signals.errors == ()
    assert signals.rising_queries_observed is False
    assert signals.autocomplete_observed is False


def test_collect_signals_records_successful_empty_provider_responses() -> None:
    trends = GoogleTrendsProvider(
        explore=lambda keyword, **kwargs: {
            "interest_over_time": [],
            "related_queries": {"rising": []},
        }
    )
    autocomplete = AutocompleteProvider(fetch=lambda keyword: [keyword, []])

    result = collect_signals("goal heads", "US", trends, autocomplete)

    assert result.rising_queries == ()
    assert result.autocomplete == ()
    assert result.rising_queries_observed is True
    assert result.autocomplete_observed is True


def test_collect_signals_marks_failed_autocomplete_as_unobserved() -> None:
    class BrokenAutocomplete:
        def suggest(self, keyword: str):
            raise RuntimeError("autocomplete unavailable")

    result = collect_signals(
        "goal heads",
        "US",
        GoogleTrendsProvider(
            explore=lambda keyword, **kwargs: {
                "interest_over_time": [],
                "related_queries": {"rising": []},
            }
        ),
        BrokenAutocomplete(),
    )

    assert result.autocomplete_observed is False
    assert result.rising_queries_observed is True


def test_collect_signals_orders_simultaneous_provider_errors() -> None:
    class Broken:
        def research(self, keyword: str, geo: str):
            raise ValueError("first\nhttps://secret.example/path?token=redacted\x1bsecond")

        def suggest(self, keyword: str):
            raise KeyError("autocomplete")

        def competition(self, keyword: str):
            raise OSError("serp")

    signals = collect_signals("goal heads", "US", Broken(), Broken(), serp=Broken(), include_serp=True)

    assert signals.errors == (
        "trends: ValueError: first <url> second",
        "autocomplete: KeyError: 'autocomplete'",
        "serp: OSError: serp",
    )


def test_collect_signals_bounds_sanitized_provider_errors() -> None:
    class BrokenTrends:
        def research(self, keyword: str, geo: str):
            raise RuntimeError("x" * 500)

    signals = collect_signals(
        "goal heads",
        "US",
        BrokenTrends(),
        AutocompleteProvider(fetch=lambda keyword: [keyword, []]),
    )

    assert signals.errors[0].startswith("trends: RuntimeError: ")
    assert len(signals.errors[0]) == 160


def test_collect_signals_redacts_common_secret_forms_without_hiding_plain_prose() -> None:
    class BrokenTrends:
        def research(self, keyword: str, geo: str):
            raise RuntimeError(
                "Bearer top-secret token=abc api_key: xyz password='pw' key features remain monkey=value"
            )

    signals = collect_signals(
        "goal heads",
        "US",
        BrokenTrends(),
        AutocompleteProvider(fetch=lambda keyword: [keyword, []]),
    )

    error = signals.errors[0]
    assert "Bearer <redacted>" in error
    assert "token=<redacted>" in error
    assert "api_key: <redacted>" in error
    assert "password='<redacted>'" in error
    assert "key features remain" in error
    assert "monkey=value" in error


def test_serp_parses_duckduckgo_redirects_and_direct_links() -> None:
    html = """
    <a class='result__a' href='https://poki.com/en/g/game'>Poki</a>
    <a class='result__a' href='/l/?uddg=https%3A%2F%2Fwww.crazygames.com%2Fgame'>Crazy</a>
    <a class='result__a' href='mailto:test@example.com'>Email</a>
    <a class='result__a' href='https://poki.com/other'>Duplicate</a>
    """

    assert parse_serp_hosts(html) == ["poki.com", "crazygames.com"]


def test_serp_ignores_duckduckgo_redirect_without_a_usable_target() -> None:
    html = "<a class='result__a' href='/l/?foo=bar'>Empty redirect</a>"

    assert parse_serp_hosts(html) == []


def test_serp_keeps_external_uddg_and_scans_past_invalid_or_duplicate_anchors() -> None:
    html = """
    <a class='result__a' href='https://example.com/path?uddg=https%3A%2F%2Fpoki.com'>External</a>
    <a class='result__a' href='https://[broken'>Broken bracket</a>
    <a class='result__a' href='https://example.net:bad-port/path'>Broken port</a>
    <a class='result__a' href='https://example.com/again'>Duplicate</a>
    """ + "".join(
        f"<a class='result__a' href='https://host{number}.example/path'>Host</a>"
        for number in range(9)
    )

    assert parse_serp_hosts(html) == ["example.com"] + [
        f"host{number}.example" for number in range(9)
    ]


def test_default_serp_request_uses_duckduckgo_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str], tuple[int, int]]] = []
    status_called = False
    session_built = False

    class Response:
        text = """
        <a class='result__a' href='https://poki.com/en/g/game'>Poki</a>
        <a class='result__a' href='https://example.com/game'>Example</a>
        """

        def raise_for_status(self) -> None:
            nonlocal status_called
            status_called = True

    class Session:
        def get(
            self,
            url: str,
            *,
            params: dict[str, str],
            timeout: tuple[int, int],
            allow_redirects: bool,
        ) -> Response:
            assert allow_redirects is False
            calls.append((url, params, timeout))
            return Response()

    def fake_build_session() -> Session:
        nonlocal session_built
        session_built = True
        return Session()

    monkeypatch.setattr(signals_module, "build_session", fake_build_session)

    assert SerpCompetitionProvider().competition("goal heads") == 0.5
    assert session_built is True
    assert calls == [
        (
            "https://html.duckduckgo.com/html/",
            {"q": "goal heads", "kl": "us-en"},
            (10, 25),
        )
    ]
    assert status_called is True


def test_default_serp_caches_and_paces_uncached_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    class Response:
        text = """
        <a class='result__a' href='https://poki.com/en/g/game'>Poki</a>
        <a class='result__a' href='https://example.com/game'>Example</a>
        """

        def raise_for_status(self) -> None:
            pass

    class Session:
        def get(self, url: str, *, params: dict[str, str], **kwargs: object) -> Response:
            calls.append(params["q"])
            return Response()

    monkeypatch.setattr(signals_module, "build_session", Session)
    provider = SerpCompetitionProvider(monotonic=lambda: 0.0, sleep=sleeps.append)

    assert provider.competition("Goal Heads") == 0.5
    assert provider.competition(" goal\u00a0heads ") == 0.5
    assert provider.competition("other") == 0.5
    assert calls == ["Goal Heads", "other"]
    assert sleeps == [1.0]


def test_default_serp_shares_cache_pacing_and_expires_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    calls: list[str] = []
    sleeps: list[float] = []

    class Response:
        text = """
        <a class='result__a' href='https://poki.com/en/g/game'>Poki</a>
        <a class='result__a' href='https://example.com/game'>Example</a>
        """

        def raise_for_status(self) -> None:
            pass

    class Session:
        def get(self, url: str, *, params: dict[str, str], **kwargs: object) -> Response:
            calls.append(params["q"])
            return Response()

    def clock() -> float:
        return now

    monkeypatch.setattr(signals_module, "build_session", Session)
    signals_module._reset_default_http_state(monotonic=clock, sleep=sleeps.append)
    try:
        first = SerpCompetitionProvider()
        second = SerpCompetitionProvider()

        assert first.competition("Goal Heads") == 0.5
        assert second.competition(" goal\u00a0heads ") == 0.5
        assert second.competition("other") == 0.5
        assert calls == ["Goal Heads", "other"]
        assert sleeps == [1.0]

        now = 60 * 60
        assert first.competition("goal heads") == 0.5
        assert calls == ["Goal Heads", "other", "goal heads"]
    finally:
        signals_module._reset_default_http_state()


def test_serp_competition_counts_real_strong_subdomains_not_lookalikes() -> None:
    provider = SerpCompetitionProvider(
        fetch_hosts=lambda keyword: [
            "https://cdn.poki.com/game",
            "https://notpoki.com/game",
            "https://itch.io/game",
            "https://www.example.com/game",
            "https://coolmathgames.com/game",
        ]
    )

    assert provider.competition("goal heads") == 0.6


def test_injected_serp_can_enable_production_pacing_without_losing_cache() -> None:
    calls: list[str] = []
    sleeps: list[float] = []
    provider = SerpCompetitionProvider(
        fetch_hosts=lambda keyword: calls.append(keyword) or ["poki.com"],
        pace_custom=True,
        monotonic=lambda: 0.0,
        sleep=sleeps.append,
    )

    provider.competition("alpha")
    provider.competition(" alpha ")
    provider.competition("beta")

    assert calls == ["alpha", "beta"]
    assert sleeps == [1.0]


def test_serp_returns_none_without_valid_hosts() -> None:
    provider = SerpCompetitionProvider(fetch_hosts=lambda keyword: ["mailto:a@example.com", "bad host"])

    assert provider.competition("goal heads") is None


def test_injected_providers_do_not_trigger_trendspyg_import() -> None:
    sys.modules.pop("trendspyg", None)
    provider = GoogleTrendsProvider(
        explore=lambda keyword, **kwargs: {"interest_over_time": [], "related_queries": {"rising": []}}
    )

    provider.research("goal heads", "US")

    assert "trendspyg" not in sys.modules


def _serpapi_timeseries_payload(values: list[int], base_timestamp: int = 1777852800) -> dict[str, object]:
    return {
        "interest_over_time": {
            "timeline_data": [
                {
                    "date": f"Day {index}",
                    "timestamp": str(base_timestamp + index * 86400),
                    "values": [
                        {"query": "test", "value": str(value), "extracted_value": value}
                    ],
                }
                for index, value in enumerate(values)
            ]
        }
    }


def _serpapi_related_payload(rising: list[dict[str, object]]) -> dict[str, object]:
    return {"related_queries": {"rising": rising}}


def test_serpapi_trends_parses_timeseries_and_related_queries() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        calls.append((engine, params))
        if params["data_type"] == "TIMESERIES":
            return _serpapi_timeseries_payload([30, 70], base_timestamp=1711324800)
        return _serpapi_related_payload(
            [{"query": "goal heads 2", "value": "Breakout", "extracted_value": 20200}]
        )

    provider = SerpApiTrendsProvider("test-key", fetch=fetch)
    signals = provider.research("goal heads", "US")

    assert signals.trend_7d == 50.0
    assert signals.trend_30d == 50.0
    assert signals.trend_90d == 50.0
    assert signals.rising_queries == ("goal heads 2",)
    assert signals.rising_queries_observed is True
    assert len(calls) == 2
    assert calls[0] == ("google_trends", {"q": "goal heads", "geo": "US", "data_type": "TIMESERIES"})
    assert calls[1] == ("google_trends", {"q": "goal heads", "geo": "US", "data_type": "RELATED_QUERIES"})


def test_serpapi_trends_caches_results() -> None:
    calls: list[str] = []

    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        calls.append(params["data_type"])
        if params["data_type"] == "TIMESERIES":
            return _serpapi_timeseries_payload([50])
        return _serpapi_related_payload([])

    provider = SerpApiTrendsProvider("test-key", fetch=fetch)
    first = provider.research("goal heads", "US")
    second = provider.research("goal heads", "US")
    third = provider.research("Goal Heads", "us")

    assert first is second
    assert first is third
    assert calls == ["TIMESERIES", "RELATED_QUERIES"]


def test_serpapi_trends_paces_default_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    calls: list[str] = []

    class Response:
        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return self._data

    class Session:
        def get(self, url: str, *, params: dict[str, str], **kwargs: object) -> Response:
            calls.append(params["data_type"])
            if params["data_type"] == "TIMESERIES":
                return Response(_serpapi_timeseries_payload([50]))
            return Response(_serpapi_related_payload([]))

    monkeypatch.setattr(signals_module, "build_session", lambda: Session())
    provider = SerpApiTrendsProvider("test-key", monotonic=lambda: 0.0, sleep=sleeps.append)

    provider.research("goal heads", "US")

    assert len(calls) == 2
    assert sleeps == [1.0]


def test_serpapi_trends_handles_empty_rising_queries() -> None:
    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        if params["data_type"] == "TIMESERIES":
            return _serpapi_timeseries_payload([50])
        return _serpapi_related_payload([])

    provider = SerpApiTrendsProvider("test-key", fetch=fetch)
    signals = provider.research("goal heads", "US")

    assert signals.rising_queries == ()
    assert signals.rising_queries_observed is True


def test_serpapi_trends_keeps_timeseries_when_related_queries_are_missing() -> None:
    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        if params["data_type"] == "TIMESERIES":
            return _serpapi_timeseries_payload([40, 60])
        return {"search_metadata": {"status": "Success"}}

    signals = SerpApiTrendsProvider("test-key", fetch=fetch).research(
        "goal heads", "US"
    )

    assert signals.trend_7d == 50.0
    assert signals.trend_30d == 50.0
    assert signals.trend_90d == 50.0
    assert signals.rising_queries == ()
    assert signals.rising_queries_observed is False
    assert len(signals.errors) == 1
    assert "SerpAPI RELATED_QUERIES" in signals.errors[0]


def test_serpapi_trends_keeps_related_queries_when_timeseries_is_missing() -> None:
    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        if params["data_type"] == "TIMESERIES":
            return {"search_metadata": {"status": "Success"}}
        return _serpapi_related_payload(
            [{"query": "goal heads online", "extracted_value": 500}]
        )

    signals = SerpApiTrendsProvider("test-key", fetch=fetch).research(
        "goal heads", "US"
    )

    assert signals.trend_7d is None
    assert signals.trend_30d is None
    assert signals.trend_90d is None
    assert signals.rising_queries == ("goal heads online",)
    assert signals.rising_queries_observed is True
    assert len(signals.errors) == 1
    assert "SerpAPI TIMESERIES" in signals.errors[0]


def test_serpapi_trends_still_fetches_related_queries_after_timeseries_request_fails() -> None:
    calls: list[str] = []

    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        calls.append(params["data_type"])
        if params["data_type"] == "TIMESERIES":
            raise OSError("temporary outage")
        return _serpapi_related_payload([{"query": "goal heads game"}])

    signals = SerpApiTrendsProvider("test-key", fetch=fetch).research(
        "goal heads", "US"
    )

    assert calls == ["TIMESERIES", "RELATED_QUERIES"]
    assert signals.rising_queries == ("goal heads game",)
    assert any("TIMESERIES" in error for error in signals.errors)


def test_serpapi_diagnostics_redact_secrets_and_urls() -> None:
    secret = "secret-value"

    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        return {
            "api_key": secret,
            "error": f"token={secret} https://example.test/private",
            "search_metadata": {"status": "Error"},
        }

    signals = SerpApiTrendsProvider("test-key", fetch=fetch).research(
        "goal heads", "US"
    )
    rendered = " ".join(signals.errors)

    assert secret not in rendered
    assert "https://example.test/private" not in rendered
    assert "<redacted>" in rendered
    assert "<url>" in rendered
    assert "status=Error" in rendered
    assert "keys=error,search_metadata" in rendered


def test_serpapi_trends_does_not_cache_when_both_components_fail() -> None:
    calls: list[str] = []

    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        calls.append(params["data_type"])
        return {"error": "no data", "search_metadata": {"status": "Error"}}

    provider = SerpApiTrendsProvider("test-key", fetch=fetch)
    first = provider.research("goal heads", "US")
    second = provider.research("goal heads", "US")

    assert calls == [
        "TIMESERIES",
        "RELATED_QUERIES",
        "TIMESERIES",
        "RELATED_QUERIES",
    ]
    assert len(first.errors) == 2
    assert len(second.errors) == 2


def test_serpapi_trends_skips_entries_with_invalid_timestamps_or_values() -> None:
    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        if params["data_type"] == "TIMESERIES":
            return {
                "interest_over_time": {
                    "timeline_data": [
                        {
                            "date": "Day 0",
                            "timestamp": "not-a-number",
                            "values": [{"query": "test", "extracted_value": 50}],
                        },
                        {
                            "date": "Day 1",
                            "timestamp": "1711324800",
                            "values": [{"query": "test", "extracted_value": 70}],
                        },
                        {
                            "date": "Day 2",
                            "timestamp": "1711411200",
                            "values": [],
                        },
                    ]
                }
            }
        return _serpapi_related_payload([])

    provider = SerpApiTrendsProvider("test-key", fetch=fetch)
    signals = provider.research("goal heads", "US")

    assert signals.trend_7d == 70.0


def test_serpapi_trends_uses_extracted_value_over_value_string() -> None:
    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        if params["data_type"] == "TIMESERIES":
            return {
                "interest_over_time": {
                    "timeline_data": [
                        {
                            "date": "Day 0",
                            "timestamp": "1711324800",
                            "values": [
                                {"query": "test", "value": "75", "extracted_value": 75}
                            ],
                        }
                    ]
                }
            }
        return _serpapi_related_payload([])

    provider = SerpApiTrendsProvider("test-key", fetch=fetch)
    signals = provider.research("goal heads", "US")

    assert signals.trend_7d == 75.0


def test_collect_signals_works_with_serpapi_trends_provider() -> None:
    def fetch(engine: str, params: dict[str, str]) -> dict[str, object]:
        if params["data_type"] == "TIMESERIES":
            return _serpapi_timeseries_payload([60])
        return _serpapi_related_payload([{"query": "goal heads online", "extracted_value": 500}])

    trends = SerpApiTrendsProvider("test-key", fetch=fetch)
    autocomplete = AutocompleteProvider(fetch=lambda keyword: [keyword, ["goal heads game"]])

    signals = collect_signals("goal heads", "US", trends, autocomplete)

    assert signals.trend_7d == 60.0
    assert signals.rising_queries == ("goal heads online",)
    assert signals.autocomplete == ("goal heads game",)
    assert signals.errors == ()


def test_serpapi_trends_default_fetch_uses_correct_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str], tuple[int, int]]] = []
    status_called = False

    class Response:
        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            nonlocal status_called
            status_called = True

        def json(self) -> dict[str, object]:
            return self._data

    class Session:
        def get(
            self,
            url: str,
            *,
            params: dict[str, str],
            timeout: tuple[int, int],
            allow_redirects: bool,
        ) -> Response:
            assert allow_redirects is False
            calls.append((url, params, timeout))
            if params["data_type"] == "TIMESERIES":
                return Response(_serpapi_timeseries_payload([50]))
            return Response(_serpapi_related_payload([]))

    monkeypatch.setattr(signals_module, "build_session", lambda: Session())

    provider = SerpApiTrendsProvider("my-api-key")
    provider.research("goal heads", "US")

    assert status_called is True
    assert len(calls) == 2
    for url, params, timeout in calls:
        assert url == "https://serpapi.com/search"
        assert params["api_key"] == "my-api-key"
        assert params["engine"] == "google_trends"
        assert timeout == (10, 20)


def test_serpapi_trends_rotates_keys_round_robin(monkeypatch: pytest.MonkeyPatch) -> None:
    keys_used: list[str] = []

    class Response:
        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return self._data

    class Session:
        def get(self, url: str, *, params: dict[str, str], **kwargs: object) -> Response:
            keys_used.append(params["api_key"])
            if params["data_type"] == "TIMESERIES":
                return Response(_serpapi_timeseries_payload([50]))
            return Response(_serpapi_related_payload([]))

    monkeypatch.setattr(signals_module, "build_session", lambda: Session())

    provider = SerpApiTrendsProvider(("key-a", "key-b", "key-c"))

    # Each research() makes 2 API calls (TIMESERIES + RELATED_QUERIES)
    provider.research("alpha", "US")   # calls 1,2 -> key-a, key-b
    provider.research("beta", "US")    # calls 3,4 -> key-c, key-a
    provider.research("gamma", "US")   # calls 5,6 -> key-b, key-c

    assert keys_used == ["key-a", "key-b", "key-c", "key-a", "key-b", "key-c"]


def test_serpapi_trends_rejects_empty_key_pool() -> None:
    with pytest.raises(ValueError, match="At least one SerpAPI key"):
        SerpApiTrendsProvider(())


def test_serpapi_trends_accepts_single_key_as_string() -> None:
    provider = SerpApiTrendsProvider("single-key", fetch=lambda e, p: _serpapi_timeseries_payload([50]) if p["data_type"] == "TIMESERIES" else _serpapi_related_payload([]))

    assert provider._api_keys == ("single-key",)
    signals = provider.research("test", "US")
    assert signals.trend_7d == 50.0
