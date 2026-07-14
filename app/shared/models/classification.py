"""FSA debtor classification for Saisei.

Aligned with the FSA Financial Inspection Manual (金融検査マニュアル). The set of
classifications is closed and MUST be limited to the five values below, which
correspond exactly to the five debtor categories defined in the Manual.

**要管理先 (Special Attention)** is a *sub-tier* of 要注意先, not a separate
top-level category. It is modelled as an optional ``special_attention`` boolean
flag carried on state (see ``app.backend.state.SaiseiState``). A borrower
classified as 要注意先 with ``special_attention=True`` is a 要管理先 borrower.
This matches the regulatory structure: the Manual defines 要管理先 as a subset
of 要注意先 whose loans are subject to special management (要管理債権).

This module is the canonical location under ``app.shared.models.classification``.
The legacy path ``shared.domain.classification`` re-exports from here.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["FsaClass"]


class FsaClass(StrEnum):
    """FSA debtor classification (債務者区分) — five categories.

    Defined by the FSA Financial Inspection Manual (金融検査マニュアル).
    Values are romanized identifiers; use ``.kanji`` / ``.english`` for display.

    Members (most healthy → most distressed):
        SEIJOSAKI:           正常先 — Normal.
        YOCHUISAKI:          要注意先 — Needs Attention (Substandard).
                             Sub-tier: 要管理先 (Special Attention) is modelled
                             as ``special_attention=True`` on state, NOT as a
                             separate top-level enum member.
        HATAN_KENENSAKI:     破綻懸念先 — In Danger of Bankruptcy (Doubtful).
        JISSHITSU_HATANSAKI: 実質破綻先 — De facto Bankrupt.
        HATANSAKI:           破綻先 — Bankrupt.

    Routing properties:
        ``requires_turnaround``: True for 要注意先 and 破綻懸念先 only — these
            borrowers route to the strategist / HITL turnaround workflow.
        ``requires_workout``: True for 実質破綻先 and 破綻先 — these borrowers
            route to the legal/liquidation workout node instead.
        正常先 routes to END (monitor only); 実質破綻先 / 破綻先 route to workout.
    """

    SEIJOSAKI = "seijosaki"
    YOCHUISAKI = "yochuisaki"
    HATAN_KENENSAKI = "hatan_kenensaki"
    JISSHITSU_HATANSAKI = "jisshitsu_hatansaki"
    HATANSAKI = "hatansaki"

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

        True for 要注意先 and 破綻懸念先 only — these route to the strategist
        and the human-in-the-loop negotiation.

        正常先 is monitor-only (False).
        実質破綻先 and 破綻先 route to the workout node, not the turnaround
        workflow (False here; use ``requires_workout`` for those).
        """
        return self in (FsaClass.YOCHUISAKI, FsaClass.HATAN_KENENSAKI)

    @property
    def requires_workout(self) -> bool:
        """Whether this classification triggers the legal/liquidation workout.

        True for 実質破綻先 and 破綻先 — these borrowers are beyond turnaround
        and require a legal or liquidation handoff.
        """
        return self in (FsaClass.JISSHITSU_HATANSAKI, FsaClass.HATANSAKI)


_KANJI: dict[FsaClass, str] = {
    FsaClass.SEIJOSAKI: "正常先",
    FsaClass.YOCHUISAKI: "要注意先",
    FsaClass.HATAN_KENENSAKI: "破綻懸念先",
    FsaClass.JISSHITSU_HATANSAKI: "実質破綻先",
    FsaClass.HATANSAKI: "破綻先",
}

_ENGLISH: dict[FsaClass, str] = {
    FsaClass.SEIJOSAKI: "Normal",
    FsaClass.YOCHUISAKI: "Needs Attention",
    FsaClass.HATAN_KENENSAKI: "In Danger of Bankruptcy",
    FsaClass.JISSHITSU_HATANSAKI: "De facto Bankrupt",
    FsaClass.HATANSAKI: "Bankrupt",
}
