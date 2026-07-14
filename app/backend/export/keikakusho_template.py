"""Render the Keikakusho body into a bank's house DOCX template.

Banks submit the 経営改善計画書 on their OWN letterhead: a house ``.docx`` with a
cover page, branding, fixed section headers, and headers/footers. "Export
delivery" is placing the deterministic Keikakusho body INTO that template rather
than always emitting a bare default document.

How it works
------------
The bank's template contains a single placeholder paragraph whose text is, by
default, ``{{KEIKAKUSHO_BODY}}``. :func:`build_keikakusho_docx_from_template`
loads the template (preserving every other element — cover page, styles,
headers/footers), renders the deterministic Keikakusho Markdown in place AT the
placeholder's position using the shared verbatim renderer, and removes the
placeholder. Everything around the placeholder is the bank's; everything in the
body is Saisei's number-safe output.

Numeric-preservation invariant
------------------------------
The body is produced by the SAME line-by-line, verbatim
:func:`~app.backend.export._markdown_docx.render_markdown_into_document` used by
the default exporter, so no figure is ever reformatted. The template only
provides the surrounding container; it cannot alter a body figure.

Pure and deterministic (``python-docx`` only; no network, no LLM). If the
template has no placeholder, a clear :class:`TemplateError` is raised rather
than silently dropping the body.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from docx import Document

from app.backend.export._markdown_docx import render_markdown_into_document

if TYPE_CHECKING:
    from docx.text.paragraph import Paragraph

__all__ = [
    "DEFAULT_BODY_PLACEHOLDER",
    "TemplateError",
    "build_keikakusho_docx_from_template",
]

#: The placeholder paragraph text a bank template must contain to mark where the
#: Keikakusho body is injected.
DEFAULT_BODY_PLACEHOLDER = "{{KEIKAKUSHO_BODY}}"


class TemplateError(ValueError):
    """Raised when a bank DOCX template is missing the body placeholder."""


def _find_placeholder(document: object, placeholder: str) -> Paragraph | None:
    """Return the first paragraph whose text contains ``placeholder``, or None."""
    for paragraph in document.paragraphs:  # type: ignore[attr-defined]
        if placeholder in paragraph.text:
            return paragraph  # type: ignore[no-any-return]
    return None


def build_keikakusho_docx_from_template(
    draft_markdown: str,
    template_bytes: bytes,
    *,
    placeholder: str = DEFAULT_BODY_PLACEHOLDER,
) -> bytes:
    """Render the Keikakusho body into a bank's house DOCX template.

    Loads ``template_bytes`` (a ``.docx``), finds the ``placeholder`` paragraph,
    renders the deterministic ``draft_markdown`` at that position with the shared
    verbatim renderer, removes the placeholder, and returns the combined
    document. The template's cover page, styles, and headers/footers are
    preserved; only the placeholder paragraph is replaced by the body.

    The body is rendered by appending to the document and then relocating the
    new elements to sit immediately before the placeholder, so the body lands
    exactly where the bank put the placeholder (not at the end), regardless of
    what content follows it in the template.

    Args:
        draft_markdown: The deterministic Keikakusho draft Markdown.
        template_bytes: The bank's ``.docx`` template as bytes.
        placeholder: The placeholder text marking the insertion point.

    Returns:
        The generated ``.docx`` (template + injected body) as bytes.

    Raises:
        TemplateError: If the template does not contain ``placeholder``.
    """
    document = Document(io.BytesIO(template_bytes))

    target = _find_placeholder(document, placeholder)
    if target is None:
        raise TemplateError(f"template is missing the body placeholder {placeholder!r}")

    # Remember how many body elements exist before rendering, so we can identify
    # exactly the elements the renderer appends (it always appends at the end).
    body = document.element.body
    existing = list(body)
    before_count = len(existing)

    render_markdown_into_document(document, draft_markdown)

    # The appended elements are everything after the original tail.
    new_elements = list(body)[before_count:]

    # Move each appended element to just before the placeholder paragraph, in
    # order, so the body appears where the bank placed the placeholder.
    placeholder_el = target._p
    for element in new_elements:
        body.remove(element)
        placeholder_el.addprevious(element)

    # Remove the now-empty placeholder paragraph.
    placeholder_el.getparent().remove(placeholder_el)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
