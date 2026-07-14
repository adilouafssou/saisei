"""End-to-end verifier for the loan-servicing HTTP API.

The servicing counterpart to ``tests/test_api_origination.py``: it exercises the
*real* FastAPI surface (``app.app.create_app``) via Starlette's ``TestClient``,
against the *real* servicing graph (``app.backend.graph_servicing``), fully
OFFLINE and SYNCHRONOUS.

Servicing arrives over HTTP as just ``{loan_id, action}``; the facility's loan
log lives in the durable loan ledger from an earlier origination / assessment.
So the test wires a single shared in-memory loan store (pre-seeded with a
DISBURSED facility) into the one ``get_loan_store(settings.loan_dsn)`` seam the
servicing intake reads and the servicing node persists to, plus a non-empty
loan DSN so it is not the offline NullLoanStore path. This mirrors
``tests/test_origination_lifecycle_e2e``'s wiring.

What is pinned:
- POST /servicing 'confirm' advances the durable facility 実行 → 正常 and persists it;
- a follow-up POST /servicing 'repay' closes it 正常 → 完済;
- GET /servicing/{thread_id} reads the snapshot back;
- an unknown action is a 422; an unknown thread is a 404;
- there is NO resume endpoint (servicing never pauses): the run is 'done' inline.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import app.backend.graph as graph_module
import app.backend.identity as identity_module
import app.backend.portfolio.loan_store_postgres as loan_store_module
import app.shared.settings as settings_module
import pytest
from app.app import create_app
from app.shared.models.loan import LoanEvent, LoanStatus, current_status
from app.shared.settings import Settings
from fastapi.testclient import TestClient

_LOAN_ID = "L-test-facility"
_TENANT = "t"
_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)

_DISBURSED_CHAIN = (
    LoanStatus.APPLIED,
    LoanStatus.UNDER_REVIEW,
    LoanStatus.APPROVED,
    LoanStatus.DISBURSED,
)


class _InMemoryLoanStore:
    """A real (in-memory) append-only loan store shared across requests."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], list[LoanEvent]] = {}

    def seed(self, loan_id: str, *statuses: LoanStatus) -> None:
        self._by_key[(_TENANT, loan_id)] = [
            LoanEvent(status=s, at=_AT + dt.timedelta(days=i), actor="system")
            for i, s in enumerate(statuses)
        ]

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        self._by_key.setdefault((tenant_id, loan_id), []).append(event)

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        return list(self._by_key.get((tenant_id, loan_id), []))


def _offline_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "use_mocks": True,
        "persist_checkpoints": False,
        "run_async": False,
        "auth_required": False,
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
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)
    monkeypatch.setattr(identity_module, "get_settings", lambda: settings)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _InMemoryLoanStore:
    """Wire a single shared in-memory loan store + a non-empty loan DSN.

    Both the servicing intake (read) and the servicing node (persist) call
    ``get_loan_store(settings.loan_dsn)`` and read ``loan_tenant_default``
    lazily, so patching the source module makes every call site share THIS
    store. A SimpleNamespace settings carrying a truthy loan_dsn keeps it off
    the offline NullLoanStore path. The store is pre-seeded with a DISBURSED
    facility for the servicing surface to act on.
    """
    s = _InMemoryLoanStore()
    s.seed(_LOAN_ID, *_DISBURSED_CHAIN)
    monkeypatch.setattr(loan_store_module, "get_loan_store", lambda dsn: s)
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(
            loan_dsn="postgresql://x",
            loan_tenant_default=_TENANT,
            run_async=False,
        ),
    )
    return s


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _install_settings(monkeypatch, _offline_settings())
    graph_module.reset_memory_saver()
    with TestClient(create_app()) as test_client:
        yield test_client
    graph_module.reset_memory_saver()


def _current(values: dict[str, Any]) -> LoanStatus:
    events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    return current_status(events)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_unknown_action_is_422(client: TestClient) -> None:
    resp = client.post("/api/v1/servicing", json={"loan_id": _LOAN_ID, "action": "refinance"})
    assert resp.status_code == 422, resp.text


def test_empty_loan_id_is_422(client: TestClient) -> None:
    resp = client.post("/api/v1/servicing", json={"loan_id": "", "action": "confirm"})
    assert resp.status_code == 422, resp.text


