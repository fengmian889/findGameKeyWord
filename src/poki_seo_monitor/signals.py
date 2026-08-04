"""Replaceable providers for lightweight keyword search signals.

The providers in this module deliberately keep external I/O at their edges so
callers can substitute deterministic fetch functions in tests and batch jobs.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import itertools
import math
import multiprocessing
import os
import re
import signal
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .http import build_session
from .models import SearchSignals


_WHITESPACE = re.compile(r"\s+", re.UNICODE)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_URL = re.compile(r"https?://[^\s\x00-\x1f\x7f-\x9f]+", re.IGNORECASE)
_BEARER_TOKEN = re.compile(r"\bbearer[ \t]+[^\s,;]+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?<![\w-])(?P<label_quote>['\"]?)(?P<label>token|api[_-]?key|key|secret|password)"
    r"(?P=label_quote)(?![\w-])(?P<separator>\s*(?:=|:)\s*)"
    r"(?P<value_quote>['\"]?)(?P<value>[^\s,;'\"]+)(?P=value_quote)",
    re.IGNORECASE,
)
_STRONG_DOMAINS = (
    "poki.com",
    "crazygames.com",
    "itch.io",
    "coolmathgames.com",
    "y8.com",
)


def _normalise_text(value: str) -> str:
    """Return a case-folded, whitespace-collapsed phrase."""
    return _WHITESPACE.sub(" ", value).strip().casefold()


class _TimedCache:
    """A small thread-safe LRU cache with monotonic expiry."""

    def __init__(self, limit: int, ttl: float, monotonic: Callable[[], float]) -> None:
        self._limit = limit
        self._ttl = ttl
        self._monotonic = monotonic
        self._values: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bool, Any]:
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return False, None
            created_at, value = entry
            if self._monotonic() - created_at >= self._ttl:
                del self._values[key]
                return False, None
            self._values.move_to_end(key)
            return True, value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._values[key] = (self._monotonic(), value)
            self._values.move_to_end(key)
            while len(self._values) > self._limit:
                self._values.popitem(last=False)


class _NetworkPacer:
    """Serialize default-provider request starts at a polite minimum interval."""

    def __init__(
        self,
        interval: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._interval = interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._monotonic()
            if self._next_allowed is not None and now < self._next_allowed:
                self._sleep(self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed or now) + self._interval


class _DefaultHttpState:
    """Shared cache and limiter state for one external endpoint."""

    def __init__(
        self,
        *,
        cache_limit: int,
        ttl: float,
        interval: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache = _TimedCache(cache_limit, ttl, monotonic)
        self.pacer = _NetworkPacer(interval, monotonic, sleep)


def _new_default_autocomplete_state(
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> _DefaultHttpState:
    return _DefaultHttpState(
        cache_limit=256, ttl=6 * 60 * 60, interval=0.25, monotonic=monotonic, sleep=sleep
    )


def _new_default_serp_state(
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> _DefaultHttpState:
    return _DefaultHttpState(
        cache_limit=512, ttl=60 * 60, interval=1.0, monotonic=monotonic, sleep=sleep
    )


def _new_default_serpapi_trends_state(
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> _DefaultHttpState:
    return _DefaultHttpState(
        cache_limit=256, ttl=12 * 60 * 60, interval=1.0, monotonic=monotonic, sleep=sleep
    )


_DEFAULT_AUTOCOMPLETE_STATE = _new_default_autocomplete_state()
_DEFAULT_SERP_STATE = _new_default_serp_state()
_DEFAULT_SERPAPI_TRENDS_STATE = _new_default_serpapi_trends_state()


def _reset_default_http_state(
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Reset shared default endpoint state; intended for deterministic tests."""
    global _DEFAULT_AUTOCOMPLETE_STATE, _DEFAULT_SERP_STATE, _DEFAULT_SERPAPI_TRENDS_STATE
    _DEFAULT_AUTOCOMPLETE_STATE = _new_default_autocomplete_state(monotonic, sleep)
    _DEFAULT_SERP_STATE = _new_default_serp_state(monotonic, sleep)
    _DEFAULT_SERPAPI_TRENDS_STATE = _new_default_serpapi_trends_state(monotonic, sleep)


