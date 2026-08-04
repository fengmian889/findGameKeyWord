# Poki SEO Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a GitHub Actions automation that discovers new Poki English game pages, researches three keyword groups with free Google search signals, scores opportunities, persists structured results, and creates idempotent GitHub Issues.

**Architecture:** A Python package implements a linear but modular pipeline: dual-source discovery, URL normalization and state diffing, page extraction, keyword generation, replaceable signal providers, scoring, persistence, and notification. All external I/O is behind narrow interfaces so unit and integration tests use fixtures and fakes; the live workflow degrades when one source or Google Trends is unavailable.

**Tech Stack:** Python 3.12, requests, Beautiful Soup 4, lxml, trendspyg 1.1.1, pytest, pytest-cov, responses, GitHub Actions, GitHub REST API.

---

## File map

```text
pyproject.toml                         Packaging, pinned dependencies, pytest config
README.md                              Setup, commands, limitations, GitHub configuration
.gitignore                             Python caches and local environment files
.github/workflows/monitor.yml          Scheduled/manual production workflow
src/poki_seo_monitor/
  __init__.py                          Package version
  config.py                            Environment-backed runtime configuration
  models.py                            Shared immutable domain records
  http.py                              Bounded HTTP client with retry policy
  urls.py                              Poki game URL normalization and validation
  discovery.py                         Sitemap and New Games discovery
  extractor.py                         Game-page fact extraction
  state.py                             Atomic JSON state and recheck scheduling
  keywords.py                          Three-group candidate generation
  signals.py                           Autocomplete and Google Trends provider adapters
  scoring.py                           Trend Opportunity Score and confidence
  reporting.py                         Markdown/JSONL/CSV output and GitHub Issues
  app.py                               Pipeline orchestration and baseline behavior
  runtime.py                           Production dependency composition
  cli.py                               Command-line entry point
tests/
  fixtures/                            Stable Poki-like XML/HTML samples
  test_config.py                       Configuration validation
  test_urls.py                         URL rules
  test_discovery.py                    Dual-source discovery and degradation
  test_extractor.py                    Page fact parsing and structure checks
  test_state.py                        Idempotency, atomic persistence, rechecks
  test_keywords.py                     Candidate quality and deduplication
  test_signals.py                      Provider mapping and graceful failure
  test_scoring.py                      Score math and confidence
  test_reporting.py                    Files and Issue idempotency
  test_app.py                          End-to-end fixture pipeline
```

### Task 1: Project scaffold and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/poki_seo_monitor/__init__.py`
- Create: `src/poki_seo_monitor/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing configuration tests**

```python
# tests/test_config.py
import pytest
from poki_seo_monitor.config import Config


def test_config_defaults_to_english_us_and_baseline_mode():
    config = Config.from_env({})
    assert config.sitemap_index == "https://poki.com/en/sitemaps/index.xml"
    assert config.new_games_url == "https://poki.com/en/new"
    assert config.geo == "US"
    assert config.max_games_per_run == 10
    assert config.baseline_sample_size == 3


def test_config_rejects_non_positive_request_budget():
    with pytest.raises(ValueError, match="MAX_GAMES_PER_RUN"):
        Config.from_env({"MAX_GAMES_PER_RUN": "0"})
```

- [ ] **Step 2: Run the test and verify the package does not exist**

Run: `python -m pytest tests/test_config.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'poki_seo_monitor'`.

- [ ] **Step 3: Create packaging and minimal configuration**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "poki-seo-monitor"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "beautifulsoup4>=4.13,<5",
  "lxml>=5.3,<7",
  "requests>=2.32,<3",
  "trendspyg==1.1.1",
]

[project.optional-dependencies]
dev = ["pytest>=8.3,<9", "pytest-cov>=6,<8", "responses>=0.25,<1"]

[project.scripts]
poki-seo-monitor = "poki_seo_monitor.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/poki_seo_monitor"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

```gitignore
# .gitignore
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.coverage
htmlcov/
.env
```

```python
# src/poki_seo_monitor/__init__.py
__version__ = "0.1.0"
```

```python
# src/poki_seo_monitor/config.py
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Config:
    sitemap_index: str
    new_games_url: str
    geo: str
    state_path: Path
    reports_dir: Path
    games_path: Path
    keywords_path: Path
    max_games_per_run: int
    baseline_sample_size: int
    github_repository: str | None
    github_token: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        max_games = int(env.get("MAX_GAMES_PER_RUN", "10"))
        if max_games <= 0:
            raise ValueError("MAX_GAMES_PER_RUN must be positive")
        return cls(
            sitemap_index=env.get("POKI_SITEMAP_INDEX", "https://poki.com/en/sitemaps/index.xml"),
            new_games_url=env.get("POKI_NEW_GAMES_URL", "https://poki.com/en/new"),
            geo=env.get("TRENDS_GEO", "US"),
            state_path=Path(env.get("STATE_PATH", "data/state.json")),
            reports_dir=Path(env.get("REPORTS_DIR", "reports")),
            games_path=Path(env.get("GAMES_PATH", "data/games.jsonl")),
            keywords_path=Path(env.get("KEYWORDS_PATH", "data/keywords.csv")),
            max_games_per_run=max_games,
            baseline_sample_size=max(0, int(env.get("BASELINE_SAMPLE_SIZE", "3"))),
            github_repository=env.get("GITHUB_REPOSITORY"),
            github_token=env.get("GITHUB_TOKEN"),
        )
```

- [ ] **Step 4: Run the test and verify it passes**

Run: `python -m pytest tests/test_config.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit the scaffold**

```bash
git add pyproject.toml .gitignore src/poki_seo_monitor/__init__.py src/poki_seo_monitor/config.py tests/test_config.py
git commit -m "chore: scaffold poki seo monitor"
```

### Task 2: Domain models, HTTP policy, and URL normalization

**Files:**
- Create: `src/poki_seo_monitor/models.py`
- Create: `src/poki_seo_monitor/http.py`
- Create: `src/poki_seo_monitor/urls.py`
- Create: `tests/test_urls.py`

- [ ] **Step 1: Write failing URL tests**

```python
# tests/test_urls.py
import pytest
from poki_seo_monitor.urls import canonical_game_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://poki.com/en/g/GoalHeads.io?ref=new", "https://poki.com/en/g/goalheads.io"),
        ("https://www.poki.com/en/g/test-game/", "https://poki.com/en/g/test-game"),
        ("/en/g/test-game", "https://poki.com/en/g/test-game"),
    ],
)
def test_canonical_game_url(raw, expected):
    assert canonical_game_url(raw) == expected


@pytest.mark.parametrize("raw", ["https://poki.com/en/new", "https://evil.test/en/g/x", "/zh/g/x"])
def test_rejects_non_english_game_urls(raw):
    assert canonical_game_url(raw) is None
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python -m pytest tests/test_urls.py -q`

