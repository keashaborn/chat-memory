#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

from scripts import memory_v1_v5_2_compiler_v8_claim_stage as stage


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
SELF_ENTITY = "35029129-27bd-457b-8cb5-82dd37ba32ba"
MAX_ENTITY = "726a17cf-9a7a-4e7e-91e9-367d9132a64d"
NEKO_ENTITY = "09308a2b-3019-4f59-8fc3-bb1fe1408a0d"
KEASHA_ENTITY = "6db538e2-b7f9-48ff-b2bc-db757708b660"


def literal(value: str) -> dict[str, Any]:
    return {
        "kind": "literal",
        "datatype": "text",
        "value": value,
        "unit": None,
        "approximate": False,
    }


TARGETS: dict[str, dict[str, Any]] = {
    # Keasha
    "2e668700-7ae6-4752-ab6a-423e6c6a8c6d": {
        "evidence_id": "3eb237da-4eec-5fa4-83fb-51360356dba6",
        "evidence_content_sha256": "18450aab927c1d47c4166b1549295194e92035830c404848f437872580375d99",
        "observation_sha256": "d47e14ad0d4b394be7bcf09cd916e1d484d2539e25c05e862e9e8e119dbbb03d",
        "predicate": "pet.breed",
        "state_relation": "not_applicable",
        "subject_entity_id": KEASHA_ENTITY,
        "subject_entity_type": "animal",
        "subject_canonical_name": "Keasha von Steffen Haus",
        "object_kind": "literal",
        "object_literal": literal("German Shepherd"),
        "canonical_text": "Keasha von Steffen Haus has recorded breed German Shepherd.",
    },
    "eed6cae7-33c9-43e9-8736-98861a057fa5": {
        "evidence_id": "3eb237da-4eec-5fa4-83fb-51360356dba6",
        "evidence_content_sha256": "18450aab927c1d47c4166b1549295194e92035830c404848f437872580375d99",
        "observation_sha256": "2401eeea7c11b597f69645be48fdf67b58a5e952c3d9dfdbccd02020d3105976",
        "predicate": "pet.species",
        "state_relation": "not_applicable",
        "subject_entity_id": KEASHA_ENTITY,
        "subject_entity_type": "animal",
        "subject_canonical_name": "Keasha von Steffen Haus",
        "object_kind": "literal",
        "object_literal": literal("dog"),
        "canonical_text": "Keasha von Steffen Haus has recorded species dog.",
    },
    "e5939e3f-b732-4f72-b7c4-b60ee7c55244": {
        "evidence_id": "3eb237da-4eec-5fa4-83fb-51360356dba6",
        "evidence_content_sha256": "18450aab927c1d47c4166b1549295194e92035830c404848f437872580375d99",
        "observation_sha256": "0f57e86fac25c1bfdc410513dd84743b031cad3ea92932d9b28647166ea061f7",
        "predicate": "relationship.has_pet",
        "state_relation": "historical",
        "subject_entity_id": SELF_ENTITY,
        "subject_entity_type": "self",
        "subject_canonical_name": "Self",
        "object_kind": "entity",
        "object_entity_id": KEASHA_ENTITY,
        "object_entity_type": "animal",
        "object_canonical_name": "Keasha von Steffen Haus",
        "canonical_text": "The user formerly had a pet named Keasha von Steffen Haus.",
    },
    "37c05103-3e18-4444-9722-4ab384896aa7": {
        "evidence_id": "3eb237da-4eec-5fa4-83fb-51360356dba6",
        "evidence_content_sha256": "18450aab927c1d47c4166b1549295194e92035830c404848f437872580375d99",
        "observation_sha256": "da37b6ed564dd0e6378ceb0eda7c16382987ab188f6896ff0683eace90abe20b",
        "predicate": "identity.name",
        "state_relation": "not_applicable",
        "subject_entity_id": KEASHA_ENTITY,
        "subject_entity_type": "animal",
        "subject_canonical_name": "Keasha von Steffen Haus",
        "object_kind": "literal",
        "object_literal": literal("Keasha von Steffen Haus"),
        "canonical_text": "Keasha von Steffen Haus' name is Keasha von Steffen Haus.",
    },
    # Max
    "978c1972-82d5-4d19-a32f-b45c7f4cfbaa": {
        "evidence_id": "5e938057-f012-5e54-8b37-148f3cc3ab9c",
        "evidence_content_sha256": "a23a3982c3fa12a4ad8fb77511785265efadf230d4f198936af2d009f91b20a2",
        "observation_sha256": "8b20c313e461fe7e7c501a71cb8d8e48629e195d2ed37c938f8bc70741265d1d",
        "predicate": "relationship.has_pet",
        "state_relation": "historical",
        "subject_entity_id": SELF_ENTITY,
        "subject_entity_type": "self",
        "subject_canonical_name": "Self",
        "object_kind": "entity",
        "object_entity_id": MAX_ENTITY,
        "object_entity_type": "animal",
        "object_canonical_name": "Max",
        "canonical_text": "The user formerly had a pet named Max.",
    },
    "cb711085-b49b-4b9c-be3b-c34d2d8456da": {
        "evidence_id": "5e938057-f012-5e54-8b37-148f3cc3ab9c",
        "evidence_content_sha256": "a23a3982c3fa12a4ad8fb77511785265efadf230d4f198936af2d009f91b20a2",
        "observation_sha256": "56a0991c24506c530a4c7675a9ec27cc9361c4e2c5b7eda073697f90d3eb4c1c",
        "predicate": "pet.species",
        "state_relation": "not_applicable",
        "subject_entity_id": MAX_ENTITY,
        "subject_entity_type": "animal",
        "subject_canonical_name": "Max",
        "object_kind": "literal",
        "object_literal": literal("dog"),
        "canonical_text": "Max has recorded species dog.",
    },
    "6db9a4fd-e109-48ed-8d95-c97eef75382c": {
        "evidence_id": "5e938057-f012-5e54-8b37-148f3cc3ab9c",
        "evidence_content_sha256": "a23a3982c3fa12a4ad8fb77511785265efadf230d4f198936af2d009f91b20a2",
        "observation_sha256": "74bc24e089840ce1c57da30a8e96b2d0be1b42b2dccdfd1f862dca7b486ef7bf",
        "predicate": "pet.breed",
        "state_relation": "not_applicable",
        "subject_entity_id": MAX_ENTITY,
        "subject_entity_type": "animal",
        "subject_canonical_name": "Max",
        "object_kind": "literal",
        "object_literal": literal("German Shepherd"),
        "canonical_text": "Max has recorded breed German Shepherd.",
    },
    "8c4476ae-b917-4678-8d63-f4949982e196": {
        "evidence_id": "5e938057-f012-5e54-8b37-148f3cc3ab9c",
        "evidence_content_sha256": "a23a3982c3fa12a4ad8fb77511785265efadf230d4f198936af2d009f91b20a2",
        "observation_sha256": "65245d4d7aee2e296fbd5f9161b7224bd6853c39741d253994910cf6db9464c4",
        "predicate": "identity.name",
        "state_relation": "not_applicable",
        "subject_entity_id": MAX_ENTITY,
        "subject_entity_type": "animal",
        "subject_canonical_name": "Max",
        "object_kind": "literal",
        "object_literal": literal("Max"),
        "canonical_text": "Max's name is Max.",
    },
    # Neko
    "6151f123-d05f-404d-b3f6-d7079de6b4e6": {
        "evidence_id": "ca637d00-7ff6-5147-8f6b-a82386dbc1c1",
        "evidence_content_sha256": "1a3d471b848c5267f15e80d137ac094cbe5eed583e2ec40026ca6e4e4e2b7df9",
        "observation_sha256": "d3d2f539d4a2d0919844f40c6d30bd10fb4120d11b3d1cbcfce6133f044bb0ff",
        "predicate": "pet.species",
        "state_relation": "not_applicable",
        "subject_entity_id": NEKO_ENTITY,
        "subject_entity_type": "animal",
        "subject_canonical_name": "Neko",
        "object_kind": "literal",
        "object_literal": literal("cat"),
        "canonical_text": "Neko has recorded species cat.",
    },
    "bc8866ad-95e8-4413-832e-813f601eece6": {
        "evidence_id": "ca637d00-7ff6-5147-8f6b-a82386dbc1c1",
        "evidence_content_sha256": "1a3d471b848c5267f15e80d137ac094cbe5eed583e2ec40026ca6e4e4e2b7df9",
        "observation_sha256": "e13b73f82fb1feb3189efddaf2c5752b5c56f7d2292438c4366f54a0f5cd7b86",
        "predicate": "relationship.has_pet",
        "state_relation": "historical",
        "subject_entity_id": SELF_ENTITY,
        "subject_entity_type": "self",
        "subject_canonical_name": "Self",
        "object_kind": "entity",
        "object_entity_id": NEKO_ENTITY,
        "object_entity_type": "animal",
        "object_canonical_name": "Neko",
        "canonical_text": "The user formerly had a pet named Neko.",
        "expected_existing_aggregates": 1,
    },
    "82c87916-a90c-4af8-b4cb-fbd2981f9f96": {
        "evidence_id": "ca637d00-7ff6-5147-8f6b-a82386dbc1c1",
        "evidence_content_sha256": "1a3d471b848c5267f15e80d137ac094cbe5eed583e2ec40026ca6e4e4e2b7df9",
        "observation_sha256": "990c5b16e4030f2dca5e870f53385ba0a061572f9a17c1d98d7002cc07d46788",
        "predicate": "identity.name",
        "state_relation": "not_applicable",
        "subject_entity_id": NEKO_ENTITY,
        "subject_entity_type": "animal",
        "subject_canonical_name": "Neko",
        "object_kind": "literal",
        "object_literal": literal("Neko"),
        "canonical_text": "Neko's name is Neko.",
    },
}


