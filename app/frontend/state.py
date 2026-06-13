"""Reflex application state for Saisei.

Drives the UI from the compiled LangGraph as a *streamed creditor meeting*: it
streams the graph node-by-node (``stream_mode="updates"``) so each agent's result
appears in the transcript the moment its node finishes, runs to the HITL
interrupt, surfaces proposed strategies + the lead arranger's burden table, and
resumes the graph with the banker's decision via ``Command(resume=...)``. State
persists through the interrupt using the Postgres checkpointer, keyed by a
per-session ``thread_id``.

The UI is display-only: it never computes a verdict or a number. Every value
shown is read from streamed node updates or the final snapshot.

This module is the canonical location under ``app.frontend.state``.
The legacy path ``saisei_ui.state`` re-exports from here.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from queue import Queue as _Queue
from typing import Any

import reflex as rx
from langgraph.types import Command
from pydantic import BaseModel

from app.backend.graph import compile_graph, postgres_checkpointer
from app.shared.logging import get_logger
from app.shared.models.classification import FsaClass
from app.shared.models.money import format_jpy

_log = get_logger(__name__)


class MeetingEvent(BaseModel):
    """A single typed transcript event for the creditor-meeting panel.

    Declared as an ``rx.Base`` model (not a bare ``dict``) so Reflex can
    introspect the field types: ``rx.foreach`` and per-field access in the UI
    require a typed var, otherwise it raises ``UntypedVarError``.
    """

    kind: str = ""  # system | critic | chair | banker
    speaker: str = "system"  # persona key (matches theme.PERSONAS)
    status: str = ""  # PASS | FAIL | APPROVED | ... (may be empty)
    priority: str = ""  # P0 | P1 | P2 (may be empty)
    title: str = ""
    body: str = ""
    blockers: list[str] = []


#: Human-readable narration for each graph node as it completes. Keys match the
#: node names registered in ``app/backend/graph.py``. Used to build system
#: transcript bubbles so the banker can follow the assessment in plain language.
_NODE_NARRATION: dict[str, str] = {
    "intake": "企業プロファイルを取得しました。 (Company profile loaded.)",
    "ews": "試算表を分析し、EWSスコアを算出しました。 (Trial balances scored.)",
    "macro": "マクロ指標・資金繰りギャップを算出しました。 (Macro & liquidity gap computed.)",
    "classifier": "債務者区分を判定しました。 (FSA classification decided.)",
    "keieisha_hosho": "経営者保証解除の評価を完了しました。 (Guarantee-release assessed.)",
    "strategist": (
        "再生戦略を立案しました。債権者会議を開始します。 "
        "(Strategies drafted; convening creditors.)"
    ),
    "plan_writer": "経営改善計画書を作成しました。 (Keikakusho drafted.)",
}

#: Node names that emit a critic CriticFeedback we render as a persona bubble.
_CRITIC_NODES: frozenset[str] = frozenset(
    {"main_bank_critic", "sub_bank_critic", "guarantor_critic"}
)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a model attribute or a mapping key.

    Snapshot/stream values returned by the LangGraph Postgres checkpointer may be
    rehydrated as plain dicts (or enums as plain strings) rather than the
    original Pydantic models. This helper reads uniformly from either shape so
    the UI never assumes a live object.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _fsa_kanji(classification: Any) -> str:
    """Return the FSA classification kanji, accepting an enum, str, or None."""
    if not classification:
        return ""
    if isinstance(classification, FsaClass):
        return classification.kanji
    try:
        return FsaClass(str(classification)).kanji
    except ValueError:
        # Unknown value (e.g. already a kanji label) — display as-is.
        return str(classification)


def _period_str(period: Any) -> str:
    """Return an ISO date string for a date or an already-serialized string."""
    if isinstance(period, dt.date):
        return period.isoformat()
    return str(period or "")


class SaiseiUIState(rx.State):
    """UI state backing the Saisei meeting-room dashboard."""

    # --- Inputs ---
    tdb_code: str = "1234567"
    thread_id: str = ""
    revision_note_buffer: str = ""

    # --- Lifecycle phase (drives progress UI; replaces the blank wait) ---
    #: idle | assessing | meeting | awaiting_decision | drafting | done | error
    phase: str = "idle"
    active_node: str = ""

    # --- Case-file display fields ---
    company_name: str = ""
    fsa_kanji: str = ""
    ews_score: float = 0.0
    working_capital_gap_display: str = "—"

    # PART 2: Hosho Kaijo display fields.
    hosho_kaijo_score: float = 0.0
    succession_ready: bool = False

    # PART 3: Creditor-meeting display fields.
    negotiation_status: str = "pending"
    revision_directive: str = ""
    revision_count: int = 0

    shisanhyo_rows: list[dict[str, str]] = []
    strategies: list[dict[str, str]] = []
    burden_rows: list[dict[str, str]] = []

    # --- Meeting transcript (chat-style, streamed) ---
    #: Typed events so rx.foreach can introspect the element type.
    meeting_events: list[MeetingEvent] = []

    # --- HITL commitment flags (banker-only gates) ---
    yakuin_hoshu_cut: bool = False
    personal_asset_disposal: bool = False

    # --- Outcome ---
    awaiting_decision: bool = False
    keikakusho_draft: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    # Derived helpers (used by the UI)
    # ------------------------------------------------------------------

    @rx.var
    def is_running(self) -> bool:
        """Whether the graph is actively executing (show progress UI)."""
        return self.phase in ("assessing", "meeting", "drafting")

    @rx.var
    def code_valid(self) -> bool:
        """Whether the TDB code is a well-formed 7-digit code."""
        return self.tdb_code.isdigit() and len(self.tdb_code) == 7

    @rx.var
    def has_started(self) -> bool:
        """Whether an assessment has been started this session."""
        return self.phase != "idle"

    @rx.var
    def classification_label(self) -> str:
        """FSA classification with a graceful empty fallback."""
        return self.fsa_kanji or "—"

    # ------------------------------------------------------------------
    # Internal mapping helpers
    # ------------------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    def _reset_run(self) -> None:
        """Clear per-run UI state before a fresh assessment."""
        self.error = ""
        self.keikakusho_draft = ""
        self.meeting_events = []
        self.strategies = []
        self.burden_rows = []
        self.shisanhyo_rows = []
        self.awaiting_decision = False
        self.negotiation_status = "pending"
        self.revision_directive = ""
        self.revision_count = 0
        self.active_node = ""

    def _push_event(self, event: MeetingEvent) -> None:
        """Append a transcript event (kept as a new list for Reflex reactivity)."""
        self.meeting_events = [*self.meeting_events, event]

    def _ingest_node_update(self, node: str, update: dict[str, Any]) -> None:
        """Translate one streamed node update into transcript event(s).

        Args:
            node: The graph node name that just completed.
            update: The partial state dict the node returned.
        """
        self.active_node = node

        # Critic nodes append CriticFeedback dicts — render each as a persona bubble.
        if node in _CRITIC_NODES:
            for fb in update.get("critic_feedbacks", []) or []:
                self._push_event(
                    MeetingEvent(
                        kind="critic",
                        speaker=str(_attr(fb, "persona", "system")),
                        status=str(_attr(fb, "status", "")),
                        priority=str(_attr(fb, "priority", "")),
                        title=str(_attr(fb, "rationale", "")),
                        blockers=[str(b) for b in _attr(fb, "fatal_blockers", []) or []],
                        body="",
                    )
                )
            return

        # Lead arranger speaks as the chair with the consolidated outcome.
        if node == "lead_arranger":
            self.phase = "meeting"
            status = str(update.get("negotiation_status", ""))
            self._push_event(
                MeetingEvent(
                    kind="chair",
                    speaker="lead_arranger",
                    status=status.upper(),
                    priority="",
                    title=_CHAIR_TITLES.get(status, "取りまとめ (Consolidation)"),
                    body=str(update.get("revision_directive", "")),
                    blockers=[],
                )
            )
            return

        # Otherwise emit a plain system-narration bubble when we have copy for it.
        narration = _NODE_NARRATION.get(node)
        if narration:
            self._push_event(
                MeetingEvent(
                    kind="system",
                    speaker="system",
                    status="",
                    priority="",
                    title=narration,
                    body="",
                    blockers=[],
                )
            )

    def _apply_snapshot(self, values: dict[str, Any]) -> None:
        """Map final graph state values onto case-file display fields."""
        profile = values.get("company_profile")
        self.company_name = _attr(profile, "name", "") or self.tdb_code
        self.fsa_kanji = _fsa_kanji(values.get("fsa_classification"))
        self.ews_score = float(values.get("ews_score") or 0.0)

        gap = values.get("working_capital_gap")
        self.working_capital_gap_display = format_jpy(gap) if gap is not None else "—"

        # PART 2: Hosho Kaijo.
        self.hosho_kaijo_score = float(values.get("hosho_kaijo_score") or 0.0)
        self.succession_ready = bool(values.get("succession_ready") or False)

        # PART 3: Creditor meeting.
        self.negotiation_status = str(values.get("negotiation_status") or "pending")
        self.revision_directive = str(values.get("revision_directive") or "")
        self.revision_count = int(values.get("revision_count") or 0)

        self.shisanhyo_rows = [
            {
                "period": _period_str(_attr(tb, "period")),
                "uriage": format_jpy(int(_attr(tb, "uriage", 0))),
                "uriage_genka": format_jpy(int(_attr(tb, "uriage_genka", 0))),
                "keijo_rieki": format_jpy(int(_attr(tb, "keijo_rieki", 0))),
            }
            for tb in values.get("shisanhyo", [])
        ]
        self.strategies = [
            {
                "index": str(i),
                "title": str(_attr(s, "title", "")),
                "rationale": str(_attr(s, "rationale", "")),
                "uplift": format_jpy(int(_attr(s, "expected_keijo_uplift", 0))),
            }
            for i, s in enumerate(values.get("proposed_strategies", []))
        ]
        self.keikakusho_draft = values.get("keikakusho_draft") or ""

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @rx.event(background=True)
    async def run_assessment(self):
        """Stream the graph from start to the HITL interrupt as a live meeting.

        Runs in the background so the UI stays responsive and each node's result
        is appended to the transcript as it completes (no blank wait).
        """
        async with self:
            if not self.code_valid:
                self.error = "TDB企業コードは7桁の数字で入力してください。 (Enter a 7-digit code.)"
                return
            self._reset_run()
            self.thread_id = str(uuid.uuid4())
            self.phase = "assessing"
            tdb_code = self.tdb_code
            config = self._config()
            self._push_event(
                MeetingEvent(
                    kind="system",
                    speaker="system",
                    title=f"診断を開始しました・TDB {tdb_code} (Assessment started.)",
                )
            )

        async for _ in self._drive_stream({"tdb_code": tdb_code}, config, resume=False):
            yield

    async def _drive_stream(
        self, payload: dict[str, Any], config: dict[str, Any], *, resume: bool
    ):
        """Drive a graph stream, applying each node update on the event loop.

        A worker thread runs the *blocking* LangGraph stream and pushes each
        ``{node: update}`` chunk onto a thread-safe queue. This coroutine drains
        the queue, applies each update under the Reflex state lock
        (``async with self``), and ``yield``s so the delta is flushed to the
        browser before the next event — giving a true live transcript.

        Mutating Reflex state must happen on the event loop under the lock, never
        from the worker thread; the queue is the hand-off boundary.
        """
        queue: _Queue[Any] = _Queue()
        _DONE = object()

        def _worker() -> None:
            try:
                with postgres_checkpointer() as cp:
                    graph_app = compile_graph(checkpointer=cp)
                    command = Command(resume=payload) if resume else payload
                    for chunk in graph_app.stream(
                        command, config=config, stream_mode="updates"
                    ):
                        queue.put(("chunk", chunk))
            except Exception as exc:  # noqa: BLE001 - propagate to the UI thread
                queue.put(("error", str(exc)))
            finally:
                queue.put(("done", _DONE))

        worker = asyncio.create_task(asyncio.to_thread(_worker))
        try:
            while True:
                kind, payload_item = await asyncio.to_thread(queue.get)
                if kind == "done":
                    break
                if kind == "error":
                    async with self:
                        self.error = str(payload_item)
                        self.phase = "error"
                    await worker
                    return
                # kind == "chunk"
                async with self:
                    for node, update in payload_item.items():
                        if isinstance(update, dict):
                            self._ingest_node_update(node, update)
                yield
        finally:
            await worker

        async with self:
            self._finalize_after_stream(config)
        yield

    def _finalize_after_stream(self, config: dict[str, Any]) -> None:
        """Read the final snapshot and settle the phase after streaming."""
        with postgres_checkpointer() as cp:
            graph_app = compile_graph(checkpointer=cp)
            snapshot = graph_app.get_state(config)
        values = dict(snapshot.values)
        self._apply_snapshot(values)
        self._refresh_burden_rows(values)
        self.active_node = ""
        if snapshot.next:
            self.awaiting_decision = True
            self.phase = "awaiting_decision"
        else:
            self.phase = "done"

    def _refresh_burden_rows(self, values: dict[str, Any]) -> None:
        """Recompute the burden-sharing table for display from current state.

        Deterministic and display-only — reuses the backend helper so the table
        shown matches exactly what the lead arranger consolidated.
        """
        try:
            from app.backend.nodes.lead_arranger import compute_burden_sharing_table
            from app.backend.state import SaiseiState

            state = SaiseiState(**values)
            rows = compute_burden_sharing_table(state)
        except Exception:  # noqa: BLE001 - table is best-effort, never fatal to UI
            self.burden_rows = []
            return
        self.burden_rows = [
            {
                "persona": str(r.get("persona", "")),
                "lender": str(r.get("lender", "")),
                "share": f"{r.get('share_pct', 0)}%",
                "grace": f"{r.get('grace_period_months', 0)}ヶ月",
                "haircut": f"{r.get('haircut_pct', 0)}%",
                "new_money": format_jpy(int(r.get("new_money_jpy", 0))),
                "allocation": str(r.get("allocation_type", "")),
            }
            for r in rows
        ]

    # --- Resume (HITL decisions), streamed ---

    @rx.event(background=True)
    async def resume_streamed(self, payload: dict[str, Any]):
        """Resume the graph after a banker decision, streaming the continuation.

        Drives the same queue-based ``_drive_stream`` used by the initial run
        (with ``resume=True``) so node updates are applied under the Reflex state
        lock on the event loop and flushed live per chunk — never mutated from
        the worker thread. ``_drive_stream`` also finalizes the snapshot itself.
        """
        async with self:
            self.awaiting_decision = False
            self.phase = "drafting" if payload.get("decision") == "approve" else "meeting"
            config = self._config()
            self._push_event(
                MeetingEvent(
                    kind="banker",
                    speaker="banker",
                    status=str(payload.get("decision", "")).upper(),
                    title=_BANKER_TITLES.get(
                        str(payload.get("decision", "")), "決定 (Decision)"
                    ),
                    body=str(payload.get("revision_note", "") or ""),
                )
            )

        async for _ in self._drive_stream(payload, config, resume=True):
            yield

    # --- Input + flag setters ---

    @rx.event
    def set_tdb_code(self, tdb_code: str) -> None:
        """Update the TDB code and reset dependent UI fields."""
        self.tdb_code = tdb_code.strip()
        self.error = ""

    @rx.event
    def set_revision_note_buffer(self, note: str) -> None:
        """Update the banker's revision note buffer."""
        self.revision_note_buffer = note
        if self.error:
            self.error = ""

    @rx.event
    def toggle_yakuin_hoshu_cut(self, value: bool) -> None:
        """Banker toggles the executive-compensation-cut commitment flag."""
        self.yakuin_hoshu_cut = value

    @rx.event
    def toggle_personal_asset_disposal(self, value: bool) -> None:
        """Banker toggles the personal-asset-disposal commitment flag."""
        self.personal_asset_disposal = value

    # --- HITL decision events (delegate to the streamed resume) ---

    @rx.event
    def approve(self, index: int):
        """Approve a proposed strategy by index and resume the graph."""
        return SaiseiUIState.resume_streamed(
            {
                "decision": "approve",
                "strategy_index": index,
                "yakuin_hoshu_cut": self.yakuin_hoshu_cut,
                "personal_asset_disposal": self.personal_asset_disposal,
            }
        )

    @rx.event
    def revise(self):
        """Request a revision with the buffered banker note and resume.

        Carries the commitment flags too: a ``needs_human`` rejection is
        cleared by the banker toggling the flags and re-submitting, so the
        revise payload must persist them or the deadlock would never break.
        """
        return SaiseiUIState.resume_streamed(
            {
                "decision": "revise",
                "revision_note": self.revision_note_buffer,
                "yakuin_hoshu_cut": self.yakuin_hoshu_cut,
                "personal_asset_disposal": self.personal_asset_disposal,
            }
        )

    @rx.event
    def reject(self):
        """Reject all strategies and resume the graph to escalation."""
        return SaiseiUIState.resume_streamed(
            {
                "decision": "reject",
                "yakuin_hoshu_cut": self.yakuin_hoshu_cut,
                "personal_asset_disposal": self.personal_asset_disposal,
            }
        )


#: Chair (lead arranger) bubble titles keyed by negotiation_status.
_CHAIR_TITLES: dict[str, str] = {
    "approved": "【承認】全評価者がPASSしました (Approved by all critics)",
    "rejected": "【差し戻し】修正が必要です (Revision required)",
    "needs_human": "【担当者確認】コミットメントが必要です (Needs your confirmation)",
    "pending": "取りまとめ中 (Consolidating…)",
}

#: Banker bubble titles keyed by decision.
_BANKER_TITLES: dict[str, str] = {
    "approve": "承認しました (Approved)",
    "revise": "修正を依頼しました (Requested revision)",
    "reject": "却下しました (Rejected)",
}