Expected: import fails because `poki_seo_monitor.urls` does not exist.

- [ ] **Step 3: Add the shared records, HTTP client, and canonicalizer**

```python
# src/poki_seo_monitor/models.py
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class DiscoveredGame:
    url: str
    sources: tuple[str, ...]
    source_rank: int | None = None


@dataclass(frozen=True)
class GamePage:
    url: str
    slug: str
    name: str
    title: str
    description: str
    body: str
    categories: tuple[str, ...] = ()
    developer: str | None = None
    related_games: tuple[str, ...] = ()


@dataclass(frozen=True)
class KeywordCandidate:
    phrase: str
    group: Literal["game_name", "category", "long_tail"]
    evidence: tuple[str, ...]
    verified: bool = False


@dataclass(frozen=True)
class SearchSignals:
    trend_7d: float | None = None
    trend_30d: float | None = None
    trend_90d: float | None = None
    rising_queries: tuple[str, ...] = ()
    autocomplete: tuple[str, ...] = ()
    competition: float | None = None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Opportunity:
    keyword: KeywordCandidate
    signals: SearchSignals
    score: int
    confidence: float
    action: str


def to_dict(value: Any) -> dict[str, Any]:
    return asdict(value)
```

```python
# src/poki_seo_monitor/http.py
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "PokiSEOResearchBot/0.1 (+repository contact)"
    retry = Retry(total=3, connect=3, read=3, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_bytes(session: requests.Session, url: str) -> bytes:
    response = session.get(url, timeout=(10, 30))
    response.raise_for_status()
    return response.content
```

```python
# src/poki_seo_monitor/urls.py
from urllib.parse import urljoin, urlsplit, urlunsplit


def canonical_game_url(raw: str) -> str | None:
    parsed = urlsplit(urljoin("https://poki.com", raw.strip()))
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host not in {"poki.com", "www.poki.com"}:
        return None
    path = parsed.path.rstrip("/").lower()
    parts = path.split("/")
    if len(parts) != 4 or parts[1:3] != ["en", "g"] or not parts[3]:
        return None
    return urlunsplit(("https", "poki.com", path, "", ""))
```

- [ ] **Step 4: Run URL tests**

Run: `python -m pytest tests/test_urls.py -q`

Expected: all parameterized cases pass.

- [ ] **Step 5: Commit domain primitives**

```bash
git add src/poki_seo_monitor/models.py src/poki_seo_monitor/http.py src/poki_seo_monitor/urls.py tests/test_urls.py
git commit -m "feat: add domain models and url normalization"
```

### Task 3: Dual-source discovery

**Files:**
- Create: `src/poki_seo_monitor/discovery.py`
- Create: `tests/fixtures/sitemap-index.xml`
- Create: `tests/fixtures/games-sitemap.xml`
- Create: `tests/fixtures/new-games.html`
- Create: `tests/test_discovery.py`

- [ ] **Step 1: Add minimal Poki-like fixtures and failing tests**

```xml
<!-- tests/fixtures/sitemap-index.xml -->
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://poki.com/en/sitemaps/games-1.xml</loc></sitemap>
</sitemapindex>
```

```xml
<!-- tests/fixtures/games-sitemap.xml -->
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://poki.com/en/g/alpha-game</loc></url>
  <url><loc>https://poki.com/en/new</loc></url>
</urlset>
```

```html
<!-- tests/fixtures/new-games.html -->
<main><a href="/en/g/beta-game">Beta Game</a><a href="/en/g/alpha-game?x=1">Alpha</a></main>
```

```python
# tests/test_discovery.py
from pathlib import Path
from poki_seo_monitor.discovery import parse_new_games, parse_sitemap_index, parse_urlset, merge_discoveries

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_sitemap_index_and_urlset():
    children = parse_sitemap_index((FIXTURES / "sitemap-index.xml").read_bytes())
    games = parse_urlset((FIXTURES / "games-sitemap.xml").read_bytes())
    assert children == ["https://poki.com/en/sitemaps/games-1.xml"]
    assert [game.url for game in games] == ["https://poki.com/en/g/alpha-game"]


def test_merges_sources_and_preserves_new_page_rank():
    sitemap = parse_urlset((FIXTURES / "games-sitemap.xml").read_bytes())
    newest = parse_new_games((FIXTURES / "new-games.html").read_text())
    merged = merge_discoveries(sitemap, newest)
    assert merged[0].url == "https://poki.com/en/g/beta-game"
    alpha = next(item for item in merged if item.url.endswith("alpha-game"))
    assert alpha.sources == ("new_games", "sitemap")
```

- [ ] **Step 2: Run discovery tests and verify failure**

Run: `python -m pytest tests/test_discovery.py -q`

Expected: import fails because `poki_seo_monitor.discovery` does not exist.

- [ ] **Step 3: Implement XML/HTML parsing and merging**

```python
# src/poki_seo_monitor/discovery.py
import gzip
from collections import defaultdict
from xml.etree import ElementTree
from bs4 import BeautifulSoup
from .models import DiscoveredGame
from .urls import canonical_game_url

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _xml(data: bytes) -> ElementTree.Element:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return ElementTree.fromstring(data)


def parse_sitemap_index(data: bytes) -> list[str]:
    return [node.text.strip() for node in _xml(data).findall("sm:sitemap/sm:loc", NS) if node.text]


def parse_urlset(data: bytes) -> list[DiscoveredGame]:
    urls = []
    for node in _xml(data).findall("sm:url/sm:loc", NS):
        canonical = canonical_game_url(node.text or "")
        if canonical:
            urls.append(DiscoveredGame(canonical, ("sitemap",)))
    return urls


def parse_new_games(html: str) -> list[DiscoveredGame]:
    found = []
    seen = set()
    for anchor in BeautifulSoup(html, "lxml").select("a[href]"):
        canonical = canonical_game_url(anchor.get("href", ""))
        if canonical and canonical not in seen:
            seen.add(canonical)
            found.append(DiscoveredGame(canonical, ("new_games",), len(found) + 1))
    return found


def merge_discoveries(*groups: list[DiscoveredGame]) -> list[DiscoveredGame]:
    by_url: dict[str, list[DiscoveredGame]] = defaultdict(list)
    for group in groups:
        for item in group:
            by_url[item.url].append(item)
    merged = []
    for url, items in by_url.items():
        sources = tuple(source for source in ("new_games", "sitemap") if any(source in item.sources for item in items))
        ranks = [item.source_rank for item in items if item.source_rank is not None]
        merged.append(DiscoveredGame(url, sources, min(ranks) if ranks else None))
    return sorted(merged, key=lambda item: (item.source_rank is None, item.source_rank or 10**9, item.url))
```

