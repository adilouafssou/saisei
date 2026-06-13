"""Saisei LangGraph state schema.

The whole graph shares a single Pydantic V2 ``SaiseiState`` model. Nodes return
partial updates (plain dicts keyed by field name). All monetary fields are
strict integer yen.

This module is the canonical location under ``app.backend.state``.
The legacy path ``shared.graph.state`` re-exports from here.

PART 2 additions (Keieisha Hosho / 経営者保証):
  - ``hosho_kaijo_score``: deterministic 0-100 guarantee-release score.
  - ``hosho_kaijo_conditions``: structured release-conditions model.
  - ``succession_ready``: succession-readiness flag.

PART 3 additions (Multi-critic burden-sharing):
  - ``critic_feedbacks``: parallel-append list of critic feedback dicts.
  - ``negotiation_status``: 'pending' | 'approved' | 'rejected'.
  - ``revision_directive``: ordered blocker list consumed by kaizen_generation.
  - ``revision_count``: cycle guard to prevent infinite revision loops.

Fix (post-merge): ``critic_feedbacks`` reducer now supports a clear sentinel so
  that ``strategist_node`` can reset the list between revision rounds without
  stale verdicts from earlier rounds accumulating in ``lead_arranger``.
  Use ``CRITIC_FEEDBACKS_CLEAR`` as the sentinel value to replace (not append).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.backend.tools.boj_macro import RatePoint, SettlementMetrics
from app.backend.tools.tdb_api import CompanyProfile
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass
from app.shared.models.money import JPY

__all__ = [
    "FsaClass",
    "NegotiationDecision",
    "Strategy",
    "SaiseiState",
    "HoshoKaijoConditions",
    "CriticFeedback",
    "CRITIC_FEEDBACKS_CLEAR",
    "critic_feedbacks_reducer",
    "FeasibilityNote",
    "FEASIBILITY_NOTES_CLEAR",
    "feasibility_notes_reducer",
]

# ---------------------------------------------------------------------------
# Sentinel for clearing critic_feedbacks between revision rounds.
# ---------------------------------------------------------------------------

#: Pass this as the value of ``critic_feedbacks`` in a node's return dict to
#: replace (clear) the accumulated list rather than appending to it.
#: Usage in strategist_node: ``{"critic_feedbacks": CRITIC_FEEDBACKS_CLEAR}``
CRITIC_FEEDBACKS_CLEAR: list[dict] = []  # identity sentinel — checked by `is`


def critic_feedbacks_reducer(
    current: list[dict], update: list[dict]
) -> list[dict]:
    """Custom LangGraph reducer for ``critic_feedbacks``.

    Supports two modes:
    - **Append** (normal fan-out): when ``update`` is a non-empty list, the
      new feedbacks are appended to the existing list.  This is the standard
      behaviour used by the three parallel critic nodes.
    - **Clear/replace** (reset between rounds): when ``update`` is the
      ``CRITIC_FEEDBACKS_CLEAR`` sentinel (the exact same object, checked via
      ``is``), the accumulated list is discarded and replaced with ``[]``.
      ``strategist_node`` uses this to start each revision round fresh.

    Args:
        current: The existing accumulated feedbacks in state.
        update: Either a list of new feedback dicts (append) or the
            ``CRITIC_FEEDBACKS_CLEAR`` sentinel (replace with empty list).

    Returns:
        The merged or reset feedback list.
    """
    if update is CRITIC_FEEDBACKS_CLEAR:
        return []
    return current + update


# ---------------------------------------------------------------------------
# PART 4: Sentinel + reducer for clearing feasibility_notes between rounds.
# ---------------------------------------------------------------------------

#: Pass this as the value of ``feasibility_notes`` in a node's return dict to
#: replace (clear) the accumulated list rather than appending to it.
#: Mirrors CRITIC_FEEDBACKS_CLEAR; used by strategist_node to reset each round.
FEASIBILITY_NOTES_CLEAR: list[dict] = []  # identity sentinel — checked by `is`


def feasibility_notes_reducer(
    current: list[dict], update: list[dict]
) -> list[dict]:
    """Custom LangGraph reducer for ``feasibility_notes``.

    Mirrors :func:`critic_feedbacks_reducer`:
    - **Append** when ``update`` is a list of new note dicts.
    - **Clear/replace** when ``update`` is the ``FEASIBILITY_NOTES_CLEAR``
      sentinel (same object, checked via ``is``), discarding the accumulated
      list. ``strategist_node`` uses this to start each revision round fresh.

    Args:
        current: The existing accumulated notes in state.
        update: Either a list of new note dicts (append) or the
            ``FEASIBILITY_NOTES_CLEAR`` sentinel (replace with empty list).

    Returns:
        The merged or reset note list.
    """
    if update is FEASIBILITY_NOTES_CLEAR:
        return []
    return current + update


class NegotiationDecision(StrEnum):
    """Outcome of the human-in-the-loop strategy negotiation."""

    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"


class Strategy(BaseModel):
    """A proposed turnaround strategy for the Keikakusho."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(description="Short strategy title.")
    rationale: str = Field(description="Why this strategy addresses the deterioration.")
    expected_keijo_uplift: JPY = Field(
        description="Expected annual ordinary-profit uplift / 経常利益改善 (JPY)."
    )


