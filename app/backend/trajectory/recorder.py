"""Trajectory recorder (Feature 3).

:func:`record_trajectory` is the single helper the HITL decision path uses to
persist one captured negotiation. It:

1. builds a deterministic ``input_summary`` + ``data_version`` from the state
   (reusing the audit ledger's ``compute_data_version`` so the two side-records
   pin to the same inputs);
2. seals the record with its ``content_hash``;
3. appends it to the configured store.

It is **best-effort and never fatal**: any failure — building the record,
hashing, or the store append — is logged and swallowed, so the regulated
workflow can never break because the trajectory backend misbehaved. It is also
**write-only on the decision path**: it returns ``None`` and changes no graph
state, gate, route, score, or figure.

Offline: with no ``trajectory_dsn`` configured the store is
:class:`NullTrajectoryStore`, so this is a no-op (the record is still built but
discarded by the no-op append), keeping the system byte-stable.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from app.backend.audit.record import compute_data_version
from app.backend.trajectory.record import (
    NodeSnapshot,
    TrajectoryDecision,
    TrajectoryRecord,
)
from app.backend.trajectory.store import TrajectoryStore, get_trajectory_store
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = ["record_trajectory", "build_input_summary", "build_node_trajectory"]

_log = get_logger(__name__)


def build_input_summary(state: Any) -> dict[str, Any]:
    """Build a compact, deterministic snapshot of the strategist's inputs.

    Captures just enough to condition / train a model without storing the whole
    state object: the FSA classification (as its string value), the EWS score,
    the working-capital gap, and the revision round. Reads defensively via
    ``getattr`` so it works on a live ``SaiseiState`` or any state-like object.

    Args:
        state: The graph state (or state-like object) for the borrower.

    Returns:
        A JSON-serialisable summary dict.
    """
    fsa = getattr(state, "fsa_classification", None)
    fsa_value = getattr(fsa, "value", fsa)
    return {
        "fsa_classification": fsa_value if fsa_value is None else str(fsa_value),
        "ews_score": getattr(state, "ews_score", None),
        "working_capital_gap": getattr(state, "working_capital_gap", None),
        "revision_count": int(getattr(state, "revision_count", 0) or 0),
    }


def _digest(item: Any) -> Any:
    """Coerce a value to a JSON-serialisable digest (model -> dict, recurse list)."""
    if item is None or isinstance(item, (str, int, float, bool)):
        return item
    if isinstance(item, dict):
        return item
    if isinstance(item, (list, tuple)):
        return [_digest(x) for x in item]
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    return str(item)


def build_node_trajectory(state: Any) -> list[NodeSnapshot]:
    """Reconstruct the ordered per-node trajectory from the accumulated state.

    By the time the graph reaches the HITL interrupt, ``SaiseiState`` already
    holds every upstream node's output. This reads them into an ordered list of
    compact, deterministic :class:`NodeSnapshot` digests (graph order) so the
    captured record carries the full agentic path — not just the negotiation
    summary — for the offline training flywheel (Feature 3.1).

    Pure / deterministic / defensive: reads only via ``getattr`` (works on a
    live ``SaiseiState`` or any state-like object), coerces pydantic sub-models
    to JSON-mode dicts, and never raises on a missing field. No graph change, no
    LLM, no I/O. A node whose output is absent yields a snapshot with empty /
    None values rather than being omitted, so the trajectory shape is stable.

    Args:
        state: The graph state (or state-like object) for the borrower.

    Returns:
        The ordered per-node snapshots (intake .. lead_arranger).
    """

    def g(name: str, default: Any = None) -> Any:
        return getattr(state, name, default)

    profile = g("company_profile")
    rate_curve = g("boj_rate_curve", []) or []
    latest_rate = _digest(rate_curve[-1]) if rate_curve else None

    snapshots: list[tuple[str, dict[str, Any]]] = [
        (
            "intake",
            {
                "tdb_code": g("tdb_code"),
                "hojin_bango": g("hojin_bango"),
                "company_name": getattr(profile, "name", None),
                "industry": getattr(profile, "industry", None),
                "tdb_score": g("tdb_score"),
                "is_insolvent": g("is_insolvent"),
            },
        ),
        (
            "ews",
            {
                "ews_score": g("ews_score"),
                "ews_breakdown": _digest(g("ews_breakdown", [])),
            },
        ),
        (
            "macro",
            {
                "working_capital_gap": g("working_capital_gap"),
                "latest_rate_point": latest_rate,
                "settlement_metrics": _digest(g("settlement_metrics")),
            },
        ),
        (
            "classifier",
            {
                "fsa_classification": _digest(g("fsa_classification")),
                "special_attention": g("special_attention"),
                "classification_reason": g("classification_reason", ""),
                "net_worth": g("net_worth"),
            },
        ),
        (
            "keieisha_hosho",
            {
                "hosho_kaijo_score": g("hosho_kaijo_score"),
                "hosho_kaijo_eligible": g("hosho_kaijo_eligible"),
                "succession_ready": g("succession_ready"),
            },
        ),
        (
            "strategist",
            {
                "proposed_strategies": _as_dicts(g("proposed_strategies", [])),
                "revision_count": int(g("revision_count", 0) or 0),
            },
        ),
        (
            "feasibility_critic",
            {
                "feasibility_notes": _digest(g("feasibility_notes", [])),
                "reconciliation_required": bool(g("reconciliation_required", False)),
                "reconciliation_details": _digest(g("reconciliation_details", [])),
            },
        ),
        (
            "critics",
            {"critic_feedbacks": _digest(g("critic_feedbacks", []))},
        ),
        (
            "lead_arranger",
            {
                "negotiation_status": g("negotiation_status", ""),
                "revision_directive": g("revision_directive"),
                "meeting_briefing": g("meeting_briefing"),
            },
        ),
    ]
    return [NodeSnapshot(node=node, output=output) for node, output in snapshots]


def _as_dicts(items: Any) -> list[dict[str, Any]]:
    """Coerce a list of pydantic models / dicts into a list of plain dicts."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(item)
        elif hasattr(item, "model_dump"):
            out.append(item.model_dump(mode="json"))
    return out