- [ ] **Step 4: Run discovery tests**

Run: `python -m pytest tests/test_discovery.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit discovery**

```bash
git add src/poki_seo_monitor/discovery.py tests/fixtures tests/test_discovery.py
git commit -m "feat: discover games from sitemap and new page"
```

### Task 4: Game-page extraction

**Files:**
- Create: `src/poki_seo_monitor/extractor.py`
- Create: `tests/fixtures/game-page.html`
- Create: `tests/test_extractor.py`

- [ ] **Step 1: Write fixture and failing extraction tests**

```html
<!-- tests/fixtures/game-page.html -->
<html><head><title>Alpha Game - Play Online | Poki</title><meta name="description" content="Race cars online."></head>
<body><main><h1>Alpha Game</h1><p>Drift a sports car and beat other racers.</p>
<a href="/en/car">Car Games</a><a href="/en/g/beta-game">Beta Game</a>
<p>Developer: Example Studio</p></main></body></html>
```

```python
# tests/test_extractor.py
from pathlib import Path
import pytest
from poki_seo_monitor.extractor import PageStructureError, extract_game_page

HTML = (Path(__file__).parent / "fixtures" / "game-page.html").read_text()


def test_extracts_required_game_facts():
    page = extract_game_page("https://poki.com/en/g/alpha-game", HTML)
    assert page.name == "Alpha Game"
    assert page.slug == "alpha-game"
    assert page.description == "Race cars online."
    assert "Car Games" in page.categories
    assert page.related_games == ("Beta Game",)


def test_rejects_page_without_h1():
    with pytest.raises(PageStructureError, match="missing h1"):
        extract_game_page("https://poki.com/en/g/broken", "<html><title>Broken</title></html>")
```

- [ ] **Step 2: Run extraction tests and verify failure**

Run: `python -m pytest tests/test_extractor.py -q`

Expected: import fails because `poki_seo_monitor.extractor` does not exist.

- [ ] **Step 3: Implement fact extraction with required-field checks**

```python
# src/poki_seo_monitor/extractor.py
import re
from bs4 import BeautifulSoup
from .models import GamePage


class PageStructureError(ValueError):
    pass


def extract_game_page(url: str, html: str) -> GamePage:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.select_one("h1")
    if h1 is None or not h1.get_text(strip=True):
        raise PageStructureError("missing h1")
    main = soup.select_one("main") or soup.body
    if main is None:
        raise PageStructureError("missing body")
    name = h1.get_text(" ", strip=True)
    title = soup.title.get_text(" ", strip=True) if soup.title else name
    meta = soup.select_one('meta[name="description"]')
    description = meta.get("content", "").strip() if meta else ""
    category_names = []
    related_names = []
    for anchor in main.select("a[href]"):
        href = anchor.get("href", "")
        label = anchor.get_text(" ", strip=True)
        if href.startswith("/en/g/") and label and label != name:
            related_names.append(label)
        elif href.startswith("/en/") and "/g/" not in href and label.endswith("Games"):
            category_names.append(label)
    body = " ".join(main.stripped_strings)
    match = re.search(r"Developer:\s*([^.|]+)", body, re.I)
    return GamePage(
        url=url,
        slug=url.rstrip("/").rsplit("/", 1)[-1],
        name=name,
        title=title,
        description=description,
        body=body,
        categories=tuple(dict.fromkeys(category_names)),
        developer=match.group(1).strip() if match else None,
        related_games=tuple(dict.fromkeys(related_names)),
    )
```

- [ ] **Step 4: Run extraction tests**

Run: `python -m pytest tests/test_extractor.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit extraction**

```bash
git add src/poki_seo_monitor/extractor.py tests/fixtures/game-page.html tests/test_extractor.py
git commit -m "feat: extract game page facts"
```

### Task 5: Durable state, baseline, and recheck scheduling

**Files:**
- Create: `src/poki_seo_monitor/state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write failing state tests**

```python
# tests/test_state.py
from datetime import UTC, datetime
from poki_seo_monitor.state import MonitorState


NOW = datetime(2026, 8, 3, tzinfo=UTC)


def test_first_run_builds_baseline_without_new_items(tmp_path):
    path = tmp_path / "state.json"
    state = MonitorState.load(path)
    assert state.diff(["https://poki.com/en/g/a"], NOW, baseline=True) == []
    state.save(path)
    assert MonitorState.load(path).games["https://poki.com/en/g/a"]["status"] == "baseline"


def test_new_url_and_missing_trends_rechecks_are_idempotent(tmp_path):
    path = tmp_path / "state.json"
    state = MonitorState.load(path)
    state.diff(["https://poki.com/en/g/a"], NOW, baseline=True)
    assert state.diff(["https://poki.com/en/g/a", "https://poki.com/en/g/b"], NOW, baseline=False) == ["https://poki.com/en/g/b"]
    state.mark_research("https://poki.com/en/g/b", NOW, trends_missing=True)
    assert len(state.games["https://poki.com/en/g/b"]["recheck_at"]) == 3
    due = state.due_rechecks(datetime(2026, 8, 11, tzinfo=UTC))
    assert due == ["https://poki.com/en/g/b"]
    assert len(state.games["https://poki.com/en/g/b"]["recheck_at"]) == 2


def test_records_one_game_error_without_losing_pending_state():
    state = MonitorState()
    state.diff(["https://poki.com/en/g/b"], NOW, baseline=False)
    state.mark_error("https://poki.com/en/g/b", "page failed", NOW)
    assert state.games["https://poki.com/en/g/b"]["status"] == "retry"
    assert state.games["https://poki.com/en/g/b"]["last_error"] == "page failed"
