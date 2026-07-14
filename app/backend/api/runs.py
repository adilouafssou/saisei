"""HTTP API for driving the Saisei graph (productionisation, first slice).

Everything to date runs the graph *inside* the Reflex process. For a bank to
integrate Saisei as a service it needs an authenticated, idempotent HTTP surface
to **start**, **read**, and **resume** a borrower assessment. This module is that
first slice:

- ``POST /api/v1/runs`` — start an assessment for a TDB code, driving the graph
  to the human-in-the-loop interrupt (or to completion for a non-distressed
  borrower). **Idempotent**: keyed by ``thread_id`` (caller-supplied or
  generated); if that thread already has state, the existing snapshot is
  returned instead of starting a second run.
- ``GET /api/v1/runs/{thread_id}`` — read the current snapshot + whether the run
  is awaiting a banker decision.
- ``POST /api/v1/runs/{thread_id}/resume`` — resume a paused run with the
  banker's decision (approve / revise / reject) via ``Command(resume=...)``.

Design honesty / scope
----------------------
This slice delivers the *capability* (drive the graph over HTTP, idempotently,
with durable resume) and leaves **authentication and per-bank tenancy as an
explicit, clearly-marked dependency seam** (:func:`require_principal`) rather
than shipping fake security. The OIDC + tenancy implementation drops into that
one seam next, without touching the route bodies. The graph itself is unchanged:
the endpoints reuse ``compile_graph`` / ``make_checkpointer`` exactly as the UI
does, so the deterministic spine, the HITL interrupt, and the audit/grounding
invariants all still hold — the human remains the only decider.

The blocking graph work runs in a threadpool so the async endpoints never block
the event loop (the minimal “long calls off the request path” posture; a full
async worker queue is a later step).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.backend.api.execution import (
    RunPhase,
    RunStatus,
    get_run_executor,
    get_run_registry,
)
from app.backend.auth import (
    AuthError,
    extract_bearer_token,
    oidc_enabled,
    verify_bearer_token,
)
from app.backend.graph import compile_graph, make_checkpointer
from app.backend.identity import (
    Identity,
    IdentityError,
    identity_from_claims,
    require_persistable,
    resolve_identity,
)
from app.shared import settings as settings_module
from app.shared.logging import get_logger

__all__ = ["router", "require_identity"]

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


_log = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["runs"])

#: Decisions the resume endpoint accepts (mirrors the HITL contract).
_VALID_DECISIONS: frozenset[str] = frozenset({"approve", "revise", "reject"})


# ---------------------------------------------------------------------------
# Auth / tenancy — reuses the existing identity seam (the OIDC plug point)
# ---------------------------------------------------------------------------


async def require_identity(
    authorization: Annotated[str | None, Header()] = None,
) -> Identity:
    """Resolve the caller identity for a request via the shared identity seam.

    Two paths, selected by deployment configuration, both ending at the SAME
    seam the Portfolio store and audit ledger already flow through
    (:func:`require_persistable`):

    * **OIDC configured** (``auth_jwks_url`` set): the ``Authorization: Bearer``
      token is VERIFIED against the provider's JWKS (signature + expiry + the
      configured issuer/audience) by :func:`verify_bearer_token`, and the
      verified claims are mapped to a real authenticated :class:`Identity` via
      :func:`identity_from_claims`. A missing / malformed / invalid token is a
      401 -- there is no fallback to a placeholder once OIDC is on.
    * **OIDC not configured** (default / offline / single-tenant demo): the
      configured placeholder identity is returned, exactly as before.

    :func:`require_persistable` then applies the production guard in BOTH paths:
    when ``SAISEI_AUTH_REQUIRED`` is set, an unauthenticated (placeholder)
    identity is refused with 401 rather than silently acting under the shared
    'default' tenant / 'banker' actor.

    Args:
        authorization: The raw ``Authorization`` request header (injected by
            FastAPI); ``None`` when absent.

    Returns:
        The resolved :class:`Identity` (authenticated under OIDC).

    Raises:
        HTTPException: 401 when a presented token is invalid, when OIDC is on
            but no usable token/claims are present, or when the production guard
            rejects an unauthenticated identity.
    """
    try:
        if oidc_enabled():
            token = extract_bearer_token(authorization)
            claims = verify_bearer_token(token)
            identity = identity_from_claims(claims)
        else:
            identity = resolve_identity()
        return require_persistable(identity)
    except (AuthError, IdentityError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


_IdentityDep = Annotated[Identity, Depends(require_identity)]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class StartRunRequest(BaseModel):
    """Body for starting an assessment run."""

    tdb_code: str = Field(description="7-digit TDB 企業コード.")
    thread_id: str | None = Field(
        default=None,
        description=(
            "Optional caller-supplied idempotency key. If a run already exists "
            "for this thread_id, its current snapshot is returned unchanged. "
            "Omit to have the server generate one."
        ),
    )


class ResumeRunRequest(BaseModel):
    """Body for resuming a paused run with the banker's decision."""

    decision: str = Field(description="One of: approve | revise | reject.")
    revision_note: str = Field(
        default="", description="Optional banker note (used by revise / reject)."
    )


