"""Load a bank's precedent back-catalogue into long-term agent memory.

The RAG corpus (consumed by the feasibility critic's advisory note and the
summonable companion's “compare to a similar past case” answer) ships only a tiny
bundled seed (``fixtures/rag_seed_corpus.json``). A real deployment must ingest
the bank's actual back-catalogue — successful 経営改善計画書, industry benchmarks,
and the FSA inspection manual — which live as **documents** (Markdown / plain
text), not as hand-authored JSON snippets.

This module is the missing loader: it walks a directory of ``.md`` / ``.txt``
files, splits each into deterministic, paragraph-aware chunks, and produces the
existing :class:`~app.backend.tools.retrieval_ingest.PrecedentDoc` records that
:func:`~app.backend.tools.retrieval_ingest.ingest_documents` already embeds and
upserts into pgvector long-term memory.

Design choices (consistent with the rest of the stack):

- **Pure / offline to load.** Walking + chunking touches no network and no
  database; only the downstream ``ingest_documents`` call does, and only when
  ``SAISEI_PGVECTOR_DSN`` is configured. ``make verify`` stays offline.
- **Deterministic chunking.** Paragraph-aware (split on blank lines) packed to a
  character budget, no NLP dependency — same text always yields the same chunks,
  so re-ingest is reproducible and the embeddings are stable.
- **Idempotent ids.** ``doc_id = "<source>/<relpath>#<chunk_index>"`` is stable
  across runs, so the existing ``ON CONFLICT (doc_id)`` upsert means re-ingesting
  an updated file replaces exactly its chunks (no duplicates).
- **Per-source access control by layout.** The ``source`` label
  (``past_keikakusho`` / ``benchmark`` / ``fsa_manual``) is inferred from the
  top-level subdirectory, so a bank organises access by folder and the label
  flows through to every chunk (and is what the grounding gate cites). Retrieval
  stays **advisory-only** — a precedent never moves a band, score, gate, or route.

Run as a module to ingest a directory tree::

    uv run python -m app.backend.tools.precedent_loader /path/to/corpus

This module is the canonical location under
``app.backend.tools.precedent_loader``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from app.backend.tools.retrieval_ingest import PrecedentDoc, ingest_documents
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "KNOWN_SOURCES",
    "DEFAULT_SOURCE",
    "chunk_text",
    "load_precedent_docs",
    "ingest_precedent_directory",
]

_log = get_logger(__name__)

#: Recognised source labels, matched to the top-level subdirectory a file sits
#: under. They mirror the ``source`` values the seed corpus + grounding gate use.
KNOWN_SOURCES: frozenset[str] = frozenset({"past_keikakusho", "benchmark", "fsa_manual"})

#: Fallback source label for files not under a recognised subdirectory.
DEFAULT_SOURCE: str = "past_keikakusho"

#: File extensions treated as precedent documents.
_TEXT_SUFFIXES: frozenset[str] = frozenset({".md", ".txt"})

#: Default target chunk size (characters). Chosen for CJK prose: large enough to
#: keep a coherent passage, small enough to embed precisely. Paragraphs are
#: never split mid-paragraph unless a single paragraph exceeds the hard cap.
_DEFAULT_CHUNK_CHARS = 600

#: Hard cap so a single very long paragraph is still split (defensive).
_HARD_CHUNK_CHARS = 1200

#: Paragraph separator: one or more blank lines (tolerant of trailing spaces).
_PARA_SPLIT = re.compile(r"\n\s*\n+")


def chunk_text(text: str, *, target_chars: int = _DEFAULT_CHUNK_CHARS) -> list[str]:
    """Split document text into deterministic, paragraph-aware chunks.

    Paragraphs (blank-line separated) are packed in order until adding the next
    would exceed ``target_chars``; an over-long single paragraph is hard-split at
    ``_HARD_CHUNK_CHARS``. Pure and deterministic: the same text always yields
    the same chunks, so embeddings and ``doc_id``s are stable across runs.

    Args:
        text: The raw document text.
        target_chars: Soft target chunk size in characters.

    Returns:
        Non-empty, whitespace-trimmed chunks in document order.
    """
    paragraphs = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        # Hard-split a single paragraph that alone exceeds the hard cap.
        if len(para) > _HARD_CHUNK_CHARS:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            for i in range(0, len(para), _HARD_CHUNK_CHARS):
                chunks.append(para[i : i + _HARD_CHUNK_CHARS].strip())
            continue
        if not buffer:
            buffer = para
        elif len(buffer) + 2 + len(para) <= target_chars:
            buffer = f"{buffer}\n\n{para}"
        else:
            chunks.append(buffer)
            buffer = para
    if buffer:
        chunks.append(buffer)
    return [c for c in chunks if c]


def _source_for(relpath: Path) -> str:
    """Infer the source label from a file's top-level subdirectory.

    A file at ``benchmark/manufacturing/2024.md`` -> ``benchmark``; a file with
    no recognised top-level folder falls back to :data:`DEFAULT_SOURCE`.
    """
    parts = relpath.parts
    if parts and parts[0] in KNOWN_SOURCES:
        return parts[0]
    return DEFAULT_SOURCE


def _doc_id(source: str, relpath: Path, index: int) -> str:
    """Build a stable, idempotent doc id for one chunk of one file.

    ``<source>/<relpath-as-posix>#<chunk_index>`` — stable across runs so the
    existing ``ON CONFLICT (doc_id)`` upsert replaces a file's chunks in place
    when it is re-ingested (no duplicates).
    """
    return f"{source}/{relpath.as_posix()}#{index}"


def load_precedent_docs(
    root: Path | str,
    *,
    target_chars: int = _DEFAULT_CHUNK_CHARS,
) -> list[PrecedentDoc]:
    """Walk ``root`` and load every ``.md`` / ``.txt`` file as precedent chunks.

    Pure and offline (no DB / network): reads files, infers each file's source
    from its top-level subdirectory, chunks the text deterministically, and emits
    :class:`PrecedentDoc` records with stable ids. Files are visited in sorted
    path order so the output is deterministic. Empty / whitespace-only files
    yield nothing.

    Args:
        root: Directory tree to walk.
        target_chars: Soft target chunk size passed to :func:`chunk_text`.

    Returns:
        The precedent documents, ready for
        :func:`~app.backend.tools.retrieval_ingest.ingest_documents`.

    Raises:
        NotADirectoryError: If ``root`` does not exist or is not a directory.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise NotADirectoryError(f"precedent corpus root not found: {root_path}")

    docs: list[PrecedentDoc] = []
    files = sorted(
        p for p in root_path.rglob("*") if p.is_file() and p.suffix.lower() in _TEXT_SUFFIXES
    )
    for path in files:
        relpath = path.relative_to(root_path)
        source = _source_for(relpath)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _log.warning("precedent.read_failed", path=str(path), error=str(exc))
            continue
        for index, chunk in enumerate(chunk_text(text, target_chars=target_chars)):
            docs.append(
                PrecedentDoc(
                    doc_id=_doc_id(source, relpath, index),
                    source=source,
                    text=chunk,
                )
            )
    _log.info("precedent.loaded", root=str(root_path), files=len(files), docs=len(docs))
    return docs