```

- [ ] **Step 2: Run state tests and verify failure**

Run: `python -m pytest tests/test_state.py -q`

Expected: import fails because `poki_seo_monitor.state` does not exist.

- [ ] **Step 3: Implement versioned state and atomic replace**

```python
# src/poki_seo_monitor/state.py
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class MonitorState:
    schema_version: int = 1
    games: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "MonitorState":
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text())
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported state schema")
        return cls(schema_version=1, games=payload.get("games", {}))

    def diff(self, urls: list[str], now: datetime, baseline: bool) -> list[str]:
        new_urls = []
        for url in urls:
            if url in self.games:
                continue
            self.games[url] = {"first_seen": now.isoformat(), "status": "baseline" if baseline else "pending", "recheck_at": []}
            if not baseline:
                new_urls.append(url)
        return new_urls

    def mark_research(self, url: str, now: datetime, trends_missing: bool) -> None:
        item = self.games[url]
        item["status"] = "researched"
        item["researched_at"] = now.isoformat()
        if trends_missing and not item.get("recheck_at"):
            item["recheck_at"] = [(now + timedelta(days=days)).isoformat() for days in (7, 14, 30)]
        elif not trends_missing:
            item["recheck_at"] = []

    def due_rechecks(self, now: datetime) -> list[str]:
        due = []
        for url, item in self.games.items():
            scheduled = item.get("recheck_at", [])
            expired = [value for value in scheduled if datetime.fromisoformat(value) <= now]
            if expired:
                due.append(url)
                item["recheck_at"] = [value for value in scheduled if value not in expired]
        return due

    def mark_error(self, url: str, message: str, now: datetime) -> None:
        self.games[url]["status"] = "retry"
        self.games[url]["last_error"] = message
        retry_at = (now + timedelta(days=1)).isoformat()
        self.games[url]["recheck_at"] = list(dict.fromkeys([retry_at, *self.games[url].get("recheck_at", [])]))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps({"schema_version": self.schema_version, "games": self.games}, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
```

- [ ] **Step 4: Run state tests**

Run: `python -m pytest tests/test_state.py -q`

Expected: `3 passed`.

- [ ] **Step 5: Commit state management**

```bash
git add src/poki_seo_monitor/state.py tests/test_state.py
git commit -m "feat: persist discovery state and rechecks"
```

### Task 6: Three-group keyword generation

**Files:**
- Create: `src/poki_seo_monitor/keywords.py`
- Create: `tests/test_keywords.py`

- [ ] **Step 1: Write failing keyword tests**

```python
# tests/test_keywords.py
from poki_seo_monitor.keywords import generate_keywords
from poki_seo_monitor.models import GamePage


PAGE = GamePage(
    url="https://poki.com/en/g/alpha-game", slug="alpha-game", name="Alpha Game",
    title="Alpha Game - Play Online", description="Race cars online.",
    body="Drift a sports car and beat other racers with arrow key controls.",
    categories=("Car Games", "Racing Games"), related_games=("Beta Racer",),
)


def test_generates_three_evidence_backed_groups_without_duplicates():
    candidates = generate_keywords(PAGE)
    phrases = [item.phrase for item in candidates]
    assert "alpha game" in phrases
    assert "play alpha game online" in phrases
    assert "online car games" in phrases
    assert "how to play alpha game" in phrases
    assert len(phrases) == len(set(phrases))
    assert {item.group for item in candidates} == {"game_name", "category", "long_tail"}
```

- [ ] **Step 2: Run keyword tests and verify failure**

Run: `python -m pytest tests/test_keywords.py -q`

Expected: import fails because `poki_seo_monitor.keywords` does not exist.

- [ ] **Step 3: Implement conservative candidate generation**

```python
# src/poki_seo_monitor/keywords.py
import re
from .models import GamePage, KeywordCandidate


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def generate_keywords(page: GamePage) -> list[KeywordCandidate]:
    name = _clean(page.name)
    values = [
        KeywordCandidate(name, "game_name", ("h1",)),
        KeywordCandidate(f"play {name} online", "game_name", ("h1", "online intent")),
        KeywordCandidate(f"{name} free", "game_name", ("h1", "free intent")),
    ]
    for category in page.categories:
        category_name = _clean(category)
        values.append(KeywordCandidate(f"online {category_name}", "category", (f"category:{category}",)))
    long_tails = [
        (f"how to play {name}", "game instructions"),
        (f"{name} controls", "page controls"),
        (f"games like {name}", "related games"),
        (f"{name} unblocked", "access intent"),
    ]
    values.extend(KeywordCandidate(phrase, "long_tail", (reason,)) for phrase, reason in long_tails)
    unique = {}
    for item in values:
        unique.setdefault(item.phrase, item)
    return list(unique.values())
```

- [ ] **Step 4: Run keyword tests**

Run: `python -m pytest tests/test_keywords.py -q`

Expected: `1 passed`.

- [ ] **Step 5: Commit keyword generation**

```bash
git add src/poki_seo_monitor/keywords.py tests/test_keywords.py
git commit -m "feat: generate seo keyword candidates"
```

### Task 7: Replaceable search signal providers

**Files:**
- Create: `src/poki_seo_monitor/signals.py`
- Create: `tests/test_signals.py`

- [ ] **Step 1: Write failing provider tests with injected network functions**

```python
# tests/test_signals.py
from poki_seo_monitor.signals import AutocompleteProvider, GoogleTrendsProvider, SerpCompetitionProvider, collect_signals


def test_autocomplete_filters_to_matching_unique_phrases():
    provider = AutocompleteProvider(lambda _: ["alpha game", ["Alpha Game Controls", "other", "alpha game controls"]])
    assert provider.suggestions("alpha game") == ("alpha game controls",)


def test_trends_maps_points_and_breakout_queries():
    envelope = {
        "interest_over_time": [{"value": 10}, {"value": 20}, {"value": 50}],
        "related_queries": {"rising": [{"query": "alpha tips", "formatted_value": "Breakout"}]},
    }
    signal = GoogleTrendsProvider(lambda *args, **kwargs: envelope).research("alpha game", "US")
    assert signal.trend_7d == 50
    assert signal.trend_30d == 20
    assert signal.rising_queries == ("alpha tips",)


def test_collection_degrades_when_trends_fails():
    trends = GoogleTrendsProvider(lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("limited")))
    autocomplete = AutocompleteProvider(lambda _: ["alpha", ["alpha controls"]])
    signals = collect_signals("alpha", "US", trends, autocomplete)
    assert signals.autocomplete == ("alpha controls",)
    assert signals.errors == ("trends:limited",)


def test_serp_competition_is_share_of_strong_game_domains():
    provider = SerpCompetitionProvider(lambda _: ["poki.com", "crazygames.com", "example.org", "itch.io"])
    assert provider.competition("alpha game") == 0.75
```

- [ ] **Step 2: Run signal tests and verify failure**

Run: `python -m pytest tests/test_signals.py -q`

Expected: import fails because `poki_seo_monitor.signals` does not exist.

- [ ] **Step 3: Implement provider adapters and bounded failure behavior**

```python
# src/poki_seo_monitor/signals.py
from collections.abc import Callable
import requests
from .models import SearchSignals


