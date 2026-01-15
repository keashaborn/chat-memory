from __future__ import annotations

from typing import Any
from qdrant_client import QdrantClient


def make_qdrant_client(*args: Any, **kwargs: Any) -> QdrantClient:
    """
    Create a QdrantClient compatible with multiple qdrant-client versions.

    Some qdrant-client builds accept `check_compatibility` (newer), while older
    builds forward unknown kwargs into an internal Client class that errors with:
      TypeError: Client.__init__() got an unexpected keyword argument 'check_compatibility'
    """
    try:
        return QdrantClient(*args, check_compatibility=False, **kwargs)
    except TypeError as e:
        if "check_compatibility" not in str(e):
            raise
        # Retry without the kwarg for older clients.
        return QdrantClient(*args, **kwargs)
