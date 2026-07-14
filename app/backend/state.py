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
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.backend.tools.boj_macro import RatePoint, SettlementMetrics
from app.backend.tools.tdb_api import CompanyProfile
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass
from app.shared.models.loan import loan_events_reducer
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
    "ReconciliationDetail",
    "ReconciliationOutcome",
    "BANKER_VERDICTS",
    "reconciliation_outcomes_reducer",
    "loan_events_reducer",
]

# ---------------------------------------------------------------------------
# Sentinel for clearing critic_feedbacks between revision rounds.
# ---------------------------------------------------------------------------

#: Pass this as the value of ``critic_feedbacks`` in a node's return dict to
#: replace (clear) the accumulated list rather than appending to it.
#: Usage in strategist_node: ``{"critic_feedbacks": CRITIC_FEEDBACKS_CLEAR}``
CRITIC_FEEDBACKS_CLEAR: list[dict[str, Any]] = []  # identity sentinel — checked by `is`


def critic_feedbacks_reducer(
    current: list[dict[str, Any]], update: list[dict[str, Any]]
) -> list[dict[str, Any]]:
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
# MR2 (outcome capture): append-only reducer for reconciliation_outcomes.
#
# Unlike critic_feedbacks / feasibility_notes, the who-was-right corpus is
# PERMANENT learning data: it is NEVER reset between revision rounds. There is
# therefore no clear sentinel — the reducer is a pure append. Offline / no
# reconciliation means the update is an empty list and the append is a no-op.
# ---------------------------------------------------------------------------


