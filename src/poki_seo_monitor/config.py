"""Runtime configuration loaded from environment variables."""

from dataclasses import dataclass, field
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
    github_token: str | None = field(repr=False)

    def __post_init__(self) -> None:
        if self.max_games_per_run <= 0:
            raise ValueError("MAX_GAMES_PER_RUN must be greater than zero")

        object.__setattr__(
            self, "baseline_sample_size", max(0, self.baseline_sample_size)
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        max_games_per_run = int(env.get("MAX_GAMES_PER_RUN", "10"))
        baseline_sample_size = int(env.get("BASELINE_SAMPLE_SIZE", "3"))

        return cls(
            sitemap_index=env.get(
                "SITEMAP_INDEX", "https://poki.com/en/sitemaps/index.xml"
            ),
            new_games_url=env.get("NEW_GAMES_URL", "https://poki.com/en/new"),
            geo=env.get("TRENDS_GEO", env.get("GEO", "US")),
            state_path=Path(env.get("STATE_PATH", "data/state.json")),
            reports_dir=Path(env.get("REPORTS_DIR", "reports")),
            games_path=Path(env.get("GAMES_PATH", "data/games.jsonl")),
            keywords_path=Path(env.get("KEYWORDS_PATH", "data/keywords.csv")),
            max_games_per_run=max_games_per_run,
            baseline_sample_size=baseline_sample_size,
            github_repository=env.get("GITHUB_REPOSITORY") or None,
            github_token=env.get("GITHUB_TOKEN") or None,
        )
