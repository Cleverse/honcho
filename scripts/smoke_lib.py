"""Shared helpers for Honcho service smoke-test scripts."""

from __future__ import annotations

import sys
from pathlib import Path

# Repo root on sys.path when this file is loaded (direct script or pytest).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ruff: noqa: E402

import argparse
import contextlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt

AUTH_HINT = (
    "Authentication failed. Set HONCHO_API_KEY or AUTH_JWT_SECRET "
    "(must match server AUTH_JWT_SECRET when AUTH_USE_AUTH=true). "
    "See scripts/bootstrap_selfhost_auth.py."
)

DERIVER_HINT = "Check deriver logs: docker compose logs deriver --tail 50"

DEFAULT_STATE_FILE = ".honcho-smoke-state.json"

SMOKE_MESSAGE = (
    "I work on distributed systems at Acme Corp. "
    "Our team builds observability tooling."
)


@dataclass
class StageResult:
    name: str
    ok: bool
    detail: str
    duration_ms: int


@dataclass
class SmokeTestConfig:
    base_url: str
    api_key: str | None = None
    auth_jwt_secret: str | None = None
    workspace_id: str | None = None
    peer_id: str | None = None
    session_id: str | None = None
    state_file: Path = field(default_factory=lambda: Path(DEFAULT_STATE_FILE))
    timeout: float = 120.0
    queue_timeout: int = 90
    skip_chat: bool = False
    no_cleanup: bool = False
    json_output: bool = False


@dataclass
class SmokeState:
    base_url: str
    workspace_id: str | None = None
    peer_id: str | None = None
    session_id: str | None = None
    created_workspace: bool = False
    smoke_suffix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SmokeState:
        return cls(
            base_url=data["base_url"],
            workspace_id=data.get("workspace_id"),
            peer_id=data.get("peer_id"),
            session_id=data.get("session_id"),
            created_workspace=bool(data.get("created_workspace", False)),
            smoke_suffix=data.get("smoke_suffix"),
        )


@dataclass
class SmokeTestResult:
    ok: bool
    stages: list[StageResult] = field(default_factory=list)
    workspace_id: str | None = None
    created_workspace: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "workspace_id": self.workspace_id,
            "created_workspace": self.created_workspace,
            "stages": [
                {
                    "name": s.name,
                    "ok": s.ok,
                    "detail": s.detail,
                    "duration_ms": s.duration_ms,
                }
                for s in self.stages
            ],
        }


def create_admin_token(secret: str) -> str:
    payload = {"t": "", "ad": True}
    return jwt.encode(payload, secret.encode("utf-8"), algorithm="HS256")


def create_workspace_key(
    client: httpx.Client,
    base_url: str,
    admin_token: str,
    workspace_id: str,
) -> str:
    url = f"{base_url.rstrip('/')}/v3/keys"
    response = client.post(
        url,
        params={"workspace_id": workspace_id},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    response.raise_for_status()
    data = response.json()
    key = data.get("key")
    if not key:
        raise RuntimeError(f"Unexpected response from {url}: {data}")
    return key


def auth_headers(api_key: str | None) -> dict[str, str]:
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def check_auth_error(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise RuntimeError(AUTH_HINT)


def run_stage(name: str, fn: Any) -> StageResult:
    start = time.perf_counter()
    try:
        detail = fn()
        ok = True
        detail_str = detail if isinstance(detail, str) else "OK"
    except Exception as exc:
        ok = False
        detail_str = str(exc)
    duration_ms = int((time.perf_counter() - start) * 1000)
    return StageResult(name=name, ok=ok, detail=detail_str, duration_ms=duration_ms)


def load_state(path: Path) -> SmokeState | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open(encoding="utf-8") as f:
        return SmokeState.from_dict(json.load(f))


def save_state(path: Path, state: SmokeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)


def config_from_state(state: SmokeState, **overrides: Any) -> SmokeTestConfig:
    return SmokeTestConfig(
        base_url=state.base_url,
        workspace_id=state.workspace_id,
        peer_id=state.peer_id,
        session_id=state.session_id,
        **overrides,
    )


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        default=os.getenv("HONCHO_BASE_URL", "http://localhost:8000"),
        help="Honcho API base URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("HONCHO_API_KEY"),
        help="API key or admin JWT (when AUTH_USE_AUTH=true)",
    )
    parser.add_argument(
        "--auth-jwt-secret",
        default=os.getenv("AUTH_JWT_SECRET"),
        help="Mint admin/workspace keys when --api-key is not set",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.getenv("HONCHO_SMOKE_STATE_FILE", DEFAULT_STATE_FILE)),
        help="JSON file shared across smoke scripts for workspace/peer/session IDs",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="HTTP timeout per request (seconds)",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit machine-readable JSON result",
    )


