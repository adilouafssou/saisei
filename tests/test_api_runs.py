"""End-to-end verifier for the run/resume HTTP API (productionisation slice).

This is the API-level companion to ``tests/test_graph_flow.py``: instead of
driving the compiled graph directly, it exercises the *real* FastAPI surface
(``app.app.create_app``) via Starlette's ``TestClient``, against the *real*
Saisei graph, in fully OFFLINE mode. Nothing is mocked except the runtime
configuration:

* ``use_mocks=True``            -> the deterministic mock data engine (no network).
* ``persist_checkpoints=False`` -> the process-wide ``MemorySaver`` singleton
  (no Postgres). Because ``graph.make_checkpointer`` returns that SAME singleton
  for every call when persistence is off, a run started by one request is
  readable / resumable by the next request -- which is exactly the durability
  contract the API promises (start -> get -> resume across HTTP calls).
* ``auth_required=False``       -> the placeholder identity is permitted, so the
  routes run; the auth-guard test flips this to True to assert the 401 seam.

The Settings are forced offline by constructing a single cached ``Settings``
instance and patching ``get_settings`` everywhere the name is *bound at import
time*. ``app.backend.graph`` and ``app.backend.identity`` both do
``from app.shared.settings import get_settings`` (a bound reference), so patching
only ``app.shared.settings.get_settings`` would miss them; we patch all three
modules' references and clear the lru_cache for good measure. This keeps the
graph's checkpointer selection, the HITL interrupt, and the identity seam all
running their real production code paths.

Borrower fixtures used (see ``app/backend/tools/tdb_api._FIXTURE_INDEX``):
* ``1234567`` (aichi_manufacturer) -- a deteriorating SME that classifies
  要注意先/破綻懸念先 and therefore reaches the HITL interrupt. With the default
  (False) commitment flags the creditor meeting consolidates to ``needs_human``
  and the graph STILL routes to HITL (it does not escalate), so the API start
  call pauses at ``awaiting_decision`` -- the documented behaviour pinned by
  ``tests/test_graph_flow.test_graph_reaches_hitl_with_default_commitment_flags``.
* ``2000001`` (normal_service_co) -- a healthy, profitable, flat service company
  that classifies 正常先 and routes straight to END, so the run COMPLETES with
  no banker decision required.
"""

from __future__ import annotations

from collections.abc import Iterator

import app.backend.graph as graph_module
import app.backend.identity as identity_module
import app.shared.settings as settings_module
import pytest
from app.app import create_app
from app.shared.settings import Settings
from fastapi.testclient import TestClient

# --- Borrower fixtures (deterministic mock TDB codes) -----------------------

#: A deteriorating SME -> 要注意先/破綻懸念先 -> reaches the HITL interrupt.
DISTRESSED_CODE = "1234567"

#: A healthy, profitable service company -> 正常先 -> completes with no decision.
NORMAL_CODE = "2000001"


# ---------------------------------------------------------------------------
# Offline-settings plumbing
# ---------------------------------------------------------------------------


