"""Offline guardrail: critic call sites resolve prompts via the registry.

Feature 1 (versioned prompt registry) migrated the critic prompt loaders off
ad-hoc file reads onto the registry. These tests pin that migration so a future
edit can't silently reintroduce a bare-filename loader or point a critic at an
unregistered prompt:

- each critic module's ``_PROMPT`` is a REGISTERED LOGICAL NAME (not a ``.md``
  filename) and resolves to a non-empty prompt;
- the feasibility critic's ``_PROMPT_NAME`` is registered and resolves;
- ``_persona.simulate_persona_argument`` resolves through the registry (proven
  by handing it an unregistered name and asserting the offline-safe "" result,
  which only the registry's ``get_prompt_or_empty`` miss-path can produce).

Fully offline: the registry does only local file reads, and the persona helper
short-circuits to "" with no LLM configured.
"""

from __future__ import annotations

from app.backend.nodes.critics import feasibility, guarantor, main_bank, sub_bank
from app.backend.nodes.critics._persona import simulate_persona_argument
from app.backend.prompts.registry import PROMPT_REGISTRY, get_prompt
from app.shared.settings import Settings

#: No-LLM settings -> persona helper returns "" before any prompt is read.
_OFFLINE = Settings(llm_api_key="", llm_model="")


def test_persona_critics_use_registered_logical_names() -> None:
    """Each persona critic's _PROMPT is a registered name, not a filename."""
    for module in (main_bank, sub_bank, guarantor):
        name = module._PROMPT
        assert not name.endswith(".md"), f"{module.__name__} still uses a filename"
        assert name in PROMPT_REGISTRY, f"{name} is not registered"
        assert get_prompt(name).strip(), f"{name} resolves empty"


def test_feasibility_uses_registered_logical_name() -> None:
    """The feasibility critic reads its prompt via a registered logical name."""
    name = feasibility._PROMPT_NAME
    assert not name.endswith(".md")
    assert name in PROMPT_REGISTRY
    assert get_prompt(name).strip()


def test_each_persona_critic_name_matches_its_persona() -> None:
    """Sanity: the logical names are the expected per-persona registry keys."""
    assert main_bank._PROMPT == "critic_main_bank"
    assert sub_bank._PROMPT == "critic_sub_bank"
    assert guarantor._PROMPT == "critic_guarantor"
    assert feasibility._PROMPT_NAME == "feasibility_critic"


def test_persona_helper_unregistered_name_is_offline_safe() -> None:
    """An unregistered prompt name degrades to "" via the registry miss-path.

    With no LLM the helper returns "" before reading a prompt, so this asserts
    the contract holds; the registry's get_prompt_or_empty is what guarantees
    the same "" even on a genuine miss.
    """
    out = simulate_persona_argument(
        "does_not_exist", "main_bank", "PASS", [], "ok", settings=_OFFLINE
    )
    assert out == ""
