"""Versioned prompt registry for Saisei (Feature 1).

Deterministic, offline, single source of truth for the static prompt assets in
``app/backend/prompts/``. Today prompts are read ad hoc (e.g. the ``_load_prompt``
helper in ``nodes/critics/_persona.py``). This registry centralises that access
behind a versioned, reviewable interface so prompt changes are auditable and
rollback-able -- the ``versioned prompt registry`` Feature 1 calls for in
``docs/en/NEXT_STEPS.md``.

Design:

* Each logical prompt is registered as a :class:`PromptSpec` mapping a stable
  logical name (e.g. ``"critic_main_bank"``) to its on-disk file and a semantic
  ``version`` string. The version is metadata for audit/rollback; it does not
  change which file is read.
* :func:`get_prompt` returns the prompt text for a logical name, reading from
  the bundled prompts directory. It raises :class:`PromptNotFound` for an
  unknown name (a programming error), but callers that must stay best-effort
  offline (the LLM personas) can use :func:`get_prompt_or_empty`, which returns
  ``""`` on any miss -- mirroring the existing offline-fallback contract so
  ``make verify`` stays green with no LLM/network.
* :func:`content_hash` gives a stable digest of a prompt's bytes so a change to
  a prompt file is detectable in review / CI without committing the full text
  to a test.

The registry performs only local file reads; it never touches the network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PromptSpec",
    "PromptNotFound",
    "PROMPT_REGISTRY",
    "prompts_dir",
    "get_prompt",
    "get_prompt_or_empty",
    "content_hash",
]

#: Bundled static prompts directory (ships inside the app package).
_PROMPTS_DIR: Path = Path(__file__).resolve().parent


class PromptNotFound(KeyError):
    """Raised when a logical prompt name is not in the registry or on disk."""


@dataclass(frozen=True)
class PromptSpec:
    """A registered, versioned prompt asset.

    Attributes:
        name: Stable logical name used by call sites.
        filename: File within the prompts directory holding the prompt text.
        version: Semantic version string for audit / rollback (metadata only).
        description: Short human-readable purpose.
    """

    name: str
    filename: str
    version: str
    description: str


#: The versioned registry. Adding/bumping a prompt is a one-line, reviewable
#: change here. Versions start at 1.0.0; bump on any material prompt edit.
PROMPT_REGISTRY: dict[str, PromptSpec] = {
    spec.name: spec
    for spec in (
        PromptSpec(
            name="extraction_rules",
            filename="extraction_rules.md",
            version="1.0.0",
            description="Deterministic financial-extraction rules.",
        ),
        PromptSpec(
            name="kaizen_templates",
            filename="kaizen_templates.md",
            version="1.0.0",
            description="Keikakusho drafting templates.",
        ),
        PromptSpec(
            name="feasibility_critic",
            filename="feasibility_critic.md",
            version="1.0.0",
            description="Feasibility critic advisory prompt.",
        ),
        PromptSpec(
            name="critic_main_bank",
            filename="critic_main_bank.md",
            version="1.0.0",
            description="Main-bank (主幹事銀行) persona prompt.",
        ),
        PromptSpec(
            name="critic_sub_bank",
            filename="critic_sub_bank.md",
            version="1.0.0",
            description="Sub-bank (協調融資銀行) persona prompt.",
        ),
        PromptSpec(
            name="critic_guarantor",
            filename="critic_guarantor.md",
            version="1.0.0",
            description="Guarantor (信用保証協会) persona prompt.",
        ),
    )
}


def prompts_dir() -> Path:
    """Return the bundled prompts directory path."""
    return _PROMPTS_DIR


def _read(spec: PromptSpec) -> str:
    """Read a spec's prompt text from disk.

    Raises:
        PromptNotFound: If the file is missing.
    """
    path = _PROMPTS_DIR / spec.filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptNotFound(f"prompt file missing: {spec.filename}") from exc


def get_prompt(name: str) -> str:
    """Return the prompt text for a registered logical name.

    Args:
        name: Logical prompt name (a key of :data:`PROMPT_REGISTRY`).

    Returns:
        The prompt file contents.

    Raises:
        PromptNotFound: If ``name`` is not registered or its file is missing.
    """
    spec = PROMPT_REGISTRY.get(name)
    if spec is None:
        raise PromptNotFound(f"unknown prompt: {name!r}")
    return _read(spec)


def get_prompt_or_empty(name: str) -> str:
    """Return the prompt text, or ``""`` on any miss (offline-safe).

    Best-effort variant for callers (the LLM personas) that must degrade
    gracefully and never raise, mirroring the project's offline-fallback
    contract.

    Args:
        name: Logical prompt name.

    Returns:
        The prompt text, or an empty string if unregistered or unreadable.
    """
    try:
        return get_prompt(name)
    except PromptNotFound:
        return ""


def content_hash(name: str) -> str:
    """Return a stable SHA-256 hex digest of a registered prompt's bytes.

    Lets review / CI detect a prompt change without embedding the full text in
    a test.

    Args:
        name: Logical prompt name.

    Returns:
        The hex SHA-256 of the prompt file contents.

    Raises:
        PromptNotFound: If ``name`` is not registered or its file is missing.
    """
    return hashlib.sha256(get_prompt(name).encode("utf-8")).hexdigest()
