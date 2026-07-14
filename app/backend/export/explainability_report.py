"""Deterministic explainability report (Feature 7).

The interactive UI already shows *why* a borrower landed in its FSA band (the
EWS per-signal breakdown, the classification reason, the guarantee-release
pillars). This module assembles those SAME already-computed, deterministic
figures into ONE exportable artifact — an examiner-facing "explainability report"
in Markdown — so the basis of a classification can be archived / attached to a
credit file, not only read on screen.

It mirrors the other export renderers (``keikakusho_docx`` / ``recovery_xlsx``):

- **Pure + deterministic + offline.** It only formats values the deterministic
  spine produced (EWS signals, the classification reason, the Hosho pillars).
  It computes no figure and no verdict, calls no LLM, and touches no network.
  Same inputs -> byte-identical report.
- **Numeric-preservation safe.** Every figure is rendered verbatim from the
  source object; the report never re-derives or rounds a score.
- **Reads defensively** (``getattr`` / mapping-aware) so it works on a live
  ``SaiseiState`` or a checkpointer-rehydrated dict, exactly like the UI helpers.

The report is the natural consumer of the SAME data the audit ledger pins, so an
examiner reading ``GET /audit/{thread_id}`` and an examiner reading this report
see one consistent story.
"""

from __future__ import annotations

from typing import Any

from app.backend.export._filenames import safe_filename_stem
from app.backend.export._markdown_docx import render_markdown_to_docx
from app.backend.export._markdown_pdf import render_markdown_to_pdf
from app.shared.models.classification import FsaClass

__all__ = [
    "build_explainability_report",
    "explainability_filename",
    "build_explainability_docx",
    "explainability_docx_filename",
    "build_explainability_pdf",
    "explainability_pdf_filename",
]


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a model attribute or a mapping key (rehydration-safe)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _fsa_kanji(classification: Any) -> str:
    """Return the FSA classification kanji from an enum, str, or None.

    Rehydration-safe: a checkpointer snapshot may carry ``fsa_classification`` as
    the live :class:`FsaClass` enum OR as its plain romanized string value (since
    ``FsaClass`` is a ``StrEnum``). Mirrors the UI's ``_fsa_kanji`` so the report
    never prints the romanized id (e.g. ``yochuisaki``) instead of ``要注意先``.
    """
    if not classification:
        return "—"
    if isinstance(classification, FsaClass):
        return classification.kanji
    try:
        return FsaClass(str(classification)).kanji
    except ValueError:
        # Unknown value (e.g. already a kanji label) — display as-is.
        return str(classification)


def _fmt_num(value: Any) -> str:
    """Format a numeric value compactly: int when whole, else 2 dp; '—' if None."""
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def _ews_section(ews_score: Any, breakdown: list[Any]) -> list[str]:
    """Render the EWS score + its per-signal contribution table."""
    lines = [
        "## EWSスコアの内訳 (EWS score breakdown)",
        "",
        f"- **EWSスコア (score):** {_fmt_num(ews_score)} / 100",
        "",
    ]
    if not breakdown:
        lines.append(
            "_十分な月次履歴がないため、内訳はありません。 (No breakdown: insufficient history.)_"
        )
        lines.append("")
        return lines
    lines.append("| シグナル (Signal) | 寄与 (Points) | 上限 (Weight) | 測定値 (Raw) |")
    lines.append("| --- | ---: | ---: | ---: |")
    for sig in breakdown:
        label = _attr(sig, "label_ja", _attr(sig, "key", ""))
        points = _fmt_num(_attr(sig, "points"))
        weight = _fmt_num(_attr(sig, "weight"))
        raw = _attr(sig, "raw")
        raw_txt = f"{float(raw):.1%}" if isinstance(raw, (int, float)) else "—"
        lines.append(f"| {label} | {points} | {weight} | {raw_txt} |")
    lines.append("")
    lines.append(
        "_各シグナルの寄与は合計してEWSスコアに一致します（単一の算出源）。 "
        "(Signal points sum to the EWS score — one source of truth.)_"
    )
    lines.append("")
    return lines


def _hosho_section(hosho_score: Any, conditions: Any) -> list[str]:
    """Render the guarantee-release (Hosho Kaijo) pillar basis, when present."""
    if conditions is None:
        return []
    lines = [
        "## 経営者保証解除の根拠 (Guarantee-release basis)",
        "",
        f"- **保証解除スコア (score):** {_fmt_num(hosho_score)} / 100",
        "",
        "| 柱 (Pillar) | 充足 (Met) | スコア (Score) |",
        "| --- | :---: | ---: |",
    ]
    pillars = (
        ("法人個人分離 (Separation)", "bunri_met", "bunri_score"),
        ("財務基盤 (Financial base)", "zaimu_met", "zaimu_score"),
        ("情報開示 (Disclosure)", "kaiji_met", "kaiji_score"),
    )
    for label, met_key, score_key in pillars:
        met = bool(_attr(conditions, met_key, False))
        met_txt = "✓" if met else "✗"
        lines.append(f"| {label} | {met_txt} | {_fmt_num(_attr(conditions, score_key))} |")
    lines.append("")

    directives = _attr(conditions, "ordered_directives", []) or []
    if directives:
        lines.append("**解除のための改善事項 (What must change to release):**")
        lines.append("")
        for directive in directives:
            lines.append(f"- {directive}")
        lines.append("")
    return lines


