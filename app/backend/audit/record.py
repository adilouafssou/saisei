"""Audit-event recorder (Feature 7, spec §7 / §12 step 3).

:func:`record_event` is the single helper every call site uses to write one
audit event. It:

1. computes the ``data_version`` (a hash over the borrower inputs the event was
   computed from) and ``thresholds_version`` (a hash over the relevant
   ``app.shared.constants`` in force), so a record can always be tied to the
   exact figures + thresholds at decision time;
2. looks up the previous event's ``content_hash`` for this ``thread_id`` to set
   ``prev_hash`` (the tamper-evident chain);
3. seals the event with its own ``content_hash`` and appends it to the sink.

It is **best-effort and never fatal** (spec §2): any failure — building the
event, hashing, or the sink append — is logged and swallowed, so the regulated
workflow can never break because the ledger backend misbehaved. It is also
**write-only on the decision path**: it returns ``None`` and changes no graph
state, gate, route, score, or figure.

Offline: with no ``audit_dsn`` configured the sink is :class:`NullAuditSink`, so
this is a no-op (the version hashes are still computed but discarded with the
no-op append), keeping the system byte-stable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

from app.backend.audit.audit_log import AuditEvent, AuditEventType
from app.backend.audit.signing import Signer, get_signer
from app.backend.audit.sink import AuditSink, get_audit_sink
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "record_event",
    "compute_data_version",
    "compute_thresholds_version",
    "summarise_event",
    "get_audit_sink",
]

_log = get_logger(__name__)


def _stable_hash(payload: Any) -> str:
    """Return a deterministic short SHA-256 over a JSON-canonical payload.

    Sorted keys + compact separators so the digest is insensitive to dict order
    (same canonicalisation contract as the event content hash). Returns the
    first 16 hex chars — enough to pin a version without bloating the record.
    """
    text = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def compute_data_version(state: Any) -> str:
    """Hash the borrower inputs an event was computed from (spec §4).

    Pins the record to the exact figures in force: the monthly trial balances
    (period + the three signal lines), the TDB score, the working-capital gap,
    net worth, and the insolvency flag. Reads defensively via ``getattr`` so it
    works on a live ``SaiseiState`` or any state-like object.

    Args:
        state: The graph state (or state-like object) for the borrower.

    Returns:
        A 16-char hex version hash (stable across dict ordering).
    """
    shisanhyo = getattr(state, "shisanhyo", None) or []
    rows = [
        {
            "period": str(getattr(tb, "period", "")),
            "uriage": int(getattr(tb, "uriage", 0)),
            "uriage_genka": int(getattr(tb, "uriage_genka", 0)),
            "keijo_rieki": int(getattr(tb, "keijo_rieki", 0)),
        }
        for tb in shisanhyo
    ]
    inputs = {
        "shisanhyo": rows,
        "tdb_score": getattr(state, "tdb_score", None),
        "working_capital_gap": getattr(state, "working_capital_gap", None),
        "net_worth": getattr(state, "net_worth", None),
        "is_insolvent": getattr(state, "is_insolvent", None),
    }
    return _stable_hash(inputs)


def compute_thresholds_version() -> str:
    """Hash the deterministic constants in force (spec §4).

    Pins the record to the threshold set that produced the verdict, so a
    classification can be reproduced even after a constant is later re-tuned.
    Imported lazily so this module has no import-time coupling to constants.

    Returns:
        A 16-char hex version hash.
    """
    from app.shared import constants as c

    relevant = {
        "EWS_SUBSTANDARD": c.EWS_SUBSTANDARD,
        "EWS_DOUBTFUL": c.EWS_DOUBTFUL,
        "EWS_DANGER": c.EWS_DANGER,
        "TDB_NORMAL_FLOOR": c.TDB_NORMAL_FLOOR,
        "HOSHO_WEIGHT_BUNRI": c.HOSHO_WEIGHT_BUNRI,
        "HOSHO_WEIGHT_ZAIMU": c.HOSHO_WEIGHT_ZAIMU,
        "HOSHO_WEIGHT_KAIJI": c.HOSHO_WEIGHT_KAIJI,
        "HOSHO_ELIGIBLE_SCORE": c.HOSHO_ELIGIBLE_SCORE,
        "HOSHO_SUCCESSION_EWS_MAX": c.HOSHO_SUCCESSION_EWS_MAX,
        "HOSHO_SUCCESSION_TDB_MIN": c.HOSHO_SUCCESSION_TDB_MIN,
        "PRO_RATA_TOLERANCE": c.PRO_RATA_TOLERANCE,
    }
    return _stable_hash(relevant)


def summarise_event(event: Any) -> str:
    """Return a one-line, banker-readable summary of an audit event.

    Display helper for the Audit tab (Feature 9): turns an :class:`AuditEvent`'s
    typed payload into a short human string per event kind, so the examiner /
    banker reads the trail at a glance without parsing raw JSON. Pure and
    defensive — reads the payload via ``.get`` and never raises on a missing
    key, so an unrecognised or partial event still yields a sensible line.

    Args:
        event: An :class:`AuditEvent` (or any object exposing ``event_type`` and
            a ``payload`` mapping).

    Returns:
        A short summary string (never empty for a known event kind).
    """
    payload = getattr(event, "payload", {}) or {}
    etype = str(getattr(getattr(event, "event_type", ""), "value", "") or "")

    if etype == AuditEventType.CLASSIFICATION.value:
        cls = payload.get("fsa_classification", "?")
        ews = payload.get("ews_score")
        ews_txt = f", EWS {ews}" if ews is not None else ""
        sa = " ・要管理先" if payload.get("special_attention") else ""
        return f"債務者区分: {cls}{ews_txt}{sa}"

    if etype == AuditEventType.GUARANTEE_RELEASE.value:
        score = payload.get("hosho_kaijo_score", "?")
        eligible = payload.get("hosho_kaijo_eligible")
        elig_txt = "適格" if eligible is True else ("不適格" if eligible is False else "?")
        return f"経営者保証解除: スコア {score} ・ {elig_txt}"

    if etype == AuditEventType.HUMAN_DECISION.value:
        decision = payload.get("decision", "?")
        title = payload.get("approved_strategy_title")
        title_txt = f" ・ {title}" if title else ""
        return f"担当者決定: {decision}{title_txt}"

    if etype == AuditEventType.ORIGINATION_DECISION.value:
        rec = payload.get("recommendation", "?")
        ceiling = payload.get("max_facility_amount")
        ceiling_txt = f" ・ 上限 ¥{ceiling:,}" if isinstance(ceiling, int) and ceiling > 0 else ""
        grounded = payload.get("grounded")
        status_txt = "接地済" if grounded is True else ("未検証あり" if grounded is False else "?")
        return f"融資組成推奨: {rec}{ceiling_txt} ・ {status_txt}"

    if etype == AuditEventType.COMPANION_QUERY.value:
        question = str(payload.get("question", "") or "")
        preview = question if len(question) <= 40 else question[:39] + "…"
        grounded = payload.get("grounded")
        status_txt = "接地済" if grounded is True else ("未検証あり" if grounded is False else "?")
        return f"AI助言の質問: 「{preview}」 ・ {status_txt}"

    return etype or "audit event"


def record_event(
    event_type: AuditEventType,
    *,
    state: Any,
    payload: dict[str, Any],
    actor: str = "system",
    thread_id: str | None = None,
    settings: Settings | None = None,
    sink: AuditSink | None = None,
    signer: Signer | None = None,
) -> None:
    """Build, hash-chain, and append one audit event. Best-effort; never raises.

    This is the single write helper for the ledger. It computes the version
    hashes, links the event to the previous one for the same ``thread_id``
    (``prev_hash``), seals it with its ``content_hash``, and appends it. Any
    exception is logged and swallowed so the workflow is never broken by the
    audit side-record (spec §2).

    It returns ``None`` and mutates no graph state — it is a side-effect only.

    Args:
        event_type: The kind of event (see :class:`AuditEventType`).
        state: The graph state (source of identity + version inputs).
        payload: Event-kind-specific contents (already deterministic figures).
        actor: "system" for deterministic nodes; the banker id for decisions.
        thread_id: Explicit run thread id. In LangGraph the thread_id lives in
            the run *config*, not in ``SaiseiState``, so node call sites pass it
            explicitly. Falls back to ``state.thread_id`` (UI/runtime objects
            that carry it) and finally to "".
        settings: Optional settings override (defaults to cached settings).
        sink: Optional sink override (defaults to the configured sink).
    """
    try:
        settings = settings or get_settings()
        sink = sink if sink is not None else get_audit_sink(settings)
        signer = signer if signer is not None else get_signer(settings)

        resolved_thread_id = str(
            thread_id if thread_id is not None else (getattr(state, "thread_id", "") or "")
        )
        # Determine prev_hash from the last event for this thread (chain link).
        prev_hash = ""
        try:
            existing = sink.read(resolved_thread_id)
            if existing:
                prev_hash = existing[-1].content_hash
        except Exception as exc:  # noqa: BLE001 - read failure must not block write
            _log.warning("audit.prev_hash_read_failed", error=str(exc))

        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            thread_id=resolved_thread_id,
            tdb_code=str(getattr(state, "tdb_code", "") or ""),
            hojin_bango=str(getattr(state, "hojin_bango", "") or ""),
            event_type=event_type,
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            actor=actor,
            payload=payload,
            data_version=compute_data_version(state),
            thresholds_version=compute_thresholds_version(),
            prev_hash=prev_hash,
        ).with_content_hash()

        # Attach a detached cryptographic signature over the sealed content_hash
        # (tamper-PROOF, not merely tamper-evident). Offline default: NullSigner
        # leaves the signature empty, so this is a no-op and the event is
        # byte-stable. Signing is excluded from the content hash, so it never
        # changes the event's identity or chain link.
        signature = signer.sign(event.content_hash)
        if signature:
            event = event.model_copy(update={"signature": signature})

        sink.append(event)
        _log.info(
            "audit.recorded",
            event_type=str(event_type),
            thread_id=resolved_thread_id,
            tdb_code=event.tdb_code,
            event_id=event.event_id,
        )
    except Exception as exc:  # noqa: BLE001 - audit is best-effort, never fatal
        _log.warning("audit.record_failed", event_type=str(event_type), error=str(exc))