def resolve_api_key(
    client: httpx.Client,
    config: SmokeTestConfig,
    workspace_id: str | None,
    api_key: str | None,
) -> str | None:
    if api_key:
        return api_key
    if config.auth_jwt_secret and workspace_id:
        admin = create_admin_token(config.auth_jwt_secret)
        return create_workspace_key(
            client, config.base_url.rstrip("/"), admin, workspace_id
        )
    return None


def try_cleanup(
    client: httpx.Client,
    base: str,
    workspace_id: str | None,
    created_workspace: bool,
    config: SmokeTestConfig,
    headers: dict[str, str],
) -> None:
    if config.no_cleanup or not created_workspace or not workspace_id:
        return
    with contextlib.suppress(Exception):
        client.delete(f"{base}/v3/workspaces/{workspace_id}", headers=headers)


def print_stage(stage: StageResult, json_output: bool) -> None:
    if json_output:
        print(json.dumps(asdict(stage), indent=2))
    else:
        label = "OK" if stage.ok else "FAIL"
        print(f"[{label}] {stage.name}: {stage.detail} ({stage.duration_ms}ms)")


def stage_health(client: httpx.Client, config: SmokeTestConfig) -> StageResult:
    base = config.base_url.rstrip("/")

    def run() -> str:
        response = client.get(f"{base}/health")
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"Unexpected health response: {data}")
        return json.dumps(data)

    return run_stage("health", run)


def stage_workspace(
    client: httpx.Client,
    config: SmokeTestConfig,
    state: SmokeState,
    *,
    api_key: str | None,
) -> tuple[StageResult, str | None, bool]:
    base = config.base_url.rstrip("/")
    created_workspace = False
    workspace_id = config.workspace_id or state.workspace_id
    key = api_key

    def run() -> str:
        nonlocal created_workspace, workspace_id, key
        if workspace_id:
            return f"using existing workspace {workspace_id}"

        suffix = state.smoke_suffix or uuid.uuid4().hex[:8]
        state.smoke_suffix = suffix
        name = f"honcho-smoke-{suffix}"

        def create_workspace(headers: dict[str, str]) -> httpx.Response:
            return client.post(
                f"{base}/v3/workspaces",
                json={"id": name},
                headers=headers,
            )

        response = create_workspace(auth_headers(key))
        if response.status_code == 401:
            if config.auth_jwt_secret:
                key = create_admin_token(config.auth_jwt_secret)
                response = create_workspace(auth_headers(key))
            else:
                raise RuntimeError(AUTH_HINT)
        check_auth_error(response)
        response.raise_for_status()
        data = response.json()
        workspace_id = data.get("id") or data.get("name")
        if not workspace_id:
            raise RuntimeError(f"Workspace response missing id: {data}")
        created_workspace = True
        state.workspace_id = workspace_id
        state.created_workspace = True
        return f"workspace_id={workspace_id}"

    stage = run_stage("workspace", run)
    return stage, key, created_workspace


def stage_peer(
    client: httpx.Client,
    config: SmokeTestConfig,
    state: SmokeState,
    headers: dict[str, str],
) -> StageResult:
    base = config.base_url.rstrip("/")
    workspace_id = state.workspace_id
    if not workspace_id:
        raise RuntimeError("workspace_id required; run test_workspace first")

    suffix = state.smoke_suffix or uuid.uuid4().hex[:8]
    state.smoke_suffix = suffix

    def run() -> str:
        peer_id = config.peer_id or f"smoke-peer-{suffix}"
        response = client.post(
            f"{base}/v3/workspaces/{workspace_id}/peers",
            json={"id": peer_id},
            headers=headers,
        )
        check_auth_error(response)
        response.raise_for_status()
        state.peer_id = peer_id
        return f"peer_id={peer_id}"

    return run_stage("peer", run)


