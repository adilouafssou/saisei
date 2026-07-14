"""Deterministic cross-signal realism check (depth step 4, part 3) — ADVISORY ONLY.

The feasibility critic now produces two INDEPENDENT deterministic signals for
each proposed strategy:

- ``achievability`` — the execution-risk band from the multi-factor floor
  (uplift-ratio + working-capital + rate + settlement stress). "How hard is
  this to pull off?"
- ``uplift_credibility`` — the magnitude-plausibility band from the firm's OWN
  self-derived headroom (margin recovery + cost reduction + WC relief). "Is the
  claimed payoff even possible for this firm?"

A sharp turnaround consultant's "is this *realistic*?" question is precisely
whether those two signals AGREE. The dangerous case is a strategy that scores
easy-to-execute yet claims a payoff the firm's own figures cannot support: it
sails through an execution-risk lens while being, in substance, fiction. The
mirror case — a believable, modest payoff that is nonetheless hard to execute —
is worth a second look for the opposite reason.

This module is a PURE FUNCTION of the two already-computed bands. It introduces
no new LLM, no new magic number, and no new data: it only reconciles two
signals the critic already produced. ADVISORY ONLY — it rides the FeasibilityNote
channel the spine proves never feeds a gate, route, or figure. Same inputs ->
same result.
"""

from __future__ import annotations

__all__ = ["assess_realism"]

#: Achievability bands that mean "not hard to execute" (execution risk is low).
_EASY_ACHIEVABILITY: frozenset[str] = frozenset({"high", "medium"})


def assess_realism(achievability: str, uplift_credibility: str) -> tuple[str, str]:
    """Reconcile the execution-risk band against the uplift-magnitude band.

    Pure deterministic function of two bands the feasibility critic already
    computed. Returns ``('', '')`` when either band is missing (an unassessed,
    no-history run), so such notes stay byte-identical to before this feature.

    Verdicts:
        - ``optimistic_uplift`` — execution looks easy (achievability high/medium)
          but the claimed payoff is ``implausible`` against the firm's own
          headroom. The dangerous contradiction: cheap to sell, impossible to
          deliver. This is the realism flag that earns its keep.
        - ``pessimistic_uplift`` — the payoff is ``grounded`` (believable) yet
          execution is ``low`` (hard). A credible plan that may be undervalued
          on feasibility; worth a second look.
        - ``consistently_weak`` — BOTH lenses condemn the strategy (execution
          ``low`` AND payoff ``implausible``). The signals do not contradict,
          but "consistent" would read as reassuring, so agreement-on-bad gets
          its own loud verdict, distinct from agreement-on-sound.
        - ``consistent`` — the two signals agree the strategy is sound (they do
          not contradict and at least one lens is favourable).

    Args:
        achievability: The execution-risk band ('high' | 'medium' | 'low').
        uplift_credibility: The magnitude band
            ('grounded' | 'stretch' | 'implausible').

    Returns:
        A tuple of (realism_flag, bilingual realism_note). Both empty when either
        input band is empty/unknown.
    """
    if not achievability or not uplift_credibility:
        return "", ""

    easy = achievability in _EASY_ACHIEVABILITY

    # Dangerous contradiction: easy to execute, but the payoff is fiction.
    if easy and uplift_credibility == "implausible":
        return (
            "optimistic_uplift",
            (
                f"不整合（楽観的）: 実行難易度は低い（{achievability}）が、"
                "上乗せ額は自社の実現上限を超える（implausible）。"
                "実行しやすく見えるが期待効果は過大。"
                "（easy to execute but the claimed payoff exceeds the firm's own "
                "headroom — review the uplift, not the execution）"
            ),
        )

    # Mirror case: believable payoff, but execution is hard.
    if not easy and uplift_credibility == "grounded":
        return (
            "pessimistic_uplift",
            (
                f"不整合（慎重）: 上乗せ額は根拠あり（grounded）だが、"
                f"実行難易度が高い（{achievability}）。"
                "実現可能な計画だが実行面の補強が必要。"
                "（a credible payoff but hard to execute — may be undervalued on "
                "feasibility）"
            ),
        )

    # Agreement-on-bad: BOTH lenses condemn the strategy (hard to execute AND the
    # payoff is fiction). The two signals do not contradict, but "consistent"
    # would dangerously read as reassuring -- so this is its own LOUD verdict,
    # distinct from agreement-on-sound. This is the design-review refinement of
    # the original binary: the flag now distinguishes agree-good from agree-bad.
    if not easy and uplift_credibility == "implausible":
        return (
            "consistently_weak",
            (
                f"要警戒（両面不足）: 実行難易度が高く（{achievability}）、"
                "かつ上乗せ額も自社の実現上限を超える（implausible）。"
                "実行も期待効果も両方とも弱く、再検討推奨。"
                "（both lenses condemn it: hard to execute AND the payoff exceeds "
                "the firm's own headroom — reconsider this strategy）"
            ),
        )

    return (
        "consistent",
        (
            f"整合あり: 実行難易度（{achievability}）と"
            f"上乗せ妥当性（{uplift_credibility}）は矛盾しない。"
            "（the two deterministic signals do not contradict）"
        ),
    )
