# SerpAPI Partial Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve valid Google Trends signals when only one SerpAPI response is usable, while reporting safe component-level diagnostics.

**Architecture:** Keep the public `SerpApiTrendsProvider.research()` and `SearchSignals` contracts. Split TIMESERIES and RELATED_QUERIES parsing into focused helpers, catch each component independently, cache only results with at least one successful component, and merge provider diagnostics in `collect_signals()`.

**Tech Stack:** Python 3.12, dataclasses, pytest, requests, existing `_TimedCache` and error-sanitization utilities.

---

### Task 1: Preserve partial SerpAPI signals

**Files:**
- Modify: `tests/test_signals.py:849-889`
- Modify: `src/poki_seo_monitor/signals.py:299-430`

- [ ] **Step 1: Replace the strict malformed-payload tests with failing partial-result tests**

```python
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
```

- [ ] **Step 2: Run the four tests and verify RED**

Run:

```bash
python -m pytest -q \
  tests/test_signals.py::test_serpapi_trends_keeps_timeseries_when_related_queries_are_missing \
  tests/test_signals.py::test_serpapi_trends_keeps_related_queries_when_timeseries_is_missing \
  tests/test_signals.py::test_serpapi_trends_still_fetches_related_queries_after_timeseries_request_fails \
  tests/test_signals.py::test_serpapi_diagnostics_redact_secrets_and_urls
```

Expected: all four fail because `research()` still raises or aborts when either component is unavailable.

- [ ] **Step 3: Add focused payload parsers before `SerpApiTrendsProvider`**

```python
def _serpapi_response_context(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return f"response_type={type(payload).__name__}"
    safe_keys = sorted(
        key
        for key in payload
        if isinstance(key, str)
        and re.fullmatch(r"[A-Za-z0-9_]{1,40}", key)
        and not re.search(r"token|api[_-]?key|secret|password", key, re.IGNORECASE)
    )[:8]
    details = [f"keys={','.join(safe_keys) or 'none'}"]
    metadata = payload.get("search_metadata")
    if isinstance(metadata, Mapping):
        status = metadata.get("status")
        if isinstance(status, (str, int, float, bool)):
            details.append(f"status={status}")
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        details.append(f"error={error}")
    return " ".join(details)


def _serpapi_payload_error(data_type: str, payload: object) -> ValueError:
    return ValueError(
        f"SerpAPI {data_type} unavailable ({_serpapi_response_context(payload)})"
    )


def _parse_serpapi_timeseries(
    payload: object,
) -> tuple[float | None, float | None, float | None]:
    if not isinstance(payload, Mapping):
        raise _serpapi_payload_error("TIMESERIES", payload)
    interest_over_time = payload.get("interest_over_time")
    if not isinstance(interest_over_time, Mapping):
        raise _serpapi_payload_error("TIMESERIES", payload)
    timeline_data = interest_over_time.get("timeline_data")
    if not isinstance(timeline_data, list):
        raise _serpapi_payload_error("TIMESERIES", payload)

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
        value = first_value.get("extracted_value", first_value.get("value"))
        if timestamp is None or value is None:
            continue
        try:
            observed_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        except (ValueError, TypeError):
            continue
        points.append({"date": observed_at.isoformat(), "value": value})
    return _trend_windows(_trend_observations(points))


def _parse_serpapi_related_queries(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        raise _serpapi_payload_error("RELATED_QUERIES", payload)
    related_queries = payload.get("related_queries")
    if not isinstance(related_queries, Mapping):
        raise _serpapi_payload_error("RELATED_QUERIES", payload)
    rising = related_queries.get("rising")
    if not isinstance(rising, list):
        rising = []
    return _rising_queries(rising)
```

- [ ] **Step 4: Replace `SerpApiTrendsProvider.research()` with independent component handling**

```python
def research(self, keyword: str, geo: str) -> SearchSignals:
    cache_key = f"{_normalise_text(keyword)}:{geo.casefold()}"
    cached, value = self._cache.get(cache_key)
    if cached:
        return value

    errors: list[str] = []
    successful_components = 0
    trend_7d = trend_30d = trend_90d = None
    rising_queries: tuple[str, ...] = ()
    rising_queries_observed = False

    try:
        payload = self._fetch(
            "google_trends",
            {"q": keyword, "geo": geo, "data_type": "TIMESERIES"},
        )
        trend_7d, trend_30d, trend_90d = _parse_serpapi_timeseries(payload)
        successful_components += 1
    except Exception as error:
        errors.append(_serpapi_component_error("TIMESERIES", error))

    try:
        payload = self._fetch(
            "google_trends",
            {"q": keyword, "geo": geo, "data_type": "RELATED_QUERIES"},
        )
        rising_queries = _parse_serpapi_related_queries(payload)
        rising_queries_observed = True
        successful_components += 1
    except Exception as error:
        errors.append(_serpapi_component_error("RELATED_QUERIES", error))

    result = SearchSignals(
        trend_7d=trend_7d,
        trend_30d=trend_30d,
        trend_90d=trend_90d,
        rising_queries=rising_queries,
        errors=tuple(errors),
        rising_queries_observed=rising_queries_observed,
    )
    self._cache.put(cache_key, result)
    return result
```

