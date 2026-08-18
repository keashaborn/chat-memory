from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from seebx.adapters.lifeswitch_openai_chat import (
    OpenAIChatMessageV2,
    OpenAIChatRequestV4,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import ACTOR, new_plan
from tests.test_lifeswitch_prompt_integration_v2 import zep_plan


class OpenAIChatRequestV4Tests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_memory_reference_name_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            OpenAIChatMessageV2(
                role="user",
                name="governed_memory_v1",
                content="retired",
            )

    async def test_prior_provenance_is_named_lower_authority_reference_data(self) -> None:
        plan = await new_plan(
            "Did you access my LifeSwitch nutrition day for Monday?", with_prior=True
        )
        request = OpenAIChatRequestV4.create(source_plan=plan)
        named = tuple(item.name for item in request.messages if item.name is not None)
        self.assertEqual(
            named,
            ("lifeswitch_domain_context_v1", "prior_lifeswitch_provenance_v1"),
        )
        self.assertFalse(request.store)
        exported = json.dumps(request.provider_kwargs(), sort_keys=True)
        self.assertNotIn(str(ACTOR), exported)

    async def test_zep_memory_is_named_lower_authority_reference_data(self) -> None:
        request = OpenAIChatRequestV4.create(source_plan=await zep_plan())
        named = tuple(item.name for item in request.messages if item.name is not None)
        self.assertEqual(named, ("zep_memory_v1",))


if __name__ == "__main__":
    unittest.main()
