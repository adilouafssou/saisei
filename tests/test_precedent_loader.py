"""Verifier for the precedent document loader + chunker.

The loader turns a bank's back-catalogue (Markdown / text files) into the
``PrecedentDoc`` records the existing pgvector ingest consumes. These tests pin
the properties that matter for a reproducible, idempotent RAG corpus:

- deterministic, paragraph-aware chunking (same text -> same chunks);
- source label inferred from the top-level subdirectory (access control by
  folder), with a safe default;
- stable, idempotent ``doc_id``s (so re-ingest replaces a file's chunks, never
  duplicates them);
- pure/offline loading, and the ingest wrapper is a no-op when pgvector is
  unconfigured (so ``make verify`` stays offline);
- a clear error when the corpus root is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.backend.tools.precedent_loader import (
    DEFAULT_SOURCE,
    chunk_text,
    ingest_precedent_directory,
    load_precedent_docs,
)
from app.shared.settings import Settings


def test_chunk_text_is_deterministic_and_paragraph_aware() -> None:
    """Blank-line paragraphs pack into chunks; same input -> same output."""
    text = "段落一。\n\n段落二。\n\n段落三。"
    a = chunk_text(text, target_chars=1000)
    b = chunk_text(text, target_chars=1000)
    assert a == b
    # All three short paragraphs fit one chunk at a large target.
    assert len(a) == 1
    assert "段落一" in a[0] and "段落三" in a[0]


def test_chunk_text_splits_on_target() -> None:
    """Paragraphs exceeding the target roll into separate chunks."""
    paras = "\n\n".join("x" * 50 for _ in range(10))
    chunks = chunk_text(paras, target_chars=120)
    assert len(chunks) > 1
    # No chunk wildly exceeds the target (each para is well under the hard cap).
    assert all(len(c) <= 160 for c in chunks)


def test_chunk_text_empty() -> None:
    """Empty / whitespace-only text yields no chunks."""
    assert chunk_text("") == []
    assert chunk_text("   \n\n   ") == []


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_source_inferred_from_subdirectory(tmp_path: Path) -> None:
    """The top-level folder sets each chunk's source label."""
    _write(tmp_path, "benchmark/mfg.md", "ベンチマーク本文。")
    _write(tmp_path, "fsa_manual/horizon.txt", "監督指針本文。")
    _write(tmp_path, "past_keikakusho/aichi.md", "計画書本文。")

    by_source = {d.doc_id: d.source for d in load_precedent_docs(tmp_path)}
    assert by_source["benchmark/benchmark/mfg.md#0"] == "benchmark"
    assert by_source["fsa_manual/fsa_manual/horizon.txt#0"] == "fsa_manual"
    assert by_source["past_keikakusho/past_keikakusho/aichi.md#0"] == "past_keikakusho"


def test_unknown_subdirectory_uses_default_source(tmp_path: Path) -> None:
    """A file outside a recognised folder falls back to the default source."""
    _write(tmp_path, "misc/note.md", "分類不明の本文。")
    docs = load_precedent_docs(tmp_path)
    assert len(docs) == 1
    assert docs[0].source == DEFAULT_SOURCE


def test_doc_ids_are_stable_and_idempotent(tmp_path: Path) -> None:
    """Re-loading the same tree yields identical ids (idempotent upsert key)."""
    _write(tmp_path, "benchmark/a.md", "段落一。\n\n段落二。")
    first = load_precedent_docs(tmp_path)
    second = load_precedent_docs(tmp_path)
    assert [d.doc_id for d in first] == [d.doc_id for d in second]
    # Chunk index is part of the id.
    assert first[0].doc_id.endswith("#0")


def test_only_text_files_are_loaded(tmp_path: Path) -> None:
    """Non-text files are ignored."""
    _write(tmp_path, "benchmark/keep.md", "本文。")
    _write(tmp_path, "benchmark/skip.pdf", "binary-ish")
    docs = load_precedent_docs(tmp_path)
    assert len(docs) == 1
    assert docs[0].doc_id == "benchmark/benchmark/keep.md#0"


def test_missing_root_raises(tmp_path: Path) -> None:
    """A missing corpus root is a clear error, not a silent empty load."""
    with pytest.raises(NotADirectoryError):
        load_precedent_docs(tmp_path / "does-not-exist")


def test_ingest_is_offline_noop_without_pgvector(tmp_path: Path) -> None:
    """With no pgvector DSN, ingest is a no-op (returns 0) and touches no DB."""
    _write(tmp_path, "benchmark/a.md", "本文。")
    settings = Settings(pgvector_dsn="")
    assert ingest_precedent_directory(tmp_path, settings=settings) == 0
