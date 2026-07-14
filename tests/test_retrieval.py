"""Tests for the two-tier agent-memory retrieval seam.

Covers the recall contract of :class:`TwoTierRetrievalProvider` (short-term
first, long-term fallback, write-back consolidation), the provider-selection
logic, and the shared response parser.
"""

from __future__ import annotations

from app.backend.tools.retrieval import (
    MockRetrievalProvider,
    RetrievalProvider,
    RetrievalSnippet,
    TwoTierRetrievalProvider,
    _parse_memory_hits,
    get_retrieval_provider,
)
from app.shared.settings import Settings


class _StubShortTerm:
    """Records calls and returns scripted recall / write-back behaviour."""

    def __init__(self, hits: list[RetrievalSnippet]) -> None:
        self._hits = hits
        self.search_calls: list[tuple[str, int]] = []
        self.remembered: list[tuple[str, list[RetrievalSnippet]]] = []

    def search(self, query: str, top_k: int) -> list[RetrievalSnippet]:
        self.search_calls.append((query, top_k))
        return self._hits

    def remember(self, query: str, snippets: list[RetrievalSnippet]) -> None:
        self.remembered.append((query, snippets))


class _StubLongTerm:
    """Records calls and returns scripted long-term recall."""

    def __init__(self, hits: list[RetrievalSnippet]) -> None:
        self._hits = hits
        self.search_calls: list[tuple[str, int]] = []

    def search(self, query: str, top_k: int) -> list[RetrievalSnippet]:
        self.search_calls.append((query, top_k))
        return self._hits


def _snippet(text: str, source: str = "benchmark") -> RetrievalSnippet:
    return RetrievalSnippet(source=source, text=text, score=0.9)


def test_short_term_hit_skips_long_term() -> None:
    """A short-term recall is returned without touching long-term memory."""
    short = _StubShortTerm([_snippet("recent precedent")])
    long = _StubLongTerm([_snippet("durable precedent")])
    provider = TwoTierRetrievalProvider(short_term=short, long_term=long)  # type: ignore[arg-type]

    result = provider.search("query", top_k=3)

    assert [s.text for s in result] == ["recent precedent"]
    assert short.search_calls == [("query", 3)]
    assert long.search_calls == []  # long-term never consulted on a hot hit
    assert short.remembered == []  # nothing to consolidate


def test_short_term_miss_falls_back_and_writes_back() -> None:
    """On a short-term miss, long-term recall is returned and consolidated."""
    short = _StubShortTerm([])
    durable = [_snippet("durable precedent")]
    long = _StubLongTerm(durable)
    provider = TwoTierRetrievalProvider(short_term=short, long_term=long)  # type: ignore[arg-type]

    result = provider.search("query", top_k=2)

    assert [s.text for s in result] == ["durable precedent"]
    assert long.search_calls == [("query", 2)]
    # Long-term hits are warmed back into short-term memory.
    assert short.remembered == [("query", durable)]


def test_both_tiers_empty_returns_no_precedents() -> None:
    """Both tiers missing yields an empty result and no write-back."""
    short = _StubShortTerm([])
    long = _StubLongTerm([])
    provider = TwoTierRetrievalProvider(short_term=short, long_term=long)  # type: ignore[arg-type]

    assert provider.search("query", top_k=3) == []
    assert short.remembered == []


def test_get_retrieval_provider_defaults_to_mock_offline() -> None:
    """With no memory tier configured, the deterministic mock is used."""
    settings = Settings(pgvector_dsn="", redisearch_url="")
    provider = get_retrieval_provider(settings)

    assert isinstance(provider, MockRetrievalProvider)
    assert isinstance(provider, RetrievalProvider)
    assert provider.search("anything", top_k=3) == []


def test_get_retrieval_provider_uses_two_tier_when_either_tier_set() -> None:
    """A single configured tier is enough to build the two-tier provider."""
    long_only = get_retrieval_provider(
        Settings(pgvector_dsn="http://pgvector:8080", redisearch_url="")
    )
    short_only = get_retrieval_provider(
        Settings(pgvector_dsn="", redisearch_url="http://redisearch:8080")
    )

    assert isinstance(long_only, TwoTierRetrievalProvider)
    assert isinstance(short_only, TwoTierRetrievalProvider)


def test_parse_memory_hits_tolerates_envelopes_and_skips_bad_hits() -> None:
    """The shared parser handles list/dict envelopes and skips unusable hits."""
    payload = {
        "results": [
            {"content": "from content field", "score": "0.5", "source": "fsa_manual"},
            {"text": "  spaced  ", "metadata": {"source": "benchmark"}},
            {"text": ""},  # empty -> skipped
            {"no_text_field": True},  # unusable -> skipped
            "not-a-dict",  # unusable -> skipped
        ]
    }

    hits = _parse_memory_hits(payload)

    assert [(h.text, h.source, h.score) for h in hits] == [
        ("from content field", "fsa_manual", 0.5),
        ("spaced", "benchmark", 0.0),
    ]


def test_parse_memory_hits_handles_bare_list_and_non_collection() -> None:
    """A bare list is accepted; a non-list/dict body yields no hits."""
    assert _parse_memory_hits([{"text": "bare"}]) == [
        RetrievalSnippet(source="unknown", text="bare", score=0.0)
    ]
    assert _parse_memory_hits("unexpected") == []
