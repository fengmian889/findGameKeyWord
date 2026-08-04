"""Durable state for monitoring discovered Poki game URLs."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import DiscoveredGame


SCHEMA_VERSION = 1
RECHECK_DELAYS = (7, 14, 30)
VALID_STATUSES = frozenset({"baseline", "pending", "researched", "retry"})
FRESHNESS_MAX_AGE_DAYS = 30.0
FRESHNESS_MAX_NEW_GAMES_RANK = 51
FRESHNESS_SOURCE_LAG_DAYS = 7.0


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    _require_aware(parsed)
    return parsed


def _invalid_game_field(url: object, field: str, detail: str) -> ValueError:
    return ValueError(f"invalid game state for URL {url!r}, field {field!r}: {detail}")


def _validate_aware_timestamp(url: str, field: str, value: object) -> None:
    if not isinstance(value, str):
        raise _invalid_game_field(url, field, "must be an ISO timestamp string")
    try:
        _parse_datetime(value)
    except (TypeError, ValueError) as error:
        raise _invalid_game_field(url, field, "must be a timezone-aware ISO timestamp") from error


def _validate_game_record(url: object, record: object) -> None:
    if not isinstance(url, str) or not url.strip():
        raise _invalid_game_field(url, "URL", "must be a nonblank string")
    if not isinstance(record, dict):
        raise _invalid_game_field(url, "record", "must be an object")

    required_fields = (
        "first_seen",
        "status",
        "recheck_at",
        "retry_at",
        "recheck_plan_started",
    )
    for field in required_fields:
        if field not in record:
            raise _invalid_game_field(url, field, "is required")

    _validate_aware_timestamp(url, "first_seen", record["first_seen"])

    if not isinstance(record["status"], str) or record["status"] not in VALID_STATUSES:
        raise _invalid_game_field(url, "status", "must be a recognized status")

    recheck_at = record["recheck_at"]
    if not isinstance(recheck_at, list):
        raise _invalid_game_field(url, "recheck_at", "must be a list")
    for timestamp in recheck_at:
        _validate_aware_timestamp(url, "recheck_at", timestamp)

    retry_at = record["retry_at"]
    if retry_at is not None:
        _validate_aware_timestamp(url, "retry_at", retry_at)

    if type(record["recheck_plan_started"]) is not bool:
        raise _invalid_game_field(url, "recheck_plan_started", "must be a boolean")

    if "researched_at" in record:
        _validate_aware_timestamp(url, "researched_at", record["researched_at"])
    if "last_error" in record and not isinstance(record["last_error"], str):
        raise _invalid_game_field(url, "last_error", "must be a string")
    if "notification_pending" in record and type(record["notification_pending"]) is not bool:
        raise _invalid_game_field(url, "notification_pending", "must be a boolean")
    if "report_path" in record and record["report_path"] is not None and (
        not isinstance(record["report_path"], str) or not record["report_path"].strip()
    ):
        raise _invalid_game_field(url, "report_path", "must be a nonblank string or null")
    if "issue_number" in record and record["issue_number"] is not None and (
        type(record["issue_number"]) is not int or record["issue_number"] <= 0
    ):
        raise _invalid_game_field(url, "issue_number", "must be a positive integer or null")
    if "issue_error" in record and record["issue_error"] is not None and (
        not isinstance(record["issue_error"], str) or not record["issue_error"].strip()
    ):
        raise _invalid_game_field(url, "issue_error", "must be a nonblank string or null")
    if "sources" in record:
        sources = record["sources"]
        if (
            not isinstance(sources, list)
            or not all(isinstance(source, str) and source.strip() for source in sources)
            or len(sources) != len(set(sources))
        ):
            raise _invalid_game_field(url, "sources", "must be a unique list of nonblank strings")
        if "source_first_seen" not in record:
            raise _invalid_game_field(url, "source_first_seen", "is required with sources")
    if "source_first_seen" in record:
        source_first_seen = record["source_first_seen"]
        if not isinstance(source_first_seen, dict) or not all(
            isinstance(source, str) and source.strip() for source in source_first_seen
        ):
            raise _invalid_game_field(url, "source_first_seen", "must be an object keyed by source")
        for source, timestamp in source_first_seen.items():
            _validate_aware_timestamp(url, f"source_first_seen.{source}", timestamp)
        if "sources" not in record:
            raise _invalid_game_field(url, "source_first_seen", "requires sources")
        if set(source_first_seen) != set(record["sources"]):
            raise _invalid_game_field(url, "source_first_seen", "keys must match sources")
    if "new_games_rank" in record and record["new_games_rank"] is not None and (
        type(record["new_games_rank"]) is not int or record["new_games_rank"] <= 0
    ):
        raise _invalid_game_field(url, "new_games_rank", "must be a positive integer or null")
    if record.get("new_games_rank") is not None and "new_games" not in record.get("sources", []):
        raise _invalid_game_field(url, "new_games_rank", "requires the new_games source")

    status = record["status"]
    retry_at = record["retry_at"]
    recheck_at = record["recheck_at"]
    plan_started = record["recheck_plan_started"]

    if not plan_started and recheck_at:
        raise _invalid_game_field(
            url, "recheck_at", "must be empty when recheck_plan_started is false"
        )
    if status != "retry" and retry_at is not None:
        raise _invalid_game_field(url, "retry_at", "must be null unless status is retry")
    if status != "retry" and "last_error" in record:
        raise _invalid_game_field(url, "last_error", "is only allowed when status is retry")

    if status == "researched":
        if "researched_at" not in record:
            raise _invalid_game_field(url, "researched_at", "is required when status is researched")
    elif status == "retry":
        if not record.get("last_error"):
            raise _invalid_game_field(url, "last_error", "must be nonempty when status is retry")
        if retry_at is None:
            raise _invalid_game_field(url, "retry_at", "is required when status is retry")
        if (plan_started or recheck_at) and "researched_at" not in record:
            raise _invalid_game_field(
                url,
                "researched_at",
                "is required for a retry with trend rechecks",
            )
    else:
        if "researched_at" in record:
            raise _invalid_game_field(
                url, "researched_at", "is only allowed after successful research"
            )
        if plan_started:
            raise _invalid_game_field(
                url,
                "recheck_plan_started",
                "must be false before successful research",
            )
        if recheck_at:
            raise _invalid_game_field(
                url, "recheck_at", "must be empty before successful research"
            )


def _validate_state(schema_version: object, games: object) -> None:
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {schema_version!r}")
    if not isinstance(games, dict):
        raise ValueError("state file games value must be an object")
    for url, record in games.items():
        _validate_game_record(url, record)


def _fsync_parent_directory(path: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return

    directory_fd = os.open(path.parent, os.O_RDONLY | directory_flag)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@dataclass
class MonitorState:
    """Persisted monitoring state, keyed by canonical game URL."""

    schema_version: int = SCHEMA_VERSION
    games: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> MonitorState:
        """Load state from *path*, returning an empty state when it is absent."""
        state_path = Path(path)
        if not state_path.exists():
            return cls()

        with state_path.open(encoding="utf-8") as state_file:
            payload = json.load(state_file)

        if not isinstance(payload, dict):
            raise ValueError("state file top-level value must be an object")
        _validate_state(payload.get("schema_version"), payload.get("games"))

        return cls(schema_version=SCHEMA_VERSION, games=payload["games"])

    def diff(
        self, urls: Iterable[str | DiscoveredGame], now: datetime, baseline: bool
    ) -> list[str]:
        """Record discoveries and return newly discovered non-baseline URLs.

        String inputs retain the original compact test API.  Structured
        discoveries additionally preserve source evidence used by freshness.
        """
        _require_aware(now)
        observations = list(urls)
        for observation in observations:
            url = observation.url if isinstance(observation, DiscoveredGame) else observation
            if not isinstance(url, str) or not url.strip():
                raise ValueError("URL must be a nonblank string")
            if isinstance(observation, DiscoveredGame):
                if (
                    not observation.sources
                    or any(not isinstance(source, str) or not source.strip() for source in observation.sources)
                    or len(observation.sources) != len(set(observation.sources))
                ):
                    raise ValueError("discovery sources must be unique nonblank strings")
                if observation.source_rank is not None and (
                    type(observation.source_rank) is not int or observation.source_rank <= 0
                ):
                    raise ValueError("source_rank must be a positive integer or None")

        new_urls: list[str] = []
        for observation in observations:
            structured = isinstance(observation, DiscoveredGame)
            url = observation.url if structured else observation
            assert isinstance(url, str)
            is_new = url not in self.games
            if is_new:
                self.games[url] = {
                    "first_seen": now.isoformat(),
                    "status": "baseline" if baseline else "pending",
                    "recheck_at": [],
                    "retry_at": None,
                    "recheck_plan_started": False,
                }
                if not baseline:
                    new_urls.append(url)
            if structured:
                self._observe_provenance(url, observation, now)
        return new_urls

    def _observe_provenance(
        self, url: str, observation: DiscoveredGame, now: datetime
    ) -> None:
        game = self._game(url)
        existing_sources = game.get("sources", [])
        sources = list(existing_sources) if isinstance(existing_sources, list) else []
        timestamps = dict(game.get("source_first_seen", {}))
        for source in observation.sources:
            if source not in sources:
                sources.append(source)
            timestamps.setdefault(source, now.isoformat())
        preferred = [source for source in ("new_games", "sitemap") if source in sources]
        preferred.extend(source for source in sources if source not in preferred)
        game["sources"] = preferred
        game["source_first_seen"] = {source: timestamps[source] for source in preferred}
        if "new_games" in observation.sources and observation.source_rank is not None:
            prior = game.get("new_games_rank")
            game["new_games_rank"] = (
                observation.source_rank
                if prior is None
                else min(prior, observation.source_rank)
            )
        elif "new_games_rank" not in game:
            game["new_games_rank"] = None

    def freshness(self, url: str, now: datetime) -> float:
        """Return documented 0--1 freshness from durable discovery evidence.

        Age decays linearly to zero over 30 days.  At age zero, evidence is
        ``0.60 + 0.25*rank + 0.15*source_timing``.  Rank decays from 1.0 at
        New Games position 1 to zero at position 51.  Source timing is 1.0
        when New Games and sitemap first observe the URL together and decays
        to zero at a seven-day lag; single-source evidence uses 0.75 for New
        Games, 0.25 for sitemap, and 0.50 for legacy/other discovery.
        Multiplying the evidence by age guarantees every recheck decays.
        """
        _require_aware(now)
        game = self._game(url)
        first_seen = _parse_datetime(game["first_seen"])
        age_days = max(0.0, (now - first_seen).total_seconds() / 86_400.0)
        age = max(0.0, 1.0 - age_days / FRESHNESS_MAX_AGE_DAYS)
        rank_value = game.get("new_games_rank")
        rank = (
            0.0
            if rank_value is None
            else max(
                0.0,
                1.0 - (rank_value - 1) / (FRESHNESS_MAX_NEW_GAMES_RANK - 1),
            )
        )
        sources = set(game.get("sources", ()))
        timestamps = game.get("source_first_seen", {})
        if {"new_games", "sitemap"} <= sources and all(
            source in timestamps for source in ("new_games", "sitemap")
        ):
            lag_days = abs(
                (_parse_datetime(timestamps["new_games"]) - _parse_datetime(timestamps["sitemap"]))
                .total_seconds()
                / 86_400.0
            )
            source_timing = max(0.0, 1.0 - lag_days / FRESHNESS_SOURCE_LAG_DAYS)
        elif "new_games" in sources:
            source_timing = 0.75
        elif "sitemap" in sources:
            source_timing = 0.25
        else:
            source_timing = 0.50
        return round(max(0.0, min(1.0, age * (0.60 + 0.25 * rank + 0.15 * source_timing))), 6)

    def discovery_metadata(self, url: str) -> dict[str, Any]:
        """Return a defensive provenance snapshot for research/reporting."""
        game = self._game(url)
        return {
            "first_seen": game["first_seen"],
            "sources": tuple(game.get("sources", ("legacy",))),
            "source_first_seen": dict(game.get("source_first_seen", {})),
            "new_games_rank": game.get("new_games_rank"),
            "recheck_at": tuple(game.get("recheck_at", ())),
            "recheck_plan_started": game.get("recheck_plan_started", False),
        }

    def mark_research(self, url: str, now: datetime, trends_missing: bool) -> None:
        """Record an attempt to research a game and manage its trend rechecks."""
        _require_aware(now)
        game = self._game(url)
        game["status"] = "researched"
        game["researched_at"] = now.isoformat()
        game.pop("last_error", None)
        game["retry_at"] = None

        if trends_missing:
            if not game["recheck_plan_started"]:
                game["recheck_at"] = [
                    (now + timedelta(days=days)).isoformat() for days in RECHECK_DELAYS
                ]
                game["recheck_plan_started"] = True
        else:
            game["recheck_at"] = []

    def pending_urls(self) -> list[str]:
        """Return pending URLs in their original discovery order."""
        return [url for url, game in self.games.items() if game["status"] == "pending"]

    def notification_pending_urls(self) -> list[str]:
        """Return URLs whose already-generated report still needs notification."""
        return [url for url, game in self.games.items() if game.get("notification_pending", False)]

    def due_rechecks(self, now: datetime, limit: int | None = None) -> list[str]:
        """Consume due retries and trend checks, returning each URL once."""
        _require_aware(now)
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("limit must be a nonnegative integer or None")
        candidates: list[str] = []
        for url in sorted(self.games):
            game = self.games[url]
            rechecks = game.get("recheck_at", [])
            retry_at = game.get("retry_at")
            retry_due = retry_at is not None and _parse_datetime(retry_at) <= now
            trend_due = any(_parse_datetime(timestamp) <= now for timestamp in rechecks)
            if trend_due or retry_due:
                candidates.append(url)
        selected = candidates if limit is None else candidates[:limit]
        for url in selected:
            game = self.games[url]
            game["recheck_at"] = [
                timestamp for timestamp in game.get("recheck_at", []) if _parse_datetime(timestamp) > now
            ]
            retry_at = game.get("retry_at")
            retry_due = retry_at is not None and _parse_datetime(retry_at) <= now
            if retry_due:
                game["retry_at"] = None
                game.pop("last_error", None)
                game["status"] = (
                    "researched" if "researched_at" in game else "pending"
                )

        return selected

    def mark_publication(
        self,
        url: str,
        report_path: str | None,
        issue_number: int | None,
        issue_error: str | None,
        notification_pending: bool | None = None,
    ) -> None:
        """Store the latest report and optional notification outcome."""
        game = self._game(url)
        if report_path is not None and (not isinstance(report_path, str) or not report_path.strip()):
            raise ValueError("report_path must be a nonblank string or None")
        if issue_number is not None and (type(issue_number) is not int or issue_number <= 0):
            raise ValueError("issue_number must be a positive integer or None")
        if issue_error is not None and (not isinstance(issue_error, str) or not issue_error.strip()):
            raise ValueError("issue_error must be a nonblank string or None")
        if notification_pending is not None and type(notification_pending) is not bool:
            raise ValueError("notification_pending must be a boolean or None")
        game["report_path"] = report_path
        game["issue_number"] = issue_number
        game["issue_error"] = issue_error
        game["notification_pending"] = (
            issue_error is not None
            if notification_pending is None
            else notification_pending
        )

    def mark_notification_result(
        self, url: str, issue_number: int | None, issue_error: str | None
    ) -> None:
        """Update only notification metadata after retrying an existing report."""
        game = self._game(url)
        self.mark_publication(url, game.get("report_path"), issue_number, issue_error)

    def mark_error(self, url: str, message: str, now: datetime) -> None:
        """Record an error and schedule one retry without changing trend checks."""
        _require_aware(now)
        game = self._game(url)
        game["status"] = "retry"
        game["last_error"] = str(message).strip() or "unknown error"
        game["retry_at"] = (now + timedelta(days=1)).isoformat()

    def save(self, path: str | Path) -> None:
        """Persist state atomically; parent-directory fsync can fail after replacement."""
        state_path = Path(path)
        _validate_state(self.schema_version, self.games)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=state_path.parent,
                prefix=f".{state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                json.dump(
                    {"games": self.games, "schema_version": self.schema_version},
                    temp_file,
                    indent=2,
                    sort_keys=True,
                )
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, state_path)
            temp_name = None
            _fsync_parent_directory(state_path)
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def _game(self, url: str) -> dict[str, Any]:
        try:
            return self.games[url]
        except KeyError:
            raise KeyError(f"unknown game URL: {url}") from None