# ---------------------------------------------------------------------------
# PART 2: Keieisha Hosho (経営者保証) models
# ---------------------------------------------------------------------------


class HoshoKaijoConditions(BaseModel):
    """Structured release-conditions directive for the personal guarantee.

    Each condition maps to one of the three FSA guideline pillars.
    All fields are deterministic — no LLM-generated content.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Condition 1: 法人個人分離 (Houjin-Kojin Bunri)
    bunri_met: bool = Field(
        description="Whether corporate/personal asset separation is adequate."
    )
    bunri_score: float = Field(
        description="Separation score component (0-40, deterministic)."
    )
    bunri_directive: str = Field(
        description="What must change to satisfy the separation condition."
    )

    # Condition 2: 財務基盤の強化 (Zaimu Kiban no Kyouka)
    zaimu_met: bool = Field(
        description="Whether the financial base is sufficiently strong."
    )
    zaimu_score: float = Field(
        description="Financial-base score component (0-35, deterministic)."
    )
    zaimu_directive: str = Field(
        description="What must change to satisfy the financial-base condition."
    )

    # Condition 3: 適時適切な情報開示 (Tekiji Tekisetsu na Jouhou Kaiji)
    kaiji_met: bool = Field(
        description="Whether timely and appropriate disclosure is in place."
    )
    kaiji_score: float = Field(
        description="Disclosure score component (0-25, deterministic)."
    )
    kaiji_directive: str = Field(
        description="What must change to satisfy the disclosure condition."
    )

    # Ordered list of what must change (priority order)
    ordered_directives: list[str] = Field(
        description="Ordered list of required changes to achieve guarantee release."
    )


# ---------------------------------------------------------------------------
# PART 3: Multi-critic models
# ---------------------------------------------------------------------------


class CriticFeedback(BaseModel):
    """Structured feedback from a single critic node.

    All PASS/FAIL decisions are deterministic rule-based gates.
    The LLM may only phrase the prose of the final report.

    PART 4 (persona layer): ``simulated_argument`` is an OPTIONAL, advisory-only
    field carrying the LLM-simulated negotiating stance for this persona. It is
    NEVER read by any gate, router, or burden-sharing computation
    (``lead_arranger`` consolidates only ``status`` / ``priority`` / ``persona``
    / ``fatal_blockers``), so the deterministic spine is unchanged. It defaults
    to an empty string so existing constructions and serialization stay
    byte-stable when no LLM is configured (mirrors the ``polish_keikakusho``
    offline-fallback contract).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    persona: str = Field(description="Critic persona identifier.")
    status: str = Field(description="'PASS' or 'FAIL'.")
    fatal_blockers: list[str] = Field(
        default_factory=list,
        description="Ordered list of fatal blockers (empty on PASS).",
    )
    priority: str = Field(
        description="Priority tier: 'P0' (compliance), 'P1' (accountability), 'P2' (fairness)."
    )
    rationale: str = Field(
        description="Brief deterministic rationale for the verdict."
    )
    simulated_argument: str = Field(
        default="",
        description=(
            "PART 4 advisory-only: LLM-simulated negotiating stance for this "
            "persona (creditor-meeting rehearsal). Never feeds any gate, route, "
            "or figure. Empty when no LLM is configured."
        ),
    )


