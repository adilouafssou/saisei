"""HTTP API for driving the loan-distress graph (条件変更 / 償却 as a service).

The distress counterpart to :mod:`app.backend.api.origination_runs`. Where that
surface drives the 融資組成 graph (申込 -> 実行 / 謝絶) and
:mod:`app.backend.api.servicing_runs` drives the non-distress 貸出管理 graph
straight to completion, this one drives the :mod:`app.backend.graph_distress`
graph — a HITL-gated distress move (条件変更 / 償却) on an attached facility.

Like origination (and UNLIKE servicing) the distress graph INTERRUPTS for the
banker's decision, so this surface has a resume / decision endpoint. It REUSES
the shared machinery of the assessment / origination surfaces so all behave
identically and stay consistent:

* the same authenticated identity seam (``require_identity``),
* the same in-process run registry + executor (idempotency, async dispatch,
  background-failure -> ``phase="error"``),
* the same ``RunSnapshot`` shape, phase vocabulary, and JSON-safe coercion,
* the same checkpointer (``make_checkpointer``), so the distress ``interrupt()``
  pause persists exactly like the 稟議 / turnaround HITL pause.

Endpoints (prefix ``/api/v1``):

* ``POST /distress``                       — start (or idempotently return) a
  distress run for an attached facility; drives to the distress interrupt (or a
  terminal state). Honours ``Settings.run_async``.
* ``GET  /distress/{thread_id}``           — read the current snapshot + whether
  it is awaiting the distress decision.
* ``POST /distress/{thread_id}/decision``  — resume the paused run with the
  banker's distress decision (``proceed`` / ``abort``).

The distress transition is HITL-gated in the graph; this surface only transports
the decision. The banker remains the only decider.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from langgraph.types import Command
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
from app.backend.graph_distress import DISTRESS_ACTIONS, compile_distress_graph
from app.shared import settings as settings_module
from app.shared.logging import get_logger

__all__ = ["router"]

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

_log = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["distress"])

#: Distress decisions the resume endpoint accepts.
_VALID_DECISIONS: frozenset[str] = frozenset({"proceed", "abort"})


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class StartDistressRequest(BaseModel):
    """Body for starting a distress run against an attached facility."""

    loan_id: str = Field(description="Stable identifier of the facility to act on.")
    action: str = Field(
        description=(
            "The distress action: 'restructure' (条件変更, PERFORMING -> "
            "RESTRUCTURED) or 'writeoff' (償却, WORKOUT -> WRITTEN_OFF)."
        )
    )
    grace_months: int = Field(
        default=0,
        ge=0,
        description=(
            "Proposed principal grace period (元本返済猟予) in months for a "
            "'restructure' action. Feeds only the advisory self-curing check; "
            "ignored by 'writeoff'."
        ),
    )
    rate_reduction_bps: int = Field(
        default=0,
        ge=0,
        description=(
            "Proposed lending-rate reduction in basis points for a 'restructure' "
            "action (e.g. 200 = 2.00%). Feeds only the advisory self-curing "
            "check; ignored by 'writeoff'."
        ),
    )
    lender_stakes: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Optional per-lender outstanding balances (JPY) giving the "
            "facility's principal baseline. Used by the deterministic relief / "
            "charged-off arithmetic when the durable ledger does not yet carry "
            "the principal."
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


class DistressDecisionRequest(BaseModel):
    """Body for resuming a paused distress run with the banker's decision."""

    decision: str = Field(description="One of: proceed | abort.")


# ---------------------------------------------------------------------------
# Graph helpers (bound to the distress graph; run in a threadpool)
# ---------------------------------------------------------------------------


def _snapshot(thread_id: str) -> tuple[bool, dict[str, Any]]:
    """Read (awaiting_decision, json-safe values) for a distress thread."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_distress_graph(checkpointer=cp)
        state = graph_app.get_state(config)
    awaiting = bool(state.next)
    return awaiting, _json_safe(dict(state.values))


def _has_state(thread_id: str) -> bool:
    """Return whether any checkpoint already exists for this thread (idempotency)."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_distress_graph(checkpointer=cp)
        state = graph_app.get_state(config)
    return bool(state.values)