class AutocompleteProvider:
    def __init__(self, fetch: Callable[[str], list] | None = None):
        self.fetch = fetch or self._fetch

    @staticmethod
    def _fetch(keyword: str) -> list:
        response = requests.get("https://suggestqueries.google.com/complete/search", params={"client": "firefox", "q": keyword, "hl": "en"}, timeout=(10, 20))
        response.raise_for_status()
        return response.json()

    def suggestions(self, keyword: str) -> tuple[str, ...]:
        payload = self.fetch(keyword)
        needle = keyword.casefold()
        values = [str(item).strip().lower() for item in payload[1]]
        return tuple(dict.fromkeys(item for item in values if needle in item.casefold() and item != needle))


class GoogleTrendsProvider:
    def __init__(self, explore: Callable | None = None):
        if explore is None:
            from trendspyg import download_google_trends_explore
            explore = download_google_trends_explore
        self.explore = explore

    def research(self, keyword: str, geo: str) -> SearchSignals:
        envelope = self.explore(keyword, geo=geo, timeframe="today 3-m")
        points = [float(point["value"]) for point in envelope.get("interest_over_time", [])]
        def window(days: int) -> float | None:
            sample = points[-days:]
            return round(sum(sample) / len(sample), 2) if sample else None
        rising = envelope.get("related_queries", {}).get("rising", [])
        return SearchSignals(
            trend_7d=window(7), trend_30d=window(30), trend_90d=window(90),
            rising_queries=tuple(item["query"].strip().lower() for item in rising if item.get("query")),
        )


class SerpCompetitionProvider:
    STRONG_DOMAINS = {"poki.com", "crazygames.com", "itch.io", "coolmathgames.com", "y8.com"}

    def __init__(self, fetch_hosts: Callable[[str], list[str]] | None = None):
        self.fetch_hosts = fetch_hosts or self._fetch_hosts

    @staticmethod
    def _fetch_hosts(keyword: str) -> list[str]:
        from bs4 import BeautifulSoup
        from urllib.parse import urlsplit
        response = requests.get("https://html.duckduckgo.com/html/", params={"q": keyword, "kl": "us-en"}, timeout=(10, 25))
        response.raise_for_status()
        return [urlsplit(anchor.get("href", "")).hostname or "" for anchor in BeautifulSoup(response.text, "lxml").select("a.result__a")[:10]]

    def competition(self, keyword: str) -> float | None:
        hosts = [host.removeprefix("www.") for host in self.fetch_hosts(keyword) if host]
        if not hosts:
            return None
        strong = sum(host in self.STRONG_DOMAINS for host in hosts)
        return round(strong / len(hosts), 2)


def collect_signals(keyword: str, geo: str, trends: GoogleTrendsProvider, autocomplete: AutocompleteProvider, include_trends: bool = True, serp: SerpCompetitionProvider | None = None, include_serp: bool = False) -> SearchSignals:
    errors = []
    try:
        trend = trends.research(keyword, geo) if include_trends else SearchSignals()
    except Exception as exc:
        trend = SearchSignals()
        errors.append(f"trends:{exc}")
    try:
        suggestions = autocomplete.suggestions(keyword)
    except Exception as exc:
        suggestions = ()
        errors.append(f"autocomplete:{exc}")
    try:
        competition = serp.competition(keyword) if include_serp and serp else trend.competition
    except Exception as exc:
        competition = None
        errors.append(f"serp:{exc}")
    return SearchSignals(
        trend_7d=trend.trend_7d, trend_30d=trend.trend_30d, trend_90d=trend.trend_90d,
        rising_queries=trend.rising_queries, autocomplete=suggestions,
        competition=competition, errors=tuple(errors),
    )
```

- [ ] **Step 4: Run signal tests**

Run: `python -m pytest tests/test_signals.py -q`

Expected: `4 passed`.

- [ ] **Step 5: Commit signal providers**

```bash
git add src/poki_seo_monitor/signals.py tests/test_signals.py
git commit -m "feat: collect trends and autocomplete signals"
```

### Task 8: Opportunity scoring and confidence

**Files:**
- Create: `src/poki_seo_monitor/scoring.py`
- Create: `tests/test_scoring.py`

- [ ] **Step 1: Write failing score tests**

```python
# tests/test_scoring.py
from poki_seo_monitor.models import KeywordCandidate, SearchSignals
from poki_seo_monitor.scoring import score_opportunity


KEYWORD = KeywordCandidate("alpha game", "game_name", ("h1",), verified=True)


def test_complete_fast_rising_signal_is_high_priority():
    result = score_opportunity(KEYWORD, SearchSignals(80, 60, 30, ("alpha tips",), ("alpha game controls",), 0.2), freshness=1.0, expansion=0.8)
    assert result.score >= 75
    assert result.confidence == 1.0
    assert result.action == "immediate"


def test_missing_trends_reduces_confidence_not_score_to_zero():
    result = score_opportunity(KEYWORD, SearchSignals(autocomplete=("alpha controls",), errors=("trends:limited",)), freshness=1.0, expansion=0.5)
    assert result.score > 0
    assert result.confidence < 0.7
    assert result.action in {"watch", "hold"}
```

- [ ] **Step 2: Run score tests and verify failure**

Run: `python -m pytest tests/test_scoring.py -q`

Expected: import fails because `poki_seo_monitor.scoring` does not exist.

- [ ] **Step 3: Implement the documented weighted score**

```python
# src/poki_seo_monitor/scoring.py
from .models import KeywordCandidate, Opportunity, SearchSignals


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_opportunity(keyword: KeywordCandidate, signals: SearchSignals, freshness: float, expansion: float) -> Opportunity:
    if signals.trend_7d is None:
        trend = 0.35
    else:
        base = signals.trend_7d / 100
        growth = 0.0 if signals.trend_90d in (None, 0) else (signals.trend_7d - signals.trend_90d) / 100
        trend = _clamp(base + max(0.0, growth))
    competition_gap = 0.5 if signals.competition is None else 1.0 - _clamp(signals.competition)
    intent = 1.0 if (signals.autocomplete or signals.rising_queries) else 0.25
    score = round(30 * trend + 25 * _clamp(freshness) + 20 * competition_gap + 15 * intent + 10 * _clamp(expansion))
    observed = sum(value is not None for value in (signals.trend_7d, signals.trend_30d, signals.trend_90d, signals.competition))
    confidence = round(_clamp((observed + bool(signals.autocomplete) + bool(signals.rising_queries)) / 6), 2)
    action = "immediate" if score >= 75 else "watch" if score >= 55 else "hold" if score >= 35 else "ignore"
    return Opportunity(keyword, signals, score, confidence, action)
