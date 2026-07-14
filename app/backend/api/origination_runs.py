"""HTTP API for driving the loan-origination graph (融資組成 as a service).

The origination counterpart to :mod:`app.backend.api.runs`. Where that surface
drives the post-origination *assessment / turnaround* graph, this one drives the
:mod:`app.backend.graph_origination` graph — a new facility application from
申込 (APPLIED) through the banker's 稟議 credit decision to 実行 (DISBURSED) or
謝絖 (DECLINED).

It deliberately REUSES the shared machinery of the assessment surface so the two
behave identically and stay consistent:

* the same authenticated identity seam (``require_identity``),
* the same in-process run registry + executor (idempotency, async dispatch,
  background-failure -> ``phase="error"``),
* the same ``RunSnapshot`` shape, phase vocabulary, and JSON-safe coercion,
* the same checkpointer (``make_checkpointer``), so the 稟議 ``interrupt()`` pause
  persists exactly like the turnaround HITL pause.

Endpoints (prefix ``/api/v1``):

* ``POST /origination``                       — start (or idempotently return) an
  origination run for a 7-digit TDB code; drives to the 稟議 interrupt (or to a
  terminal state). Honours ``Settings.run_async``.
* ``GET  /origination/{thread_id}``           — read the current snapshot +
  whether it is awaiting the credit decision.
* ``POST /origination/{thread_id}/decision``  — resume the paused run with the
  banker's credit decision (``approve`` / ``decline``).

The credit decision is HITL-gated in the graph; this surface only transports it.
The banker remains the only decider.
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
from app.backend.graph_origination import compile_origination_graph
from app.shared import settings as settings_module
from app.shared.logging import get_logger

__all__ = ["router"]

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

_log = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["origination"])

#: Credit decisions the origination resume endpoint accepts.
_VALID_DECISIONS: frozenset[str] = frozenset({"approve", "decline"})


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class StartOriginationRequest(BaseModel):
    """Body for starting an origination run."""

    tdb_code: str = Field(description="7-digit TDB 企業コード of the applicant.")
    collateral_value: int = Field(
        default=0,
        ge=0,
        description=(
            "Optional pledged collateral value (担保評価額) in integer yen. Feeds "
            "ONLY the advisory collateral-coverage check on the recommendation; "
            "never feeds a gate, route, or the recommended facility. 0 (treated "
            "as no collateral) by default."
        ),
    )
    guarantee_coverage: int = Field(
        default=0,
        ge=0,
        description=(
            "Optional guaranteed portion (保証カバー額) in integer yen. Feeds ONLY "
            "the advisory collateral-coverage check; never feeds a gate, route, "
            "or the recommended facility. 0 (treated as no guarantee) by default."
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


class OriginationDecisionRequest(BaseModel):
    """Body for resuming a paused origination run with the credit decision."""

    decision: str = Field(description="One of: approve | decline.")


# ---------------------------------------------------------------------------
# Graph helpers (bound to the origination graph; run in a threadpool)
# ---------------------------------------------------------------------------


def _code_valid(tdb_code: str) -> bool:
    """Return whether a TDB code is a well-formed 7-digit string."""
    return tdb_code.isdigit() and len(tdb_code) == 7


def _snapshot(thread_id: str) -> tuple[bool, dict[str, Any]]:
    """Read (awaiting_decision, json-safe values) for an origination thread."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_origination_graph(checkpointer=cp)
        state = graph_app.get_state(config)
    awaiting = bool(state.next)
    return awaiting, _json_safe(dict(state.values))


def _has_state(thread_id: str) -> bool:
    """Return whether any checkpoint already exists for this thread (idempotency)."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_origination_graph(checkpointer=cp)
        state = graph_app.get_state(config)
    return bool(state.values)


def _run_to_pause(payload: dict[str, Any], thread_id: str, *, resume: bool) -> None:
    """Drive the origination graph to the next interrupt / completion (blocking)."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    command: Any = Command(resume=payload) if resume else payload
    with make_checkpointer() as cp:
        graph_app = compile_origination_graph(checkpointer=cp)
        graph_app.invoke(command, config=config)