def _offline_settings(**overrides: object) -> Settings:
    """Build a Settings instance forced fully offline for the API tests.

    ``use_mocks`` and ``persist_checkpoints`` are the two that matter for the
    graph: mocks => no network, no persistence => the shared in-process
    MemorySaver singleton (so state is durable across TestClient requests).
    ``auth_required`` defaults False so the placeholder identity is accepted.
    """
    base: dict[str, object] = {
        "use_mocks": True,
        "persist_checkpoints": False,
        "auth_required": False,
        # Belt-and-braces: keep every live integration unconfigured so a stray
        # client can never reach out over the network from within a test.
        "llm_api_key": "",
        "tdb_api_key": "",
        "audit_dsn": "",
        "portfolio_dsn": "",
        "trajectory_dsn": "",
        "langsmith_tracing": False,
        "ui_meeting_pace_seconds": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _install_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Patch ``get_settings`` to return ``settings`` at every bound call site.

    ``app.backend.graph`` and ``app.backend.identity`` import ``get_settings``
    by name (a bound reference captured at import), and ``app.app`` /
    ``app.backend.api.runs`` call it indirectly through those. We patch all three
    references plus clear the canonical lru_cache so nothing falls through to a
    real environment-derived Settings.
    """
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)
    monkeypatch.setattr(identity_module, "get_settings", lambda: settings)


def _reset_memory_saver() -> None:
    """Drop the process-wide MemorySaver so each test starts with a clean store.

    The singleton is module-global and shared across the whole process, so
    without this reset thread_ids would leak between tests (a run started in one
    test would still 'exist' in the next). Clearing it keeps each test isolated
    while still letting run -> get -> resume share state WITHIN a test. Uses the
    public :func:`graph.reset_memory_saver` seam so the test never depends on a
    module-private global.
    """
    graph_module.reset_memory_saver()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient on a fresh offline app with an isolated in-memory store."""
    _install_settings(monkeypatch, _offline_settings())
    _reset_memory_saver()
    with TestClient(create_app()) as test_client:
        yield test_client
    _reset_memory_saver()


# ---------------------------------------------------------------------------
# Happy path: distressed borrower -> pause -> durable read -> resume -> done
# ---------------------------------------------------------------------------


class TestDistressedRunLifecycle:
    """start (pause at HITL) -> get (same paused run) -> resume(approve) -> done."""

    def test_start_pauses_at_hitl_interrupt(self, client: TestClient) -> None:
        """A distressed borrower start pauses awaiting the banker's decision."""
        resp = client.post(
            "/api/v1/runs",
            json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-distressed"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["thread_id"] == "t-distressed"
        assert body["awaiting_decision"] is True
        assert body["phase"] == "awaiting_decision"
        # The snapshot must carry the proposed strategies the banker reviews.
        assert body["values"]["proposed_strategies"], "strategies must be proposed"
        # And it must be on a turnaround classification, not normal/workout.
        assert body["values"]["fsa_classification"] in ("yochuisaki", "hatan_kenensaki")

    def test_run_is_durable_across_requests(self, client: TestClient) -> None:
        """GET reads the SAME paused run a previous POST started (shared store)."""
        client.post(
            "/api/v1/runs",
            json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-durable"},
        )
        resp = client.get("/api/v1/runs/t-durable")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["thread_id"] == "t-durable"
        assert body["awaiting_decision"] is True
        assert body["phase"] == "awaiting_decision"
        assert body["values"]["proposed_strategies"]

    def test_resume_approve_completes_and_writes_keikakusho(self, client: TestClient) -> None:
        """resume(approve) finishes the run and produces the Keikakusho draft."""
        client.post(
            "/api/v1/runs",
            json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-approve"},
        )
        resp = client.post(
            "/api/v1/runs/t-approve/resume",
            json={"decision": "approve"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["awaiting_decision"] is False
        assert body["phase"] == "done"
        draft = body["values"]["keikakusho_draft"]
        assert draft and "\u7d4c\u55b6\u6539\u5584\u8a08\u753b\u66f8" in draft
        assert body["values"]["approved_strategy"] is not None

    def test_idempotent_start_does_not_start_a_second_run(self, client: TestClient) -> None:
        """A second POST with the same thread_id returns the existing snapshot.

        Idempotency is keyed by the caller-supplied thread_id: once a run exists
        for it, a repeat start must NOT re-run the graph; it returns what the
        thread already has. We verify the repeat hits the existing paused run.
        """
        first = client.post(
            "/api/v1/runs",
            json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-idem"},
        ).json()
        second = client.post(
            "/api/v1/runs",
            json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-idem"},
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["thread_id"] == "t-idem"
        assert body["awaiting_decision"] is True
        # Same run: the proposed strategies are identical to the first response.
        assert body["values"]["proposed_strategies"] == first["values"]["proposed_strategies"]


# ---------------------------------------------------------------------------
# Normal borrower: completes without a banker decision
# ---------------------------------------------------------------------------


class TestNormalRun:
    """A 正常先 borrower runs to completion with no HITL interrupt."""

    def test_normal_borrower_completes_without_decision(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/runs",
            json={"tdb_code": NORMAL_CODE, "thread_id": "t-normal"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["awaiting_decision"] is False
        assert body["phase"] == "done"
        assert body["values"]["fsa_classification"] == "seijosaki"
        # Monitor-only: no strategies proposed, no Keikakusho written.
        assert not body["values"].get("proposed_strategies")
        assert body["values"].get("keikakusho_draft") in (None, "")

    def test_resume_completed_normal_run_is_conflict(self, client: TestClient) -> None:
        """A completed (never-paused) run has nothing to resume -> 409."""
        client.post(
            "/api/v1/runs",
            json={"tdb_code": NORMAL_CODE, "thread_id": "t-normal-409"},
        )
        resp = client.post(
            "/api/v1/runs/t-normal-409/resume",
            json={"decision": "approve"},
        )
        assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# Validation / error surface
# ---------------------------------------------------------------------------


class TestValidationAndErrors:
    """The route guards: 422 bad code, 404 unknown thread, 422 bad decision."""

    @pytest.mark.parametrize(
        "bad_code",
        ["123", "12345678", "abcdefg", "", "123456a"],
    )
    def test_start_rejects_malformed_tdb_code(self, client: TestClient, bad_code: str) -> None:
        resp = client.post("/api/v1/runs", json={"tdb_code": bad_code})
        assert resp.status_code == 422, resp.text

    def test_get_unknown_thread_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/runs/does-not-exist")
        assert resp.status_code == 404, resp.text

    def test_resume_unknown_thread_is_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/runs/does-not-exist/resume",
            json={"decision": "approve"},
        )
        assert resp.status_code == 404, resp.text

    def test_resume_invalid_decision_is_422(self, client: TestClient) -> None:
        """An unknown decision is rejected before the run is even looked up."""
        client.post(
            "/api/v1/runs",
            json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-baddecision"},
        )
        resp = client.post(
            "/api/v1/runs/t-baddecision/resume",
            json={"decision": "maybe"},
        )
        assert resp.status_code == 422, resp.text

    def test_server_generates_thread_id_when_omitted(self, client: TestClient) -> None:
        """Omitting thread_id has the server mint one (still a valid run)."""
        resp = client.post("/api/v1/runs", json={"tdb_code": DISTRESSED_CODE})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["thread_id"]  # a generated id is present
        assert body["awaiting_decision"] is True


# ---------------------------------------------------------------------------
# Auth guard seam: auth_required + placeholder identity -> 401
# ---------------------------------------------------------------------------


class TestAuthGuard:
    """With auth_required set, the placeholder identity is refused at the seam."""

    @pytest.fixture
    def guarded_client(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
        _install_settings(monkeypatch, _offline_settings(auth_required=True))
        _reset_memory_saver()
        with TestClient(create_app(), raise_server_exceptions=False) as test_client:
            yield test_client
        _reset_memory_saver()

    def test_start_is_unauthorized_under_auth_required(self, guarded_client: TestClient) -> None:
        resp = guarded_client.post("/api/v1/runs", json={"tdb_code": DISTRESSED_CODE})
        assert resp.status_code == 401, resp.text

    def test_get_is_unauthorized_under_auth_required(self, guarded_client: TestClient) -> None:
        resp = guarded_client.get("/api/v1/runs/anything")
        assert resp.status_code == 401, resp.text

    def test_resume_is_unauthorized_under_auth_required(self, guarded_client: TestClient) -> None:
        resp = guarded_client.post("/api/v1/runs/anything/resume", json={"decision": "approve"})
        assert resp.status_code == 401, resp.text
