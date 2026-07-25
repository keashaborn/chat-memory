#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from scripts import memory_v1_v5_2_compiler_v8_claim_stage as stage
from scripts.memory_v1_v5_2_projection_dispatch import (
    build_canonical_name_reinforcement_packet,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OBSERVATION = "5261da41-f863-42cd-8e3f-6e947f9743f2"
EVIDENCE = "33126656-fc5a-5fc1-a035-246b14576ee5"
ENTITY = "09308a2b-3019-4f59-8fc3-bb1fe1408a0d"
CLAIM = "8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9"
CLAIM_REVISION = 2

stage.MANIFEST_CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_stage_manifest_v1"
)
stage.AUTHORIZATION_CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_stage_authorization_v1"
)
stage.RESULT_CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_stage_result_v1"
)
stage.EXPECTED_TABLE_ROWS = {
    "observation_entailment_v5": 1,
    "relational_operation_request": 1,
    "projection_plan": 1,
    "projection_plan_item": 1,
    "projection_claim_payload": 1,
    "projection_plan_observation": 1,
}
stage.EXPECTED_NEW_ROWS = sum(stage.EXPECTED_TABLE_ROWS.values())
stage.EXPECTED_ITEM_ROWS = 6
stage.EXPECTED_EXISTING_AGGREGATES = 1
stage.ENTAILMENT_APPLY_OUTCOME = "applied"
stage.ENTAILMENT_REPLAY_OUTCOME = "replayed"
stage.ASSESSOR_REF = "controlled_v5_2_neko_correction_reinforcement"
stage.CONFIRMATION = "STAGE_EXACT_NEKO_CORRECTION_REINFORCEMENT_ONLY"
stage.APPLY_ENV = "MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_STAGE_APPLY"
stage.TARGET_OWNER = OWNER
stage.TARGETS = {
    OBSERVATION: {
        "evidence_id": EVIDENCE,
        "evidence_content_sha256": (
            "be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4"
        ),
        "observation_sha256": (
            "847c8ddf93e6bd10e939d36c9c8bd1d117a80561c388930a874f2dac2ee5815c"
        ),
        "predicate": "identity.name_canonical",
        "state_relation": "current",
        "canonical_text": "Neko's canonical name is Neko.",
    }
}


def build_projection_packet(
    owner_user_id: str, source: dict[str, Any]
) -> dict[str, Any]:
    return build_canonical_name_reinforcement_packet(
        owner_user_id,
        source,
        target_claim_id=CLAIM,
        expected_revision_number=CLAIM_REVISION,
    )


async def preflight_projection(
    conn: Any, plan_id: str, packet_text: str
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """SELECT *
           FROM memory.preflight_projection_reinforcement_v5_2($1,$2)""",
        uuid.UUID(plan_id),
        packet_text,
    )
    if row is None:
        raise stage.ProjectionStageError("reinforcement preflight is absent")
    result = dict(row)
    if (
        str(result["target_claim_id"]) != CLAIM
        or result["target_revision_number"] != CLAIM_REVISION
    ):
        raise stage.ProjectionStageError("reinforcement target drifted")
    return result


async def persist_projection(
    conn: Any,
    plan_id: str,
    packet_text: str,
    owner_manifest_sha256: str,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """SELECT *
           FROM memory.stage_projection_reinforcement_v5_2($1,$2,$3)""",
        uuid.UUID(plan_id),
        packet_text,
        owner_manifest_sha256,
    )
    if row is None:
        raise stage.ProjectionStageError("reinforcement stage result is absent")
    return dict(row)


async def source_snapshot(
    conn: Any, observation_id: str, expected_owner: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    target = stage.TARGETS.get(observation_id)
    if target is None:
        raise stage.ProjectionStageError(
            "observation is outside the exact Neko correction target"
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
            "exact owner-scoped Neko correction source not found"
        )
    source = dict(source_row)
    spans = entailment_row["source_spans"]
    for field in ("object_literal", "project_scope", "temporal"):
        if isinstance(source.get(field), str):
            source[field] = json.loads(source[field])
    if isinstance(spans, str):
        spans = json.loads(spans)
    expected_literal = {
        "kind": "literal",
        "datatype": "text",
        "value": "Neko",
        "unit": None,
        "approximate": False,
    }
    if (
        not isinstance(spans, list)
        or not spans
        or str(source["owner_user_id"]) != expected_owner
        or str(source["evidence_id"]) != EVIDENCE
        or str(entailment_row["evidence_id"]) != EVIDENCE
        or source["evidence_content_sha256"]
        != target["evidence_content_sha256"]
        or entailment_row["observation_sha256"]
        != source["observation_sha256"]
        or source["observation_sha256"] != target["observation_sha256"]
        or source["predicate_registry_version"]
        != "memory_predicate_registry_v5_2"
        or source["predicate"] != "identity.name_canonical"
        or source["projection_class"] != "correction"
        or source["surface_policy"] != "normalization_only"
        or source["modality"] != "corrective"
        or source["polarity"] != "affirmed"
        or source["object_kind"] != "literal"
        or source["object_literal"] != expected_literal
        or str(source["subject_entity_id"]) != ENTITY
        or source["subject_entity_type"] != "animal"
        or source["subject_entity_status"] != "active"
        or source["subject_canonical_name"] != "Neko"
        or source["object_entity_id"] is not None
        or state_relation != "current"
        or source["evidence_status"] != "active"
    ):
        raise stage.ProjectionStageError(
            "source is outside the exact Neko correction reinforcement boundary"
        )
    source["temporal"]["state_relation"] = state_relation
    return source, spans, entailment_row["observation_ref"]


stage.build_projection_packet = build_projection_packet
stage.preflight_projection = preflight_projection
stage.persist_projection = persist_projection
stage.source_snapshot = source_snapshot


if __name__ == "__main__":
    parsed = stage.arguments()
    if parsed.command == "authorize":
        raise SystemExit(stage.authorize_without_database(parsed))
    raise SystemExit(asyncio.run(stage.run()))
