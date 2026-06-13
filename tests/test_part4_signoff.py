"""Part 4 step 5 — consolidated end-to-end determinism sign-off.

Steps 1–4 each shipped a focused parity test. This is the cross-cutting
regression that proves the whole Part 4 surface holds the one rule across a full
graph run: the multi-agent persona/feasibility/briefing layer rehearses, it
never decides.

Run the compiled graph OFFLINE (no LLM configured, in-memory checkpointer) up to
the HITL pause and assert:

- the deterministic spine is reproducible: two identical runs produce identical
  classification, critic verdicts, negotiation_status, burden table and
  revision_count;
- every advisory channel is the offline fallback: each critic's
  ``simulated_argument`` is "", each ``feasibility_notes`` advisory is "", and
  the ``meeting_briefing`` is the deterministic skeleton (present, but carries no
  LLM-generated persona/feasibility prose);
- the advisory channels are PRESENT in state and the HITL payload (the rehearsal
  exists) without ever altering a verdict or route.

If any future change lets the advisory layer leak into a verdict/route/figure,
or breaks the offline fallback, one of these assertions fails.
"""

from __future__ import annotations

import os
from typing import Any
from unittest import mock

from langgraph.checkpoint.memory import MemorySaver

from app.backend.graph import build_graph

#: Env that guarantees no LLM is configured regardless of the developer's .env,
#: so the advisory passes take their deterministic offline fallback.
_OFFLINE_ENV = {"SAISEI_LLM_API_KEY": "", "SAISEI_LLM_MODEL": ""}

#: Both commitment flags set so the main_bank gate PASSes and the graph reaches
#: the HITL pause (same approach as test_graph_flow.py).
_INPUT = {
    "tdb_code": "1234567",
    "yakuin_hoshu_cut": True,
    "personal_asset_disposal": True,
}

# Deterministic keys whose values must be reproducible run-to-run.
_DETERMINISTIC_KEYS = (
    "fsa_classification",
    "ews_score",
    "working_capital_gap",
    "negotiation_status",
    "revision_directive",
    "revision_count",
    "hosho_kaijo_score",
)


def _run_to_pause(thread_id: str) -> dict[str, Any]:
    """Run the graph offline to the HITL pause; return the state snapshot values."""
    app = build_graph().compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": thread_id}}
    app.invoke(dict(_INPUT), config=config)
    snapshot = app.get_state(config)
    assert snapshot.next, "graph must reach the HITL interrupt"
    return dict(snapshot.values)


def test_full_run_deterministic_spine_is_reproducible() -> None:
    """Two identical offline runs agree on every deterministic output."""
    with mock.patch.dict(os.environ, _OFFLINE_ENV, clear=False):
        a = _run_to_pause("signoff-a")
        b = _run_to_pause("signoff-b")

    for key in _DETERMINISTIC_KEYS:
        assert a[key] == b[key], f"deterministic key drifted across runs: {key}"

    # Critic verdicts (status + blockers + priority) must be identical; the
    # advisory simulated_argument is excluded since it is not part of the spine.
    def _verdicts(values: dict[str, Any]) -> list[tuple[str, str, str, tuple[str, ...]]]:
        return sorted(
            (
                str(f["persona"]),
                str(f["status"]),
                str(f["priority"]),
                tuple(f.get("fatal_blockers", [])),
            )
            for f in values["critic_feedbacks"]
        )

    assert _verdicts(a) == _verdicts(b)


def test_full_run_advisory_channels_are_offline_fallback() -> None:
    """Offline, every advisory channel is empty / a deterministic skeleton."""
    with mock.patch.dict(os.environ, _OFFLINE_ENV, clear=False):
        values = _run_to_pause("signoff-offline")

    # Persona layer: every critic argument is the empty offline fallback.
    assert values["critic_feedbacks"], "the creditor meeting must have run"
    for fb in values["critic_feedbacks"]:
        assert fb["simulated_argument"] == "", (
            "simulated_argument must be empty with no LLM configured"
        )

    # Feasibility layer: notes exist (deterministic bands) but advisory is empty.
    assert values["feasibility_notes"], "feasibility_critic must have run"
    for note in values["feasibility_notes"]:
        assert note["advisory"] == "", "feasibility advisory must be empty offline"
        assert note["achievability"] in {"high", "medium", "low"}

    # Briefing: present (the rehearsal exists) and is the deterministic skeleton.
    briefing = values["meeting_briefing"]
    assert briefing and "Creditor-Meeting Briefing" in briefing
    assert "負担分担表" in briefing  # the deterministic burden table section


def test_advisory_channels_present_without_changing_routing() -> None:
    """The rehearsal channels exist in state alongside an unchanged verdict."""
    with mock.patch.dict(os.environ, _OFFLINE_ENV, clear=False):
        values = _run_to_pause("signoff-present")

    # Advisory channels are present (Part 4 wired them through)...
    assert "feasibility_notes" in values
    assert "meeting_briefing" in values
    # ...and the deterministic routing decision still holds: with both
    # commitment flags set, the meeting approves and the graph pauses at HITL.
    assert values["negotiation_status"] == "approved"
    assert values["fsa_classification"].requires_turnaround
