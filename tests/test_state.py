from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from poki_seo_monitor.models import DiscoveredGame
from poki_seo_monitor.state import MonitorState


URL = "https://poki.com/en/g/example"
OTHER_URL = "https://poki.com/en/g/other"


def at(day: int = 1) -> datetime:
    return datetime(2026, 1, day, 9, 30, tzinfo=UTC)


def test_baseline_records_urls_without_reporting_them() -> None:
    state = MonitorState()

    assert state.diff([URL, URL], at(), baseline=True) == []
    assert state.games[URL] == {
        "first_seen": at().isoformat(),
        "status": "baseline",
        "recheck_at": [],
        "retry_at": None,
        "recheck_plan_started": False,
    }


def test_new_urls_are_reported_once_in_input_order() -> None:
    state = MonitorState()

    assert state.diff([URL, OTHER_URL, URL], at(), baseline=False) == [URL, OTHER_URL]
    assert state.diff([OTHER_URL, URL], at(2), baseline=False) == []


def test_discovery_provenance_tracks_sources_rank_and_source_first_seen() -> None:
    state = MonitorState()

    state.diff([DiscoveredGame(URL, ("new_games",), 4)], at(), baseline=False)
    state.diff(
        [DiscoveredGame(URL, ("new_games", "sitemap"), 2)],
        at(2),
        baseline=False,
    )

    assert state.games[URL]["sources"] == ["new_games", "sitemap"]
    assert state.games[URL]["new_games_rank"] == 2
    assert state.games[URL]["source_first_seen"] == {
        "new_games": at().isoformat(),
        "sitemap": at(2).isoformat(),
    }


def test_freshness_is_bounded_and_decays_with_age_rank_and_source_lag() -> None:
    top = MonitorState()
    top.diff(
        [DiscoveredGame(URL, ("new_games", "sitemap"), 1)],
        at(),
        baseline=False,
    )
    lower = MonitorState()
    lower.diff(
        [DiscoveredGame(URL, ("new_games", "sitemap"), 51)],
        at(),
        baseline=False,
    )
    lagged = MonitorState()
    lagged.diff([DiscoveredGame(URL, ("new_games",), 1)], at(), baseline=False)
    lagged.diff([DiscoveredGame(URL, ("sitemap",))], at(8), baseline=False)

    assert top.freshness(URL, at()) == 1.0
    assert 0.0 <= lower.freshness(URL, at()) < top.freshness(URL, at()) <= 1.0
    assert lagged.freshness(URL, at(8)) < top.freshness(URL, at(8))
    assert top.freshness(URL, at(15)) < top.freshness(URL, at(8))
    assert top.freshness(URL, datetime(2026, 3, 1, tzinfo=UTC)) == 0.0


def test_legacy_state_without_provenance_has_conservative_decaying_freshness() -> None:
    state = MonitorState(games={URL: valid_game()})

    assert 0.0 < state.freshness(URL, at()) < 1.0
    assert state.freshness(URL, at(15)) < state.freshness(URL, at())


def test_diff_rejects_invalid_later_url_without_partial_mutation() -> None:
    state = MonitorState()

    with pytest.raises(ValueError, match="URL"):
        state.diff([URL, "   "], at(), baseline=False)

    assert state.games == {}


def test_save_and_load_round_trip(tmp_path) -> None:
    path = tmp_path / "nested" / "state.json"
    state = MonitorState()
    state.diff([URL], at(), baseline=False)
    state.mark_research(URL, at(2), trends_missing=True)

    state.save(path)

    assert path.read_text(encoding="utf-8").endswith("\n")
    assert MonitorState.load(path) == state


def test_missing_state_loads_empty(tmp_path) -> None:
    assert MonitorState.load(tmp_path / "not-there.json") == MonitorState()


def test_missing_trends_schedules_each_recheck_once() -> None:
    state = MonitorState()
    state.diff([URL], at(), baseline=False)

    state.mark_research(URL, at(), trends_missing=True)
    state.mark_research(URL, at(2), trends_missing=True)

    assert state.games[URL]["recheck_at"] == [
        at(8).isoformat(),
        at(15).isoformat(),
        at(31).isoformat(),
    ]
    assert state.games[URL]["recheck_plan_started"] is True