```

- [ ] **Step 4: Run score tests**

Run: `python -m pytest tests/test_scoring.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit scoring**

```bash
git add src/poki_seo_monitor/scoring.py tests/test_scoring.py
git commit -m "feat: score seo trend opportunities"
```

### Task 9: Reports, structured outputs, and GitHub Issues

**Files:**
- Create: `src/poki_seo_monitor/reporting.py`
- Create: `tests/test_reporting.py`

- [ ] **Step 1: Write failing output and idempotency tests**

```python
# tests/test_reporting.py
from poki_seo_monitor.models import GamePage, KeywordCandidate, Opportunity, SearchSignals
from poki_seo_monitor.reporting import Reporter


def test_writes_markdown_jsonl_csv_and_only_posts_issue_once(tmp_path):
    calls = []
    reporter = Reporter(tmp_path / "reports", tmp_path / "games.jsonl", tmp_path / "keywords.csv", issue_post=lambda title, body, marker: calls.append((title, marker)) or 42)
    page = GamePage("https://poki.com/en/g/a", "a", "A", "A", "desc", "body")
    opportunity = Opportunity(KeywordCandidate("a", "game_name", ("h1",)), SearchSignals(), 80, 0.5, "immediate")
    first = reporter.publish(page, [opportunity], "2026-08-03", allow_issue=True)
    second = reporter.publish(page, [opportunity], "2026-08-03", allow_issue=True)
    assert first.issue_number == 42
    assert second.issue_number == 42
    assert len(calls) == 1
    assert (tmp_path / "reports/2026-08-03/a.md").exists()
    assert len((tmp_path / "games.jsonl").read_text().splitlines()) == 1
    assert len((tmp_path / "keywords.csv").read_text().splitlines()) == 2
```

- [ ] **Step 2: Run reporting tests and verify failure**

Run: `python -m pytest tests/test_reporting.py -q`

Expected: import fails because `poki_seo_monitor.reporting` does not exist.

- [ ] **Step 3: Implement atomic reports and stable Issue markers**

```python
# src/poki_seo_monitor/reporting.py
import csv
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Callable
from .models import GamePage, Opportunity


@dataclass(frozen=True)
class PublishResult:
    report_path: Path
    issue_number: int | None


class Reporter:
    def __init__(self, reports_dir: Path, games_path: Path, keywords_path: Path, issue_post: Callable[[str, str, str], int] | None = None):
        self.reports_dir = reports_dir
        self.games_path = games_path
        self.keywords_path = keywords_path
        self.issue_post = issue_post
        self.issue_cache: dict[str, int] = {}

    def publish(self, page: GamePage, opportunities: list[Opportunity], date: str, allow_issue: bool) -> PublishResult:
        marker = hashlib.sha256(page.url.encode()).hexdigest()[:16]
        report = self.reports_dir / date / f"{page.slug}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(f"| {item.keyword.phrase} | {item.keyword.group} | {item.score} | {item.confidence:.2f} | {item.action} |" for item in opportunities)
        body = f"# {page.name}\n\nSource: {page.url}\n\n<!-- poki-seo:{marker} -->\n\n| Keyword | Group | Score | Confidence | Action |\n|---|---|---:|---:|---|\n{rows}\n"
        report.write_text(body)
        self.games_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.games_path.read_text().splitlines() if self.games_path.exists() else []
        record = json.dumps({"page": asdict(page), "opportunities": [asdict(item) for item in opportunities]}, sort_keys=True)
        if record not in existing:
            with self.games_path.open("a") as handle:
                handle.write(record + "\n")
        self.keywords_path.parent.mkdir(parents=True, exist_ok=True)
        existing_csv = self.keywords_path.read_text().splitlines() if self.keywords_path.exists() else []
        retained = [line for line in existing_csv[1:] if not line.startswith(f'"{page.url}",') and not line.startswith(f"{page.url},")]
        with self.keywords_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["game_url", "keyword", "group", "score", "confidence", "action"])
            for line in retained:
                handle.write(line + "\n")
            for item in opportunities:
                writer.writerow([page.url, item.keyword.phrase, item.keyword.group, item.score, item.confidence, item.action])
        issue = self.issue_cache.get(marker)
        trends_missing = opportunities and all(item.signals.trend_7d is None for item in opportunities)
        should_notify = max((item.score for item in opportunities), default=0) >= 55 or trends_missing
        if issue is None and allow_issue and self.issue_post and should_notify:
            issue = self.issue_post(f"SEO opportunity: {page.name}", body, marker)
            self.issue_cache[marker] = issue
        return PublishResult(report, issue)
```

- [ ] **Step 4: Add the production GitHub REST poster**

```python
# Append to src/poki_seo_monitor/reporting.py
import requests


def github_issue_poster(repository: str, token: str) -> Callable[[str, str, str], int]:
    session = requests.Session()
    session.headers.update({"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2026-03-10"})

    def post(title: str, body: str, marker: str) -> int:
        search = session.get(f"https://api.github.com/search/issues", params={"q": f'repo:{repository} is:issue "poki-seo:{marker}"'}, timeout=(10, 30))
        search.raise_for_status()
        if search.json().get("items"):
            return int(search.json()["items"][0]["number"])
        response = session.post(f"https://api.github.com/repos/{repository}/issues", json={"title": title, "body": body, "labels": ["seo-opportunity"]}, timeout=(10, 30))
        response.raise_for_status()
        return int(response.json()["number"])

    return post
```

- [ ] **Step 5: Run reporting tests**

Run: `python -m pytest tests/test_reporting.py -q`

Expected: `1 passed`.

- [ ] **Step 6: Commit reporting**

```bash
git add src/poki_seo_monitor/reporting.py tests/test_reporting.py
git commit -m "feat: publish seo reports and github issues"
```

### Task 10: Orchestrator and command-line entry point

**Files:**
- Create: `src/poki_seo_monitor/app.py`
- Create: `src/poki_seo_monitor/cli.py`
- Create: `tests/test_app.py`

- [ ] **Step 1: Write a failing fixture-driven pipeline test**

