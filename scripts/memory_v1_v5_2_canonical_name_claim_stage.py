#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from scripts import memory_v1_v5_2_compiler_v8_claim_stage as stage


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OBSERVATION = "93024235-89a8-49d5-88fa-7e4a143b68f3"
EVIDENCE = "36e92633-08fd-441f-8d1d-27f16c2a3479"
ENTITY = "09308a2b-3019-4f59-8fc3-bb1fe1408a0d"

stage.MANIFEST_CONTRACT = (
    "memory_v1_v5_2_canonical_name_claim_stage_manifest_v1"
)
stage.AUTHORIZATION_CONTRACT = (
    "memory_v1_v5_2_canonical_name_claim_stage_authorization_v1"
)
stage.RESULT_CONTRACT = (
    "memory_v1_v5_2_canonical_name_claim_stage_result_v1"
)
stage.EXPECTED_TABLE_ROWS = {
    "projection_plan": 1,
    "projection_plan_item": 1,
    "projection_claim_payload": 1,
    "projection_plan_observation": 1,
}
stage.EXPECTED_NEW_ROWS = sum(stage.EXPECTED_TABLE_ROWS.values())
stage.EXPECTED_ITEM_ROWS = 4
stage.ENTAILMENT_APPLY_OUTCOME = "reused"
stage.ENTAILMENT_REPLAY_OUTCOME = "reused"
stage.ASSESSOR_REF = "controlled_v5_2_canonical_name_claim_stage"
stage.CONFIRMATION = "STAGE_EXACT_NEKO_CANONICAL_NAME_CLAIM_CANDIDATE_ONLY"
stage.APPLY_ENV = "MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_STAGE_APPLY"
stage.TARGET_OWNER = OWNER
stage.TARGETS = {
    OBSERVATION: {
        "evidence_id": EVIDENCE,
        "evidence_content_sha256": (
            "bd2e59f74888b232822500c0ac4fae12a96f85b1121da4507a0f5efc9b4bb9ce"
        ),
        "observation_sha256": (
            "28b4cb5db9082ca6b718cb7588a1740b1d9dbb6ddd5486d90758facfe00fb675"
        ),
        "predicate": "identity.name_canonical",
        "state_relation": "not_applicable",
        "canonical_text": "Neko's canonical name is Neko.",
    }
}


async def preflight_entailment(
    conn: Any,
    observation_id: str,
    spans: list[dict[str, Any]],
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """SELECT *
           FROM memory.preflight_reusable_observation_entailment_v5_2(
             $1,$2::jsonb
           )""",
        uuid.UUID(observation_id),
        stage.stable_json(spans),
    )
    if row is None:
        raise stage.ProjectionStageError(
            "reusable canonical-name entailment is absent"
        )
    result = dict(row)
    if (
        result["decision"] != "accepted"
        or result["reason_code"] != "predicate_entailment_v5_1_accepted"
        or result["policy_version"]
        != "memory_v1_predicate_entailment_v5_1"
        or result["assessor_type"] != "system"
    ):
        raise stage.ProjectionStageError(
            "reusable canonical-name entailment drifted"
        )
    return result


async def persist_entailment(
    conn: Any,
    item: dict[str, Any],
    spans: list[dict[str, Any]],
) -> dict[str, Any]:
    result = await preflight_entailment(
        conn, item["observation_id"], spans
    )
    if (
        result["authorization_manifest_sha256"]
        != item["entailment_authorization_manifest_sha256"]
    ):
        raise stage.ProjectionStageError(
            "reusable canonical-name entailment hash drifted"
        )
    return {
        "decision_id": result["decision_id"],
        "outcome": "reused",
        "rows_written": 0,
    }


async def source_snapshot(
    conn: Any, observation_id: str, expected_owner: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    target = stage.TARGETS.get(observation_id)
    if target is None:
        raise stage.ProjectionStageError(
            "observation is outside the exact canonical-name target"
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
            "exact owner-scoped canonical-name source not found"
        )
    source = dict(source_row)
    spans = entailment_row["source_spans"]
    for field in ("object_literal", "project_scope", "temporal"):
        if isinstance(source.get(field), str):
            source[field] = json.loads(source[field])
    if isinstance(spans, str):
        spans = json.loads(spans)
    if not isinstance(spans, list) or not spans:
        raise stage.ProjectionStageError("canonical-name source spans are absent")
    source["temporal"]["state_relation"] = state_relation
    expected_literal = {
        "kind": "literal",
        "datatype": "text",
        "value": "Neko",
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
        or source["predicate"] != "identity.name_canonical"
        or source["projection_class"] != "direct_claim"
        or source["surface_policy"] != "direct_or_relevant"
        or source["modality"] != "corrective"
        or source["polarity"] != "affirmed"
        or source["object_kind"] != "literal"
        or source["object_literal"] != expected_literal
        or str(source["subject_entity_id"]) != ENTITY
        or source["subject_entity_type"] != "animal"
        or source["subject_entity_status"] != "active"
        or source["subject_canonical_name"] != "Neko"
        or source["object_entity_id"] is not None
        or source["temporal"]["state_relation"] != "not_applicable"
        or source["evidence_status"] != "active"
    ):
        raise stage.ProjectionStageError(
            "source is outside the exact canonical-name claim boundary"
        )
    return source, spans, entailment_row["observation_ref"]


stage.source_snapshot = source_snapshot
stage.preflight_entailment = preflight_entailment
stage.persist_entailment = persist_entailment


if __name__ == "__main__":
    parsed = stage.arguments()
    if parsed.command == "authorize":
        raise SystemExit(stage.authorize_without_database(parsed))
    raise SystemExit(asyncio.run(stage.run()))
