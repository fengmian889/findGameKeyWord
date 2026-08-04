import csv
import errno
import json
from pathlib import Path
import re
import threading

import pytest

from poki_seo_monitor.models import GamePage, KeywordCandidate, Opportunity, SearchSignals
from poki_seo_monitor.reporting import Reporter, ResearchMetadata, github_issue_poster


def _page() -> GamePage:
    return GamePage(
        url="https://poki.com/en/g/goal-heads",
        slug="goal-heads",
        name="Goal Heads",
        title="Goal Heads | Poki",
        description="A football game",
        body="Play football.",
        categories=("Sports",),
        developer="Acme",
        related_games=("Soccer Stars",),
    )


def _opportunity(score: int = 60, trend: float | None = 10) -> Opportunity:
    return Opportunity(
        KeywordCandidate("goal heads tips", "long_tail", ("page",), True),
        SearchSignals(trend_7d=trend, autocomplete=("goal heads controls",)),
        score,
        0.8,
        "watch",
    )


def _metadata(*, recheck_at: tuple[str, ...] = ()) -> ResearchMetadata:
    return ResearchMetadata(
        first_seen="2026-08-03T00:00:00+00:00",
        sources=("new_games", "sitemap"),
        source_first_seen={
            "new_games": "2026-08-03T00:00:00+00:00",
            "sitemap": "2026-08-03T06:00:00+00:00",
        },
        new_games_rank=2,
        recheck_at=recheck_at,
        recheck_status="scheduled" if recheck_at else "not_required",
        errors=("autocomplete: unavailable",),
    )


def _other_page(slug: str) -> GamePage:
    return GamePage(
        f"https://poki.com/en/g/{slug}", slug, slug.title(), "Title", "Description", "Body"
    )


def test_publish_writes_report_structured_files_and_posts_only_once(tmp_path: Path) -> None:
    issues: list[tuple[str, str, str]] = []

    def post(title: str, body: str, marker: str) -> int:
        issues.append((title, body, marker))
        return 9

    reporter = Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv", post)
    first = reporter.publish(_page(), [_opportunity()], "2026-08-03", allow_issue=True)
    second = reporter.publish(_page(), [_opportunity()], "2026-08-03", allow_issue=True)

    assert first.issue_number == 9
    assert second.issue_number is None
    assert len(issues) == 1
    assert first.report_path.exists()
    assert "<!-- poki-seo:" in first.report_path.read_text()
    assert len((tmp_path / "games.jsonl").read_text().splitlines()) == 1
    assert (tmp_path / "keywords.csv").read_text().splitlines()[0] == (
        "game_url,game_name,keyword,group,verified,score,confidence,action,trend_7d,trend_30d,trend_90d"
    )


def test_retry_notification_reconstructs_latest_persisted_report(tmp_path: Path) -> None:
    attempts = []
    reporter = Reporter(
        tmp_path / "reports",
        tmp_path / "games.jsonl",
        tmp_path / "keywords.csv",
        lambda *args: (_ for _ in ()).throw(RuntimeError("temporary")),
    )
    first = reporter.publish(_page(), [_opportunity()], "2026-08-03", True)
    assert first.issue_error
    reporter.issue_post = lambda title, body, marker: attempts.append((title, body, marker)) or 21

    retried = reporter.retry_notification(_page().url)

    assert retried.issue_number == 21
    assert retried.report_path.exists()
    assert len(attempts) == 1
    assert "Goal Heads" in attempts[0][1]


def test_run_notifications_partition_high_and_ordinary_with_per_url_markers(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str]] = []
    reporter = Reporter(
        tmp_path / "reports",
        tmp_path / "games.jsonl",
        tmp_path / "keywords.csv",
        lambda title, body, marker: calls.append((title, body, marker)) or len(calls),
    )
    high = _other_page("high")
    ordinary_a = _other_page("ordinary-a")
    ordinary_b = _other_page("ordinary-b")
    low = _other_page("low")
    for page, score in ((high, 80), (ordinary_a, 55), (ordinary_b, 74), (low, 54)):
        result = reporter.publish(
            page,
            [_opportunity(score)],
            "2026-08-03",
            allow_issue=True,
            defer_issue=True,
            metadata=_metadata(),
        )
        assert result.notification_pending is (score >= 55)

    outcomes = reporter.publish_run_notifications(
        [high.url, ordinary_a.url, ordinary_b.url]
    )

    assert len(calls) == 2
    assert "high priority" in calls[0][0].lower()
    assert "summary" in calls[1][0].lower()
    assert all(f"poki-seo:{_url_marker(url)}" in calls[1][1] for url in (ordinary_a.url, ordinary_b.url))
    assert _url_marker(low.url) not in calls[1][1]
    assert outcomes[high.url].issue_number == 1
    assert outcomes[ordinary_a.url].issue_number == outcomes[ordinary_b.url].issue_number == 2


def test_run_notification_partial_failure_retries_saved_high_artifact_only(
    tmp_path: Path,
) -> None:
    attempts: list[str] = []
    reporter = Reporter(
        tmp_path / "reports",
        tmp_path / "games.jsonl",
        tmp_path / "keywords.csv",
        lambda title, body, marker: (
            (_ for _ in ()).throw(RuntimeError("high unavailable"))
            if "high priority" in title.lower()
            else 22
        ),
    )
    high, ordinary = _other_page("retry-high"), _other_page("retry-ordinary")
    for page, score in ((high, 80), (ordinary, 60)):
        reporter.publish(
            page,
            [_opportunity(score)],
            "2026-08-03",
            True,
            defer_issue=True,
            metadata=_metadata(),
        )

    first = reporter.publish_run_notifications([high.url, ordinary.url])
    reporter.issue_post = lambda title, body, marker: attempts.append(marker) or 23
    second = reporter.publish_run_notifications([high.url])

    assert first[high.url].issue_error and first[ordinary.url].issue_number == 22
    assert second[high.url].issue_number == 23
    assert attempts == [_url_marker(high.url)]
    records = [json.loads(line) for line in (tmp_path / "games.jsonl").read_text().splitlines()]
    events = [record for record in records if record.get("record_type") == "issue_outcome"]
    assert [event["issue"]["status"] for event in events] == ["failed", "created", "created"]