def _as_dict(item: Any) -> dict[str, Any] | None:
    """Coerce a single pydantic model / dict / None into a plain dict or None."""
    if item is None:
        return None
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")  # type: ignore[no-any-return]
    return None


def record_trajectory(
    *,
    state: Any,
    decision: TrajectoryDecision | str,
    thread_id: str | None = None,
    actor: str = "system",
    revision_note: str = "",
    approved_strategy: Any = None,
    node_trajectory: list[NodeSnapshot] | None = None,
    interrupt_payload: dict[str, Any] | None = None,
    settings: Settings | None = None,
    store: TrajectoryStore | None = None,
) -> None:
    """Build and append one trajectory record. Best-effort; never raises.

    The single write helper for the data flywheel. It snapshots the strategist's
    inputs, the proposed strategies, the banker's decision + note, the approved
    strategy, and the final plan (when present), seals the record with its
    ``content_hash``, and appends it to the configured store. Any exception is
    logged and swallowed so the workflow is never broken by the side-record.

    It returns ``None`` and mutates no graph state — a side-effect only.

    Args:
        state: The graph state (source of identity, inputs, strategies, plan).
        decision: The banker's decision ('approve' | 'revise' | 'reject').
        thread_id: Explicit run thread id. In LangGraph the thread_id lives in
            the run *config*, not in ``SaiseiState``, so call sites pass it
            explicitly. Falls back to ``state.thread_id`` then "".
        actor: The banker id who decided (or a placeholder).
        revision_note: The banker's free-text note (critique). Defaults to the
            ``revision_note`` on state when not supplied.
        approved_strategy: Explicit approved strategy (model or dict). On the
            HITL approve path the chosen strategy is in the node's return dict,
            not yet on ``state``, so the call site passes it here; falls back to
            ``state.approved_strategy`` when not supplied.
        node_trajectory: Feature 3.1 — the ordered per-node output digests to
            seal into the record (typically from :func:`build_node_trajectory`).
            When ``None`` the record's ``node_trajectory`` is left empty, so the
            shipped negotiation-summary-only behaviour is unchanged unless the
            caller opts in.
        interrupt_payload: Feature 3.1 — the raw HITL interrupt payload the
            banker saw. When ``None`` the record's ``interrupt_payload`` is empty.
        settings: Optional settings override (defaults to cached settings).
        store: Optional store override (defaults to the configured store).
    """
    try:
        settings = settings or get_settings()
        store = store if store is not None else get_trajectory_store(settings)

        resolved_thread_id = str(
            thread_id if thread_id is not None else (getattr(state, "thread_id", "") or "")
        )
        note = revision_note or str(getattr(state, "revision_note", "") or "")
        decision_enum = (
            decision
            if isinstance(decision, TrajectoryDecision)
            else TrajectoryDecision(str(decision))
        )

        record = TrajectoryRecord(
            trajectory_id=str(uuid.uuid4()),
            thread_id=resolved_thread_id,
            tdb_code=str(getattr(state, "tdb_code", "") or ""),
            hojin_bango=str(getattr(state, "hojin_bango", "") or ""),
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            actor=actor,
            decision=decision_enum,
            revision_note=note,
            data_version=compute_data_version(state),
            input_summary=build_input_summary(state),
            proposed_strategies=_as_dicts(getattr(state, "proposed_strategies", [])),
            approved_strategy=_as_dict(
                approved_strategy
                if approved_strategy is not None
                else getattr(state, "approved_strategy", None)
            ),
            keikakusho_draft=str(getattr(state, "keikakusho_draft", "") or ""),
            node_trajectory=list(node_trajectory or []),
            interrupt_payload=dict(interrupt_payload or {}),
        ).with_content_hash()

        store.append(record)
        _log.info(
            "trajectory.recorded",
            thread_id=resolved_thread_id,
            tdb_code=record.tdb_code,
            decision=str(decision_enum),
            trajectory_id=record.trajectory_id,
        )
    except Exception as exc:  # noqa: BLE001 - flywheel is best-effort, never fatal
        _log.warning("trajectory.record_failed", error=str(exc))
