"""Verifier for off-request-path (async) run execution.

Complements ``tests/test_api_runs.py`` (synchronous mode). Here ``run_async`` is
ON: ``POST /runs`` must return immediately with ``phase="running"`` and the run
must converge, via background execution, to the same terminal state the
synchronous path produces — observed by polling ``GET /runs/{thread_id}``.

Fully offline: mock data engine + in-process MemorySaver + the in-process
ThreadRunExecutor (no Redis, no external worker). The registry/executor
singletons and the MemorySaver are reset per test for isolation.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import app.backend.api.execution as execution_module
import app.backend.graph as graph_module
import app.backend.identity as identity_module
import app.shared.settings as settings_module
import pytest
from app.app import create_app
from app.shared.settings import Settings
from fastapi.testclient import TestClient

DISTRESSED_CODE = "1234567"
NORMAL_CODE = "2000001"


def _async_settings() -> Settings:
    return Settings(
        use_mocks=True,
        persist_checkpoints=False,
        auth_required=False,
        run_async=True,
        ui_meeting_pace_seconds=0.0,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = _async_settings()
    settings_module.get_settings.cache_clear()
    for mod in (settings_module, graph_module, identity_module):
        monkeypatch.setattr(mod, "get_settings", lambda: settings)
    # Fresh executor + registry + checkpointer so runs cannot leak across tests.
    execution_module._REGISTRY = None  # noqa: SLF001 - intentional test reset
    execution_module._EXECUTOR = None  # noqa: SLF001 - intentional test reset
    graph_module.reset_memory_saver()
    with TestClient(create_app()) as test_client:
        yield test_client
    execution_module._REGISTRY = None  # noqa: SLF001
    execution_module._EXECUTOR = None  # noqa: SLF001
    graph_module.reset_memory_saver()


def _poll_until_terminal(
    client: TestClient, thread_id: str, *, timeout: float = 10.0
) -> dict[str, Any]:
    """Poll GET until the run leaves the 'running' phase (or time out)."""
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/runs/{thread_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["phase"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {thread_id} did not finish within {timeout}s: {body}")


def test_start_returns_running_immediately(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/runs",
        json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-async-start"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "running"
    assert body["awaiting_decision"] is False


def test_distressed_run_converges_to_awaiting_decision(client: TestClient) -> None:
    client.post(
        "/api/v1/runs",
        json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-async-distressed"},
    )
    body = _poll_until_terminal(client, "t-async-distressed")
    assert body["phase"] == "awaiting_decision"
    assert body["awaiting_decision"] is True
    assert body["values"]["proposed_strategies"]


def test_normal_run_converges_to_done(client: TestClient) -> None:
    client.post(
        "/api/v1/runs",
        json={"tdb_code": NORMAL_CODE, "thread_id": "t-async-normal"},
    )
    body = _poll_until_terminal(client, "t-async-normal")
    assert body["phase"] == "done"
    assert body["awaiting_decision"] is False
    assert body["values"]["fsa_classification"] == "seijosaki"


def test_async_resume_completes_and_writes_keikakusho(client: TestClient) -> None:
    client.post(
        "/api/v1/runs",
        json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-async-approve"},
    )
    _poll_until_terminal(client, "t-async-approve")
    resp = client.post("/api/v1/runs/t-async-approve/resume", json={"decision": "approve"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["phase"] == "running"
    body = _poll_until_terminal(client, "t-async-approve")
    assert body["phase"] == "done"
    draft = body["values"]["keikakusho_draft"]
    assert draft and "\u7d4c\u55b6\u6539\u5584\u8a08\u753b\u66f8" in draft


def test_unknown_thread_is_404(client: TestClient) -> None:
    resp = client.get("/api/v1/runs/never-started")
    assert resp.status_code == 404, resp.text


def test_async_run_failure_is_reported_as_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background job that raises is surfaced as phase='error' via the registry."""
    import app.backend.api.runs as runs_module

    def _boom(payload: dict[str, Any], thread_id: str, *, resume: bool) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(runs_module, "_run_to_pause", _boom)
    client.post(
        "/api/v1/runs",
        json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-async-error"},
    )
    body = _poll_until_terminal(client, "t-async-error")
    assert body["phase"] == "error"
    assert body["error"]
