"""Deterministic model card + governing-constants change log (Feature 7).

The “model” in production today is the DETERMINISTIC spine: the EWS score, the
five-category FSA classification cascade, the guarantee-release (保証解除) pillars,
and the advisory feasibility floor. There is no trained model in the decision
path — every figure is rule-based and auditable to the yen. The regulator-facing
“model card” for such a system is therefore a faithful description of THAT logic
and the exact thresholds that govern it.

This module renders that card from the LIVE constants
(:mod:`app.shared.constants`) and the LIVE classification enum
(:class:`app.shared.models.classification.FsaClass`), so the card can never
drift from the running engine: change a threshold and the card changes with it.

It also provides a deterministic CHANGE LOG primitive: given the current
governing constants and a previously-recorded baseline, it renders exactly which
thresholds changed (old → new) — the auditable record an examiner needs when a
bank tunes a threshold. (Recording the baseline at rest is a deployment concern;
this module supplies the pure renderer.)

It mirrors the other export renderers (``keikakusho_docx`` / ``recovery_xlsx`` /
``explainability_report``):

- **Pure + deterministic + offline.** It reads constants and formats them. No
  LLM, no network, no clock. Same inputs -> byte-identical output.
- **Single source of truth.** Every threshold is read from ``constants`` at call
  time, never re-typed, so the card and the engine agree by construction.
- Uses the shared :func:`safe_filename_stem` download-name contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.backend.export._filenames import safe_filename_stem
from app.backend.export._markdown_docx import render_markdown_to_docx
from app.backend.export._markdown_pdf import render_markdown_to_pdf
from app.shared import constants as C
from app.shared.models.classification import FsaClass

__all__ = [
    "governing_constants",
    "build_model_card",
    "build_model_card_docx",
    "build_model_card_pdf",
    "build_constants_changelog",
    "build_constants_changelog_docx",
    "build_constants_changelog_pdf",
    "model_card_filename",
    "model_card_docx_filename",
    "model_card_pdf_filename",
    "constants_changelog_docx_filename",
    "constants_changelog_pdf_filename",
    "CONSTANTS_BASELINE_PATH",
    "load_constants_baseline",
    "detect_constants_drift",
]

#: Path to the committed governing-constants baseline (the last REVIEWED set of
#: decision thresholds). The drift guard (tests/test_constants_governance.py)
#: fails when the live constants differ from this file, so a regulated decision
#: threshold can never change without the baseline being consciously updated in
#: the same MR — making the change reviewable and recorded by construction.
CONSTANTS_BASELINE_PATH = Path(__file__).with_name("governing_constants_baseline.json")

#: The model-card schema version. Bump when the CARD STRUCTURE changes (new
#: sections / different layout) — NOT when a threshold value changes (those are
#: read live from ``constants`` and tracked by the change log instead).
MODEL_CARD_VERSION = "1.0"


def governing_constants() -> dict[str, Any]:
    """Return the deterministic decision thresholds that govern the engine.

    The single, ordered set of constants the model card documents and the change
    log diffs. Read live from :mod:`app.shared.constants` so this dict is always
    the engine's actual configuration — never a hand-copied snapshot. Keys are
    the constant names; values are their current values.

    Returns:
        An ordered mapping of constant name -> current value.
    """
    return {
        # EWS / FSA classification bands.
        "EWS_SUBSTANDARD": C.EWS_SUBSTANDARD,
        "EWS_DOUBTFUL": C.EWS_DOUBTFUL,
        "EWS_DANGER": C.EWS_DANGER,
        "TDB_NORMAL_FLOOR": C.TDB_NORMAL_FLOOR,
        # Guarantee-release (保証解除) pillar weights + eligibility.
        "HOSHO_WEIGHT_BUNRI": C.HOSHO_WEIGHT_BUNRI,
        "HOSHO_WEIGHT_ZAIMU": C.HOSHO_WEIGHT_ZAIMU,
        "HOSHO_WEIGHT_KAIJI": C.HOSHO_WEIGHT_KAIJI,
        "HOSHO_ELIGIBLE_SCORE": C.HOSHO_ELIGIBLE_SCORE,
        "HOSHO_SUCCESSION_EWS_MAX": C.HOSHO_SUCCESSION_EWS_MAX,
        "HOSHO_SUCCESSION_TDB_MIN": C.HOSHO_SUCCESSION_TDB_MIN,
        # Advisory feasibility floor (never gates; documented for transparency).
        "FEASIBILITY_WEIGHT_UPLIFT": C.FEASIBILITY_WEIGHT_UPLIFT,
        "FEASIBILITY_WEIGHT_WC": C.FEASIBILITY_WEIGHT_WC,
        "FEASIBILITY_WEIGHT_RATE": C.FEASIBILITY_WEIGHT_RATE,
        "FEASIBILITY_WEIGHT_SETTLE": C.FEASIBILITY_WEIGHT_SETTLE,
        "FEASIBILITY_INDUSTRY_UPLIFT_FACTOR": C.FEASIBILITY_INDUSTRY_UPLIFT_FACTOR,
        "FEASIBILITY_HIGH_FLOOR": C.FEASIBILITY_HIGH_FLOOR,
        "FEASIBILITY_MEDIUM_FLOOR": C.FEASIBILITY_MEDIUM_FLOOR,
        # Reconciliation (advisory-to-HITL) calibration placeholders.
        "RECONCILIATION_BAND_DISTANCE": C.RECONCILIATION_BAND_DISTANCE,
        "MAX_RECONCILIATION_TRIGGERS": C.MAX_RECONCILIATION_TRIGGERS,
        # Workflow / financing assumptions.
        "MAX_REVISION_CYCLES": C.MAX_REVISION_CYCLES,
        "PRO_RATA_TOLERANCE": C.PRO_RATA_TOLERANCE,
        "MIN_RECOVERY_HORIZON_YEARS": C.MIN_RECOVERY_HORIZON_YEARS,
        "WORKING_CAPITAL_FINANCING_RATE": C.WORKING_CAPITAL_FINANCING_RATE,
    }


def _fmt(value: Any) -> str:
    """Format a constant value compactly (int when whole, else as-is)."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _classification_cascade_lines() -> list[str]:
    """Render the five-category FSA classification cascade (most severe first).

    Describes the EXACT decision order in
    :func:`app.backend.nodes.ews_scoring.classify`, with the live thresholds
    inlined. The kanji labels come from :class:`FsaClass` so the card and the
    engine's labels can never diverge.
    """
    return [
        "## 分類ロジック（Classification cascade）",
        "",
        "債務者区分は以下の順序（重い順）で決定されます。 "
        "(The FSA debtor class is decided in this order; the first match wins.)",
        "",
        f"1. **{FsaClass.HATANSAKI.kanji}** (Bankrupt): "
        "破綻認定（is_insolvent）かつ純資産マイナス（net_worth < 0）。",
        f"2. **{FsaClass.JISSHITSU_HATANSAKI.kanji}** (De facto bankrupt): "
        f"破綻認定 OR 純資産マイナス OR EWS ≥ {_fmt(C.EWS_DANGER)}。",
        f"3. **{FsaClass.HATAN_KENENSAKI.kanji}** (In danger): "
        f"EWS ≥ {_fmt(C.EWS_DOUBTFUL)} OR （資金繰り不足 AND EWS ≥ {_fmt(C.EWS_SUBSTANDARD)}）。",
        f"4. **{FsaClass.YOCHUISAKI.kanji}** (Needs attention): "
        f"EWS ≥ {_fmt(C.EWS_SUBSTANDARD)} OR 資金繰り不足 OR "
        f"TDBスコア < {_fmt(C.TDB_NORMAL_FLOOR)}。"
        "資金繰り不足を伴う場合は要管理先（special attention）。",
        f"5. **{FsaClass.SEIJOSAKI.kanji}** (Normal): 上記のいずれにも該当しない場合。",
        "",
    ]


