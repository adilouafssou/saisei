"""LangSmith opt-in tracing configuration and HITL feedback dataset capture.

Provides:
- :func:`configure_tracing`: reads ``langsmith_*`` settings and, when both
  ``langsmith_tracing=True`` and ``langsmith_api_key`` is non-empty, sets the
  four ``LANGCHAIN_*`` environment variables that LangGraph uses for
  auto-instrumentation.
- :func:`capture_hitl_feedback`: captures HITL banker decisions (approve /
  revise / reject from NegotiationDecision, plus reconciliation resolutions) as
  LangSmith dataset examples when tracing is configured. Strict no-op offline.
  Seeds the future outcomes corpus for LLM-as-judge evaluation.

**Offline-by-default contract**: when either guard is false (the default), both
functions set *nothing* and make *zero* network calls. They import cleanly with
no external dependencies so ``make verify`` runs fully offline.

LangGraph auto-instruments once the ``LANGCHAIN_*`` env vars are present; no
manual node wrapping is required.
"""

from __future__ import annotations

import os
from typing import Any

from app.backend.secrets import resolve_secret
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "configure_tracing",
    "capture_hitl_feedback",
    "push_golden_dataset",
]

_log = get_logger(__name__)

#: LangSmith dataset name for HITL banker decisions (outcomes corpus).
_HITL_DATASET_NAME = "saisei-hitl-decisions"

#: LangSmith dataset name for the versioned classification golden cases.
_GOLDEN_DATASET_NAME = "saisei-classification-golden"


def configure_tracing(settings: Settings | None = None) -> bool:
    """Configure LangSmith tracing from application settings.

    Reads the ``langsmith_*`` fields on :class:`~app.shared.settings.Settings`
    and activates LangGraph auto-instrumentation by setting the four
    ``LANGCHAIN_*`` environment variables **only** when both guards are true:

    1. ``settings.langsmith_tracing`` is ``True``.
    2. ``settings.langsmith_api_key`` is non-empty.

    When either guard is false (the default), this function is a strict no-op:
    no environment variables are set and no network calls are made. This mirrors
    the ``empty/false -> offline mock`` pattern used by ``llm_*``, ``pgvector_*``,
    ``boj_*``, and ``hojin_bango_*`` in :mod:`app.shared.settings`.

    Args:
        settings: Application settings instance. Defaults to
            :func:`~app.shared.settings.get_settings` (the cached singleton).

    Returns:
        ``True`` when tracing was enabled (env vars set); ``False`` otherwise.
    """
    cfg = settings or get_settings()

    # Resolve the key through the secret seam so it may be a literal (default) or
    # a @env:/@file:/@/path reference (and, in production, a Vault-backed value).
    api_key = resolve_secret(cfg.langsmith_api_key)
    if not cfg.langsmith_tracing or not api_key:
        _log.info(
            "observability.tracing.disabled",
            langsmith_tracing=cfg.langsmith_tracing,
            api_key_set=bool(api_key),
        )
        return False

    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = api_key
    os.environ["LANGCHAIN_PROJECT"] = cfg.langsmith_project
    os.environ["LANGCHAIN_ENDPOINT"] = cfg.langsmith_endpoint

    _log.info(
        "observability.tracing.enabled",
        project=cfg.langsmith_project,
        endpoint=cfg.langsmith_endpoint,
    )
    return True


def _resolved_langsmith_key(settings: Settings) -> str:
    """Return the LangSmith API key resolved through the secret seam ("" if unset).

    The single resolver used by the active-gate AND both x-api-key header
    builders, so a @env:/@file:/@/path reference is dereferenced consistently.
    Without this, _tracing_active would pass on the literal reference string and
    the upload paths would then send that reference as the API key (401).
    A literal key passes through unchanged (back-compat).
    """
    return resolve_secret(settings.langsmith_api_key)


def _tracing_active(settings: Settings) -> bool:
    """Return True when LangSmith tracing is fully configured."""
    return bool(settings.langsmith_tracing and _resolved_langsmith_key(settings))


