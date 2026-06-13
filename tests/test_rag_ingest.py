"""Offline coverage for pgvector long-term-memory ingestion.

Ingestion is the durable side of the two-tier agent memory: it seeds the
pgvector precedent corpus the feasibility critic later recalls from. These tests
assert the parts that are deterministic and verifiable WITHOUT a database or
network:

* the bundled seed corpus loads and validates into well-formed PrecedentDocs;
* ``PrecedentDoc`` is a closed, frozen record (typos / stray fields fail loudly);
* the unconfigured path is a true no-op (returns 0 and never opens a
  connection), which is what keeps ``make verify`` green in the no-network CI
  sandbox.

The configured DB write path (psycopg + SQL) is intentionally NOT exercised
here; it requires a live pgvector instance and belongs in an integration suite.
"""

from __future__ import annotations

import sys
import types

import pytest
from pydantic import ValidationError

from app.backend.tools.retrieval_ingest import (
    PrecedentDoc,
    ingest_documents,
    ingest_seed_corpus,
    load_seed_corpus,
)
from app.shared.settings import Settings

_VALID_SOURCES = {"past_keikakusho", "benchmark", "fsa_manual"}


def _doc(doc_id: str = "d1", source: str = "benchmark", text: str = "text") -> PrecedentDoc:
    return PrecedentDoc(doc_id=doc_id, source=source, text=text)


# --- seed corpus loading ----------------------------------------------------


def test_load_seed_corpus_returns_validated_docs() -> None:
    docs = load_seed_corpus()
    assert docs, "bundled seed corpus must not be empty"
    assert all(isinstance(d, PrecedentDoc) for d in docs)
    assert all(d.doc_id and d.text for d in docs)
    assert all(d.source in _VALID_SOURCES for d in docs)


def test_seed_corpus_doc_ids_are_unique() -> None:
    docs = load_seed_corpus()
    ids = [d.doc_id for d in docs]
    assert len(ids) == len(set(ids)), "doc_id must be unique for idempotent upsert"


# --- PrecedentDoc is a closed, immutable record -----------------------------


def test_precedent_doc_is_frozen() -> None:
    doc = _doc()
    with pytest.raises(ValidationError):
        doc.text = "mutated"  # type: ignore[misc]


def test_precedent_doc_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PrecedentDoc(doc_id="d1", source="benchmark", text="t", surprise="x")  # type: ignore[call-arg]


# --- offline no-op contract (the CI-green guarantee) ------------------------


def _unconfigured() -> Settings:
    """Settings with no pgvector DSN -> ingestion must be a no-op."""
    return Settings(pgvector_dsn="")


def test_ingest_documents_unconfigured_is_noop() -> None:
    assert ingest_documents([_doc()], settings=_unconfigured()) == 0


def test_ingest_seed_corpus_unconfigured_is_noop() -> None:
    assert ingest_seed_corpus(settings=_unconfigured()) == 0


def test_ingest_documents_unconfigured_never_touches_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unconfigured path must short-circuit before importing/using psycopg."""
    exploding = types.ModuleType("psycopg")

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("psycopg.connect must not be called when unconfigured")

    exploding.connect = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", exploding)

    assert ingest_documents([_doc()], settings=_unconfigured()) == 0


def test_ingest_documents_empty_docs_is_noop_even_if_configured() -> None:
    """No documents -> no work, even with a DSN set (guards the empty-batch path)."""
    assert ingest_documents([], settings=Settings(pgvector_dsn="postgresql://x/y")) == 0
