"""Console entry point for the Poki SEO monitor."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
import sys
from collections.abc import Mapping, Sequence

from .app import sanitize_error
from .config import Config
from .runtime import build_monitor


@contextmanager
def _without_process_github_token(enabled: bool):
    """Temporarily prevent dry-run child processes from inheriting a token."""
    if not enabled:
        yield
        return

    had_token = "GITHUB_TOKEN" in os.environ
    token = os.environ.pop("GITHUB_TOKEN", "")
    try:
        yield
    finally:
        if had_token:
            os.environ["GITHUB_TOKEN"] = token
        else:
            os.environ.pop("GITHUB_TOKEN", None)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="poki-seo-monitor")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="makes live network requests and writes state and artifacts without creating GitHub Issues",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    environment = os.environ if env is None else env
    configured_token = environment.get("GITHUB_TOKEN")
    try:
        args = parse_args(argv)
        with _without_process_github_token(args.dry_run):
            config = Config.from_env(environment)
            if args.dry_run:
                config = replace(config, github_token=None)
            summary = build_monitor(config).run(datetime.now(UTC))
        print(json.dumps(summary.to_dict(), sort_keys=True))
        return 0
    except Exception as error:
        message = sanitize_error(error)
        if configured_token:
            message = message.replace(configured_token, "[REDACTED]")
        print(json.dumps({"error": message}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
