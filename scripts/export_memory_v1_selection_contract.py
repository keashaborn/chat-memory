from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_selection_envelope import (
    FinalAnswerMemoryBindingV1,
    MemoryPromptAssemblyInputV1,
    MemorySelectionEnvelopeV1,
    MemorySelectionRequestV1,
)


SCHEMAS = (
    (
        MemorySelectionRequestV1,
        ROOT / "specs" / "memory_selection_request_v1.schema.json",
        "https://verbalsage.internal/specs/memory_selection_request_v1.schema.json",
        "MemorySelectionRequestV1",
    ),
    (
        MemorySelectionEnvelopeV1,
        ROOT / "specs" / "memory_selection_envelope_v1.schema.json",
        "https://verbalsage.internal/specs/memory_selection_envelope_v1.schema.json",
        "MemorySelectionEnvelopeV1",
    ),
    (
        MemoryPromptAssemblyInputV1,
        ROOT / "specs" / "memory_prompt_assembly_input_v1.schema.json",
        "https://verbalsage.internal/specs/memory_prompt_assembly_input_v1.schema.json",
        "MemoryPromptAssemblyInputV1",
    ),
    (
        FinalAnswerMemoryBindingV1,
        ROOT / "specs" / "final_answer_memory_binding_v1.schema.json",
        "https://verbalsage.internal/specs/final_answer_memory_binding_v1.schema.json",
        "FinalAnswerMemoryBindingV1",
    ),
)


def main() -> None:
    for model, path, schema_id, title in SCHEMAS:
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = schema_id
        schema["title"] = title
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
