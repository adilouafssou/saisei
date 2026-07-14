"""Portfolio watchlist recorder (Feature 8.1 — opt-in continuous monitoring).

:func:`record_snapshot` is the single helper the assessment path uses to persist
one borrower's latest watchlist snapshot. It mirrors the Feature 7 audit
recorder (``app/backend/audit/record.py``) and the Feature 3 trajectory recorder
(``app/backend/trajectory/recorder.py``):

1. it reads the borrower's identity + already-computed display figures from the
   graph state (EWS, FSA label, a deterministic EWS series for the sparkline);
2. it scopes the snapshot to a tenant (the storage isolation key); and
3. it upserts it into the configured store (current-state book, replace-by-key).

It is **best-effort and never fatal**: any failure — building the snapshot or the
store upsert — is logged and swallowed, so the regulated workflow can never break
because the watchlist backend misbehaved. It is **write-only**: it returns
``None`` and changes no graph state, gate, route, score, or figure.

Governance posture (why this is safe to wire on the default path):

- **Offline default = no-op.** With no ``SAISEI_PORTFOLIO_DSN`` configured the
  store is :class:`NullPortfolioStore`, so this discards the snapshot and the
  watchlist stays ephemeral / in-session — byte-identical to before.
- **One identity seam.** Tenant scoping and the production auth guard go through
  ``app.backend.identity`` (``resolve_identity`` / ``require_persistable``), the
  SAME resolver the UI read path and the audit actor use — so the write tenant
  always matches the read tenant, and when Feature 6 (OIDC) lands the real bank
  identity flows in from one place. When ``auth_required`` is set but only the
  placeholder identity is available, ``require_persistable`` raises and the
  best-effort guard skips the write, so a misconfigured production deployment
  never writes an un-attributed book.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.backend.identity import require_persistable, resolve_identity
from app.backend.portfolio.store import (
    PortfolioSnapshot,
    PortfolioStore,
    get_portfolio_store,
)
from app.shared.logging import get_logger
from app.shared.models.loan import LoanEvent, current_status
from app.shared.settings import Settings, get_settings

__all__ = [
    "record_snapshot",
    "record_origination_snapshot",
    "build_ews_series",
    "loan_status_kanji",
]

_log = get_logger(__name__)


def loan_status_kanji(state: Any) -> str:
    """Return the facility's current loan-lifecycle status as a Japanese label.

    Derives the current status by replaying the already-recorded loan-event log
    on state (the same append-only ledger the graph maintains) and returns its
    kanji label (申込 / 審査中 / 承認 / 実行 / 正常 / 条件変更 / 管理回収 / ...).
    Returns ``""`` when no facility is attached (no loan_events), so the
    watchlist simply shows no lifecycle badge for that borrower. Pure display
    derivation — never a new judgement, gate, route, or figure. Read defensively
    via ``getattr`` so it works on a live ``SaiseiState`` or any state-like
    object, and never raises (a malformed log yields ``""``).

    Args:
        state: The graph state (or state-like object) for the borrower.

    Returns:
        The current status kanji, or ``""`` when there is no attached facility.
    """
    raw_events = getattr(state, "loan_events", None) or []
    if not raw_events:
        return ""
    try:
        events = [LoanEvent.model_validate(e) for e in raw_events]
        return current_status(events).kanji
    except Exception:  # noqa: BLE001 - display derivation must never raise
        return ""


def build_ews_series(state: Any) -> str:
    """Build the deterministic per-month EWS series for the sparkline.

    Returns a comma-joined string of the REAL monthly keijo-rieki-derived signal
    the watchlist sparkline draws, taken straight off the trial balances so the
    trend is never fabricated. Reads defensively via ``getattr`` so it works on a
    live ``SaiseiState`` or any state-like object. Returns ``""`` when there is
    no monthly history (the sparkline then simply self-hides).

    The series is the monthly ordinary profit (経常利益) per trial balance, oldest
    -> newest: a falling line is deteriorating health, which the watchlist
    colours accordingly. This is a DISPLAY series only; it computes no verdict.

    Args:
        state: The graph state (or state-like object) for the borrower.

    Returns:
        A comma-joined string of integer yen values, or ``""`` when empty.
    """
    shisanhyo = getattr(state, "shisanhyo", None) or []
    values: list[str] = []
    for tb in shisanhyo:
        keijo = getattr(tb, "keijo_rieki", None)
        if keijo is None:
            continue
        values.append(str(int(keijo)))
    return ",".join(values)


def record_snapshot(
    *,
    state: Any,
    tenant_id: str | None = None,
    settings: Settings | None = None,
    store: PortfolioStore | None = None,
) -> None:
    """Build and upsert one watchlist snapshot. Best-effort; never raises.

    The single write helper for the opt-in watchlist. It snapshots the
    borrower's identity + already-computed display figures (EWS, FSA label, the
    deterministic EWS series), scopes it to the resolved tenant, and upserts it
    into the configured store. Any exception is logged and swallowed so the
    workflow is never broken by the side-record. It returns ``None`` and mutates
    no graph state — a side-effect only.

    Offline (no ``portfolio_dsn``) the store is :class:`NullPortfolioStore`, so
    this is a no-op and the watchlist stays ephemeral.

    Tenant + auth guard flow through the ONE ``app.backend.identity`` seam
    (``resolve_identity`` / ``require_persistable``) — the same resolver the UI
    read path (``current_tenant_id``) and the audit actor use — so the write
    tenant always matches the read tenant. When ``auth_required`` is set but only
    the placeholder identity is available, ``require_persistable`` raises
    :class:`~app.backend.identity.IdentityError`, which the best-effort guard
    swallows (the write is skipped).

    Args:
        state: The graph state (source of identity + display figures).
        tenant_id: Explicit tenant override (storage isolation key). When ``None``
            (the normal path) the tenant is resolved via the identity seam.
            Passing it bypasses the auth guard, so it is for tests / callers that
            have already established a real tenant.
        settings: Optional settings override (defaults to cached settings).
        store: Optional store override (defaults to the configured store).
    """
    try:
        settings = settings or get_settings()

        if tenant_id is not None:
            resolved_tenant = str(tenant_id)
        else:
            # Resolve + enforce the production auth guard through the one seam.
            # require_persistable raises IdentityError under auth_required with a
            # placeholder identity; the outer best-effort guard skips the write.
            identity = require_persistable(resolve_identity(settings), settings)
            resolved_tenant = identity.tenant_id

        store = store if store is not None else get_portfolio_store(settings)

        fsa = getattr(state, "fsa_classification", None)
        fsa_kanji = str(getattr(fsa, "kanji", "") or "") if fsa is not None else ""
        profile = getattr(state, "company_profile", None)
        company_name = str(getattr(profile, "company_name", "") or "") if profile else ""

        snapshot = PortfolioSnapshot(
            tenant_id=resolved_tenant,
            tdb_code=str(getattr(state, "tdb_code", "") or ""),
            company_name=company_name,
            ews=float(getattr(state, "ews_score", 0.0) or 0.0),
            fsa_kanji=fsa_kanji,
            ews_series=build_ews_series(state),
            loan_status=loan_status_kanji(state),
            updated_at=dt.datetime.now(dt.UTC).isoformat(),
        )

        store.upsert(snapshot)
        _log.info(
            "portfolio.snapshot_recorded",
            tenant_id=resolved_tenant,
            tdb_code=snapshot.tdb_code,
            ews=snapshot.ews,
        )
    except Exception as exc:  # noqa: BLE001 - watchlist is best-effort, never fatal
        _log.warning("portfolio.snapshot_failed", error=str(exc))


def record_origination_snapshot(
    *,
    state: Any,
    tenant_id: str | None = None,
    settings: Settings | None = None,
    store: PortfolioStore | None = None,
) -> None:
    """Record a watchlist snapshot for an ORIGINATED facility. Best-effort.

    The origination counterpart to :func:`record_snapshot`. Where that records a
    borrower under turnaround ASSESSMENT (EWS + FSA class), this records a new
    facility as it moves through origination, so an originated facility appears
    in the SAME watchlist book the moment it is created — the visible payoff of
    the unified lifecycle spine. It carries the facility's current loan-lifecycle
    status (申込 / 審査中 / 承認 / 実行 / 謝絶) and the company name; EWS / FSA stay
    empty because an applicant has no distress assessment yet (a later turnaround
    assessment fills those, upserting the SAME borrower row — one continuous
    record, not two).

    Identical governance posture to :func:`record_snapshot`: write-only, mutates
    no graph state, never fatal (any failure is logged and swallowed), tenant +
    auth guard flow through the one ``app.backend.identity`` seam, and offline
    (no ``portfolio_dsn``) it is a no-op (``NullPortfolioStore``).

    Args:
        state: The origination graph state (source of identity + loan status).
        tenant_id: Explicit tenant override; when ``None`` the tenant is resolved
            via the identity seam (which also enforces the production auth guard).
        settings: Optional settings override (defaults to cached settings).
        store: Optional store override (defaults to the configured store).
    """
    try:
        settings = settings or get_settings()

        if tenant_id is not None:
            resolved_tenant = str(tenant_id)
        else:
            identity = require_persistable(resolve_identity(settings), settings)
            resolved_tenant = identity.tenant_id

        store = store if store is not None else get_portfolio_store(settings)

        profile = getattr(state, "company_profile", None)
        company_name = str(getattr(profile, "name", "") or "") if profile else ""

        snapshot = PortfolioSnapshot(
            tenant_id=resolved_tenant,
            tdb_code=str(getattr(state, "tdb_code", "") or ""),
            company_name=company_name,
            # An applicant has no distress assessment yet: EWS / FSA stay empty
            # and a later turnaround assessment upserts the SAME borrower row.
            ews=0.0,
            fsa_kanji="",
            ews_series="",
            loan_status=loan_status_kanji(state),
            updated_at=dt.datetime.now(dt.UTC).isoformat(),
        )

        store.upsert(snapshot)
        _log.info(
            "portfolio.origination_snapshot_recorded",
            tenant_id=resolved_tenant,
            tdb_code=snapshot.tdb_code,
            loan_status=snapshot.loan_status,
        )
    except Exception as exc:  # noqa: BLE001 - watchlist is best-effort, never fatal
        _log.warning("portfolio.origination_snapshot_failed", error=str(exc))