def _dispatch_async(thread_id: str, payload: dict[str, Any], *, resume: bool) -> RunSnapshot:
    """Mark the run RUNNING, submit the graph job off the request path, return.

    Mirrors ``app.backend.api.runs._dispatch_async`` exactly, but bound to the
    origination graph helpers. Returns immediately with ``phase="running"`` so
    the client polls ``GET /origination/{thread_id}``.
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


@router.post("/origination", response_model=RunSnapshot)
async def start_origination(body: StartOriginationRequest, identity: _IdentityDep) -> RunSnapshot:
    """Start (or idempotently return) an origination run for a TDB code.

    Drives the origination graph to the 稟議 credit-decision interrupt (or to a
    terminal state). Idempotent on ``thread_id`` across BOTH the in-flight
    registry and the durable checkpointer, exactly like the assessment surface.
    Honours ``Settings.run_async`` (sync returns the snapshot; async returns
    ``phase="running"`` and the client polls).
    """
    if not _code_valid(body.tdb_code):
        raise HTTPException(status_code=422, detail="tdb_code must be a 7-digit string")
    thread_id = body.thread_id or str(uuid.uuid4())

    if body.thread_id:
        status = get_run_registry().get(thread_id)
        if status is not None:
            registry_view = _registry_snapshot(thread_id, status)
            if registry_view is not None:
                _log.info(
                    "api.origination.idempotent_hit",
                    thread_id=thread_id,
                    via="registry",
                    phase=registry_view.phase,
                )
                return registry_view
        if await run_in_threadpool(_has_state, thread_id):
            awaiting, values = await run_in_threadpool(_snapshot, thread_id)
            _log.info("api.origination.idempotent_hit", thread_id=thread_id, via="checkpoint")
            return RunSnapshot(
                thread_id=thread_id,
                awaiting_decision=awaiting,
                phase=_phase_for(awaiting),
                values=values,
            )

    _log.info("api.origination.start", thread_id=thread_id, tenant=identity.tenant_id)
    payload: dict[str, Any] = {"tdb_code": body.tdb_code}
    # Optional underwriting coverage figures feed ONLY the advisory
    # collateral-coverage check (the breadth twin of the debt-capacity check).
    # Pass them through to the graph invoke when supplied; omitted -> the state
    # defaults (0) keep the snapshot byte-stable and band the facility as the
    # prudent 'uncovered'. They never feed a gate, route, or the ceiling.
    if body.collateral_value > 0:
        payload["collateral_value"] = int(body.collateral_value)
    if body.guarantee_coverage > 0:
        payload["guarantee_coverage"] = int(body.guarantee_coverage)

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


@router.get("/origination/{thread_id}", response_model=RunSnapshot)
async def get_origination(thread_id: str, identity: _IdentityDep) -> RunSnapshot:
    """Read the current snapshot for an origination run (404 when unknown).

    A RUNNING registry status is authoritative over any intermediate checkpoint
    (the same race fix as the assessment surface): report ``running`` until the
    background job records its terminal phase. ERROR is registry-only. Otherwise
    the durable snapshot is read.
    """
    status = get_run_registry().get(thread_id)
    has_state = await run_in_threadpool(_has_state, thread_id)
    if status is None and not has_state:
        raise HTTPException(status_code=404, detail="origination run not found")

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


@router.post("/origination/{thread_id}/decision", response_model=RunSnapshot)
async def decide_origination(
    thread_id: str, body: OriginationDecisionRequest, identity: _IdentityDep
) -> RunSnapshot:
    """Resume a paused origination run with the banker's credit decision.

    Validates the decision (approve / decline), confirms the run exists and is
    actually paused at the 稟議 interrupt, then resumes via ``Command(resume=...)``.
    Resuming a run that is not awaiting a decision is a 409. Honours
    ``Settings.run_async`` exactly like :func:`start_origination`.
    """
    decision = body.decision.strip().lower()
    if decision not in _VALID_DECISIONS:
        raise HTTPException(
            status_code=422,
            detail=f"decision must be one of {sorted(_VALID_DECISIONS)}",
        )
    if not await run_in_threadpool(_has_state, thread_id):
        raise HTTPException(status_code=404, detail="origination run not found")
    awaiting, _ = await run_in_threadpool(_snapshot, thread_id)
    if not awaiting:
        raise HTTPException(status_code=409, detail="origination run is not awaiting a decision")

    _log.info(
        "api.origination.decision",
        thread_id=thread_id,
        decision=decision,
        tenant=identity.tenant_id,
    )
    # The HITL node reads the decision + records the actor; pass the resolved
    # banker id so the credit-decision event and audit are attributed correctly.
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
