"""Shared constants for the Saisei app package."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# EWS classification thresholds (higher score = worse health).
#
# These map to the five FSA debtor categories (金融検査マニュアル):
#
#   EWS < EWS_SUBSTANDARD (40)          → 正常先  (Normal)
#   EWS_SUBSTANDARD <= EWS < EWS_DOUBTFUL (70)  → 要注意先 (Needs Attention)
#   EWS >= EWS_DOUBTFUL (70)            → 破綻懸念先 (In Danger of Bankruptcy)
#   EWS >= EWS_DANGER (85) OR insolvency signal → 実質破綻先 / 破綻先
#
# The insolvency signal (is_insolvent=True or net_worth < 0) overrides EWS
# and places the borrower in the two genuinely-bankrupt bands.
# ---------------------------------------------------------------------------

#: EWS threshold for 要注意先 (Needs Attention / Substandard).
#: Borrowers with EWS >= this value (but below EWS_DOUBTFUL) are 要注意先.
EWS_SUBSTANDARD: float = 40.0

#: EWS threshold for 破綻懸念先 (In Danger of Bankruptcy / Doubtful).
#: Borrowers with EWS >= this value are at minimum 破綻懸念先.
#: Also triggered by: working-capital deficit AND EWS >= EWS_SUBSTANDARD.
EWS_DOUBTFUL: float = 70.0

#: EWS threshold for the severe insolvency bands (実質破綻先 / 破綻先).
#: Borrowers with EWS >= this value are classified as 実質破綻先 even without
#: an explicit insolvency signal, because the financial deterioration is so
#: severe that de-facto bankruptcy is indicated.
#: The insolvency signal (is_insolvent=True or net_worth < 0) also reaches
#: these bands regardless of EWS score.
EWS_DANGER: float = 85.0

#: TDB score floor below which a debtor cannot be Normal.
TDB_NORMAL_FLOOR: int = 60

#: Annualisation factor (monthly -> yearly).
MONTHS_PER_YEAR: int = 12

#: Maximum revision cycles before the graph forces escalation.
MAX_REVISION_CYCLES: int = 3

#: Pro-rata deviation tolerance for sub-bank critic (fraction, e.g. 0.20 = 20%).
#: This is the single source of truth; sub_bank.py and lead_arranger.py import it.
PRO_RATA_TOLERANCE: float = 0.20

#: Minimum recovery horizon (years) required by guarantor critic.
MIN_RECOVERY_HORIZON_YEARS: int = 5

#: Keieisha Hosho scoring weights (must sum to 100).
HOSHO_WEIGHT_BUNRI: float = 40.0  # 法人個人分離
HOSHO_WEIGHT_ZAIMU: float = 35.0  # 財務基盤の強化
HOSHO_WEIGHT_KAIJI: float = 25.0  # 適時適切な情報開示

#: Thresholds for Hosho Kaijo eligibility.
HOSHO_ELIGIBLE_SCORE: float = 70.0  # Score >= this → eligible for release
HOSHO_SUCCESSION_EWS_MAX: float = 50.0  # EWS must be below this for succession readiness
HOSHO_SUCCESSION_TDB_MIN: int = 55  # TDB score must be >= this for succession readiness

# ---------------------------------------------------------------------------
# MR #2: Re-posed feasibility floor — deterministic multi-factor formula.
#
# The feasibility floor combines four deterministic signals:
#
#   1. uplift_ratio   = expected_annual_uplift / annual_sales
#                       (strategy ambition relative to firm size)
#   2. wc_stress      = max(0, -working_capital_gap) / annual_sales
#                       (working-capital deficit as a fraction of sales;
#                        0 when gap >= 0, i.e. no deficit)
#   3. rate_stress    = latest_policy_rate_bps / 10_000
#                       (BOJ rate as a decimal; 60 bps → 0.006)
#   4. settlement_stress = max(0, receivable_days - payable_days) / 90
#                          (cash-conversion-cycle stress; 90 days = reference)
#
# Composite score (0-100, higher = more achievable):
#
#   raw = 100
#         - FEASIBILITY_WEIGHT_UPLIFT   * uplift_ratio   * 100
#         - FEASIBILITY_WEIGHT_WC       * wc_stress      * 100
#         - FEASIBILITY_WEIGHT_RATE     * rate_stress    * 100
#         - FEASIBILITY_WEIGHT_SETTLE   * settle_stress  * 100
#
# Clamped to [0, 100] and rounded to 2 decimal places.
#
# Band thresholds (score-based, not ratio-based):
#   score >= FEASIBILITY_HIGH_FLOOR  → 'high'
#   score >= FEASIBILITY_MEDIUM_FLOOR → 'medium'
#   score <  FEASIBILITY_MEDIUM_FLOOR → 'low'
#
# Industry-specific uplift weight modifier (FEASIBILITY_INDUSTRY_UPLIFT_FACTOR):
#   Manufacturing / 製造業 industries carry higher execution risk for a given
#   uplift ratio (capital-intensive, long lead times) → weight multiplied by
#   FEASIBILITY_INDUSTRY_UPLIFT_FACTOR (> 1.0).  Service industries use the
#   base weight (factor = 1.0).  The industry string is matched by keyword.
#
# All weights are auditable constants; no LLM involvement.
# ---------------------------------------------------------------------------

#: Weight for uplift-ratio component (base; may be scaled by industry factor).
FEASIBILITY_WEIGHT_UPLIFT: float = 1.5

#: Weight for working-capital-deficit component.
FEASIBILITY_WEIGHT_WC: float = 1.2

#: Weight for BOJ rate-stress component.
FEASIBILITY_WEIGHT_RATE: float = 0.8

#: Weight for settlement / cash-conversion-cycle stress component.
FEASIBILITY_WEIGHT_SETTLE: float = 0.5

#: Industry uplift-weight multiplier for capital-intensive sectors
#: (製造業, 建設業, 運輸業).  Service industries use 1.0 (no adjustment).
FEASIBILITY_INDUSTRY_UPLIFT_FACTOR: float = 1.25

#: Score floor for 'high' achievability band.
FEASIBILITY_HIGH_FLOOR: float = 65.0

#: Score floor for 'medium' achievability band (below → 'low').
FEASIBILITY_MEDIUM_FLOOR: float = 35.0

# ---------------------------------------------------------------------------
# MR #2: LLM-vs-floor reconciliation threshold.
#
# The reconciliation trigger is a PURE DETERMINISTIC PREDICATE:
#   band_distance = |ordinal(deterministic_band) - ordinal(llm_band)|
#   reconciliation_required = band_distance >= RECONCILIATION_BAND_DISTANCE
#
# Band ordinals: 'high' = 2, 'medium' = 1, 'low' = 0.
# A distance of 2 means the LLM and the floor are at opposite ends of the
# scale (e.g. floor='high', LLM='low'), which is a strong signal of
# disagreement that warrants human review.
#
# When no LLM is configured, reconciliation_required stays False (no-op).
# The LLM can ONLY raise the question; the routing decision is a pure function
# of (deterministic_band, llm_band, RECONCILIATION_BAND_DISTANCE).
# ---------------------------------------------------------------------------

#: Minimum band distance (ordinal) that triggers reconciliation-to-HITL.
#: 1 = any adjacent-band disagreement; 2 = only full-scale disagreement.
#:
#: CALIBRATION PLACEHOLDER (see #1): this value is hand-set, not fitted to
#: outcomes. It is a safe cold-start floor, NOT a principled threshold. The
#: tracked path off this magic number is the live who-was-right corpus captured
#: at every HITL resolution — ``SaiseiState.reconciliation_outcomes`` (see
#: ``ReconciliationOutcome`` in app/backend/state.py). A small offline analysis
#: over that corpus reports the empirical precision of each band-distance level
#: so this threshold can be moved on evidence. Until enough outcomes accrue,
#: this constant is intentionally conservative (full-scale disagreement only).
RECONCILIATION_BAND_DISTANCE: int = 2

# ---------------------------------------------------------------------------
# MR #3: Deterministic per-run ceiling on reconciliation triggers.
#
# RATIONALE — alert-fatigue / authority-leakage risk:
#   A pathological LLM that disagrees on every strategy would flood the banker
#   with review triggers, effectively influencing decisions through fatigue and
#   violating the advisory-only contract in practice. This ceiling closes that
#   risk structurally.
#
# DESIGN — ranked selection, not truncation:
#   All qualifying disagreements (band_distance >= RECONCILIATION_BAND_DISTANCE)
#   are recorded in reconciliation_details for full audit transparency. The top-N
#   by band_distance (descending) are marked routed=True and drive the routing
#   decision. This ensures the LLM's STRONGEST signals always reach the human
#   while flooding is structurally impossible.
#
# VALUE RATIONALE — why 2:
#   Typical runs produce 3-4 strategies. A budget of 2 lets genuine
#   multi-strategy disagreement surface (keeps the LLM powerful) while
#   preventing a pathological 'everything disagrees' run from carpet-bombing
#   the banker (keeps authority bounded). 'More power, never more authority.'
#
# INVARIANT: reconciliation_required is True if and only if at least one
#   disagreement qualifies (single-disagreement routing is unchanged). The
#   ceiling governs how details are prioritised/marked, not whether a single
#   disagreement still routes.
# ---------------------------------------------------------------------------

#: Maximum number of qualifying disagreements that may drive routing (routed=True).
#: All qualifying disagreements are still recorded for audit; only the top-N by
#: band_distance are marked routed=True. Ties broken by strategy_title ascending
#: for byte-stable deterministic output.
#:
#: CALIBRATION PLACEHOLDER (see #1): this per-run cap is hand-set, not derived
#: from banker capacity or outcome data. It is a safe floor that stops flooding
#: today, NOT the destination. The tracked path off this magic number is the
#: live who-was-right corpus captured at every HITL resolution —
#: ``SaiseiState.reconciliation_outcomes`` (see ``ReconciliationOutcome`` in
#: app/backend/state.py). The robust version replaces this cap with a triage
#: queue ranked by calibrated value, filled to the banker's actual attention.
MAX_RECONCILIATION_TRIGGERS: int = 2

# ---------------------------------------------------------------------------
# Working-capital strategy: recurring financing-cost benefit.
#
# The working-capital improvement strategy's expected ordinary-profit uplift is
# a recurring FLOW (the annual financing cost saved by closing the gap), NOT the
# full working-capital gap STOCK. Using the raw abs(gap) as the uplift made a
# single strategy dwarf the price/COGS/SG&A measures and distorted the sub-bank
# pro-rata heuristic (largest-strategy share). The recurring benefit is the
# carrying cost of the gap at an assumed financing rate.
# ---------------------------------------------------------------------------

#: Assumed annual financing-cost rate applied to the working-capital gap to
#: derive the recurring ordinary-profit benefit of closing it (a flow figure).
WORKING_CAPITAL_FINANCING_RATE: float = 0.05

# ---------------------------------------------------------------------------
# Strategist uplift-credibility grounding (depth step 4) — ADVISORY ONLY.
#
# A deterministic plausibility CEILING for a strategist's claimed annual
# ordinary-profit uplift, derived entirely from the firm's OWN figures so the
# "is the SME actually saved?" recovery curve is not built on an uplift the firm
# could never produce. The ceiling is the sum of three self-derived headrooms:
#
#   1. margin-recovery headroom — recovering the firm's compressed gross margin
#      back toward its OWN historical-best margin, applied to current sales and
#      annualised. Zero when margin never compressed (you cannot claim recovery
#      of a margin the firm never lost).
#   2. cost-reduction headroom — at most UPLIFT_SGA_REDUCTION_CEILING of the
#      firm's OWN SG&A (販売費), annualised. A bounded, realistic cost take-out,
#      never an open-ended one.
#   3. working-capital financing relief — the recurring flow already modelled by
#      WORKING_CAPITAL_FINANCING_RATE (reused; single source of truth).
#
# The claimed uplift is then classified against the ceiling:
#   claimed <= ceiling                              → 'grounded'
#   ceiling < claimed <= ceiling * STRETCH_FACTOR   → 'stretch'
#   claimed > ceiling * STRETCH_FACTOR              → 'implausible'
#
# These are conservative cold-start constants, NOT fitted to outcomes; a
# production deployment calibrates them to the bank's turnaround track record.
# They are auditable — no LLM involvement — and feed no gate, route, or figure.
# ---------------------------------------------------------------------------

#: Maximum fraction of the firm's own SG&A (販売費) treated as plausibly
#: reducible within a turnaround horizon (the cost-reduction headroom ceiling).
#: Conservative: a realistic cost take-out, not an open-ended one.
UPLIFT_SGA_REDUCTION_CEILING: float = 0.20

#: Multiple of the self-derived headroom ceiling below which an over-claim is a
#: 'stretch' rather than 'implausible'. A claim up to this multiple of the firm's
#: own plausible headroom is ambitious-but-arguable; beyond it the uplift is not
#: credibly supported by the firm's figures.
UPLIFT_STRETCH_FACTOR: float = 1.5

# ---------------------------------------------------------------------------
# Loan-loss provisioning (貸倒引当金) by FSA debtor classification.
#
# Under the FSA self-assessment framework (自己査定), a bank reserves against
# each loan in proportion to the borrower's debtor class. These are the
# deterministic reserve ratios applied to a facility's outstanding principal to
# compute its loan-loss provision (貸倒引当金):
#
#   正常先   (Normal)                  → PROVISION_RATE_NORMAL
#   要注意先  (Needs Attention)          → PROVISION_RATE_NEEDS_ATTENTION
#   要管理先  (Special Attention sub-tier) → PROVISION_RATE_SPECIAL_ATTENTION
#   破綻懸念先 (In Danger of Bankruptcy)  → PROVISION_RATE_DOUBTFUL
#   実質破綻先 / 破綻先 (De-facto / Bankrupt) → PROVISION_RATE_BANKRUPT
#
# These are conservative, illustrative reference ratios for the deterministic
# model; a production deployment calibrates them to the bank's own historical
# loss experience (実績率) and collateral coverage. They are auditable
# constants — no LLM involvement — and the provision is a deterministic figure
# computed from outstanding principal, never produced or altered by a model.
#
# 要管理先 (Special Attention) is a sub-tier of 要注意先 carrying a heavier
# reserve, mirroring how the classification layer models it as a
# special_attention flag on a 要注意先 borrower rather than a separate class.
# ---------------------------------------------------------------------------

#: Reserve ratio for 正常先 (Normal).
PROVISION_RATE_NORMAL: float = 0.002

#: Reserve ratio for 要注意先 (Needs Attention / Substandard).
PROVISION_RATE_NEEDS_ATTENTION: float = 0.05

#: Reserve ratio for 要管理先 (Special Attention — a heavier sub-tier of 要注意先).
PROVISION_RATE_SPECIAL_ATTENTION: float = 0.15

#: Reserve ratio for 破綻懸念先 (In Danger of Bankruptcy / Doubtful).
PROVISION_RATE_DOUBTFUL: float = 0.70

#: Reserve ratio for 実質破綻先 / 破綻先 (De-facto Bankrupt / Bankrupt).
PROVISION_RATE_BANKRUPT: float = 1.0

# ---------------------------------------------------------------------------
# Loan origination (融資組成) — deterministic underwriting thresholds (breadth).
#
# The origination half of the loan-lifecycle spine. Where the distress half maps
# an FSA debtor class onto a 条件変更 / 管理回収 transition
# (``proposed_transition_for``) and reserves a 貸倒引当金 (``provision_amount``),
# the origination half turns the TDB credit assessment of an applicant into a
# deterministic, advisory credit recommendation (APPROVE / DECLINE) plus a
# provisional facility ceiling at the 稟議 (UNDER_REVIEW → APPROVED) gate.
#
# These mirror the distress constants exactly in spirit: auditable reference
# values, no LLM involvement, ADVISORY ONLY. The helpers that read them only
# *propose*; the UNDER_REVIEW → APPROVED / DECLINED transition they imply is
# HITL-gated (see HITL_GATED_TRANSITIONS in app/shared/models/loan.py), so the
# banker remains the only decider. A production deployment calibrates them to
# the bank's own credit policy / 信用格付 model and collateral framework.
# ---------------------------------------------------------------------------

#: Minimum TDB credit score (1-100) an applicant must meet to be RECOMMENDED for
#: approval at the 稟議 gate. Below this the deterministic recommendation is
#: DECLINE. Pinned to the existing 正常先 TDB floor (TDB_NORMAL_FLOOR) so
#: origination and post-origination classification agree on what "creditworthy"
#: means — an applicant the bank would immediately classify below 正常先 should
#: not be recommended for a new facility.
ORIGINATION_TDB_APPROVE_FLOOR: int = TDB_NORMAL_FLOOR

#: Provisional facility ceiling as a multiple of the applicant's annualised
#: sales (年商). The recommended maximum facility =
#: round(annual_sales * this multiple). A conservative cold-start cap on
#: exposure relative to firm size; a production deployment replaces it with the
#: bank's exposure-to-turnover policy and collateral coverage.
ORIGINATION_MAX_FACILITY_SALES_MULTIPLE: float = 0.5

# ---------------------------------------------------------------------------
# Origination debt-service-capacity check (breadth step 2) — ADVISORY ONLY.
#
# The facility ceiling above is anchored to firm SIZE (年商) but is blind to the
# firm's debt-servicing CAPACITY: a firm with ¥200M sales and razor-thin or
# negative ordinary profit gets the same ceiling as a healthy ¥200M-sales firm.
# That is the origination twin of the naive uplift number the distress side had
# before uplift_grounding -- a figure anchored to the wrong denominator.
#
# The firm's own P&L already says what debt it can carry: ordinary profit
# (経常利益) is the cash available to service new debt. This check derives the
# facility's implied ANNUAL debt service and compares it to a prudent fraction
# of the firm's DEMONSTRATED ordinary profit:
#
#   implied_annual_debt_service =
#       facility / DEBT_CAPACITY_AMORTIZATION_YEARS          (principal leg)
#     + facility * WORKING_CAPITAL_FINANCING_RATE            (interest leg;
#                                                            reused, single
#                                                            source of truth)
#   prudent_service_ceiling = ordinary_profit_base * DEBT_CAPACITY_DSCR_FRACTION
#
# Bands (mirroring the uplift credibility band exactly):
#   service <= ceiling                                 → 'within_capacity'
#   ceiling < service <= ceiling * STRETCH_FACTOR      → 'stretch'
#   service > ceiling * STRETCH_FACTOR                 → 'over_capacity'
#
# Prudent-banker asymmetry vs the uplift verifier: where uplift_grounding uses
# the firm's BEST historical margin (you cannot claim recovery of a margin never
# lost), this check uses a CONSERVATIVE ordinary-profit base -- min(trailing
# average, latest), floored at 0 -- because over-stating serviceable capacity is
# the dangerous direction. A firm with non-positive ordinary profit can service
# no new debt, so any positive facility is 'over_capacity' (the parallel of the
# uplift 'ceiling <= 0 → implausible' rule).
#
# Conservative cold-start constants, NOT fitted to outcomes; a production
# deployment calibrates them to the bank's credit policy. Auditable -- no LLM
# involvement -- and ADVISORY ONLY: they feed no gate, route, or figure.
# ---------------------------------------------------------------------------

#: Standard amortization horizon (years) used to derive the principal leg of a
#: facility's implied annual debt service. A longer horizon implies a smaller
#: annual principal repayment; 5 years is a conservative mid-term reference for
#: an SME facility. The interest leg reuses WORKING_CAPITAL_FINANCING_RATE
#: (single source of truth) rather than introducing a separate rate constant.
DEBT_CAPACITY_AMORTIZATION_YEARS: int = 5

#: Prudent fraction of the firm's demonstrated annual ordinary profit (経常利益)
#: treated as available to service new debt -- a DSCR-style cushion that keeps
#: headroom for existing obligations, capex, and earnings volatility rather than
#: assuming every yen of ordinary profit can be pledged. Conservative.
DEBT_CAPACITY_DSCR_FRACTION: float = 0.5

#: Multiple of the prudent service ceiling below which an over-sized facility is
#: a 'stretch' rather than 'over_capacity'. Mirrors UPLIFT_STRETCH_FACTOR: a
#: facility whose implied debt service is up to this multiple of prudent
#: capacity is aggressive-but-arguable; beyond it it is over-sized relative to
#: the firm's demonstrated capacity.
DEBT_CAPACITY_STRETCH_FACTOR: float = 1.5

# ---------------------------------------------------------------------------
# Restructure self-curing grounding (depth step 5) — ADVISORY ONLY.
#
# The distress mirror of the origination debt-capacity check. A restructure
# (条件変更 / リスケ) grants a borrower relief — a principal grace period and/or a
# lending-rate reduction — to buy time to recover. But a restructure that does
# NOT bring the borrower back under the 正常先 EWS floor within a prudent horizon is
# not a turnaround, it is forbearance that defers (and often deepens) the loss:
# a 貸出条件緩和債権 that never cures. Nothing deterministically checked whether a
# proposed restructure is actually SELF-CURING against the borrower's own EWS
# trajectory.
#
# This check computes the recurring annual ordinary-profit relief the restructure
# produces, built entirely from the facility's OWN figures:
#
#   grace relief    = (outstanding / DEBT_CAPACITY_AMORTIZATION_YEARS)
#                     * grace_fraction        (principal repayment deferred)
#   rate relief     = outstanding * (rate_reduction_bps / 10_000)
#                                             (interest saved by the cut)
#   annual_relief   = grace relief + rate relief
#
# That relief is then fed through the SAME deterministic recovery projector the
# recovery curve uses (project_recovery), and the month the borrower's recomputed
# EWS crosses back under the floor is compared to the prudent regulatory horizon
# (MIN_RECOVERY_HORIZON_YEARS):
#
#   recovers within horizon            → 'self_curing'
#   recovers, but only beyond horizon  → 'marginal'
#   never recovers within projection   → 'non_curing'
#
# Conservative cold-start constants, NOT fitted to outcomes. Auditable — no LLM
# involvement — and ADVISORY ONLY: it annotates, feeding no gate, route, or
# figure (the 条件変更 transition stays HITL-gated).
# ---------------------------------------------------------------------------

#: Fraction of the scheduled annual principal repayment that a grace period
#: (元本返済猶予) defers — i.e. the share of amortization relieved while the grace
#: is in effect. 1.0 models a full principal holiday (the common リスケ shape);
#: a partial grace passes a smaller fraction. Used to size the grace relief.
RESTRUCTURE_FULL_GRACE_FRACTION: float = 1.0

# ---------------------------------------------------------------------------
# Origination collateral / guarantee coverage check (breadth step 6) — ADVISORY ONLY.
#
# The breadth twin of the debt-capacity check, on the OTHER side of the credit
# question. The debt-capacity check asks "can the firm's P&L SERVICE this
# facility?"; this asks "if it cannot, what of the facility is SECURED?" — the
# collateral (担保) and guarantee (保証) coverage behind the exposure. A facility
# can be within debt-service capacity yet largely unsecured (a clean credit risk),
# or over capacity yet fully collateralised (a recoverable one); the banker needs
# BOTH lenses at the 稟議 gate, and only the capacity lens existed.
#
# Coverage is the secured + guaranteed value as a fraction of the proposed
# facility:
#
#   covered_amount  = collateral_value + guarantee_coverage   (floored at 0)
#   coverage_ratio  = covered_amount / facility               (None when facility <= 0)
#   uncovered_amount = max(0, facility - covered_amount)       (the clean-risk tail)
#
# Bands (mirroring the debt-capacity band shape, prudent-banker direction):
#   coverage_ratio >= COVERAGE_WELL_COVERED_FLOOR   → 'well_covered'
#   coverage_ratio >= COVERAGE_PARTIAL_FLOOR        → 'partial'
#   coverage_ratio <  COVERAGE_PARTIAL_FLOOR        → 'uncovered'
#
# A facility with no proposed amount (a DECLINE carries a 0 ceiling) is trivially
# 'well_covered' (no exposure to cover), mirroring the debt-capacity 0-facility
# 'within_capacity' rule. Coverage figures default to 0 (the conservative,
# prudent-banker base: unknown collateral is treated as none, never assumed), so
# a run that supplies no coverage data bands as 'uncovered' rather than guessing.
#
# Conservative cold-start thresholds, NOT fitted to outcomes; a production
# deployment calibrates them to the bank's collateral-haircut and guarantee
# framework. Auditable — no LLM involvement — and ADVISORY ONLY: they feed no
# gate, route, or figure (the 稟議 credit decision stays HITL-gated).
# ---------------------------------------------------------------------------

#: Coverage ratio (secured+guaranteed / facility) at or above which a facility is
#: 'well_covered' — fully or near-fully secured. 1.0 means the pledged collateral
#: and guarantee at least equal the exposure.
COVERAGE_WELL_COVERED_FLOOR: float = 1.0

#: Coverage ratio at or above which a facility is 'partial' (below → 'uncovered').
#: A facility covered for at least this fraction of its value carries a meaningful
#: but incomplete security cushion; below it the exposure is materially unsecured.
COVERAGE_PARTIAL_FLOOR: float = 0.5
