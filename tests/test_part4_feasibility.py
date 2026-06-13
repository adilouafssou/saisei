"""Part 4 step 2 (feasibility_critic) — determinism-parity tests.

The feasibility critic is an ADVISORY-ONLY upstream pre-screen. These tests are
the verifier proving the deterministic spine is untouched and the node is
offline-safe:

- achievability band + score are deterministic and reproducible;
- with NO LLM configured, the advisory text is empty (offline fallback);
- the node emits ONLY feasibility_notes and never a gate field;
- the three deterministic critics + lead_arranger produce byte-identical
  verdicts/routing/burden whether or not feasibility_notes are present, because
  nothing downstream reads them.

If a later step lets feasibility influence a verdict/route/figure, one of these
assertions must fail — that is the guardrail.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.critics.feasibility import (
    assess_feasibility,
    feasibility_critic_node,
)
from app.backend.nodes.critics.guarantor import guarantor_critic_node
from app.backend.nodes.critics.main_bank import main_bank_critic_node
from app.backend.nodes.critics.sub_bank import sub_bank_critic_node
from app.backend.nodes.lead_arranger import lead_arranger_node
from app.backend.state import SaiseiState, Strategy
from app.shared.models.accounting import TrialBalance
from app.shared.settings import Settings


def _strategy(title: str, uplift: int) -> Strategy:
    return Strategy(title=title, rationale="test", expected_keijo_uplift=uplift)


def _full_strategies() -> list[Strategy]:
    return [
        _strategy("price", 43_920_000),
        _strategy("cogs", 30_960_000),
        _strategy("sga", 14_100_000),
        _strategy("wc", 5_000_000),
    ]


def _tb(uriage: int) -> TrialBalance:
    """Minimal trial balance with the sales figure the proxy reads.

    Profit lines (uriage_sourieki, keijo_rieki, ...) are computed fields on the
    model, so only the raw accounts are supplied.
    """
    return TrialBalance(
        period=dt.date(2024, 1, 31),
        uriage=uriage,
        uriage_genka=uriage // 2,
        hanbaihi=uriage // 5,
        eigai_shueki=0,
        eigai_hiyo=0,
    )


#: No-LLM settings: empty api_key/model -> offline fallback.
_OFFLINE = Settings(llm_api_key="", llm_model="")


def test_achievability_band_is_deterministic() -> None:
    """Same strategy + sales always yields the same band and score."""
    s = _strategy("price", 5_000_000)
    a = assess_feasibility(s, monthly_sales=100_000_000)
    b = assess_feasibility(s, monthly_sales=100_000_000)
    assert a == b
    # 5,000,000 / (100,000,000 * 12) = 0.42% -> well under 5% -> high.
    assert a.achievability == "high"
    assert a.advisory == ""


def test_achievability_bands_cover_low_medium_high() -> None:
    """The proxy separates a small, mid, and oversized uplift into 3 bands."""
    monthly = 10_000_000  # annual sales = 120,000,000
    high = assess_feasibility(_strategy("a", 1_000_000), monthly)      # ~0.8%
    medium = assess_feasibility(_strategy("b", 12_000_000), monthly)   # 10%
    low = assess_feasibility(_strategy("c", 60_000_000), monthly)      # 50%
    assert high.achievability == "high"
    assert medium.achievability == "medium"
    assert low.achievability == "low"
    # Score decreases as the ask grows.
    assert high.achievability_score > medium.achievability_score > low.achievability_score


def test_node_offline_emits_only_feasibility_notes_with_empty_advisory() -> None:
    """With no LLM, the node returns only feasibility_notes; advisory is empty."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        shisanhyo=[_tb(100_000_000)],
    )
    out = feasibility_critic_node(state, settings=_OFFLINE)
    assert set(out.keys()) == {"feasibility_notes"}
    notes = out["feasibility_notes"]
    assert len(notes) == len(_full_strategies())
    for note in notes:
        assert note["advisory"] == ""
        assert note["achievability"] in {"high", "medium", "low"}
        assert set(note.keys()) == {
            "strategy_title",
            "achievability",
            "achievability_score",
            "rationale",
            "advisory",
        }


def test_node_is_reproducible_offline() -> None:
    """Two runs with the same state produce byte-identical notes (offline)."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        shisanhyo=[_tb(100_000_000)],
    )
    a = feasibility_critic_node(state, settings=_OFFLINE)
    b = feasibility_critic_node(state, settings=_OFFLINE)
    assert a == b


def test_node_no_strategies_returns_empty() -> None:
    state = SaiseiState(tdb_code="1234567")
    out = feasibility_critic_node(state, settings=_OFFLINE)
    assert out == {"feasibility_notes": []}


def _run_meeting(state: SaiseiState) -> dict:
    """Run the three deterministic critics + lead_arranger over a state."""
    feedbacks: list[dict] = []
    feedbacks += main_bank_critic_node(state)["critic_feedbacks"]
    feedbacks += sub_bank_critic_node(state)["critic_feedbacks"]
    feedbacks += guarantor_critic_node(state)["critic_feedbacks"]
    consolidated = state.model_copy(update={"critic_feedbacks": feedbacks})
    return lead_arranger_node(consolidated)


def test_meeting_is_byte_identical_with_and_without_feasibility_notes() -> None:
    """feasibility_notes never change the gate verdicts, routing, or burden.

    Run the deterministic critics + lead_arranger twice: once on a bare state,
    once on a state already carrying feasibility_notes. The consolidated output
    must be identical, proving nothing downstream reads the advisory channel.
    """
    base = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        shisanhyo=[_tb(100_000_000)],
        working_capital_gap=-5_000_000,
        revision_count=0,
    )
    notes = feasibility_critic_node(base, settings=_OFFLINE)["feasibility_notes"]
    with_notes = base.model_copy(update={"feasibility_notes": notes})

    result_without = _run_meeting(base)
    result_with = _run_meeting(with_notes)

    assert result_without == result_with, (
        "meeting consolidation must not depend on feasibility_notes"
    )