async def prepare_item(
    conn: Any,
    owner: str,
    observation_id: str,
    plan_id: str,
    entailment_request_id: str,
    registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    target = TARGETS[observation_id]
    source, spans, observation_ref = await source_snapshot(
        conn, observation_id, owner
    )
    packet = stage.build_projection_packet(owner, source)
    stage.validate_packet(packet, owner, registry)
    packet_text = stage.stable_json(packet)
    packet_preflight = await stage.preflight_projection(
        conn, plan_id, packet_text
    )
    entailment_preflight = await stage.preflight_entailment(
        conn, observation_id, spans
    )
    expected_aggregates = int(
        target.get("expected_existing_aggregates", 0)
    )
    canonical_match = (
        packet["projections"][0]["payload"]["canonical_text"]
        == target["canonical_text"]
    )
    if (
        packet_preflight is None
        or packet_preflight["existing_aggregates"] != expected_aggregates
        or packet_preflight["existing_plans"] != 0
        or not canonical_match
    ):
        aggregate_count = (
            None
            if packet_preflight is None
            else packet_preflight["existing_aggregates"]
        )
        plan_count = (
            None
            if packet_preflight is None
            else packet_preflight["existing_plans"]
        )
        raise stage.ProjectionStageError(
            "pet projection source is not exact and stageable: "
            f"observation={observation_id} "
            f"existing_aggregates={aggregate_count} "
            f"expected_aggregates={expected_aggregates} "
            f"existing_plans={plan_count} "
            f"canonical_match={canonical_match}"
        )
    return {
        "observation_ref": observation_ref,
        "evidence_id": target["evidence_id"],
        "predicate": target["predicate"],
        "state_relation": target["state_relation"],
        "observation_id": observation_id,
        "observation_sha256": source["observation_sha256"],
        "plan_id": plan_id,
        "entailment_request_id": entailment_request_id,
        "source_spans_sha256": stage.canonical_hash(spans),
        "packet": packet,
        "packet_text_sha256": hashlib.sha256(
            packet_text.encode("utf-8")
        ).hexdigest(),
        "packet_sha256": packet["packet_sha256"],
        "owner_manifest_sha256": packet_preflight[
            "owner_manifest_sha256"
        ],
        "entailment_authorization_manifest_sha256": entailment_preflight[
            "authorization_manifest_sha256"
        ],
        "canonical_text_sha256": stage.canonical_hash(
            packet["projections"][0]["payload"]["canonical_text"]
        ),
        "canonical_text": packet["projections"][0]["payload"][
            "canonical_text"
        ],
    }


async def validate_live_item(
    conn: Any,
    manifest: dict[str, Any],
    item: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    *,
    replay: bool,
) -> tuple[list[dict[str, Any]], str]:
    source, spans, observation_ref = await source_snapshot(
        conn, item["observation_id"], manifest["owner_user_id"]
    )
    packet = stage.build_projection_packet(
        manifest["owner_user_id"], source
    )
    stage.validate_packet(packet, manifest["owner_user_id"], registry)
    packet_text = stage.stable_json(packet)
    if (
        observation_ref != item["observation_ref"]
        or str(source["evidence_id"]) != item["evidence_id"]
        or source["predicate"] != item["predicate"]
        or source["temporal"]["state_relation"]
        != item["state_relation"]
        or source["observation_sha256"] != item["observation_sha256"]
        or stage.canonical_hash(spans) != item["source_spans_sha256"]
        or packet != item["packet"]
        or hashlib.sha256(packet_text.encode("utf-8")).hexdigest()
        != item["packet_text_sha256"]
    ):
        raise stage.ProjectionStageError(
            "live deterministic pet source drifted"
        )
    packet_preflight = await stage.preflight_projection(
        conn, item["plan_id"], packet_text
    )
    entailment_preflight = await stage.preflight_entailment(
        conn, item["observation_id"], spans
    )
    expected_plans = 1 if replay else 0
    expected_aggregates = int(
        TARGETS[item["observation_id"]].get(
            "expected_existing_aggregates", 0
        )
    )
    if (
        packet_preflight["existing_aggregates"] != expected_aggregates
        or packet_preflight["existing_plans"] != expected_plans
        or packet_preflight["owner_manifest_sha256"]
        != item["owner_manifest_sha256"]
        or entailment_preflight["authorization_manifest_sha256"]
        != item["entailment_authorization_manifest_sha256"]
    ):
        raise stage.ProjectionStageError(
            "live pet database preflight drifted"
        )
    return spans, packet_text


async def source_snapshot(
    conn: Any, observation_id: str, expected_owner: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    target = TARGETS.get(observation_id)
    if target is None:
        raise stage.ProjectionStageError(
            "observation is outside the exact pet profile target"
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
            "exact owner-scoped pet projection source not found"
        )
    source = dict(source_row)
    spans = entailment_row["source_spans"]
    for field in ("object_literal", "project_scope", "temporal"):
        if isinstance(source.get(field), str):
            source[field] = json.loads(source[field])
    if isinstance(spans, str):
        spans = json.loads(spans)
    if not isinstance(spans, list) or not spans:
        raise stage.ProjectionStageError("pet projection source spans are absent")

    expected_object_entity_id = target.get("object_entity_id")
    actual_object_entity_id = (
        str(source["object_entity_id"])
        if source["object_entity_id"] is not None
        else None
    )
    if (
        str(source["owner_user_id"]) != expected_owner
        or str(source["evidence_id"]) != target["evidence_id"]
        or str(entailment_row["evidence_id"]) != target["evidence_id"]
        or source["evidence_content_sha256"]
        != target["evidence_content_sha256"]
        or entailment_row["observation_sha256"]
        != source["observation_sha256"]
        or source["observation_sha256"] != target["observation_sha256"]
        or source["predicate_registry_version"]
        != "memory_predicate_registry_v5_2"
        or source["predicate"] != target["predicate"]
        or source["projection_class"] != "direct_claim"
        or source["surface_policy"] != "direct_or_relevant"
        or source["modality"] != "asserted"
        or source["polarity"] != "affirmed"
        or source["object_kind"] != target["object_kind"]
        or source["object_literal"] != target.get("object_literal")
        or str(source["subject_entity_id"]) != target["subject_entity_id"]
        or source["subject_entity_type"] != target["subject_entity_type"]
        or source["subject_entity_status"] != "active"
        or source["subject_canonical_name"] != target["subject_canonical_name"]
        or actual_object_entity_id != expected_object_entity_id
        or source["object_entity_type"] != target.get("object_entity_type")
        or source["object_canonical_name"] != target.get("object_canonical_name")
        or (
            expected_object_entity_id is not None
            and source["object_entity_status"] != "active"
        )
        or state_relation != target["state_relation"]
        or source["evidence_status"] != "active"
    ):
        raise stage.ProjectionStageError(
            "source is outside the exact pet profile claim boundary"
        )
    source["temporal"]["state_relation"] = state_relation
    return source, spans, entailment_row["observation_ref"]


def configure_stage() -> None:
    stage.MANIFEST_CONTRACT = "memory_v1_v5_2_pet_profile_claim_stage_manifest_v1"
    stage.AUTHORIZATION_CONTRACT = (
        "memory_v1_v5_2_pet_profile_claim_stage_authorization_v1"
    )
    stage.RESULT_CONTRACT = "memory_v1_v5_2_pet_profile_claim_stage_result_v1"
    stage.EXPECTED_TABLE_ROWS = {
        "observation_entailment_v5": len(TARGETS),
        "relational_operation_request": len(TARGETS),
        "projection_plan": len(TARGETS),
        "projection_plan_item": len(TARGETS),
        "projection_claim_payload": len(TARGETS),
        "projection_plan_observation": len(TARGETS),
    }
    stage.EXPECTED_NEW_ROWS = sum(stage.EXPECTED_TABLE_ROWS.values())
    stage.EXPECTED_ITEM_ROWS = 6
    stage.EXPECTED_EXISTING_AGGREGATES = 0
    stage.ENTAILMENT_APPLY_OUTCOME = "applied"
    stage.ENTAILMENT_REPLAY_OUTCOME = "replayed"
    stage.ASSESSOR_REF = "controlled_v5_2_pet_profile_bound_claim_stage"
    stage.CONFIRMATION = "STAGE_EXACT_ELEVEN_PET_PROFILE_CLAIM_CANDIDATES_ONLY"
    stage.APPLY_ENV = "MEMORY_V1_V5_2_PET_PROFILE_CLAIM_STAGE_APPLY"
    stage.TARGET_OWNER = OWNER
    stage.TARGETS = TARGETS
    stage.source_snapshot = source_snapshot
    stage.prepare_item = prepare_item
    stage.validate_live_item = validate_live_item


if __name__ == "__main__":
    configure_stage()
    parsed = stage.arguments()
    if parsed.command == "authorize":
        raise SystemExit(stage.authorize_without_database(parsed))
    raise SystemExit(asyncio.run(stage.run()))