def capture_hitl_feedback(
    *,
    tdb_code: str,
    decision: str,
    strategies: list[dict[str, Any]],
    approved_strategy: dict[str, Any] | None = None,
    revision_note: str | None = None,
    reconciliation_required: bool = False,
    reconciliation_details: list[dict[str, Any]] | None = None,
    feasibility_notes: list[dict[str, Any]] | None = None,
    fsa_classification: str | None = None,
    working_capital_gap: int | None = None,
    settings: Settings | None = None,
) -> bool:
    """Capture a HITL banker decision as a LangSmith dataset example.

    Records the banker's approve / revise / reject decision (and any
    reconciliation resolution) as a labelled example in the LangSmith
    ``saisei-hitl-decisions`` dataset. This seeds the future outcomes corpus
    for LLM-as-judge evaluation of the advisory layer.

    **Offline-by-default contract**: this function is a strict no-op when
    LangSmith tracing is not configured (``langsmith_tracing=False`` or
    ``langsmith_api_key`` empty). No network calls are made in that case.
    Mirrors the ``empty/false -> offline mock`` pattern.

    The dataset example schema:
        inputs:
            tdb_code, fsa_classification, working_capital_gap,
            strategies (list of dicts), reconciliation_required,
            reconciliation_details, feasibility_notes
        outputs:
            decision ('approve'|'revise'|'reject'),
            approved_strategy (dict or None),
            revision_note (str or None)

    Args:
        tdb_code: 7-digit TDB Kigyo code for the borrower.
        decision: Banker decision ('approve' | 'revise' | 'reject').
        strategies: Proposed strategies at the time of the decision.
        approved_strategy: The approved strategy dict (when decision='approve').
        revision_note: Banker revision note (when decision='revise'/'reject').
        reconciliation_required: Whether reconciliation was triggered.
        reconciliation_details: Per-strategy reconciliation detail dicts.
        feasibility_notes: Advisory feasibility note dicts.
        fsa_classification: FSA classification value string (e.g. '要注意先').
        working_capital_gap: Working-capital gap in JPY (negative = deficit).
        settings: Application settings. Defaults to cached settings.

    Returns:
        ``True`` when the example was successfully captured; ``False`` otherwise
        (including all offline / no-tracing cases).
    """
    cfg = settings or get_settings()
    if not _tracing_active(cfg):
        _log.info(
            "observability.hitl_feedback.skipped",
            reason="tracing_not_configured",
            tdb_code=tdb_code,
            decision=decision,
        )
        return False

    try:
        return _upload_to_langsmith(
            cfg=cfg,
            tdb_code=tdb_code,
            decision=decision,
            strategies=strategies,
            approved_strategy=approved_strategy,
            revision_note=revision_note,
            reconciliation_required=reconciliation_required,
            reconciliation_details=reconciliation_details or [],
            feasibility_notes=feasibility_notes or [],
            fsa_classification=fsa_classification,
            working_capital_gap=working_capital_gap,
        )
    except Exception as exc:  # noqa: BLE001 - capture is best-effort
        _log.warning(
            "observability.hitl_feedback.failed",
            error=str(exc),
            tdb_code=tdb_code,
        )
        return False


def _upload_to_langsmith(
    *,
    cfg: Settings,
    tdb_code: str,
    decision: str,
    strategies: list[dict[str, Any]],
    approved_strategy: dict[str, Any] | None,
    revision_note: str | None,
    reconciliation_required: bool,
    reconciliation_details: list[dict[str, Any]],
    feasibility_notes: list[dict[str, Any]],
    fsa_classification: str | None,
    working_capital_gap: int | None,
) -> bool:
    """Upload a HITL feedback example to LangSmith via the REST API.

    Uses the LangSmith REST API directly (httpx) to avoid adding langsmith as a
    hard dependency. The dataset is created on first use if it does not exist.
    Best-effort: any HTTP or parse error is swallowed by the caller.

    Args:
        cfg: Application settings (LangSmith endpoint / key / project).
        tdb_code: 7-digit TDB Kigyo code.
        decision: Banker decision string.
        strategies: Proposed strategies at decision time.
        approved_strategy: Approved strategy dict or None.
        revision_note: Revision note or None.
        reconciliation_required: Whether reconciliation was triggered.
        reconciliation_details: Reconciliation detail dicts.
        feasibility_notes: Feasibility note dicts.
        fsa_classification: FSA classification value string.
        working_capital_gap: Working-capital gap in JPY.

    Returns:
        True on success.
    """
    import httpx

    base = cfg.langsmith_endpoint.rstrip("/")
    headers = {
        "x-api-key": _resolved_langsmith_key(cfg),
        "Content-Type": "application/json",
    }

    # --- Ensure the dataset exists (idempotent) ---
    dataset_id = _ensure_dataset(base, headers, _HITL_DATASET_NAME)

    # --- Upload the example ---
    example_payload: dict[str, Any] = {
        "dataset_id": dataset_id,
        "inputs": {
            "tdb_code": tdb_code,
            "fsa_classification": fsa_classification,
            "working_capital_gap": working_capital_gap,
            "strategies": strategies,
            "reconciliation_required": reconciliation_required,
            "reconciliation_details": reconciliation_details,
            "feasibility_notes": feasibility_notes,
        },
        "outputs": {
            "decision": decision,
            "approved_strategy": approved_strategy,
            "revision_note": revision_note,
        },
    }
    resp = httpx.post(
        f"{base}/examples",
        json=example_payload,
        headers=headers,
        timeout=10.0,
    )
    resp.raise_for_status()

    _log.info(
        "observability.hitl_feedback.captured",
        tdb_code=tdb_code,
        decision=decision,
        dataset=_HITL_DATASET_NAME,
    )
    return True


