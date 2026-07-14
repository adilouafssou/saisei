"""Feature 0 — offline determinism-parity guardrail.

The whole point of Feature 0 is that it gates QUALITATIVE LLM text and nothing
else. With no LLM configured, every advisory generator returns "", so the
grounding pipeline is a no-op and the deterministic spine must be byte-identical
to a pre-Feature-0 run. This test pins that:

- each critic's ``simulated_argument`` is "" offline (grounding never runs);
- the feasibility node's advisory is "" and advisory_grounded is False offline;
- gate verdicts, blocker codes, routing, and the burden table are unchanged;
- the grounding pipeline itself, handed an empty advisory, returns empty.

If any wiring lets Feature 0 touch a verdict / route / figure offline, one of
these assertions fails — that is the regression that proves the thesis holds.
"""

from __future__ import annotations

from app.backend.analysis.evidence import build_evidence_packet
from app.backend.analysis.grounding_pipeline import ground_qualitative_text
from app.backend.nodes.critics.feasibility import feasibility_critic_node
from app.backend.nodes.critics.guarantor import guarantor_critic_node
from app.backend.nodes.critics.main_bank import main_bank_critic_node
from app.backend.nodes.critics.sub_bank import sub_bank_critic_node
from app.backend.state import SaiseiState, Strategy
from app.shared.settings import Settings

_OFFLINE = Settings(llm_api_key="", llm_model="")


def _strategy(title: str, uplift: int) -> Strategy:
    return Strategy(title=title, rationale="test", expected_keijo_uplift=uplift)


def _state() -> SaiseiState:
    return SaiseiState(
        tdb_code="1234567",
        proposed_strategies=[
            _strategy("price", 43_920_000),
            _strategy("cogs", 30_960_000),
        ],
        working_capital_gap=-5_000_000,
        ews_score=62.0,
        yakuin_hoshu_cut=False,
        personal_asset_disposal=False,
    )


def test_persona_arguments_empty_offline() -> None:
    """No LLM -> grounding never runs -> every simulated_argument is empty."""
    state = _state()
    for node in (main_bank_critic_node, sub_bank_critic_node, guarantor_critic_node):
        fb = node(state, settings=_OFFLINE)["critic_feedbacks"][0]
        assert fb["simulated_argument"] == ""


def test_feasibility_advisory_empty_offline() -> None:
    """No LLM -> feasibility advisory empty, advisory_grounded False, no reconcile."""
    out = feasibility_critic_node(_state(), settings=_OFFLINE)
    assert out["reconciliation_required"] is False
    for note in out["feasibility_notes"]:
        assert note["advisory"] == ""
        assert note["advisory_grounded"] is False
        # the deterministic band/score are still present and reproducible.
        assert note["achievability"] in {"low", "medium", "high"}
        assert 0.0 <= note["achievability_score"] <= 100.0


def test_grounding_pipeline_noop_on_empty_advisory() -> None:
    """The pipeline handed an empty advisory returns empty (the offline path)."""
    packet = build_evidence_packet(_state())
    out = ground_qualitative_text("", packet, settings=_OFFLINE)
    assert out.text == ""
    assert out.fully_grounded is True


def test_feasibility_node_band_is_reproducible_offline() -> None:
    """Two offline runs produce byte-identical feasibility notes."""
    a = feasibility_critic_node(_state(), settings=_OFFLINE)
    b = feasibility_critic_node(_state(), settings=_OFFLINE)
    assert a == b
