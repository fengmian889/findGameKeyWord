"""Durable local reports and optional GitHub Issue publication."""

from __future__ import annotations

import csv
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import errno
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Protocol
import uuid
import weakref

try:
    import fcntl
except ImportError:  # pragma: no cover - explicit unsupported-platform path
    fcntl = None  # type: ignore[assignment]

from .http import build_session
from .models import GamePage, Opportunity, to_dict
from .urls import canonical_game_url


_DATE_FORMAT = "%Y-%m-%d"
_SLUG = re.compile(r"[a-z0-9][a-z0-9.-]*\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_CSV_HEADER = [
    "game_url",
    "game_name",
    "keyword",
    "group",
    "verified",
    "score",
    "confidence",
    "action",
    "trend_7d",
    "trend_30d",
    "trend_90d",
]
_ISSUE_BODY_LIMIT = 59_000
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_KEYWORD_GROUPS = {"game_name", "category", "long_tail"}
_ACTIONS = {"immediate", "watch", "hold", "ignore"}
_UNSUPPORTED_DIRECTORY_FSYNC = {
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}
_BEARER_CREDENTIAL = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?P<label>token|api[_-]?key|key|secret|password)\s*(?P<separator>=|:)\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(?P<prefix>https?://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
_ISSUE_MARKER = re.compile(r"[0-9a-f]{16}\Z")
_GITHUB_LOCK_GUARD = threading.Lock()
_GITHUB_POST_LOCKS: weakref.WeakValueDictionary[tuple[str, str], threading.Lock] = weakref.WeakValueDictionary()
_GITHUB_ISSUE_CACHE: OrderedDict[tuple[str, str], int] = OrderedDict()
_GITHUB_COOLDOWNS: dict[str, float] = {}
_github_now = time.time
_PUBLISH_LOCK_GUARD = threading.Lock()
_PUBLISH_THREAD_LOCKS: weakref.WeakValueDictionary[Path, threading.RLock] = weakref.WeakValueDictionary()
_MARKER_POSTED: OrderedDict[tuple[Path, str], None] = OrderedDict()
_MARKER_CACHE_LOCK = threading.RLock()
_MARKER_CACHE_LIMIT = 2048
_JOURNAL_VERSION = 1


@dataclass(frozen=True)
class PublishResult:
    report_path: Path
    issue_number: int | None
    issue_error: str | None
    notification_pending: bool = False


@dataclass(frozen=True)
class ResearchMetadata:
    """Strict provenance and scheduling data for a v2 research record."""

    first_seen: str
    sources: tuple[str, ...]
    source_first_seen: dict[str, str]
    new_games_rank: int | None
    recheck_at: tuple[str, ...]
    recheck_status: str
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "source_first_seen", dict(self.source_first_seen))
        object.__setattr__(self, "recheck_at", tuple(self.recheck_at))
        object.__setattr__(self, "errors", tuple(self.errors))


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class _Session(Protocol):
    def get(self, url: str, **kwargs: Any) -> _Response: ...

    def post(self, url: str, **kwargs: Any) -> _Response: ...


IssuePoster = Callable[[str, str, str], int]


class GitHubRateLimitError(RuntimeError):
    """GitHub declined a request and the repository is in a local cooldown."""