def _ensure_dataset(base: str, headers: dict[str, str], name: str) -> str:
    """Return the dataset ID for ``name``, creating it if it does not exist.

    Args:
        base: LangSmith API base URL.
        headers: HTTP headers (API key).
        name: Dataset name.

    Returns:
        The dataset ID string.
    """
    import httpx

    # Try to fetch existing dataset by name.
    resp = httpx.get(
        f"{base}/datasets",
        params={"name": name},
        headers=headers,
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    datasets = data if isinstance(data, list) else data.get("datasets", [])
    for ds in datasets:
        if isinstance(ds, dict) and ds.get("name") == name:
            return str(ds["id"])

    # Create the dataset.
    create_resp = httpx.post(
        f"{base}/datasets",
        json={
            "name": name,
            "description": (
                "HITL banker decisions (approve/revise/reject) and reconciliation "
                "resolutions captured by Saisei for LLM-as-judge evaluation."
            ),
        },
        headers=headers,
        timeout=10.0,
    )
    create_resp.raise_for_status()
    return str(create_resp.json()["id"])


def push_golden_dataset(settings: Settings | None = None) -> int:
    """Push the in-repo classification golden cases to a LangSmith dataset.

    Versions the deterministic golden dataset (``tests/eval/golden_dataset.py``)
    in LangSmith so the same labelled ``(TDB code -> expected FSA class,
    special_attention)`` cases that gate CI offline are also available online for
    LangSmith-side evaluation and drift tracking. This is the
    ``Version the golden dataset in LangSmith`` item of Feature 1.

    Each :class:`~tests.eval.golden_dataset.GoldenCase` becomes one example:
        inputs:  {"tdb_code": <code>, "label": <label>}
        outputs: {"expected_fsa": <value>, "expected_special_attention": <bool>}

    **Offline-by-default contract**: a strict no-op returning ``0`` when
    LangSmith tracing is not configured (``langsmith_tracing=False`` or
    ``langsmith_api_key`` empty), so it never touches the network in CI / offline
    -- mirroring :func:`capture_hitl_feedback`. Best-effort on any HTTP error:
    the count of examples successfully uploaded is returned.

    The golden dataset is imported lazily from ``tests.eval`` so importing this
    module never pulls in the test package; the import is resolved only on the
    online path (when an operator actually pushes the dataset).

    Args:
        settings: Application settings. Defaults to cached settings.

    Returns:
        The number of golden cases uploaded (``0`` offline / on a hard failure).
    """
    cfg = settings or get_settings()
    if not _tracing_active(cfg):
        _log.info(
            "observability.golden_dataset.skipped",
            reason="tracing_not_configured",
        )
        return 0

    try:
        from tests.eval.golden_dataset import GOLDEN_DATASET
    except Exception as exc:  # noqa: BLE001 - import is best-effort
        _log.warning("observability.golden_dataset.import_failed", error=str(exc))
        return 0

    try:
        return _upload_golden_dataset(cfg, GOLDEN_DATASET)
    except Exception as exc:  # noqa: BLE001 - push is best-effort
        _log.warning("observability.golden_dataset.failed", error=str(exc))
        return 0


def _upload_golden_dataset(
    cfg: Settings,
    cases: Any,
) -> int:
    """Upload the golden cases to LangSmith, returning the count uploaded.

    Creates the dataset on first use (idempotent) and posts one example per
    case. Each example upload is best-effort: a single failed example is logged
    and skipped rather than aborting the whole push.

    Args:
        cfg: Application settings (LangSmith endpoint / key / project).
        cases: Iterable of GoldenCase records.

    Returns:
        The number of examples successfully uploaded.
    """
    import httpx

    base = cfg.langsmith_endpoint.rstrip("/")
    headers = {
        "x-api-key": _resolved_langsmith_key(cfg),
        "Content-Type": "application/json",
    }
    dataset_id = _ensure_dataset(base, headers, _GOLDEN_DATASET_NAME)

    uploaded = 0
    for case in cases:
        # FsaClass is a str-enum; .value keeps the example JSON-serialisable.
        expected_fsa = getattr(case.expected_fsa, "value", str(case.expected_fsa))
        payload: dict[str, Any] = {
            "dataset_id": dataset_id,
            "inputs": {"tdb_code": case.tdb_code, "label": case.label},
            "outputs": {
                "expected_fsa": expected_fsa,
                "expected_special_attention": case.expected_special_attention,
            },
        }
        try:
            resp = httpx.post(f"{base}/examples", json=payload, headers=headers, timeout=10.0)
            resp.raise_for_status()
            uploaded += 1
        except Exception as exc:  # noqa: BLE001 - per-example best-effort
            _log.warning(
                "observability.golden_dataset.example_failed",
                tdb_code=case.tdb_code,
                error=str(exc),
            )

    _log.info(
        "observability.golden_dataset.pushed",
        dataset=_GOLDEN_DATASET_NAME,
        uploaded=uploaded,
    )
    return uploaded
