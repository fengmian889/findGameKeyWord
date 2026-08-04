from pathlib import Path
from datetime import datetime
import json

import pytest

from poki_seo_monitor.app import ResearchResult
from poki_seo_monitor.config import Config
from poki_seo_monitor.models import KeywordCandidate, Opportunity, SearchSignals
from poki_seo_monitor.reporting import PublishResult
from poki_seo_monitor.state import MonitorState
from poki_seo_monitor import runtime


URL = "https://poki.com/en/g/alpha"
OTHER = "https://poki.com/en/g/beta"
INDEX = b'''<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://poki.com/en/sitemaps/a.xml</loc></sitemap><sitemap><loc>https://poki.com/en/sitemaps/b.xml</loc></sitemap></sitemapindex>'''
URLSET = b'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://poki.com/en/g/alpha</loc></url></urlset>'''
NEW = b'<html><a href="/en/g/beta">Beta</a><a href="/en/g/alpha">Alpha</a></html>'
PAGE = b'<html><head><title>Alpha</title></head><body><main><h1>Alpha</h1><p>A puzzle game.</p></main></body></html>'


def config(tmp_path: Path, **changes) -> Config:
    values = dict(
        sitemap_index="https://poki.com/en/sitemaps/index.xml",
        new_games_url="https://poki.com/en/new",
        geo="US",
        state_path=tmp_path / "state.json",
        reports_dir=tmp_path / "reports",
        games_path=tmp_path / "games.jsonl",
        keywords_path=tmp_path / "keywords.csv",
        max_games_per_run=10,
        baseline_sample_size=2,
        github_repository=None,
        github_token=None,
    )
    values.update(changes)
    return Config(**values)


class ReporterFake:
    def __init__(self, *args):
        self.issue_post = args[-1]
        self.published = []
        self.retried = []

    def publish(self, page, opportunities, date, allow_issue, **kwargs):
        self.published.append((page, opportunities, date, allow_issue))
        return PublishResult(Path("/tmp/report.md"), None, None)

    def publish_run_notifications(self, urls):
        return {
            url: PublishResult(Path("/tmp/report.md"), None, None) for url in urls
        }

    def retry_notification(self, url):
        self.retried.append(url)
        return PublishResult(Path("/tmp/report.md"), 8, None)


def install(monkeypatch, fetch):
    reporter = ReporterFake(None)
    monkeypatch.setattr(runtime, "build_session", lambda: object())
    monkeypatch.setattr(runtime, "get_bytes", fetch)
    monkeypatch.setattr(runtime, "Reporter", lambda *args: reporter)
    return reporter


def test_discovery_merges_both_sources_and_uses_exact_budgets(tmp_path, monkeypatch) -> None:
    calls = []

    def fetch(session, url, max_bytes):
        calls.append((url, max_bytes))
        if url.endswith("index.xml"):
            return INDEX
        if url.endswith("a.xml") or url.endswith("b.xml"):
            return URLSET
        return NEW

    install(monkeypatch, fetch)
    result = runtime.build_monitor(config(tmp_path)).discover()

    assert [game.url for game in result.games] == [OTHER, URL]
    assert result.games[1].sources == ("new_games", "sitemap")
    assert calls == [
        ("https://poki.com/en/sitemaps/index.xml", runtime.SITEMAP_MAX_BYTES),
        ("https://poki.com/en/sitemaps/a.xml", runtime.SITEMAP_MAX_BYTES),
        ("https://poki.com/en/sitemaps/b.xml", runtime.SITEMAP_MAX_BYTES),
        ("https://poki.com/en/new", runtime.NEW_GAMES_MAX_BYTES),
    ]


