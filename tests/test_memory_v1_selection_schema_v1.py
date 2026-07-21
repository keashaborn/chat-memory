from __future__ import annotations

import json
import unittest
from pathlib import Path

from rag_engine.memory_v1_selection_envelope import (
    ANSWER_BINDING_VERSION,
    CONTRACT_VERSION,
    PROMPT_ASSEMBLY_INPUT_VERSION,
    REQUEST_VERSION,
    FinalAnswerMemoryBindingV1,
    MemoryPromptAssemblyInputV1,
    MemorySelectionEnvelopeV1,
    MemorySelectionRequestV1,
)


ROOT = Path(__file__).resolve().parents[1]


def generated_schema(model: type, schema_id: str, title: str) -> dict:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = schema_id
    schema["title"] = title
    return schema


class MemorySelectionSchemaV1Tests(unittest.TestCase):
    def test_committed_request_schema_is_exactly_generated(self) -> None:
        path = ROOT / "specs" / "memory_selection_request_v1.schema.json"
        committed = json.loads(path.read_text(encoding="utf-8"))
        expected = generated_schema(
            MemorySelectionRequestV1,
            "https://verbalsage.internal/specs/memory_selection_request_v1.schema.json",
            "MemorySelectionRequestV1",
        )
        self.assertEqual(committed, expected)
        self.assertIn("contract_version", committed["required"])
        self.assertEqual(
            committed["properties"]["contract_version"]["const"],
            REQUEST_VERSION,
        )
        self.assertIn("query_text", committed["required"])
        self.assertIn("query_vector", committed["required"])

    def test_committed_envelope_schema_is_exactly_generated(self) -> None:
        path = ROOT / "specs" / "memory_selection_envelope_v1.schema.json"
        committed = json.loads(path.read_text(encoding="utf-8"))
        expected = generated_schema(
            MemorySelectionEnvelopeV1,
            "https://verbalsage.internal/specs/memory_selection_envelope_v1.schema.json",
            "MemorySelectionEnvelopeV1",
        )
        self.assertEqual(committed, expected)
        self.assertFalse(committed["additionalProperties"])
        self.assertEqual(
            committed["properties"]["contract_version"]["const"],
            CONTRACT_VERSION,
        )
        self.assertIn("contract_version", committed["required"])

    def test_committed_prompt_assembly_schema_is_exactly_generated(self) -> None:
        path = ROOT / "specs" / "memory_prompt_assembly_input_v1.schema.json"
        committed = json.loads(path.read_text(encoding="utf-8"))
        expected = generated_schema(
            MemoryPromptAssemblyInputV1,
            "https://verbalsage.internal/specs/memory_prompt_assembly_input_v1.schema.json",
            "MemoryPromptAssemblyInputV1",
        )
        self.assertEqual(committed, expected)
        self.assertIn("contract_version", committed["required"])
        self.assertEqual(
            committed["properties"]["contract_version"]["const"],
            PROMPT_ASSEMBLY_INPUT_VERSION,
        )

    def test_committed_binding_schema_is_exactly_generated(self) -> None:
        path = ROOT / "specs" / "final_answer_memory_binding_v1.schema.json"
        committed = json.loads(path.read_text(encoding="utf-8"))
        expected = generated_schema(
            FinalAnswerMemoryBindingV1,
            "https://verbalsage.internal/specs/final_answer_memory_binding_v1.schema.json",
            "FinalAnswerMemoryBindingV1",
        )
        self.assertEqual(committed, expected)
        self.assertFalse(committed["additionalProperties"])
        self.assertEqual(
            committed["properties"]["contract_version"]["const"],
            ANSWER_BINDING_VERSION,
        )
        self.assertIn("contract_version", committed["required"])

    def test_every_object_schema_is_closed(self) -> None:
        for model in (
            MemorySelectionRequestV1,
            MemorySelectionEnvelopeV1,
            MemoryPromptAssemblyInputV1,
            FinalAnswerMemoryBindingV1,
        ):
            schema = model.model_json_schema()
            objects = [schema, *schema.get("$defs", {}).values()]
            for item in objects:
                if item.get("type") == "object":
                    self.assertFalse(
                        item.get("additionalProperties", True),
                        msg=f"open object in {model.__name__}: {item.get('title')}",
                    )

    def test_memory_contract_has_no_fm_persona_or_vantage_authority(self) -> None:
        encoded = json.dumps(
            MemoryPromptAssemblyInputV1.model_json_schema(),
            sort_keys=True,
        )
        for forbidden in (
            '"response_mode"',
            '"fm"',
            '"persona"',
            '"vantage_id"',
            '"prompt_block"',
            '"memory_chunks"',
        ):
            self.assertNotIn(forbidden, encoded)

    def test_final_binding_contains_no_governed_prose_fields(self) -> None:
        encoded = json.dumps(
            FinalAnswerMemoryBindingV1.model_json_schema(),
            sort_keys=True,
        )
        for forbidden in (
            '"text"',
            '"query"',
            '"answer_text"',
            '"prompt"',
            '"evidence_text"',
        ):
            self.assertNotIn(forbidden, encoded)


if __name__ == "__main__":
    unittest.main()
