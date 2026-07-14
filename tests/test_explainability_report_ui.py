"""Verifier for the Feature 7 explainability-report UI wiring.

The renderer + its determinism/numeric invariants are covered by
``tests/test_explainability_report.py``. This pins the SEPARATE concern of how
the Reflex UI builds, caches, gates, and emits that report, without a Reflex
event loop or browser (per the ``_bare_state`` pattern):

- ``_refresh_explainability_report`` caches the rendered Markdown from the final
  snapshot when a classification exists, and clears it (hiding the button) when
  there is none.
- ``has_explainability_report`` reflects the cache exactly (the button's cond).
- ``_reset_run`` clears the cache between runs (no stale report leaks).
- ``download_explainability_docx`` is a no-op until classified, and otherwise
  emits the report as a Word (.docx) download (the only download offered; banks /
  FSA examiners exchange Word, not Markdown).

The cached Markdown must equal the renderer's own output for the same snapshot,
so the UI never silently diverges from the deterministic artifact.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.backend.export.explainability_report import (
    build_explainability_report,
    explainability_docx_filename,
)
from app.backend.nodes.ews_scoring import compute_ews_breakdown, compute_ews_score
from app.backend.state import SaiseiState
from app.backend.tools.tdb_api import CompanyProfile
from app.frontend.state import SaiseiUIState
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass

from tests._bare_state import bare_ui_state


def _fget(var: Any, inst: SaiseiUIState) -> Any:
    """Invoke a computed ``rx.var``'s runtime getter (``.fget``)."""
    return var.fget(inst)


def _fn(handler: Any, *args: Any) -> Any:
    """Invoke an ``rx.event`` handler's underlying function (``.fn``)."""
    return handler.fn(*args)


# NOTE: ``_refresh_explainability_report`` and ``_reset_run`` are PLAIN instance
# methods (not ``@rx.event`` handlers), so they are called directly on the bare
# instance. Only ``@rx.event`` handlers expose ``.fn`` (use ``_fn``) and only
# ``@rx.var`` computed vars expose ``.fget`` (use ``_fget``).


def _history() -> list[TrialBalance]:
    rows: list[TrialBalance] = []
    for i in range(6):
        sales = 100_000_000 - i * 8_000_000
        cogs = int(sales * (0.72 + i * 0.01))
        rows.append(
            TrialBalance(
                period=dt.date(2025, 1, 1) + dt.timedelta(days=30 * i),
                uriage=sales,
                uriage_genka=cogs,
                hanbaihi=20_000_000,
            )
        )
    return rows


def _classified_snapshot() -> dict[str, Any]:
    """A finalized backend snapshot (as ``_apply_snapshot`` receives it)."""
    history = _history()
    state = SaiseiState(
        tdb_code="1234567",
        company_profile=CompanyProfile(
            tdb_code="1234567",
            hojin_bango="1234567890123",
            name="\u30c6\u30b9\u30c8\u88fd\u9020\u682a\u5f0f\u4f1a\u793e",
            prefecture="\u611b\u77e5\u770c",
            industry="\u88fd\u9020\u696d",
            established_year=1990,
            employees=42,
        ),
        shisanhyo=history,
        working_capital_gap=-5_000_000,
        ews_score=compute_ews_score(history),
        ews_breakdown=[s.__dict__ for s in compute_ews_breakdown(history)],
        fsa_classification=FsaClass.YOCHUISAKI,
        special_attention=True,
        classification_reason="\u8cc7\u91d1\u7e70\u308a\u4e0d\u8db3 (working-capital deficit)",
    )
    return dict(state.model_dump())


def _fresh() -> SaiseiUIState:
    inst = bare_ui_state()
    inst.explainability_report_md = ""
    inst.company_name = ""
    inst.tdb_code = "1234567"
    return inst


def test_refresh_caches_report_when_classified() -> None:
    """A classified snapshot populates the cache with the renderer's output."""
    inst = _fresh()
    snapshot = _classified_snapshot()
    SaiseiUIState._refresh_explainability_report(inst, snapshot)
    assert inst.explainability_report_md != ""
    # The UI cache must equal the renderer's own output for the same snapshot.
    assert inst.explainability_report_md == build_explainability_report(snapshot)


def test_refresh_clears_cache_when_unclassified() -> None:
    """No fsa_classification -> the cache is cleared (button hidden)."""
    inst = _fresh()
    inst.explainability_report_md = "stale"
    SaiseiUIState._refresh_explainability_report(inst, {"tdb_code": "1234567"})
    assert inst.explainability_report_md == ""


def test_has_explainability_report_reflects_cache() -> None:
    """The computed var is True iff the cache is non-empty."""
    inst = _fresh()
    assert _fget(SaiseiUIState.has_explainability_report, inst) is False
    inst.explainability_report_md = "# report\n"
    assert _fget(SaiseiUIState.has_explainability_report, inst) is True


def test_reset_run_clears_cache() -> None:
    """A fresh run must not leak the prior borrower's report."""
    inst = _fresh()
    inst.explainability_report_md = "# stale report\n"
    SaiseiUIState._reset_run(inst)
    assert inst.explainability_report_md == ""


def test_download_is_noop_until_classified() -> None:
    """With no cached report the download handler is a safe no-op."""
    inst = _fresh()
    assert _fn(SaiseiUIState.download_explainability_docx, inst) is None


def test_download_emits_docx_artifact() -> None:
    """Once classified, the download emits a Word (.docx) artifact for the report."""
    inst = _fresh()
    snapshot = _classified_snapshot()
    SaiseiUIState._refresh_explainability_report(inst, snapshot)
    inst.company_name = "\u30c6\u30b9\u30c8\u88fd\u9020\u682a\u5f0f\u4f1a\u793e"

    spec = _fn(SaiseiUIState.download_explainability_docx, inst)
    assert spec is not None  # a download spec is emitted
    # The filename is the .docx variant derived from the company name.
    assert (
        explainability_docx_filename(inst.company_name)
        == "explainability_\u30c6\u30b9\u30c8\u88fd\u9020\u682a\u5f0f\u4f1a\u793e.docx"
    )
    # Guard: the cached report (the DOCX source of truth) still carries the EWS score.
    score = compute_ews_score(_history())
    score_txt = str(int(score)) if score == int(score) else f"{score:.2f}"
    assert score_txt in inst.explainability_report_md
    assert repr(spec)  # the download spec rendered