def test_runtime_enables_pacing_for_shared_session_signal_fetches(
    tmp_path, monkeypatch
) -> None:
    options = []

    class Provider:
        def __init__(self, *args, **kwargs):
            options.append(kwargs)

    monkeypatch.setattr(runtime, "build_session", lambda: object())
    monkeypatch.setattr(runtime, "AutocompleteProvider", Provider)
    monkeypatch.setattr(runtime, "SerpCompetitionProvider", Provider)
    monkeypatch.setattr(runtime, "GoogleTrendsProvider", Provider)
    monkeypatch.setattr(runtime, "Reporter", ReporterFake)

    runtime.build_monitor(config(tmp_path))

    assert options[0]["pace_custom"] is True
    assert options[2]["pace_custom"] is True


def test_discovery_retains_partial_sitemap_and_degrades(tmp_path, monkeypatch) -> None:
    def fetch(session, url, max_bytes):
        if url.endswith("index.xml"):
            return INDEX
        if url.endswith("a.xml"):
            return URLSET
        if url.endswith("b.xml"):
            raise OSError("child down")
        raise OSError("new down")

    install(monkeypatch, fetch)
    result = runtime.build_monitor(config(tmp_path)).discover()

    assert [game.url for game in result.games] == [URL]
    assert result.degraded is True
    assert [error.split(":", 1)[0] for error in result.errors] == ["sitemap child 2", "new_games"]


def test_discovery_both_sources_fail(tmp_path, monkeypatch) -> None:
    install(monkeypatch, lambda *args, **kwargs: (_ for _ in ()).throw(OSError("down")))

    with pytest.raises(RuntimeError, match="discovery failed"):
        runtime.build_monitor(config(tmp_path)).discover()


def test_research_enforces_provider_budgets_and_verification(tmp_path, monkeypatch) -> None:
    calls = []
    reporter = install(monkeypatch, lambda session, url, max_bytes: PAGE)
    candidates = [KeywordCandidate(f"alpha {index}", "long_tail", ("page",)) for index in range(12)]
    monkeypatch.setattr(runtime, "generate_keywords", lambda page: candidates)

    def signals(keyword, geo, trends, autocomplete, include_trends, serp, include_serp):
        calls.append((keyword, include_trends, include_serp))
        return SearchSignals(
            trend_7d=1 if include_trends else None,
            autocomplete=(f"play {keyword}",) if keyword == "alpha 0" else (),
        )

    monkeypatch.setattr(runtime, "collect_signals", signals)
    monitor = runtime.build_monitor(config(tmp_path))

    result = monitor.research(URL, True)

    assert len(calls) == 10
    assert sum(call[1] for call in calls) == 3
    assert sum(call[2] for call in calls) == 1
    assert reporter.published[0][1][0].keyword.verified is True
    assert reporter.published[0][1][1].keyword.verified is False
    assert result.trends_missing is False


def test_monitor_passes_evidence_freshness_to_every_score_on_rechecks(
    tmp_path, monkeypatch
) -> None:
    seen_freshness = []
    reporter = install(
        monkeypatch,
        lambda session, url, max_bytes: (
            INDEX if url.endswith("index.xml") else
            URLSET if url.endswith(".xml") else
            NEW if url.endswith("/new") else
            PAGE
        ),
    )
    monkeypatch.setattr(
        runtime,
        "generate_keywords",
        lambda page: [KeywordCandidate("alpha", "game_name", ("page",))],
    )
    monkeypatch.setattr(
        runtime,
        "collect_signals",
        lambda *args, **kwargs: SearchSignals(),
    )
    real_score = runtime.score_opportunity

    def score(keyword, signals, freshness, expansion):
        seen_freshness.append(freshness)
        return real_score(keyword, signals, freshness, expansion)

    monkeypatch.setattr(runtime, "score_opportunity", score)
    monitor = runtime.build_monitor(config(tmp_path, baseline_sample_size=1))

    monitor.run(datetime(2026, 8, 3, tzinfo=runtime.UTC))
    monitor.run(datetime(2026, 8, 10, tzinfo=runtime.UTC))

    assert len(reporter.published) == 2
    assert 0.0 < seen_freshness[1] < seen_freshness[0] < 1.0


