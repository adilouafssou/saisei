"""Tests for the Feature 7 assessment-basis section in the Keikakusho.

Locks in two contracts for ``render_keikakusho``'s explainability addendum:

1. **Byte-identical when omitted.** Without the new keyword-only basis args the
   rendered draft is exactly the pre-Feature-7 output, so existing render /
   polish / graph-flow expectations are unaffected.
2. **Faithful when supplied.** When the EWS score, per-signal breakdown, and
   classification reason are passed, the "## 1-2. 診断根拠" section appears with
   those exact deterministic figures.

Offline, deterministic; imports only from ``app.*``.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.kaizen_generation import render_keikakusho
from app.backend.state import Strategy
from app.shared.models.accounting import TrialBalance

_LATEST = TrialBalance(
    period=dt.date(2025, 6, 30),
    uriage=100_000_000,
    uriage_genka=78_000_000,
    hanbaihi=18_000_000,
    eigai_shueki=0,
    eigai_hiyo=0,
)
_STRATEGY = Strategy(
    title="価格転嫁の実行",
    rationale="Recover margin via price pass-through.",
    expected_keijo_uplift=36_000_000,
)


def _render(**kwargs: object) -> str:
    return render_keikakusho(
        company_name="テスト製造株式会社",
        hojin_bango="1234567890123",
        fsa_kanji="要注意先",
        latest=_LATEST,
        strategy=_STRATEGY,
        working_capital_gap=-5_000_000,
        **kwargs,  # type: ignore[arg-type]
    )


def test_byte_identical_when_basis_omitted() -> None:
    """No basis args -> output identical to a bare render (no new section)."""
    bare = _render()
    assert "診断根拠" not in bare
    # Passing only empty/None basis is equivalent to omitting it entirely.
    assert _render(ews_score=None, ews_breakdown=None, classification_reason="") == bare


def test_basis_section_present_when_supplied() -> None:
    """Supplying the basis appends the section with the exact figures."""
    breakdown = [
        {"key": "sales_drop", "label_ja": "売上減少", "raw": 0.18, "points": 13.5, "weight": 25.0},
        {
            "key": "margin_drop",
            "label_ja": "粗利率低下",
            "raw": 0.04,
            "points": 12.0,
            "weight": 30.0,
        },
    ]
    out = _render(
        ews_score=62.5,
        ews_breakdown=breakdown,
        classification_reason="EWS 62 ≥ 40 (要注意水準)",
    )
    assert "## 1-2. 診断根拠（Assessment basis）" in out
    assert "62.50 / 100" in out
    assert "EWS 62 ≥ 40" in out
    # Breakdown rows: label, measure (raw*100), points, weight.
    assert "売上減少" in out
    assert "18.0%" in out
    assert "13.5" in out
    assert "25" in out
    # The section sits inside section 1 / before section 2.
    assert out.index("診断根拠") < out.index("## 2. 改善施策")


def test_reason_only_renders_without_table() -> None:
    """A classification reason with no breakdown still renders the section."""
    out = _render(ews_score=10.0, ews_breakdown=[], classification_reason="全閾値を下回る")
    assert "診断根拠" in out
    assert "全閾値を下回る" in out
    # No table header when there is no breakdown.
    assert "寄与点 (Points)" not in out


class _HoshoConditions:
    """Minimal stand-in for HoshoKaijoConditions (attribute access)."""

    bunri_met = True
    bunri_score = 40.0
    zaimu_met = False
    zaimu_score = 17.5
    kaiji_met = False
    kaiji_score = 15.0
    ordered_directives = [
        "[P2 財務基盤] 経常利益を改善してください。",
        "[P3 情報開示] 12ヶ月分の試算表を提出してください。",
    ]


def test_hosho_basis_omitted_is_byte_identical() -> None:
    """No hosho args -> output identical to a bare render (no 保証解除 section)."""
    bare = _render()
    assert "経営者保証解除" not in bare
    assert _render(hosho_score=None, hosho_eligible=None, hosho_conditions=None) == bare


def test_hosho_basis_present_when_supplied() -> None:
    """Supplying the hosho basis appends the section with score, table, directives."""
    out = _render(
        hosho_score=72.5,
        hosho_eligible=True,
        hosho_conditions=_HoshoConditions(),
    )
    assert "## 5. 経営者保証解除（Guarantee-release basis）" in out
    assert "72.50 / 100" in out
    assert "該当（Eligible）" in out
    # Per-pillar table rows with verbatim points.
    assert "法人個人分離（Asset separation）" in out
    assert "40.0" in out
    assert "17.5" in out
    # Ordered actionable directives are listed.
    assert "解除に向けた課題（Required changes）" in out
    assert "経常利益を改善" in out


def test_hosho_conditions_accepts_dict() -> None:
    """A rehydrated dict shape renders identically to the model shape."""
    as_dict = {
        "bunri_met": True,
        "bunri_score": 40.0,
        "zaimu_met": False,
        "zaimu_score": 17.5,
        "kaiji_met": False,
        "kaiji_score": 15.0,
        "ordered_directives": _HoshoConditions.ordered_directives,
    }
    out = _render(hosho_score=72.5, hosho_eligible=True, hosho_conditions=as_dict)
    assert "## 5. 経営者保証解除（Guarantee-release basis）" in out
    assert "72.50 / 100" in out