class AutocompleteProvider:
    """Collect relevant Google autocomplete phrases for a keyword."""

    def __init__(
        self,
        fetch: Callable[[str], Any] | None = None,
        *,
        pace_custom: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._default_network = fetch is None
        self._session = build_session() if self._default_network else None
        self._fetch = fetch or self._default_fetch
        custom_clock = monotonic is not time.monotonic or sleep is not time.sleep
        if self._default_network and not custom_clock:
            state = _DEFAULT_AUTOCOMPLETE_STATE
        else:
            state = _new_default_autocomplete_state(monotonic, sleep)
        self._cache = state.cache
        self._pacer = state.pacer if self._default_network or pace_custom else None

    def _default_fetch(self, keyword: str) -> Any:
        assert self._session is not None
        assert self._pacer is not None
        self._pacer.wait()
        response = self._session.get(
            "https://suggestqueries.google.com/complete/search",
            params={"client": "firefox", "q": keyword, "hl": "en"},
            timeout=(10, 20),
            allow_redirects=False,
        )
        response.raise_for_status()
        return response.json()

    def suggest(self, keyword: str) -> tuple[str, ...]:
        normalized_keyword = _normalise_text(keyword)
        cached, value = self._cache.get(normalized_keyword)
        if cached:
            return value
        if not self._default_network and self._pacer is not None:
            self._pacer.wait()
        payload = self._fetch(keyword)
        if (
            not isinstance(payload, list)
            or len(payload) < 2
            or not isinstance(payload[1], list)
        ):
            raise ValueError("Malformed autocomplete payload")

        suggestions: list[str] = []
        seen: set[str] = set()
        for suggestion in payload[1]:
            if not isinstance(suggestion, str):
                continue
            normalized = _normalise_text(suggestion)
            if (
                not normalized
                or normalized == normalized_keyword
                or normalized_keyword not in normalized
                or normalized in seen
            ):
                continue
            seen.add(normalized)
            suggestions.append(normalized)
        result = tuple(suggestions)
        self._cache.put(normalized_keyword, result)
        return result


class GoogleTrendsProvider:
    """Read relative Google Trends interest and rising-query signals.

    Default trendspyg work runs in a child process so a timeout can kill its
    browser process group.  Injected callables retain a lightweight, injectable
    deadline runner and do not need to be spawn-picklable.
    """

    def __init__(
        self,
        explore: Callable[..., Any] | None = None,
        *,
        deadline_seconds: float = 90.0,
        deadline_runner: Callable[[Callable[[], Any], float], Any] | None = None,
        process_runner: Callable[[str, str, str, float], Any] | None = None,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be greater than zero")
        self._explore = explore
        self._deadline_seconds = deadline_seconds
        self._deadline_runner = deadline_runner or _run_with_deadline
        self._process_runner = process_runner or _run_default_trends_process

    @staticmethod
    def _default_explore(keyword: str, **kwargs: Any) -> Any:
        # trendspyg requires a browser-capable environment, so importing it is
        # deferred until a caller elects to use the default provider.
        from trendspyg import download_google_trends_explore

        return download_google_trends_explore(keyword, **kwargs)

    def research(self, keyword: str, geo: str) -> SearchSignals:
        if self._explore is None:
            payload = self._process_runner(
                keyword, geo, "today 3-m", self._deadline_seconds
            )
        else:
            payload = self._deadline_runner(
                lambda: self._explore(keyword, geo=geo, timeframe="today 3-m"),
                self._deadline_seconds,
            )
        if not isinstance(payload, Mapping):
            raise ValueError("Malformed Google Trends payload")

        points = payload.get("interest_over_time")
        related = payload.get("related_queries")
        if not isinstance(points, list) or not isinstance(related, Mapping):
            raise ValueError("Malformed Google Trends payload")
        rising = related.get("rising")
        if not isinstance(rising, list):
            raise ValueError("Malformed Google Trends payload")

        observations = _trend_observations(points)
        trend_7d, trend_30d, trend_90d = _trend_windows(observations)
        return SearchSignals(
            trend_7d=trend_7d,
            trend_30d=trend_30d,
            trend_90d=trend_90d,
            rising_queries=_rising_queries(rising),
            rising_queries_observed=True,
        )


class SerpApiTrendsProvider:
    """Read Google Trends data via SerpAPI HTTP endpoints.

    Makes two HTTP GET requests to SerpAPI for TIMESERIES and RELATED_QUERIES
    data, with 12h TTL cache and 1s pacer between requests.

    Accepts a pool of API keys and rotates through them round-robin to spread
    usage across multiple accounts.
    """

    def __init__(
        self,
        api_keys: str | tuple[str, ...],
        fetch: Callable[[str, dict[str, str]], Any] | None = None,
        *,
        pace_custom: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(api_keys, str):
            api_keys = (api_keys,)
        if not api_keys:
            raise ValueError("At least one SerpAPI key is required")
        self._api_keys = api_keys
        self._key_cycle = itertools.cycle(api_keys)
        self._key_lock = threading.Lock()
        self._default_network = fetch is None
        self._session = build_session() if self._default_network else None
        self._fetch = fetch or self._default_fetch
        custom_clock = monotonic is not time.monotonic or sleep is not time.sleep
        if self._default_network and not custom_clock:
            state = _DEFAULT_SERPAPI_TRENDS_STATE
        else:
            state = _new_default_serpapi_trends_state(monotonic, sleep)
        self._cache = state.cache
        self._pacer = state.pacer if self._default_network or pace_custom else None

    def _next_key(self) -> str:
        with self._key_lock:
            return next(self._key_cycle)

    def _default_fetch(self, engine: str, params: dict[str, str]) -> Any:
        assert self._session is not None
        assert self._pacer is not None
        self._pacer.wait()
        request_params = {"api_key": self._next_key(), "engine": engine, **params}
        response = self._session.get(
            "https://serpapi.com/search",
            params=request_params,
            timeout=(10, 20),
            allow_redirects=False,
        )
        response.raise_for_status()
        return response.json()

    def research(self, keyword: str, geo: str) -> SearchSignals:
        cache_key = f"{_normalise_text(keyword)}:{geo.casefold()}"
        cached, value = self._cache.get(cache_key)
        if cached:
            return value

        # Fetch TIMESERIES data
        timeseries_payload = self._fetch(
            "google_trends",
            {"q": keyword, "geo": geo, "data_type": "TIMESERIES"},
        )

        # Fetch RELATED_QUERIES data
        related_payload = self._fetch(
            "google_trends",
            {"q": keyword, "geo": geo, "data_type": "RELATED_QUERIES"},
        )

        # Extract and transform timeseries data
        interest_over_time = timeseries_payload.get("interest_over_time")
        if not isinstance(interest_over_time, Mapping):
            raise ValueError("Malformed SerpAPI TIMESERIES payload")

        timeline_data = interest_over_time.get("timeline_data")
        if not isinstance(timeline_data, list):
            raise ValueError("Malformed SerpAPI TIMESERIES payload")

        # Transform to _trend_observations compatible format
        points: list[dict[str, Any]] = []
        for entry in timeline_data:
            if not isinstance(entry, Mapping):
                continue
            timestamp = entry.get("timestamp")
            values = entry.get("values")
            if not isinstance(values, list) or not values:
                continue
            first_value = values[0]
            if not isinstance(first_value, Mapping):
                continue
            # Use extracted_value if available, otherwise parse value string
            value = first_value.get("extracted_value", first_value.get("value"))
            if timestamp is not None and value is not None:
                # Convert unix timestamp to ISO format
                try:
                    dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
                    points.append({"date": dt.isoformat(), "value": value})
                except (ValueError, TypeError):
                    continue

        # Extract related queries
        related_queries = related_payload.get("related_queries")
        if not isinstance(related_queries, Mapping):
            raise ValueError("Malformed SerpAPI RELATED_QUERIES payload")

        rising = related_queries.get("rising")
        if not isinstance(rising, list):
            rising = []

        observations = _trend_observations(points)
        trend_7d, trend_30d, trend_90d = _trend_windows(observations)
        result = SearchSignals(
            trend_7d=trend_7d,
            trend_30d=trend_30d,
            trend_90d=trend_90d,
            rising_queries=_rising_queries(rising),
            rising_queries_observed=True,
        )

        self._cache.put(cache_key, result)
        return result


def _run_with_deadline(operation: Callable[[], Any], deadline_seconds: float) -> Any:
    """Run one potentially blocking operation without keeping Python alive."""
    completed = threading.Event()
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = operation()
        except BaseException as error:
            outcome["error"] = error
        finally:
            completed.set()

    worker = threading.Thread(target=run, daemon=True, name="google-trends-provider")
    worker.start()
    if not completed.wait(deadline_seconds):
        raise TimeoutError(f"Google Trends research exceeded {deadline_seconds:g} seconds")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _trends_process_context() -> Any:
    """Use fork on POSIX so the top-level worker remains simple and isolated."""
    return multiprocessing.get_context("fork" if os.name == "posix" else "spawn")


def _trendspyg_worker(sender: Any, keyword: str, geo: str, timeframe: str) -> None:
    """Run the default trendspyg adapter in a separately killable process."""
    isolated_process_group = False
    if os.name == "posix":
        try:
            os.setsid()
            isolated_process_group = True
        except OSError:
            pass
    try:
        sender.send(("ready", isolated_process_group))
        from trendspyg import download_google_trends_explore

        sender.send(
            ("value", download_google_trends_explore(keyword, geo=geo, timeframe=timeframe))
        )
    except BaseException as error:
        sender.send(("error", (type(error).__name__, str(error))))
    finally:
        sender.close()


def _signal_process_group(process: Any, signal_number: int) -> None:
    """Best-effort child-group cleanup on POSIX, with no effect elsewhere."""
    if os.name != "posix" or not getattr(process, "pid", None):
        return
    try:
        os.killpg(os.getpgid(process.pid), signal_number)
    except OSError:
        pass


def _stop_trends_process(process: Any, *, signal_group: bool) -> None:
    """Terminate a worker and, if needed, kill its process group."""
    if signal_group:
        _signal_process_group(process, signal.SIGTERM)
    process.terminate()
    process.join(1.0)
    if process.is_alive():
        if signal_group:
            _signal_process_group(process, signal.SIGKILL)
        process.kill()
        process.join(1.0)


def _run_default_trends_process(
    keyword: str, geo: str, timeframe: str, deadline_seconds: float
) -> Any:
    """Run trendspyg under a timeout and always close its IPC endpoints."""
    receiver = sender = process = None
    isolated_process_group = False
    started_at = time.monotonic()
    try:
        context = _trends_process_context()
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_trendspyg_worker, args=(sender, keyword, geo, timeframe)
        )
        process.start()
        sender.close()
        sender = None
        remaining = max(0.0, deadline_seconds - (time.monotonic() - started_at))
        if not receiver.poll(remaining):
            _stop_trends_process(process, signal_group=False)
            raise TimeoutError(f"Google Trends research exceeded {deadline_seconds:g} seconds")
        try:
            kind, payload = receiver.recv()
        except (EOFError, OSError) as error:
            raise RuntimeError("Google Trends worker exited without a result") from error
        if kind != "ready":
            raise RuntimeError("Google Trends worker failed before initialization")
        isolated_process_group = bool(payload)
        remaining = max(0.0, deadline_seconds - (time.monotonic() - started_at))
        if not receiver.poll(remaining):
            _stop_trends_process(process, signal_group=isolated_process_group)
            raise TimeoutError(f"Google Trends research exceeded {deadline_seconds:g} seconds")
        try:
            kind, payload = receiver.recv()
        except (EOFError, OSError) as error:
            raise RuntimeError("Google Trends worker exited without a result") from error
        process.join(1.0)
        if process.is_alive():
            _stop_trends_process(process, signal_group=isolated_process_group)
        if kind == "error":
            error_type, message = payload
            raise RuntimeError(f"trendspyg {error_type}: {message}")
        if kind != "value":
            raise RuntimeError("Google Trends worker returned an invalid result")
        return payload
    finally:
        if process is not None and process.is_alive():
            _stop_trends_process(process, signal_group=isolated_process_group)
        if sender is not None:
            sender.close()
        if receiver is not None:
            receiver.close()


def _trend_observations(points: list[Any]) -> list[tuple[datetime | None, float]]:
    observations: list[tuple[datetime | None, float]] = []
    for point in points:
        if not isinstance(point, Mapping):
            continue
        value = _interest_value(point.get("value"))
        if value is None:
            continue
        observations.append((_iso_timestamp(point.get("date", point.get("timestamp"))), value))
    return observations


def _interest_value(raw: Any) -> float | None:
    if isinstance(raw, (list, tuple)):
        if len(raw) != 1:
            return None
        raw = raw[0]
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not 0 <= value <= 100:
        return None
    return value


def _iso_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _trend_windows(
    observations: list[tuple[datetime | None, float]],
) -> tuple[float | None, float | None, float | None]:
    if not observations:
        return None, None, None
    if all(timestamp is not None for timestamp, _ in observations):
        dated = [(timestamp, value) for timestamp, value in observations]
        latest = max(timestamp for timestamp, _ in dated)
        return _calendar_average(dated, latest, 7), _calendar_average(
            dated, latest, 30
        ), _calendar_average(dated, latest, 90)
    values = [value for _, value in observations]
    return _average(values[-7:]), _average(values[-30:]), _average(values[-90:])


def _calendar_average(
    observations: list[tuple[datetime, float]], latest: datetime, days: int
) -> float | None:
    return _average(
        value
        for timestamp, value in observations
        if timestamp >= latest - timedelta(days=days)
    )


def _average(values: Any) -> float | None:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else None


def _rising_queries(rising: list[Any]) -> tuple[str, ...]:
    queries: list[str] = []
    seen: set[str] = set()
    for item in rising:
        if not isinstance(item, Mapping) or not isinstance(item.get("query"), str):
            continue
        query = _normalise_text(item["query"])
        if not query or query in seen:
            continue
        seen.add(query)
        queries.append(query)
        if len(queries) == 20:
            break
    return tuple(queries)


def parse_serp_hosts(html: str) -> list[str]:
    """Extract up to ten unique, HTTP(S) result hostnames from DDG HTML."""
    soup = BeautifulSoup(html, "lxml")
    hosts: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select("a.result__a"):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        target = _ddg_target(href)
        host = _normalise_hostname(target)
        if host is not None and host not in seen:
            seen.add(host)
            hosts.append(host)
            if len(hosts) == 10:
                break
    return hosts


def _ddg_target(href: str) -> str | None:
    try:
        absolute = urljoin("https://html.duckduckgo.com/", href)
        parsed = urlsplit(absolute)
        _ = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return None
    is_redirect = (
        hostname is not None
        and (hostname == "duckduckgo.com" or hostname.endswith(".duckduckgo.com"))
        and parsed.path.startswith("/l/")
    )
    if not is_redirect:
        return absolute
    targets = parse_qs(parsed.query).get("uddg")
    return targets[0] if targets and targets[0].strip() else None


class SerpCompetitionProvider:
    """Estimate SERP competition from a small, low-frequency DDG result set."""

    def __init__(
        self,
        fetch_hosts: Callable[[str], list[str]] | None = None,
        *,
        pace_custom: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._default_network = fetch_hosts is None
        self._session = build_session() if self._default_network else None
        self._fetch_hosts = fetch_hosts or self._default_fetch_hosts
        custom_clock = monotonic is not time.monotonic or sleep is not time.sleep
        if self._default_network and not custom_clock:
            state = _DEFAULT_SERP_STATE
        else:
            state = _new_default_serp_state(monotonic, sleep)
        self._cache = state.cache
        self._pacer = state.pacer if self._default_network or pace_custom else None

    def _default_fetch_hosts(self, keyword: str) -> list[str]:
        assert self._session is not None
        assert self._pacer is not None
        self._pacer.wait()
        response = self._session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": keyword, "kl": "us-en"},
            timeout=(10, 25),
            allow_redirects=False,
        )
        response.raise_for_status()
        return parse_serp_hosts(response.text)

    def competition(self, keyword: str) -> float | None:
        cache_key = _normalise_text(keyword)
        cached, value = self._cache.get(cache_key)
        if cached:
            return value
        if not self._default_network and self._pacer is not None:
            self._pacer.wait()
        hosts: list[str] = []
        seen: set[str] = set()
        for candidate in self._fetch_hosts(keyword):
            host = _normalise_hostname(candidate)
            if host is not None and host not in seen:
                seen.add(host)
                hosts.append(host)
        if not hosts:
            self._cache.put(cache_key, None)
            return None
        strong = sum(_is_strong_domain(host) for host in hosts)
        result = round(strong / len(hosts), 2)
        self._cache.put(cache_key, result)
        return result


def _normalise_hostname(target: str) -> str | None:
    if not isinstance(target, str) or not target.strip() or any(char.isspace() for char in target):
        return None
    try:
        parsed = urlsplit(target.strip())
        if not parsed.scheme:
            parsed = urlsplit(f"https://{target.strip()}")
        _ = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    hostname = hostname.casefold().rstrip(".")
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname or ".." in hostname:
        return None
    return hostname


def _is_strong_domain(host: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in _STRONG_DOMAINS)


def collect_signals(
    keyword: str,
    geo: str,
    trends: GoogleTrendsProvider | SerpApiTrendsProvider,
    autocomplete: AutocompleteProvider,
    include_trends: bool = True,
    serp: SerpCompetitionProvider | None = None,
    include_serp: bool = False,
    include_autocomplete: bool = True,
) -> SearchSignals:
    """Collect independent search signals without losing partial results."""
    trend_signals = SearchSignals()
    autocomplete_values: tuple[str, ...] = ()
    autocomplete_observed = False
    competition: float | None = None
    errors: list[str] = []

    if include_trends:
        try:
            trend_signals = trends.research(keyword, geo)
        except Exception as error:  # Providers are optional external integrations.
            errors.append(_provider_error("trends", error))
    if include_autocomplete:
        try:
            autocomplete_values = autocomplete.suggest(keyword)
            autocomplete_observed = True
        except Exception as error:  # Providers are optional external integrations.
            errors.append(_provider_error("autocomplete", error))
    if include_serp and serp is not None:
        try:
            competition = serp.competition(keyword)
        except Exception as error:  # Providers are optional external integrations.
            errors.append(_provider_error("serp", error))

    return SearchSignals(
        trend_7d=trend_signals.trend_7d,
        trend_30d=trend_signals.trend_30d,
        trend_90d=trend_signals.trend_90d,
        rising_queries=trend_signals.rising_queries,
        autocomplete=autocomplete_values,
        competition=competition,
        errors=tuple(errors),
        autocomplete_observed=autocomplete_observed,
        rising_queries_observed=trend_signals.rising_queries_observed,
    )


def _provider_error(provider: str, error: Exception) -> str:
    message = _CONTROL_CHARACTERS.sub(" ", str(error))
    message = _URL.sub("<url>", message)
    message = _BEARER_TOKEN.sub("Bearer <redacted>", message)
    message = _SECRET_ASSIGNMENT.sub(_redact_assignment, message)
    message = _WHITESPACE.sub(" ", message).strip()
    rendered = f"{provider}: {type(error).__name__}: {message}"
    return rendered[:160]


def _redact_assignment(match: re.Match[str]) -> str:
    return (
        f"{match['label_quote']}{match['label']}{match['label_quote']}"
        f"{match['separator']}{match['value_quote']}<redacted>{match['value_quote']}"
    )
