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

from typing import Any

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
) -> dict[str, Any]:
    return CriticFeedback(
        persona=persona,
        status=status,
        fatal_blockers=blockers or [],
        priority=priority,
        rationale=f"{persona}: {status}",
        simulated_argument=simulated_argument,
    ).model_dump()


def _passing_feedbacks(simulated: bool = False) -> list[dict[str, Any]]:
    arg = "主張テキスト" if simulated else ""
    return [
        _feedback("guarantor", "PASS", "P0", simulated_argument=arg),
        _feedback("main_bank", "PASS", "P1", simulated_argument=arg),
        _feedback("sub_bank", "PASS", "P2", simulated_argument=arg),
    ]


def _state(
    feedbacks: list[dict[str, Any]],
    feasibility_notes: list[dict[str, Any]] | None = None,
) -> SaiseiState:
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


def test_uplift_credibility_rendered_in_briefing() -> None:
    """Depth step 4 part 2: an implausible uplift verdict is rendered IN the
    rehearsal prose (with its over-claim multiple), and only the briefing text
    changes -- status / directive / count / burden stay byte-identical."""
    note = {
        "strategy_title": "price",
        "achievability": "high",
        "achievability_score": 95.0,
        "rationale": "r",
        "advisory": "",
        "uplift_credibility": "implausible",
        "uplift_credibility_ratio": 2.3,
        "uplift_credibility_reason": "det reason",
    }
    without = lead_arranger_node(_state(_passing_feedbacks()))
    with_notes = lead_arranger_node(_state(_passing_feedbacks(), feasibility_notes=[note]))

    without_rest = {k: v for k, v in without.items() if k != "meeting_briefing"}
    with_rest = {k: v for k, v in with_notes.items() if k != "meeting_briefing"}
    assert without_rest == with_rest  # spine untouched

    briefing = with_notes["meeting_briefing"]
    assert "上乗せ妥当性" in briefing
    assert "implausible" in briefing
    assert "2.3倍" in briefing  # the over-claim multiple is shown


def test_grounded_uplift_omits_multiple() -> None:
    """A grounded verdict (ratio <= 1.0) shows the label without a multiple."""
    note = {
        "strategy_title": "price",
        "achievability": "high",
        "achievability_score": 95.0,
        "rationale": "r",
        "advisory": "",
        "uplift_credibility": "grounded",
        "uplift_credibility_ratio": 0.4,
        "uplift_credibility_reason": "det reason",
    }
    result = lead_arranger_node(_state(_passing_feedbacks(), feasibility_notes=[note]))
    briefing = result["meeting_briefing"]
    assert "上乗せ妥当性" in briefing
    assert "grounded" in briefing
    assert "倍）" not in briefing  # no over-claim multiple for a grounded claim


def test_unassessed_uplift_omits_credibility_line() -> None:
    """A note with no uplift band (no-history run) renders no credibility line,
    keeping such briefings byte-identical to before this feature."""
    note = {
        "strategy_title": "price",
        "achievability": "high",
        "achievability_score": 95.0,
        "rationale": "r",
        "advisory": "",
        "uplift_credibility": "",
        "uplift_credibility_ratio": None,
        "uplift_credibility_reason": "",
    }
    result = lead_arranger_node(_state(_passing_feedbacks(), feasibility_notes=[note]))
    assert "上乗せ妥当性" not in result["meeting_briefing"]


def test_realism_contradiction_rendered_in_briefing() -> None:
    """Depth step 4 pt 3: an optimistic_uplift contradiction is surfaced as a
    整合性 line, and only the briefing text changes (spine byte-identical)."""
    note = {
        "strategy_title": "price",
        "achievability": "high",
        "achievability_score": 95.0,
        "rationale": "r",
        "advisory": "",
        "uplift_credibility": "implausible",
        "uplift_credibility_ratio": 2.3,
        "uplift_credibility_reason": "det reason",
        "realism_flag": "optimistic_uplift",
        "realism_note": "不整合（楽観的）: det realism note",
    }
    without = lead_arranger_node(_state(_passing_feedbacks()))
    with_notes = lead_arranger_node(_state(_passing_feedbacks(), feasibility_notes=[note]))
    without_rest = {k: v for k, v in without.items() if k != "meeting_briefing"}
    with_rest = {k: v for k, v in with_notes.items() if k != "meeting_briefing"}
    assert without_rest == with_rest  # spine untouched
    assert "整合性" in with_notes["meeting_briefing"]
    assert "楽観的" in with_notes["meeting_briefing"]


def test_consistent_realism_omits_line() -> None:
    """A 'consistent' realism verdict adds no 整合性 line (keeps briefing quiet)."""
    note = {
        "strategy_title": "price",
        "achievability": "high",
        "achievability_score": 95.0,
        "rationale": "r",
        "advisory": "",
        "uplift_credibility": "grounded",
        "uplift_credibility_ratio": 0.4,
        "uplift_credibility_reason": "det reason",
        "realism_flag": "consistent",
        "realism_note": "整合あり: ...",
    }
    result = lead_arranger_node(_state(_passing_feedbacks(), feasibility_notes=[note]))
    assert "整合性" not in result["meeting_briefing"]


def test_consistently_weak_realism_rendered_in_briefing() -> None:
    """Design-review refinement: agreement-on-bad (consistently_weak) renders a
    loud 整合性 line -- it must NOT be silently treated like 'consistent'."""
    note = {
        "strategy_title": "price",
        "achievability": "low",
        "achievability_score": 10.0,
        "rationale": "r",
        "advisory": "",
        "uplift_credibility": "implausible",
        "uplift_credibility_ratio": 3.0,
        "uplift_credibility_reason": "det reason",
        "realism_flag": "consistently_weak",
        "realism_note": "要警戒（両面不足）: det weak note",
    }
    without = lead_arranger_node(_state(_passing_feedbacks()))
    with_notes = lead_arranger_node(_state(_passing_feedbacks(), feasibility_notes=[note]))
    without_rest = {k: v for k, v in without.items() if k != "meeting_briefing"}
    with_rest = {k: v for k, v in with_notes.items() if k != "meeting_briefing"}
    assert without_rest == with_rest  # spine untouched
    assert "整合性" in with_notes["meeting_briefing"]
    assert "要警戒" in with_notes["meeting_briefing"]


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
