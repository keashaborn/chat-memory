#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from scripts import memory_v1_v5_2_compiler_v8_claim_stage as stage


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OBSERVATION = "fc86c43e-3fa6-465e-b348-8656e3a896c0"
EVIDENCE = "a7065682-12c2-414f-9c6f-fdcb898ee1c8"
SELF_ENTITY = "35029129-27bd-457b-8cb5-82dd37ba32ba"


def configure() -> None:
    stage.MANIFEST_CONTRACT = (
        "memory_v1_v5_2_self_identity_name_claim_stage_manifest_v1"
    )
    stage.AUTHORIZATION_CONTRACT = (
        "memory_v1_v5_2_self_identity_name_claim_stage_authorization_v1"
    )
    stage.RESULT_CONTRACT = (
        "memory_v1_v5_2_self_identity_name_claim_stage_result_v1"
    )
    stage.EXPECTED_TABLE_ROWS = {
        "observation_entailment_v5": 1,
        "relational_operation_request": 1,
        "projection_plan": 1,
        "projection_plan_item": 1,
        "projection_claim_payload": 1,
        "projection_plan_observation": 1,
    }
    stage.EXPECTED_NEW_ROWS = 6
    stage.EXPECTED_ITEM_ROWS = 6
    stage.EXPECTED_EXISTING_AGGREGATES = 0
    stage.ENTAILMENT_APPLY_OUTCOME = "applied"
    stage.ENTAILMENT_REPLAY_OUTCOME = "replayed"
    stage.ASSESSOR_REF = "controlled_v5_2_self_identity_name_claim_stage"
    stage.CONFIRMATION = (
        "STAGE_EXACT_SELF_IDENTITY_NAME_CLAIM_CANDIDATE_ONLY"
    )
    stage.APPLY_ENV = (
        "MEMORY_V1_V5_2_SELF_IDENTITY_NAME_CLAIM_STAGE_APPLY"
    )
    stage.TARGET_OWNER = OWNER
    stage.TARGETS = {
        OBSERVATION: {
            "evidence_id": EVIDENCE,
            "evidence_content_sha256": (
                "ad9bed4219a0588bb9c794f6346a7643b79f0fa20cc9b5ab300bf04b45747a5a"
            ),
            "observation_sha256": (
                "93d372de66a3557dee5756191f18183a3e7b65de6d00ebc79558013f948ee6b7"
            ),
            "predicate": "identity.name",
            "state_relation": "not_applicable",
            "canonical_text": "The user's name is Eric.",
        }
    }


async def source_snapshot(
    conn: Any, observation_id: str, expected_owner: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    target = stage.TARGETS.get(observation_id)
    if target is None:
        raise stage.ProjectionStageError(
            "observation is outside the exact self-name target"
        )
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
        raise stage.ProjectionStageError(
            "exact owner-scoped self-name source not found"
        )
    source = dict(source_row)
    spans = entailment_row["source_spans"]
    for field in ("object_literal", "project_scope", "temporal"):
        if isinstance(source.get(field), str):
            source[field] = json.loads(source[field])
    if isinstance(spans, str):
        spans = json.loads(spans)
    if not isinstance(spans, list) or not spans:
        raise stage.ProjectionStageError(
            "self-name projection source spans are absent"
        )
    source["temporal"]["state_relation"] = state_relation
    expected_literal = {
        "kind": "literal",
        "datatype": "text",
        "value": "Eric",
        "unit": None,
        "approximate": False,
    }
    if (
        str(source["owner_user_id"]) != expected_owner
        or str(source["evidence_id"]) != EVIDENCE
        or str(entailment_row["evidence_id"]) != EVIDENCE
        or source["evidence_content_sha256"]
        != target["evidence_content_sha256"]
        or entailment_row["observation_sha256"]
        != source["observation_sha256"]
        or source["observation_sha256"] != target["observation_sha256"]
        or source["predicate_registry_version"]
        != "memory_predicate_registry_v5_2"
        or source["predicate"] != "identity.name"
        or source["projection_class"] != "direct_claim"
        or source["surface_policy"] != "direct_or_relevant"
        or source["modality"] != "asserted"
        or source["polarity"] != "affirmed"
        or source["object_kind"] != "literal"
        or source["object_literal"] != expected_literal
        or str(source["subject_entity_id"]) != SELF_ENTITY
        or source["subject_entity_type"] != "self"
        or source["subject_entity_status"] != "active"
        or source["subject_canonical_name"] != "Self"
        or source["object_entity_id"] is not None
        or source["temporal"]["state_relation"] != "not_applicable"
        or source["evidence_status"] != "active"
    ):
        raise stage.ProjectionStageError(
            "source is outside the exact self-name claim boundary"
        )
    return source, spans, entailment_row["observation_ref"]


if __name__ == "__main__":
    configure()
    stage.source_snapshot = source_snapshot
    parsed = stage.arguments()
    if parsed.command == "authorize":
        raise SystemExit(stage.authorize_without_database(parsed))
    raise SystemExit(asyncio.run(stage.run()))
