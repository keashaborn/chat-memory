from __future__ import annotations

from typing import Callable, Dict, List, Sequence

from .openai_client import embed_text


class QueryEmbeddingCache:
    """Lazily create one embedding vector for all consumers in one route call."""

    def __init__(
        self,
        text: str,
        *,
        model: str = "text-embedding-3-large",
        embedder: Callable[..., Sequence[float]] | None = None,
    ) -> None:
        self._text = str(text or "")
        self._model = str(model or "text-embedding-3-large")
        self._embedder = embedder or embed_text
        self._vector: tuple[float, ...] | None = None
        self._provider_calls = 0
        self._consumer_reads = 0

    def get(self) -> List[float]:
        self._consumer_reads += 1
        if self._vector is None:
            values = self._embedder(self._text, model=self._model)
            vector = tuple(float(value) for value in values)
            if not vector:
                raise RuntimeError("embedding provider returned an empty vector")
            self._vector = vector
            self._provider_calls += 1
        return list(self._vector)

    def stats(self) -> Dict[str, int | str]:
        return {
            "version": "shared_query_embedding_v1",
            "provider_calls": self._provider_calls,
            "consumer_reads": self._consumer_reads,
        }
