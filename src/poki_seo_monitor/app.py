"""Production orchestration for one bounded monitor run."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
import inspect
from pathlib import Path
import re
from typing import Any

from .models import DiscoveredGame
from .state import MonitorState


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BEARER = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_SECRET = re.compile(
    r"\b(token|api[_-]?key|secret|password)\s*(?:=|:)\s*[^\s,;]+", re.IGNORECASE
)
_CREDENTIAL_URL = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)


def sanitize_error(error: object) -> str:
    message = _CONTROL.sub(" ", str(error))
    message = _BEARER.sub("Bearer [REDACTED]", message)
    message = _SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
    message = _CREDENTIAL_URL.sub(r"\1[REDACTED]@", message)
    message = " ".join(message.split())[:480]
    return message or "unknown error"


@dataclass(frozen=True)
class ResearchResult:
    trends_missing: bool
    report_path: str | None = None
    issue_number: int | None = None
    issue_error: str | None = None
    notification_pending: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchContext:
    """Durable discovery evidence and run time supplied to production research."""

    now: datetime
    first_seen: str
    sources: tuple[str, ...]
    source_first_seen: dict[str, str]
    new_games_rank: int | None
    recheck_at: tuple[str, ...]
    recheck_plan_started: bool
    freshness: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "source_first_seen", dict(self.source_first_seen))
        object.__setattr__(self, "recheck_at", tuple(self.recheck_at))


@dataclass(frozen=True)
class RunSummary:
    baseline: bool
    discovered: int
    new: int
    processed: int
    completed: int
    failed: int
    notification_retried: int
    degraded: bool
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryResult:
    games: tuple[DiscoveredGame, ...]
    degraded: bool = False
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "games", tuple(self.games))
        object.__setattr__(self, "errors", tuple(self.errors))


class Monitor:
    """Coordinate discovery, durable state, bounded research, and retries."""

    def __init__(
        self,
        state_path: str | Path,
        discover: Callable[[], DiscoveryResult | list[str]],
        research: Callable[[str, bool], ResearchResult | bool | None],
        retry_notification: Callable[[str], ResearchResult | bool | None] | int | None = None,
        max_games: int | None = None,
        baseline_sample_size: int | None = None,
        publish_notifications: Callable[
            [list[str]], Mapping[str, ResearchResult | bool | None]
        ]
        | None = None,
    ) -> None:
        # Compatibility for the original five/four-positional-argument test
        # constructor, where retry_notification was not part of the contract.
        if type(retry_notification) is int and (
            max_games is None or (type(max_games) is int and baseline_sample_size is None)
        ):
            if max_games is not None:
                baseline_sample_size = max_games
            max_games = retry_notification
            retry_notification = None
        if max_games is None:
            max_games = 10
        if baseline_sample_size is None:
            baseline_sample_size = 3
        if type(max_games) is not int or max_games <= 0:
            raise ValueError("max_games must be greater than zero")
        if type(baseline_sample_size) is not int or baseline_sample_size < 0:
            raise ValueError("baseline_sample_size must be nonnegative")
        self.state_path = Path(state_path)
        self.discover = discover
        self.research = research
        self.retry_notification = (
            retry_notification if retry_notification is not None else (lambda _url: None)
        )
        self.max_games = max_games
        self.baseline_sample_size = baseline_sample_size
        self.publish_notifications = publish_notifications

    @classmethod
    def for_test(
        cls,
        root: str | Path,
        discover: Callable[[], DiscoveryResult | list[str]],
        research: Callable[[str, bool], ResearchResult | bool | None],
        max_games: int = 10,
        baseline_sample_size: int = 3,
        retry_notification: Callable[[str], ResearchResult | bool | None] | None = None,
        publish_notifications: Callable[
            [list[str]], Mapping[str, ResearchResult | bool | None]
        ]
        | None = None,
    ) -> Monitor:
        """Build a monitor from the compact dependency forms used in tests."""
        return cls(
            state_path=Path(root) / "state.json",
            discover=discover,
            research=research,
            retry_notification=retry_notification,
            max_games=max_games,
            baseline_sample_size=baseline_sample_size,
            publish_notifications=publish_notifications,
        )

    def run(self, now: datetime) -> RunSummary:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")

        # Discovery deliberately precedes all state access: a total discovery
        # outage must not create, load, or rewrite durable state.
        discovered = _adapt_discovery(self.discover())

        baseline = not self.state_path.exists()
        state = MonitorState.load(self.state_path)
        errors = [sanitize_error(error) for error in discovered.errors]
        new_urls = state.diff(discovered.games, now, baseline=baseline)
        # Persist even an empty successful first discovery so later runs are
        # not mistaken for a first-run baseline again.
        mutated = baseline or bool(new_urls)
        processed = completed = failed = notification_retried = 0
        pending_from_run: list[str] = []

        if baseline:
            ranked = [game for game in discovered.games if "new_games" in game.sources]
            ranked.extend(game for game in discovered.games if "new_games" not in game.sources)
            baseline_limit = min(self.baseline_sample_size, self.max_games)
            queue = [game.url for game in ranked[:baseline_limit]]
            notify = False
        else:
            # Notification retries use persisted reports and do not spend the
            # page/signal research budget.
            retry_urls = state.notification_pending_urls()[: self.max_games]
            notification_retried = len(retry_urls)
            if self.publish_notifications is not None and retry_urls:
                self._publish_notification_batch(state, retry_urls, errors)
                mutated = True
            else:
                for url in retry_urls:
                    try:
                        outcome = _adapt_research(self.retry_notification(url))
                        issue_error = sanitize_error(outcome.issue_error) if outcome.issue_error else None
                        state.mark_notification_result(url, outcome.issue_number, issue_error)
                        if issue_error:
                            errors.append(f"{url}: {issue_error}")
                    except Exception as error:
                        message = sanitize_error(error)
                        state.mark_notification_result(url, None, message)
                        errors.append(f"{url}: {message}")
                    mutated = True

            pending = state.pending_urls()
            remaining = max(0, self.max_games - len(pending[: self.max_games]))
            due = state.due_rechecks(now, limit=remaining)
            if due:
                mutated = True
            queue = list(dict.fromkeys([*pending, *due]))[: self.max_games]
            notify = True

        try:
            for url in queue:
                processed += 1
                try:
                    metadata = state.discovery_metadata(url)
                    context = ResearchContext(
                        now=now,
                        first_seen=metadata["first_seen"],
                        sources=metadata["sources"],
                        source_first_seen=metadata["source_first_seen"],
                        new_games_rank=metadata["new_games_rank"],
                        recheck_at=metadata["recheck_at"],
                        recheck_plan_started=metadata["recheck_plan_started"],
                        freshness=state.freshness(url, now),
                    )
                    outcome = _adapt_research(_call_research(self.research, url, notify, context))
                    state.mark_research(url, now, outcome.trends_missing)
                    issue_error = sanitize_error(outcome.issue_error) if outcome.issue_error else None
                    state.mark_publication(
                        url,
                        outcome.report_path,
                        outcome.issue_number,
                        issue_error,
                        notification_pending=(
                            outcome.notification_pending or issue_error is not None
                        ),
                    )
                    if outcome.notification_pending or issue_error is not None:
                        pending_from_run.append(url)
                    completed += 1
                    if issue_error:
                        errors.append(f"{url}: {issue_error}")
                except Exception as error:
                    message = sanitize_error(error)
                    state.mark_error(url, message, now)
                    failed += 1
                    errors.append(f"{url}: {message}")
                mutated = True
            if self.publish_notifications is not None and pending_from_run:
                # Reports and pending state must be durable before any external
                # notification attempt.  A crash after this save is retryable.
                state.save(self.state_path)
                self._publish_notification_batch(state, pending_from_run, errors)
        finally:
            if mutated:
                state.save(self.state_path)

        return RunSummary(
            baseline=baseline,
            discovered=len(discovered.games),
            new=0 if baseline else len(new_urls),
            processed=processed,
            completed=completed,
            failed=failed,
            notification_retried=notification_retried,
            degraded=discovered.degraded or bool(errors),
            errors=tuple(errors),
        )

    def _publish_notification_batch(
        self, state: MonitorState, urls: list[str], errors: list[str]
    ) -> None:
        assert self.publish_notifications is not None
        try:
            raw = self.publish_notifications(list(urls))
            if not isinstance(raw, Mapping):
                raise TypeError("publish_notifications must return a URL mapping")
        except Exception as error:
            message = sanitize_error(error)
            for url in urls:
                state.mark_notification_result(url, None, message)
                errors.append(f"{url}: {message}")
            return
        for url in urls:
            if url not in raw:
                message = "notification result missing for URL"
                state.mark_notification_result(url, None, message)
                errors.append(f"{url}: {message}")
                continue
            outcome = _adapt_research(raw[url])
            issue_error = sanitize_error(outcome.issue_error) if outcome.issue_error else None
            state.mark_notification_result(url, outcome.issue_number, issue_error)
            if issue_error:
                errors.append(f"{url}: {issue_error}")


def _adapt_discovery(value: DiscoveryResult | list[str]) -> DiscoveryResult:
    if isinstance(value, DiscoveryResult):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(url, str) for url in value):
        return DiscoveryResult(
            tuple(
                DiscoveredGame(url, ("discovery",), index + 1)
                for index, url in enumerate(value)
            )
        )
    raise TypeError("discover must return DiscoveryResult or a list of URLs")


def _adapt_research(value: ResearchResult | bool | None) -> ResearchResult:
    if isinstance(value, ResearchResult):
        return value
    if type(value) is bool:
        return ResearchResult(trends_missing=value)
    if value is None:
        return ResearchResult(trends_missing=False)
    raise TypeError("research must return ResearchResult, bool, or None")


def _call_research(
    research: Callable[..., ResearchResult | bool | None],
    url: str,
    notify: bool,
    context: ResearchContext,
) -> ResearchResult | bool | None:
    """Pass freshness context when supported while retaining the legacy API."""
    try:
        inspect.signature(research).bind(url, notify, context)
    except (TypeError, ValueError):
        return research(url, notify)
    return research(url, notify, context)