def _valid_date(value: str) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("date must be ISO YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, _DATE_FORMAT)
    except ValueError as error:
        raise ValueError("date must be ISO YYYY-MM-DD") from error
    if parsed.strftime(_DATE_FORMAT) != value:
        raise ValueError("date must be ISO YYYY-MM-DD")
    return value


def _safe_report_path(reports_dir: Path, date: str, slug: str) -> Path:
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or not slug.strip("."):
        raise ValueError("game slug is not safe")
    root = reports_dir.resolve()
    raw_path = root / date / f"{slug}.md"
    if raw_path.is_symlink():
        raise ValueError("reporting target must not be a symlink")
    path = raw_path.resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("report path escapes reports directory") from error
    return path


def _validated_targets(report_path: Path, games_path: Path, keywords_path: Path) -> tuple[Path, Path, Path]:
    raw = (report_path, games_path, keywords_path)
    if any(path.is_symlink() for path in raw):
        raise ValueError("reporting target must not be a symlink")
    resolved = tuple(path.resolve() for path in raw)
    if len(set(resolved)) != 3:
        raise ValueError("report, games, and keywords targets must be distinct")
    for path in resolved:
        if path.exists() and not path.is_file():
            raise ValueError("reporting target must be a regular file destination")
    return resolved


def _lock_root(reports_dir: Path, games_path: Path, keywords_path: Path) -> Path:
    roots = [reports_dir.resolve(), games_path.parent.resolve(), keywords_path.parent.resolve()]
    common = Path(os.path.commonpath([str(root) for root in roots])).resolve()
    if common == common.parent:
        raise ValueError("reporting artifacts do not have a meaningful shared lock root")
    return common


@contextmanager
def _publication_lock(lock_root: Path) -> Iterable[Path]:
    if fcntl is None:
        raise RuntimeError("cross-process reporting locks are unsupported on this platform")
    lock_path = lock_root / ".poki-reporting.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _PUBLISH_LOCK_GUARD:
        thread_lock = _PUBLISH_THREAD_LOCKS.setdefault(lock_path.resolve(), threading.RLock())
    with thread_lock:
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as error:
                raise RuntimeError("could not acquire cross-process reporting lock") from error
            try:
                yield lock_path
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _marker_lock(lock_root: Path, marker: str) -> Iterable[None]:
    if fcntl is None:
        raise RuntimeError("cross-process reporting locks are unsupported on this platform")
    path = lock_root / f".poki-reporting-issue-{marker}.lock"
    with _PUBLISH_LOCK_GUARD:
        thread_lock = _PUBLISH_THREAD_LOCKS.setdefault(path.resolve(), threading.RLock())
    with thread_lock:
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    """Synchronize a directory when the host supports opening it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as error:
        if error.errno in _UNSUPPORTED_DIRECTORY_FSYNC:
            return
        raise
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno not in _UNSUPPORTED_DIRECTORY_FSYNC:
            raise
    finally:
        os.close(fd)


def _stage_write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as temporary:
            temporary_name = Path(temporary.name)
            temporary.write(content.encode("utf-8") if isinstance(content, str) else content)
            temporary.flush()
            os.fsync(temporary.fileno())
        return temporary_name
    except Exception:
        if temporary_name is not None:
            _remove_temporary(temporary_name)
        raise


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _batch_atomic_write(payloads: dict[Path, str]) -> None:
    """Commit a staged set of files or restore every changed target on failure."""
    paths = list(payloads)
    snapshots = [(path, path.exists(), path.read_bytes() if path.exists() else None) for path in paths]
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for path, payload in payloads.items():
            staged[path] = _stage_write(path, payload)
        for path in paths:
            os.replace(staged[path], path)
            replaced.append(path)
        for directory in dict.fromkeys(path.parent for path in paths):
            _fsync_directory(directory)
    except Exception as primary:
        rollback_errors: list[Exception] = []
        snapshot_by_path = {path: (exists, content) for path, exists, content in snapshots}
        for path in reversed(replaced):
            exists, content = snapshot_by_path[path]
            try:
                if exists:
                    assert content is not None
                    restore = _stage_write(path, content)
                    try:
                        os.replace(restore, path)
                    finally:
                        _remove_temporary(restore)
                else:
                    path.unlink()
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            raise ExceptionGroup("report transaction failed and rollback was incomplete", [primary, *rollback_errors])
        raise
    finally:
        for temporary in staged.values():
            _remove_temporary(temporary)


def _atomic_write(path: Path, content: str) -> None:
    """Compatibility wrapper for one-target durable writes."""
    _batch_atomic_write({path: content})


def _transaction_stage(path: Path, content: bytes, transaction_id: str, label: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".poki-reporting-{transaction_id}-{label}-",
            delete=False,
        ) as temporary:
            temporary_name = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        return temporary_name
    except Exception:
        if temporary_name is not None:
            _remove_temporary(temporary_name)
        raise


def _journal_path(games_path: Path) -> Path:
    return games_path.parent / ".poki-reporting.journal"


def _write_journal(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise RuntimeError("reporting journal must not be a symlink")
    staged = _stage_write(path, json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    try:
        os.replace(staged, path)
    finally:
        _remove_temporary(staged)
    _fsync_directory(path.parent)


def _journal_entries(
    journal: Path, games_path: Path, keywords_path: Path, reports_dir: Path
) -> tuple[str, list[dict[str, Any]]]:
    if journal.is_symlink():
        raise RuntimeError("reporting journal must not be a symlink")
    try:
        value = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("reporting journal is corrupt; refusing recovery") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != _JOURNAL_VERSION
        or value.get("state") != "prepared"
        or not isinstance(value.get("transaction_id"), str)
        or not isinstance(value.get("targets"), list)
        or len(value["targets"]) != 3
    ):
        raise RuntimeError("reporting journal has an invalid schema")
    transaction_id = value["transaction_id"]
    entries = value["targets"]
    fixed_targets = {games_path, keywords_path}
    report_target: Path | None = None
    seen: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("existed")) is not bool:
            raise RuntimeError("reporting journal has an invalid target entry")
        try:
            target = Path(entry["target"]).resolve()
            stage = Path(entry["stage"])
        except (KeyError, TypeError, OSError) as error:
            raise RuntimeError("reporting journal has an invalid target path") from error
        if target in seen:
            raise RuntimeError("reporting journal targets do not match this publication")
        if target not in fixed_targets:
            try:
                relative = target.relative_to(reports_dir)
                if len(relative.parts) != 2 or relative.suffix != ".md":
                    raise ValueError
                _valid_date(relative.parts[0])
                slug = relative.stem
                if not _SLUG.fullmatch(slug) or not slug.strip("."):
                    raise ValueError
            except ValueError as error:
                raise RuntimeError("reporting journal targets do not match this configuration") from error
            if report_target is not None:
                raise RuntimeError("reporting journal has multiple report targets")
            report_target = target
        seen.add(target)
        prefix = f".poki-reporting-{transaction_id}-"
        try:
            backup_artifact = Path(entry["backup"]) if entry.get("backup") else None
        except (TypeError, OSError) as error:
            raise RuntimeError("reporting journal has an invalid backup path") from error
        for artifact in (stage, backup_artifact):
            if artifact is None:
                continue
            if artifact.is_symlink() or artifact.parent.resolve() != target.parent or not artifact.name.startswith(prefix):
                raise RuntimeError("reporting journal references an unsafe artifact")
        if entry["existed"] and not isinstance(entry.get("backup"), str):
            raise RuntimeError("reporting journal lacks a required backup")
        if not entry["existed"] and entry.get("backup") is not None:
            raise RuntimeError("reporting journal has an invalid new-target backup")
    if seen != fixed_targets | {report_target} or report_target is None:
        raise RuntimeError("reporting journal targets do not match this publication")
    return transaction_id, entries


def _recover_journal(journal: Path, games_path: Path, keywords_path: Path, reports_dir: Path) -> None:
    if journal.is_symlink():
        raise RuntimeError("reporting journal must not be a symlink")
    if not journal.exists():
        return
    _, entries = _journal_entries(journal, games_path, keywords_path, reports_dir)
    errors: list[Exception] = []
    for entry in reversed(entries):
        target = Path(entry["target"]).resolve()
        try:
            if entry["existed"]:
                backup = Path(entry["backup"])
                if not backup.is_file() or backup.is_symlink():
                    raise RuntimeError("reporting journal backup is unavailable")
                os.replace(backup, target)
            elif target.exists():
                target.unlink()
        except Exception as error:
            errors.append(error)
    target_parents = dict.fromkeys(Path(entry["target"]).resolve().parent for entry in entries)
    for directory in target_parents:
        try:
            _fsync_directory(directory)
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("reporting recovery failed", errors)
    for entry in entries:
        _remove_temporary(Path(entry["stage"]))
        backup = entry.get("backup")
        if backup:
            _remove_temporary(Path(backup))
    journal.unlink()
    _fsync_directory(journal.parent)


def _journaled_batch_write(
    payloads: dict[Path, str], games_path: Path, keywords_path: Path, reports_dir: Path
) -> None:
    paths = tuple(sorted(payloads, key=lambda path: str(path)))
    expected = tuple(paths)
    if len(expected) != 3:
        raise ValueError("reporting transaction requires exactly three targets")
    journal = _journal_path(games_path)
    transaction_id = uuid.uuid4().hex
    entries: list[dict[str, Any]] = []
    try:
        for index, path in enumerate(paths):
            existed = path.exists()
            stage = _transaction_stage(path, payloads[path].encode("utf-8"), transaction_id, f"stage-{index}")
            backup: Path | None = None
            if existed:
                backup = _transaction_stage(path, path.read_bytes(), transaction_id, f"backup-{index}")
            entries.append({"target": str(path), "stage": str(stage), "backup": str(backup) if backup else None, "existed": existed})
        for directory in dict.fromkeys(path.parent for path in paths):
            _fsync_directory(directory)
        _write_journal(journal, {"version": _JOURNAL_VERSION, "transaction_id": transaction_id, "state": "prepared", "targets": entries})
        for entry in entries:
            os.replace(entry["stage"], entry["target"])
        for directory in dict.fromkeys(path.parent for path in paths):
            _fsync_directory(directory)
    except Exception as primary:
        if journal.exists():
            try:
                _recover_journal(journal, games_path, keywords_path, reports_dir)
            except Exception as recovery:
                raise ExceptionGroup("reporting transaction and recovery failed", [primary, recovery])
        else:
            for entry in entries:
                _remove_temporary(Path(entry["stage"]))
                if entry.get("backup"):
                    _remove_temporary(Path(entry["backup"]))
        raise
    journal.unlink()
    _fsync_directory(journal.parent)
    for entry in entries:
        _remove_temporary(Path(entry["stage"]))
        if entry.get("backup"):
            _remove_temporary(Path(entry["backup"]))


def _markdown_cell(value: object) -> str:
    return (
        html.escape(str(value), quote=False)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", "")
        .replace("\n", "<br>")
    )


def _joined(values: Iterable[str]) -> str:
    return ", ".join(_markdown_cell(value) for value in values) or "—"


def _trend(value: float | None) -> str:
    return "—" if value is None else _markdown_cell(value)


def _marker(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _render_report(page: GamePage, opportunities: list[Opportunity], marker: str) -> str:
    lines = [
        f"# {_markdown_cell(page.name)}",
        "",
        f"<!-- poki-seo:{marker} -->",
        "",
        "## Page facts (extracted)",
        "",
        f"- Source URL: {_markdown_cell(page.url)}",
        f"- Title: {_markdown_cell(page.title)}",
        f"- Description: {_markdown_cell(page.description)}",
        f"- Categories: {_joined(page.categories)}",
        f"- Developer: {_markdown_cell(page.developer) if page.developer else '—'}",
        f"- Related games: {_joined(page.related_games)}",
        "",
        "## Generated keyword candidates",
        "",
        "Candidates are generated research leads. Scores and actions are inferences, not page facts.",
        "",
        "| Phrase | Group | Verified | Score | Confidence | Action | Trend 7d | Trend 30d | Trend 90d |",
        "| --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: |",
    ]
    for opportunity in opportunities:
        keyword = opportunity.keyword
        signals = opportunity.signals
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(keyword.phrase),
                    _markdown_cell(keyword.group),
                    "yes" if keyword.verified else "no",
                    _markdown_cell(opportunity.score),
                    _markdown_cell(opportunity.confidence),
                    _markdown_cell(opportunity.action),
                    _trend(signals.trend_7d),
                    _trend(signals.trend_30d),
                    _trend(signals.trend_90d),
                )
            )
            + " |"
        )
    if not opportunities:
        lines.append("| — | — | — | — | — | — | — | — | — |")
    lines.extend(("", "## Verified signals and inferences", ""))
    if not opportunities:
        lines.append("No keyword opportunities were generated.")
    for opportunity in opportunities:
        signals = opportunity.signals
        lines.extend(
            (
                f"### {_markdown_cell(opportunity.keyword.phrase)}",
                "",
                f"- Autocomplete evidence: {_joined(signals.autocomplete)}",
                f"- Rising-query evidence: {_joined(signals.rising_queries)}",
                f"- Provider errors: {_joined(signals.errors)}",
                f"- Inference: score {_markdown_cell(opportunity.score)}, confidence {_markdown_cell(opportunity.confidence)}, action {_markdown_cell(opportunity.action)}.",
                "",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _issue_body(report: str) -> str:
    if len(report) <= _ISSUE_BODY_LIMIT:
        return report
    suffix = "\n\n_Report truncated; see the local Markdown report for all candidates._\n"
    return report[: _ISSUE_BODY_LIMIT - len(suffix)].rstrip() + suffix


def _record(
    page: GamePage,
    opportunities: list[Opportunity],
    date: str,
    report_path: Path,
    issue_status: str,
    metadata: ResearchMetadata | None,
) -> str:
    if metadata is None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "generated_at": date,
            "page": to_dict(page),
            "opportunities": [to_dict(opportunity) for opportunity in opportunities],
        }
    else:
        raw = {
            "schema_version": 2,
            "record_type": "research",
            "generated_at": date,
            "first_seen": metadata.first_seen,
            "discovery": {
                "sources": metadata.sources,
                "source_first_seen": metadata.source_first_seen,
                "new_games_rank": metadata.new_games_rank,
            },
            "research_status": "researched",
            "recheck": {
                "status": metadata.recheck_status,
                "schedule": metadata.recheck_at,
            },
            "report": {"reference": str(report_path)},
            "issue": {
                "status": issue_status,
                "kind": (
                    "high" if opportunities and max(item.score for item in opportunities) >= 75
                    else "summary" if issue_status in {"pending", "not_configured"}
                    else None
                ),
                "marker": _marker(page.url),
                "number": None,
                "error": None,
            },
            "errors": metadata.errors,
            "page": to_dict(page),
            "opportunities": [to_dict(opportunity) for opportunity in opportunities],
        }
    value = _json_value(raw)
    assert isinstance(value, dict)
    _validate_record(value, 0)
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _json_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _read_jsonl(path: Path) -> list[str]:
    if not path.exists():
        return []
    records: list[str] = []
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise ValueError(f"corrupt JSONL: blank line {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"corrupt JSONL at line {line_number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"corrupt JSONL at line {line_number}: expected object")
        _validate_record(value, line_number)
        records.append(
            json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
        )
    return records


def _jsonl_payload(records: list[str], record: str) -> str:
    records = list(records)
    if record not in records:
        records.append(record)
    return "\n".join(records) + "\n"


def _invalid_record(line_number: int, field: str, message: str) -> None:
    raise ValueError(f"corrupt JSONL at line {line_number}: {field} {message}")


def _nonblank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_record(record: dict[str, Any], line_number: int) -> None:
    version = record.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        _invalid_record(line_number, "schema_version", "must be integer 1 or 2")
    if version == 2:
        record_type = record.get("record_type")
        if record_type is None:
            _invalid_record(line_number, "schema_version", "2 requires a record_type")
        if record_type == "issue_outcome":
            _validate_issue_event(record, line_number)
            return
        if record_type != "research":
            _invalid_record(line_number, "record_type", "must be research or issue_outcome")
    try:
        _valid_date(record.get("generated_at"))
    except ValueError:
        _invalid_record(line_number, "generated_at", "must be ISO YYYY-MM-DD")

    page = record.get("page")
    if not isinstance(page, dict):
        _invalid_record(line_number, "page", "must be an object")
    for field in ("url", "slug", "name"):
        if not _nonblank_string(page.get(field)):
            _invalid_record(line_number, f"page.{field}", "must be a nonblank string")
    for field in ("title", "description", "body"):
        if not isinstance(page.get(field), str):
            _invalid_record(line_number, f"page.{field}", "must be a string")
    for field in ("categories", "related_games"):
        if (
            not isinstance(page.get(field), list)
            or not all(isinstance(value, str) for value in page[field])
        ):
            _invalid_record(line_number, f"page.{field}", "must be a list of strings")
    if "developer" not in page or (
        page["developer"] is not None and not isinstance(page["developer"], str)
    ):
        _invalid_record(line_number, "page.developer", "must be a string or null")

    opportunities = record.get("opportunities")
    if not isinstance(opportunities, list):
        _invalid_record(line_number, "opportunities", "must be a list")
    for index, opportunity in enumerate(opportunities):
        prefix = f"opportunities[{index}]"
        if not isinstance(opportunity, dict):
            _invalid_record(line_number, prefix, "must be an object")
        keyword = opportunity.get("keyword")
        if not isinstance(keyword, dict):
            _invalid_record(line_number, f"{prefix}.keyword", "must be an object")
        if not _nonblank_string(keyword.get("phrase")):
            _invalid_record(line_number, f"{prefix}.keyword.phrase", "must be a nonblank string")
        if keyword.get("group") not in _KEYWORD_GROUPS:
            _invalid_record(line_number, f"{prefix}.keyword.group", "must be a supported group")
        if (
            not isinstance(keyword.get("evidence"), list)
            or not all(isinstance(value, str) for value in keyword["evidence"])
        ):
            _invalid_record(line_number, f"{prefix}.keyword.evidence", "must be a list of strings")
        if type(keyword.get("verified")) is not bool:
            _invalid_record(line_number, f"{prefix}.keyword.verified", "must be a boolean")
        signals = opportunity.get("signals")
        if not isinstance(signals, dict):
            _invalid_record(line_number, f"{prefix}.signals", "must be an object")
        for field in ("trend_7d", "trend_30d", "trend_90d"):
            value = signals.get(field)
            if value is not None and not _number_in_range(value, 0, 100):
                _invalid_record(line_number, f"{prefix}.signals.{field}", "must be null or a finite number from 0 to 100")
            if field not in signals:
                _invalid_record(line_number, f"{prefix}.signals.{field}", "is required")
        for field in ("rising_queries", "autocomplete", "errors"):
            if (
                not isinstance(signals.get(field), list)
                or not all(isinstance(value, str) for value in signals[field])
            ):
                _invalid_record(line_number, f"{prefix}.signals.{field}", "must be a list of strings")
        competition = signals.get("competition")
        if "competition" not in signals or (
            competition is not None and not _number_in_range(competition, 0, 1)
        ):
            _invalid_record(line_number, f"{prefix}.signals.competition", "must be null or a finite number from 0 to 1")
        for field in ("autocomplete_observed", "rising_queries_observed"):
            if type(signals.get(field)) is not bool:
                _invalid_record(line_number, f"{prefix}.signals.{field}", "must be a boolean")
        score = opportunity.get("score")
        if type(score) is not int or not 0 <= score <= 100:
            _invalid_record(line_number, f"{prefix}.score", "must be an integer from 0 to 100")
        confidence = opportunity.get("confidence")
        if not _number_in_range(confidence, 0, 1):
            _invalid_record(line_number, f"{prefix}.confidence", "must be a finite number from 0 to 1")
        if opportunity.get("action") not in _ACTIONS:
            _invalid_record(line_number, f"{prefix}.action", "must be a supported action")
    if "fingerprint" in record and not _nonblank_string(record["fingerprint"]):
        _invalid_record(line_number, "fingerprint", "must be a nonblank string")
    if version == 2:
        _validate_v2_research(record, line_number)


def _aware_iso(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _validate_v2_research(record: dict[str, Any], line_number: int) -> None:
    if not _aware_iso(record.get("first_seen")):
        _invalid_record(line_number, "first_seen", "must be a timezone-aware ISO timestamp")
    discovery = record.get("discovery")
    if not isinstance(discovery, dict):
        _invalid_record(line_number, "discovery", "must be an object")
    sources = discovery.get("sources")
    if (
        not isinstance(sources, list)
        or not sources
        or not all(_nonblank_string(source) for source in sources)
        or len(sources) != len(set(sources))
    ):
        _invalid_record(line_number, "discovery.sources", "must be a unique nonempty list")
    source_first_seen = discovery.get("source_first_seen")
    if (
        not isinstance(source_first_seen, dict)
        or set(source_first_seen) != set(sources)
        or not all(_aware_iso(value) for value in source_first_seen.values())
    ):
        _invalid_record(line_number, "discovery.source_first_seen", "must match sources with aware timestamps")
    rank = discovery.get("new_games_rank")
    if rank is not None and (type(rank) is not int or rank <= 0):
        _invalid_record(line_number, "discovery.new_games_rank", "must be a positive integer or null")
    if record.get("research_status") != "researched":
        _invalid_record(line_number, "research_status", "must be researched")
    recheck = record.get("recheck")
    if not isinstance(recheck, dict) or recheck.get("status") not in {
        "scheduled", "not_required", "complete"
    }:
        _invalid_record(line_number, "recheck.status", "must be scheduled, not_required, or complete")
    schedule = recheck.get("schedule")
    if not isinstance(schedule, list) or not all(_aware_iso(value) for value in schedule):
        _invalid_record(line_number, "recheck.schedule", "must be a list of aware timestamps")
    if recheck["status"] == "scheduled" and not schedule:
        _invalid_record(line_number, "recheck.schedule", "must be nonempty when scheduled")
    if recheck["status"] != "scheduled" and schedule:
        _invalid_record(line_number, "recheck.schedule", "must be empty unless scheduled")
    report = record.get("report")
    if not isinstance(report, dict) or not _nonblank_string(report.get("reference")):
        _invalid_record(line_number, "report.reference", "must be a nonblank string")
    issue = record.get("issue")
    if not isinstance(issue, dict) or issue.get("status") not in {
        "baseline", "pending", "not_configured", "not_qualified"
    }:
        _invalid_record(line_number, "issue.status", "must be a supported initial outcome")
    if issue.get("kind") not in {None, "high", "summary"}:
        _invalid_record(line_number, "issue.kind", "must be high, summary, or null")
    if not isinstance(issue.get("marker"), str) or not _ISSUE_MARKER.fullmatch(issue["marker"]):
        _invalid_record(line_number, "issue.marker", "must be a stable marker")
    if issue.get("number") is not None or issue.get("error") is not None:
        _invalid_record(line_number, "issue", "initial number and error must be null")
    errors = record.get("errors")
    if not isinstance(errors, list) or not all(isinstance(error, str) for error in errors):
        _invalid_record(line_number, "errors", "must be a list of strings")


def _validate_issue_event(record: dict[str, Any], line_number: int) -> None:
    try:
        _valid_date(record.get("generated_at"))
    except ValueError:
        _invalid_record(line_number, "generated_at", "must be ISO YYYY-MM-DD")
    urls = record.get("game_urls")
    if not isinstance(urls, list) or not urls or not all(
        isinstance(url, str) and canonical_game_url(url) == url for url in urls
    ):
        _invalid_record(line_number, "game_urls", "must be canonical game URLs")
    issue = record.get("issue")
    if not isinstance(issue, dict) or issue.get("kind") not in {"high", "summary"}:
        _invalid_record(line_number, "issue.kind", "must be high or summary")
    if issue.get("status") not in {"created", "failed"}:
        _invalid_record(line_number, "issue.status", "must be created or failed")
    if not isinstance(issue.get("marker"), str) or not _ISSUE_MARKER.fullmatch(issue["marker"]):
        _invalid_record(line_number, "issue.marker", "must be a stable marker")
    markers = issue.get("url_markers")
    if not isinstance(markers, dict) or set(markers) != set(urls) or not all(
        isinstance(marker, str) and _ISSUE_MARKER.fullmatch(marker) for marker in markers.values()
    ):
        _invalid_record(line_number, "issue.url_markers", "must map every URL to a stable marker")
    number, error = issue.get("number"), issue.get("error")
    if issue["status"] == "created":
        if type(number) is not int or number <= 0 or error is not None:
            _invalid_record(line_number, "issue", "created outcomes require a number and null error")
    elif number is not None or not _nonblank_string(error):
        _invalid_record(line_number, "issue", "failed outcomes require a nonblank error")


def _number_in_range(value: object, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def _csv_safe(value: object) -> str:
    text = str(value)
    return "'" + text if re.match(r"^[\s\x00-\x1f\x7f-\x9f]*[=+\-@]", text) else text


def _read_csv(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle, strict=True))
    if not rows or rows[0] != _CSV_HEADER:
        raise ValueError("keywords CSV has an invalid header")
    if any(len(row) != len(_CSV_HEADER) for row in rows[1:]):
        raise ValueError("keywords CSV has an invalid row shape")
    _validate_csv_rows(rows[1:])
    return rows[1:]


def _validate_csv_rows(rows: list[list[str]]) -> None:
    for index, row in enumerate(rows, start=2):
        if row[3] not in _KEYWORD_GROUPS:
            raise ValueError(f"keywords CSV row {index} has an invalid group")
        if row[4] not in {"True", "False"}:
            raise ValueError(f"keywords CSV row {index} has an invalid verified value")
        try:
            score = int(row[5])
            confidence = float(row[6])
        except ValueError as error:
            raise ValueError(f"keywords CSV row {index} has invalid numeric values") from error
        if not 0 <= score <= 100 or not _number_in_range(confidence, 0, 1):
            raise ValueError(f"keywords CSV row {index} has invalid numeric ranges")
        if row[7] not in _ACTIONS:
            raise ValueError(f"keywords CSV row {index} has an invalid action")
        for trend in row[8:]:
            if trend and not _number_in_range(_parse_csv_number(trend, index), 0, 100):
                raise ValueError(f"keywords CSV row {index} has an invalid trend")


def _parse_csv_number(value: str, row: int) -> float:
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"keywords CSV row {row} has invalid numeric values") from error


def _csv_payload(existing: list[list[str]], page: GamePage, opportunities: list[Opportunity]) -> str:
    rows = [[_csv_safe(cell) for cell in row] for row in existing if row[0] != _csv_safe(page.url)]
    for opportunity in opportunities:
        keyword, signals = opportunity.keyword, opportunity.signals
        rows.append(
            [
                _csv_safe(page.url),
                _csv_safe(page.name),
                _csv_safe(keyword.phrase),
                _csv_safe(keyword.group),
                _csv_safe(keyword.verified),
                _csv_safe(opportunity.score),
                _csv_safe(opportunity.confidence),
                _csv_safe(opportunity.action),
                _csv_safe("" if signals.trend_7d is None else signals.trend_7d),
                _csv_safe("" if signals.trend_30d is None else signals.trend_30d),
                _csv_safe("" if signals.trend_90d is None else signals.trend_90d),
            ]
        )
    from io import StringIO

    result = StringIO(newline="")
    writer = csv.writer(result, lineterminator="\n")
    writer.writerow(_CSV_HEADER)
    writer.writerows(rows)
    return result.getvalue()


def _issue_error(error: Exception) -> str:
    message = _CONTROL.sub(" ", str(error))
    message = _BEARER_CREDENTIAL.sub("Bearer [REDACTED]", message)
    message = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('label')}={ '[REDACTED]' }", message
    )
    message = _URL_CREDENTIALS.sub(r"\g<prefix>[REDACTED]@", message)
    message = " ".join(message.split())
    return f"{type(error).__name__}: {message[:480]}".rstrip()


def _marker_was_posted(key: tuple[Path, str]) -> bool:
    with _MARKER_CACHE_LOCK:
        if key not in _MARKER_POSTED:
            return False
        _MARKER_POSTED.move_to_end(key)
        return True


def _remember_marker(key: tuple[Path, str]) -> None:
    with _MARKER_CACHE_LOCK:
        _MARKER_POSTED[key] = None
        _MARKER_POSTED.move_to_end(key)
        while len(_MARKER_POSTED) > _MARKER_CACHE_LIMIT:
            _MARKER_POSTED.popitem(last=False)


class Reporter:
    """Publish one game's repeatable local research artifacts."""

    def __init__(
        self,
        reports_dir: Path,
        games_path: Path,
        keywords_path: Path,
        issue_post: IssuePoster | None = None,
    ) -> None:
        self.reports_dir = Path(reports_dir)
        self.games_path = Path(games_path)
        self.keywords_path = Path(keywords_path)
        self.issue_post = issue_post
        self._posted_markers: set[str] = set()

    def publish(
        self,
        page: GamePage,
        opportunities: list[Opportunity],
        date: str,
        allow_issue: bool,
        *,
        defer_issue: bool = False,
        metadata: ResearchMetadata | None = None,
    ) -> PublishResult:
        date = _valid_date(date)
        if not isinstance(page.url, str) or canonical_game_url(page.url) != page.url:
            raise ValueError("page.url must be an already canonical Poki game URL")
        report_path, games_path, keywords_path = _validated_targets(
            _safe_report_path(self.reports_dir, date, page.slug), self.games_path, self.keywords_path
        )
        reports_dir = self.reports_dir.resolve()
        lock_root = _lock_root(reports_dir, games_path, keywords_path)
        marker = _marker(page.url)
        needed = bool(opportunities) and (
            max(opportunity.score for opportunity in opportunities) >= 55
            or all(
                opportunity.signals.trend_7d is None
                for opportunity in opportunities
            )
        )
        issue_status = (
            "baseline" if not allow_issue
            else "not_qualified" if not needed
            else "not_configured" if self.issue_post is None
            else "pending"
        )
        with _publication_lock(lock_root):
            _recover_journal(_journal_path(games_path), games_path, keywords_path, reports_dir)
            report = _render_report(page, opportunities, marker)
            existing_records = _read_jsonl(games_path)
            existing_csv = _read_csv(keywords_path)
            record = _record(
                page, opportunities, date, report_path, issue_status, metadata
            )
            _journaled_batch_write(
                {
                    report_path: report,
                    games_path: _jsonl_payload(existing_records, record),
                    keywords_path: _csv_payload(existing_csv, page, opportunities),
                },
                games_path,
                keywords_path,
                reports_dir,
            )

            if defer_issue:
                return PublishResult(
                    report_path,
                    None,
                    None,
                    notification_pending=(allow_issue and needed and self.issue_post is not None),
                )
            if not allow_issue or self.issue_post is None or not needed or marker in self._posted_markers:
                return PublishResult(report_path, None, None)
        marker_key = (lock_root, marker)
        with _marker_lock(lock_root, marker):
            if marker in self._posted_markers or _marker_was_posted(marker_key):
                return PublishResult(report_path, None, None)
            try:
                issue_number = self.issue_post(
                    f"Poki SEO opportunities: {page.name}", _issue_body(report), marker
                )
                if type(issue_number) is not int or issue_number <= 0:
                    raise ValueError("issue poster returned an invalid issue number")
            except Exception as error:
                return PublishResult(report_path, None, _issue_error(error))
            self._posted_markers.add(marker)
            _remember_marker(marker_key)
            return PublishResult(report_path, issue_number, None)

    def publish_run_notifications(
        self, urls: Iterable[str]
    ) -> dict[str, PublishResult]:
        """Publish deferred Issues from persisted records, partitioned by score.

        High-priority games receive individual Issues.  Ordinary opportunities
        from the same call share one summary Issue.  No page or signal provider
        is consulted on this path.
        """
        requested = list(dict.fromkeys(urls))
        if any(not isinstance(url, str) or canonical_game_url(url) != url for url in requested):
            raise ValueError("urls must contain canonical Poki game URLs")
        if not requested:
            return {}
        if self.games_path.is_symlink() or self.keywords_path.is_symlink():
            raise ValueError("reporting target must not be a symlink")
        games_path = self.games_path.resolve()
        keywords_path = self.keywords_path.resolve()
        reports_dir = self.reports_dir.resolve()
        lock_root = _lock_root(reports_dir, games_path, keywords_path)
        with _publication_lock(lock_root):
            _recover_journal(_journal_path(games_path), games_path, keywords_path, reports_dir)
            records = _read_jsonl(games_path)
            latest: dict[str, dict[str, Any]] = {}
            for line in records:
                record = json.loads(line)
                if record.get("record_type") == "issue_outcome":
                    continue
                page = record.get("page")
                if isinstance(page, dict) and page.get("url") in requested:
                    latest[page["url"]] = record
            missing = [url for url in requested if url not in latest]
            if missing:
                raise ValueError(f"no persisted research record for URL: {missing[0]}")
            artifacts = [self._notification_artifact(latest[url]) for url in requested]

        outcomes: dict[str, PublishResult] = {}
        qualified = [artifact for artifact in artifacts if artifact["eligible"]]
        for artifact in qualified:
            if artifact["score"] < 75:
                continue
            outcomes.update(
                self._post_notification_group(lock_root, [artifact], "high")
            )
        ordinary = [artifact for artifact in qualified if artifact["score"] < 75]
        if ordinary:
            outcomes.update(
                self._post_notification_group(lock_root, ordinary, "summary")
            )
        for artifact in artifacts:
            outcomes.setdefault(
                artifact["url"], PublishResult(artifact["report_path"], None, None)
            )
        return outcomes

    def _notification_artifact(self, record: dict[str, Any]) -> dict[str, Any]:
        page = _page_from_record(record["page"])
        opportunities = [
            _opportunity_from_record(value) for value in record["opportunities"]
        ]
        report_path, _, _ = _validated_targets(
            _safe_report_path(self.reports_dir, record["generated_at"], page.slug),
            self.games_path,
            self.keywords_path,
        )
        marker = _marker(page.url)
        report = _render_report(page, opportunities, marker)
        return {
            "url": page.url,
            "page": page,
            "score": max((item.score for item in opportunities), default=0),
            "eligible": bool(opportunities)
            and (
                max(item.score for item in opportunities) >= 55
                or all(item.signals.trend_7d is None for item in opportunities)
            ),
            "report": report,
            "report_path": report_path,
            "date": record["generated_at"],
            "marker": marker,
        }

    def _post_notification_group(
        self,
        lock_root: Path,
        artifacts: list[dict[str, Any]],
        kind: str,
    ) -> dict[str, PublishResult]:
        urls = [artifact["url"] for artifact in artifacts]
        marker = (
            artifacts[0]["marker"]
            if kind == "high"
            else hashlib.sha256(
                ("summary\0" + "\0".join(sorted(urls))).encode("utf-8")
            ).hexdigest()[:16]
        )
        title = (
            f"Poki SEO high priority: {artifacts[0]['page'].name}"
            if kind == "high"
            else f"Poki SEO opportunity summary: {len(artifacts)} games"
        )
        if kind == "high":
            body = _issue_body(artifacts[0]["report"])
        else:
            marker_lines = [f"<!-- poki-seo:{marker} -->"] + [
                f"<!-- poki-seo:{artifact['marker']} -->" for artifact in artifacts
            ]
            sections = [
                f"## {_markdown_cell(artifact['page'].name)}\n\n{artifact['report']}"
                for artifact in artifacts
            ]
            body = _issue_body(
                "# Poki SEO ordinary opportunity summary\n\n"
                + "\n".join(marker_lines)
                + "\n\n"
                + "\n\n---\n\n".join(sections)
            )
        error_message: str | None = None
        number: int | None = None
        if self.issue_post is None:
            return {
                artifact["url"]: PublishResult(artifact["report_path"], None, None)
                for artifact in artifacts
            }
        marker_key = (lock_root, marker)
        with _marker_lock(lock_root, marker):
            if marker in self._posted_markers or _marker_was_posted(marker_key):
                return {
                    artifact["url"]: PublishResult(artifact["report_path"], None, None)
                    for artifact in artifacts
                }
            try:
                number = self.issue_post(title, body, marker)
                if type(number) is not int or number <= 0:
                    raise ValueError("issue poster returned an invalid issue number")
            except Exception as error:
                error_message = _issue_error(error)
            else:
                self._posted_markers.add(marker)
                _remember_marker(marker_key)
        self._append_issue_event(
            lock_root, artifacts, kind, marker, number, error_message
        )
        return {
            artifact["url"]: PublishResult(
                artifact["report_path"], number, error_message
            )
            for artifact in artifacts
        }

    def _append_issue_event(
        self,
        lock_root: Path,
        artifacts: list[dict[str, Any]],
        kind: str,
        marker: str,
        number: int | None,
        error: str | None,
    ) -> None:
        games_path = self.games_path.resolve()
        keywords_path = self.keywords_path.resolve()
        reports_dir = self.reports_dir.resolve()
        event_value = {
            "schema_version": 2,
            "record_type": "issue_outcome",
            "generated_at": artifacts[0]["date"],
            "game_urls": [artifact["url"] for artifact in artifacts],
            "issue": {
                "kind": kind,
                "marker": marker,
                "url_markers": {
                    artifact["url"]: artifact["marker"] for artifact in artifacts
                },
                "status": "created" if number is not None else "failed",
                "number": number,
                "error": error,
            },
        }
        event = json.dumps(
            event_value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        _validate_record(event_value, 0)
        with _publication_lock(lock_root):
            _recover_journal(
                _journal_path(games_path), games_path, keywords_path, reports_dir
            )
            records = _read_jsonl(games_path)
            _atomic_write(games_path, _jsonl_payload(records, event))

    def retry_notification(self, url: str) -> PublishResult:
        """Retry issue publication from the latest strict JSONL record for *url*.

        This path intentionally performs no page or keyword-provider I/O.  It
        reconstructs the immutable report inputs already persisted locally.
        """
        if not isinstance(url, str) or canonical_game_url(url) != url:
            raise ValueError("url must be an already canonical Poki game URL")
        if self.games_path.is_symlink() or self.keywords_path.is_symlink():
            raise ValueError("reporting target must not be a symlink")
        games_path = self.games_path.resolve()
        keywords_path = self.keywords_path.resolve()
        reports_dir = self.reports_dir.resolve()
        lock_root = _lock_root(reports_dir, games_path, keywords_path)
        with _publication_lock(lock_root):
            _recover_journal(
                _journal_path(games_path), games_path, keywords_path, reports_dir
            )
            records = _read_jsonl(games_path)
            selected: dict[str, Any] | None = None
            for line in records:
                record = json.loads(line)
                page_value = record.get("page")
                if isinstance(page_value, dict) and page_value.get("url") == url:
                    selected = record
            if selected is None:
                raise ValueError("no persisted research record for URL")
            page = _page_from_record(selected["page"])
            opportunities = [
                _opportunity_from_record(value) for value in selected["opportunities"]
            ]
            date = selected["generated_at"]
            report_path, _, _ = _validated_targets(
                _safe_report_path(self.reports_dir, date, page.slug),
                self.games_path,
                self.keywords_path,
            )
            marker = _marker(url)
            report = _render_report(page, opportunities, marker)
            _atomic_write(report_path, report)
            needed = bool(opportunities) and (
                max(opportunity.score for opportunity in opportunities) >= 55
                or all(
                    opportunity.signals.trend_7d is None
                    for opportunity in opportunities
                )
            )
        if self.issue_post is None or not needed:
            return PublishResult(report_path, None, None)
        marker_key = (lock_root, marker)
        with _marker_lock(lock_root, marker):
            if marker in self._posted_markers or _marker_was_posted(marker_key):
                return PublishResult(report_path, None, None)
            try:
                number = self.issue_post(
                    f"Poki SEO opportunities: {page.name}", _issue_body(report), marker
                )
                if type(number) is not int or number <= 0:
                    raise ValueError("issue poster returned an invalid issue number")
            except Exception as error:
                return PublishResult(report_path, None, _issue_error(error))
            self._posted_markers.add(marker)
            _remember_marker(marker_key)
            return PublishResult(report_path, number, None)


def _page_from_record(value: dict[str, Any]) -> GamePage:
    return GamePage(
        url=value["url"],
        slug=value["slug"],
        name=value["name"],
        title=value["title"],
        description=value["description"],
        body=value["body"],
        categories=tuple(value["categories"]),
        developer=value["developer"],
        related_games=tuple(value["related_games"]),
    )


def _opportunity_from_record(value: dict[str, Any]) -> Opportunity:
    from .models import KeywordCandidate, SearchSignals

    keyword = value["keyword"]
    signals = value["signals"]
    return Opportunity(
        KeywordCandidate(
            keyword["phrase"], keyword["group"], tuple(keyword["evidence"]), keyword["verified"]
        ),
        SearchSignals(
            trend_7d=signals["trend_7d"],
            trend_30d=signals["trend_30d"],
            trend_90d=signals["trend_90d"],
            rising_queries=tuple(signals["rising_queries"]),
            autocomplete=tuple(signals["autocomplete"]),
            competition=signals["competition"],
            errors=tuple(signals["errors"]),
            autocomplete_observed=signals["autocomplete_observed"],
            rising_queries_observed=signals["rising_queries_observed"],
        ),
        value["score"],
        value["confidence"],
        value["action"],
    )


def github_issue_poster(
    repository: str, token: str, session: _Session | None = None
) -> IssuePoster:
    """Return a thread-safe idempotency helper for one repository.

    Search indexing cannot provide global exactly-once publication. Actions
    concurrency controls and persisted state complement this process-local
    lock/cache; a lost POST response is searched once before it is reported.
    """
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be an owner/repo identifier")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("GitHub token must not be blank")
    client = session if session is not None else build_session()
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    search_url = "https://api.github.com/search/issues"
    issue_url = f"https://api.github.com/repos/{repository}/issues"

    def post(title: str, body: str, marker: str) -> int:
        _validate_issue_input(title, body, marker)
        key = (repository, marker)
        with _github_post_lock(key):
            _check_github_cooldown(repository)
            cached = _github_cache_get(key)
            if cached is not None:
                return cached
            found = _search_issue(client, search_url, headers, repository, marker)
            if found is not None:
                _github_cache_put(key, found)
                return found
            try:
                created = client.post(
                    issue_url,
                    json={"title": title, "body": body},
                    headers=headers,
                    timeout=(10, 30),
                    allow_redirects=False,
                )
                _raise_for_github_status(created, repository)
                payload = created.json()
                if (
                    not isinstance(payload, dict)
                    or type(payload.get("number")) is not int
                    or payload["number"] <= 0
                ):
                    raise ValueError("malformed GitHub issue create response")
            except GitHubRateLimitError:
                raise
            except Exception as primary:
                try:
                    recovered = _search_issue(client, search_url, headers, repository, marker)
                except Exception:
                    raise primary
                if recovered is not None:
                    _github_cache_put(key, recovered)
                    return recovered
                raise primary
            number = payload["number"]
            _github_cache_put(key, number)
            return number

    return post


def _validate_issue_input(title: object, body: object, marker: object) -> None:
    if not isinstance(title, str) or not title.strip() or len(title) > 256:
        raise ValueError("issue title must be nonblank and at most 256 characters")
    if not isinstance(body, str) or len(body) > _ISSUE_BODY_LIMIT:
        raise ValueError("issue body must be a string within the safe limit")
    if not isinstance(marker, str) or not _ISSUE_MARKER.fullmatch(marker):
        raise ValueError("issue marker must be 16 lowercase hexadecimal characters")


def _github_post_lock(key: tuple[str, str]) -> threading.Lock:
    with _GITHUB_LOCK_GUARD:
        return _GITHUB_POST_LOCKS.setdefault(key, threading.Lock())


def _github_cache_get(key: tuple[str, str]) -> int | None:
    with _GITHUB_LOCK_GUARD:
        value = _GITHUB_ISSUE_CACHE.get(key)
        if value is not None:
            _GITHUB_ISSUE_CACHE.move_to_end(key)
        return value


def _github_cache_put(key: tuple[str, str], number: int) -> None:
    with _GITHUB_LOCK_GUARD:
        _GITHUB_ISSUE_CACHE[key] = number
        _GITHUB_ISSUE_CACHE.move_to_end(key)
        while len(_GITHUB_ISSUE_CACHE) > 2048:
            _GITHUB_ISSUE_CACHE.popitem(last=False)


def _check_github_cooldown(repository: str) -> None:
    now = _github_now()
    until = _GITHUB_COOLDOWNS.get(repository)
    if until is None:
        return
    if until <= now:
        del _GITHUB_COOLDOWNS[repository]
        return
    raise GitHubRateLimitError(f"GitHub rate limit cooldown active for {int(math.ceil(until - now))} seconds")


def _search_issue(
    client: _Session, search_url: str, headers: dict[str, str], repository: str, marker: str
) -> int | None:
    query = f'repo:{repository} is:issue "poki-seo:{marker}"'
    search = client.get(
        search_url,
        params={"q": query},
        headers=headers,
        timeout=(10, 30),
        allow_redirects=False,
    )
    _raise_for_github_status(search, repository)
    payload = search.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("malformed GitHub issue search response")
    for item in payload["items"]:
        if not isinstance(item, dict):
            raise ValueError("malformed GitHub issue search item")
        number = item.get("number")
        if type(number) is not int or number <= 0:
            raise ValueError("malformed GitHub issue number")
        return number
    return None


def _raise_for_github_status(response: _Response, repository: str) -> None:
    status = getattr(response, "status_code", None)
    if status == 429 or (status == 403 and _is_rate_limited_403(response)):
        delay = _github_retry_delay(getattr(response, "headers", {}))
        _GITHUB_COOLDOWNS[repository] = _github_now() + delay
        raise GitHubRateLimitError(f"GitHub rate limited {repository}; retry after {int(math.ceil(delay))} seconds")
    response.raise_for_status()


def _is_rate_limited_403(response: _Response) -> bool:
    headers = getattr(response, "headers", {})
    values = headers if isinstance(headers, Mapping) else {}
    if values.get("Retry-After") or values.get("retry-after"):
        return True
    if values.get("X-RateLimit-Remaining") == "0" or values.get("x-ratelimit-remaining") == "0":
        return True
    try:
        payload = response.json()
    except Exception:
        return False
    return isinstance(payload, dict) and "rate limit" in str(payload.get("message", "")).lower()


def _github_retry_delay(headers: object) -> float:
    values = headers if isinstance(headers, Mapping) else {}
    retry_after = values.get("Retry-After") or values.get("retry-after")
    try:
        delay = float(retry_after)
        if math.isfinite(delay) and 0 < delay <= 86_400:
            return delay
    except (TypeError, ValueError):
        pass
    reset = values.get("X-RateLimit-Reset") or values.get("x-ratelimit-reset")
    try:
        delay = float(reset) - _github_now()
        if math.isfinite(delay) and delay > 0:
            return min(delay, 86_400)
    except (TypeError, ValueError):
        pass
    return 60.0
