#!/usr/bin/env -S uv run python
"""Smoke test: delete ephemeral smoke workspace."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ruff: noqa: E402

import httpx

from scripts.smoke_lib import (
    SmokeTestConfig,
    add_connection_args,
    auth_headers,
    load_state,
    print_stage,
    resolve_api_key,
    stage_cleanup,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Honcho smoke test: cleanup")
    add_connection_args(parser)
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip deletion (keep workspace for debugging)",
    )
    args = parser.parse_args(argv)

    state = load_state(args.state_file)
    if state is None:
        print(f"ERROR: state file not found: {args.state_file}", file=sys.stderr)
        return 1

    config = SmokeTestConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        auth_jwt_secret=args.auth_jwt_secret,
        state_file=args.state_file,
        timeout=args.timeout,
        no_cleanup=args.no_cleanup,
        json_output=args.json_output,
    )

    with httpx.Client(timeout=config.timeout) as client:
        headers = auth_headers(
            resolve_api_key(client, config, state.workspace_id, config.api_key)
        )
        stage = stage_cleanup(client, config, state, headers)

    if stage.ok and not config.no_cleanup and state.created_workspace:
        with contextlib.suppress(OSError):
            args.state_file.unlink()

    print_stage(stage, config.json_output)
    return 0 if stage.ok else 1


if __name__ == "__main__":
    sys.exit(main())