def _url_marker(url: str) -> str:
    import hashlib

    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def test_deferred_issue_without_github_config_is_not_pending(tmp_path: Path) -> None:
    result = Reporter(
        tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv"
    ).publish(
        _page(),
        [_opportunity(80)],
        "2026-08-03",
        True,
        defer_issue=True,
        metadata=_metadata(),
    )

    assert result.notification_pending is False
    record = json.loads((tmp_path / "games.jsonl").read_text())
    assert record["issue"]["status"] == "not_configured"


def test_missing_trends_low_score_is_deferred_into_summary_and_retries_from_v2_artifacts(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fail(title: str, body: str, marker: str) -> int:
        calls.append((title, body, marker))
        raise RuntimeError("temporary")

    reporter = Reporter(
        tmp_path / "reports",
        tmp_path / "games.jsonl",
        tmp_path / "keywords.csv",
        fail,
    )
    result = reporter.publish(
        _page(),
        [_opportunity(40, None)],
        "2026-08-03",
        True,
        defer_issue=True,
        metadata=_metadata(recheck_at=("2026-08-10T00:00:00+00:00",)),
    )

    first = reporter.publish_run_notifications([_page().url])
    reporter.issue_post = lambda title, body, marker: calls.append((title, body, marker)) or 31
    second = reporter.publish_run_notifications([_page().url])

    assert result.notification_pending is True
    assert "summary" in calls[0][0].lower()
    assert f"poki-seo:{_url_marker(_page().url)}" in calls[0][1]
    assert first[_page().url].issue_error
    assert second[_page().url].issue_number == 31
    records = [json.loads(line) for line in (tmp_path / "games.jsonl").read_text().splitlines()]
    research = next(record for record in records if record.get("record_type") == "research")
    assert research["recheck"] == {
        "status": "scheduled",
        "schedule": ["2026-08-10T00:00:00+00:00"],
    }
    assert research["issue"]["status"] == "pending"
    assert research["issue"]["kind"] == "summary"


def test_missing_trends_low_score_without_github_is_not_left_pending(
    tmp_path: Path,
) -> None:
    reporter = Reporter(
        tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv"
    )

    result = reporter.publish(
        _page(),
        [_opportunity(40, None)],
        "2026-08-03",
        True,
        defer_issue=True,
        metadata=_metadata(recheck_at=("2026-08-10T00:00:00+00:00",)),
    )

    assert result.notification_pending is False
    record = json.loads((tmp_path / "games.jsonl").read_text())
    assert record["issue"]["status"] == "not_configured"
    assert record["issue"]["kind"] == "summary"


def test_legacy_retry_notification_skips_mixed_v2_issue_outcome_events(
    tmp_path: Path,
) -> None:
    reporter = Reporter(
        tmp_path / "reports",
        tmp_path / "games.jsonl",
        tmp_path / "keywords.csv",
        lambda *args: (_ for _ in ()).throw(RuntimeError("temporary")),
    )
    reporter.publish(
        _page(),
        [_opportunity(60)],
        "2026-08-03",
        True,
        defer_issue=True,
        metadata=_metadata(),
    )
    reporter.publish_run_notifications([_page().url])
    reporter.issue_post = lambda *args: 44

    result = reporter.retry_notification(_page().url)

    assert result.issue_number == 44


def test_v2_research_record_contains_complete_provenance_and_schedule(tmp_path: Path) -> None:
    result = Reporter(
        tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv"
    ).publish(
        _page(),
        [_opportunity()],
        "2026-08-03",
        False,
        defer_issue=True,
        metadata=_metadata(recheck_at=("2026-08-10T00:00:00+00:00",)),
    )

    record = json.loads((tmp_path / "games.jsonl").read_text())
    assert record["schema_version"] == 2 and record["record_type"] == "research"
    assert record["discovery"] == {
        "sources": ["new_games", "sitemap"],
        "source_first_seen": {
            "new_games": "2026-08-03T00:00:00+00:00",
            "sitemap": "2026-08-03T06:00:00+00:00",
        },
        "new_games_rank": 2,
    }
    assert record["first_seen"] == "2026-08-03T00:00:00+00:00"
    assert record["recheck"] == {
        "status": "scheduled",
        "schedule": ["2026-08-10T00:00:00+00:00"],
    }
    assert record["report"] == {"reference": str(result.report_path)}
    assert record["issue"]["status"] == "baseline"
    assert record["errors"] == ["autocomplete: unavailable"]


def test_retry_notification_rejects_symlink_report_target(tmp_path: Path) -> None:
    reporter = Reporter(
        tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv"
    )
    result = reporter.publish(_page(), [_opportunity()], "2026-08-03", False)
    destination = tmp_path / "outside.md"
    destination.write_text("safe", encoding="utf-8")
    result.report_path.unlink()
    result.report_path.symlink_to(destination)

    with pytest.raises(ValueError, match="symlink"):
        reporter.retry_notification(_page().url)

    assert destination.read_text(encoding="utf-8") == "safe"


def test_retry_notification_rejects_symlink_source_data(tmp_path: Path) -> None:
    games = tmp_path / "games.jsonl"
    reporter = Reporter(tmp_path / "reports", games, tmp_path / "keywords.csv")
    reporter.publish(_page(), [_opportunity()], "2026-08-03", False)
    outside = tmp_path / "outside.jsonl"
    games.replace(outside)
    games.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        reporter.retry_notification(_page().url)


def test_changed_research_appends_immutable_jsonl_record_and_replaces_csv_rows(tmp_path: Path) -> None:
    reporter = Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv")
    other = GamePage("https://poki.com/en/g/other", "other", "Other", "T", "D", "B")
    reporter.publish(other, [_opportunity(1)], "2026-08-03", False)
    reporter.publish(_page(), [_opportunity(10)], "2026-08-03", False)
    reporter.publish(_page(), [_opportunity(20)], "2026-08-03", False)

    records = [json.loads(line) for line in (tmp_path / "games.jsonl").read_text().splitlines()]
    assert [record["page"]["url"] for record in records] == [other.url, _page().url, _page().url]
    with (tmp_path / "keywords.csv").open(newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == [
        "game_url", "game_name", "keyword", "group", "verified", "score", "confidence", "action", "trend_7d", "trend_30d", "trend_90d"
    ]
    assert [row[0] for row in rows[1:]] == [other.url, _page().url]
    assert rows[-1][5] == "20"


def test_markdown_labels_sections_and_escapes_table_text(tmp_path: Path) -> None:
    page = _page()
    opportunity = Opportunity(
        KeywordCandidate("bad|phrase\nnext", "long_tail", ("page",), True),
        SearchSignals(autocomplete=("one|two\nthree",), rising_queries=("up|now",), errors=("bad\nprovider",)),
        10,
        0.2,
        "watch",
    )
    result = Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords").publish(
        page, [opportunity], "2026-08-03", False
    )
    text = result.report_path.read_text()

    assert "## Page facts (extracted)" in text
    assert "## Generated keyword candidates" in text
    assert "## Verified signals and inferences" in text
    assert "bad\\|phrase<br>next" in text
    assert "one\\|two<br>three" in text
    assert "<!-- poki-seo:" in text


@pytest.mark.parametrize("date", ["2026-2-03", "2026-02-30", "2026/02/03", "2026-08-03x"])
def test_publish_rejects_noncanonical_date(tmp_path: Path, date: str) -> None:
    reporter = Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords")
    with pytest.raises(ValueError, match="date"):
        reporter.publish(_page(), [], date, False)


@pytest.mark.parametrize("slug", ["..", ".", "../escape", "Goal-Heads", "a/b"])
def test_publish_rejects_unsafe_slug(tmp_path: Path, slug: str) -> None:
    reporter = Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords")
    with pytest.raises(ValueError, match="slug"):
        reporter.publish(GamePage(_page().url, slug, "N", "T", "D", "B"), [], "2026-08-03", False)


def test_publish_rejects_corrupt_existing_jsonl(tmp_path: Path) -> None:
    games = tmp_path / "games.jsonl"
    games.write_text("not json\n")
    reporter = Reporter(tmp_path / "reports", games, tmp_path / "keywords.csv")
    with pytest.raises(ValueError, match="corrupt JSONL"):
        reporter.publish(_page(), [], "2026-08-03", False)


def _stored_record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": "2026-08-03",
        "page": {
            "url": "https://poki.com/en/g/old",
            "slug": "old",
            "name": "Old",
            "title": "Old title",
            "description": "Old description",
            "body": "Old body",
            "categories": ["Puzzle"],
            "developer": None,
            "related_games": [],
        },
        "opportunities": [
            {
                "keyword": {
                    "phrase": "old game",
                    "group": "game_name",
                    "evidence": ["page"],
                    "verified": False,
                },
                "signals": {
                    "trend_7d": None,
                    "trend_30d": None,
                    "trend_90d": None,
                    "rising_queries": [],
                    "autocomplete": [],
                    "competition": None,
                    "errors": [],
                    "autocomplete_observed": False,
                    "rising_queries_observed": False,
                },
                "score": 5,
                "confidence": 0.2,
                "action": "watch",
            }
        ],
    }