def build_explainability_report(state: Any) -> str:
    """Assemble the deterministic explainability report (Markdown) from state.

    Renders, from the already-computed deterministic figures on ``state``:
      1. the borrower identity + the FSA classification and the exact reason it
         landed there (which signal crossed which threshold);
      2. the EWS per-signal contribution table (parts that sum to the score);
      3. the guarantee-release pillar basis + the actionable directives.

    Pure and offline: it formats values, never computing a figure or verdict.
    Reads defensively so it works on a live state or a rehydrated dict.

    Args:
        state: The graph state (or state-like object) for the borrower.

    Returns:
        A Markdown report string.
    """
    fsa = _attr(state, "fsa_classification")
    fsa_kanji = _fsa_kanji(fsa)
    tdb_code = _attr(state, "tdb_code", "") or "—"
    profile = _attr(state, "company_profile")
    # CompanyProfile exposes ``name``; ``company_name`` is only a rehydration-safe
    # fallback (e.g. a dict snapshot that used that key).
    company = ""
    if profile:
        company = _attr(profile, "name", "") or _attr(profile, "company_name", "")
    reason = _attr(state, "classification_reason", "") or "—"
    special = bool(_attr(state, "special_attention", False))

    lines: list[str] = [
        "# 説明可能性レポート (Explainability report)",
        "",
        "本レポートは決定論的に生成されており、各値はエンジンが算出した数値をそのまま表示します。 "
        "(Deterministically generated; every figure is rendered verbatim.)",
        "",
        "## 借り入れ先 (Borrower)",
        "",
        f"- **企業名 (Company):** {company or '—'}",
        f"- **TDBコード:** {tdb_code}",
        "",
        "## 債務者区分 (FSA classification)",
        "",
        f"- **区分 (Class):** {fsa_kanji}"
        + ("（要管理先 / special attention）" if special else ""),
        f"- **根拠 (Reason):** {reason}",
        "",
    ]
    lines += _ews_section(_attr(state, "ews_score"), _attr(state, "ews_breakdown", []) or [])
    lines += _hosho_section(
        _attr(state, "hosho_kaijo_score"), _attr(state, "hosho_kaijo_conditions")
    )
    return "\n".join(lines).rstrip() + "\n"


def explainability_filename(company_or_code: str) -> str:
    """Return a safe ``explainability_<name>.md`` filename.

    Sanitises the name for cross-OS downloads via the shared helper the DOCX /
    XLSX exporters use (collapses path separators / whitespace and strips the
    Windows-illegal characters ``: * ? \" < > |`` and control chars); falls back
    to ``borrower`` when the name sanitises to empty.
    """
    stem = safe_filename_stem(company_or_code or "", fallback="borrower")
    return f"explainability_{stem}.md"


def build_explainability_docx(state: Any) -> bytes:
    """Render the deterministic explainability report to ``.docx`` bytes.

    Japanese banks and FSA examiners exchange regulated documents as Word, not
    Markdown. This is the Word path for the SAME report :func:`build_explainability_report`
    produces: it renders that exact Markdown through the shared, number-safe
    :func:`render_markdown_to_docx` walker (the same converter the Keikakusho
    DOCX uses), so the classification basis (which signal crossed which
    threshold, the EWS breakdown, the guarantee-release pillars) can be archived
    or attached to a credit file as an editable Word artifact.

    Pure, deterministic, and offline: it formats already-computed figures and
    computes nothing; same state in -> same ``.docx`` structure out. Reads
    defensively (a live state or a rehydrated dict) exactly like the Markdown
    renderer, since it simply reuses it.

    Args:
        state: The graph state (or state-like object) for the borrower.

    Returns:
        The generated ``.docx`` file as bytes.
    """
    return render_markdown_to_docx(build_explainability_report(state))


def explainability_docx_filename(company_or_code: str) -> str:
    """Return a safe ``explainability_<name>.docx`` filename.

    Mirrors :func:`explainability_filename` (the Markdown variant) but with the
    ``.docx`` extension, using the same shared cross-OS sanitiser the DOCX / XLSX
    exporters use; falls back to ``borrower`` when the name sanitises to empty.
    """
    stem = safe_filename_stem(company_or_code or "", fallback="borrower")
    return f"explainability_{stem}.docx"


def build_explainability_pdf(state: Any) -> bytes:
    """Render the deterministic explainability report to ``.pdf`` bytes.

    The PDF path for the SAME report :func:`build_explainability_report`
    produces: it renders that exact Markdown through the shared, CJK-correct,
    number-safe :func:`render_markdown_to_pdf` renderer, so an FSA examiner can
    archive the classification basis as a searchable, layout-stable PDF.

    Pure, deterministic, and offline (given the embedded font); reads defensively
    (a live state or a rehydrated dict) since it reuses the Markdown renderer.

    Args:
        state: The graph state (or state-like object) for the borrower.

    Returns:
        The generated ``.pdf`` file as bytes.

    Raises:
        PdfFontUnavailableError: When no CJK font is available to embed (the
            renderer never emits unreadable tofu). Callers that must be
            best-effort should catch it and fall back to the DOCX export.
    """
    return render_markdown_to_pdf(build_explainability_report(state))


def explainability_pdf_filename(company_or_code: str) -> str:
    """Return a safe ``explainability_<name>.pdf`` filename.

    Mirrors :func:`explainability_filename` with the ``.pdf`` extension via the
    shared cross-OS sanitiser; falls back to ``borrower`` when empty.
    """
    stem = safe_filename_stem(company_or_code or "", fallback="borrower")
    return f"explainability_{stem}.pdf"