def test_due_rechecks_consumes_only_due_trend_dates() -> None:
    state = MonitorState()
    state.diff([URL], at(), baseline=False)
    state.mark_research(URL, at(), trends_missing=True)

    assert state.due_rechecks(at(15)) == [URL]
    assert state.games[URL]["recheck_at"] == [at(31).isoformat()]
    assert state.due_rechecks(at(15)) == []


def test_due_rechecks_limit_does_not_consume_unselected_urls() -> None:
    state = MonitorState()
    state.diff([URL, OTHER_URL], at(), baseline=False)
    state.mark_research(URL, at(), trends_missing=True)
    state.mark_research(OTHER_URL, at(), trends_missing=True)

    assert state.due_rechecks(at(8), limit=1) == [URL]
    assert state.due_rechecks(at(8), limit=1) == [OTHER_URL]


def test_pending_urls_preserve_insertion_order() -> None:
    state = MonitorState()
    state.diff([OTHER_URL, URL], at(), baseline=False)

    assert state.pending_urls() == [OTHER_URL, URL]


def test_notification_metadata_defaults_and_transitions_round_trip(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = MonitorState()
    state.diff([URL], at(), baseline=False)
    state.mark_research(URL, at(), trends_missing=False)
    state.mark_publication(URL, "/tmp/report.md", None, "GitHub unavailable")

    assert state.notification_pending_urls() == [URL]
    assert state.games[URL]["notification_pending"] is True
    state.save(path)
    loaded = MonitorState.load(path)
    loaded.mark_notification_result(URL, 17, None)

    assert loaded.notification_pending_urls() == []
    assert loaded.games[URL]["issue_number"] == 17
    assert loaded.games[URL]["issue_error"] is None


def test_final_recheck_does_not_restart_plan() -> None:
    state = MonitorState()
    state.diff([URL], at(), baseline=False)
    state.mark_research(URL, at(), trends_missing=True)
    assert state.due_rechecks(at(31)) == [URL]

    state.mark_research(URL, at(31), trends_missing=True)

    assert state.games[URL]["recheck_at"] == []
    assert state.games[URL]["recheck_plan_started"] is True


def test_error_schedules_retry_and_dedupes_it_with_trend_due() -> None:
    state = MonitorState()
    state.diff([URL], at(), baseline=False)
    state.mark_research(URL, at(), trends_missing=True)

    state.mark_error(URL, "network unavailable", at(7))

    assert state.games[URL]["status"] == "retry"
    assert state.games[URL]["last_error"] == "network unavailable"
    assert state.games[URL]["retry_at"] == at(8).isoformat()
    assert state.due_rechecks(at(8)) == [URL]
    assert state.games[URL]["retry_at"] is None
    assert state.games[URL]["recheck_at"] == [at(15).isoformat(), at(31).isoformat()]


def test_empty_error_message_is_normalized_to_a_saveable_value(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = MonitorState()
    state.diff([URL], at(), baseline=False)

    state.mark_error(URL, "", at())
    state.save(path)

    assert state.games[URL]["last_error"] == "unknown error"
    assert MonitorState.load(path).games[URL]["last_error"] == "unknown error"


def test_consuming_a_retry_restores_a_saveable_research_state(tmp_path) -> None:
    state = MonitorState()
    state.diff([URL], at(), baseline=False)
    state.mark_research(URL, at(), trends_missing=True)
    state.mark_error(URL, "network unavailable", at(7))

    assert state.due_rechecks(at(8)) == [URL]
    assert state.games[URL]["status"] == "researched"
    assert "last_error" not in state.games[URL]

    state.save(tmp_path / "state.json")


def test_successful_trends_clear_remaining_checks_and_retry() -> None:
    state = MonitorState()
    state.diff([URL], at(), baseline=False)
    state.mark_research(URL, at(), trends_missing=True)
    state.mark_error(URL, "temporary", at(2))

    state.mark_research(URL, at(3), trends_missing=False)

    assert state.games[URL]["status"] == "researched"
    assert state.games[URL]["recheck_at"] == []
    assert state.games[URL]["retry_at"] is None
    assert "last_error" not in state.games[URL]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": 2, "games": {}}, "schema_version"),
        ({"schema_version": True, "games": {}}, "schema_version"),
        ({"schema_version": 1.0, "games": {}}, "schema_version"),
        ([], "top-level"),
        ({"schema_version": 1, "games": []}, "games"),
    ],
)
def test_load_rejects_unsupported_or_malformed_state(tmp_path, payload, message) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        MonitorState.load(path)


