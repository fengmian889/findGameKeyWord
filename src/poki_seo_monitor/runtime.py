"""Compose production discovery, research providers, reporting, and orchestration."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .app import DiscoveryResult, Monitor, ResearchContext, ResearchResult, sanitize_error
from .config import Config
from .discovery import merge_discoveries, parse_new_games, parse_sitemap_index, parse_urlset
from .extractor import extract_game_page
from .http import build_session, get_bytes
from .keywords import generate_keywords
from .models import KeywordCandidate, SearchSignals
from .reporting import Reporter, ResearchMetadata, github_issue_poster
from .scoring import score_opportunity
from .state import RECHECK_DELAYS
from .signals import (
    AutocompleteProvider,
    GoogleTrendsProvider,
    SerpApiTrendsProvider,
    SerpCompetitionProvider,
    collect_signals,
    parse_serp_hosts,
)


SITEMAP_MAX_BYTES = 20 * 1024 * 1024
NEW_GAMES_MAX_BYTES = 5 * 1024 * 1024
GAME_PAGE_MAX_BYTES = 3 * 1024 * 1024


def _runtime_error(source: str, error: Exception) -> str:
    return f"{source}: {type(error).__name__}: {sanitize_error(error)}"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _relevant(phrase: str, values: tuple[str, ...]) -> bool:
    needle = " ".join(phrase.casefold().split())
    return any(
        needle in (candidate := " ".join(value.casefold().split())) or candidate in needle
        for value in values
        if value.strip()
    )


def build_monitor(config: Config) -> Monitor:
    """Build a monitor whose HTTP-backed integrations share one session."""
    session = build_session()

    def autocomplete_fetch(keyword: str) -> Any:
        response = session.get(
            "https://suggestqueries.google.com/complete/search",
            params={"client": "firefox", "q": keyword, "hl": "en"},
            timeout=(10, 20),
            allow_redirects=False,
        )
        response.raise_for_status()
        return response.json()

    def serp_fetch(keyword: str) -> list[str]:
        response = session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": keyword, "kl": "us-en"},
            timeout=(10, 25),
            allow_redirects=False,
        )
        response.raise_for_status()
        return parse_serp_hosts(response.text)

    autocomplete = AutocompleteProvider(fetch=autocomplete_fetch, pace_custom=True)
    if config.serpapi_keys:
        trends: GoogleTrendsProvider | SerpApiTrendsProvider = SerpApiTrendsProvider(
            config.serpapi_keys
        )
    else:
        trends = GoogleTrendsProvider()
    serp = SerpCompetitionProvider(fetch_hosts=serp_fetch, pace_custom=True)
    issue_post = (
        github_issue_poster(config.github_repository, config.github_token, session)
        if config.github_repository and config.github_token
        else None
    )
    reporter = Reporter(
        config.reports_dir,
        config.games_path,
        config.keywords_path,
        issue_post,
    )

    def discover() -> DiscoveryResult:
        errors: list[str] = []
        sitemap_games = []
        sitemap_successes = 0
        try:
            index = get_bytes(session, config.sitemap_index, max_bytes=SITEMAP_MAX_BYTES)
            children = parse_sitemap_index(index)
            for child_index, child_url in enumerate(children, start=1):
                try:
                    sitemap_games.extend(
                        parse_urlset(
                            get_bytes(session, child_url, max_bytes=SITEMAP_MAX_BYTES)
                        )
                    )
                    sitemap_successes += 1
                except Exception as error:
                    errors.append(_runtime_error(f"sitemap child {child_index}", error))
            if sitemap_successes == 0:
                raise RuntimeError("no sitemap child could be read")
        except Exception as error:
            errors.append(_runtime_error("sitemap", error))
            sitemap_games = []
            sitemap_successes = 0

        new_games = []
        new_success = False
        try:
            new_games = parse_new_games(
                _decode(get_bytes(session, config.new_games_url, max_bytes=NEW_GAMES_MAX_BYTES))
            )
            new_success = True
        except Exception as error:
            errors.append(_runtime_error("new_games", error))

        if not sitemap_successes and not new_success:
            raise RuntimeError("discovery failed: " + "; ".join(errors))
        merged = merge_discoveries(new_games, sitemap_games)
        return DiscoveryResult(tuple(merged), degraded=bool(errors), errors=tuple(errors))

    def research(
        url: str, notify: bool, context: ResearchContext | None = None
    ) -> ResearchResult:
        page = extract_game_page(
            url,
            _decode(get_bytes(session, url, max_bytes=GAME_PAGE_MAX_BYTES)),
        )
        candidates = generate_keywords(page)[:20]
        opportunities = []
        attempted_trends: list[SearchSignals] = []
        expansion = min(1.0, len(candidates) / 20.0)
        for index, original in enumerate(candidates):
            if index < 10:
                signals = collect_signals(
                    original.phrase,
                    config.geo,
                    trends,
                    autocomplete,
                    include_trends=index < 3,
                    serp=serp,
                    include_serp=index < 1,
                )
            else:
                signals = SearchSignals()
            if index < 3:
                attempted_trends.append(signals)
            verified = _relevant(
                original.phrase, (*signals.autocomplete, *signals.rising_queries)
            )
            candidate = replace(original, verified=verified)
            opportunities.append(
                score_opportunity(
                    candidate,
                    signals,
                    freshness=1.0 if context is None else context.freshness,
                    expansion=expansion,
                )
            )
        trends_missing = bool(attempted_trends) and all(
            signal.trend_7d is None for signal in attempted_trends
        )
        metadata = None
        if context is not None:
            if trends_missing:
                recheck_at = (
                    context.recheck_at
                    if context.recheck_plan_started
                    else tuple(
                        (context.now + timedelta(days=days)).isoformat()
                        for days in RECHECK_DELAYS
                    )
                )
                recheck_status = "scheduled" if recheck_at else "complete"
            else:
                recheck_at = ()
                recheck_status = (
                    "complete" if context.recheck_plan_started else "not_required"
                )
            metadata = ResearchMetadata(
                first_seen=context.first_seen,
                sources=context.sources,
                source_first_seen=context.source_first_seen,
                new_games_rank=context.new_games_rank,
                recheck_at=recheck_at,
                recheck_status=recheck_status,
                errors=tuple(
                    dict.fromkeys(
                        error
                        for opportunity in opportunities
                        for error in opportunity.signals.errors
                    )
                ),
            )
        published = reporter.publish(
            page,
            opportunities,
            (datetime.now(UTC) if context is None else context.now).date().isoformat(),
            allow_issue=notify,
            defer_issue=context is not None,
            metadata=metadata,
        )
        return ResearchResult(
            trends_missing,
            str(published.report_path),
            published.issue_number,
            published.issue_error,
            published.notification_pending,
        )

    def retry_notification(url: str) -> ResearchResult:
        published = reporter.retry_notification(url)
        return ResearchResult(
            False,
            str(published.report_path),
            published.issue_number,
            published.issue_error,
        )

    def publish_notifications(urls: list[str]) -> dict[str, ResearchResult]:
        return {
            url: ResearchResult(
                False,
                str(published.report_path),
                published.issue_number,
                published.issue_error,
            )
            for url, published in reporter.publish_run_notifications(urls).items()
        }

    return Monitor(
        config.state_path,
        discover,
        research,
        retry_notification,
        config.max_games_per_run,
        config.baseline_sample_size,
        publish_notifications=publish_notifications,
    )
