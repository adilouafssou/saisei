"""HTTP API for driving the loan-servicing graph (貸出管理 as a service).

The servicing counterpart to :mod:`app.backend.api.runs` (assessment / turnaround)
and :mod:`app.backend.api.origination_runs` (融資組成). It drives the
:mod:`app.backend.graph_servicing` graph, which records a deterministic,
**non-distress** lifecycle transition along the performing arc of a facility:

* ``confirm`` — 実行 → 正常 (DISBURSED → PERFORMING): a drawn-down facility enters
  normal servicing.
* ``repay``   — 正常 → 完済 (PERFORMING → CLOSED): full repayment.

It REUSES the shared machinery of the assessment surface so all three behave
identically and stay consistent:

* the same authenticated identity seam (``require_identity``),
* the same in-process run registry + executor (idempotency, async dispatch,
  background-failure -> ``phase="error"``),
* the same ``RunSnapshot`` shape, phase vocabulary, and JSON-safe coercion,
* the same checkpointer (``make_checkpointer``).

CRUCIAL DIFFERENCE from the other two surfaces: a servicing transition is an
operational FACT, not a banker-authority credit / distress decision (the
servicing transitions are disjoint from ``HITL_GATED_TRANSITIONS``). So the
servicing graph NEVER interrupts and there is **no resume / decision endpoint** —
the single ``POST`` drives the graph straight to completion. The credit and
distress decisions remain HITL-gated on their own surfaces; nothing here lets a
caller move a facility into 条件変更 / 管理回収 / 償却.

Endpoints (prefix ``/api/v1``):

* ``POST /servicing``               — start (or idempotently return) a servicing
  run that records the requested transition. Honours ``Settings.run_async``.
* ``GET  /servicing/{thread_id}``   — read the current snapshot.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.backend.api.execution import (
    RunPhase,
    get_run_executor,
    get_run_registry,
)
from app.backend.api.runs import (
    RunSnapshot,
    _IdentityDep,
    _json_safe,
    _phase_for,
    _registry_snapshot,
)
from app.backend.graph import make_checkpointer
from app.backend.graph_servicing import compile_servicing_graph
from app.backend.nodes.servicing import SERVICING_ACTIONS
from app.shared import settings as settings_module
from app.shared.logging import get_logger

__all__ = ["router"]

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

_log = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["servicing"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class StartServicingRequest(BaseModel):
    """Body for starting a servicing run against an attached facility."""

    loan_id: str = Field(description="Stable identifier of the facility to service.")
    action: str = Field(
        description=(
            "The servicing action: 'confirm' (実行→正常), 'repay_amount' (一部入金, "
            "a partial paydown of ``amount`` yen), or 'repay' (完済, full payoff)."
        )
    )
    amount: int = Field(
        default=0,
        ge=0,
        description=(
            "Principal to repay for a 'repay_amount' action (一部入金, integer yen "
            ">= 0). Ignored by 'confirm' and 'repay'."
        ),
    )
    lender_stakes: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Optional per-lender outstanding balances (JPY) giving the facility's "
            "principal baseline a repayment draws down. Required for a repayment "
            "(repay / repay_amount) when the durable ledger does not yet carry "
            "the principal; ignored by 'confirm'."
        ),
    )
    thread_id: str | None = Field(
        default=None,
        description=(
            "Optional caller-supplied idempotency key. If a run already exists "
            "for this thread_id, its current snapshot is returned unchanged. "
            "Omit to have the server generate one."
        ),
    )


# ---------------------------------------------------------------------------
# Graph helpers (bound to the servicing graph; run in a threadpool)
# ---------------------------------------------------------------------------


def _snapshot(thread_id: str) -> tuple[bool, dict[str, Any]]:
    """Read (awaiting_decision, json-safe values) for a servicing thread.

    ``awaiting_decision`` is always False for servicing (the graph never pauses),
    but the shared snapshot shape is preserved for consistency with the other
    two surfaces.
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_servicing_graph(checkpointer=cp)
        state = graph_app.get_state(config)
    awaiting = bool(state.next)
    return awaiting, _json_safe(dict(state.values))


def _has_state(thread_id: str) -> bool:
    """Return whether any checkpoint already exists for this thread (idempotency)."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_servicing_graph(checkpointer=cp)
        state = graph_app.get_state(config)
    return bool(state.values)


def _run(payload: dict[str, Any], thread_id: str) -> None:
    """Drive the servicing graph to completion (blocking). Never resumes."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_servicing_graph(checkpointer=cp)
        graph_app.invoke(payload, config=config)  # type: ignore[call-overload]


