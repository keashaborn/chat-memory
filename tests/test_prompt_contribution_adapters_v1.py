from __future__ import annotations

import asyncio
import unittest

from rag_engine.memory_prompt_renderer_v1 import (
    MEMORY_PROMPT_RENDERER_VERSION,
    render_governed_memory_v1,
)
from rag_engine.memory_v1_selection_envelope import (
    MemoryPromptAssemblyContextV1,
    MemoryPromptAssemblyInputV1,
    SelectionDirective,
    select_governed_memory_v1,
)
from rag_engine.prompt_contribution_adapters_v1 import (
    PromptContributionAdapterError,
    memory_render_to_prompt_contribution,
)
from rag_engine.prompt_contribution_v1 import PromptContributionKind
from tests.test_memory_v1_selection_envelope_v1 import OWNER, request, selector


def rendered(*, suppress: bool = False):
    envelope = asyncio.run(
        select_governed_memory_v1(
            selector(),
            request(
                directive=(
                    SelectionDirective.SUPPRESS
                    if suppress
                    else SelectionDirective.EVALUATE
                )
            ),
        )
    )
    context = MemoryPromptAssemblyContextV1.from_envelope(
        envelope=envelope,
        authenticated_actor_user_id=OWNER,
        renderer_version=MEMORY_PROMPT_RENDERER_VERSION,
    )
    memory_input = MemoryPromptAssemblyInputV1.create(
        context=context,
        envelope=envelope,
    )
    return render_governed_memory_v1(memory_input=memory_input)


class PromptContributionAdaptersV1Test(unittest.TestCase):
    def test_memory_render_becomes_reference_contribution(self) -> None:
        source = rendered()
        contribution = memory_render_to_prompt_contribution(source)
        self.assertIsNotNone(contribution)
        assert contribution is not None
        self.assertEqual(contribution.kind, PromptContributionKind.MEMORY)
        self.assertEqual(contribution.content, source.content)
        self.assertEqual(contribution.source_version, source.renderer_version)

    def test_empty_or_suppressed_memory_adds_no_prompt_content(self) -> None:
        source = rendered(suppress=True)
        self.assertEqual(source.content, "")
        self.assertIsNone(memory_render_to_prompt_contribution(source))

    def test_forged_render_manifest_fails_closed(self) -> None:
        source = rendered()
        forged = source.model_copy(update={"content_sha256": "0" * 64})
        with self.assertRaises(PromptContributionAdapterError):
            memory_render_to_prompt_contribution(forged)


if __name__ == "__main__":
    unittest.main()