def stage_session(
    client: httpx.Client,
    config: SmokeTestConfig,
    state: SmokeState,
    headers: dict[str, str],
) -> StageResult:
    base = config.base_url.rstrip("/")
    workspace_id = state.workspace_id
    peer_id = state.peer_id
    if not workspace_id or not peer_id:
        raise RuntimeError("workspace_id and peer_id required; run earlier smoke scripts")

    suffix = state.smoke_suffix or uuid.uuid4().hex[:8]

    def run() -> str:
        session_id = config.session_id or f"smoke-session-{suffix}"
        response = client.post(
            f"{base}/v3/workspaces/{workspace_id}/sessions",
            json={"id": session_id, "peers": {peer_id: {}}},
            headers=headers,
        )
        check_auth_error(response)
        response.raise_for_status()
        state.session_id = session_id
        return f"session_id={session_id}"

    return run_stage("session", run)


def stage_messages(
    client: httpx.Client,
    state: SmokeState,
    headers: dict[str, str],
) -> StageResult:
    base = state.base_url.rstrip("/")
    workspace_id = state.workspace_id
    session_id = state.session_id
    peer_id = state.peer_id
    if not workspace_id or not session_id or not peer_id:
        raise RuntimeError(
            "workspace_id, session_id, and peer_id required; run earlier smoke scripts"
        )

    def run() -> str:
        response = client.post(
            f"{base}/v3/workspaces/{workspace_id}/sessions/{session_id}/messages",
            json={
                "messages": [
                    {"peer_id": peer_id, "content": SMOKE_MESSAGE},
                ]
            },
            headers=headers,
        )
        check_auth_error(response)
        response.raise_for_status()
        data = response.json()
        if not data:
            raise RuntimeError("No messages returned")
        return f"created {len(data)} message(s)"

    return run_stage("messages", run)


def stage_queue(
    client: httpx.Client,
    config: SmokeTestConfig,
    state: SmokeState,
    headers: dict[str, str],
) -> StageResult:
    base = state.base_url.rstrip("/")
    workspace_id = state.workspace_id
    if not workspace_id:
        raise RuntimeError("workspace_id required; run test_workspace first")

    last_status: dict[str, Any] = {}

    def run() -> str:
        nonlocal last_status
        time.sleep(1)
        deadline = time.time() + config.queue_timeout
        while time.time() < deadline:
            response = client.get(
                f"{base}/v3/workspaces/{workspace_id}/queue/status",
                headers=headers,
            )
            check_auth_error(response)
            response.raise_for_status()
            last_status = response.json()
            pending = last_status.get("pending_work_units", -1)
            in_progress = last_status.get("in_progress_work_units", -1)
            if pending == 0 and in_progress == 0:
                return (
                    f"queue empty "
                    f"(completed={last_status.get('completed_work_units', '?')})"
                )
            time.sleep(1)
        raise RuntimeError(
            f"Queue did not empty within {config.queue_timeout}s. "
            f"Last status: {last_status}. {DERIVER_HINT}"
        )

    return run_stage("queue", run)


def stage_dialectic(
    client: httpx.Client,
    state: SmokeState,
    headers: dict[str, str],
) -> StageResult:
    base = state.base_url.rstrip("/")
    workspace_id = state.workspace_id
    peer_id = state.peer_id
    session_id = state.session_id
    if not workspace_id or not peer_id or not session_id:
        raise RuntimeError(
            "workspace_id, peer_id, and session_id required; run earlier smoke scripts"
        )

    def run() -> str:
        response = client.post(
            f"{base}/v3/workspaces/{workspace_id}/peers/{peer_id}/chat",
            json={
                "query": "What do you know about my work?",
                "session_id": session_id,
                "stream": False,
                "reasoning_level": "minimal",
            },
            headers=headers,
        )
        check_auth_error(response)
        if response.status_code >= 500:
            raise RuntimeError(
                f"Chat failed ({response.status_code}): {response.text}. "
                "Ensure LLM provider keys are configured on the server."
            )
        response.raise_for_status()
        data = response.json()
        content = data.get("content")
        if not content or not str(content).strip():
            raise RuntimeError(
                f"Chat returned empty content: {data}. "
                "Ensure LLM provider keys are configured on the server."
            )
        preview = str(content).strip()[:80]
        return f"response length={len(str(content))}, preview={preview!r}"

    return run_stage("dialectic", run)


