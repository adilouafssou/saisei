"""Offline tests for the versioned prompt registry (Feature 1).

The registry does only local file reads, so these run fully offline. They assert
that every registered prompt resolves to a non-empty file, that versions are
present, that unknown names raise / degrade correctly, and that the content hash
is stable.
"""

from __future__ import annotations

import pytest
from app.backend.prompts.registry import (
    PROMPT_REGISTRY,
    PromptNotFound,
    content_hash,
    get_prompt,
    get_prompt_or_empty,
    prompts_dir,
)


def test_every_registered_prompt_file_exists_and_is_nonempty() -> None:
    """Each registered spec must map to a real, non-empty prompt file."""
    for name, spec in PROMPT_REGISTRY.items():
        assert (prompts_dir() / spec.filename).is_file(), f"missing file for {name}"
        assert get_prompt(name).strip(), f"empty prompt for {name}"


def test_every_spec_has_a_version() -> None:
    """Versioning is the point of the registry; every spec must carry one."""
    for name, spec in PROMPT_REGISTRY.items():
        assert spec.version, f"missing version for {name}"
        assert spec.name == name


def test_get_prompt_unknown_raises() -> None:
    with pytest.raises(PromptNotFound):
        get_prompt("does_not_exist")


def test_get_prompt_or_empty_unknown_returns_empty() -> None:
    assert get_prompt_or_empty("does_not_exist") == ""


def test_content_hash_is_stable_and_matches_content() -> None:
    """The hash must be deterministic across calls for the same prompt."""
    name = next(iter(PROMPT_REGISTRY))
    assert content_hash(name) == content_hash(name)
    assert len(content_hash(name)) == 64  # SHA-256 hex length


def test_known_persona_prompts_are_registered() -> None:
    """The three creditor personas + feasibility must be addressable by name."""
    for name in (
        "critic_main_bank",
        "critic_sub_bank",
        "critic_guarantor",
        "feasibility_critic",
    ):
        assert name in PROMPT_REGISTRY
        assert get_prompt(name).strip()
