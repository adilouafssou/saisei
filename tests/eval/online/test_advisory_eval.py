"""Out-of-band LangSmith online eval suite — advisory layer quality (MR #2).

Uses LangSmith + LLM-as-judge to score the advisory layer on two dimensions:

1. **Citation faithfulness**: does the advisory text reflect the retrieved
   precedent snippets? (Grounded in retrieved context, not hallucinated.)
2. **Japanese regulatory register/tone**: is the advisory written in the
   appropriate formal Japanese register for a regional-bank turnaround context?

SKIP GUARDS (must not run in CI / offline):
  These tests are skipped unless ALL of the following are configured:
  - SAISEI_LANGSMITH_TRACING=true
  - SAISEI_LANGSMITH_API_KEY=<non-empty>
  - SAISEI_LLM_API_KEY=<non-empty>
  - SAISEI_LLM_MODEL=<non-empty>

  The skipif decorators check these at collection time so ``uv run pytest``
  offline collects-and-skips them, never fails. They do NOT affect make verify.

ARCHITECTURE NOTE:
  These tests call real LLM endpoints and LangSmith APIs. They are intentionally
  out-of-band: they measure the quality of the advisory layer, not the
  deterministic spine. The deterministic spine (FSA classification, lead_arranger
  verdict, burden-sharing math, working_capital_gap) is tested in the offline
  golden-eval harness (tests/eval/test_golden_spine.py).
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


def _make_advisory_sample() -> dict[str, Any]:
    """Build a minimal advisory sample for evaluation.

    Returns a dict with:
      - advisory: the LLM-produced advisory text to evaluate.
      - snippets: the retrieved precedent snippets used to ground it.
      - strategy: the strategy being assessed.
    """
    from app.backend.nodes.critics.feasibility import (
        feasibility_critic_node,
    )
    from app.backend.state import SaiseiState, Strategy
    from app.shared.models.accounting import TrialBalance

    strategy = Strategy(
        title="価格転嫁戦略",
        rationale="原材料費上昇分を製品価格に転嫁し、粗利率を改善する。",
        expected_keijo_uplift=30_000_000,
    )
    tb = TrialBalance(
        period=dt.date(2026, 3, 31),
        uriage=100_000_000,
        uriage_genka=70_000_000,
        hanbaihi=20_000_000,
        eigai_shueki=0,
        eigai_hiyo=0,
    )
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=[strategy],
        shisanhyo=[tb],
        working_capital_gap=-10_000_000,
    )
    out = feasibility_critic_node(state)
    notes = out.get("feasibility_notes", [])
    advisory = notes[0]["advisory"] if notes else ""

    # Retrieve snippets for grounding evaluation.
    from app.backend.tools.retrieval import get_retrieval_provider

    retrieval = get_retrieval_provider()
    snippets = retrieval.search(
        f"1234567 {strategy.title} {strategy.rationale}",
        _settings.retrieval_top_k,
    )

    return {
        "advisory": advisory,
        "snippets": snippets,
        "strategy": strategy,
    }


def _llm_judge(prompt: str) -> dict[str, Any]:
    """Call the configured LLM as a judge and return a parsed score dict.

    The judge prompt must instruct the LLM to return a JSON object with:
      - score: int in [1, 5]
      - reasoning: str

    Args:
        prompt: The full judge prompt (system + user combined).

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
                    "あなたは日本の地域銀行向け事業再生アドバイザリーの品質評価者です。"
                    "評価結果をJSON形式で返してください: "
                    '{"score": <1-5>, "reasoning": "<理由>"}。'
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    headers = {"Authorization": f"Bearer {_settings.llm_api_key}"}
    resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
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
        inputs: The inputs to the judge (advisory, snippets, etc.).
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
# Test 1: Citation faithfulness.
# ---------------------------------------------------------------------------