def test_load_propagates_corrupt_json(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        MonitorState.load(path)


def valid_game() -> dict[str, object]:
    return {
        "first_seen": at().isoformat(),
        "status": "pending",
        "recheck_at": [],
        "retry_at": None,
        "recheck_plan_started": False,
    }


@pytest.mark.parametrize(
    ("url", "record", "field"),
    [
        (URL, [], "record"),
        (URL, {key: value for key, value in valid_game().items() if key != "first_seen"}, "first_seen"),
        (URL, {**valid_game(), "status": "unknown"}, "status"),
        (URL, {**valid_game(), "status": ["pending"]}, "status"),
        (URL, {**valid_game(), "recheck_at": "not-a-list"}, "recheck_at"),
        (URL, {**valid_game(), "first_seen": "not-a-date"}, "first_seen"),
        (URL, {**valid_game(), "first_seen": "2026-01-01T09:30:00"}, "first_seen"),
        (URL, {**valid_game(), "recheck_at": ["not-a-date"]}, "recheck_at"),
        (URL, {**valid_game(), "recheck_at": ["2026-01-01T09:30:00"]}, "recheck_at"),
        (URL, {**valid_game(), "retry_at": 42}, "retry_at"),
        (URL, {**valid_game(), "retry_at": "2026-01-01T09:30:00"}, "retry_at"),
        (URL, {**valid_game(), "recheck_plan_started": 1}, "recheck_plan_started"),
        (URL, {**valid_game(), "researched_at": 42}, "researched_at"),
        (URL, {**valid_game(), "researched_at": "2026-01-01T09:30:00"}, "researched_at"),
        (URL, {**valid_game(), "last_error": 42}, "last_error"),
        ("   ", valid_game(), "URL"),
    ],
)
def test_load_rejects_invalid_game_records(tmp_path, url, record, field) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"schema_version": 1, "games": {url: record}}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match=field):
        MonitorState.load(path)


def test_load_allows_unknown_game_metadata(tmp_path) -> None:
    path = tmp_path / "state.json"
    record = {**valid_game(), "future_metadata": {"source": "next-version"}}
    path.write_text(
        json.dumps({"schema_version": 1, "games": {URL: record}}), encoding="utf-8"
    )

    assert MonitorState.load(path).games[URL] == record


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"sources": "new_games"}, "sources"),
        ({"sources": ["new_games"]}, "source_first_seen"),
        (
            {
                "source_first_seen": {"new_games": at().isoformat()},
            },
            "source_first_seen",
        ),
        (
            {
                "sources": ["new_games"],
                "source_first_seen": {"new_games": "2026-01-01T00:00:00"},
            },
            "source_first_seen",
        ),
        (
            {
                "sources": ["sitemap"],
                "source_first_seen": {"sitemap": at().isoformat()},
                "new_games_rank": 3,
            },
            "new_games_rank",
        ),
        ({"new_games_rank": True}, "new_games_rank"),
    ],
)
def test_load_strictly_validates_known_discovery_provenance(
    tmp_path, extra, field
) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "games": {URL: {**valid_game(), **extra}}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=field):
        MonitorState.load(path)


