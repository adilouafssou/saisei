"""Part 4 (multi-agent meeting simulator) — determinism-parity tests.

Step 1 adds an OPTIONAL advisory ``CriticFeedback.simulated_argument`` field.
These tests are the verifier proving the deterministic spine is untouched:

- the field defaults to "" (offline / no-LLM fallback);
- it is the ONLY change to a critic's serialized output;
- ``lead_arranger`` consolidation (status, directive, revision_count) is
  byte-identical whether or not a persona argument is present, because it never
  reads ``simulated_argument``.

If a later step lets the field influence a verdict/route/figure, one of these
assertions must fail — that is the guardrail.
"""

from __future__ import annotations

from app.backend.nodes.critics.main_bank import main_bank_critic_node
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


def test_simulated_argument_defaults_to_empty() -> None:
    """The new field is optional and defaults to an empty string."""
    fb = CriticFeedback(persona="main_bank", status="PASS", priority="P1", rationale="ok")
    assert fb.simulated_argument == ""
    assert fb.model_dump()["simulated_argument"] == ""


def test_critic_dump_adds_only_the_new_key() -> None:
    """A critic's serialized output gains exactly one new key: simulated_argument.

    Locks the serialization shape so a future change can't silently alter the
    other (deterministic) keys.
    """
    state = SaiseiState(
        tdb_code="1234567",
        working_capital_gap=-5_000_000,
        proposed_strategies=_full_strategies(),
        yakuin_hoshu_cut=True,
        personal_asset_disposal=True,
    )
    fb = main_bank_critic_node(state)["critic_feedbacks"][0]
    assert set(fb.keys()) == {
        "persona",
        "status",
        "fatal_blockers",
        "priority",
        "rationale",
        "simulated_argument",
    }
    # The deterministic gate output itself is unchanged.
    assert fb["status"] == "PASS"
    assert fb["simulated_argument"] == ""


def test_lead_arranger_ignores_simulated_argument() -> None:
    """lead_arranger output is byte-identical with or without persona arguments.

    Same gate verdicts + blockers, but one set of feedbacks carries populated
    simulated_argument strings. The consolidated status, revision_directive and
    revision_count must be identical, proving the field never feeds routing or
    the burden table.
    """
    base_blocker = ["yakuin_hoshu_not_cut: confirm exec-comp cut"]

    plain = [
        _feedback("guarantor", "PASS", "P0"),
        _feedback("main_bank", "FAIL", "P1", base_blocker),
        _feedback("sub_bank", "PASS", "P2"),
    ]
    with_args = [
        _feedback("guarantor", "PASS", "P0", simulated_argument="保証協会としては…"),
        _feedback(
            "main_bank",
            "FAIL",
            "P1",
            base_blocker,
            simulated_argument="主幹事として役員報酬の削減を強く求める。",
        ),
        _feedback("sub_bank", "PASS", "P2", simulated_argument="協調行として異議なし。"),
    ]

    def _run(feedbacks: list[dict]) -> dict:
        state = SaiseiState(
            tdb_code="1234567",
            critic_feedbacks=feedbacks,
            proposed_strategies=_full_strategies(),
            working_capital_gap=-5_000_000,
            revision_count=0,
        )
        return lead_arranger_node(state)

    result_plain = _run(plain)
    result_with_args = _run(with_args)

    assert result_plain == result_with_args, (
        "lead_arranger consolidation must not depend on simulated_argument"
    )
    # Sanity: this is the banker-only blocker path.
    assert result_plain["negotiation_status"] == "needs_human"
    assert result_plain["revision_count"] == 0
