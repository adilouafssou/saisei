"""試算表 (Shisanhyo) upload dropzone component.

Provides a drag-and-drop / click-to-browse file upload widget for Excel (.xlsx)
and CSV trial-balance files.  On upload the file is parsed by the deterministic
:func:`~app.backend.tools.shisanhyo_parser.parse_shisanhyo` parser (via a
:class:`~app.frontend.state.SaiseiUIState` background event) and the proposed
rows are shown for **banker confirmation** before they enter the pipeline.

Design principles
-----------------
- **Display-only**: the component never computes a figure.  It shows what the
  parser returned and lets the banker decide.
- **Reuses the case-file column style**: uses :data:`~app.frontend.theme.TABLE_STYLE`
  and the same token set as the existing
  :func:`~app.frontend.components.shisanhyo_table.shisanhyo_table`.
- **Confirm / Cancel**: Confirm copies the proposed rows into ``state.shisanhyo``
  and triggers the normal assessment run; Cancel discards without any state change.
- **Warnings callout**: any parser warnings (bad cells, J-GAAP invariant
  violations, etc.) are shown in an amber callout so the banker can make an
  informed decision.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, RADII, SHADOW, TABLE_STYLE, TYPE

__all__ = ["shisanhyo_upload", "shisanhyo_upload_dialog"]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _editable_cell(index: rx.Var[int], field: str, value: rx.Var[str]) -> rx.Component:
    """Render one editable numeric cell bound to a staged row field.

    The banker can correct a misread figure in place; the change writes back to
    the staged rows via :meth:`SaiseiUIState.edit_upload_cell` and triggers live
    re-validation. Display-only correction — nothing commits until confirm.
    """
    return rx.table.cell(
        rx.input(
            default_value=value,
            on_blur=lambda v: SaiseiUIState.edit_upload_cell(index, field, v),
            size="1",
            variant="surface",
            width="130px",
            text_align="right",
            style={"fontFamily": "var(--mono, monospace)"},
        ),
    )


def _preview_row(row: rx.Var[dict[str, str]]) -> rx.Component:
    """Render one editable proposed trial-balance row in the preview table.

    The period stays read-only (an identity, not a figure); the three money
    cells are editable inputs so the banker can correct a misread value before
    confirming. A per-row validation message is shown when the edited row breaks
    the gross-profit identity or is not a non-negative integer.
    """
    index = row["index"].to(int)
    error = SaiseiUIState.upload_row_errors[index]
    return rx.table.row(
        rx.table.row_header_cell(
            rx.cond(
                SaiseiUIState.upload_is_guided,
                rx.input(
                    default_value=row["period"],
                    on_blur=lambda v: SaiseiUIState.edit_upload_cell(index, "period", v),
                    size="1",
                    variant="surface",
                    width="120px",
                    placeholder="YYYY-MM",
                ),
                rx.text(row["period"]),
            ),
        ),
        _editable_cell(index, "uriage", row["uriage_raw"]),
        _editable_cell(index, "uriage_genka", row["uriage_genka_raw"]),
        rx.table.cell(
            rx.vstack(
                rx.text(row["keijo_rieki"], style=TYPE["small"], color=COLORS["text"]),
                rx.cond(
                    error != "",
                    rx.text(error, style=TYPE["caption"], color=COLORS["fail"]),
                ),
                spacing="1",
                align="end",
            ),
        ),
    )


def _warning_item(warning: rx.Var[str]) -> rx.Component:
    """Render one parser warning as a text row."""
    return rx.text(
        "• " + warning,
        style=TYPE["small"],
        color=COLORS["warn"],
        width="100%",
    )


#: The named upload component id. Used both by ``rx.upload`` and by
#: ``rx.upload_files`` / ``rx.selected_files`` so they target the same control.
_UPLOAD_ID = "shisanhyo_upload"


def _dropzone_area() -> rx.Component:
    """The drag-and-drop upload area (shown when no preview is pending).

    ``rx.upload`` renders a ``div`` and has NO ``on_upload`` event trigger;
    wiring one raises "the div does not take in an on_upload". Reflex uploads are
    a two-step flow: the dropzone only *selects* files into the named upload
    component, then a submit button hands them to the background handler via
    :func:`rx.upload_files`. The selected filename(s) are shown so the banker
    can confirm what will be parsed before submitting.
    """
    return rx.vstack(
        rx.upload(
            rx.vstack(
                rx.cond(
                    SaiseiUIState.upload_processing,
                    rx.spinner(size="3", color=COLORS["brand"]),
                    rx.icon("upload-cloud", size=32, color=COLORS["text_faint"]),
                ),
                rx.text(
                    rx.cond(
                        SaiseiUIState.upload_processing,
                        "解析中… (Parsing…)",
                        "試算表をドロップ / クリックして選択",
                    ),
                    style=TYPE["body"],
                    color=COLORS["text_muted"],
                    text_align="center",
                ),
                rx.text(
                    ".xlsx または .csv (Excel / CSV trial balance)",
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                    text_align="center",
                ),
                spacing="3",
                align="center",
                padding="16px",
            ),
            id=_UPLOAD_ID,
            accept={
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
                "text/csv": [".csv"],
                "text/plain": [".csv"],
            },
            max_files=1,
            border=f"2px dashed {COLORS['border']}",
            border_radius=RADII["lg"],
            padding="32px 24px",
            background=COLORS["surface"],
            width="100%",
            cursor="pointer",
            _hover={"borderColor": COLORS["brand"], "background": COLORS["brand_soft"]},
        ),
        # Selected filename(s) preview (before submit).
        rx.cond(
            rx.selected_files(_UPLOAD_ID).length() > 0,
            rx.hstack(
                rx.icon("file-check", size=14, color=COLORS["brand"]),
                rx.text(
                    rx.selected_files(_UPLOAD_ID).join(", "),
                    style=TYPE["small"],
                    color=COLORS["text_muted"],
                ),
                align="center",
                spacing="2",
                width="100%",
            ),
        ),
        # Submit button: hands the selected files to the background handler.
        rx.button(
            rx.cond(
                SaiseiUIState.upload_processing,
                rx.spinner(size="2"),
                rx.icon("upload", size=14),
            ),
            "解析 (Parse)",
            on_click=SaiseiUIState.handle_upload_and_stage(rx.upload_files(upload_id=_UPLOAD_ID)),
            disabled=(
                (rx.selected_files(_UPLOAD_ID).length() == 0) | SaiseiUIState.upload_processing
            ),
            color_scheme="grass",
            size="2",
        ),
        # Guided manual entry (Feature 8 channel 4): the no-data fallback when
        # there is neither a core-banking record nor a file to upload. Seeds
        # blank rows into the SAME staging/confirm flow the parser feeds.
        rx.hstack(
            rx.divider(),
            rx.text("または (or)", style=TYPE["caption"], color=COLORS["text_faint"]),
            rx.divider(),
            align="center",
            spacing="2",
            width="100%",
        ),
        rx.button(
            rx.icon("keyboard", size=14),
            "手入力で作成 (Enter figures manually)",
            on_click=SaiseiUIState.start_guided_entry(12),
            disabled=SaiseiUIState.upload_processing,
            variant="soft",
            color_scheme="gray",
            size="2",
        ),
        spacing="3",
        align="center",
        width="100%",
    )


def _preview_panel() -> rx.Component:
    """The proposed-rows preview + Confirm/Cancel (shown after a successful parse)."""
    return rx.vstack(
        # Warnings callout (shown only when there are warnings).
        rx.cond(
            SaiseiUIState.upload_warnings.length() > 0,
            rx.callout(
                rx.vstack(
                    rx.text(
                        "解析警告 (Parser warnings) — 確認してから承認してください:",
                        style=TYPE["small"],
                        color=COLORS["warn"],
                        font_weight="600",
                    ),
                    rx.vstack(
                        rx.foreach(SaiseiUIState.upload_warnings, _warning_item),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                color_scheme="amber",
                width="100%",
            ),
        ),
        # Preview table.
        rx.cond(
            SaiseiUIState.upload_preview_rows.length() > 0,
            rx.vstack(
                rx.hstack(
                    rx.icon("table", size=14, color=COLORS["brand"]),
                    rx.text(
                        rx.cond(
                            SaiseiUIState.upload_is_guided,
                            "試算表を手入力 (Enter trial balance figures)",
                            "提案された試算表 (Proposed trial balance rows)",
                        ),
                        style=TYPE["small"],
                        color=COLORS["text_muted"],
                        font_weight="600",
                    ),
                    align="center",
                    spacing="2",
                ),
                rx.table.root(
                    rx.table.header(
                        rx.table.row(
                            rx.table.column_header_cell("期間 (Period)"),
                            rx.table.column_header_cell("売上 (Uriage)"),
                            rx.table.column_header_cell("売上原価 (Genka)"),
                            rx.table.column_header_cell("経常利益 (Keijo)"),
                        )
                    ),
                    rx.table.body(rx.foreach(SaiseiUIState.upload_preview_rows, _preview_row)),
                    variant="surface",
                    size="1",
                    width="100%",
                    style=TABLE_STYLE,
                ),
                spacing="2",
                width="100%",
            ),
            # No rows parsed but warnings exist — show a message.
            rx.cond(
                SaiseiUIState.upload_warnings.length() > 0,
                rx.text(
                    "有効な行が解析されませんでした。 (No valid rows were parsed.)",
                    style=TYPE["small"],
                    color=COLORS["text_muted"],
                ),
            ),
        ),
        # Confirm / Cancel buttons.
        rx.hstack(
            rx.button(
                rx.icon("check", size=14),
                "確認・診断実行 (Confirm & Run)",
                on_click=SaiseiUIState.confirm_upload,
                disabled=(~SaiseiUIState.upload_is_valid | SaiseiUIState.is_running),
                color_scheme="grass",
                size="2",
            ),
            rx.button(
                rx.icon("x", size=14),
                "キャンセル (Cancel)",
                on_click=SaiseiUIState.cancel_upload,
                variant="soft",
                color_scheme="gray",
                size="2",
            ),
            spacing="3",
            align="center",
            width="100%",
        ),
        spacing="4",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Public component
# ---------------------------------------------------------------------------


def shisanhyo_upload_dialog() -> rx.Component:
    """Render the upload flow inside a button-triggered dialog.

    A compact top-bar / case-file button opens a modal containing the full
    dropzone → parse → editable-preview → confirm/cancel flow, so the upload UI
    is on-demand instead of always occupying the case-file column. Cancel closes
    the dialog and discards staging; Confirm runs the assessment and closes it.

    Display-only: the component never computes a figure.
    """
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(
                rx.icon("upload", size=16),
                "試算表をアップロード (Upload)",
                variant="soft",
                color_scheme="gray",
                size="2",
            ),
        ),
        rx.dialog.content(
            rx.dialog.title("試算表アップロード (Upload Trial Balance)"),
            rx.dialog.description(
                "Excel (.xlsx) または CSV をアップロードするか、ファイルがなければ"
                "手入力で作成し、必要なら修正してから確認してください。",
                style=TYPE["small"],
                color=COLORS["text_muted"],
                margin_bottom="12px",
            ),
            rx.cond(
                SaiseiUIState.upload_has_preview,
                _preview_panel(),
                _dropzone_area(),
            ),
            rx.flex(
                rx.dialog.close(
                    rx.button(
                        "閉じる (Close)",
                        on_click=SaiseiUIState.cancel_upload,
                        variant="soft",
                        color_scheme="gray",
                        size="2",
                    ),
                ),
                justify="end",
                margin_top="16px",
            ),
            max_width="640px",
            style={"background": COLORS["surface"]},
        ),
    )


def shisanhyo_upload() -> rx.Component:
    """Render the trial-balance upload dropzone with confirm/cancel flow.

    Shows the dropzone when no upload is pending; switches to the preview panel
    (proposed rows + warnings + Confirm/Cancel) once a file has been parsed.

    The component is display-only: it never computes a figure.  All values
    shown are read from :class:`~app.frontend.state.SaiseiUIState` fields
    populated by the deterministic parser.
    """
    return rx.vstack(
        rx.hstack(
            rx.icon("file-spreadsheet", size=16, color=COLORS["chrome"]),
            rx.heading(
                "試算表アップロード (Upload Trial Balance)",
                style=TYPE["h3"],
                color=COLORS["text"],
            ),
            align="center",
            spacing="2",
        ),
        rx.text(
            "Excel (.xlsx) または CSV の試算表をアップロードして、"
            "担当者が確認後に診断を実行します。",
            style=TYPE["small"],
            color=COLORS["text_muted"],
        ),
        # Show dropzone OR preview panel, never both.
        rx.cond(
            (SaiseiUIState.upload_preview_rows.length() > 0)
            | (SaiseiUIState.upload_warnings.length() > 0)
            | SaiseiUIState.upload_processing,
            _preview_panel(),
            _dropzone_area(),
        ),
        padding="20px",
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["lg"],
        box_shadow=SHADOW["sm"],
        spacing="4",
        width="100%",
    )
