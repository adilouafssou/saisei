"""Saisei application entry point.

Initialises the Reflex app (``rx.App``) and mounts the FastAPI health/readiness
probes onto the Reflex API router. The lifespan handler configures structured
logging at startup and emits a shutdown event.

Exports:
- ``app``: the Reflex ``rx.App`` instance (used by ``reflex run``).
- ``create_app``: factory that returns the underlying FastAPI application
  (used by uvicorn: ``uvicorn app.main:create_app --factory``).
- ``asgi_app``: the ASGI application for direct mounting.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import reflex as rx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.backend.api.runs import require_identity
from app.backend.audit.sink import get_audit_sink
from app.backend.identity import Identity
from app.backend.observability import configure_tracing
from app.shared.logging import configure_logging, get_logger
from app.shared.settings import get_settings

__all__ = ["app", "create_app", "asgi_app"]

# Audit-admin request models + the injected-identity dependency live at MODULE
# scope (not inside create_app). With ``from __future__ import annotations`` in
# effect, FastAPI resolves a route handler's annotations via ``get_type_hints``
# against the module globalns; function-LOCAL classes / aliases are invisible
# there, so a body model and the Depends() identity were both misread as query
# parameters and every admin POST 422'd ("field required: query.body /
# query.identity"). Defining them here makes the annotations resolvable, so the
# body is parsed from JSON and the identity is injected as a dependency.
_IdentityDep = Annotated[Identity, Depends(require_identity)]


class _RedactionBody(BaseModel):
    target_event_id: str = Field(description="event_id whose payload keys to mask.")
    redact_keys: list[str] = Field(description="Payload keys to mask at view time.")
    reason: str = Field(description="Why the redaction is made (recorded).")


class _HoldBody(BaseModel):
    reason: str = Field(default="", description="Why the hold is placed / released.")


@asynccontextmanager
async def _lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger(__name__)
    configure_tracing(settings)
    log.info("saisei.startup", use_mocks=settings.use_mocks)
    yield
    log.info("saisei.shutdown")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application with health probes.

    Returns:
        The configured FastAPI application.
    """
    application = FastAPI(
        title="Saisei API",
        version="0.1.0",
        description="Autonomous EWS & Keiei Kaizen Keikakusho platform.",
        lifespan=_lifespan,
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @application.get("/ready")
    async def ready() -> dict[str, str]:
        """Readiness probe."""
        settings = get_settings()
        return {"status": "ready", "mocks": str(settings.use_mocks).lower()}

    # Productionisation (first slice): the idempotent run/resume HTTP surface
    # for driving the graph as a service, keyed by thread_id. Auth + per-bank
    # tenancy live behind the require_principal dependency seam in the router
    # (a placeholder today; OIDC drops into that one function next).
    from app.backend.api import (
        distress_router,
        origination_router,
        runs_router,
        servicing_router,
    )

    application.include_router(runs_router)
    # The origination counterpart: drive the 融資組成 graph (申込 -> 実行 / 謝絶)
    # as a service. Same idempotency / async / HITL-resume semantics, bound to
    # the origination graph.
    application.include_router(origination_router)
    # The servicing counterpart: drive the 貸出管理 graph (実行 -> 正常 -> 完済) as
    # a service. Same idempotency / async semantics, but NO resume endpoint --
    # servicing transitions are non-gated operational facts, so the graph runs
    # straight to completion (credit / distress decisions stay HITL-gated on
    # their own surfaces).
    application.include_router(servicing_router)
    # The distress counterpart: drive the 条件変更 / 償却 graph (HITL-gated
    # PERFORMING -> RESTRUCTURED and WORKOUT -> WRITTEN_OFF) as a service. Like
    # origination (and UNLIKE servicing) it INTERRUPTS for the banker, so it has
    # a resume / decision endpoint; same idempotency / async semantics, bound to
    # the distress graph. This is the service edge that makes restructure_node /
    # writeoff_node reachable end to end.
    application.include_router(distress_router)

    @application.get("/audit")
    async def audit_query(
        tdb_code: str | None = None,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read-only CROSS-THREAD examiner query over the audit ledger.

        The book-level / regulator complement to ``GET /audit/{thread_id}``:
        returns audit events across ALL borrowers matching the optional filters
        (borrower code, event type, actor, ISO 8601 created_at range), in global
        write order, capped by ``limit``. This answers questions a single-thread
        read cannot, e.g. "every human_decision by this banker in March".

        READ-ONLY: there is no write path here; the ledger remains append-only.
        Offline-safe: with no ``SAISEI_AUDIT_DSN`` the NullAuditSink returns an
        empty list. An unknown ``event_type`` is a 400 (rather than silently
        returning everything).

        NOTE (spec §8): like the per-thread surface this is an examiner tool, to
        be placed behind the same auth as the rest of the API in deployment.
        """
        from app.backend.audit.audit_log import AuditEventType
        from app.backend.audit.sink import AuditQuery

        parsed_type: AuditEventType | None = None
        if event_type:
            try:
                parsed_type = AuditEventType(event_type)
            except ValueError as exc:
                valid = ", ".join(t.value for t in AuditEventType)
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown event_type {event_type!r}; valid: {valid}",
                ) from exc

        query = AuditQuery(
            tdb_code=tdb_code or None,
            event_type=parsed_type,
            actor=actor or None,
            since=since or None,
            until=until or None,
            limit=limit,
        )
        sink = get_audit_sink(get_settings())
        events = sink.query(query)
        return {
            "count": len(events),
            "limit": query.effective_limit(),
            "events": [event.model_dump(mode="json") for event in events],
        }

    @application.get("/audit/analytics")
    async def audit_analytics(
        tdb_code: str | None = None,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Read-only CROSS-THREAD analytics over the audit ledger.

        The book-level / supervisory aggregation on top of ``GET /audit``: instead
        of returning raw rows, it returns DETERMINISTIC counts over the events
        matching the same optional filters (borrower code, event type, actor, ISO
        8601 created_at range) -- totals, per-event-type / per-actor / per-borrower
        breakdowns, the human-decision (approve/revise/reject) split, distinct
        cardinalities, the activity time span, and the governance posture (active
        legal holds, redaction count). This answers the questions a dashboard
        asks, e.g. "how many decisions did each banker make in March, and were
        any threads on hold?".

        It aggregates the SAME events the raw query returns (reusing
        :class:`AuditQuery` + the sink seam), so the two surfaces are always
        consistent. The ``limit`` caps how many events are summarised (default
        1000, the query ceiling) so the aggregation is bounded.

        READ-ONLY: no write path; the ledger stays append-only. Offline-safe: with
        no ``SAISEI_AUDIT_DSN`` the NullAuditSink yields an all-zero summary. An
        unknown ``event_type`` is a 400, matching ``GET /audit``.

        NOTE (spec §8): an examiner tool, to sit behind the same auth as the rest
        of the API in deployment.
        """
        from app.backend.audit.analytics import summarise
        from app.backend.audit.audit_log import AuditEventType
        from app.backend.audit.sink import AuditQuery

        parsed_type: AuditEventType | None = None
        if event_type:
            try:
                parsed_type = AuditEventType(event_type)
            except ValueError as exc:
                valid = ", ".join(t.value for t in AuditEventType)
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown event_type {event_type!r}; valid: {valid}",
                ) from exc

        query = AuditQuery(
            tdb_code=tdb_code or None,
            event_type=parsed_type,
            actor=actor or None,
            since=since or None,
            until=until or None,
            limit=limit,
        )
        sink = get_audit_sink(get_settings())
        events = sink.query(query)
        analytics = summarise(events)
        return {
            "limit": query.effective_limit(),
            "analytics": {
                "total_events": analytics.total_events,
                "by_event_type": analytics.by_event_type,
                "by_actor": analytics.by_actor,
                "by_borrower": analytics.by_borrower,
                "decisions": analytics.decisions,
                "distinct_actors": analytics.distinct_actors,
                "distinct_borrowers": analytics.distinct_borrowers,
                "distinct_threads": analytics.distinct_threads,
                "active_legal_holds": analytics.active_legal_holds,
                "redaction_events": analytics.redaction_events,
                "earliest": analytics.earliest,
                "latest": analytics.latest,
            },
        }

    @application.get("/audit/{thread_id}")
    async def audit(thread_id: str) -> dict[str, Any]:
        """Read-only examiner surface for the immutable audit ledger (Feature 7).

        Returns the ordered audit events for a ``thread_id`` plus the tamper-
        evidence chain verdict, as JSON. READ-ONLY: there is no write path here;
        events are only ever appended by the deterministic decision nodes via
        ``record_event``.

        Offline-safe: with no ``SAISEI_AUDIT_DSN`` configured the sink is the
        no-op :class:`~app.backend.audit.sink.NullAuditSink`, so this returns an
        empty list and an OK (trivially intact) chain verdict.

        NOTE (spec §8): this is an examiner tool, not the banker UI. It must be
        placed behind the same auth as the rest of the API once OIDC auth lands
        (Feature 6); until then it exposes only the audit records, which contain
        no secrets (deterministic figures + version hashes).
        """
        sink = get_audit_sink(get_settings())
        events = sink.read(thread_id)
        # Verify the chain + signatures on the RAW (unmasked) events -- the
        # on-disk rows are what the hashes/signatures sealed. Masking is applied
        # only to the DISPLAYED payloads afterwards, so verification reflects the
        # true ledger state, not the redacted view.
        verdict = sink.verify_chain(thread_id)
        from app.backend.audit.admin import apply_redactions

        displayed = apply_redactions(events)
        result: dict[str, Any] = {
            "thread_id": thread_id,
            "count": len(displayed),
            "events": [event.model_dump(mode="json") for event in displayed],
            "chain": {
                "ok": verdict.ok,
                "broken_at": verdict.broken_at,
                "reason": verdict.reason,
            },
        }
        # Tamper-PROOF layer: when a public key is configured, additionally
        # report the cryptographic-signature verdict over the same events, so an
        # examiner sees not just that the chain is internally consistent but that
        # each event was signed by the holder of the private key. Omitted when no
        # public key is set (offline / unsigned posture), keeping the response
        # byte-stable for the default deployment.
        from app.backend.audit.signing import verify_signatures

        public_key = (get_settings().audit_signing_public_key or "").strip()
        if public_key:
            sig = verify_signatures(events, public_key)
            result["signatures"] = {
                "ok": sig.ok,
                "checked": sig.checked,
                "unsigned": sig.unsigned,
                "broken_at": sig.broken_at,
                "reason": sig.reason,
            }
        return result

    # -----------------------------------------------------------------------
    # Audit admin actions (retention / redaction / legal hold).
    #
    # These are AUTHENTICATED, append-only administrative actions: each is
    # recorded as its OWN immutable, hash-chained, signed audit event (never an
    # edit/delete), so the act itself is part of the permanent trail. They reuse
    # the same require_identity seam as the run/resume API, so when OIDC is
    # configured a real admin identity is required and recorded as the actor;
    # offline they run under the placeholder identity (gated by auth_required).
    # -----------------------------------------------------------------------
    @application.post("/audit/{thread_id}/redactions")
    async def post_redaction(
        thread_id: str,
        body: _RedactionBody,
        identity: _IdentityDep,
    ) -> dict[str, Any]:
        """Record a redaction directive (append-only) for an event's payload keys.

        Appends a REDACTION event naming the target + keys + reason + admin; the
        target row is never edited. Subsequent reads mask those keys at view
        time. Requires a valid identity (the recorded redactor).
        """
        from app.backend.audit.admin import record_redaction

        record_redaction(
            thread_id,
            body.target_event_id,
            body.redact_keys,
            reason=body.reason,
            actor=identity.actor,
        )
        return {"status": "recorded", "thread_id": thread_id, "actor": identity.actor}

    @application.post("/audit/{thread_id}/legal-hold")
    async def post_legal_hold(
        thread_id: str,
        body: _HoldBody,
        identity: _IdentityDep,
    ) -> dict[str, Any]:
        """Place a legal hold on a thread (append-only); excludes it from purge."""
        from app.backend.audit.admin import place_legal_hold

        place_legal_hold(thread_id, reason=body.reason, actor=identity.actor)
        return {"status": "held", "thread_id": thread_id, "actor": identity.actor}

    @application.post("/audit/{thread_id}/legal-hold/release")
    async def post_legal_hold_release(
        thread_id: str,
        body: _HoldBody,
        identity: _IdentityDep,
    ) -> dict[str, Any]:
        """Release a previously placed legal hold (append-only)."""
        from app.backend.audit.admin import release_legal_hold

        release_legal_hold(thread_id, reason=body.reason, actor=identity.actor)
        return {"status": "released", "thread_id": thread_id, "actor": identity.actor}

    return application


#: Underlying FastAPI ASGI application (for uvicorn direct use via --factory,
#: which DOES run the FastAPI lifespan defined above).
asgi_app: FastAPI = create_app()

# ---------------------------------------------------------------------------
# Reflex app — mounts the FastAPI endpoints onto the Reflex API router.
# ---------------------------------------------------------------------------
# Import pages to register them with the Reflex app.
from app.frontend.pages import index  # noqa: E402 — must come after rx import
from app.frontend.state import SaiseiUIState  # noqa: E402 — on_load handlers


def _startup() -> None:
    """Initialise logging + tracing for the Reflex (`reflex run`) launch path.

    The FastAPI ``lifespan`` on ``asgi_app`` only runs when the FastAPI app is
    served directly (uvicorn ``--factory``). When the FastAPI app is merged via
    ``rx.App(api_transformer=...)``, Reflex drives its OWN ASGI lifespan and the
    transformer's lifespan is NOT executed — so logging/tracing would never be
    configured under ``reflex run``. Registering the same setup as a Reflex
    lifespan task makes both launch paths initialise identically.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing(settings)
    get_logger(__name__).info("saisei.startup", use_mocks=settings.use_mocks)


app = rx.App(
    api_transformer=asgi_app,
    theme=rx.theme(
        # Default to LIGHT — the trustworthy default for a bank tool. The toggle
        # (rx.color_mode.button in the top bar) lets analysts switch to dark for
        # long / low-light sessions without changing the brand identity. The
        # Saisei CSS custom properties (light :root + .dark overrides) are
        # injected as an rx.el.style tag at the top of the page (see
        # app/frontend/pages/index.py), which reliably emits raw global CSS.
        appearance="light",
        accent_color="grass",
        gray_color="sand",
        radius="large",
        scaling="100%",
    ),
)
app.register_lifespan_task(_startup)
app.add_page(index, title="Saisei 再生 — 経営改善")

# Feature 9 §6 — deep-linkable borrower tabs (forward-compatible Phase-2 routes).
# The same workspace page is registered under one static route per tab so each
# tab is bookmarkable / shareable by URL (e.g. /borrower/audit links straight to
# the Audit tab). Each route's on_load delegates to a literal tab setter (no
# query-param parsing — robust across Reflex versions); the page body is the
# identical ``index`` workspace, which renders the selected tab via
# ``effective_tab``. This realises the Phase-1 ``active_tab`` enum -> route
# segment mapping without the premature full route tree / left rail (those wait
# for the Portfolio altitude, per the meta-interface spec §4/§6).
for _route, _on_load in (
    ("/borrower/assessment", SaiseiUIState.open_assessment_tab),
    ("/borrower/meeting", SaiseiUIState.open_meeting_tab),
    ("/borrower/plan", SaiseiUIState.open_plan_tab),
    ("/borrower/audit", SaiseiUIState.open_audit_tab),
):
    app.add_page(
        index,
        route=_route,
        on_load=_on_load,
        title="Saisei 再生 — 経営改善",
    )

# Feature 8.1 — the Altitude-1 Portfolio watchlist route. Same workspace page;
# its on_load shows the (ephemeral, session-scoped) watchlist over the borrower
# workspace. Deep-linkable so a banker can bookmark the book view.
app.add_page(
    index,
    route="/portfolio",
    on_load=SaiseiUIState.open_portfolio,
    title="Saisei 再生 — ポートフォリオ",
)