@pytest.mark.parametrize(
    ("record", "field"),
    [
        ({}, "schema_version"),
        ({**_stored_record(), "schema_version": 2}, "schema_version"),
        ({**_stored_record(), "schema_version": True}, "schema_version"),
        ({key: value for key, value in _stored_record().items() if key != "generated_at"}, "generated_at"),
        ({**_stored_record(), "generated_at": "2026-8-03"}, "generated_at"),
        ({**_stored_record(), "page": {}}, "page.url"),
        ({**_stored_record(), "page": {"url": " ", "slug": "old", "name": "Old"}}, "page.url"),
        ({**_stored_record(), "opportunities": {}}, "opportunities"),
        ({**_stored_record(), "opportunities": [{}]}, "opportunities[0].keyword"),
        ({**_stored_record(), "opportunities": [{"keyword": {}, "score": 1, "confidence": 0.1, "action": "watch"}]}, "opportunities[0].keyword.phrase"),
    ],
)
def test_publish_rejects_valid_json_with_invalid_record_schema(
    tmp_path: Path, record: dict[str, object], field: str
) -> None:
    games = tmp_path / "games.jsonl"
    games.write_text(json.dumps(record) + "\n")
    reporter = Reporter(tmp_path / "reports", games, tmp_path / "keywords.csv")

    with pytest.raises(ValueError, match=rf"line 1.*{re.escape(field)}"):
        reporter.publish(_page(), [], "2026-08-03", False)


