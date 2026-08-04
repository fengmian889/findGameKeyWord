from pathlib import Path

import pytest

from poki_seo_monitor.config import Config


def test_from_env_uses_documented_defaults() -> None:
    config = Config.from_env({})

    assert config.sitemap_index == "https://poki.com/en/sitemaps/index.xml"
    assert config.new_games_url == "https://poki.com/en/new"
    assert config.geo == "US"
    assert config.state_path == Path("data/state.json")
    assert config.reports_dir == Path("reports")
    assert config.games_path == Path("data/games.jsonl")
    assert config.keywords_path == Path("data/keywords.csv")
    assert config.max_games_per_run == 10
    assert config.baseline_sample_size == 3
    assert config.github_repository is None
    assert config.github_token is None


def test_from_env_prefers_trends_geo_over_legacy_geo() -> None:
    config = Config.from_env({"TRENDS_GEO": "CA", "GEO": "GB"})

    assert config.geo == "CA"


def test_from_env_supports_legacy_geo_as_fallback() -> None:
    config = Config.from_env({"GEO": "GB"})

    assert config.geo == "GB"


@pytest.mark.parametrize("value", ["0", "-1"])
def test_from_env_rejects_non_positive_request_budget(value: str) -> None:
    with pytest.raises(ValueError, match="MAX_GAMES_PER_RUN"):
        Config.from_env({"MAX_GAMES_PER_RUN": value})


def test_direct_construction_enforces_request_budget() -> None:
    with pytest.raises(ValueError, match="MAX_GAMES_PER_RUN"):
        Config(
            sitemap_index="https://example.com/sitemap.xml",
            new_games_url="https://example.com/new",
            geo="US",
            state_path=Path("state.json"),
            reports_dir=Path("reports"),
            games_path=Path("games.jsonl"),
            keywords_path=Path("keywords.csv"),
            max_games_per_run=0,
            baseline_sample_size=3,
            github_repository=None,
            github_token=None,
        )


def test_from_env_clamps_negative_baseline_sample_size_to_zero() -> None:
    config = Config.from_env({"BASELINE_SAMPLE_SIZE": "-1"})

    assert config.baseline_sample_size == 0


def test_from_env_normalizes_blank_github_optionals() -> None:
    config = Config.from_env({"GITHUB_REPOSITORY": "", "GITHUB_TOKEN": ""})

    assert config.github_repository is None
    assert config.github_token is None


def test_github_token_is_excluded_from_config_repr() -> None:
    config = Config.from_env({"GITHUB_TOKEN": "secret-token"})

    assert "secret-token" not in repr(config)
