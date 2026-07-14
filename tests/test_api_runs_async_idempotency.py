"""Regression: async start_run must be idempotent against an in-flight run.

Before the fix, start_run's idempotency guard only checked the durable
checkpointer. In async mode a dispatched run is RUNNING in the registry before
any checkpoint exists, so a second POST /runs with the same thread_id dispatched
a SECOND concurrent job racing on the same thread_id. This pins that the second
POST is an idempotent 'running' hit and the executor is invoked only once.

Fully offline: a controllable fake executor that holds the job (never runs it),
so the run stays RUNNING with no checkpoint — exactly the window the bug lived in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import app.backend.api.execution as execution_module
import app.backend.graph as graph_module
import app.backend.identity as identity_module
import app.shared.settings as settings_module
import pytest
from app.app import create_app
from app.backend.api.execution import RunRegistry
from app.shared.settings import Settings
from fastapi.testclient import TestClient

DISTRESSED_CODE = "1234567"


class _HoldingExecutor:
    """Records submissions but never runs the job (keeps the run RUNNING)."""

    def __init__(self) -> None:
        self.submissions: list[str] = []

    def submit(self, thread_id: str, job: Callable[[], None]) -> None:
        self.submissions.append(thread_id)  # deliberately do NOT call job()


def _async_settings() -> Settings:
    return Settings(
        use_mocks=True,
        persist_checkpoints=False,
        auth_required=False,
        run_async=True,
        ui_meeting_pace_seconds=0.0,
    )


@pytest.fixture
def holding(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, _HoldingExecutor]]:
    settings = _async_settings()
    settings_module.get_settings.cache_clear()
    for mod in (settings_module, graph_module, identity_module):
        monkeypatch.setattr(mod, "get_settings", lambda: settings)
    execution_module._REGISTRY = RunRegistry()  # noqa: SLF001 - test reset
    executor = _HoldingExecutor()
    execution_module._EXECUTOR = executor  # noqa: SLF001 - inject fake
    graph_module.reset_memory_saver()
    with TestClient(create_app()) as client:
        yield client, executor
    execution_module._REGISTRY = None  # noqa: SLF001
    execution_module._EXECUTOR = None  # noqa: SLF001
    graph_module.reset_memory_saver()


def test_second_start_during_running_does_not_dispatch_again(
    holding: tuple[TestClient, _HoldingExecutor],
) -> None:
    client, executor = holding
    tid = "t-idem-running"

    first = client.post("/api/v1/runs", json={"tdb_code": DISTRESSED_CODE, "thread_id": tid})
    assert first.status_code == 200, first.text
    assert first.json()["phase"] == "running"

    # Second POST with the SAME thread_id while the first is still RUNNING.
    second = client.post("/api/v1/runs", json={"tdb_code": DISTRESSED_CODE, "thread_id": tid})
    assert second.status_code == 200, second.text
    assert second.json()["phase"] == "running"

    # The executor must have been asked to run the job exactly ONCE.
    assert executor.submissions == [tid]
