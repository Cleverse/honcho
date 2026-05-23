#!/usr/bin/env -S uv run python
"""Smoke test: API liveness (GET /health)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo root must be on sys.path before `from scripts...` (direct file execution).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ruff: noqa: E402

import httpx

from scripts.smoke_lib import (
    SmokeTestConfig,
    add_connection_args,
    print_stage,
    stage_health,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Honcho smoke test: health")
    add_connection_args(parser)
    args = parser.parse_args(argv)
    config = SmokeTestConfig(
        base_url=args.base_url,
        timeout=args.timeout,
        json_output=args.json_output,
    )
    with httpx.Client(timeout=config.timeout) as client:
        stage = stage_health(client, config)
    print_stage(stage, config.json_output)
    return 0 if stage.ok else 1


if __name__ == "__main__":
    sys.exit(main())