# ---------------------------------------------------------------------------
# PART 4: Feasibility critic (upstream operational pre-screen)
# ---------------------------------------------------------------------------


class FeasibilityNote(BaseModel):
    """Advisory operational-feasibility opinion on a single proposed Strategy.

    Produced by the upstream ``feasibility_critic`` agent. ADVISORY ONLY: this
    annotates strategies for the banker's rehearsal; it NEVER gates PASS/FAIL,
    feeds routing, or alters any figure. The deterministic ``achievability``
    band is a transparent rule-based proxy; ``advisory`` carries optional
    LLM-phrased reasoning and is empty when no LLM is configured (mirrors the
    ``polish_keikakusho`` offline-fallback contract).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_title: str = Field(
        description="Title of the strategy this note assesses."
    )
    achievability: str = Field(
        description=(
            "Deterministic feasibility band: 'high' | 'medium' | 'low'. "
            "Rule-based proxy from the strategy's uplift relative to sales."
        )
    )
    achievability_score: float = Field(
        description="Deterministic feasibility score (0-100; higher = more achievable)."
    )
    rationale: str = Field(
        description="Brief deterministic rationale for the achievability band."
    )
    advisory: str = Field(
        default="",
        description=(
            "Advisory-only: optional LLM-phrased operational feasibility note "
            "for the banker's rehearsal. Never feeds any gate, route, or figure. "
            "Empty when no LLM is configured."
        ),
    )


# ---------------------------------------------------------------------------
# Unified state
# ---------------------------------------------------------------------------


class SaiseiState(BaseModel):
    """Shared state for the Saisei turnaround graph."""

    model_config = ConfigDict(extra="forbid")

    # --- Identity ---
    tdb_code: str = Field(description="7-digit TDB Kigyo code / 企業コード.")
    hojin_bango: str = Field(default="", description="13-digit Corporate Number / 法人番号.")
    company_profile: CompanyProfile | None = Field(default=None)
    tdb_score: int | None = Field(default=None, description="TDB credit score (1-100).")

    # --- Financials ---
    shisanhyo: list[TrialBalance] = Field(default_factory=list)
    working_capital_gap: int | None = Field(
        default=None, description="Shikin Kuri gap / 資金繰り (JPY; negative = deficit)."
    )

    # --- Macro ---
    boj_rate_curve: list[RatePoint] = Field(default_factory=list)
    settlement_metrics: SettlementMetrics | None = Field(default=None)

    # --- Assessment ---
    ews_score: float | None = Field(
        default=None, description="Early Warning Signal score (0-100; higher = worse)."
    )
    fsa_classification: FsaClass | None = Field(default=None)

    # --- PART 2: Keieisha Hosho (経営者保証) assessment ---
    hosho_kaijo_score: float | None = Field(
        default=None,
        description="Deterministic guarantee-release score (0-100; higher = more eligible).",
    )
    hosho_kaijo_conditions: HoshoKaijoConditions | None = Field(
        default=None,
        description="Structured release-conditions directive (three FSA guideline pillars).",
    )
    succession_ready: bool | None = Field(
        default=None,
        description="Whether the business is succession-ready (Jigyou Shoukei / 事業承継).",
    )
    hosho_kaijo_eligible: bool | None = Field(
        default=None,
        description=(
            "Deterministic guarantee-release eligibility verdict: "
            "hosho_kaijo_score >= HOSHO_ELIGIBLE_SCORE. None until assessed."
        ),
    )

    # --- Turnaround ---
    proposed_strategies: list[Strategy] = Field(default_factory=list)
    negotiation_decision: NegotiationDecision | None = Field(default=None)
    approved_strategy: Strategy | None = Field(default=None)
    revision_note: str | None = Field(
        default=None, description="Banker feedback when a revision is requested."
    )
    keikakusho_draft: str | None = Field(default=None)

    # --- PART 3: Multi-critic burden-sharing ---
    # LangGraph reducer: parallel fan-out appends; CRITIC_FEEDBACKS_CLEAR resets.
    critic_feedbacks: Annotated[list[dict], critic_feedbacks_reducer] = Field(
        default_factory=list,
        description=(
            "Accumulated critic feedback dicts (parallel-append via LangGraph reducer). "
            "Reset between revision rounds by returning CRITIC_FEEDBACKS_CLEAR from strategist."
        ),
    )
    negotiation_status: str = Field(
        default="pending",
        description=(
            "Creditor-meeting status: 'pending' | 'approved' | 'rejected' | "
            "'needs_human'. 'needs_human' means the only fatal blockers are "
            "banker-only commitment flags, so the graph routes to HITL instead "
            "of looping the strategist toward escalation."
        ),
    )
    revision_directive: str | None = Field(
        default=None,
        description="Ordered blocker list from lead_arranger, consumed by kaizen_generation.",
    )
    meeting_briefing: str | None = Field(
        default=None,
        description=(
            "PART 4 advisory-only: human-readable creditor-meeting rehearsal "
            "assembled by lead_arranger from the deterministic verdict plus the "
            "per-persona simulated_argument and feasibility_notes. For the "
            "banker's preparation before HITL; never feeds any gate, route, or "
            "figure. Deterministic skeleton when no LLM is configured."
        ),
    )
    revision_count: int = Field(
        default=0,
        description="Number of kaizen revision cycles (cycle guard).",
    )

    # --- PART 4: Feasibility critic (advisory-only operational pre-screen) ---
    # Parallel-append via LangGraph reducer; FEASIBILITY_NOTES_CLEAR resets it
    # between revision rounds (mirrors critic_feedbacks). ADVISORY ONLY: never
    # read by any gate, router, or burden-sharing computation.
    feasibility_notes: Annotated[list[dict], feasibility_notes_reducer] = Field(
        default_factory=list,
        description=(
            "Per-strategy operational-feasibility notes from feasibility_critic "
            "(advisory only). Reset between revision rounds by returning "
            "FEASIBILITY_NOTES_CLEAR from strategist."
        ),
    )

    # --- PART 3 (post-merge fix): Optional per-lender stake data ---
    # When provided, sub_bank and lead_arranger use actual lender stakes for the
    # pro-rata fairness check and burden-sharing table instead of the heuristic
    # proxy derived from strategy uplifts.
    # Keys are lender identifiers (e.g. "main_bank", "sub_bank"); values are
    # integer yen outstanding balances.
    lender_stakes: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Optional per-lender outstanding balance (JPY) for stake-based pro-rata "
            "burden-sharing.  Keys: lender identifiers (e.g. 'main_bank', 'sub_bank'). "
            "When empty, sub_bank and lead_arranger fall back to the heuristic proxy "
            "(largest strategy uplift / total uplift).  See sub_bank.py and "
            "lead_arranger.py for the documented proxy limitation."
        ),
    )

    # --- PART 3 (post-merge fix): Explicit accountability commitment flags ---
    # These flags represent real banker/HITL accountability commitments that the
    # main_bank critic gates on.  They default to False (no commitment) so that
    # existing tests and flows are unaffected until a banker explicitly sets them.
    # They are surfaced in the HITL interrupt payload so the banker can confirm.
    yakuin_hoshu_cut: bool = Field(
        default=False,
        description=(
            "Explicit commitment that executive compensation (役員報酬) has been / "
            "will be cut as part of the turnaround plan.  Set by the banker/HITL. "
            "main_bank critic FAILs when this is False."
        ),
    )
    personal_asset_disposal: bool = Field(
        default=False,
        description=(
            "Explicit commitment that the owner will dispose of personal assets "
            "to cover the working-capital deficit (個人資産処分コミットメント). "
            "Set by the banker/HITL.  main_bank critic FAILs when this is False "
            "and a working-capital deficit exists."
        ),
    )

    # --- Control ---
    errors: list[str] = Field(default_factory=list)