class RunSnapshot(BaseModel):
    """A JSON-safe view of a run's current state."""

    thread_id: str
    awaiting_decision: bool = Field(
        description="True when the run is paused at the HITL interrupt."
    )
    phase: str = Field(description="running | awaiting_decision | done | error.")
    values: dict[str, Any] = Field(
        default_factory=dict, description="JSON-safe snapshot of the graph state."
    )
    error: str = Field(default="", description="Error message when phase is 'error', else empty.")


def _phase_for(awaiting: bool) -> str:
    """Map an awaiting flag to the terminal snapshot phase string."""
    return "awaiting_decision" if awaiting else "done"


# ---------------------------------------------------------------------------
# Graph helpers (run in a threadpool; the graph API is synchronous)
# ---------------------------------------------------------------------------


def _code_valid(tdb_code: str) -> bool:
    """Return whether a TDB code is a well-formed 7-digit string."""
    return tdb_code.isdigit() and len(tdb_code) == 7


def _snapshot(thread_id: str) -> tuple[bool, dict[str, Any]]:
    """Read (awaiting_decision, json-safe values) for a thread; ({}, False) if none.

    Blocking (reads the checkpointer); call via ``run_in_threadpool``.
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_graph(checkpointer=cp)
        state = graph_app.get_state(config)
    awaiting = bool(state.next)
    return awaiting, _json_safe(dict(state.values))


def _has_state(thread_id: str) -> bool:
    """Return whether any checkpoint already exists for this thread (idempotency)."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_graph(checkpointer=cp)
        state = graph_app.get_state(config)
    return bool(state.values)