def _dispatch_async(thread_id: str, payload: dict[str, Any]) -> RunSnapshot:
    """Mark the run RUNNING, submit the graph job off the request path, return.

    Mirrors the assessment / origination async dispatch, bound to the servicing
    graph. The servicing graph never pauses, so the terminal phase is always
    DONE. Returns immediately with ``phase="running"`` so the client polls
    ``GET /servicing/{thread_id}``.
    """
    registry = get_run_registry()
    registry.set(thread_id, RunPhase.RUNNING)

    def _job() -> None:
        _run(payload, thread_id)
        registry.set(thread_id, RunPhase.DONE)

    get_run_executor().submit(thread_id, _job)
    return RunSnapshot(thread_id=thread_id, awaiting_decision=False, phase="running")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/servicing", response_model=RunSnapshot)
async def start_servicing(body: StartServicingRequest, identity: _IdentityDep) -> RunSnapshot:
    """Start (or idempotently return) a servicing run recording one transition.

    Validates the action ('confirm' / 'repay_amount' / 'repay') and drives the
    servicing graph to completion (it never pauses). A 'repay_amount' requires a
    positive ``amount``. Idempotent on ``thread_id`` across BOTH the in-flight
    registry and the durable checkpointer, exactly like the assessment and
    origination surfaces. Honours ``Settings.run_async``.

    The facility's existing loan log must already be durable in the loan ledger
    (originated / assessed earlier); the servicing graph reads it through the
    same ``SaiseiState`` the other graphs share. The deterministic node records
    a no-op when the requested transition is not legal from the facility's
    current status, so an out-of-order call never corrupts the ledger.
    """
    action = body.action.strip().lower()
    if action not in SERVICING_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"action must be one of {sorted(SERVICING_ACTIONS)}",
        )
    if not body.loan_id.strip():
        raise HTTPException(status_code=422, detail="loan_id must be non-empty")
    if action == "repay_amount" and body.amount <= 0:
        raise HTTPException(
            status_code=422,
            detail="amount must be a positive integer for action 'repay_amount'",
        )
    thread_id = body.thread_id or str(uuid.uuid4())

    if body.thread_id:
        status = get_run_registry().get(thread_id)
        if status is not None:
            registry_view = _registry_snapshot(thread_id, status)
            if registry_view is not None:
                _log.info(
                    "api.servicing.idempotent_hit",
                    thread_id=thread_id,
                    via="registry",
                    phase=registry_view.phase,
                )
                return registry_view
        if await run_in_threadpool(_has_state, thread_id):
            awaiting, values = await run_in_threadpool(_snapshot, thread_id)
            _log.info("api.servicing.idempotent_hit", thread_id=thread_id, via="checkpoint")
            return RunSnapshot(
                thread_id=thread_id,
                awaiting_decision=awaiting,
                phase=_phase_for(awaiting),
                values=values,
            )

    _log.info(
        "api.servicing.start",
        thread_id=thread_id,
        loan_id=body.loan_id,
        action=action,
        tenant=identity.tenant_id,
    )
    payload: dict[str, Any] = {
        "loan_id": body.loan_id,
        "servicing_action": action,
        "servicing_amount": int(body.amount),
    }
    if body.lender_stakes:
        payload["lender_stakes"] = {k: int(v) for k, v in body.lender_stakes.items()}

    if settings_module.get_settings().run_async:
        return _dispatch_async(thread_id, payload)

    await run_in_threadpool(_run, payload, thread_id)
    awaiting, values = await run_in_threadpool(_snapshot, thread_id)
    return RunSnapshot(
        thread_id=thread_id,
        awaiting_decision=awaiting,
        phase=_phase_for(awaiting),
        values=values,
    )


@router.get("/servicing/{thread_id}", response_model=RunSnapshot)
async def get_servicing(thread_id: str, identity: _IdentityDep) -> RunSnapshot:
    """Read the current snapshot for a servicing run (404 when unknown).

    A RUNNING registry status is authoritative over any intermediate checkpoint
    (the same race fix as the other surfaces); ERROR is registry-only. Otherwise
    the durable snapshot is read. The servicing graph never pauses, so a
    completed run reports ``done``.
    """
    status = get_run_registry().get(thread_id)
    has_state = await run_in_threadpool(_has_state, thread_id)
    if status is None and not has_state:
        raise HTTPException(status_code=404, detail="servicing run not found")

    if status is not None:
        if status.phase is RunPhase.RUNNING:
            return RunSnapshot(thread_id=thread_id, awaiting_decision=False, phase="running")
        if status.phase is RunPhase.ERROR:
            return RunSnapshot(
                thread_id=thread_id,
                awaiting_decision=False,
                phase="error",
                error=status.error,
            )

    awaiting, values = await run_in_threadpool(_snapshot, thread_id)
    return RunSnapshot(
        thread_id=thread_id,
        awaiting_decision=awaiting,
        phase=_phase_for(awaiting),
        values=values,
    )
