"""Deterministic bias / fairness review report (Feature 7).

Renders the advisory :class:`~app.backend.analysis.fairness_review.FairnessReport`
(industry / region disparity over classification outcomes) into ONE exportable,
examiner-facing artifact, mirroring the other export renderers
(``explainability_report`` / ``model_card``):

- **Pure + deterministic + offline.** It only formats values the fairness
  analysis already computed. No figure, no verdict, no LLM, no network. Same
  report in -> byte-identical Markdown out.
- **Advisory framing baked in.** The report states plainly that a flagged
  disparity is a prompt for HUMAN review, not a proof of bias and not an
  automatic action — consistent with the analysis module's guardrails.
- Reuses the shared, number-safe Markdown -> DOCX / PDF renderers (the same
  converters the Keikakusho / explainability exporters use) so a regulator can
  receive the review as Word or a searchable PDF.
"""

from __future__ import annotations

from app.backend.analysis.fairness_review import FairnessAxis, FairnessReport
from app.backend.export._filenames import safe_filename_stem
from app.backend.export._markdown_docx import render_markdown_to_docx
from app.backend.export._markdown_pdf import render_markdown_to_pdf

__all__ = [
    "build_fairness_report",
    "build_fairness_report_docx",
    "build_fairness_report_pdf",
    "fairness_report_filename",
    "fairness_report_docx_filename",
    "fairness_report_pdf_filename",
]

#: Japanese axis labels for the report heading.
_AXIS_JA: dict[FairnessAxis, str] = {
    FairnessAxis.INDUSTRY: "業種 (Industry)",
    FairnessAxis.REGION: "地域 (Region / 都道府県)",
}


def build_fairness_report(report: FairnessReport) -> str:
    """Assemble the deterministic bias / fairness review as Markdown.

    Renders, from the already-computed :class:`FairnessReport`:
      1. the axis, the overall adverse rate, and the review thresholds used;
      2. a per-group table (borrowers, distressed, adverse rate, signed
         disparity, and whether the group is flagged), worst-faring first;
      3. the advisory rationale, stating that a flag prompts human review and is
         not a proof of bias or an automatic action.

    Pure and offline: it formats values, computing nothing.

    Args:
        report: The fairness analysis result to render.

    Returns:
        A Markdown report string.
    """
    axis_label = _AXIS_JA.get(report.axis, report.axis.value)
    lines: list[str] = [
        "# 公平性レビュー (Bias / fairness review)",
        "",
        "本レポートは決定論的に生成された参考情報（advisory）です。 "
        "フラグは人間によるレビューを促すものであり、偏見の証拠でも自動的な措置でもありません。 "
        "(Deterministically generated; advisory only. A flag prompts HUMAN review "
        "— it is neither a proof of bias nor an automatic action.)",
        "",
        f"## 軸 (Axis): {axis_label}",
        "",
        f"- **全体の要注意以上率 (Overall adverse rate):** {report.overall_adverse_rate:.1%}",
        f"- **分析対象件数 (Classified outcomes):** {report.classified_outcomes} "
        f"/ {report.total_outcomes}",
        f"- **判定閾値 (Disparity tolerance):** {report.disparity_tolerance:.0%}",
        f"- **最小グループ件数 (Min group size):** {report.min_group_size}",
        "",
    ]

    if report.per_group:
        lines += [
            "## グループ別 (Per group)",
            "",
            (
                "| グループ (Group) | 件数 (N) | 要注意以上 (Distressed) | "
                "率 (Rate) | 乖離 (Disparity) | フラグ |"
            ),
            "| --- | ---: | ---: | ---: | ---: | :---: |",
        ]
        for s in report.per_group:
            flag = "⚠" if s.flagged else "—"
            lines.append(
                f"| {s.group} | {s.total} | {s.distressed} | {s.adverse_rate:.1%} | "
                f"{s.disparity:+.1%} | {flag} |"
            )
        lines.append("")

    lines += [
        "## 所見 (Finding)",
        "",
        report.rationale,
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def build_fairness_report_docx(report: FairnessReport) -> bytes:
    """Render the deterministic fairness review to ``.docx`` bytes.

    The Word path for :func:`build_fairness_report`, via the shared number-safe
    :func:`render_markdown_to_docx` walker, so a regulator can receive the review
    as an editable Word document. Pure, deterministic, offline.
    """
    return render_markdown_to_docx(build_fairness_report(report))


def build_fairness_report_pdf(report: FairnessReport) -> bytes:
    """Render the deterministic fairness review to ``.pdf`` bytes.

    The PDF path for :func:`build_fairness_report`, via the shared CJK-correct
    :func:`render_markdown_to_pdf` renderer, so the review can be archived as a
    searchable, layout-stable PDF.

    Raises:
        PdfFontUnavailableError: When no CJK font is available to embed (the
            renderer never emits tofu; callers may fall back to DOCX).
    """
    return render_markdown_to_pdf(build_fairness_report(report))


def fairness_report_filename(axis: FairnessAxis) -> str:
    """Return a safe ``fairness_<axis>.md`` filename."""
    stem = safe_filename_stem(axis.value, fallback="review")
    return f"fairness_{stem}.md"


def fairness_report_docx_filename(axis: FairnessAxis) -> str:
    """Return a safe ``fairness_<axis>.docx`` filename."""
    stem = safe_filename_stem(axis.value, fallback="review")
    return f"fairness_{stem}.docx"


def fairness_report_pdf_filename(axis: FairnessAxis) -> str:
    """Return a safe ``fairness_<axis>.pdf`` filename."""
    stem = safe_filename_stem(axis.value, fallback="review")
    return f"fairness_{stem}.pdf"