@pytest.mark.parametrize(
    ("record", "field"),
    [
        ({**valid_game(), "status": "researched"}, "researched_at"),
        (
            {
                **valid_game(),
                "status": "researched",
                "researched_at": at().isoformat(),
                "retry_at": at(2).isoformat(),
            },
            "retry_at",
        ),
        (
            {
                **valid_game(),
                "status": "researched",
                "researched_at": at().isoformat(),
                "last_error": "stale",
            },
            "last_error",
        ),
        ({**valid_game(), "status": "retry"}, "last_error"),
        (
            {**valid_game(), "status": "retry", "last_error": "temporary"},
            "retry_at",
        ),
        (
            {
                **valid_game(),
                "status": "retry",
                "last_error": "",
                "retry_at": at(2).isoformat(),
            },
            "last_error",
        ),
        (
            {
                **valid_game(),
                "status": "retry",
                "last_error": "temporary",
                "retry_at": at(2).isoformat(),
                "recheck_plan_started": True,
                "recheck_at": [at(8).isoformat()],
            },
            "researched_at",
        ),
        ({**valid_game(), "retry_at": at(2).isoformat()}, "retry_at"),
        ({**valid_game(), "last_error": "stale"}, "last_error"),
        ({**valid_game(), "recheck_at": [at(2).isoformat()]}, "recheck_at"),
        ({**valid_game(), "recheck_plan_started": True}, "recheck_plan_started"),
        ({**valid_game(), "researched_at": at().isoformat()}, "researched_at"),
        (
            {
                **valid_game(),
                "recheck_plan_started": False,
                "recheck_at": [at(2).isoformat()],
            },
            "recheck_at",
        ),
    ],
)
def test_load_rejects_semantically_inconsistent_game_records(tmp_path, record, field) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"schema_version": 1, "games": {URL: record}}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match=field):
        MonitorState.load(path)


@pytest.mark.parametrize(
    "state",
    [
        MonitorState(schema_version=True),
        MonitorState(games={URL: {}}),
        MonitorState(games={URL: {**valid_game(), "status": "retry"}}),
    ],
)
def test_save_rejects_unloadable_public_state(tmp_path, state) -> None:
    path = tmp_path / "state.json"

    with pytest.raises(ValueError):
        state.save(path)

    assert not path.exists()


