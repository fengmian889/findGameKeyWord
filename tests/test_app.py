from datetime import UTC, datetime

import pytest

from poki_seo_monitor.app import DiscoveryResult, Monitor, ResearchResult
from poki_seo_monitor.models import DiscoveredGame
from poki_seo_monitor.state import MonitorState


NOW = datetime(2026, 8, 3, tzinfo=UTC)
URLS = tuple(f"https://poki.com/en/g/game-{index}" for index in range(5))


def discovery(*urls: str, degraded=False, errors=()):
    return DiscoveryResult(
        tuple(DiscoveredGame(url, ("new_games",), index + 1) for index, url in enumerate(urls)),
        degraded,
        errors,
    )


def test_first_run_samples_baseline_without_notifications(tmp_path) -> None:
    calls = []
    monitor = Monitor(
        tmp_path / "state.json",
        lambda: discovery(*URLS),
        lambda url, notify: calls.append((url, notify)) or ResearchResult(False, f"/{url.rsplit('/', 1)[-1]}.md"),
        lambda url: pytest.fail("no notification retry"),
        max_games=1,
        baseline_sample_size=2,
    )

    summary = monitor.run(NOW)

    assert calls == [(URLS[0], False)]
    assert summary.baseline is True
    assert summary.processed == summary.completed == 1
    assert MonitorState.load(tmp_path / "state.json").games[URLS[4]]["status"] == "baseline"


def test_monitor_passes_persisted_freshness_context_and_rechecks_decay(tmp_path) -> None:
    contexts = []

    def research(url, notify, context):
        contexts.append(context)
        return ResearchResult(trends_missing=True)

    monitor = Monitor.for_test(
        tmp_path,
        lambda: DiscoveryResult(
            (DiscoveredGame(URLS[0], ("new_games", "sitemap"), 3),)
        ),
        research,
        baseline_sample_size=1,
    )

    monitor.run(NOW)
    monitor.run(datetime(2026, 8, 10, tzinfo=UTC))

    assert contexts[0].sources == ("new_games", "sitemap")
    assert contexts[0].new_games_rank == 3
    assert contexts[0].first_seen == NOW.isoformat()
    assert contexts[0].freshness > contexts[1].freshness


def test_prescribed_constructor_adapts_simple_discovery_and_research_results(tmp_path) -> None:
    calls = []
    monitor = Monitor(
        state_path=tmp_path / "state.json",
        discover=lambda: [URLS[0], URLS[1]],
        research=lambda url, notify: calls.append((url, notify)) or True,
        max_games=2,
        baseline_sample_size=1,
    )

    summary = monitor.run(NOW)

    assert summary.completed == 1
    assert calls == [(URLS[0], False)]
    assert MonitorState.load(tmp_path / "state.json").games[URLS[0]]["recheck_at"]


def test_for_test_accepts_none_research_result_and_optional_retry(tmp_path) -> None:
    monitor = Monitor.for_test(
        root=tmp_path,
        discover=lambda: [URLS[0]],
        research=lambda url, notify: None,
        max_games=1,
        baseline_sample_size=1,
    )

    assert monitor.run(NOW).completed == 1
    assert (tmp_path / "state.json").exists()


def test_for_test_plan_contract_baselines_then_researches_new_url(tmp_path) -> None:
    discovered = [[URLS[0]], [URLS[0], URLS[1]]]
    researched = []
    monitor = Monitor.for_test(
        tmp_path,
        discover=lambda: discovered.pop(0),
        research=lambda url, notify: researched.append((url, notify)),
    )

    monitor.run(NOW)
    monitor.run(datetime(2026, 8, 3, 6, tzinfo=UTC))

    assert researched == [(URLS[0], False), (URLS[1], True)]


def test_later_runs_process_only_new_and_keep_over_budget_pending(tmp_path) -> None:
    path = tmp_path / "state.json"
    Monitor(path, lambda: discovery(URLS[0]), lambda *_: ResearchResult(False), lambda _: ResearchResult(False), 1, 0).run(NOW)
    calls = []
    current = [URLS[0], URLS[1], URLS[2]]
    monitor = Monitor(path, lambda: discovery(*current), lambda url, notify: calls.append(url) or ResearchResult(False), lambda _: ResearchResult(False), 1, 0)

    first = monitor.run(NOW)
    current[:] = [URLS[0], URLS[1], URLS[2]]
    second = monitor.run(NOW)

    assert calls == [URLS[1], URLS[2]]
    assert first.new == 2 and first.processed == 1
    assert second.new == 0 and second.processed == 1


