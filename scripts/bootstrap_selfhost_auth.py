#!/usr/bin/env -S uv run python
"""
Bootstrap self-hosted Honcho auth: mint admin JWT and workspace-scoped API key.

Requires AUTH_JWT_SECRET to match the Honcho server deployment.

Usage:
  AUTH_JWT_SECRET=... uv run python scripts/bootstrap_selfhost_auth.py \\
    --base-url http://localhost:8000 \\
    --workspace-id pr_cOROM7QwO1Ao

  # From a pod in aerogram-prod:
  AUTH_JWT_SECRET=... uv run python scripts/bootstrap_selfhost_auth.py \\
    --base-url http://honcho-api:8000 \\
    --workspace-id pr_cOROM7QwO1Ao
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx
import jwt

# Allow importing security helpers from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.security import JWTParams  # noqa: E402


def create_admin_token(secret: str) -> str:
    params = JWTParams(t="", ad=True)
    payload = {k: v for k, v in params.__dict__.items() if v is not None}
    return jwt.encode(payload, secret.encode("utf-8"), algorithm="HS256")


def create_workspace_key(
    base_url: str, admin_token: str, workspace_id: str
) -> str:
    url = f"{base_url.rstrip('/')}/v3/keys"
    response = httpx.post(
        url,
        params={"workspace_id": workspace_id},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60.0,
    )
    response.raise_for_status()
    data = response.json()
    key = data.get("key")
    if not key:
        raise RuntimeError(f"Unexpected response from {url}: {data}")
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap Honcho self-host API keys")
    parser.add_argument(
        "--base-url",
        default=os.getenv("HONCHO_BASE_URL", "http://localhost:8000"),
        help="Honcho API base URL",
    )
    parser.add_argument(
        "--workspace-id",
        default=os.getenv("HONCHO_WORKSPACE_ID", "pr_cOROM7QwO1Ao"),
        help="Workspace ID for the LINE AI client key",
    )
    parser.add_argument(
        "--auth-jwt-secret",
        default=os.getenv("AUTH_JWT_SECRET"),
        help="Must match AUTH_JWT_SECRET on the Honcho server",
    )
    args = parser.parse_args()

    if not args.auth_jwt_secret:
        print(
            "ERROR: Set AUTH_JWT_SECRET (same value as honcho-secrets auth-jwt-secret)",
            file=sys.stderr,
        )
        sys.exit(1)

    health = httpx.get(f"{args.base_url.rstrip('/')}/health", timeout=30.0)
    health.raise_for_status()
    print(f"Health OK: {health.json()}")

    admin_token = create_admin_token(args.auth_jwt_secret)
    workspace_key = create_workspace_key(
        args.base_url, admin_token, args.workspace_id
    )

    print("\nWorkspace-scoped API key (paste into aerogram config honcho.clientApiKey):")
    print(workspace_key)
    print("\nGitOps: arken-cdk8s/src/aerogram/config.ts → honcho.prod.clientApiKey")


if __name__ == "__main__":
    main()
