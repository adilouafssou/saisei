"""Shared download-filename sanitisation for the export modules.

The DOCX and XLSX exporters both build a download filename from a company name
or TDB code. A raw name can contain characters that are illegal in filenames on
common operating systems (notably Windows: ``\\ / : * ? " < > |``) or control
characters, which make some browsers/OSes reject or mangle the download. This is
the single helper both exporters use so the rule lives in one place.

Pure and deterministic (stdlib ``re`` only).
"""

from __future__ import annotations

import re

__all__ = ["safe_filename_stem"]

#: Characters illegal in a filename on Windows (a superset of POSIX needs), plus
#: path separators and whitespace. ASCII control chars (0x00-0x1f) are handled
#: separately. Each run is collapsed to a single underscore.
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\s\x00-\x1f]+')
#: Leading/trailing dots and underscores are trimmed (a trailing dot is invalid
#: on Windows; leading dots hide files on POSIX).
_TRIM_RE = re.compile(r"^[._]+|[._]+$")


def safe_filename_stem(name: str, *, fallback: str = "export") -> str:
    """Return ``name`` reduced to a cross-platform-safe filename stem.

    Collapses runs of illegal characters (path separators, whitespace, the
    Windows-reserved ``: * ? " < > |``, and control characters) to a single
    underscore, then trims leading/trailing dots and underscores. Returns
    ``fallback`` when the result is empty.

    Args:
        name: The raw name (e.g. a company name or TDB code).
        fallback: Value to return when sanitisation yields an empty string.

    Returns:
        A safe filename stem (never empty).
    """
    cleaned = _ILLEGAL_RE.sub("_", name or "")
    cleaned = _TRIM_RE.sub("", cleaned)
    return cleaned or fallback