def _constants_table_lines() -> list[str]:
    """Render the governing-constants table (name + live value)."""
    lines = [
        "## 決定閾値（Governing thresholds）",
        "",
        "本表の値はコード（``app/shared/constants.py``）から直接読み出しています。 "
        "(Read live from the engine constants — the card cannot drift from the code.)",
        "",
        "| 定数 (Constant) | 値 (Value) |",
        "| --- | ---: |",
    ]
    for name, value in governing_constants().items():
        lines.append(f"| `{name}` | {_fmt(value)} |")
    lines.append("")
    return lines


def build_model_card() -> str:
    """Assemble the deterministic engine model card as Markdown.

    Documents, from the LIVE engine configuration:
      1. what the “model” is (a deterministic, rule-based spine — no trained
         model in the decision path) and its intended use;
      2. the exact five-category FSA classification cascade with live thresholds;
      3. the full governing-constants table (read live from ``constants``);
      4. the data inputs, the human-authority contract, and the known limits.

    Pure and offline: every value is read from ``app.shared.constants`` /
    ``FsaClass`` at call time, so the card always matches the running engine.

    Returns:
        A Markdown model-card string.
    """
    lines: list[str] = [
        "# モデルカード（Model card — Saisei 再生エンジン）",
        "",
        f"- **カード版（Card version）:** {MODEL_CARD_VERSION}",
        "- **種別（Model type）:** 決定論的ルールベースエンジン "
        "(deterministic, rule-based — NO trained model in the decision path).",
        "",
        "## 用途（Intended use）",
        "",
        "中小企業の早期警戒（EWS）スコア算出、金融庁検査マニュアルに準拠した債務者区分、"
        "経営者保証解除の評価、および再生計画の策定支援。 "
        "(EWS scoring, FSA debtor classification, guarantee-release assessment, "
        "and turnaround-plan support for distressed SMEs.)",
        "",
        "すべての数値は決定論的に算出され、監査可能です。LLMは文章の推敲と"
        "参考情報の提示に限定され、いかなる数値・区分・ゲート・経路にも関与しません。 "
        "(Every figure is deterministic and auditable. The LLM only assists with "
        "language and advisory context — never a number, class, gate, or route.)",
        "",
        *_classification_cascade_lines(),
        *_constants_table_lines(),
        "## 入力データ（Inputs）",
        "",
        "- 月次試算表（Shisanhyo / TrialBalance）: 売上・売上原価・販売費・営業外損益。",
        "- 企業プロファイル（TDB CompanyProfile）とTDB信用スコア。",
        "- マクロ指標（BOJ政策金利カーブ・決済指標）。",
        "- 保証・破綻シグナル（net_worth / is_insolvent）。",
        "",
        "## 人間の権限（Human authority）",
        "",
        "システムは提案し、担当者（銀行）が承認します。修正サイクルは最大 "
        f"{_fmt(C.MAX_REVISION_CYCLES)} 回で、すべての決定は不変の監査ログに記録されます。 "
        "(The system proposes; the banker approves. Every decision is recorded in "
        "the immutable, hash-chained audit ledger.)",
        "",
        "## 限界・注意（Limitations）",
        "",
        "- 閾値は手動設定の出発点であり、実データによる校正が今後の課題です"
        "（RECONCILIATION_BAND_DISTANCE / MAX_RECONCILIATION_TRIGGERS は校正待ち）。",
        "- 実現可能性フロアと参考事例（RAG）は参考情報（advisory）であり、"
        "区分・スコア・ゲート・経路には使用されません。",
        "- 反社会的勢力チェック等の公平性・バイアスレビューは人間が上書き可能であるべきです。",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def build_constants_changelog(
    current: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
) -> str:
    """Render the governing-constants change log (current vs. a baseline).

    The auditable record an examiner needs when a bank tunes a threshold: a
    deterministic diff of the governing constants against a previously-recorded
    baseline, showing every added / removed / changed constant (old → new).

    Pure: it compares two mappings and formats the differences. Recording the
    baseline at rest is a deployment concern; this is the renderer.

    Args:
        current: The current governing constants. Defaults to the live
            :func:`governing_constants` when omitted.
        previous: The baseline governing constants to diff against. When ``None``
            or empty, the log reports that no baseline exists (first issuance).

    Returns:
        A Markdown change-log string.
    """
    current = governing_constants() if current is None else current
    previous = previous or {}

    lines: list[str] = [
        "# 定数変更履歴（Governing-constants change log）",
        "",
    ]

    if not previous:
        lines += [
            "ベースライン（前回記録）がないため、初回発行として現在の値を記録します。 "
            "(No baseline recorded — first issuance; current values logged.)",
            "",
            "| 定数 (Constant) | 値 (Value) |",
            "| --- | ---: |",
        ]
        for name, value in current.items():
            lines.append(f"| `{name}` | {_fmt(value)} |")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    added = [k for k in current if k not in previous]
    removed = [k for k in previous if k not in current]
    changed = [k for k in current if k in previous and current[k] != previous[k]]

    if not (added or removed or changed):
        lines += [
            "変更はありません（ベースラインと一致）。 (No changes — identical to the baseline.)",
            "",
        ]
        return "\n".join(lines).rstrip() + "\n"

    if changed:
        lines += [
            "## 変更された定数（Changed）",
            "",
            "| 定数 (Constant) | 旧 (Old) | 新 (New) |",
            "| --- | ---: | ---: |",
        ]
        for name in changed:
            lines.append(f"| `{name}` | {_fmt(previous[name])} | {_fmt(current[name])} |")
        lines.append("")
    if added:
        lines += [
            "## 追加された定数（Added）",
            "",
            "| 定数 (Constant) | 値 (Value) |",
            "| --- | ---: |",
        ]
        for name in added:
            lines.append(f"| `{name}` | {_fmt(current[name])} |")
        lines.append("")
    if removed:
        lines += [
            "## 削除された定数（Removed）",
            "",
            "| 定数 (Constant) | 旧値 (Old) |",
            "| --- | ---: |",
        ]
        for name in removed:
            lines.append(f"| `{name}` | {_fmt(previous[name])} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def model_card_filename(stem: str = "saisei_engine") -> str:
    """Return a safe ``model_card_<stem>.md`` filename.

    Sanitises ``stem`` for cross-OS downloads via the shared helper the other
    exporters use; falls back to ``engine`` when it sanitises to empty.
    """
    safe = safe_filename_stem(stem or "", fallback="engine")
    return f"model_card_{safe}.md"


def build_model_card_docx() -> bytes:
    """Render the deterministic engine model card to ``.docx`` bytes.

    The Word path for the SAME card :func:`build_model_card` produces, rendered
    through the shared, number-safe :func:`render_markdown_to_docx` walker (the
    converter the Keikakusho / explainability DOCX exporters use), so a regulator
    pulling the card during an inspection can receive it as an editable Word
    document. Pure, deterministic, offline; every threshold is carried verbatim
    from the live constants.

    Returns:
        The generated ``.docx`` file as bytes.
    """
    return render_markdown_to_docx(build_model_card())


def build_constants_changelog_docx(
    current: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
) -> bytes:
    """Render the governing-constants change log to ``.docx`` bytes.

    The Word path for :func:`build_constants_changelog`: the auditable record of
    what changed (old -> new) when a bank tunes a threshold, as an editable Word
    document. Pure and deterministic; reuses the shared number-safe renderer so
    every old/new value is carried verbatim.

    Args:
        current: The current governing constants (defaults to the live set).
        previous: The baseline to diff against (``None``/empty -> first issuance).

    Returns:
        The generated ``.docx`` file as bytes.
    """
    return render_markdown_to_docx(build_constants_changelog(current=current, previous=previous))


def model_card_docx_filename(stem: str = "saisei_engine") -> str:
    """Return a safe ``model_card_<stem>.docx`` filename.

    Mirrors :func:`model_card_filename` with a ``.docx`` extension via the shared
    cross-OS sanitiser; falls back to ``engine`` when ``stem`` sanitises to empty.
    """
    safe = safe_filename_stem(stem or "", fallback="engine")
    return f"model_card_{safe}.docx"


def constants_changelog_docx_filename() -> str:
    """Return the fixed ``governing_constants_changelog.docx`` filename."""
    return "governing_constants_changelog.docx"


def build_model_card_pdf() -> bytes:
    """Render the deterministic engine model card to ``.pdf`` bytes.

    The PDF path for :func:`build_model_card`, via the shared CJK-correct,
    number-safe :func:`render_markdown_to_pdf` renderer, so a regulator can
    archive the card (engine type, FSA cascade with live thresholds, the full
    governing-constants table, intended use + limits) as a searchable PDF. Pure,
    deterministic, offline (given the embedded font); every live threshold is
    carried verbatim.

    Raises:
        PdfFontUnavailableError: When no CJK font is available to embed.
    """
    return render_markdown_to_pdf(build_model_card())


def build_constants_changelog_pdf(
    current: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
) -> bytes:
    """Render the governing-constants change log to ``.pdf`` bytes.

    The PDF path for :func:`build_constants_changelog`: the auditable old -> new
    threshold diff as a searchable, layout-stable PDF an examiner can archive.
    Pure and deterministic; every value is carried verbatim.

    Args:
        current: The current governing constants (defaults to the live set).
        previous: The baseline to diff against (``None``/empty -> first issuance).

    Raises:
        PdfFontUnavailableError: When no CJK font is available to embed.
    """
    return render_markdown_to_pdf(build_constants_changelog(current=current, previous=previous))


def model_card_pdf_filename(stem: str = "saisei_engine") -> str:
    """Return a safe ``model_card_<stem>.pdf`` filename.

    Mirrors :func:`model_card_filename` with a ``.pdf`` extension; falls back to
    ``engine`` when ``stem`` sanitises to empty.
    """
    safe = safe_filename_stem(stem or "", fallback="engine")
    return f"model_card_{safe}.pdf"


def constants_changelog_pdf_filename() -> str:
    """Return the fixed ``governing_constants_changelog.pdf`` filename."""
    return "governing_constants_changelog.pdf"


# ---------------------------------------------------------------------------
# Governance: drift guard against a committed, reviewed baseline.
#
# A regulated decision threshold changing silently is the most dangerous failure
# mode for this product. The committed baseline + the drift guard close that
# risk structurally: the live constants must equal the last reviewed baseline,
# so any change forces a conscious baseline update in the SAME merge request,
# where it is reviewed and recorded (the change log renders the exact diff).
# ---------------------------------------------------------------------------


def load_constants_baseline() -> dict[str, Any]:
    """Load the committed governing-constants baseline (the last reviewed set).

    Returns:
        The baseline mapping of constant name -> value. Returns ``{}`` when the
        baseline file is absent (treated as “no baseline yet” / first issuance).

    Raises:
        ValueError: When the file exists but is not valid JSON, so a corrupt
            baseline fails loudly rather than silently disabling the guard.
    """
    if not CONSTANTS_BASELINE_PATH.exists():
        return {}
    try:
        data = json.loads(CONSTANTS_BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Corrupt governing-constants baseline at {CONSTANTS_BASELINE_PATH}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"Governing-constants baseline must be a JSON object, got {type(data).__name__}"
        )
    return data


def detect_constants_drift(
    current: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare the live governing constants against the committed baseline.

    Pure comparison — the building block of the CI drift guard. Reports every
    constant that was added, removed, or changed (with old/new values) versus the
    baseline.

    Args:
        current: The current governing constants. Defaults to the live
            :func:`governing_constants` when omitted.
        baseline: The baseline to compare against. Defaults to the committed
            :func:`load_constants_baseline` when omitted.

    Returns:
        A mapping with keys ``added`` (list of names only in current),
        ``removed`` (list of names only in baseline), ``changed`` (mapping name
        -> ``{"old": ..., "new": ...}``), and ``drifted`` (bool; True when any of
        the three is non-empty).
    """
    current = governing_constants() if current is None else current
    baseline = load_constants_baseline() if baseline is None else baseline

    added = sorted(k for k in current if k not in baseline)
    removed = sorted(k for k in baseline if k not in current)
    changed = {
        k: {"old": baseline[k], "new": current[k]}
        for k in current
        if k in baseline and current[k] != baseline[k]
    }
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "drifted": bool(added or removed or changed),
    }
