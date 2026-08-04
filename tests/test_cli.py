import json
import os

import pytest

from poki_seo_monitor import cli
from poki_seo_monitor.app import RunSummary


def test_dry_run_removes_token_and_prints_sorted_json(monkeypatch, capsys) -> None:
    seen = []

    class Built:
        def run(self, now):
            return RunSummary(False, 0, 0, 0, 0, 0, 0, False, ())

    monkeypatch.setattr(cli, "build_monitor", lambda config: seen.append(config) or Built())
    result = cli.main(["--dry-run"], {"GITHUB_TOKEN": "secret", "GITHUB_REPOSITORY": "owner/repo"})

    assert result == 0
    assert seen[0].github_token is None
    output = capsys.readouterr().out
    assert "secret" not in output
    assert json.loads(output)["processed"] == 0


def test_dry_run_hides_process_token_from_build_and_run_then_restores_it(
    monkeypatch, capsys
) -> None:
    observed = []

    class Built:
        def run(self, now):
            observed.append(("run", os.environ.get("GITHUB_TOKEN")))
            return RunSummary(False, 0, 0, 0, 0, 0, 0, False, ())

    def build(config):
        observed.append(("build", os.environ.get("GITHUB_TOKEN")))
        return Built()

    monkeypatch.setenv("GITHUB_TOKEN", "process-secret")
    monkeypatch.setattr(cli, "build_monitor", build)

    assert cli.main(["--dry-run"]) == 0

    assert observed == [("build", None), ("run", None)]
    assert os.environ["GITHUB_TOKEN"] == "process-secret"
    assert "process-secret" not in capsys.readouterr().out


def test_dry_run_help_discloses_live_requests_and_artifact_writes(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.parse_args(["--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "live network requests" in help_text
    assert "writes state and artifacts" in help_text


def test_failure_is_sanitized_json_on_stderr(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "build_monitor", lambda config: (_ for _ in ()).throw(RuntimeError("token=secret\nboom")))

    assert cli.main([], {"GITHUB_TOKEN": "secret"}) == 1
    captured = capsys.readouterr()
    assert "secret" not in captured.err
    assert json.loads(captured.err)["error"]


def test_failure_never_prints_bare_configured_token(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "build_monitor",
        lambda config: (_ for _ in ()).throw(RuntimeError(config.github_token)),
    )

    assert cli.main([], {"GITHUB_TOKEN": "uniquely-sensitive-value"}) == 1
    assert "uniquely-sensitive-value" not in capsys.readouterr().err
