"""Audit-trail panel — the Feature 7 immutable ledger, surfaced in the UI.

The Audit tab of the borrower workspace (Feature 9 §5.5). Read-only: it renders
the ordered audit events for the current ``thread_id`` plus the tamper-evidence
hash-chain verdict, all loaded in-process from the audit sink by
:meth:`~app.frontend.state.SaiseiUIState.load_audit_trail`.

Offline-safe by construction: with no ``SAISEI_AUDIT_DSN`` the sink is the no-op
NullAuditSink, so the panel shows a clean "no audit backend configured" empty
state instead of an error. Display-only — it never writes the ledger.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, RADII, SHADOW, TYPE

__all__ = ["audit_panel", "governance_panel", "loan_ledger_panel"]

#: Per-event-type accent + label for the timeline rows.
_EVENT_META: dict[str, tuple[str, str]] = {
    "classification": ("chrome", "債務者区分"),
    "guarantee_release": ("positive", "経営者保証解除"),
    "human_decision": ("warn", "担当者決定"),
}


def _chain_badge() -> rx.Component:
    """The hash-chain verdict badge (ok / broken / hidden when unknown)."""
    return rx.cond(
        SaiseiUIState.audit_chain_status != "",
        rx.badge(
            rx.cond(
                SaiseiUIState.audit_chain_status == "ok",
                rx.hstack(
                    rx.icon("shield-check", size=13),
                    rx.text("ハッシュチェーン整合 (chain intact)"),
                    align="center",
                    spacing="1",
                ),
                rx.hstack(
                    rx.icon("shield-alert", size=13),
                    rx.text("チェーン破損 (chain broken)"),
                    align="center",
                    spacing="1",
                ),
            ),
            color_scheme=rx.cond(SaiseiUIState.audit_chain_status == "ok", "grass", "red"),
            variant="soft",
            radius="full",
            size="2",
        ),
    )


def _event_row(row: rx.Var[dict[str, str]]) -> rx.Component:
    """One audit event as a timeline row."""
    accent = rx.match(
        row["event_type"],
        ("classification", COLORS["chrome"]),
        ("guarantee_release", COLORS["positive"]),
        ("human_decision", COLORS["warn"]),
        COLORS["text_faint"],
    )
    return rx.hstack(
        # Accent rail + dot.
        rx.box(
            width="8px",
            height="8px",
            min_width="8px",
            border_radius=RADII["pill"],
            background=accent,
            margin_top="6px",
        ),
        rx.vstack(
            rx.hstack(
                rx.text(
                    row["summary"],
                    style=TYPE["small"],
                    color=COLORS["text"],
                    font_weight="600",
                ),
                rx.spacer(),
                rx.badge(
                    row["actor"],
                    variant="soft",
                    color_scheme="gray",
                    radius="full",
                    size="1",
                ),
                align="center",
                width="100%",
            ),
            rx.text(
                row["created_at"],
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            spacing="1",
            align="start",
            width="100%",
        ),
        align="start",
        spacing="3",
        width="100%",
        padding="10px 12px",
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["md"],
    )


def _empty_state() -> rx.Component:
    """Shown when there are no audit events for the current thread."""
    return rx.vstack(
        rx.icon("file-clock", size=28, color=COLORS["text_faint"]),
        rx.text(
            "このスレッドの監査記録はありません。",
            style=TYPE["small"],
            color=COLORS["text_muted"],
            text_align="center",
        ),
        rx.text(
            "監査台帳は SAISEI_AUDIT_DSN 設定時に永続化されます（オフラインでは空）。 "
            "(The append-only ledger persists when SAISEI_AUDIT_DSN is set; "
            "empty offline.)",
            style=TYPE["caption"],
            color=COLORS["text_faint"],
            text_align="center",
        ),
        spacing="2",
        align="center",
        width="100%",
        padding="32px 16px",
    )


def audit_panel() -> rx.Component:
    """Render the borrower's immutable audit trail (Feature 7 / Feature 9 Audit tab).

    Read-only and offline-safe: loads via ``load_audit_trail`` (in-process sink
    read), shows the chain-verdict badge, the event timeline, or an empty state.
    The UI never writes the ledger.
    """
    return rx.vstack(
        rx.hstack(
            rx.icon("scroll-text", size=16, color=COLORS["chrome"]),
            rx.heading("監査記録 (Audit trail)", style=TYPE["h3"], color=COLORS["text"]),
            rx.spacer(),
            _chain_badge(),
            rx.button(
                rx.cond(
                    SaiseiUIState.audit_loading,
                    rx.spinner(size="1"),
                    rx.icon("refresh-cw", size=13),
                ),
                on_click=SaiseiUIState.load_audit_trail,
                variant="soft",
                color_scheme="gray",
                size="1",
            ),
            align="center",
            spacing="2",
            width="100%",
        ),
        rx.text(
            "この案件の分類・保証解除評価・担当者決定の改ざん防止記録。 "
            "(Append-only, hash-chained record of every classification, "
            "guarantee-release assessment, and human decision for this case.)",
            style=TYPE["caption"],
            color=COLORS["text_faint"],
        ),
        rx.cond(
            SaiseiUIState.audit_rows.length() > 0,
            rx.vstack(
                rx.foreach(SaiseiUIState.audit_rows, _event_row),
                spacing="2",
                width="100%",
            ),
            _empty_state(),
        ),
        padding="20px",
        background=COLORS["surface_2"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["lg"],
        box_shadow=SHADOW["sm"],
        spacing="3",
        width="100%",
        align="start",
    )


def governance_panel() -> rx.Component:
    """Render the engine-level governance documents for the examiner (Feature 7).

    The examiner-facing companion to the per-borrower audit trail: it exports the
    deterministic engine's MODEL CARD (what the engine is, the FSA classification
    cascade with live thresholds, the full governing-constants table, intended
    use + limits) and the GOVERNING-CONSTANTS CHANGE LOG (the live thresholds
    diffed against the committed, reviewed baseline).

    Both are engine-level (not borrower-specific), so this panel is always
    available regardless of whether a case has been run. Both renderers are pure,
    deterministic, and read from the live constants, so a regulator pulling these
    during an inspection always gets documents that match the running engine.
    Display-only: the buttons emit downloads; the UI computes nothing.
    """
    return rx.vstack(
        rx.hstack(
            rx.icon("book-check", size=16, color=COLORS["chrome"]),
            rx.heading("モデル統制 (Model governance)", style=TYPE["h3"], color=COLORS["text"]),
            align="center",
            spacing="2",
            width="100%",
        ),
        rx.text(
            "決定論的エンジンのモデルカードと定数変更履歴を出力します。いずれも"
            "コードの値から生成され、稼働中のエンジンと一致します。 "
            "(Export the deterministic engine's model card and constants change "
            "log. Both are generated from the live code and match the running "
            "engine.)",
            style=TYPE["caption"],
            color=COLORS["text_faint"],
        ),
        rx.hstack(
            rx.button(
                rx.icon("file-down", size=14),
                "モデルカード (Model card)",
                on_click=SaiseiUIState.download_model_card_docx,
                variant="soft",
                color_scheme="gray",
                size="1",
            ),
            rx.cond(
                SaiseiUIState.pdf_export_available,
                rx.button(
                    rx.icon("file-down", size=14),
                    "モデルカード (PDF)",
                    on_click=SaiseiUIState.download_model_card_pdf,
                    variant="soft",
                    color_scheme="gray",
                    size="1",
                ),
            ),
            rx.button(
                rx.icon("file-down", size=14),
                "定数変更履歴 (Change log)",
                on_click=SaiseiUIState.download_constants_changelog_docx,
                variant="soft",
                color_scheme="gray",
                size="1",
            ),
            rx.cond(
                SaiseiUIState.pdf_export_available,
                rx.button(
                    rx.icon("file-down", size=14),
                    "定数変更履歴 (PDF)",
                    on_click=SaiseiUIState.download_constants_changelog_pdf,
                    variant="soft",
                    color_scheme="gray",
                    size="1",
                ),
            ),
            spacing="2",
            wrap="wrap",
            width="100%",
        ),
        padding="20px",
        background=COLORS["surface_2"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["lg"],
        box_shadow=SHADOW["sm"],
        spacing="3",
        width="100%",
        align="start",
    )


def _loan_ledger_row(row: rx.Var[dict[str, str]]) -> rx.Component:
    """One durable loan-event as a timeline row (status + actor + note)."""
    distressed = (row["status_english"] == "Workout") | (row["status_english"] == "Written Off")
    accent = rx.cond(distressed, COLORS["fail"], COLORS["chrome"])
    return rx.hstack(
        rx.box(
            width="8px",
            height="8px",
            min_width="8px",
            border_radius=RADII["pill"],
            background=accent,
            margin_top="6px",
        ),
        rx.vstack(
            rx.hstack(
                rx.text(
                    row["status_kanji"] + " (" + row["status_english"] + ")",
                    style=TYPE["small"],
                    color=COLORS["text"],
                    font_weight="600",
                ),
                rx.spacer(),
                rx.badge(
                    row["actor"],
                    variant="soft",
                    color_scheme="gray",
                    radius="full",
                    size="1",
                ),
                align="center",
                width="100%",
            ),
            rx.cond(
                row["note"] != "",
                rx.text(
                    row["note"],
                    style=TYPE["caption"],
                    color=COLORS["text_muted"],
                ),
            ),
            rx.text(
                row["at"],
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            spacing="1",
            align="start",
            width="100%",
        ),
        align="start",
        spacing="3",
        width="100%",
        padding="10px 12px",
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["md"],
    )


def _loan_ledger_empty() -> rx.Component:
    """Shown when the facility has no durable loan-event ledger."""
    return rx.vstack(
        rx.icon("landmark", size=28, color=COLORS["text_faint"]),
        rx.text(
            "この案件の融資ライフサイクル記録はありません。",
            style=TYPE["small"],
            color=COLORS["text_muted"],
            text_align="center",
        ),
        rx.text(
            "融資台帳は SAISEI_LOAN_DSN 設定時に永続化されます（オフラインでは空）。 "
            "(The append-only loan ledger persists when SAISEI_LOAN_DSN is set; "
            "empty offline.)",
            style=TYPE["caption"],
            color=COLORS["text_faint"],
            text_align="center",
        ),
        spacing="2",
        align="center",
        width="100%",
        padding="32px 16px",
    )


def loan_ledger_panel() -> rx.Component:
    """Render the facility's durable loan-lifecycle ledger (Audit tab).

    The examiner companion to the audit trail: a read-only timeline of the
    facility's append-only loan-event history (申込 -> ... -> 条件変更 / 管理回収),
    loaded in-process from the durable loan store by ``load_audit_trail`` (which
    also drives the audit trail on the same tab). Offline-safe: with no
    ``SAISEI_LOAN_DSN`` the store is the no-op NullLoanStore, so the panel shows
    a clean empty state. Display-only -- it never writes the ledger.
    """
    return rx.vstack(
        rx.hstack(
            rx.icon("landmark", size=16, color=COLORS["chrome"]),
            rx.heading("融資台帳 (Loan ledger)", style=TYPE["h3"], color=COLORS["text"]),
            rx.spacer(),
            rx.button(
                rx.cond(
                    SaiseiUIState.loan_ledger_loading,
                    rx.spinner(size="1"),
                    rx.icon("refresh-cw", size=13),
                ),
                on_click=SaiseiUIState.load_audit_trail,
                variant="soft",
                color_scheme="gray",
                size="1",
            ),
            align="center",
            spacing="2",
            width="100%",
        ),
        rx.text(
            "この融資案件の改ざん防止・追記専用のライフサイクル記録。 "
            "(Append-only, tamper-evident record of this facility's "
            "loan-lifecycle transitions.)",
            style=TYPE["caption"],
            color=COLORS["text_faint"],
        ),
        rx.cond(
            SaiseiUIState.loan_ledger_rows.length() > 0,
            rx.vstack(
                rx.foreach(SaiseiUIState.loan_ledger_rows, _loan_ledger_row),
                spacing="2",
                width="100%",
            ),
            _loan_ledger_empty(),
        ),
        padding="20px",
        background=COLORS["surface_2"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["lg"],
        box_shadow=SHADOW["sm"],
        spacing="3",
        width="100%",
        align="start",
    )
