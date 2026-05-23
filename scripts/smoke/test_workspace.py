#!/usr/bin/env -S uv run python
"""Smoke test: create workspace (DB + migrations)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ruff: noqa: E402

import httpx

from scripts.smoke_lib import (
    SmokeState,
    SmokeTestConfig,
    add_connection_args,
    load_state,
    print_stage,
    save_state,
    stage_workspace,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Honcho smoke test: workspace")
    add_connection_args(parser)
    parser.add_argument(
        "--workspace-id",
        default=None,
        help="Use an existing workspace instead of creating one",
    )
    args = parser.parse_args(argv)

    state = load_state(args.state_file) or SmokeState(base_url=args.base_url)
    state.base_url = args.base_url
    if args.workspace_id:
        state.workspace_id = args.workspace_id

    config = SmokeTestConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        auth_jwt_secret=args.auth_jwt_secret,
        workspace_id=state.workspace_id,
        state_file=args.state_file,
        timeout=args.timeout,
        json_output=args.json_output,
    )

    with httpx.Client(timeout=config.timeout) as client:
        stage, _, _ = stage_workspace(client, config, state, api_key=config.api_key)

    save_state(args.state_file, state)
    print_stage(stage, config.json_output)
    return 0 if stage.ok else 1


if __name__ == "__main__":
    sys.exit(main())
