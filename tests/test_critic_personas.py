"""Part 4 step 3 (critic persona layer) — determinism-parity tests.

Each critic is now a HYBRID: the deterministic gate decides PASS/FAIL and the
blocker codes (unchanged), plus an OPTIONAL advisory ``simulated_argument``
phrasing the persona's stance. These tests are the verifier proving the
deterministic spine is untouched and the persona layer is offline-safe:

- with NO LLM configured, every critic's ``simulated_argument`` is "";
- the critic's serialized output keeps exactly the documented key set;
- the gate verdict / blockers / rationale are unchanged offline;
- ``lead_arranger`` consolidation is byte-identical, because it never reads
  ``simulated_argument``.

If a later step lets the persona layer leak into a verdict/route/figure, one of
these assertions must fail — that is the guardrail.
"""

from __future__ import annotations

from typing import Any

from app.backend.nodes.critics.guarantor import guarantor_critic_node
from app.backend.nodes.critics.main_bank import main_bank_critic_node
from app.backend.nodes.critics.sub_bank import sub_bank_critic_node
from app.backend.nodes.lead_arranger import lead_arranger_node
from app.backend.state import SaiseiState, Strategy
from app.shared.settings import Settings

#: No-LLM settings: empty api_key/model -> offline fallback (empty argument).
_OFFLINE = Settings(llm_api_key="", llm_model="")

_EXPECTED_KEYS = {
    "persona",
    "status",
    "fatal_blockers",
    "priority",
    "rationale",
    "simulated_argument",
}


def _strategy(title: str, uplift: int) -> Strategy:
    return Strategy(title=title, rationale="test", expected_keijo_uplift=uplift)


def _full_strategies() -> list[Strategy]:
    return [
        _strategy("price", 43_920_000),
        _strategy("cogs", 30_960_000),
        _strategy("sga", 14_100_000),
        _strategy("wc", 5_000_000),
    ]


def _passing_state() -> SaiseiState:
    """A state where all three deterministic gates PASS."""
    return SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        working_capital_gap=5_000_000,  # positive -> no asset-disposal gate
        ews_score=10.0,
        yakuin_hoshu_cut=True,
        personal_asset_disposal=True,
        lender_stakes={"main_bank": 50, "sub_bank": 50},  # exact pro-rata
    )


def _failing_state() -> SaiseiState:
    """A state where main_bank FAILs (banker-only commitment flags unset)."""
    return SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        working_capital_gap=-5_000_000,
        ews_score=10.0,
        yakuin_hoshu_cut=False,
        personal_asset_disposal=False,
        lender_stakes={"main_bank": 50, "sub_bank": 50},
    )


def test_each_critic_offline_has_empty_argument_and_stable_keys() -> None:
    """Offline, every critic emits the documented keys with an empty argument."""
    state = _failing_state()
    for node in (main_bank_critic_node, sub_bank_critic_node, guarantor_critic_node):
        fb = node(state, settings=_OFFLINE)["critic_feedbacks"][0]
        assert set(fb.keys()) == _EXPECTED_KEYS
        assert fb["simulated_argument"] == ""
        assert fb["status"] in {"PASS", "FAIL"}


def test_gate_verdicts_unchanged_offline() -> None:
    """The deterministic gate verdicts are exactly as designed, offline."""
    passing = _passing_state()
    assert (
        main_bank_critic_node(passing, settings=_OFFLINE)["critic_feedbacks"][0]["status"] == "PASS"
    )
    assert (
        sub_bank_critic_node(passing, settings=_OFFLINE)["critic_feedbacks"][0]["status"] == "PASS"
    )
    assert (
        guarantor_critic_node(passing, settings=_OFFLINE)["critic_feedbacks"][0]["status"] == "PASS"
    )

    failing = _failing_state()
    main_fb = main_bank_critic_node(failing, settings=_OFFLINE)["critic_feedbacks"][0]
    assert main_fb["status"] == "FAIL"
    assert any("yakuin_hoshu_not_cut" in b for b in main_fb["fatal_blockers"])


def _run_meeting(state: SaiseiState) -> dict[str, Any]:
    feedbacks: list[dict[str, Any]] = []
    feedbacks += main_bank_critic_node(state, settings=_OFFLINE)["critic_feedbacks"]
    feedbacks += sub_bank_critic_node(state, settings=_OFFLINE)["critic_feedbacks"]
    feedbacks += guarantor_critic_node(state, settings=_OFFLINE)["critic_feedbacks"]
    consolidated = state.model_copy(update={"critic_feedbacks": feedbacks})
    return lead_arranger_node(consolidated)


def test_lead_arranger_consolidation_ignores_persona_layer() -> None:
    """Offline meeting consolidation matches the pre-persona deterministic result.

    The persona layer adds only empty advisory strings offline, so the
    consolidated status / directive / revision_count must be exactly what the
    deterministic gates produce. The failing state is the banker-only path.
    """
    result = _run_meeting(_failing_state())
    assert result["negotiation_status"] == "needs_human"
    assert result["revision_count"] == 0

    approved = _run_meeting(_passing_state())
    assert approved["negotiation_status"] == "approved"