def ingest_precedent_directory(
    root: Path | str,
    *,
    settings: Settings | None = None,
    target_chars: int = _DEFAULT_CHUNK_CHARS,
) -> int:
    """Load a directory of precedent documents and upsert them into long-term memory.

    Composes :func:`load_precedent_docs` (pure) with the existing
    :func:`~app.backend.tools.retrieval_ingest.ingest_documents` (the embed +
    pgvector upsert). A no-op returning 0 when pgvector is unconfigured, so this
    is safe to call offline; raises on a real database error during a configured
    run.

    Args:
        root: Directory tree of ``.md`` / ``.txt`` precedent files.
        settings: Optional settings override (defaults to cached settings).
        target_chars: Soft target chunk size.

    Returns:
        The number of chunks upserted (0 when pgvector is unconfigured / empty).
    """
    docs = load_precedent_docs(root, target_chars=target_chars)
    return ingest_documents(docs, settings=settings)


if __name__ == "__main__":  # pragma: no cover - manual operational entry point
    if len(sys.argv) != 2:
        sys.stderr.write("usage: python -m app.backend.tools.precedent_loader <corpus-dir>\n")
        raise SystemExit(2)
    _settings = get_settings()
    if not _settings.pgvector_dsn:
        sys.stderr.write(
            "error: SAISEI_PGVECTOR_DSN is not set; nothing was ingested.\n"
            "Configure pgvector long-term memory before ingesting a corpus.\n"
        )
        raise SystemExit(1)
    _count = ingest_precedent_directory(sys.argv[1], settings=_settings)
    _log.info("precedent.cli_done", count=_count)
    sys.stdout.write(f"ingested {_count} precedent chunk(s) into long-term memory\n")
