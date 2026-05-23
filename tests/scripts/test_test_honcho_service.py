"""Tests for Honcho service smoke-test library and orchestrator."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.smoke_lib import (
    AUTH_HINT,
    SmokeTestConfig,
    run_smoke_test,
)


def _make_handler(
    *,
    auth_fail_workspace: bool = False,
    queue_never_empty: bool = False,
    chat_called: list[bool] | None = None,
    delete_called: list[bool] | None = None,
) -> httpx.MockTransport:
    queue_polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal queue_polls
        path = request.url.path

        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})

        if path == "/v3/workspaces" and request.method == "POST":
            if auth_fail_workspace:
                return httpx.Response(401, json={"detail": "Unauthorized"})
            return httpx.Response(201, json={"id": "honcho-smoke-testws"})

        if path.endswith("/peers") and request.method == "POST":
            return httpx.Response(201, json={"id": "smoke-peer-test"})

        if path.endswith("/sessions") and request.method == "POST":
            return httpx.Response(201, json={"id": "smoke-session-test"})

        if path.endswith("/messages") and request.method == "POST":
            return httpx.Response(201, json=[{"id": "msg-1"}])

        if path.endswith("/queue/status") and request.method == "GET":
            queue_polls += 1
            if queue_never_empty:
                return httpx.Response(
                    200,
                    json={"pending_work_units": 1, "in_progress_work_units": 0},
                )
            if queue_polls < 2:
                return httpx.Response(
                    200,
                    json={"pending_work_units": 1, "in_progress_work_units": 1},
                )
            return httpx.Response(
                200,
                json={"pending_work_units": 0, "in_progress_work_units": 0},
            )

        if path.endswith("/chat") and request.method == "POST":
            if chat_called is not None:
                chat_called.append(True)
            return httpx.Response(200, json={"content": "You work at Acme Corp."})

        if path.startswith("/v3/workspaces/") and request.method == "DELETE":
            if delete_called is not None:
                delete_called.append(True)
            return httpx.Response(202)

        return httpx.Response(404, json={"detail": f"unmocked {request.method} {path}"})

    return httpx.MockTransport(handler)


@pytest.fixture
def base_config(tmp_path: Path) -> SmokeTestConfig:
    return SmokeTestConfig(
        base_url="http://test.local:8000",
        timeout=5.0,
        queue_timeout=5,
        state_file=tmp_path / "smoke-state.json",
    )


def test_all_stages_pass(base_config: SmokeTestConfig) -> None:
    chat_called: list[bool] = []
    delete_called: list[bool] = []
    transport = _make_handler(chat_called=chat_called, delete_called=delete_called)
    with httpx.Client(transport=transport, base_url=base_config.base_url) as client:
        result = run_smoke_test(client, base_config)

    assert result.ok is True
    names = [s.name for s in result.stages]
    assert names == [
        "health",
        "workspace",
        "peer",
        "session",
        "messages",
        "queue",
        "dialectic",
        "cleanup",
    ]
    assert chat_called == [True]
    assert delete_called == [True]
    assert result.workspace_id == "honcho-smoke-testws"
    assert result.created_workspace is True


def test_queue_timeout_fails(base_config: SmokeTestConfig) -> None:
    config = SmokeTestConfig(
        base_url=base_config.base_url,
        timeout=base_config.timeout,
        queue_timeout=2,
        state_file=base_config.state_file,
    )
    transport = _make_handler(queue_never_empty=True)
    with httpx.Client(transport=transport, base_url=config.base_url) as client:
        result = run_smoke_test(client, config)

    assert result.ok is False
    failed = [s for s in result.stages if not s.ok]
    assert len(failed) == 1
    assert failed[0].name == "queue"
    assert "did not empty" in failed[0].detail


def test_auth_failure_hint(base_config: SmokeTestConfig) -> None:
    transport = _make_handler(auth_fail_workspace=True)
    with httpx.Client(transport=transport, base_url=base_config.base_url) as client:
        result = run_smoke_test(client, base_config)

    assert result.ok is False
    assert result.stages[-1].name == "workspace"
    assert AUTH_HINT in result.stages[-1].detail


def test_skip_chat_skips_dialectic(base_config: SmokeTestConfig) -> None:
    chat_called: list[bool] = []
    config = SmokeTestConfig(
        base_url=base_config.base_url,
        timeout=base_config.timeout,
        queue_timeout=base_config.queue_timeout,
        state_file=base_config.state_file,
        skip_chat=True,
    )
    transport = _make_handler(chat_called=chat_called)
    with httpx.Client(transport=transport, base_url=config.base_url) as client:
        result = run_smoke_test(client, config)

    assert result.ok is True
    assert "dialectic" not in [s.name for s in result.stages]
    assert chat_called == []


def test_no_cleanup_skips_delete(base_config: SmokeTestConfig) -> None:
    delete_called: list[bool] = []
    config = SmokeTestConfig(
        base_url=base_config.base_url,
        timeout=base_config.timeout,
        queue_timeout=base_config.queue_timeout,
        state_file=base_config.state_file,
        skip_chat=True,
        no_cleanup=True,
    )
    transport = _make_handler(delete_called=delete_called)
    with httpx.Client(transport=transport, base_url=config.base_url) as client:
        result = run_smoke_test(client, config)

    assert result.ok is True
    assert "cleanup" not in [s.name for s in result.stages]
    assert delete_called == []


def test_json_result_shape(base_config: SmokeTestConfig) -> None:
    transport = _make_handler()
    config = SmokeTestConfig(
        base_url=base_config.base_url,
        timeout=base_config.timeout,
        queue_timeout=base_config.queue_timeout,
        state_file=base_config.state_file,
        skip_chat=True,
    )
    with httpx.Client(transport=transport, base_url=config.base_url) as client:
        result = run_smoke_test(client, config)

    payload = result.to_dict()
    assert payload["ok"] is True
    assert payload["workspace_id"] == "honcho-smoke-testws"
    assert isinstance(payload["stages"], list)
    assert json.dumps(payload)