def reconciliation_outcomes_reducer(
    current: list[dict[str, Any]], update: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append-only LangGraph reducer for ``reconciliation_outcomes``.

    The who-was-right corpus is permanent learning data captured every time a
    banker resolves a reconciliation interrupt. It is intentionally
    append-only: there is NO clear sentinel (contrast
    :func:`critic_feedbacks_reducer`), because outcomes from earlier rounds must
    survive across revision cycles to be fittable to the calibration thresholds.

    Args:
        current: The existing accumulated outcomes in state.
        update: A (possibly empty) list of new outcome dicts to append. Empty
            when no reconciliation occurred (offline-safe no-op).

    Returns:
        The concatenated outcome list.
    """
    return current + update


# ---------------------------------------------------------------------------
# PART 4: Sentinel + reducer for clearing feasibility_notes between rounds.
# ---------------------------------------------------------------------------

#: Pass this as the value of ``feasibility_notes`` in a node's return dict to
#: replace (clear) the accumulated list rather than appending to it.
#: Mirrors CRITIC_FEEDBACKS_CLEAR; used by strategist_node to reset each round.
FEASIBILITY_NOTES_CLEAR: list[dict[str, Any]] = []  # identity sentinel — checked by `is`


def feasibility_notes_reducer(
    current: list[dict[str, Any]], update: list[dict[str, Any]]
) -> list[dict[str, Any]]:
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

    def __getitem__(self, key: str) -> Any:
        """Support dict-style read access (e.g. ``strategy["title"]``).

        ``proposed_strategies`` may be consumed either as Pydantic objects
        (``strategy.title``) or, in the golden-eval harness and burden-sharing
        callers, via subscript (``strategy["title"]``).  This thin accessor keeps
        the model frozen and validated while supporting both styles uniformly,
        whether the strategy is the live object or a checkpoint round-trip.

        Args:
            key: A model field name.

        Returns:
            The value of the named field.

        Raises:
            KeyError: If ``key`` is not a field of the model.
        """
        if key not in type(self).model_fields:
            raise KeyError(key)
        return getattr(self, key)


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
    bunri_met: bool = Field(description="Whether corporate/personal asset separation is adequate.")
    bunri_score: float = Field(description="Separation score component (0-40, deterministic).")
    bunri_directive: str = Field(
        description="What must change to satisfy the separation condition."
    )

    # Condition 2: 財務基盤の強化 (Zaimu Kiban no Kyouka)
    zaimu_met: bool = Field(description="Whether the financial base is sufficiently strong.")
    zaimu_score: float = Field(description="Financial-base score component (0-35, deterministic).")
    zaimu_directive: str = Field(
        description="What must change to satisfy the financial-base condition."
    )

    # Condition 3: 適時適切な情報開示 (Tekiji Tekisetsu na Jouhou Kaiji)
    kaiji_met: bool = Field(description="Whether timely and appropriate disclosure is in place.")
    kaiji_score: float = Field(description="Disclosure score component (0-25, deterministic).")
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
    rationale: str = Field(description="Brief deterministic rationale for the verdict.")
    simulated_argument: str = Field(
        default="",
        description=(
            "PART 4 advisory-only: LLM-simulated negotiating stance for this "
            "persona (creditor-meeting rehearsal). Never feeds any gate, route, "
            "or figure. Empty when no LLM is configured."
        ),
    )


# ---------------------------------------------------------------------------
# MR #2: Reconciliation detail — one entry per strategy with a disagreement.
# ---------------------------------------------------------------------------


class ReconciliationDetail(BaseModel):
    """Structured record of a single LLM-vs-floor feasibility disagreement.

    Produced by the deterministic reconciliation predicate in
    ``feasibility_critic_node`` when the LLM-derived feasibility band and the
    deterministic floor band are separated by >= RECONCILIATION_BAND_DISTANCE.

    ADVISORY ONLY: this record is surfaced in the HITL interrupt payload so the
    banker can see the disagreement with full context. It NEVER feeds any gate,
    route direction, or figure — the only routing effect is to send the graph to
    hitl_negotiation (which a human then resolves).

    MR #3 addition — ``routed`` field:
        All qualifying disagreements are recorded here for full audit
        transparency. The top-N by band_distance (up to MAX_RECONCILIATION_TRIGGERS)
        are marked ``routed=True`` to indicate they drove the routing decision.
        Entries with ``routed=False`` are present for audit only and did NOT
        contribute to the routing trigger (ceiling enforcement). Ties in
        band_distance are broken by strategy_title ascending for byte-stable
        deterministic output.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_title: str = Field(description="Title of the strategy with the disagreement.")
    deterministic_band: str = Field(
        description="Band from the deterministic floor formula ('high'|'medium'|'low')."
    )
    deterministic_score: float = Field(
        description="Score from the deterministic floor formula (0-100)."
    )
    llm_band: str = Field(
        description="Band derived from the LLM feasibility signal ('high'|'medium'|'low')."
    )
    llm_score: float = Field(description="Score derived from the LLM feasibility signal (0-100).")
    band_distance: int = Field(
        description="Ordinal distance between deterministic_band and llm_band (0-2)."
    )
    routed: bool = Field(
        default=False,
        description=(
            "MR #3: True when this disagreement is one of the top-N by band_distance "
            "that drove the routing decision (N = MAX_RECONCILIATION_TRIGGERS). "
            "False for audit-only entries that were recorded but did not trigger "
            "routing (ceiling enforcement). Ties broken by strategy_title ascending."
        ),
    )


# ---------------------------------------------------------------------------
# MR2 (outcome capture): who-was-right corpus entry.
# ---------------------------------------------------------------------------

#: Valid values for ``ReconciliationOutcome.banker_verdict``.
#:   'floor'   — the deterministic floor band was right.
#:   'llm'     — the LLM band was right.
#:   'neither' — neither was right (banker had a third view).
#:   ''        — not yet adjudicated (the banker did not pick a side).
BANKER_VERDICTS: frozenset[str] = frozenset({"floor", "llm", "neither", ""})


class ReconciliationOutcome(BaseModel):
    """Human-verified who-was-right record for one routed reconciliation.

    Captured every time a banker resolves a reconciliation interrupt, one entry
    per ROUTED disagreement (``ReconciliationDetail.routed == True`` — the ones
    that actually drove the trigger; audit-only details are skipped). This is
    the permanent, append-only learning corpus that turns the two CALIBRATION
    PLACEHOLDER magic numbers (RECONCILIATION_BAND_DISTANCE,
    MAX_RECONCILIATION_TRIGGERS) into something fittable to real outcomes, and
    the same human-verified corpus later RAG / knowledge-graph grounding
    depends on.

    LEARNING DATA, NEVER A DECISION-MAKER: this record is captured AFTER the
    banker decides. No router, gate, or figure reads it. It adds no autonomous
    LLM to the decision path.

    The ``banker_verdict`` field is constrained to :data:`BANKER_VERDICTS` by a
    field validator (case-insensitive, whitespace-trimmed) so the model is
    self-validating even when constructed directly rather than through the
    orchestrator builder — consistent with its ``frozen=True`` /
    ``extra="forbid"`` configuration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_title: str = Field(description="Title of the strategy that was routed.")
    deterministic_band: str = Field(
        description="Deterministic floor band at trigger time ('high'|'medium'|'low')."
    )
    llm_band: str = Field(
        description="LLM feasibility band at trigger time ('high'|'medium'|'low')."
    )
    band_distance: int = Field(
        description="Ordinal distance between the two bands at trigger time (0-2)."
    )
    banker_decision: str = Field(
        description="The banker's HITL decision ('approve'|'revise'|'reject')."
    )
    banker_verdict: str = Field(
        default="",
        description=(
            "Who the banker judged correct: 'floor' | 'llm' | 'neither' | '' "
            "(not adjudicated). The fittable label of the corpus."
        ),
    )

    @field_validator("banker_verdict", mode="before")
    @classmethod
    def _normalise_verdict(cls, value: Any) -> str:
        """Normalise and validate ``banker_verdict`` against :data:`BANKER_VERDICTS`.

        Coerces ``None`` to the empty (not-adjudicated) string, trims
        surrounding whitespace, and lower-cases the value so the corpus stores a
        single canonical form. Any value outside the documented domain raises a
        ``ValueError`` so the model can never silently accept an invalid verdict.

        Args:
            value: The raw ``banker_verdict`` input.

        Returns:
            The canonical verdict string (one of :data:`BANKER_VERDICTS`).

        Raises:
            ValueError: If ``value`` is not one of the documented verdicts.
        """
        if value is None:
            return ""
        normalised = str(value).strip().lower()
        if normalised not in BANKER_VERDICTS:
            allowed = ", ".join(sorted(repr(v) for v in BANKER_VERDICTS))
            raise ValueError(f"banker_verdict must be one of {{{allowed}}}, got {value!r}")
        return normalised


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

    strategy_title: str = Field(description="Title of the strategy this note assesses.")
    achievability: str = Field(
        description=(
            "Deterministic feasibility band: 'high' | 'medium' | 'low'. "
            "Rule-based proxy from the strategy's uplift relative to sales."
        )
    )
    achievability_score: float = Field(
        description="Deterministic feasibility score (0-100; higher = more achievable)."
    )
    rationale: str = Field(description="Brief deterministic rationale for the achievability band.")
    advisory: str = Field(
        default="",
        description=(
            "Advisory-only: optional LLM-phrased operational feasibility note "
            "for the banker's rehearsal. Never feeds any gate, route, or figure. "
            "Empty when no LLM is configured."
        ),
    )
    advisory_grounded: bool = Field(
        default=False,
        description=(
            "MR #2: True when the advisory text is supported by >= 1 retrieved "
            "precedent snippet (token/source overlap heuristic). False when no "
            "LLM is configured or no snippets were retrieved. Metadata only — "
            "never feeds any gate, route, or figure."
        ),
    )
    advisory_provenance: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Feature 0 phase 4 (provenance in the UI): per-sentence provenance "
            "for the advisory text, as produced by the claim-grounding pipeline. "
            "Each entry is {'text': str, 'status': 'grounded'|'unverified'"
            "|'non_claim', 'citations': list[str]} so the UI can show the banker "
            "which claims are attributable and to what. Empty when no LLM is "
            "configured (offline no-op). Display metadata only — never feeds any "
            "gate, route, or figure."
        ),
    )
    uplift_credibility: str = Field(
        default="",
        description=(
            "Depth step 4: deterministic credibility band for this strategy's "
            "claimed annual uplift against the firm's OWN self-derived headroom "
            "ceiling: 'grounded' | 'stretch' | 'implausible'. Empty when there is "
            "insufficient history to assess. ADVISORY ONLY — surfaced to the "
            "banker so an over-claimed uplift is visible BEFORE the recovery "
            "curve is trusted; never feeds any gate, route, or figure."
        ),
    )
    uplift_credibility_ratio: float | None = Field(
        default=None,
        description=(
            "Depth step 4: the over-claim multiple (claimed uplift / self-derived "
            "headroom ceiling). None when the ceiling is 0 (no plausible headroom) "
            "or history is insufficient. Display metadata only."
        ),
    )
    uplift_credibility_reason: str = Field(
        default="",
        description=(
            "Depth step 4: deterministic bilingual explanation naming which "
            "self-derived headroom supports the claim and by what multiple it is "
            "exceeded (mirrors classification_reason style). Empty when not "
            "assessed. Display/audit prose only — decides nothing."
        ),
    )
    realism_flag: str = Field(
        default="",
        description=(
            "Depth step 4 part 3: deterministic cross-signal consistency verdict "
            "between the execution-risk band (achievability) and the magnitude "
            "band (uplift_credibility): 'consistent' | 'optimistic_uplift' "
            "(easy to execute but the claimed payoff exceeds the firm's own "
            "headroom) | 'pessimistic_uplift' (a believable payoff but hard to "
            "execute). Empty when either band is unassessed. ADVISORY ONLY — a "
            "pure function of two already-computed deterministic bands; never "
            "feeds a gate, route, or figure."
        ),
    )
    realism_note: str = Field(
        default="",
        description=(
            "Depth step 4 part 3: deterministic bilingual explanation of the "
            "realism_flag (why the two signals agree or contradict). Empty when "
            "not assessed. Display/audit prose only — decides nothing."
        ),
    )


# ---------------------------------------------------------------------------
# Unified state
# ---------------------------------------------------------------------------


class SaiseiState(BaseModel):
    """Shared state for the Saisei turnaround graph."""

    model_config = ConfigDict(extra="forbid")

    # --- Identity ---
    # Defaults to "" so the servicing graph (whose input contract is just
    # {loan_id, servicing_action, ...}) can be coerced into SaiseiState without a
    # TDB code: servicing reasons only over the facility's durable loan log and
    # never performs a TDB lookup. The origination / turnaround surfaces always
    # supply a real code (their HTTP layers reject a malformed one with 422
    # before the graph runs), so those paths are unchanged. Mirrors the sibling
    # ``hojin_bango`` identity field, which is likewise optional.
    tdb_code: str = Field(default="", description="7-digit TDB Kigyo code / 企業コード.")
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
    ews_breakdown: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Feature 7 explainability: per-signal EWS contributions, each "
            "{key, label_ja, raw, points, weight}. The points sum to ews_score "
            "by construction. Display/audit metadata only — never feeds a gate, "
            "route, or figure. Empty until EWS is scored."
        ),
    )
    fsa_classification: FsaClass | None = Field(default=None)
    classification_reason: str = Field(
        default="",
        description=(
            "Feature 7 explainability: the deterministic threshold reason for "
            "fsa_classification (which signal crossed which threshold), mirroring "
            "the classify() cascade. Display/audit metadata only — never decides "
            "the class or feeds a gate, route, or figure. Empty until classified."
        ),
    )

    # --- Insolvency signals (additive; used by classifier for severe bands) ---
    # ``net_worth`` is the borrower's net worth in JPY (integer yen; may be
    # negative for technically insolvent firms). A negative value is a hard
    # insolvency signal that places the borrower in 実質破綻先 or 破綻先.
    net_worth: int | None = Field(
        default=None,
        description=(
            "Borrower net worth / 純資産 (JPY int; may be negative). "
            "Negative net worth is a hard insolvency signal used by the "
            "classifier to reach 実質破綻先 or 破綻先. None = not yet assessed."
        ),
    )
    # ``is_insolvent`` is an explicit insolvency flag that can be set by the
    # intake node or a banker override when the borrower is known to be
    # insolvent (e.g. court filing, FSA examination finding). It overrides EWS
    # and routes directly to the workout node.
    is_insolvent: bool | None = Field(
        default=None,
        description=(
            "Explicit insolvency flag (True = insolvent; None = not yet assessed). "
            "When True, the classifier places the borrower in 実質破綻先 or 破綻先 "
            "regardless of EWS score. Set by intake or banker override."
        ),
    )
    # ``special_attention`` is the 要管理先 sub-tier flag. A borrower classified
    # as 要注意先 with special_attention=True is a 要管理先 borrower (Special
    # Attention). This is a sub-category of 要注意先 per the FSA Manual; it is
    # NOT a separate top-level FsaClass member. The classifier sets this flag
    # deterministically (e.g. 要注意先 with a working-capital deficit).
    special_attention: bool | None = Field(
        default=None,
        description=(
            "要管理先 sub-tier flag (Special Attention). "
            "True when fsa_classification is 要注意先 AND the borrower has a "
            "working-capital deficit (要管理債権 indicator). "
            "This is a sub-category of 要注意先 per the FSA Financial Inspection "
            "Manual — NOT a separate top-level classification. "
            "None = not yet assessed."
        ),
    )

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
    critic_feedbacks: Annotated[list[dict[str, Any]], critic_feedbacks_reducer] = Field(
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
    feasibility_notes: Annotated[list[dict[str, Any]], feasibility_notes_reducer] = Field(
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

    # --- Workout handoff (additive; set by workout_node for bankrupt borrowers) ---
    # Populated only for 実質破綻先 / 破綻先 borrowers routed to the workout node.
    # Contains a structured audit record for the bank's workout / special-assets team.
    workout_handoff: str | None = Field(
        default=None,
        description=(
            "Legal/liquidation handoff record set by workout_node for "
            "実質破綻先 and 破綻先 borrowers. Deterministic audit trail for "
            "the workout team; never generated by an LLM."
        ),
    )

    # --- Loan write-off (償却) closure record (additive; set by writeoff_node) ---
    # Set when the bank charges off a facility in workout: the deterministic
    # charged-off amount (償却額 = outstanding principal at the bankrupt-class
    # 100% loss) plus whether the HITL-gated WORKOUT -> WRITTEN_OFF transition was
    # recorded. The amount is a deterministic figure (provision_amount), surfaced
    # for the banker / ledger; it never feeds a gate, route, or figure. None until
    # the write-off node runs.
    loan_writeoff: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Loan write-off (償却) closure record from writeoff_node: the "
            "deterministic charged-off amount (outstanding principal at the "
            "bankrupt-class loss) and whether the HITL-gated WORKOUT -> "
            "WRITTEN_OFF transition was recorded. Surfaced for the banker / "
            "ledger; never feeds a gate, route, or figure. None until charged off."
        ),
    )

    # --- MR #2: LLM-vs-floor reconciliation (deterministic predicate, HITL route) ---
    # reconciliation_required is set by a PURE DETERMINISTIC PREDICATE in
    # feasibility_critic_node: it is True iff the LLM-derived feasibility band
    # and the deterministic floor band are separated by >= RECONCILIATION_BAND_DISTANCE
    # for at least one strategy. When True, the graph routes to hitl_negotiation
    # BEFORE the critic fan-out so a human can resolve the disagreement.
    # The LLM can ONLY raise the question; it NEVER decides direction/verdict/figure.
    # When no LLM is configured, reconciliation_required stays False (no-op offline).
    reconciliation_required: bool = Field(
        default=False,
        description=(
            "MR #2: True when the deterministic feasibility floor and the LLM "
            "feasibility signal disagree by >= RECONCILIATION_BAND_DISTANCE for "
            "at least one strategy. Routes to hitl_negotiation before the critic "
            "fan-out. Pure deterministic predicate — LLM never decides direction. "
            "False (default) when no LLM is configured (offline-safe)."
        ),
    )
    reconciliation_details: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "MR #2: Per-strategy disagreement records (ReconciliationDetail dicts) "
            "surfaced in the HITL interrupt payload. Empty when reconciliation_required "
            "is False. Advisory only — never feeds any gate, route, or figure."
        ),
    )

    # --- MR2 (outcome capture): who-was-right corpus (append-only) ---
    # ReconciliationOutcome dicts captured by the HITL orchestrator AFTER the
    # banker decides, one per ROUTED disagreement. PERMANENT learning data:
    # never reset between rounds (append-only reducer, no clear sentinel). This
    # is the corpus the calibration of RECONCILIATION_BAND_DISTANCE and
    # MAX_RECONCILIATION_TRIGGERS is fitted to. LEARNING DATA ONLY — never read
    # by any gate, router, or figure. Empty when no reconciliation occurred.
    reconciliation_outcomes: Annotated[list[dict[str, Any]], reconciliation_outcomes_reducer] = (
        Field(
            default_factory=list,
            description=(
                "MR2: Append-only who-was-right corpus (ReconciliationOutcome dicts) "
                "captured at each HITL resolution, one per routed disagreement. "
                "Permanent learning data — never reset between rounds, never read by "
                "any gate, route, or figure. The corpus the reconciliation thresholds "
                "are fitted to. Empty when no reconciliation occurred (offline-safe)."
            ),
        )
    )

    # --- Part 6: Excel/CSV upload staging (additive; never remove/rename) ---
    # ``uploaded_shisanhyo`` holds PROPOSED rows parsed from a banker-uploaded
    # .xlsx/.csv file, kept SEPARATE from the committed ``shisanhyo`` field.
    # The deterministic pipeline (EWS / classification / macro) NEVER reads
    # ``uploaded_shisanhyo``; it only reads ``shisanhyo``.
    # Confirmation copies ``uploaded_shisanhyo`` into ``shisanhyo`` and clears
    # the staging fields; cancel just clears them.
    uploaded_shisanhyo: list[TrialBalance] = Field(
        default_factory=list,
        description=(
            "Part 6: PROPOSED trial-balance rows parsed from a banker-uploaded "
            ".xlsx/.csv file, awaiting banker confirmation. "
            "NEVER read by the deterministic pipeline (EWS/classification/macro). "
            "Confirmation copies these into ``shisanhyo`` and clears staging; "
            "cancel just clears them."
        ),
    )
    upload_warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Part 6: Human-readable parser warnings from the last .xlsx/.csv "
            "upload (missing columns, bad cells, J-GAAP invariant violations, "
            "etc.). Shown to the banker alongside the proposed rows so they can "
            "make an informed confirmation decision. Cleared on confirm or cancel."
        ),
    )

    # --- Control ---
    errors: list[str] = Field(default_factory=list)

    # --- Loan-lifecycle spine (additive; append-only event log) ---
    # Optional event-sourced loan-facility log. Populated only when a loan is
    # attached to the run. The deterministic assessment pipeline (EWS /
    # classification / macro / critics) NEVER reads loan_events; it is a
    # side-record of where the facility sits in its lifecycle, appended to at
    # the human decision point (hitl_negotiation) when the banker approves a
    # turnaround/workout that implies a 条件変更 / 管理回収 transition. Append-only
    # via the LangGraph reducer (mirrors reconciliation_outcomes): never reset,
    # never read by any gate, route, or figure.
    loan_id: str = Field(
        default="",
        description=(
            "Stable facility identifier for the attached loan, or '' when no "
            "loan is attached to this run."
        ),
    )
    loan_events: Annotated[list[dict[str, Any]], loan_events_reducer] = Field(
        default_factory=list,
        description=(
            "Append-only loan-lifecycle event log (LoanEvent dicts) for the "
            "attached facility. Appended to at hitl_negotiation when the banker "
            "approves an FSA-implied 条件変更 / 管理回収 transition. Never reset, "
            "never read by any gate, route, or figure. Empty when no loan is "
            "attached (offline-safe)."
        ),
    )

    # --- Loan origination (融資組成) advisory recommendation (additive) ---
    # Set by loan_origination_node at the 稟議 gate: the deterministic, advisory
    # credit recommendation (APPROVE / DECLINE), provisional facility ceiling,
    # the grounded reason, and its grounding status. ADVISORY ONLY — the credit
    # decision (UNDER_REVIEW → APPROVED / DECLINED) is HITL-gated; this never
    # feeds a gate, route, or figure. None until the origination node runs.
    origination_recommendation: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Advisory origination recommendation from loan_origination_node at "
            "the 稟議 gate: deterministic APPROVE / DECLINE, provisional 融資上限, "
            "grounded reason, and grounding status. Surfaced to the banker; the "
            "credit decision is HITL-gated. Never feeds a gate, route, or figure."
        ),
    )

    # --- Loan origination (融資組成) banker credit decision (additive) ---
    # Set by origination_hitl_node after the banker decides at the 稟議 gate:
    # 'approve' (承認) or 'decline' (謝絖). The graph routes on it (approve →
    # disbursement; decline → END). None until the banker decides.
    origination_decision: str | None = Field(
        default=None,
        description=(
            "The banker's 稟議 credit decision recorded by origination_hitl_node: "
            "'approve' or 'decline'. Drives the origination graph's post-decision "
            "route (approve → disbursement; decline → END). None until decided."
        ),
    )

    # --- Loan servicing (貸出管理) operational action (additive) ---
    # Selects the deterministic, non-distress servicing transition the servicing
    # node records along the performing arc of a facility's life:
    #   'confirm' -> 実行 → 正常 (DISBURSED → PERFORMING): a drawn-down facility
    #                enters normal servicing (an operational step).
    #   'repay'   -> 正常 → 完済 (PERFORMING → CLOSED): full repayment (完済).
    # These are non-credit, non-distress operational facts and are NOT HITL-gated
    # (unlike every 条件変更 / 管理回収 / 償却 move, which the depth half owns). The
    # field is never read by the deterministic assessment pipeline or any gate,
    # route, or figure; it only tells the servicing node which transition to
    # record. None until a servicing action is requested.
    servicing_action: str | None = Field(
        default=None,
        description=(
            "The requested loan-servicing action recorded by servicing_node: "
            "'confirm' (実行 → 正常, enter normal servicing), 'repay' (正常 → 完済, "
            "full repayment), or 'repay_amount' (一部入金, a partial paydown of "
            "``servicing_amount`` yen that lowers the outstanding balance; auto-"
            "closes to 完済 when the balance reaches zero). Non-distress, non-gated "
            "operational moves; never feeds a gate, route, or figure. None until "
            "requested."
        ),
    )
    servicing_amount: int = Field(
        default=0,
        ge=0,
        description=(
            "Principal repaid in a 'repay_amount' servicing action (一部入金, "
            "integer yen, >= 0). Ignored by 'confirm'; for 'repay' the node uses "
            "the full outstanding balance. Recorded as principal_repaid on a "
            "repayment self-event; never feeds a gate, route, or figure."
        ),
    )

    # --- Loan restructure (条件変更 / リスケ) terms + advisory curing verdict ---
    # The HITL-gated distress move along the performing arc: a banker grants a
    # principal grace period and/or a lending-rate reduction to a struggling
    # PERFORMING / RESTRUCTURED facility. The proposed terms drive the
    # deterministic self-curing check (restructure_grounding.assess_restructure),
    # which projects the borrower's EWS trajectory under the relief and bands it
    # self_curing / marginal / non_curing. The terms feed only that ADVISORY
    # check; the PERFORMING -> RESTRUCTURED transition itself is HITL-gated and is
    # recorded by restructure_node only when authorised. None / 0 until proposed.
    restructure_grace_months: int = Field(
        default=0,
        ge=0,
        description=(
            "Proposed principal grace period (元本返済猶予) in months for a 条件変更 "
            "(リスケ). > 0 enables the grace relief leg of the self-curing check; "
            "the relief feeds only the ADVISORY restructure_curing verdict, never "
            "a gate, route, or figure. 0 until a restructure is proposed."
        ),
    )
    restructure_rate_reduction_bps: int = Field(
        default=0,
        ge=0,
        description=(
            "Proposed lending-rate reduction in basis points (e.g. 200 = 2.00%) "
            "for a 条件変更. > 0 enables the rate relief leg of the self-curing "
            "check; ADVISORY only, never feeds a gate, route, or figure. 0 until "
            "a restructure is proposed."
        ),
    )
    restructure_curing: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Advisory self-curing verdict from restructure_node: the deterministic "
            "RestructureCuring (band self_curing / marginal / non_curing, the "
            "annual relief breakdown, the EWS recovery month, and a bilingual "
            "reason) for the proposed 条件変更. Surfaced to the banker so forbearance "
            "that never cures is visible BEFORE the HITL-gated transition. Never "
            "feeds a gate, route, or figure. None until the node runs."
        ),
    )

    # --- Loan origination (融資組成) collateral / guarantee coverage inputs ---
    # Optional underwriting coverage figures the bank knows at the 稟議 gate but
    # the mock TDB feed does not carry: the pledged collateral value (担保評価額)
    # and the guaranteed portion (保証カバー額). They feed ONLY the advisory
    # collateral-coverage check (coverage.assess_coverage) on the origination
    # recommendation; they never feed a gate, route, or the recommended facility.
    # Default 0 (the prudent-banker base: unknown coverage is treated as none,
    # so a run that supplies neither bands as 'uncovered' rather than assuming
    # security) which also keeps existing runs byte-stable.
    collateral_value: int = Field(
        default=0,
        ge=0,
        description=(
            "Pledged collateral value (担保評価額) in integer yen for the proposed "
            "facility. Feeds only the advisory collateral-coverage check; never "
            "feeds a gate, route, or figure. 0 (treated as no collateral) until "
            "supplied."
        ),
    )
    guarantee_coverage: int = Field(
        default=0,
        ge=0,
        description=(
            "Guaranteed portion (保証カバー額) in integer yen for the proposed "
            "facility. Feeds only the advisory collateral-coverage check; never "
            "feeds a gate, route, or figure. 0 (treated as no guarantee) until "
            "supplied."
        ),
    )

    # --- Loan distress (条件変更 / 償却) graph control fields (additive) ---
    # Drive the HITL-gated distress graph (app.backend.graph_distress), the
    # graph-side edge that makes restructure_node / writeoff_node reachable.
    # Neither field is read by the deterministic assessment pipeline or any gate,
    # route, or figure beyond the distress graph's own proceed/abort route; they
    # only select which distress node records the transition and carry the
    # banker's gate decision. None until a distress run is started.
    distress_action: str | None = Field(
        default=None,
        description=(
            "The requested loan-distress action for the distress graph: "
            "'restructure' (条件変更, PERFORMING -> RESTRUCTURED, via "
            "restructure_node) or 'writeoff' (償却, WORKOUT -> WRITTEN_OFF, via "
            "writeoff_node). Selects the recording node on the proceed branch; "
            "never feeds a gate, route, or figure beyond that selection. None "
            "until a distress run is started."
        ),
    )
    distress_decision: str | None = Field(
        default=None,
        description=(
            "The banker's HITL-gated distress decision recorded by "
            "distress_hitl_node: 'proceed' (実行) or 'abort' (中止). Drives the "
            "distress graph's post-decision route (proceed -> the recording "
            "node; abort -> END). The gated transition is recorded only on "
            "proceed. None until the banker decides."
        ),
    )