def test_publish_rejects_corrupt_csv_header(tmp_path: Path) -> None:
    keywords = tmp_path / "keywords.csv"
    keywords.write_text("wrong,header\n")
    reporter = Reporter(tmp_path / "reports", tmp_path / "games.jsonl", keywords)
    with pytest.raises(ValueError, match="header"):
        reporter.publish(_page(), [], "2026-08-03", False)


def test_csv_neutralizes_formula_cells(tmp_path: Path) -> None:
    page = GamePage("https://poki.com/en/g/x", "x", "=Name", "T", "D", "B")
    opportunity = Opportunity(KeywordCandidate("=SUM(A1)", "long_tail", ("page",)), SearchSignals(), 1, 0.1, "watch")
    path = tmp_path / "keywords.csv"
    Reporter(tmp_path / "reports", tmp_path / "games", path).publish(page, [opportunity], "2026-08-03", False)
    with path.open(newline="") as handle:
        row = list(csv.reader(handle))[1]
    assert row[1] == "'=Name"
    assert row[2] == "'=SUM(A1)"
    assert row[7] == "watch"


@pytest.mark.parametrize("phrase", ["\t=SUM(A1)", "\r+SUM(A1)", "  -SUM(A1)", "\n@SUM(A1)"])
def test_csv_neutralizes_formula_after_leading_whitespace_and_controls(tmp_path: Path, phrase: str) -> None:
    path = tmp_path / "keywords.csv"
    opportunity = Opportunity(KeywordCandidate(phrase, "long_tail", ("page",)), SearchSignals(), 1, 0.1, "watch")
    Reporter(tmp_path / "reports", tmp_path / "games", path).publish(_page(), [opportunity], "2026-08-03", False)
    with path.open(newline="") as handle:
        row = list(csv.reader(handle))[1]
    assert row[2] == "'" + phrase


def test_csv_leaves_ordinary_whitespace_and_apostrophe_prefixed_text_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "keywords.csv"
    opportunities = [
        Opportunity(KeywordCandidate("  plain text", "long_tail", ("page",)), SearchSignals(), 1, 0.1, "watch"),
        Opportunity(KeywordCandidate("'=already safe", "long_tail", ("page",)), SearchSignals(), 1, 0.1, "watch"),
    ]
    Reporter(tmp_path / "reports", tmp_path / "games", path).publish(_page(), opportunities, "2026-08-03", False)
    with path.open(newline="") as handle:
        rows = list(csv.reader(handle))[1:]
    assert [row[2] for row in rows] == ["  plain text", "'=already safe"]


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda record: record["page"].pop("title"), "page.title"),  # type: ignore[index]
        (lambda record: record["page"].update({"description": 2}), "page.description"),  # type: ignore[index]
        (lambda record: record["page"].update({"categories": ["ok", 2]}), "page.categories"),  # type: ignore[index]
        (lambda record: record["page"].update({"developer": 2}), "page.developer"),  # type: ignore[index]
        (lambda record: record["opportunities"][0]["keyword"].pop("group"), "keyword.group"),  # type: ignore[index]
        (lambda record: record["opportunities"][0]["keyword"].update({"group": "bad"}), "keyword.group"),  # type: ignore[index]
        (lambda record: record["opportunities"][0]["keyword"].pop("evidence"), "keyword.evidence"),  # type: ignore[index]
        (lambda record: record["opportunities"][0]["keyword"].update({"verified": 1}), "keyword.verified"),  # type: ignore[index]
        (lambda record: record["opportunities"][0].pop("signals"), "signals"),  # type: ignore[index]
        (lambda record: record["opportunities"][0].update({"signals": {}}), "signals.trend_7d"),  # type: ignore[index]
        (lambda record: record["opportunities"][0]["signals"].update({"trend_7d": True}), "signals.trend_7d"),  # type: ignore[index]
        (lambda record: record["opportunities"][0]["signals"].update({"trend_30d": 101}), "signals.trend_30d"),  # type: ignore[index]
        (lambda record: record["opportunities"][0]["signals"].update({"competition": float("nan")}), "signals.competition"),  # type: ignore[index]
        (lambda record: record["opportunities"][0]["signals"].update({"autocomplete": [1]}), "signals.autocomplete"),  # type: ignore[index]
        (lambda record: record["opportunities"][0]["signals"].update({"autocomplete_observed": 0}), "signals.autocomplete_observed"),  # type: ignore[index]
        (lambda record: record["opportunities"][0].update({"score": 101}), "score"),  # type: ignore[index]
        (lambda record: record["opportunities"][0].update({"confidence": float("nan")}), "confidence"),  # type: ignore[index]
        (lambda record: record["opportunities"][0].update({"confidence": 1.1}), "confidence"),  # type: ignore[index]
        (lambda record: record["opportunities"][0].update({"action": "publish"}), "action"),  # type: ignore[index]
    ],
)
def test_publish_rejects_invalid_emitted_record_fields(tmp_path: Path, mutate: object, field: str) -> None:
    record = _stored_record()
    mutate(record)  # type: ignore[operator]
    games = tmp_path / "games.jsonl"
    games.write_text(json.dumps(record) + "\n")

    with pytest.raises(ValueError, match=re.escape(field)):
        Reporter(tmp_path / "reports", games, tmp_path / "keywords.csv").publish(
            _page(), [], "2026-08-03", False
        )


@pytest.mark.parametrize(
    "url",
    ["=SUM(A1)", "https://www.poki.com/en/g/goal-heads", "https://poki.com/en/g/goal-heads/", "https://example.com/en/g/goal-heads"],
)
def test_publish_requires_already_canonical_poki_game_url(tmp_path: Path, url: str) -> None:
    page = GamePage(url, "goal-heads", "Goal Heads", "Title", "Description", "Body")
    with pytest.raises(ValueError, match="canonical Poki game URL"):
        Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords").publish(
            page, [], "2026-08-03", False
        )


