from __future__ import annotations

"""Narrow adapters from subsystem-owned render results to prompt contributions."""

from rag_engine.memory_prompt_renderer_v1 import MemoryPromptRenderResultV1
from rag_engine.prompt_contribution_v1 import (
    PromptContributionKind,
    PromptContributionV1,
)


class PromptContributionAdapterError(RuntimeError):
    """Fail-closed error when a subsystem result cannot be revalidated."""


def memory_render_to_prompt_contribution(
    result: MemoryPromptRenderResultV1,
) -> PromptContributionV1 | None:
    """Convert an exact Memory render without losing its separate audit object.

    Controls-only and empty selections intentionally return no model-facing
    contribution.  Callers retain ``result`` for binding and control auditing.
    """

    if not isinstance(result, MemoryPromptRenderResultV1):
        raise PromptContributionAdapterError(
            "expected MemoryPromptRenderResultV1"
        )
    try:
        verified = MemoryPromptRenderResultV1.model_validate_json(
            result.model_dump_json()
        )
    except Exception as exc:
        raise PromptContributionAdapterError(
            "invalid MemoryPromptRenderResultV1"
        ) from exc
    if not verified.content:
        return None
    return PromptContributionV1.create(
        contribution_id="governed_memory.render.v1",
        kind=PromptContributionKind.MEMORY,
        source_version=verified.renderer_version,
        content=verified.content,
    )


__all__ = [
    "PromptContributionAdapterError",
    "memory_render_to_prompt_contribution",
]
