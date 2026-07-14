"""Verifier for the shared export filename sanitiser (safe_filename_stem).

No CI here, so this pins the cross-platform download-name contract used by both
the DOCX and XLSX exporters: path separators / whitespace collapse to '_', the
Windows-reserved characters : * ? " < > | and control chars are removed, runs
collapse, leading/trailing dots/underscores are trimmed, and an empty result
falls back safely. Ordinary names are unchanged.
"""

from __future__ import annotations

import pytest
from app.backend.export._filenames import safe_filename_stem
from app.backend.export.keikakusho_docx import docx_filename
from app.backend.export.recovery_xlsx import xlsx_filename


def test_ordinary_name_is_preserved() -> None:
    assert safe_filename_stem("AichiManufacturing") == "AichiManufacturing"


def test_spaces_and_separators_collapse_to_underscore() -> None:
    assert (
        safe_filename_stem("\u30c6\u30b9\u30c8 \u88fd\u9020") == "\u30c6\u30b9\u30c8_\u88fd\u9020"
    )
    assert safe_filename_stem("a/b\\c") == "a_b_c"


@pytest.mark.parametrize("ch", list(':*?"<>|'))
def test_windows_illegal_characters_are_removed(ch: str) -> None:
    """Each Windows-reserved character is stripped (collapsed to '_')."""
    out = safe_filename_stem(f"co{ch}name")
    assert ch not in out
    assert out == "co_name"


def test_control_characters_are_removed() -> None:
    assert safe_filename_stem("a\x00\x1fb") == "a_b"


def test_runs_collapse_to_single_underscore() -> None:
    assert safe_filename_stem("a   ///  b") == "a_b"


def test_leading_trailing_dots_and_underscores_trimmed() -> None:
    assert safe_filename_stem("...name...") == "name"
    assert safe_filename_stem("__name__") == "name"


def test_empty_or_all_illegal_falls_back() -> None:
    assert safe_filename_stem("") == "export"
    assert safe_filename_stem("///", fallback="recovery") == "recovery"
    assert safe_filename_stem("***", fallback="keikakusho") == "keikakusho"


def test_exporters_use_the_sanitiser_and_stay_safe() -> None:
    """Both exporter filename builders strip illegal chars and never crash."""
    assert docx_filename("bad:name?") == "keikakusho_bad_name.docx"
    assert xlsx_filename("bad:name?") == "recovery_bad_name.xlsx"
    # Empty falls back to each module's own stem.
    assert docx_filename("") == "keikakusho_keikakusho.docx"
    assert xlsx_filename("") == "recovery_recovery.xlsx"