def stage_cleanup(
    client: httpx.Client,
    config: SmokeTestConfig,
    state: SmokeState,
    headers: dict[str, str],
) -> StageResult:
    base = state.base_url.rstrip("/")
    workspace_id = state.workspace_id

    def run() -> str:
        if config.no_cleanup or not state.created_workspace or not workspace_id:
            return "skipped"
        response = client.delete(
            f"{base}/v3/workspaces/{workspace_id}",
            headers=headers,
        )
        check_auth_error(response)
        if response.status_code not in (200, 202, 204):
            response.raise_for_status()
        return f"deleted workspace {workspace_id}"

    return run_stage("cleanup", run)


def run_smoke_test(
    client: httpx.Client,
    config: SmokeTestConfig,
) -> SmokeTestResult:
    """Run all smoke stages in sequence (used by the orchestrator)."""
    base = config.base_url.rstrip("/")
    state = load_state(config.state_file) or SmokeState(base_url=config.base_url)
    state.base_url = config.base_url
    if config.workspace_id:
        state.workspace_id = config.workspace_id
    if not state.smoke_suffix:
        state.smoke_suffix = uuid.uuid4().hex[:8]

    result = SmokeTestResult(ok=True)
    api_key = config.api_key

    stage = stage_health(client, config)
    result.stages.append(stage)
    if not stage.ok:
        result.ok = False
        return result

    stage, api_key, _ = stage_workspace(client, config, state, api_key=api_key)
    result.stages.append(stage)
    result.workspace_id = state.workspace_id
    result.created_workspace = state.created_workspace
    save_state(config.state_file, state)
    if not stage.ok:
        result.ok = False
        return result

    headers = auth_headers(
        resolve_api_key(client, config, state.workspace_id, api_key)
    )

    for stage_fn in (stage_peer, stage_session):
        stage = stage_fn(client, config, state, headers)
        result.stages.append(stage)
        save_state(config.state_file, state)
        if not stage.ok:
            result.ok = False
            try_cleanup(
                client,
                base,
                state.workspace_id,
                state.created_workspace,
                config,
                headers,
            )
            return result

    stage = stage_messages(client, state, headers)
    result.stages.append(stage)
    save_state(config.state_file, state)
    if not stage.ok:
        result.ok = False
        try_cleanup(
            client, base, state.workspace_id, state.created_workspace, config, headers
        )
        return result

    stage = stage_queue(client, config, state, headers)
    result.stages.append(stage)
    if not stage.ok:
        result.ok = False
        try_cleanup(
            client, base, state.workspace_id, state.created_workspace, config, headers
        )
        return result

    if not config.skip_chat:
        stage = stage_dialectic(client, state, headers)
        result.stages.append(stage)
        if not stage.ok:
            result.ok = False
            try_cleanup(
                client, base, state.workspace_id, state.created_workspace, config, headers
            )
            return result

    if not config.no_cleanup:
        stage = stage_cleanup(client, config, state, headers)
        result.stages.append(stage)
        if not stage.ok:
            result.ok = False

    if not config.no_cleanup and state.created_workspace:
        with contextlib.suppress(OSError):
            config.state_file.unlink()

    return result


def print_human_result(result: SmokeTestResult) -> None:
    import sys

    for stage in result.stages:
        label = "OK" if stage.ok else "FAIL"
        print(f"[{label}] {stage.name}: {stage.detail} ({stage.duration_ms}ms)")
    if result.ok:
        print("\nAll stages passed.")
    else:
        print("\nSmoke test failed.", file=sys.stderr)
