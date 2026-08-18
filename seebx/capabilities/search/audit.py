from __future__ import annotations

"""Content-free hashing contract for search audit requests."""

import hashlib


def query_sha256(query: str) -> str:
    return hashlib.sha256(str(query).encode("utf-8")).hexdigest()


__all__ = ["query_sha256"]