def test_jsonl_uses_ascii_and_literal_newlines_for_unicode_line_separators(tmp_path: Path) -> None:
    page = GamePage(
        _page().url, _page().slug, "Goal\u0085Heads", "Title", "Description\u2028with\u2029separators", "Body"
    )
    opportunity = Opportunity(
        KeywordCandidate("phrase\u0085with\u2028separators", "long_tail", ("page",)), SearchSignals(), 1, 0.1, "watch"
    )
    reporter = Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv")
    reporter.publish(page, [opportunity], "2026-08-03", False)
    reporter.publish(page, [opportunity], "2026-08-03", False)

    stored = (tmp_path / "games.jsonl").read_text()
    assert "\\u0085" in stored and "\\u2028" in stored and "\\u2029" in stored
    assert len(stored.split("\n")) == 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_invalid_current_nan_or_infinite_record_is_rejected_before_writes(tmp_path: Path, value: float) -> None:
    report = tmp_path / "reports" / "2026-08-03" / "goal-heads.md"
    report.parent.mkdir(parents=True)
    report.write_text("old report")
    bad = Opportunity(KeywordCandidate("bad", "long_tail", ("page",)), SearchSignals(trend_7d=value), 1, 0.1, "watch")
    reporter = Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv")

    with pytest.raises(ValueError, match="trend_7d"):
        reporter.publish(_page(), [bad], "2026-08-03", False)
    assert report.read_text() == "old report"
    reporter.publish(_page(), [_opportunity(1)], "2026-08-03", False)


@pytest.mark.parametrize("bad_path", ["games.jsonl", "keywords.csv"])
def test_prevalidation_failure_leaves_existing_report_untouched(tmp_path: Path, bad_path: str) -> None:
    report = tmp_path / "reports" / "2026-08-03" / "goal-heads.md"
    report.parent.mkdir(parents=True)
    report.write_text("old report")
    bad = tmp_path / bad_path
    bad.write_text("not json\n" if bad_path.endswith("jsonl") else "wrong,header\n")

    with pytest.raises(ValueError):
        Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv").publish(
            _page(), [_opportunity(1)], "2026-08-03", False
        )
    assert report.read_text() == "old report"


@pytest.mark.parametrize("fail_at", [2, 3])
def test_failed_batch_replace_rolls_back_every_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int
) -> None:
    import poki_seo_monitor.reporting as reporting

    reporter = Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv")
    reporter.publish(_page(), [_opportunity(1)], "2026-08-03", False)
    paths = [tmp_path / "reports" / "2026-08-03" / "goal-heads.md", tmp_path / "games.jsonl", tmp_path / "keywords.csv"]
    before = {path: path.read_bytes() for path in paths}
    real_replace = reporting.os.replace
    calls = 0

    def fail_once(source: str | Path, destination: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(reporting.os, "replace", fail_once)
    with pytest.raises(OSError, match="replace failed"):
        reporter.publish(_page(), [_opportunity(2)], "2026-08-03", False)
    assert {path: path.read_bytes() for path in paths} == before


def test_directory_fsync_propagates_real_io_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import poki_seo_monitor.reporting as reporting

    monkeypatch.setattr(reporting.os, "fsync", lambda _: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
    with pytest.raises(OSError) as error:
        reporting._fsync_directory(tmp_path)
    assert error.value.errno == errno.EIO


def test_staging_fsync_failure_keeps_destination_and_cleans_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import poki_seo_monitor.reporting as reporting

    report = tmp_path / "reports" / "2026-08-03" / "goal-heads.md"
    report.parent.mkdir(parents=True)
    report.write_text("old report")
    before = set(report.parent.iterdir())
    monkeypatch.setattr(reporting.os, "fsync", lambda _: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
    with pytest.raises(OSError):
        Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords").publish(
            _page(), [], "2026-08-03", False
        )
    assert report.read_text() == "old report"
    assert set(report.parent.iterdir()) == before


def test_markdown_escapes_html_in_scraped_and_generated_text(tmp_path: Path) -> None:
    page = GamePage(_page().url, "goal-heads", "<script>x</script>", "<b>title</b>", "a & b", "Body")
    opportunity = Opportunity(KeywordCandidate("<img src=x>", "long_tail", ("page",)), SearchSignals(), 1, 0.1, "watch")
    report = Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords").publish(
        page, [opportunity], "2026-08-03", False
    ).report_path.read_text()
    assert "&lt;script&gt;x&lt;/script&gt;" in report
    assert "&lt;img src=x&gt;" in report
    assert "a &amp; b" in report
    assert "<script>" not in report


def test_existing_csv_rows_are_resanitized_before_preserving(tmp_path: Path) -> None:
    path = tmp_path / "keywords.csv"
    path.write_text(
        "game_url,game_name,keyword,group,verified,score,confidence,action,trend_7d,trend_30d,trend_90d\n"
        "https://poki.com/en/g/other,=danger,=formula,long_tail,False,1,0.1,watch,,,\n"
    )
    Reporter(tmp_path / "reports", tmp_path / "games", path).publish(_page(), [_opportunity(1)], "2026-08-03", False)
    with path.open(newline="") as handle:
        existing = list(csv.reader(handle))[1]
    assert existing[1:3] == ["'=danger", "'=formula"]


def test_issue_error_redacts_credentials_and_controls() -> None:
    import poki_seo_monitor.reporting as reporting

    error = RuntimeError("Bearer abc123 token=xyz password: nope https://user:pass@example.com\nfailed")
    message = reporting._issue_error(error)
    assert "abc123" not in message and "xyz" not in message and "nope" not in message and "user:pass" not in message
    assert "\n" not in message


@pytest.mark.parametrize(
    ("title", "body", "marker"),
    [("", "body", "a" * 16), ("x" * 257, "body", "a" * 16), ("title", "x" * 59_001, "a" * 16), ("title", "body", "ABCDEF0123456789")],
)
def test_github_poster_validates_callable_inputs_before_network(title: str, body: str, marker: str) -> None:
    session = _Session(_Response({"items": []}), _Response({"number": 1}))
    with pytest.raises(ValueError):
        github_issue_poster("boundary/repo", "secret", session)(title, body, marker)
    assert session.calls == []


def test_github_poster_caches_success_across_poster_instances() -> None:
    first = _Session(_Response({"items": []}), _Response({"number": 31}))
    second = _Session(_Response({"items": []}), _Response({"number": 32}))
    marker = "d" * 16
    assert github_issue_poster("cache/repo", "secret", first)("Title", "Body", marker) == 31
    assert github_issue_poster("cache/repo", "secret", second)("Title", "Body", marker) == 31
    assert len(first.calls) == 2
    assert second.calls == []


class _QueuedSession:
    def __init__(self, gets: list[object], post_result: object) -> None:
        self.gets, self.post_result = gets, post_result
        self.calls: list[str] = []

    def get(self, _: str, **__: object) -> object:
        self.calls.append("get")
        result = self.gets.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]

    def post(self, _: str, **__: object) -> object:
        self.calls.append("post")
        if isinstance(self.post_result, Exception):
            raise self.post_result
        return self.post_result  # type: ignore[return-value]


