"""Shared persona-layer LLM helper for the creditor-meeting critics (PART 4).

Each critic is a HYBRID: the existing deterministic gate decides PASS/FAIL and
the blocker codes (unchanged), and this helper adds an OPTIONAL, advisory-only
``simulated_argument`` — the persona's negotiating stance, for the banker's
rehearsal.

DESIGN CONTRACT (the one rule that governs everything):
- The deterministic verdict is the INPUT here, never the output. The LLM only
  phrases how this persona would *argue* the verdict; it never produces or
  alters ``status``, ``fatal_blockers``, ``priority`` or any figure.
- Best-effort with a deterministic offline fallback: returns ``""`` when no LLM
  is configured or on any error, so ``make verify`` stays green with no network
  (mirrors the ``polish_keikakusho`` contract).

This module centralises the OpenAI-compatible Chat Completions client so the
three critics don't each duplicate it.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = ["simulate_persona_argument"]

_log = get_logger(__name__)

#: Directory holding the static persona prompts (kept out of Python).
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _llm_configured(settings: Settings) -> bool:
    """Return whether an LLM is configured for the persona pass."""
    return bool(settings.llm_api_key and settings.llm_model)


def _load_prompt(prompt_name: str) -> str:
    """Load a static persona prompt by file name; empty string if unavailable."""
    try:
        return (_PROMPTS_DIR / prompt_name).read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        _log.warning("persona.prompt_missing", prompt=prompt_name, error=str(exc))
        return ""


def _call_llm(settings: Settings, system_prompt: str, user_content: str) -> str:
    """Request a persona argument via Chat Completions (polish_keikakusho pattern).

    Raises on any transport or shape error; the caller swallows it for the
    offline fallback.
    """
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.llm_model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    response = httpx.post(
        url, json=payload, headers=headers, timeout=settings.llm_timeout_seconds
    )
    response.raise_for_status()
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Unexpected LLM response shape") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Empty LLM response content")
    return content.strip()


def simulate_persona_argument(
    prompt_name: str,
    persona: str,
    status: str,
    blockers: list[str],
    rationale: str,
    settings: Settings | None = None,
) -> str:
    """Return an advisory persona argument for a deterministic verdict.

    ADVISORY ONLY. The deterministic verdict (``status`` / ``blockers`` /
    ``rationale``) is the INPUT; the returned string is the persona's simulated
    negotiating stance for the banker's rehearsal and is NEVER read by any gate
    or router.

    Best-effort: returns ``""`` when no LLM is configured, when the prompt is
    missing, or on any transport/response error (deterministic offline
    fallback).

    Args:
        prompt_name: File name of the persona prompt in ``app/backend/prompts``.
        persona: Persona identifier (for logging).
        status: Deterministic verdict ('PASS' or 'FAIL').
        blockers: Deterministic fatal-blocker strings (may be empty).
        rationale: Deterministic rationale string.
        settings: Optional settings override (defaults to cached settings).

    Returns:
        The simulated argument text, or ``""`` on the offline fallback.
    """
    settings = settings or get_settings()
    if not _llm_configured(settings):
        return ""

    system_prompt = _load_prompt(prompt_name)
    if not system_prompt:
        return ""

    blocker_block = "\n".join(f"- {b}" for b in blockers) if blockers else "（なし）"
    user_content = (
        f"決定論的評定: {status}\n"
        f"根拠: {rationale}\n"
        f"致命的ブロッカー:\n{blocker_block}\n"
    )

    try:
        argument = _call_llm(settings, system_prompt, user_content)
    except Exception as exc:  # noqa: BLE001 - persona layer is best-effort
        _log.warning("persona.simulate_failed", persona=persona, error=str(exc))
        return ""

    _log.info("persona.simulated", persona=persona, chars=len(argument))
    return argument
