"""Deterministic uplift-credibility grounding (depth step 4) — ADVISORY ONLY.

The verifier that makes the "is the SME actually saved?" claim honest.

:mod:`app.backend.analysis.pnl_recovery` already projects the recovery curve and
reports the month the borrower's EWS crosses back under the 40 floor — but it
trusts the strategist's ``annual_uplift`` as given. Nothing deterministically
checks whether that claimed uplift is itself *credible against the firm's own
figures*. Without this gate a strategist could claim a ¥50M annual uplift on a
firm with ¥80M/yr sales and the recovery curve would dutifully project a fast,
beautiful — and fictional — recovery.

What this module does
---------------------
Given the latest trial balance history and a claimed annual ordinary-profit
uplift, it computes a deterministic *plausibility ceiling* built entirely from
the firm's OWN P&L structure (never an invented target):

- **margin-recovery headroom** — recovering the firm's compressed gross margin
  back toward its OWN historical-best margin, applied to current sales and
  annualised. Zero when margin never compressed (you cannot claim recovery of a
  margin the firm never lost).
- **cost-reduction headroom** — at most ``UPLIFT_SGA_REDUCTION_CEILING`` of the
  firm's OWN SG&A (販売費), annualised.
- **working-capital financing relief** — the recurring flow already modelled by
  ``WORKING_CAPITAL_FINANCING_RATE`` (reused here; single source of truth).

The claimed uplift is then classified against the ceiling as ``grounded`` /
``stretch`` / ``implausible`` (thresholds in :mod:`app.shared.constants`), with a
deterministic bilingual reason mirroring the ``classification_reason`` /
``trend_descriptor`` style.

Scope / honesty guard
---------------------
ADVISORY ONLY, exactly like the feasibility critic: this feeds no gate, no route,
and no figure used downstream. It annotates, so a banker sees an over-claimed
uplift BEFORE trusting the recovery curve. Pure arithmetic over verified inputs:
same inputs → same result, no LLM, no network, stdlib + models only. It can be
hand-verified (see ``tests/test_uplift_grounding.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.shared.constants import (
    MONTHS_PER_YEAR,
    UPLIFT_SGA_REDUCTION_CEILING,
    UPLIFT_STRETCH_FACTOR,
    WORKING_CAPITAL_FINANCING_RATE,
)
from app.shared.models.accounting import TrialBalance

__all__ = [
    "UpliftHeadroom",
    "UpliftCredibility",
    "compute_uplift_headroom",
    "classify_uplift_credibility",
    "uplift_credibility_reason",
    "assess_uplift",
]


# ---------------------------------------------------------------------------
# Headroom breakdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpliftHeadroom:
    """The firm's self-derived plausible annual ordinary-profit headroom.

    Every component is in integer yen and is derived from the firm's OWN figures.
    The components sum to :attr:`ceiling` (the maximum annual uplift the firm's
    structure plausibly supports).

    Attributes:
        margin_recovery: Annual yen from recovering compressed gross margin back
            toward the firm's own historical-best margin (0 if never compressed).
        cost_reduction: Annual yen from a bounded reduction of the firm's own
            SG&A (at most ``UPLIFT_SGA_REDUCTION_CEILING`` of 販売費).
        wc_financing_relief: Recurring annual yen from closing a working-capital
            deficit at ``WORKING_CAPITAL_FINANCING_RATE`` (0 when no deficit).
        ceiling: The sum of the three components — the plausibility ceiling.
    """

    margin_recovery: int
    cost_reduction: int
    wc_financing_relief: int
    ceiling: int


@dataclass(frozen=True)
class UpliftCredibility:
    """The credibility verdict for a strategist's claimed annual uplift.

    Attributes:
        claimed_uplift: The strategist's claimed annual ordinary-profit uplift.
        headroom: The firm's self-derived plausibility headroom.
        band: 'grounded' | 'stretch' | 'implausible'.
        ratio: claimed_uplift / ceiling (the over-claim multiple); ``None`` when
            the ceiling is 0 (no plausible headroom at all).
        reason: A deterministic bilingual explanation of the band.
    """

    claimed_uplift: int
    headroom: UpliftHeadroom
    band: str
    ratio: float | None
    reason: str


def _best_gross_margin(shisanhyo: list[TrialBalance]) -> float | None:
    """Return the firm's best historical gross-margin ratio, or None.

    Walks every month with positive sales and returns the highest
    gross-profit / sales ratio observed — the firm's OWN demonstrated best, not
    an industry benchmark or invented target. Returns None when no month has
    positive sales (margin is undefined).
    """
    margins = [tb.uriage_sourieki / int(tb.uriage) for tb in shisanhyo if int(tb.uriage) > 0]
    return max(margins) if margins else None


def compute_uplift_headroom(shisanhyo: list[TrialBalance]) -> UpliftHeadroom:
    """Compute the firm's self-derived plausible annual uplift ceiling.

    Pure deterministic arithmetic over the latest trial balance (the recovery
    base) plus the firm's own margin history. All three components are floored at
    0 — a firm that never compressed its margin gets no margin-recovery headroom,
    and a firm with no working-capital deficit gets no financing relief.

    Args:
        shisanhyo: Actual monthly trial balances, ascending by period. The last
            entry is the recovery base; the full series supplies the margin
            history. The working-capital deficit is NOT read from the trial
            balance (it is a separate signal); pass it via :func:`assess_uplift`.

    Returns:
        An :class:`UpliftHeadroom`. All-zero when there is insufficient history
        (< 1 month) or the latest month has no positive sales.
    """
    if not shisanhyo:
        return UpliftHeadroom(0, 0, 0, 0)

    latest = shisanhyo[-1]
    sales = int(latest.uriage)

    # --- 1. Margin-recovery headroom (annualised) ---
    # Recover the gap between the firm's best historical margin and its current
    # margin, applied to current monthly sales, annualised. Zero when margin
    # never compressed (current margin already equals the historical best) or
    # when sales are zero (margin undefined this month).
    margin_recovery_monthly = 0.0
    if sales > 0:
        best = _best_gross_margin(shisanhyo)
        current = latest.uriage_sourieki / sales
        if best is not None and best > current:
            margin_recovery_monthly = (best - current) * sales
    margin_recovery = int(round(margin_recovery_monthly * MONTHS_PER_YEAR))

    # --- 2. Cost-reduction headroom (annualised) ---
    # A bounded fraction of the firm's OWN SG&A (販売費). Never open-ended.
    cost_reduction_monthly = int(latest.hanbaihi) * UPLIFT_SGA_REDUCTION_CEILING
    cost_reduction = int(round(cost_reduction_monthly * MONTHS_PER_YEAR))

    # --- working-capital relief is added in assess_uplift (needs the gap) ---
    ceiling = max(0, margin_recovery) + max(0, cost_reduction)

    return UpliftHeadroom(
        margin_recovery=max(0, margin_recovery),
        cost_reduction=max(0, cost_reduction),
        wc_financing_relief=0,
        ceiling=ceiling,
    )


def _wc_financing_relief(working_capital_gap: int | None) -> int:
    """Return the recurring annual financing relief from closing a WC deficit.

    Reuses ``WORKING_CAPITAL_FINANCING_RATE`` (the single source of truth used by
    the working-capital strategy) applied to the deficit magnitude. Zero when
    there is no deficit (gap >= 0) or no gap is known.
    """
    if working_capital_gap is None or working_capital_gap >= 0:
        return 0
    return int(round(-working_capital_gap * WORKING_CAPITAL_FINANCING_RATE))


def classify_uplift_credibility(claimed_uplift: int, ceiling: int) -> tuple[str, float | None]:
    """Classify a claimed annual uplift against the firm's headroom ceiling.

    Bands (thresholds in :mod:`app.shared.constants`):
        claimed <= ceiling                          -> 'grounded'
        ceiling < claimed <= ceiling * STRETCH      -> 'stretch'
        claimed > ceiling * STRETCH                 -> 'implausible'

    A non-positive claimed uplift is always 'grounded' (claiming no improvement
    cannot over-claim). When the ceiling is 0 but a positive uplift is claimed,
    the firm's figures support NO uplift at all, so any positive claim is
    'implausible' and the ratio is None (division undefined).

    Args:
        claimed_uplift: The strategist's claimed annual ordinary-profit uplift.
        ceiling: The firm's self-derived plausibility ceiling.

    Returns:
        A tuple of (band, ratio) where ratio is claimed/ceiling (or None when
        the ceiling is 0).
    """
    if claimed_uplift <= 0:
        return "grounded", 0.0
    if ceiling <= 0:
        # No plausible headroom at all, but a positive uplift is claimed.
        return "implausible", None
    ratio = claimed_uplift / ceiling
    if claimed_uplift <= ceiling:
        return "grounded", ratio
    if claimed_uplift <= ceiling * UPLIFT_STRETCH_FACTOR:
        return "stretch", ratio
    return "implausible", ratio


def uplift_credibility_reason(
    band: str, claimed_uplift: int, headroom: UpliftHeadroom, ratio: float | None
) -> str:
    """Return the deterministic bilingual reason for a credibility band.

    Names WHICH self-derived headroom supports the claim and by what multiple the
    claim exceeds (or sits within) it — mirroring the ``classification_reason``
    style so a banker reads WHY the uplift is or isn't credible. Display/audit
    prose only; it decides nothing.

    Args:
        band: The band from :func:`classify_uplift_credibility`.
        claimed_uplift: The claimed annual uplift (JPY).
        headroom: The firm's self-derived headroom breakdown.
        ratio: The over-claim multiple, or None when the ceiling is 0.

    Returns:
        A short bilingual reason string.
    """
    ceiling = headroom.ceiling
    components = (
        f"粗利回復 {headroom.margin_recovery:,}円 + "
        f"販管費削減 {headroom.cost_reduction:,}円 + "
        f"資金繰り改善 {headroom.wc_financing_relief:,}円"
    )
    base = (
        f"計上上乗せ {claimed_uplift:,}円/年 vs 自社図せる実現上限 {ceiling:,}円/年（{components}）"
    )
    if band == "grounded":
        if claimed_uplift <= 0:
            return f"{base} ・ 上乗せ主張なし（no uplift claimed）"
        pct = f"{ratio * 100:.0f}%" if ratio is not None else "—"
        return f"{base} ・ 根拠あり（grounded; 実現上限の {pct} 内）"
    if band == "stretch":
        mult = f"{ratio:.1f}倍" if ratio is not None else "—"
        return f"{base} ・ 野心的（stretch; 実現上限の {mult}、要根拠補強）"
    # implausible
    if ratio is None:
        return f"{base} ・ 非現実的（implausible; 自社図せる実現上限がゼロ）"
    return f"{base} ・ 非現実的（implausible; 実現上限の {ratio:.1f}倍、過大計上）"


def assess_uplift(
    shisanhyo: list[TrialBalance],
    claimed_uplift: int,
    working_capital_gap: int | None = None,
) -> UpliftCredibility:
    """Assess the credibility of a claimed annual uplift against the firm's figures.

    The single public entry point. Composes the self-derived headroom (margin
    recovery + cost reduction + working-capital financing relief), classifies the
    claim against the resulting ceiling, and attaches a deterministic bilingual
    reason. Pure and ADVISORY ONLY — it returns an annotation and feeds no gate,
    route, or figure.

    Args:
        shisanhyo: Actual monthly trial balances, ascending by period.
        claimed_uplift: The strategist's claimed annual ordinary-profit uplift.
        working_capital_gap: Shikin Kuri gap (JPY; negative = deficit), or None.

    Returns:
        An :class:`UpliftCredibility` verdict.
    """
    partial = compute_uplift_headroom(shisanhyo)
    relief = _wc_financing_relief(working_capital_gap)
    headroom = UpliftHeadroom(
        margin_recovery=partial.margin_recovery,
        cost_reduction=partial.cost_reduction,
        wc_financing_relief=relief,
        ceiling=partial.ceiling + relief,
    )
    claimed = int(claimed_uplift)
    band, ratio = classify_uplift_credibility(claimed, headroom.ceiling)
    reason = uplift_credibility_reason(band, claimed, headroom, ratio)
    return UpliftCredibility(
        claimed_uplift=claimed,
        headroom=headroom,
        band=band,
        ratio=ratio,
        reason=reason,
    )