def test_github_poster_researches_after_lost_post_response() -> None:
    session = _QueuedSession([_Response({"items": []}), _Response({"items": [{"number": 44}]})], TimeoutError("lost"))
    assert github_issue_poster("recovery/repo", "secret", session)("Title", "Body", "e" * 16) == 44
    assert session.calls == ["get", "post", "get"]


class _RateResponse:
    status_code = 429
    headers = {"Retry-After": "30"}

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        raise RuntimeError("rate limited")

    def json(self) -> object:
        return self.payload


def test_github_poster_rate_limit_cooldown_avoids_followup_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import poki_seo_monitor.reporting as reporting

    now = 1_000.0
    monkeypatch.setattr(reporting, "_github_now", lambda: now)
    session = _Session(_RateResponse({"message": "rate"}))
    post = github_issue_poster("limit/repo", "secret", session)
    with pytest.raises(reporting.GitHubRateLimitError):
        post("Title", "Body", "f" * 16)
    with pytest.raises(reporting.GitHubRateLimitError):
        post("Title", "Body", "f" * 16)
    assert len(session.calls) == 1


def test_publish_rejects_symlink_destination_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "outside.jsonl"
    target.write_text("outside")
    games = tmp_path / "games.jsonl"
    games.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        Reporter(tmp_path / "reports", games, tmp_path / "keywords.csv").publish(
            _page(), [], "2026-08-03", False
        )
    assert target.read_text() == "outside"


def test_publish_recovers_prepared_journal_before_next_transaction(tmp_path: Path) -> None:
    import poki_seo_monitor.reporting as reporting

    reporter = Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv")
    reporter.publish(_page(), [_opportunity(1)], "2026-08-03", False)
    paths = tuple(
        sorted(
            [tmp_path / "reports" / "2026-08-03" / "goal-heads.md", tmp_path / "games.jsonl", tmp_path / "keywords.csv"],
            key=str,
        )
    )
    old = {path: path.read_bytes() for path in paths}
    transaction = "crash"
    entries = []
    for index, path in enumerate(paths):
        backup = path.parent / f".poki-reporting-{transaction}-backup-{index}"
        stage = path.parent / f".poki-reporting-{transaction}-stage-{index}"
        backup.write_bytes(old[path])
        stage.write_bytes(b"new")
        entries.append({"target": str(path), "stage": str(stage), "backup": str(backup), "existed": True})
    paths[0].write_text("partially replaced")
    journal = tmp_path / ".poki-reporting.journal"
    reporting._write_journal(journal, {"version": 1, "transaction_id": transaction, "state": "prepared", "targets": entries})

    reporter.publish(_page(), [_opportunity(1)], "2026-08-03", False)
    assert {path: path.read_bytes() for path in paths} == old
    assert not journal.exists()


def test_foreign_journal_stops_without_touching_targets(tmp_path: Path) -> None:
    import poki_seo_monitor.reporting as reporting

    report = tmp_path / "reports" / "2026-08-03" / "goal-heads.md"
    report.parent.mkdir(parents=True)
    report.write_text("old")
    journal = tmp_path / ".poki-reporting.journal"
    reporting._write_journal(
        journal,
        {
            "version": 1,
            "transaction_id": "foreign",
            "state": "prepared",
            "targets": [
                {"target": str(tmp_path / f"foreign-{index}"), "stage": str(tmp_path / f".poki-reporting-foreign-stage-{index}"), "backup": None, "existed": False}
                for index in range(3)
            ],
        },
    )
    with pytest.raises(RuntimeError, match="journal"):
        Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv").publish(
            _page(), [], "2026-08-03", False
        )
    assert report.read_text() == "old"


class _PermissionResponse:
    status_code = 403
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        raise RuntimeError("forbidden")

    def json(self) -> object:
        return {"message": "forbidden"}