def _run_to_pause(payload: dict[str, Any], thread_id: str, *, resume: bool) -> None:
    """Drive the graph to the next interrupt / completion (blocking).

    Mirrors the UI's worker: a fresh checkpointer + compiled graph, invoked with
    either the initial payload or a ``Command(resume=...)``. The checkpointer
    persists the pause, so a subsequent read/resume continues exactly here.
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    command: Any = Command(resume=payload) if resume else payload
    with make_checkpointer() as cp:
        graph_app = compile_graph(checkpointer=cp)
        graph_app.invoke(command, config=config)


def _json_safe(values: dict[str, Any]) -> dict[str, Any]:
    """Best-effort coerce a state dict to JSON-serialisable primitives.

    The snapshot can contain Pydantic models / enums / dates (rehydrated from the
    checkpointer). Pydantic's ``to_jsonable_python`` handles all three; on any
    odd value it falls back to ``str`` so the endpoint never 500s on encoding.
    """
    from pydantic_core import to_jsonable_python

    out: dict[str, Any] = {}
    for key, value in values.items():
        try:
            out[key] = to_jsonable_python(value, fallback=str)
        except Exception:  # noqa: BLE001 - serialisation is best-effort
            out[key] = str(value)
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _registry_snapshot(thread_id: str, status: RunStatus) -> RunSnapshot | None:
    """Map an in-flight / failed registry status to a snapshot, or None.

    Returns a ``running`` snapshot for a dispatched-but-not-yet-checkpointed run
    and an ``error`` snapshot for a failed background run — the two states the
    durable checkpointer cannot express. Returns None for terminal phases
    (awaiting_decision / done), letting the caller fall through to the durable
    snapshot, which is authoritative once a checkpoint exists.
    """
    if status.phase is RunPhase.ERROR:
        return RunSnapshot(
            thread_id=thread_id,
            awaiting_decision=False,
            phase="error",
            error=status.error,
        )
    if status.phase is RunPhase.RUNNING:
        return RunSnapshot(thread_id=thread_id, awaiting_decision=False, phase="running")
    return None


@router.post("/runs", response_model=RunSnapshot)
async def start_run(body: StartRunRequest, identity: _IdentityDep) -> RunSnapshot:
    """Start (or idempotently return) an assessment run for a TDB code.

    Generates a ``thread_id`` when none is supplied. If the supplied thread_id
    already has state, the existing snapshot is returned WITHOUT starting a
    second run (idempotency).

    Idempotency covers BOTH the durable checkpointer AND the in-flight registry:
    in async mode a dispatched run is ``RUNNING`` in the registry before any
    checkpoint exists, so a second ``POST /runs`` with the same thread_id (a
    retry, or a load-balancer re-send) must NOT dispatch a second concurrent job
    racing on the same thread_id — it returns the in-flight status instead.

    Execution mode is set by ``Settings.run_async``:

    * **Synchronous** (default): the graph runs to the HITL interrupt or to
      completion before responding, and the resulting snapshot is returned
      (original behaviour).
    * **Async**: the graph work is dispatched OFF the request path; the response
      returns immediately with ``phase="running"`` and the client polls
      ``GET /runs/{thread_id}`` for progress.
    """
    if not _code_valid(body.tdb_code):
        raise HTTPException(status_code=422, detail="tdb_code must be a 7-digit string")
    thread_id = body.thread_id or str(uuid.uuid4())

    # Idempotency for a caller-supplied thread_id. A run is "already started" if
    # EITHER the registry knows it (in-flight / failed, async mode — may have no
    # checkpoint yet) OR a durable checkpoint exists. Either way we never start a
    # second run; we return what the thread already has.
    if body.thread_id:
        status = get_run_registry().get(thread_id)
        if status is not None:
            registry_view = _registry_snapshot(thread_id, status)
            if registry_view is not None:
                _log.info(
                    "api.run.idempotent_hit",
                    thread_id=thread_id,
                    via="registry",
                    phase=registry_view.phase,
                )
                return registry_view
        if await run_in_threadpool(_has_state, thread_id):
            awaiting, values = await run_in_threadpool(_snapshot, thread_id)
            _log.info("api.run.idempotent_hit", thread_id=thread_id, via="checkpoint")
            return RunSnapshot(
                thread_id=thread_id,
                awaiting_decision=awaiting,
                phase=_phase_for(awaiting),
                values=values,
            )

    _log.info("api.run.start", thread_id=thread_id, tenant=identity.tenant_id)
    payload = {"tdb_code": body.tdb_code}

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


@router.get("/runs/{thread_id}", response_model=RunSnapshot)
async def get_run(thread_id: str, identity: _IdentityDep) -> RunSnapshot:
    """Read the current snapshot for a run (404 when the thread is unknown).

    In async mode a run may be in flight (``phase="running"``) before any
    checkpoint exists, or may have FAILED in the background (``phase="error"``) --
    states the checkpointer alone cannot express. So the registry is consulted
    first: a ``running`` / ``error`` status is reported directly; otherwise the
    durable snapshot is read. A thread unknown to BOTH the registry and the
    checkpointer is a 404.
    """
    status = get_run_registry().get(thread_id)
    has_state = await run_in_threadpool(_has_state, thread_id)
    if status is None and not has_state:
        raise HTTPException(status_code=404, detail="run not found")

    # A still-running or failed background job is reported from the registry; its
    # durable snapshot is NOT yet authoritative. While the job runs it writes
    # INTERMEDIATE checkpoints at every super-step boundary, and at such a
    # boundary the snapshot's ``next`` can transiently be empty (between two
    # nodes) -- which _phase_for would misread as the terminal ``done`` and a
    # poller would latch onto, even though the run has not reached HITL / END.
    # So a RUNNING registry status is authoritative over any intermediate
    # checkpoint: report ``running`` until the background job records its OWN
    # terminal phase (awaiting_decision / done) in the registry after
    # _run_to_pause returns. (A run dispatched but not yet checkpointed is also
    # reported running.) ERROR is likewise registry-only.
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


@router.post("/runs/{thread_id}/resume", response_model=RunSnapshot)
async def resume_run(thread_id: str, body: ResumeRunRequest, identity: _IdentityDep) -> RunSnapshot:
    """Resume a paused run with the banker's decision.

    Validates the decision, confirms the run exists and is actually paused at the
    interrupt, then resumes via ``Command(resume=...)``. Resuming a run that is
    not awaiting a decision is a 409. Honours ``Settings.run_async`` exactly like
    :func:`start_run`: synchronous returns the post-resume snapshot; async
    dispatches the resume off the request path and returns ``phase="running"``.
    """
    decision = body.decision.strip().lower()
    if decision not in _VALID_DECISIONS:
        raise HTTPException(
            status_code=422,
            detail=f"decision must be one of {sorted(_VALID_DECISIONS)}",
        )
    if not await run_in_threadpool(_has_state, thread_id):
        raise HTTPException(status_code=404, detail="run not found")
    awaiting, _ = await run_in_threadpool(_snapshot, thread_id)
    if not awaiting:
        raise HTTPException(status_code=409, detail="run is not awaiting a decision")

    _log.info(
        "api.run.resume",
        thread_id=thread_id,
        decision=decision,
        tenant=identity.tenant_id,
    )
    payload = {"decision": decision, "revision_note": body.revision_note}

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


def _dispatch_async(thread_id: str, payload: dict[str, Any], *, resume: bool) -> RunSnapshot:
    """Mark the run RUNNING, submit the graph job off the request path, return.

    The submitted job runs the blocking ``_run_to_pause`` and, on success,
    records the terminal phase (awaiting_decision / done) in the registry. A
    failure is recorded as ERROR by the executor wrapper. Returns immediately
    with ``phase="running"`` so the client polls ``GET /runs/{thread_id}``.
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