def test_trends_missing_requires_all_attempted_top_three_to_lack_trend_7d(
    tmp_path, monkeypatch
) -> None:
    install(monkeypatch, lambda session, url, max_bytes: PAGE)
    monkeypatch.setattr(
        runtime,
        "generate_keywords",
        lambda page: [
            KeywordCandidate(f"alpha {index}", "long_tail", ("page",))
            for index in range(3)
        ],
    )
    values = iter((None, 12, None))
    monkeypatch.setattr(
        runtime,
        "collect_signals",
        lambda *args, **kwargs: SearchSignals(trend_7d=next(values)),
    )

    assert runtime.build_monitor(config(tmp_path)).research(URL, False).trends_missing is False


def test_trends_missing_uses_trend_7d_only(tmp_path, monkeypatch) -> None:
    install(monkeypatch, lambda session, url, max_bytes: PAGE)
    monkeypatch.setattr(
        runtime,
        "generate_keywords",
        lambda page: [KeywordCandidate("alpha", "game_name", ("page",))],
    )
    monkeypatch.setattr(
        runtime,
        "collect_signals",
        lambda *args, **kwargs: SearchSignals(trend_30d=25),
    )

    assert runtime.build_monitor(config(tmp_path)).research(URL, False).trends_missing is True


def test_no_github_config_and_notification_retry_use_only_reporter(tmp_path, monkeypatch) -> None:
    reporter = install(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("page fetch must not run during notification retry"),
    )
    monkeypatch.setattr(runtime, "github_issue_poster", lambda *args: pytest.fail("GitHub poster must not be configured"))
    monitor = runtime.build_monitor(config(tmp_path))

    result = monitor.retry_notification(URL)

    assert reporter.issue_post is None
    assert reporter.retried == [URL]
    assert result == ResearchResult(False, "/tmp/report.md", 8, None)


def test_runtime_persists_complete_records_and_partitions_run_issues(
    tmp_path, monkeypatch
) -> None:
    phase = {"new": False}
    issue_calls = []
    empty_urlset = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'

    class Session:
        pass

    def fetch(session, url, max_bytes):
        if url.endswith("index.xml"):
            return b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://poki.com/en/sitemaps/a.xml</loc></sitemap></sitemapindex>'
        if url.endswith("a.xml"):
            return empty_urlset
        if url.endswith("/new"):
            if not phase["new"]:
                return b"<html></html>"
            return b'<a href="/en/g/high">High</a><a href="/en/g/ordinary-a">A</a><a href="/en/g/ordinary-b">B</a>'
        slug = url.rsplit("/", 1)[-1]
        return f"<html><title>{slug}</title><main><h1>{slug}</h1><p>A puzzle game.</p></main></html>".encode()

    monkeypatch.setattr(runtime, "build_session", Session)
    monkeypatch.setattr(runtime, "get_bytes", fetch)
    monkeypatch.setattr(
        runtime,
        "github_issue_poster",
        lambda *args: lambda title, body, marker: issue_calls.append((title, body, marker)) or len(issue_calls),
    )
    monkeypatch.setattr(
        runtime,
        "generate_keywords",
        lambda page: [KeywordCandidate(page.slug, "game_name", ("page",), True)],
    )
    monkeypatch.setattr(
        runtime,
        "collect_signals",
        lambda *args, **kwargs: SearchSignals(trend_7d=10),
    )
    scores = {"high": 80, "ordinary-a": 55, "ordinary-b": 74}
    monkeypatch.setattr(
        runtime,
        "score_opportunity",
        lambda keyword, signals, freshness, expansion: Opportunity(
            keyword,
            signals,
            scores[keyword.phrase],
            0.8,
            "immediate" if scores[keyword.phrase] >= 75 else "watch",
        ),
    )
    monitor = runtime.build_monitor(
        config(
            tmp_path,
            baseline_sample_size=0,
            github_repository="owner/repo",
            github_token="secret",
        )
    )
    monitor.run(datetime(2026, 8, 3, tzinfo=runtime.UTC))
    phase["new"] = True

    result = monitor.run(datetime(2026, 8, 4, tzinfo=runtime.UTC))

    assert result.completed == 3
    assert len(issue_calls) == 2
    assert "high priority" in issue_calls[0][0].lower()
    assert "summary" in issue_calls[1][0].lower()
    records = [json.loads(line) for line in (tmp_path / "games.jsonl").read_text().splitlines()]
    research = [record for record in records if record.get("record_type") == "research"]
    events = [record for record in records if record.get("record_type") == "issue_outcome"]
    assert len(research) == 3 and len(events) == 2
    assert all(record["discovery"]["sources"] == ["new_games"] for record in research)
    assert all(record["first_seen"].startswith("2026-08-04") for record in research)
    assert all(record["recheck"] == {"status": "not_required", "schedule": []} for record in research)
    assert all(record["report"]["reference"].endswith(".md") for record in research)
    assert [record["issue"]["status"] for record in research] == ["pending"] * 3
    state = MonitorState.load(tmp_path / "state.json")
    assert all(game["issue_number"] in {1, 2} for game in state.games.values())