def test_loaded_pending_retry_restores_pending_after_it_is_due(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = MonitorState()
    state.diff([URL], at(), baseline=False)
    state.mark_error(URL, "temporary", at())
    state.save(path)

    reloaded = MonitorState.load(path)
    assert reloaded.due_rechecks(at(2)) == [URL]
    assert reloaded.games[URL]["status"] == "pending"
    reloaded.save(path)

    assert MonitorState.load(path) == reloaded


def test_loaded_researched_retry_preserves_future_trend_checks_after_due(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = MonitorState()
    state.diff([URL], at(), baseline=False)
    state.mark_research(URL, at(), trends_missing=True)
    state.mark_error(URL, "temporary", at(7))
    state.save(path)

    reloaded = MonitorState.load(path)
    assert reloaded.due_rechecks(at(8)) == [URL]
    assert reloaded.games[URL]["status"] == "researched"
    assert reloaded.games[URL]["recheck_at"] == [at(15).isoformat(), at(31).isoformat()]
    reloaded.save(path)

    assert MonitorState.load(path) == reloaded


def test_save_failure_preserves_existing_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    old_contents = '{"previous": true}\n'
    path.write_text(old_contents, encoding="utf-8")
    state = MonitorState()

    def fail_replace(source, destination) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr("poki_seo_monitor.state.os.replace", fail_replace)

    with pytest.raises(OSError, match="disk failure"):
        state.save(path)

    assert path.read_text(encoding="utf-8") == old_contents
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_write_failure_cleans_up_temp_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"previous": true}\n', encoding="utf-8")

    def fail_dump(*args, **kwargs) -> None:
        raise OSError("write failure")

    monkeypatch.setattr("poki_seo_monitor.state.json.dump", fail_dump)

    with pytest.raises(OSError, match="write failure"):
        MonitorState().save(path)

    assert path.read_text(encoding="utf-8") == '{"previous": true}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_preserves_primary_failure_when_temp_cleanup_also_fails(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    original_unlink = os.unlink

    def fail_dump(*args, **kwargs) -> None:
        raise OSError("write failure")

    def fail_unlink(target) -> None:
        raise OSError("cleanup failure")

    monkeypatch.setattr("poki_seo_monitor.state.json.dump", fail_dump)
    monkeypatch.setattr("poki_seo_monitor.state.os.unlink", fail_unlink)

    with pytest.raises(OSError, match="write failure"):
        MonitorState().save(path)

    temp_files = list(tmp_path.glob("*.tmp"))
    assert len(temp_files) == 1
    for temp_file in temp_files:
        original_unlink(temp_file)


@pytest.mark.skipif(not hasattr(os, "O_DIRECTORY"), reason="requires os.O_DIRECTORY")
def test_save_fsyncs_and_closes_parent_directory(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    original_open = os.open
    original_fsync = os.fsync
    original_close = os.close
    opened_directory_fds: list[int] = []
    fsynced_fds: list[int] = []
    closed_fds: list[int] = []

    def tracking_open(target, flags, *args, **kwargs):
        fd = original_open(target, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            opened_directory_fds.append(fd)
        return fd

    def tracking_fsync(fd) -> None:
        fsynced_fds.append(fd)
        original_fsync(fd)

    def tracking_close(fd) -> None:
        closed_fds.append(fd)
        original_close(fd)

    monkeypatch.setattr("poki_seo_monitor.state.os.open", tracking_open)
    monkeypatch.setattr("poki_seo_monitor.state.os.fsync", tracking_fsync)
    monkeypatch.setattr("poki_seo_monitor.state.os.close", tracking_close)

    MonitorState().save(path)

    assert len(opened_directory_fds) == 1
    directory_fd = opened_directory_fds[0]
    assert directory_fd in fsynced_fds
    assert directory_fd in closed_fds


@pytest.mark.skipif(not hasattr(os, "O_DIRECTORY"), reason="requires os.O_DIRECTORY")
def test_directory_fsync_failure_keeps_replaced_state_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    original_open = os.open
    original_fsync = os.fsync
    original_close = os.close
    directory_fds: list[int] = []
    closed_fds: list[int] = []

    def tracking_open(target, flags, *args, **kwargs):
        fd = original_open(target, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            directory_fds.append(fd)
        return fd

    def fail_directory_fsync(fd) -> None:
        if fd in directory_fds:
            raise OSError("directory fsync failure")
        original_fsync(fd)

    def tracking_close(fd) -> None:
        closed_fds.append(fd)
        original_close(fd)

    monkeypatch.setattr("poki_seo_monitor.state.os.open", tracking_open)
    monkeypatch.setattr("poki_seo_monitor.state.os.fsync", fail_directory_fsync)
    monkeypatch.setattr("poki_seo_monitor.state.os.close", tracking_close)

    with pytest.raises(OSError, match="directory fsync failure"):
        MonitorState().save(path)

    assert MonitorState.load(path) == MonitorState()
    assert directory_fds[0] in closed_fds


@pytest.mark.parametrize(
    "operation",
    [
        lambda state: state.diff([URL], datetime(2026, 1, 1), baseline=False),
        lambda state: state.mark_research(URL, datetime(2026, 1, 1), trends_missing=True),
        lambda state: state.due_rechecks(datetime(2026, 1, 1)),
        lambda state: state.mark_error(URL, "bad", datetime(2026, 1, 1)),
    ],
)
def test_operations_reject_naive_datetimes(operation) -> None:
    state = MonitorState()
    state.diff([URL], at(), baseline=False)

    with pytest.raises(ValueError, match="timezone-aware"):
        operation(state)


def test_mutations_require_known_url() -> None:
    state = MonitorState()

    with pytest.raises(KeyError):
        state.mark_research(URL, at(), trends_missing=True)
    with pytest.raises(KeyError):
        state.mark_error(URL, "bad", at())
