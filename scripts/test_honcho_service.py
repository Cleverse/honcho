#!/usr/bin/env -S uv run python
"""
Orchestrator: run all Honcho service smoke-test stages in sequence.

Individual stages live under scripts/smoke/ and can be run standalone; they
share state via --state-file (default: .honcho-smoke-state.json).

Usage:
  uv run python scripts/test_honcho_service.py
  uv run python scripts/test_honcho_service.py --skip-chat
  uv run python scripts/test_honcho_service.py --stages health,workspace,queue

Run one stage only (from repo root):
  uv run python -m scripts.smoke.test_health --base-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ruff: noqa: E402

import httpx

from scripts.smoke_lib import (
    SmokeTestConfig,
    add_connection_args,
    print_human_result,
    run_smoke_test,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STAGE_MODULES: list[tuple[str, str]] = [
    ("health", "scripts.smoke.test_health"),
    ("workspace", "scripts.smoke.test_workspace"),
    ("peer", "scripts.smoke.test_peer"),
    ("session", "scripts.smoke.test_session"),
    ("messages", "scripts.smoke.test_messages"),
    ("queue", "scripts.smoke.test_queue"),
    ("dialectic", "scripts.smoke.test_dialectic"),
    ("cleanup", "scripts.smoke.test_cleanup"),
]


def _shared_args(config: SmokeTestConfig) -> list[str]:
    args = [
        "--base-url",
        config.base_url,
        "--state-file",
        str(config.state_file),
        "--timeout",
        str(config.timeout),
    ]
    if config.api_key:
        args.extend(["--api-key", config.api_key])
    if config.auth_jwt_secret:
        args.extend(["--auth-jwt-secret", config.auth_jwt_secret])
    if config.json_output:
        args.append("--json")
    return args


def run_stages_subprocess(
    config: SmokeTestConfig,
    stages: list[str],
) -> int:
    """Run selected stage scripts as subprocesses."""
    stage_map = dict(STAGE_MODULES)
    shared = _shared_args(config)
    failed: str | None = None

    for name in stages:
        module = stage_map.get(name)
        if module is None:
            print(f"ERROR: unknown stage {name!r}", file=sys.stderr)
            return 1
        if name == "dialectic" and config.skip_chat:
            continue

        cmd = [sys.executable, "-m", module, *shared]
        if name == "workspace" and config.workspace_id:
            cmd.extend(["--workspace-id", config.workspace_id])
        if name == "queue":
            cmd.extend(["--queue-timeout", str(config.queue_timeout)])
        if name == "cleanup" and config.no_cleanup:
            cmd.append("--no-cleanup")

        if not config.json_output:
            print(f"\n--- {name} ---")

        result = subprocess.run(cmd, cwd=PROJECT_ROOT, check=False)
        if result.returncode != 0:
            failed = name
            break

    if failed:
        if not config.json_output:
            print(f"\nSmoke test failed at stage: {failed}", file=sys.stderr)
        return 1
    if not config.json_output:
        print("\nAll stages passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Honcho service smoke tests (orchestrator or in-process)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Prerequisites: API and deriver must be running. "
            "Full run requires LLM keys; use --skip-chat otherwise. "
            "Stages: "
            + ", ".join(name for name, _ in STAGE_MODULES)
        ),
    )
    add_connection_args(parser)
    parser.add_argument(
        "--workspace-id",
        default=os.getenv("HONCHO_WORKSPACE_ID"),
        help="Use an existing workspace instead of creating one",
    )
    parser.add_argument(
        "--queue-timeout",
        type=int,
        default=90,
        help="Max seconds to wait for deriver queue to drain",
    )
    parser.add_argument(
        "--skip-chat",
        action="store_true",
        help="Skip dialectic stage (no LLM required)",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep the smoke workspace after the test",
    )
    parser.add_argument(
        "--stages",
        default=None,
        help="Comma-separated stage names to run (default: all)",
    )
    parser.add_argument(
        "--subprocess",
        action="store_true",
        help="Run each stage as a separate script subprocess (default: in-process)",
    )
    args = parser.parse_args(argv)

    config = SmokeTestConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        auth_jwt_secret=args.auth_jwt_secret,
        workspace_id=args.workspace_id,
        state_file=args.state_file,
        timeout=args.timeout,
        queue_timeout=args.queue_timeout,
        skip_chat=args.skip_chat,
        no_cleanup=args.no_cleanup,
        json_output=args.json_output,
    )

    if args.stages:
        names = [s.strip() for s in args.stages.split(",") if s.strip()]
    else:
        names = [name for name, _ in STAGE_MODULES]
        if config.skip_chat and "dialectic" in names:
            names = [n for n in names if n != "dialectic"]
        if config.no_cleanup and "cleanup" in names:
            names = [n for n in names if n != "cleanup"]

    if args.subprocess:
        return run_stages_subprocess(config, names)

    with httpx.Client(timeout=config.timeout) as client:
        result = run_smoke_test(client, config)

    if config.json_output:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print_human_result(result)

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
