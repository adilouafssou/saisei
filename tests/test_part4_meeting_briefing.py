"""Part 4 step 4 (meeting_briefing) — determinism-parity tests.

lead_arranger now assembles an advisory ``meeting_briefing`` from the
deterministic verdict + per-persona ``simulated_argument`` + ``feasibility_notes``.
These tests prove the deterministic spine is untouched and the briefing is
offline-safe:

- a deterministic briefing is emitted on every path (needs_human / approved);
- the briefing is reproducible offline;
- populating the advisory persona arguments / feasibility notes changes ONLY the
  briefing text — status, revision_directive, revision_count and the burden
  table are byte-identical, because nothing downstream of the gate reads the
  advisory channel.

If a later step lets the briefing influence a verdict/route/figure, one of these
assertions must fail — that is the guardrail.
"""

from __future__ import annotations

from app.backend.nodes.lead_arranger import lead_arranger_node
from app.backend.state import CriticFeedback, SaiseiState, Strategy


def _strategy(title: str, uplift: int) -> Strategy:
    return Strategy(title=title, rationale="test", expected_keijo_uplift=uplift)


def _full_strategies() -> list[Strategy]:
    return [
        _strategy("price", 43_920_000),
        _strategy("cogs", 30_960_000),
        _strategy("sga", 14_100_000),
        _strategy("wc", 5_000_000),
    ]


def _feedback(
    persona: str,
    status: str,
    priority: str,
    blockers: list[str] | None = None,
    simulated_argument: str = "",
) -> dict:
    return CriticFeedback(
        persona=persona,
        status=status,
        fatal_blockers=blockers or [],
        priority=priority,
        rationale=f"{persona}: {status}",
        simulated_argument=simulated_argument,
    ).model_dump()


def _passing_feedbacks(simulated: bool = False) -> list[dict]:
    arg = "主張テキスト" if simulated else ""
    return [
        _feedback("guarantor", "PASS", "P0", simulated_argument=arg),
        _feedback("main_bank", "PASS", "P1", simulated_argument=arg),
        _feedback("sub_bank", "PASS", "P2", simulated_argument=arg),
    ]


def _state(feedbacks: list[dict], feasibility_notes: list[dict] | None = None) -> SaiseiState:
    return SaiseiState(
        tdb_code="1234567",
        critic_feedbacks=feedbacks,
        feasibility_notes=feasibility_notes or [],
        proposed_strategies=_full_strategies(),
        working_capital_gap=-5_000_000,
        revision_count=0,
    )


def test_briefing_emitted_and_reproducible_offline() -> None:
    """A deterministic briefing is produced and is byte-stable across runs."""
    a = lead_arranger_node(_state(_passing_feedbacks()))
    b = lead_arranger_node(_state(_passing_feedbacks()))
    assert a["meeting_briefing"]
    assert "Creditor-Meeting Briefing" in a["meeting_briefing"]
    assert a == b


def test_advisory_changes_only_the_briefing_text() -> None:
    """Persona arguments change the briefing text but nothing else.

    Same gate verdicts, but one run carries populated simulated_argument values.
    Every consolidated key EXCEPT meeting_briefing must be identical, proving the
    advisory channel never feeds status / directive / count / burden.
    """
    plain = lead_arranger_node(_state(_passing_feedbacks(simulated=False)))
    with_args = lead_arranger_node(_state(_passing_feedbacks(simulated=True)))

    plain_rest = {k: v for k, v in plain.items() if k != "meeting_briefing"}
    with_rest = {k: v for k, v in with_args.items() if k != "meeting_briefing"}
    assert plain_rest == with_rest

    # The briefing itself DID pick up the advisory argument.
    assert "主張テキスト" in with_args["meeting_briefing"]
    assert "主張テキスト" not in plain["meeting_briefing"]


def test_feasibility_notes_change_only_the_briefing_text() -> None:
    """Feasibility notes are surfaced in the briefing but never gate/route."""
    note = {
        "strategy_title": "price",
        "achievability": "high",
        "achievability_score": 95.0,
        "rationale": "r",
        "advisory": "実現可能性コメント",
    }
    without = lead_arranger_node(_state(_passing_feedbacks()))
    with_notes = lead_arranger_node(_state(_passing_feedbacks(), feasibility_notes=[note]))

    without_rest = {k: v for k, v in without.items() if k != "meeting_briefing"}
    with_rest = {k: v for k, v in with_notes.items() if k != "meeting_briefing"}
    assert without_rest == with_rest
    assert "実現可能性コメント" in with_notes["meeting_briefing"]


def test_needs_human_path_also_emits_briefing() -> None:
    """The banker-only needs_human path carries a briefing for the rehearsal."""
    feedbacks = [
        _feedback("guarantor", "PASS", "P0"),
        _feedback(
            "main_bank",
            "FAIL",
            "P1",
            ["yakuin_hoshu_not_cut: confirm exec-comp cut"],
        ),
        _feedback("sub_bank", "PASS", "P2"),
    ]
    result = lead_arranger_node(_state(feedbacks))
    assert result["negotiation_status"] == "needs_human"
    assert result["meeting_briefing"]
    assert result["revision_count"] == 0