```python
# tests/test_app.py
from datetime import UTC, datetime
from poki_seo_monitor.app import Monitor


def test_first_run_baselines_then_second_run_researches_only_new_url(tmp_path):
    discovered = [["https://poki.com/en/g/a"], ["https://poki.com/en/g/a", "https://poki.com/en/g/b"]]
    researched = []
    monitor = Monitor.for_test(tmp_path, discover=lambda: discovered.pop(0), research=lambda url, notify: researched.append((url, notify)))
    monitor.run(datetime(2026, 8, 3, tzinfo=UTC))
    assert researched == [("https://poki.com/en/g/a", False)]
    monitor.run(datetime(2026, 8, 3, 6, tzinfo=UTC))
    assert researched[-1] == ("https://poki.com/en/g/b", True)


def test_one_research_failure_does_not_stop_remaining_games(tmp_path):
    urls = ["https://poki.com/en/g/a", "https://poki.com/en/g/b"]
    discoveries = [[], urls]
    calls = []
    def research(url, notify):
        calls.append(url)
        if url.endswith("/a"):
            raise RuntimeError("broken page")
        return False
    monitor = Monitor(tmp_path / "state.json", lambda: discoveries.pop(0), research, max_games=10, baseline_sample_size=0)
    monitor.run(datetime(2026, 8, 3, tzinfo=UTC))
    monitor.run(datetime(2026, 8, 3, 6, tzinfo=UTC))
    assert calls == urls
```

- [ ] **Step 2: Run the pipeline test and verify failure**

Run: `python -m pytest tests/test_app.py -q`

Expected: import fails because `poki_seo_monitor.app` does not exist.

- [ ] **Step 3: Implement the idempotent orchestration boundary**

```python
# src/poki_seo_monitor/app.py
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from .state import MonitorState


class Monitor:
    def __init__(self, state_path: Path, discover: Callable[[], list[str]], research: Callable[[str, bool], bool], max_games: int = 10, baseline_sample_size: int = 3):
        self.state_path = state_path
        self.discover = discover
        self.research = research
        self.max_games = max_games
        self.baseline_sample_size = baseline_sample_size

    @classmethod
    def for_test(cls, root: Path, discover: Callable[[], list[str]], research: Callable[[str, bool], bool | None]) -> "Monitor":
        return cls(root / "state.json", discover, lambda url, notify: bool(research(url, notify)), 10, 3)

    def run(self, now: datetime) -> dict[str, int | bool]:
        state = MonitorState.load(self.state_path)
        baseline = not self.state_path.exists()
        discovered = self.discover()
        new_urls = state.diff(discovered, now, baseline)
        due_urls = [] if baseline else state.due_rechecks(now)
        work_urls = (discovered[: self.baseline_sample_size] if baseline else list(dict.fromkeys(new_urls + due_urls)))[: self.max_games]
        completed = 0
        failed = 0
        for url in work_urls:
            try:
                trends_missing = self.research(url, not baseline)
                state.mark_research(url, now, trends_missing=trends_missing)
                completed += 1
            except Exception as exc:
                state.mark_error(url, str(exc), now)
                failed += 1
        state.save(self.state_path)
        return {"baseline": baseline, "discovered": len(discovered), "new": len(new_urls), "completed": completed, "failed": failed}
```

- [ ] **Step 4: Add CLI wiring that composes existing modules**

```python
# src/poki_seo_monitor/cli.py
import json
import os
from datetime import UTC, datetime
from .config import Config


def main() -> int:
    config = Config.from_env(os.environ)
    # Composition is deliberately imported here so unit tests can import the package without Chrome startup.
    from .runtime import build_monitor
    result = build_monitor(config).run(datetime.now(UTC))
    print(json.dumps(result, sort_keys=True))
    return 0
```

- [ ] **Step 5: Create runtime composition with dual-source degradation**

**Files:**
- Create: `src/poki_seo_monitor/runtime.py`

```python
# src/poki_seo_monitor/runtime.py
from datetime import UTC, datetime
from .app import Monitor
from .config import Config
from .discovery import merge_discoveries, parse_new_games, parse_sitemap_index, parse_urlset
from .extractor import extract_game_page
from .http import build_session, get_bytes
from .keywords import generate_keywords
from .reporting import Reporter, github_issue_poster
from .scoring import score_opportunity
from .signals import AutocompleteProvider, GoogleTrendsProvider, SerpCompetitionProvider, collect_signals


def build_monitor(config: Config) -> Monitor:
    session = build_session()

    def discover() -> list[str]:
        groups = []
        errors = []
        try:
            children = parse_sitemap_index(get_bytes(session, config.sitemap_index))
            sitemap_games = []
            for child in children:
                sitemap_games.extend(parse_urlset(get_bytes(session, child)))
            groups.append(sitemap_games)
        except Exception as exc:
            errors.append(f"sitemap:{exc}")
        try:
            groups.append(parse_new_games(get_bytes(session, config.new_games_url).decode("utf-8", "replace")))
        except Exception as exc:
            errors.append(f"new_games:{exc}")
        if not groups:
            raise RuntimeError("both discovery sources failed: " + "; ".join(errors))
        return [item.url for item in merge_discoveries(*groups)]

    poster = github_issue_poster(config.github_repository, config.github_token) if config.github_repository and config.github_token else None
    reporter = Reporter(config.reports_dir, config.games_path, config.keywords_path, poster)
    trends = GoogleTrendsProvider()
    autocomplete = AutocompleteProvider()
    serp = SerpCompetitionProvider()

    def research(url: str, notify: bool) -> bool:
        page = extract_game_page(url, get_bytes(session, url).decode("utf-8", "replace"))
        candidates = generate_keywords(page)
        opportunities = []
        for index, candidate in enumerate(candidates):
            signals = collect_signals(candidate.phrase, config.geo, trends, autocomplete, include_trends=index < 3, serp=serp, include_serp=index == 0)
            verified = bool(signals.autocomplete or signals.rising_queries)
            candidate = type(candidate)(candidate.phrase, candidate.group, candidate.evidence, verified)
            opportunities.append(score_opportunity(candidate, signals, freshness=1.0, expansion=min(1.0, len(candidates) / 12)))
        reporter.publish(page, opportunities, datetime.now(UTC).date().isoformat(), allow_issue=notify)
        return all(item.signals.trend_7d is None for item in opportunities[:3])

    return Monitor(config.state_path, discover, research, config.max_games_per_run, config.baseline_sample_size)
```

- [ ] **Step 6: Run orchestrator and full unit suite**

Run: `python -m pytest -q`

Expected: all tests pass and no live HTTP request occurs.

- [ ] **Step 7: Commit the application pipeline**