Add this helper beside `_provider_error()` so all component exceptions use the existing sanitizer:

```python
def _serpapi_component_error(data_type: str, error: Exception) -> str:
    wrapped = RuntimeError(
        f"SerpAPI {data_type} failed ({type(error).__name__}): {error}"
    )
    return _provider_error("trends", wrapped)
```

- [ ] **Step 5: Run the four tests and verify GREEN**

Run the command from Step 2.

Expected: `4 passed`.

- [ ] **Step 6: Run all SerpAPI tests**

Run:

```bash
python -m pytest -q tests/test_signals.py -k serpapi
```

Expected: all selected tests pass; update the two obsolete strict-rejection assertions to the partial-result expectations from Step 1 rather than weakening production behavior.

- [ ] **Step 7: Commit the partial-result parser**

```bash
git add src/poki_seo_monitor/signals.py tests/test_signals.py
git commit -m "fix: preserve partial SerpAPI trend results"
```

### Task 2: Cache only useful results

**Files:**
- Modify: `tests/test_signals.py:849-1035`
- Modify: `src/poki_seo_monitor/signals.py:299-450`

- [ ] **Step 1: Add the failing total-failure cache test**

```python
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
```

- [ ] **Step 2: Run the cache test and verify RED**

Run:

```bash
python -m pytest -q \
  tests/test_signals.py::test_serpapi_trends_does_not_cache_when_both_components_fail
```

Expected: FAIL because Task 1 caches the empty failure result and performs only two calls instead of four.

- [ ] **Step 3: Guard the existing cache write**

Replace the unconditional cache write at the end of `research()`:

```python
if successful_components:
    self._cache.put(cache_key, result)
return result
```

- [ ] **Step 4: Run the cache test and verify GREEN**

Run the command from Step 2.

Expected: `1 passed`.

- [ ] **Step 5: Commit request independence and caching behavior**

```bash
git add src/poki_seo_monitor/signals.py tests/test_signals.py
git commit -m "test: cover SerpAPI partial failure caching"
```

### Task 3: Expose provider diagnostics through `collect_signals()`

**Files:**
- Modify: `tests/test_signals.py:488-555`
- Modify: `src/poki_seo_monitor/signals.py:758-815`

- [ ] **Step 1: Add the failing collector test**

```python
def test_collect_signals_keeps_partial_trends_and_provider_errors() -> None:
    class PartialTrends:
        def research(self, keyword: str, geo: str) -> SearchSignals:
            return SearchSignals(
                trend_7d=75.0,
                errors=("trends: RuntimeError: SerpAPI RELATED_QUERIES unavailable",),
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
```

Add `SearchSignals` to the existing test import:

```python
from poki_seo_monitor.models import SearchSignals
```

- [ ] **Step 2: Run the collector test and verify RED**

Run:

```bash
python -m pytest -q \
  tests/test_signals.py::test_collect_signals_keeps_partial_trends_and_provider_errors
```

Expected: FAIL because `collect_signals()` currently drops `trend_signals.errors`.

- [ ] **Step 3: Merge trend provider diagnostics in `collect_signals()`**

Insert the `else` branch immediately after the Trends provider call:

```python
if include_trends:
    try:
        trend_signals = trends.research(keyword, geo)
    except Exception as error:
        errors.append(_provider_error("trends", error))
    else:
        errors.extend(trend_signals.errors)
```

Keep the return statement unchanged except that its existing `errors=tuple(errors)` now includes the Provider diagnostics.

- [ ] **Step 4: Run the collector test and verify GREEN**

Run the command from Step 2.

Expected: `1 passed`.

- [ ] **Step 5: Run the complete signal-provider test module**

```bash
python -m pytest -q tests/test_signals.py
```

Expected: all tests in `tests/test_signals.py` pass with zero warnings or errors.

- [ ] **Step 6: Commit collector diagnostics**

```bash
git add src/poki_seo_monitor/signals.py tests/test_signals.py
git commit -m "fix: report partial SerpAPI diagnostics"
```

### Task 4: Full regression verification

**Files:**
- Verify: `src/poki_seo_monitor/signals.py`
- Verify: `tests/test_signals.py`

- [ ] **Step 1: Run formatting and whitespace validation**

```bash
git diff --check
```

Expected: exit code 0 and no output.

- [ ] **Step 2: Run the complete test suite**

```bash
python -m pytest -q
```

Expected: all project tests pass with zero failures.

- [ ] **Step 3: Inspect the final diff**

```bash
git status --short --branch
git diff a3e35d1 -- src/poki_seo_monitor/signals.py tests/test_signals.py
```

Expected: only the planned Provider, collector, and test changes are present; generated `data/` and `reports/` files remain untouched.
