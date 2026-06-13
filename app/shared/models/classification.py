"""FSA debtor classification for Saisei.

Aligned with the FSA Financial Inspection Manual (金融検査マニュアル). The set of
classifications is closed and MUST be limited to the three values below.

This module is the canonical location under ``app.shared.models.classification``.
The legacy path ``shared.domain.classification`` re-exports from here.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["FsaClass"]


class FsaClass(StrEnum):
    """FSA debtor classification (債務者区分).

    Members:
        JOYO: 正常 — Normal.
        YOI_KANRI: 要注意 — Substandard / needs attention.
        YUKYO_GUCHI: 要管理 — Doubtful / needs management.
    """

    JOYO = "joyo"
    YOI_KANRI = "yoi_kanri"
    YUKYO_GUCHI = "yukyo_guchi"

    @property
    def kanji(self) -> str:
        """Return the Japanese label for this classification."""
        return _KANJI[self]

    @property
    def english(self) -> str:
        """Return the English label for this classification."""
        return _ENGLISH[self]

    @property
    def requires_turnaround(self) -> bool:
        """Whether this classification triggers the turnaround workflow.

        ``JOYO`` is monitor-only; the other two route to the strategist and the
        human-in-the-loop negotiation.
        """
        return self is not FsaClass.JOYO


_KANJI: dict[FsaClass, str] = {
    FsaClass.JOYO: "正常",
    FsaClass.YOI_KANRI: "要注意",
    FsaClass.YUKYO_GUCHI: "要管理",
}

_ENGLISH: dict[FsaClass, str] = {
    FsaClass.JOYO: "Normal",
    FsaClass.YOI_KANRI: "Substandard",
    FsaClass.YUKYO_GUCHI: "Doubtful",
}