def test_missing_trends_low_score_notifies_and_retries_without_research_io(
    tmp_path, monkeypatch
) -> None:
    phase = {"new": False}
    page_calls = 0
    signal_calls = 0
    issue_calls = 0
    empty_urlset = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'

    def fetch(session, url, max_bytes):
        nonlocal page_calls
        if url.endswith("index.xml"):
            return b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://poki.com/en/sitemaps/a.xml</loc></sitemap></sitemapindex>'
        if url.endswith("a.xml"):
            return empty_urlset
        if url.endswith("/new"):
            return b'<a href="/en/g/low-missing">Low</a>' if phase["new"] else b"<html></html>"
        page_calls += 1
        return b"<html><title>Low</title><main><h1>Low</h1><p>A puzzle game.</p></main></html>"

    def post(title, body, marker):
        nonlocal issue_calls
        issue_calls += 1
        if issue_calls == 1:
            raise RuntimeError("temporary")
        return 77

    def signals(*args, **kwargs):
        nonlocal signal_calls
        signal_calls += 1
        return SearchSignals()

    monkeypatch.setattr(runtime, "build_session", lambda: object())
    monkeypatch.setattr(runtime, "get_bytes", fetch)
    monkeypatch.setattr(runtime, "github_issue_poster", lambda *args: post)
    monkeypatch.setattr(
        runtime,
        "generate_keywords",
        lambda page: [KeywordCandidate("low missing", "game_name", ("page",))],
    )
    monkeypatch.setattr(runtime, "collect_signals", signals)
    monkeypatch.setattr(
        runtime,
        "score_opportunity",
        lambda keyword, signals, freshness, expansion: Opportunity(
            keyword, signals, 40, 0.2, "hold"
        ),
    )
    monitor = runtime.build_monitor(
        config(
            tmp_path,
            baseline_sample_size=0,
            github_repository="owner/repo",
            github_token="secret",
        )
    )
    monitor.run(datetime(2026, 8, 3, tzinfo=runtime.UTC))
    phase["new"] = True

    first = monitor.run(datetime(2026, 8, 4, tzinfo=runtime.UTC))
    state_after_failure = MonitorState.load(tmp_path / "state.json")
    second = monitor.run(datetime(2026, 8, 5, tzinfo=runtime.UTC))

    game = state_after_failure.games["https://poki.com/en/g/low-missing"]
    assert first.completed == 1 and game["notification_pending"] is True
    assert game["recheck_at"] == [
        datetime(2026, 8, day, tzinfo=runtime.UTC).isoformat()
        for day in (11, 18)
    ] + [datetime(2026, 9, 3, tzinfo=runtime.UTC).isoformat()]
    assert second.notification_retried == 1 and second.processed == 0
    assert page_calls == signal_calls == 1
    assert MonitorState.load(tmp_path / "state.json").games[
        "https://poki.com/en/g/low-missing"
    ]["issue_number"] == 77