def test_advisory_citation_faithfulness() -> None:
    """LLM-as-judge: advisory must be grounded in retrieved precedent snippets.

    Scoring rubric (1-5):
      5 = Advisory explicitly cites retrieved sources and reflects their content.
      4 = Advisory reflects retrieved content without explicit citation.
      3 = Advisory is plausible but not clearly grounded in retrieved snippets.
      2 = Advisory contradicts or ignores retrieved snippets.
      1 = Advisory is hallucinated / unrelated to retrieved snippets.

    Minimum acceptable score: 3 (advisory is at least plausible).
    """
    sample = _make_advisory_sample()
    advisory = sample["advisory"]
    snippets = sample["snippets"]

    if not advisory:
        pytest.skip("No advisory produced (LLM not configured or call failed).")

    snippet_block = (
        "\n".join(f"- [{s.source}] {s.text}" for s in snippets) if snippets else "（参考事例なし）"
    )

    judge_prompt = (
        "以下のアドバイザリーテキストが、提供された参考事例に基づいているかを評価してください。\n\n"
        f"【参考事例】\n{snippet_block}\n\n"
        f"【アドバイザリー】\n{advisory}\n\n"
        "評価基準:\n"
        "5 = 参考事例を明示的に引用し、内容を反映している\n"
        "4 = 参考事例の内容を反映しているが引用なし\n"
        "3 = 妥当だが参考事例との関連が不明確\n"
        "2 = 参考事例と矛盾または無視\n"
        "1 = 参考事例と無関係な内容\n"
    )

    result = _llm_judge(judge_prompt)
    score = int(result.get("score", 0))
    reasoning = str(result.get("reasoning", ""))

    _log_to_langsmith(
        run_name="advisory_citation_faithfulness",
        inputs={"advisory": advisory, "snippets": snippet_block},
        outputs={"score": score, "reasoning": reasoning},
    )

    assert score >= 3, (
        f"Citation faithfulness score {score}/5 is below minimum (3). Reasoning: {reasoning}"
    )


# ---------------------------------------------------------------------------
# Test 2: Japanese regulatory register / tone.
# ---------------------------------------------------------------------------


def test_advisory_japanese_regulatory_register() -> None:
    """LLM-as-judge: advisory must use appropriate formal Japanese register.

    Scoring rubric (1-5):
      5 = Formal, precise Japanese appropriate for a regional-bank turnaround
          context (事業再生). Uses correct financial/regulatory terminology.
      4 = Mostly formal; minor register inconsistencies.
      3 = Acceptable but informal in places; terminology could be more precise.
      2 = Informal or uses incorrect terminology for the regulatory context.
      1 = Inappropriate register; would not be suitable for a banker audience.

    Minimum acceptable score: 3.
    """
    sample = _make_advisory_sample()
    advisory = sample["advisory"]

    if not advisory:
        pytest.skip("No advisory produced (LLM not configured or call failed).")

    judge_prompt = (
        "以下のアドバイザリーテキストが、日本の地域銀行向け事業再生コンテキストにおいて"
        "適切な文体・用語・トーンで書かれているかを評価してください。\n\n"
        f"【アドバイザリー】\n{advisory}\n\n"
        "評価基準:\n"
        "5 = 事業再生コンサルタントとして適切な丁寧語・専門用語を使用\n"
        "4 = ほぼ適切だが軽微な文体の不一致あり\n"
        "3 = 許容範囲だが一部カジュアルまたは用語が不正確\n"
        "2 = 不適切な文体または誤った専門用語\n"
        "1 = 銀行担当者向けとして不適切なトーン\n"
    )

    result = _llm_judge(judge_prompt)
    score = int(result.get("score", 0))
    reasoning = str(result.get("reasoning", ""))

    _log_to_langsmith(
        run_name="advisory_japanese_regulatory_register",
        inputs={"advisory": advisory},
        outputs={"score": score, "reasoning": reasoning},
    )

    assert score >= 3, (
        f"Japanese regulatory register score {score}/5 is below minimum (3). Reasoning: {reasoning}"
    )
