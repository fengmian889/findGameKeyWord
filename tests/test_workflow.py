from pathlib import Path
import re


WORKFLOW_PATH = Path(".github/workflows/monitor.yml")
LOCK_PATH = Path("requirements.lock")
PYPROJECT_PATH = Path("pyproject.toml")
CHECKOUT_SHA = "93cb6efe18208431cddfb8368fd83d5badbf9bfd"
SETUP_PYTHON_SHA = "a309ff8b426b58ec0e2a45f0f869d46889d02405"


def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_has_schedule_manual_trigger_permissions_and_concurrency():
    workflow = workflow_text()

    assert "schedule:" in workflow
    assert 'cron: "17 */6 * * *"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert re.search(
        r"\n  monitor:\n(?:.*\n)*?    needs: test\n(?:.*\n)*?    permissions:\n"
        r"      contents: write\n      issues: write\n",
        workflow,
    )
    assert "concurrency:" in workflow
    assert "group: poki-seo-monitor" in workflow
    assert "cancel-in-progress: false" in workflow


def test_workflow_uses_python_312_and_runs_install_tests_and_monitor():
    workflow = workflow_text()

    assert workflow.count(f"actions/checkout@{CHECKOUT_SHA} # v5.0.1") == 2
    assert workflow.count("persist-credentials: false") == 2
    assert workflow.count(f"actions/setup-python@{SETUP_PYTHON_SHA} # v6.2.0") == 2
    assert 'python-version: "3.12"' in workflow
    assert workflow.count("cache-dependency-path: requirements.lock") == 2
    assert workflow.count("python -m pip install --require-hashes -r requirements.lock") == 2
    assert workflow.count(
        "python -m pip install --no-build-isolation --no-deps ."
    ) == 2
    assert "python -m pytest -q" in workflow
    assert "poki-seo-monitor" in workflow
    assert "TRENDS_GEO: US" in workflow


def test_workflow_passes_builtin_token_through_environment_not_shell_script():
    workflow = workflow_text()
    run_blocks = re.findall(r"(?:^|\n)\s+run:\s*(?:\||>)?\s*\n((?:\s{10,}.+\n?)+)", workflow)

    assert workflow.count("GITHUB_TOKEN: ${{ github.token }}") == 2
    assert "secrets.GITHUB_TOKEN" not in workflow
    assert all("${{" not in block for block in run_blocks)


def test_dependency_lock_contains_hashes():
    lock = LOCK_PATH.read_text(encoding="utf-8")
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")

    assert "--hash=sha256:" in lock
    assert "hatchling==1.28.0" in pyproject
    assert "hatchling==1.28.0 \\\n    --hash=sha256:" in lock
    assert "pytest==" in lock
    assert "trendspyg==1.1.1" in lock


def test_workflow_commits_only_changed_outputs_and_retries_authenticated_push():
    workflow = workflow_text()

    assert "git status --porcelain -- data reports" in workflow
    assert "git add -- data reports" in workflow
    assert "git diff --cached --quiet" in workflow
    assert 'git fetch origin "$GITHUB_REF_NAME"' in workflow
    assert 'git rebase "origin/$GITHUB_REF_NAME"' in workflow
    assert 'git push origin "HEAD:$GITHUB_REF_NAME"' in workflow
    assert "for attempt in 1 2 3; do" in workflow
    assert 'if [ "$attempt" -eq 3 ]; then' in workflow
    assert "x-access-token:%s" in workflow
    assert "http.https://github.com/.extraheader" in workflow
    assert "trap cleanup_auth EXIT" in workflow
    assert "push:" not in workflow