def _run_to_pause(payload: dict[str, Any], thread_id: str, *, resume: bool) -> None:
    """Drive the distress graph to the next interrupt / completion (blocking)."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    command: Any = Command(resume=payload) if resume else payload
    with make_checkpointer() as cp:
        graph_app = compile_distress_graph(checkpointer=cp)
        graph_app.invoke(command, config=config)


def _dispatch_async(thread_id: str, payload: dict[str, Any], *, resume: bool) -> RunSnapshot:
    """Mark the run RUNNING, submit the graph job off the request path, return.

    Mirrors ``app.backend.api.origination_runs._dispatch_async`` exactly, bound
    to the distress graph helpers. Returns immediately with ``phase="running"``
    so the client polls ``GET /distress/{thread_id}``.
    """
    registry = get_run_registry()
    registry.set(thread_id, RunPhase.RUNNING)

    def _job() -> None:
        _run_to_pause(payload, thread_id, resume=resume)
        awaiting, _ = _snapshot(thread_id)
        registry.set(
            thread_id,
            RunPhase.AWAITING_DECISION if awaiting else RunPhase.DONE,
        )

    get_run_executor().submit(thread_id, _job)
    return RunSnapshot(thread_id=thread_id, awaiting_decision=False, phase="running")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/distress", response_model=RunSnapshot)
async def start_distress(body: StartDistressRequest, identity: _IdentityDep) -> RunSnapshot:
    """Start (or idempotently return) a distress run for an attached facility.

    Validates the action ('restructure' / 'writeoff') and drives the distress
    graph to its HITL interrupt (or a terminal state). Idempotent on
    ``thread_id`` across BOTH the in-flight registry and the durable
    checkpointer, exactly like the origination surface. Honours
    ``Settings.run_async``.

    The facility's existing loan log must already be durable in the loan ledger
    (originated / assessed earlier); the distress graph reads it through the same
    ``SaiseiState`` the other graphs share. The deterministic nodes record a
    no-op transition when the requested move is not legal from the facility's
    current status, so an out-of-order call never corrupts the ledger — the
    advisory verdict is still surfaced.
    """
    action = body.action.strip().lower()
    if action not in DISTRESS_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"action must be one of {sorted(DISTRESS_ACTIONS)}",
        )
    if not body.loan_id.strip():
        raise HTTPException(status_code=422, detail="loan_id must be non-empty")
    thread_id = body.thread_id or str(uuid.uuid4())

    if body.thread_id:
        status = get_run_registry().get(thread_id)
        if status is not None:
            registry_view = _registry_snapshot(thread_id, status)
            if registry_view is not None:
                _log.info(
                    "api.distress.idempotent_hit",
                    thread_id=thread_id,
                    via="registry",
                    phase=registry_view.phase,
                )
                return registry_view
        if await run_in_threadpool(_has_state, thread_id):
            awaiting, values = await run_in_threadpool(_snapshot, thread_id)
            _log.info("api.distress.idempotent_hit", thread_id=thread_id, via="checkpoint")
            return RunSnapshot(
                thread_id=thread_id,
                awaiting_decision=awaiting,
                phase=_phase_for(awaiting),
                values=values,
            )

    _log.info(
        "api.distress.start",
        thread_id=thread_id,
        loan_id=body.loan_id,
        action=action,
        tenant=identity.tenant_id,
    )
    payload: dict[str, Any] = {
        "loan_id": body.loan_id,
        "distress_action": action,
    }
    if action == "restructure":
        payload["restructure_grace_months"] = int(body.grace_months)
        payload["restructure_rate_reduction_bps"] = int(body.rate_reduction_bps)
    if body.lender_stakes:
        payload["lender_stakes"] = {k: int(v) for k, v in body.lender_stakes.items()}

    if settings_module.get_settings().run_async:
        return _dispatch_async(thread_id, payload, resume=False)

    await run_in_threadpool(_run_to_pause, payload, thread_id, resume=False)
    awaiting, values = await run_in_threadpool(_snapshot, thread_id)
    return RunSnapshot(
        thread_id=thread_id,
        awaiting_decision=awaiting,
        phase=_phase_for(awaiting),
        values=values,
    )


@router.get("/distress/{thread_id}", response_model=RunSnapshot)
async def get_distress(thread_id: str, identity: _IdentityDep) -> RunSnapshot:
    """Read the current snapshot for a distress run (404 when unknown).

    A RUNNING registry status is authoritative over any intermediate checkpoint
    (the same race fix as the other surfaces); ERROR is registry-only. Otherwise
    the durable snapshot is read.
    """
    status = get_run_registry().get(thread_id)
    has_state = await run_in_threadpool(_has_state, thread_id)
    if status is None and not has_state:
        raise HTTPException(status_code=404, detail="distress run not found")

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


@router.post("/distress/{thread_id}/decision", response_model=RunSnapshot)
async def decide_distress(
    thread_id: str, body: DistressDecisionRequest, identity: _IdentityDep
) -> RunSnapshot:
    """Resume a paused distress run with the banker's proceed / abort decision.

    Validates the decision (proceed / abort), confirms the run exists and is
    actually paused at the distress interrupt, then resumes via
    ``Command(resume=...)``. Resuming a run that is not awaiting a decision is a
    409. The resolved banker id is passed so the gated transition event and
    audit are attributed correctly. Honours ``Settings.run_async`` exactly like
    :func:`start_distress`.
    """
    decision = body.decision.strip().lower()
    if decision not in _VALID_DECISIONS:
        raise HTTPException(
            status_code=422,
            detail=f"decision must be one of {sorted(_VALID_DECISIONS)}",
        )
    if not await run_in_threadpool(_has_state, thread_id):
        raise HTTPException(status_code=404, detail="distress run not found")
    awaiting, _ = await run_in_threadpool(_snapshot, thread_id)
    if not awaiting:
        raise HTTPException(status_code=409, detail="distress run is not awaiting a decision")

    _log.info(
        "api.distress.decision",
        thread_id=thread_id,
        decision=decision,
        tenant=identity.tenant_id,
    )
    # The distress nodes resolve the banker via the identity seam; pass the
    # resolved actor so the gated transition event is attributed correctly.
    payload = {"decision": decision, "actor": identity.actor}

    if settings_module.get_settings().run_async:
        return _dispatch_async(thread_id, payload, resume=True)

    await run_in_threadpool(_run_to_pause, payload, thread_id, resume=True)
    awaiting, values = await run_in_threadpool(_snapshot, thread_id)
    return RunSnapshot(
        thread_id=thread_id,
        awaiting_decision=awaiting,
        phase=_phase_for(awaiting),
        values=values,
    )
