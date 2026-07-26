#!/usr/bin/env python3
"""Exact one-observation stance adapter for the governed V5.2 claim stage."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from scripts import memory_v1_v5_2_compiler_v8_claim_stage as stage


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
SELF_ENTITY_ID = "35029129-27bd-457b-8cb5-82dd37ba32ba"
OBSERVATION_ID = "87ce1a11-01ae-4d6f-80ea-8e62b5b43cff"
EXPECTED_LITERAL = {
    "kind": "literal",
    "unit": None,
    "value": {
        "context": None,
        "position": "Fractal Monism will help people in life",
        "topic_key": "fractal_monism.application",
        "topic_text": "Fractal Monism",
        "orientation": "supports",
    },
    "datatype": "json",
    "approximate": False,
}
TARGETS = {
    OBSERVATION_ID: {
        "evidence_id": "049205b4-9a6c-5e1a-bb8f-2ab9f05f8964",
        "evidence_content_sha256": (
            "579427858fe9e181b02fc4c622a56cdda4f75e86356baf91664504a16396899d"
        ),
        "observation_sha256": (
            "28287f724e9d33f9a53fa18fcd644b5a3b7e512065aa14034586851238243879"
        ),
        "predicate": "stance.reported",
        "object_literal_sha256": (
            "ca265e3dbc1f92bf8ca4eab4e374487cca4fc03ebeb3d7fc9ece0b7973e729f3"
        ),
        "state_relation": "not_applicable",
        "canonical_text": (
            'The user reports this position: "Fractal Monism will help people '
            'in life."'
        ),
    }
}


async def source_snapshot(
    conn: Any, observation_id: str, expected_owner: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    target = TARGETS.get(observation_id)
    if target is None:
        raise stage.ProjectionStageError("observation is outside the exact stance target")
    source_row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
        uuid.UUID(observation_id),
    )
    entailment_row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_entailment_source_v5_2($1)",
        uuid.UUID(observation_id),
    )
    state_relation = await conn.fetchval(
        "SELECT memory.preflight_projection_temporal_state_v5_2($1)",
        uuid.UUID(observation_id),
    )
    if source_row is None or entailment_row is None or state_relation is None:
        raise stage.ProjectionStageError("exact owner-scoped stance source not found")
    source = dict(source_row)
    spans = entailment_row["source_spans"]
    for field in ("object_literal", "project_scope", "temporal"):
        if isinstance(source.get(field), str):
            source[field] = json.loads(source[field])
    if isinstance(spans, str):
        spans = json.loads(spans)
    if not isinstance(spans, list) or len(spans) != 1:
        raise stage.ProjectionStageError("exact stance source span is absent")
    source["temporal"]["state_relation"] = state_relation
    if (
        str(source["owner_user_id"]) != expected_owner
        or expected_owner != OWNER
        or str(source["evidence_id"]) != target["evidence_id"]
        or str(entailment_row["evidence_id"]) != target["evidence_id"]
        or source["evidence_content_sha256"] != target["evidence_content_sha256"]
        or entailment_row["observation_sha256"] != source["observation_sha256"]
        or source["observation_sha256"] != target["observation_sha256"]
        or source["predicate_registry_version"] != stage.REGISTRY_VERSION
        or source["predicate"] != target["predicate"]
        or source["projection_class"] != "reported_stance"
        or source["surface_policy"] != "relevant_recall_or_explicit_recall"
        or source["modality"] != "reported_belief"
        or source["polarity"] != "affirmed"
        or source["object_kind"] != "literal"
        or source["object_entity_id"] is not None
        or source["object_literal"] != EXPECTED_LITERAL
        or source["object_literal_sha256"] != target["object_literal_sha256"]
        or str(source["subject_entity_id"]) != SELF_ENTITY_ID
        or source["subject_entity_type"] != "self"
        or source["subject_entity_status"] != "active"
        or source["temporal"]["state_relation"] != target["state_relation"]
        or source["evidence_status"] != "active"
    ):
        raise stage.ProjectionStageError(
            "source is outside the exact attributed-stance boundary"
        )
    return source, spans, entailment_row["observation_ref"]


stage.TARGET_OWNER = OWNER
stage.SELF_ENTITY_ID = SELF_ENTITY_ID
stage.TARGETS = TARGETS
stage.EXPECTED_TABLE_ROWS = {
    "observation_entailment_v5": 1,
    "relational_operation_request": 1,
    "projection_plan": 1,
    "projection_plan_item": 1,
    "projection_claim_payload": 1,
    "projection_plan_observation": 1,
}
stage.EXPECTED_NEW_ROWS = 6
stage.ASSESSOR_REF = "controlled_v5_2_evidence_context_stance_claim_stage"
stage.CONFIRMATION = "STAGE_EXACT_ONE_EVIDENCE_CONTEXT_STANCE_CANDIDATE_ONLY"
stage.APPLY_ENV = "MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_STAGE_APPLY"
stage.source_snapshot = source_snapshot


if __name__ == "__main__":
    parsed = stage.arguments()
    if parsed.command == "authorize":
        raise SystemExit(stage.authorize_without_database(parsed))
    raise SystemExit(asyncio.run(stage.run()))