```bash
git add src/poki_seo_monitor/app.py src/poki_seo_monitor/cli.py src/poki_seo_monitor/runtime.py tests/test_app.py
git commit -m "feat: orchestrate poki seo monitoring pipeline"
```

### Task 11: GitHub Actions automation

**Files:**
- Create: `.github/workflows/monitor.yml`
- Create: `tests/test_workflow.py`

- [ ] **Step 1: Write a failing workflow contract test**

```python
# tests/test_workflow.py
from pathlib import Path


def test_workflow_has_schedule_manual_trigger_permissions_and_concurrency():
    workflow = Path(".github/workflows/monitor.yml").read_text()
    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "contents: write" in workflow
    assert "issues: write" in workflow
    assert "concurrency:" in workflow
    assert "poki-seo-monitor" in workflow
```

- [ ] **Step 2: Run workflow test and verify failure**

Run: `python -m pytest tests/test_workflow.py -q`

Expected: `FileNotFoundError` for `.github/workflows/monitor.yml`.

- [ ] **Step 3: Create the scheduled workflow**

```yaml
# .github/workflows/monitor.yml
name: Poki SEO Monitor

on:
  schedule:
    - cron: "17 */6 * * *"
  workflow_dispatch:

permissions:
  contents: write
  issues: write

concurrency:
  group: poki-seo-monitor
  cancel-in-progress: false

jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip
      - run: python -m pip install -e '.[dev]'
      - run: python -m pytest -q
      - name: Monitor Poki
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          MAX_GAMES_PER_RUN: "10"
          TRENDS_GEO: US
        run: poki-seo-monitor
      - name: Commit changed state and reports
        run: |
          if [ -z "$(git status --porcelain -- data reports)" ]; then
            exit 0
          fi
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data reports
          git commit -m "data: update Poki SEO opportunities"
          git push
```

- [ ] **Step 4: Run workflow contract and full tests**

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit automation**

```bash
git add .github/workflows/monitor.yml tests/test_workflow.py
git commit -m "ci: schedule poki seo monitor"
```

### Task 12: Documentation, live smoke mode, and final verification

**Files:**
- Create: `README.md`
- Modify: `src/poki_seo_monitor/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Add a failing CLI dry-run test**

```python
# tests/test_cli.py
from poki_seo_monitor.cli import parse_args


def test_parse_args_supports_dry_run():
    assert parse_args(["--dry-run"]).dry_run is True
```

- [ ] **Step 2: Run the test and verify failure**

Run: `python -m pytest tests/test_cli.py -q`

Expected: import fails because `parse_args` does not exist.

- [ ] **Step 3: Add dry-run argument and suppress Issue posting in runtime**

```python
# Replace src/poki_seo_monitor/cli.py with:
import argparse
import json
import os
from datetime import UTC, datetime
from .config import Config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Poki for new SEO opportunities")
    parser.add_argument("--dry-run", action="store_true", help="run without creating GitHub Issues")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = dict(os.environ)
    if args.dry_run:
        env.pop("GITHUB_TOKEN", None)
    config = Config.from_env(env)
    from .runtime import build_monitor
    result = build_monitor(config).run(datetime.now(UTC))
    print(json.dumps(result, sort_keys=True))
    return 0
```

- [ ] **Step 4: Write operator documentation with explicit Trends limitation**

```markdown
# Poki SEO Monitor

Monitors Poki's English sitemap and New Games page, researches newly discovered games for US-first SEO opportunities, stores JSONL/CSV/Markdown, and opens GitHub Issues for scores of 55 or higher.

## Local setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/poki-seo-monitor --dry-run
```

The first run establishes a URL baseline and does not notify on all historical games. Delete `data/state.json` only when intentionally rebuilding that baseline.

## GitHub setup

Enable repository Issues and allow GitHub Actions to read and write repository contents. The workflow runs at minute 17 every six hours and can also be started manually. It uses the repository `GITHUB_TOKEN`; no personal access token is required.

## Google Trends limitation

Google's official Trends API is limited Alpha access. This project initially uses `trendspyg` against the public Trends experience. That adapter is slow and upstream-sensitive, so failures lower confidence and schedule a recheck instead of failing the full run. Once official API access is available, replace only the Trends provider.

## Outputs

- `data/state.json`: URL state and recheck schedule
- `data/games.jsonl`: immutable research records
- `data/keywords.csv`: keyword-level table
- `reports/YYYY-MM-DD/<slug>.md`: human-readable evidence
```

- [ ] **Step 5: Run formatting-independent verification**

Run: `python -m pytest --cov=poki_seo_monitor --cov-report=term-missing -q`

Expected: all tests pass; coverage includes every package module and is at least 85%.

- [ ] **Step 6: Run the safe live baseline smoke test**

Run: `STATE_PATH=/tmp/poki-seo-smoke-state.json REPORTS_DIR=/tmp/poki-seo-smoke-reports GAMES_PATH=/tmp/poki-seo-smoke-games.jsonl KEYWORDS_PATH=/tmp/poki-seo-smoke-keywords.csv poki-seo-monitor --dry-run`

Expected: prints JSON containing `"baseline": true`; creates only temporary baseline/output files; does not create a GitHub Issue.

- [ ] **Step 7: Inspect generated data and validate idempotency**

Run: `STATE_PATH=/tmp/poki-seo-smoke-state.json REPORTS_DIR=/tmp/poki-seo-smoke-reports GAMES_PATH=/tmp/poki-seo-smoke-games.jsonl KEYWORDS_PATH=/tmp/poki-seo-smoke-keywords.csv poki-seo-monitor --dry-run`

Expected: prints JSON containing `"baseline": false` and does not research existing baseline URLs as new games.

- [ ] **Step 8: Commit documentation and release-ready state**

```bash
git add README.md src/poki_seo_monitor/cli.py tests/test_cli.py
git commit -m "docs: document monitor operation and limitations"
```

## Implementation notes from current primary documentation

- Google Trends' official API remains a limited Alpha program; it offers consistently scaled data but cannot be assumed available without accepted access. The first implementation therefore treats the third-party Trends adapter as optional and degraded on failure.
- `trendspyg` 1.1.1 documents `download_google_trends_explore(keyword, geo="US", timeframe="today 12-m")` and warns that Explore uses Chrome and is rate-limit sensitive. Keep the per-run keyword budget conservative.
- GitHub Actions workflows live under `.github/workflows`, support both `schedule` and `workflow_dispatch`, and `concurrency` limits overlapping runs.
- Creating Issues requires `issues: write`; committing state requires `contents: write`. The Issue REST request uses the current documented API version header `2026-03-10`.
