#!/usr/bin/env python3
from __future__ import annotations

from rag_engine.query_embedding_cache import QueryEmbeddingCache
from rag_engine.retriever_unified import _resolve_query_vector


def main() -> int:
    calls: list[tuple[str, str]] = []

    def fake_embedder(text: str, *, model: str) -> list[float]:
        calls.append((text, model))
        return [0.25, 0.5, 0.75]

    cache = QueryEmbeddingCache(
        "one route query",
        model="test-embedding-model",
        embedder=fake_embedder,
    )
    first = cache.get()
    second = cache.get()
    first[0] = 99.0
    third = cache.get()

    if calls != [("one route query", "test-embedding-model")]:
        raise AssertionError(f"expected one provider call, got {calls}")
    if second != [0.25, 0.5, 0.75] or third != [0.25, 0.5, 0.75]:
        raise AssertionError("cache did not return protected vector copies")
    if cache.stats() != {
        "version": "shared_query_embedding_v1",
        "provider_calls": 1,
        "consumer_reads": 3,
    }:
        raise AssertionError(f"unexpected cache stats: {cache.stats()}")

    supplied = _resolve_query_vector("no provider call", [1, 2.5, 3])
    if supplied != [1.0, 2.5, 3.0]:
        raise AssertionError(f"supplied query vector was not preserved: {supplied}")
    try:
        _resolve_query_vector("invalid", [])
    except ValueError:
        pass
    else:
        raise AssertionError("empty supplied query vector was accepted")

    print("query_embedding_cache: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