def test_unknown_thread_get_is_404(client: TestClient) -> None:
    resp = client.get("/api/v1/servicing/does-not-exist")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# the servicing arc over HTTP (durable ledger wired)
# ---------------------------------------------------------------------------


def test_confirm_advances_disbursed_to_performing(
    client: TestClient, store: _InMemoryLoanStore
) -> None:
    resp = client.post(
        "/api/v1/servicing",
        json={"loan_id": _LOAN_ID, "action": "confirm", "thread_id": "svc-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Servicing never pauses: the run completes inline.
    assert body["phase"] == "done"
    assert body["awaiting_decision"] is False
    assert _current(body["values"]) is LoanStatus.PERFORMING
    # And the transition was persisted to the durable ledger.
    assert current_status(store.read(_TENANT, _LOAN_ID)) is LoanStatus.PERFORMING


def test_repay_closes_a_performing_facility(client: TestClient, store: _InMemoryLoanStore) -> None:
    # Move it to PERFORMING first, then repay -> CLOSED. A repayment needs a
    # principal baseline; over HTTP the caller supplies the facility's
    # lender_stakes in the start payload (the graph accepts arbitrary state).
    client.post(
        "/api/v1/servicing",
        json={"loan_id": _LOAN_ID, "action": "confirm", "thread_id": "svc-a"},
    )
    resp = client.post(
        "/api/v1/servicing",
        json={
            "loan_id": _LOAN_ID,
            "action": "repay",
            "thread_id": "svc-b",
            "lender_stakes": {"main_bank": 100_000_000},
        },
    )
    assert resp.status_code == 200, resp.text
    assert _current(resp.json()["values"]) is LoanStatus.CLOSED
    assert current_status(store.read(_TENANT, _LOAN_ID)) is LoanStatus.CLOSED


def test_repay_amount_requires_positive_amount(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/servicing",
        json={"loan_id": _LOAN_ID, "action": "repay_amount", "amount": 0},
    )
    assert resp.status_code == 422, resp.text


def test_repay_amount_partial_paydown_stays_performing(
    client: TestClient, store: _InMemoryLoanStore
) -> None:
    # Confirm to PERFORMING, then a partial paydown: stays 正常, balance lower.
    client.post(
        "/api/v1/servicing",
        json={"loan_id": _LOAN_ID, "action": "confirm", "thread_id": "svc-pa"},
    )
    resp = client.post(
        "/api/v1/servicing",
        json={
            "loan_id": _LOAN_ID,
            "action": "repay_amount",
            "amount": 30_000_000,
            "thread_id": "svc-pb",
            "lender_stakes": {"main_bank": 100_000_000},
        },
    )
    assert resp.status_code == 200, resp.text
    # Still performing (partial paydown), and the durable ledger recorded the
    # 一部入金 self-event.
    assert current_status(store.read(_TENANT, _LOAN_ID)) is LoanStatus.PERFORMING
    repaid = sum(e.principal_repaid for e in store.read(_TENANT, _LOAN_ID))
    assert repaid == 30_000_000


def test_get_reads_back_the_snapshot(client: TestClient, store: _InMemoryLoanStore) -> None:
    client.post(
        "/api/v1/servicing",
        json={"loan_id": _LOAN_ID, "action": "confirm", "thread_id": "svc-get"},
    )
    resp = client.get("/api/v1/servicing/svc-get")
    assert resp.status_code == 200, resp.text
    assert _current(resp.json()["values"]) is LoanStatus.PERFORMING


def test_start_is_idempotent_on_thread_id(client: TestClient, store: _InMemoryLoanStore) -> None:
    first = client.post(
        "/api/v1/servicing",
        json={"loan_id": _LOAN_ID, "action": "confirm", "thread_id": "svc-idem"},
    )
    assert first.status_code == 200, first.text
    # A repeat call with the same thread_id returns the existing snapshot and
    # does NOT record a second transition.
    second = client.post(
        "/api/v1/servicing",
        json={"loan_id": _LOAN_ID, "action": "confirm", "thread_id": "svc-idem"},
    )
    assert second.status_code == 200, second.text
    assert _current(second.json()["values"]) is LoanStatus.PERFORMING
    # Ledger advanced exactly once past the seeded DISBURSED chain.
    persisted = store.read(_TENANT, _LOAN_ID)
    assert [e.status for e in persisted] == [
        *_DISBURSED_CHAIN,
        LoanStatus.PERFORMING,
    ]