def test_ordinary_github_403_is_not_rate_limited() -> None:
    session = _Session(_PermissionResponse())
    post = github_issue_poster("permission/repo", "secret", session)
    with pytest.raises(RuntimeError, match="forbidden"):
        post("Title", "Body", "1" * 16)
    with pytest.raises(RuntimeError, match="forbidden"):
        post("Title", "Body", "1" * 16)
    assert len(session.calls) == 2


def test_github_issue_cache_is_bounded_lru(monkeypatch: pytest.MonkeyPatch) -> None:
    import poki_seo_monitor.reporting as reporting
    from collections import OrderedDict

    monkeypatch.setattr(reporting, "_GITHUB_ISSUE_CACHE", OrderedDict())
    for index in range(2_049):
        reporting._github_cache_put(("lru/repo", f"{index:016x}"), index + 1)
    assert len(reporting._GITHUB_ISSUE_CACHE) == 2_048
    assert ("lru/repo", f"{0:016x}") not in reporting._GITHUB_ISSUE_CACHE


def test_two_reporters_serialize_shared_jsonl_and_csv(tmp_path: Path) -> None:
    reports, games, keywords = tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv"
    other = GamePage("https://poki.com/en/g/other", "other", "Other", "Title", "Description", "Body")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def publish(page: GamePage) -> None:
        try:
            barrier.wait()
            Reporter(reports, games, keywords).publish(page, [_opportunity(1)], "2026-08-03", False)
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=publish, args=(_page(),))
    second = threading.Thread(target=publish, args=(other,))
    first.start()
    second.start()
    first.join()
    second.join()

    assert errors == []
    assert len(games.read_text().splitlines()) == 2
    with keywords.open(newline="") as handle:
        urls = [row[0] for row in list(csv.reader(handle))[1:]]
    assert set(urls) == {_page().url, other.url}


def test_cross_game_publish_recovers_prior_game_journal(tmp_path: Path) -> None:
    import poki_seo_monitor.reporting as reporting

    reports, games, keywords = tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv"
    first = Reporter(reports, games, keywords)
    first.publish(_page(), [_opportunity(1)], "2026-08-03", False)
    report_a = reports / "2026-08-03" / "goal-heads.md"
    old_report = report_a.read_bytes()
    paths = tuple(sorted((report_a, games, keywords), key=str))
    entries, transaction = [], "crossgame"
    for index, path in enumerate(paths):
        backup = path.parent / f".poki-reporting-{transaction}-backup-{index}"
        stage = path.parent / f".poki-reporting-{transaction}-stage-{index}"
        backup.write_bytes(path.read_bytes())
        stage.write_bytes(b"new")
        entries.append({"target": str(path), "stage": str(stage), "backup": str(backup), "existed": True})
    report_a.write_text("partial")
    reporting._write_journal(
        tmp_path / ".poki-reporting.journal",
        {"version": 1, "transaction_id": transaction, "state": "prepared", "targets": entries},
    )
    other = GamePage("https://poki.com/en/g/other", "other", "Other", "Title", "Description", "Body")
    Reporter(reports, games, keywords).publish(other, [_opportunity(1)], "2026-08-03", False)
    assert report_a.read_bytes() == old_report
    assert len(games.read_text().splitlines()) == 2


def test_journal_recovery_fsyncs_actual_report_parent_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import poki_seo_monitor.reporting as reporting

    reports, games, keywords = tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv"
    Reporter(reports, games, keywords).publish(_page(), [_opportunity(1)], "2026-08-03", False)
    report = reports / "2026-08-03" / "goal-heads.md"
    paths = tuple(sorted((report, games, keywords), key=str))
    entries, transaction = [], "fsyncdirs"
    for index, path in enumerate(paths):
        backup = path.parent / f".poki-reporting-{transaction}-backup-{index}"
        stage = path.parent / f".poki-reporting-{transaction}-stage-{index}"
        backup.write_bytes(path.read_bytes())
        stage.write_bytes(b"new")
        entries.append({"target": str(path), "stage": str(stage), "backup": str(backup), "existed": True})
    journal = tmp_path / ".poki-reporting.journal"
    reporting._write_journal(journal, {"version": 1, "transaction_id": transaction, "state": "prepared", "targets": entries})
    seen: list[Path] = []
    monkeypatch.setattr(reporting, "_fsync_directory", lambda path: seen.append(Path(path)))

    reporting._recover_journal(journal, games.resolve(), keywords.resolve(), reports.resolve())
    assert report.parent.resolve() in seen
    assert games.parent.resolve() in seen
    assert keywords.parent.resolve() in seen


def test_marker_lru_is_thread_safe_and_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import poki_seo_monitor.reporting as reporting
    from collections import OrderedDict

    monkeypatch.setattr(reporting, "_MARKER_POSTED", OrderedDict())
    monkeypatch.setattr(reporting, "_MARKER_CACHE_LIMIT", 4)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def remember(prefix: str) -> None:
        try:
            barrier.wait()
            for index in range(32):
                key = (tmp_path, f"{prefix}{index:015x}")
                reporting._remember_marker(key)
                reporting._marker_was_posted(key)
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=remember, args=("a",))
    second = threading.Thread(target=remember, args=("b",))
    first.start()
    second.start()
    first.join()
    second.join()
    assert errors == []
    assert len(reporting._MARKER_POSTED) <= 4


