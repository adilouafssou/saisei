"""Out-of-band LangSmith online eval suite — Keikakusho polish layer (Feature 1).

This is the final slice of Feature 1 (LangSmith eval). It evaluates the LLM
**polish** step (``polish_keikakusho``), complementing the existing advisory
judge (``test_advisory_eval.py``) which covers the *advisory* layer.

THE SPEC REQUIREMENT — gate the judge behind a deterministic check
----------------------------------------------------------------
The project's one inviolable rule is: the LLM may improve prose, but it must
NEVER add, drop, or alter a number in a regulated credit document. That rule is
not a matter of opinion, so it is NOT delegated to the LLM judge. Instead:

1. **Deterministic pre-gate (hard assertion):** render a real Keikakusho draft,
   run the real ``polish_keikakusho`` (live LLM), then assert
   ``check_numbers_preserved(draft, polished).preserved``. Numbers must survive.
   This is cheap, offline-equivalent, and non-negotiable.
2. **LLM-as-judge (only if the pre-gate passes):** the judge scores ONLY
   prose-level quality (readability + that the section structure and the FSA
   classification are preserved). Min score 3, logged to LangSmith.

SKIP GUARDS (must not run in CI / offline):
  Skipped unless ALL of the following are configured:
  - SAISEI_LANGSMITH_TRACING=true
  - SAISEI_LANGSMITH_API_KEY=<non-empty>
  - SAISEI_LLM_API_KEY=<non-empty>
  - SAISEI_LLM_MODEL=<non-empty>

  The skipif decorator checks these at collection time so ``uv run pytest``
  offline collects-and-skips them, never fails. It does NOT affect make verify.

ARCHITECTURE NOTE:
  These tests call real LLM endpoints and LangSmith APIs. They are intentionally
  out-of-band: they measure the quality of the polish layer, not the
  deterministic spine. The deterministic spine (FSA classification, burden
  sharing, working_capital_gap) and the numeric-preservation verifier itself are
  tested offline (tests/eval/test_golden_spine.py,
  tests/eval/test_numeric_preservation.py).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from app.shared.settings import get_settings

# ---------------------------------------------------------------------------
# Skip guard: all four conditions must be met for these tests to run.
# ---------------------------------------------------------------------------

_settings = get_settings()

_ONLINE_EVAL_ENABLED = (
    _settings.langsmith_tracing
    and bool(_settings.langsmith_api_key)
    and bool(_settings.llm_api_key)
    and bool(_settings.llm_model)
)

_SKIP_REASON = (
    "Online eval requires SAISEI_LANGSMITH_TRACING=true, "
    "SAISEI_LANGSMITH_API_KEY, SAISEI_LLM_API_KEY, and SAISEI_LLM_MODEL "
    "to be configured. Skipped in CI and offline environments."
)

pytestmark = pytest.mark.skipif(not _ONLINE_EVAL_ENABLED, reason=_SKIP_REASON)

# ---------------------------------------------------------------------------
# Fixtures and helpers (only evaluated when tests actually run).
# ---------------------------------------------------------------------------


def _draft() -> str:
    """Render a real Keikakusho draft with known, distinct figures.

    Mirrors the fixture in tests/eval/test_numeric_preservation.py so the figures
    under test are the actual ones the product renders, not hand-invented
    strings.
    """
    from app.backend.nodes.kaizen_generation import render_keikakusho
    from app.backend.state import Strategy
    from app.shared.models.accounting import TrialBalance
    from app.shared.models.classification import FsaClass

    latest = TrialBalance(
        period=dt.date(2025, 5, 31),
        uriage=138_000_000,
        uriage_genka=115_000_000,
        hanbaihi=21_000_000,
    )
    strategy = Strategy(
        title="原価低減（COGS reduction）",
        rationale="仕入先の多角化と歩留まり改善により原価を削減する。",
        expected_keijo_uplift=5_000_000,
    )
    return render_keikakusho(
        company_name="愛知精密製作所株式会社",
        hojin_bango="1234567890123",
        fsa_kanji=FsaClass.YOCHUISAKI.kanji,
        latest=latest,
        strategy=strategy,
        working_capital_gap=-1_000_000,
    )


def _llm_judge(prompt: str) -> dict[str, Any]:
    """Call the configured LLM as a judge and return a parsed score dict.

    The judge prompt must instruct the LLM to return a JSON object with:
      - score: int in [1, 5]
      - reasoning: str

    Args:
        prompt: The full judge prompt (user message body).

    Returns:
        Dict with 'score' (int) and 'reasoning' (str).
    """
    import json

    import httpx

    url = f"{_settings.llm_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": _settings.llm_model,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "あなたは日本の地域銀行向け経営改善計画書（経営改善計画書）の"
                    "文章品質評価者です。評価結果をJSON形式で返してください: "
                    '{"score": <1-5>, "reasoning": "<理由>"}。'
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    headers = {"Authorization": f"Bearer {_settings.llm_api_key}"}
    resp = httpx.post(url, json=payload, headers=headers, timeout=_settings.llm_timeout_seconds)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)  # type: ignore[no-any-return]


def _log_to_langsmith(
    run_name: str,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
) -> None:
    """Log an evaluation run to LangSmith via the REST API.

    Best-effort: any failure is swallowed so the test still passes/fails on
    the score assertion, not on the logging step.

    Args:
        run_name: Human-readable name for the eval run.
        inputs: The inputs to the judge.
        outputs: The judge's output (score, reasoning).
    """
    try:
        import httpx

        base = _settings.langsmith_endpoint.rstrip("/")
        headers = {
            "x-api-key": _settings.langsmith_api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "name": run_name,
            "run_type": "evaluation",
            "inputs": inputs,
            "outputs": outputs,
            "project_name": _settings.langsmith_project,
        }
        httpx.post(f"{base}/runs", json=payload, headers=headers, timeout=10.0)
    except Exception:  # noqa: BLE001 - logging is best-effort
        pass


# ---------------------------------------------------------------------------
# Test 1: Deterministic numeric-preservation pre-gate (hard, non-judge).
# ---------------------------------------------------------------------------


def test_polish_preserves_numbers_live() -> None:
    """HARD GATE: the live polish pass must preserve every yen figure.

    This is the deterministic check the spec requires the judge to be gated
    behind. It is NOT an LLM-judge call: numeric preservation is the project's
    one inviolable rule, so it is asserted directly. If this fails, the polish
    model corrupted a figure and the failure is unambiguous and merge-blocking
    (when run in the online suite).
    """
    from app.backend.analysis.numeric_preservation import check_numbers_preserved
    from app.backend.nodes.kaizen_generation import polish_keikakusho

    draft = _draft()
    polished = polish_keikakusho(draft, _settings)

    result = check_numbers_preserved(draft, polished)

    _log_to_langsmith(
        run_name="polish_numeric_preservation",
        inputs={"draft": draft, "polished": polished},
        outputs={
            "preserved": result.preserved,
            "missing": sorted(result.missing),
            "added": sorted(result.added),
            "reason": result.reason(),
        },
    )

    assert result.preserved, (
        f"Live polish altered the numeric content of the Keikakusho. {result.reason()}"
    )


# ---------------------------------------------------------------------------
# Test 2: LLM-as-judge on prose quality — gated behind the numeric pre-gate.
# ---------------------------------------------------------------------------


def test_polish_prose_quality_judge() -> None:
    """LLM-as-judge: polish improves readability while preserving structure.

    GATING (spec requirement): the cheap deterministic numeric-preservation
    check runs FIRST. The judge is invoked ONLY when numbers are preserved — we
    never spend a judge call (or risk a prose verdict) on a draft that already
    failed the inviolable numeric rule.

    Scoring rubric (1-5), prose-level ONLY:
      5 = Clearly more readable formal Japanese; ALL section headings and the
          FSA classification preserved; no content invented.
      4 = More readable; structure preserved; very minor register slips.
      3 = At least as readable as the draft; structure preserved.
      2 = Readability not improved, or a heading / FSA class was dropped.
      1 = Structure broken or content distorted.

    Minimum acceptable score: 3.
    """
    from app.backend.analysis.numeric_preservation import check_numbers_preserved
    from app.backend.nodes.kaizen_generation import polish_keikakusho

    draft = _draft()
    polished = polish_keikakusho(draft, _settings)

    # --- Deterministic pre-gate: do not judge prose on a numerically broken
    # draft. Numbers are the rule, not a matter of opinion. ---
    preservation = check_numbers_preserved(draft, polished)
    assert preservation.preserved, (
        f"Numeric pre-gate failed before prose judging could run. {preservation.reason()}"
    )

    # If the polish was a no-op (LLM call failed / fell back to the draft), there
    # is no prose change to judge; skip rather than penalise the deterministic
    # fallback contract of polish_keikakusho.
    if polished == draft:
        pytest.skip("Polish returned the draft unchanged (no prose change to judge).")

    judge_prompt = (
        "以下は経営改善計画書の、推敲前ドラフトと推敲後のテキストです。"
        "推敲後のテキストが、(1) より読みやすく適切な丁寧語になっているか、"
        "(2) すべてのセクション見出しと債務者区分（FSA分類）が保持されているか、"
        "を評価してください。数値の保持は別途検証済みのため、ここでは評価不要です。\n\n"
        f"【推敲前ドラフト】\n{draft}\n\n"
        f"【推敲後】\n{polished}\n\n"
        "評価基準:\n"
        "5 = 明らかに読みやすい丁寧な日本語。全見出しとFSA分類を保持。内容の捧造なし\n"
        "4 = 読みやすく構造も保持。軽微な文体の不一致のみ\n"
        "3 = ドラフトと同等以上に読みやすく、構造を保持\n"
        "2 = 読みやすさが向上していない、または見出し/FSA分類が欠落\n"
        "1 = 構造が破壊されている、または内容が歪んでいる\n"
    )

    result = _llm_judge(judge_prompt)
    score = int(result.get("score", 0))
    reasoning = str(result.get("reasoning", ""))

    _log_to_langsmith(
        run_name="polish_prose_quality",
        inputs={"draft": draft, "polished": polished},
        outputs={"score": score, "reasoning": reasoning},
    )

    assert score >= 3, (
        f"Polish prose-quality score {score}/5 is below minimum (3). Reasoning: {reasoning}"
    )
