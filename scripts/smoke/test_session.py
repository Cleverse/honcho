#!/usr/bin/env -S uv run python
"""Smoke test: create session with peer."""

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
    SmokeTestConfig,
    add_connection_args,
    auth_headers,
    load_state,
    print_stage,
    resolve_api_key,
    save_state,
    stage_session,
    try_cleanup,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Honcho smoke test: session")
    add_connection_args(parser)
    args = parser.parse_args(argv)

    state = load_state(args.state_file)
    if state is None or not state.peer_id:
        print(
            f"ERROR: state file missing peer_id: {args.state_file}. "
            "Run scripts/smoke/test_peer.py first.",
            file=sys.stderr,
        )
        return 1

    config = SmokeTestConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        auth_jwt_secret=args.auth_jwt_secret,
        state_file=args.state_file,
        timeout=args.timeout,
        json_output=args.json_output,
    )

    with httpx.Client(timeout=config.timeout) as client:
        headers = auth_headers(
            resolve_api_key(client, config, state.workspace_id, config.api_key)
        )
        stage = stage_session(client, config, state, headers)
        if not stage.ok:
            try_cleanup(
                client,
                config.base_url.rstrip("/"),
                state.workspace_id,
                state.created_workspace,
                config,
                headers,
            )

    save_state(args.state_file, state)
    print_stage(stage, config.json_output)
    return 0 if stage.ok else 1


if __name__ == "__main__":
    sys.exit(main())