def test_blocked_issue_does_not_block_unrelated_local_publish(tmp_path: Path) -> None:
    started, release, complete = threading.Event(), threading.Event(), threading.Event()

    def blocked(*_: str) -> int:
        started.set()
        release.wait(timeout=2)
        return 1

    first = Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv", blocked)
    thread = threading.Thread(target=lambda: first.publish(_page(), [_opportunity(55)], "2026-08-03", True))
    thread.start()
    assert started.wait(timeout=1)
    other = GamePage("https://poki.com/en/g/other", "other", "Other", "Title", "Description", "Body")

    def second_publish() -> None:
        Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv").publish(other, [_opportunity(1)], "2026-08-03", False)
        complete.set()

    second = threading.Thread(target=second_publish)
    second.start()
    assert complete.wait(timeout=1)
    release.set()
    thread.join(timeout=2)
    second.join(timeout=2)


@pytest.mark.parametrize(
    ("opportunities", "called"),
    [([], False), ([_opportunity(54, 2)], False), ([_opportunity(55, 2)], True), ([_opportunity(1, None)], True)],
)
def test_issue_decision_uses_score_or_missing_trends(tmp_path: Path, opportunities: list[Opportunity], called: bool) -> None:
    posts: list[str] = []
    reporter = Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords", lambda *_: posts.append("yes") or 4)
    result = reporter.publish(_page(), opportunities, "2026-08-03", True)

    assert bool(posts) is called
    assert result.issue_number == (4 if called else None)


def test_issue_allow_false_and_failure_retries(tmp_path: Path) -> None:
    calls = 0

    def post(*_: str) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("\nfailed\x00")
        return 7

    reporter = Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords", post)
    assert reporter.publish(_page(), [_opportunity()], "2026-08-03", False).issue_number is None
    failed = reporter.publish(_page(), [_opportunity()], "2026-08-03", True)
    retried = reporter.publish(_page(), [_opportunity()], "2026-08-03", True)

    assert failed.issue_error == "RuntimeError: failed"
    assert retried.issue_number == 7
    assert calls == 2


def test_invalid_issue_number_is_reported_as_error(tmp_path: Path) -> None:
    reporter = Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords", lambda *_: 0)
    result = reporter.publish(_page(), [_opportunity()], "2026-08-03", True)
    assert result.issue_number is None
    assert result.issue_error == "ValueError: issue poster returned an invalid issue number"


def test_atomic_report_failure_keeps_destination_and_removes_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import poki_seo_monitor.reporting as reporting

    report_dir = tmp_path / "reports" / "2026-08-03"
    report_dir.mkdir(parents=True)
    destination = report_dir / "goal-heads.md"
    destination.write_text("old report")
    before = set(report_dir.iterdir())
    monkeypatch.setattr(reporting.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk failed")))

    reporter = Reporter(tmp_path / "reports", tmp_path / "games", tmp_path / "keywords")
    with pytest.raises(OSError, match="disk failed"):
        reporter.publish(_page(), [], "2026-08-03", False)

    assert destination.read_text() == "old report"
    assert set(report_dir.iterdir()) == before


def test_issue_body_is_capped_without_losing_marker_or_facts(tmp_path: Path) -> None:
    captured: list[str] = []
    phrase = "x" * 1_000
    opportunities = [
        Opportunity(KeywordCandidate(f"{phrase}{index}", "long_tail", ("page",)), SearchSignals(), 55, 0.1, "watch")
        for index in range(100)
    ]
    reporter = Reporter(
        tmp_path / "reports", tmp_path / "games", tmp_path / "keywords", lambda _, body, __: captured.append(body) or 1
    )
    reporter.publish(_page(), opportunities, "2026-08-03", True)

    assert len(captured[0]) < 60_000
    assert "<!-- poki-seo:" in captured[0]
    assert "## Page facts (extracted)" in captured[0]


class _Response:
    def __init__(self, payload: object, error: Exception | None = None) -> None:
        self.payload, self.error = payload, error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self) -> object:
        return self.payload


class _Session:
    def __init__(self, search: _Response, created: _Response | None = None) -> None:
        self.search, self.created = search, created
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("get", url, kwargs))
        return self.search

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("post", url, kwargs))
        assert self.created is not None
        return self.created


def test_github_poster_reuses_existing_issue_with_exact_request_policy() -> None:
    session = _Session(_Response({"items": [{"number": 12}]}))
    number = github_issue_poster("owner/repo", "secret", session)("T", "B", "a" * 16)

    assert number == 12
    assert len(session.calls) == 1
    _, url, kwargs = session.calls[0]
    assert url == "https://api.github.com/search/issues"
    assert kwargs["params"] == {"q": 'repo:owner/repo is:issue "poki-seo:aaaaaaaaaaaaaaaa"'}
    assert kwargs["headers"] == {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer secret",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    assert kwargs["timeout"] == (10, 30)
    assert kwargs["allow_redirects"] is False


def test_github_poster_creates_and_validates_payloads() -> None:
    session = _Session(_Response({"items": []}), _Response({"number": 13}))
    assert github_issue_poster("owner/repo", "secret", session)("Title", "Body", "b" * 16) == 13
    assert session.calls[1] == (
        "post",
        "https://api.github.com/repos/owner/repo/issues",
        {
            "json": {"title": "Title", "body": "Body"},
            "headers": {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer secret",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            "timeout": (10, 30),
            "allow_redirects": False,
        },
    )
    malformed = github_issue_poster("owner/repo", "secret", _Session(_Response({"items": "bad"})))
    with pytest.raises(ValueError, match="malformed"):
        malformed("T", "B", "c" * 16)


@pytest.mark.parametrize("repository", ["", "owner", "owner/repo/extra", "owner /repo", "../repo"])
def test_github_poster_validates_repository_and_token(repository: str) -> None:
    with pytest.raises(ValueError):
        github_issue_poster(repository, "token", _Session(_Response({"items": []})))
    with pytest.raises(ValueError):
        github_issue_poster("owner/repo", "  ", _Session(_Response({"items": []})))