def test_one_failure_continues_and_retries_when_due(tmp_path) -> None:
    path = tmp_path / "state.json"
    Monitor(path, lambda: discovery(), lambda *_: ResearchResult(False), lambda _: ResearchResult(False), 5, 0).run(NOW)
    attempts = []

    def research(url, notify):
        attempts.append(url)
        if attempts.count(url) == 1 and url == URLS[0]:
            raise RuntimeError("token=secret\nfailed")
        return ResearchResult(False)

    monitor = Monitor(path, lambda: discovery(URLS[0], URLS[1]), research, lambda _: ResearchResult(False), 5, 0)
    first = monitor.run(NOW)
    second = monitor.run(datetime(2026, 8, 4, tzinfo=UTC))

    assert first.completed == 1 and first.failed == 1
    assert "secret" not in " ".join(first.errors)
    assert second.completed == 1
    assert attempts == [URLS[0], URLS[1], URLS[0]]


def test_discovery_failure_leaves_state_untouched(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("original", encoding="utf-8")
    monitor = Monitor(path, lambda: (_ for _ in ()).throw(RuntimeError("both sources failed")), lambda *_: ResearchResult(False), lambda _: ResearchResult(False), 1, 1)

    with pytest.raises(RuntimeError, match="both"):
        monitor.run(NOW)

    assert path.read_text(encoding="utf-8") == "original"


def test_degraded_discovery_is_reflected_in_summary(tmp_path) -> None:
    monitor = Monitor(tmp_path / "state.json", lambda: discovery(degraded=True, errors=("new_games: down",)), lambda *_: ResearchResult(False), lambda _: ResearchResult(False), 1, 0)

    result = monitor.run(NOW)

    assert result.degraded is True
    assert result.errors == ("new_games: down",)


def test_notification_failure_retries_without_research(tmp_path) -> None:
    path = tmp_path / "state.json"
    Monitor(path, lambda: discovery(), lambda *_: ResearchResult(False), lambda _: ResearchResult(False), 2, 0).run(NOW)
    research_calls = []
    retry_calls = []
    issue_attempt = 0

    def research(url, notify):
        research_calls.append(url)
        return ResearchResult(False, "/report.md", issue_error="temporary")

    monitor = Monitor(path, lambda: discovery(URLS[0]), research, lambda url: retry_calls.append(url) or ResearchResult(False, "/report.md", 42), 2, 0)
    first = monitor.run(NOW)
    second = monitor.run(NOW)

    assert first.completed == 1
    assert second.notification_retried == 1 and second.processed == 0
    assert research_calls == [URLS[0]]
    assert retry_calls == [URLS[0]]
    assert MonitorState.load(path).games[URLS[0]]["issue_number"] == 42


def test_run_level_notification_saves_pending_before_batch_and_retries_together(
    tmp_path,
) -> None:
    path = tmp_path / "state.json"
    Monitor(path, lambda: discovery(), lambda *_: ResearchResult(False), max_games=3, baseline_sample_size=0).run(NOW)
    batches = []
    fail = True

    def research(url, notify):
        return ResearchResult(
            False,
            f"/{url.rsplit('/', 1)[-1]}.md",
            notification_pending=True,
        )

    def publish(urls):
        nonlocal fail
        batches.append(tuple(urls))
        saved = MonitorState.load(path)
        assert all(saved.games[url]["notification_pending"] for url in urls)
        if fail:
            fail = False
            return {
                url: ResearchResult(False, issue_error="temporary") for url in urls
            }
        return {url: ResearchResult(False, issue_number=91) for url in urls}

    monitor = Monitor(
        path,
        lambda: discovery(URLS[0], URLS[1]),
        research,
        max_games=3,
        baseline_sample_size=0,
        publish_notifications=publish,
    )

    first = monitor.run(NOW)
    second = monitor.run(NOW)

    assert first.completed == 2 and second.processed == 0
    assert second.notification_retried == 2
    assert batches == [(URLS[0], URLS[1]), (URLS[0], URLS[1])]
    assert all(
        game["issue_number"] == 91 and not game["notification_pending"]
        for game in MonitorState.load(path).games.values()
    )


def test_run_rejects_naive_datetime_before_discovery(tmp_path) -> None:
    called = []
    monitor = Monitor(tmp_path / "state.json", lambda: called.append(True) or discovery(), lambda *_: ResearchResult(False), lambda _: ResearchResult(False), 1, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        monitor.run(datetime(2026, 8, 3))
    assert called == []
