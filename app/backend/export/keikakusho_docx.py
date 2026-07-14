"""Deterministic Keikakusho → DOCX (Word) exporter.

Japanese banks exchange the 経営改善計画書 as PDF or Word, not Markdown. This
module turns the deterministic ``keikakusho_draft`` Markdown (the source of
truth produced by ``render_keikakusho`` and already passed through the polish
numeric-preservation gate) into an editable ``.docx`` a banker can annotate
before submitting.

The Markdown → DOCX conversion itself lives in the shared, number-safe
:func:`app.backend.export._markdown_docx.render_markdown_to_docx` renderer (also
used by the explainability-report DOCX exporter), so the converter — and its
numeric-preservation invariant — is defined exactly once. This module keeps the
Keikakusho-specific entry point and filename helper.

Numeric-preservation invariant
------------------------------
The one inviolable project rule is that no step may add, drop, or alter a
number. The shared renderer upholds it structurally: it walks the draft
**line-by-line** and copies each line's text **verbatim** into a Word paragraph
(or table cell), only mapping Markdown structure (heading levels, list items,
tables) to Word styling. It never parses, reformats, or re-renders a figure —
the bytes of every yen value are carried across unchanged.

The function is pure and deterministic: same draft in → same ``.docx`` bytes
out (no network, no LLM). ``python-docx`` is the only dependency.
"""

from __future__ import annotations

from pathlib import Path

from app.backend.export._filenames import safe_filename_stem
from app.backend.export._markdown_docx import render_markdown_to_docx
from app.backend.export.keikakusho_template import (
    build_keikakusho_docx_from_template,
)

__all__ = [
    "build_keikakusho_docx",
    "build_keikakusho_docx_for_settings",
    "docx_filename",
]


def build_keikakusho_docx(draft_markdown: str, template_bytes: bytes | None = None) -> bytes:
    """Build a ``.docx`` from the deterministic Keikakusho Markdown draft.

    Two modes, both number-safe (the body always goes through the shared
    verbatim renderer, so no figure is ever reformatted):

    * **Default** (``template_bytes`` is None): emit a bare document via
      :func:`render_markdown_to_docx` — the original behaviour, unchanged.
    * **Bank template** (``template_bytes`` given): inject the body into the
      bank's house ``.docx`` at its ``{{KEIKAKUSHO_BODY}}`` placeholder via
      :func:`build_keikakusho_docx_from_template`, preserving the bank's cover
      page, styles, and headers/footers.

    Markdown tables (a header row, a ``| --- |`` delimiter, then body rows) are
    rendered as real Word tables with every cell copied verbatim; the delimiter
    row is layout and is dropped.

    Args:
        draft_markdown: The Keikakusho draft Markdown (the source of truth).
        template_bytes: Optional bank house-template ``.docx`` bytes.

    Returns:
        The generated ``.docx`` file as bytes.
    """
    if template_bytes is not None:
        return build_keikakusho_docx_from_template(draft_markdown, template_bytes)
    return render_markdown_to_docx(draft_markdown)


def build_keikakusho_docx_for_settings(draft_markdown: str, settings: object) -> bytes:
    """Build the Keikakusho ``.docx`` honouring a configured bank template.

    Reads ``settings.keikakusho_docx_template`` (a filesystem path). When set and
    the file exists, the body is injected into that bank template; otherwise the
    default bare document is produced. Offline-safe and deterministic: an empty
    or missing template path simply yields the default export, never an error,
    so ``make verify`` / demo runs are unaffected.

    Args:
        draft_markdown: The Keikakusho draft Markdown (the source of truth).
        settings: A settings-like object exposing ``keikakusho_docx_template``.

    Returns:
        The generated ``.docx`` file as bytes.
    """
    template_path = (getattr(settings, "keikakusho_docx_template", "") or "").strip()
    if template_path:
        path = Path(template_path)
        if path.is_file():
            return build_keikakusho_docx(draft_markdown, path.read_bytes())
    return build_keikakusho_docx(draft_markdown)


def docx_filename(company_or_code: str) -> str:
    """Return a safe ``keikakusho_<name>.docx`` filename.

    Sanitises the name so the generated download works across operating systems:
    path separators and whitespace collapse to ``_``, and characters that are
    illegal in Windows filenames (``: * ? \" < > |`` and control chars) are
    removed. A name that sanitises to empty falls back to ``keikakusho``.
    """
    stem = safe_filename_stem(company_or_code or "", fallback="keikakusho")
    return f"keikakusho_{stem}.docx"
