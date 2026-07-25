#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncpg

from scripts.memory_v1_predicate_runtime_profile_v2 import (
    load_runtime_profile_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LOCAL_PROVIDER_ID,
    LOCAL_PROVIDER_VERSION,
    SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
    LlamaCppSecureTransport,
    LocalLlamaCppProvider,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    load_registry,
    load_schema,
    validate_and_normalize,
)


CONTRACT_VERSION = "memory_v1_v5_2_general_compiler_v7_clone_verify_v1"
OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_OWNER = uuid.UUID("557ea042-cb82-48f8-9429-472e96c957ef")
MODEL = "qwen3-14b-local-extractor"
MODEL_FILE_SHA256 = (
    "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0"
)
RUNTIME_REVISION = "llama.cpp-b10066-86a9c79f8"
CASES = {
    uuid.UUID("33126656-fc5a-5fc1-a035-246b14576ee5"): {
        "case": "pet_name_correction",
        "content_sha256": (
            "be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4"
        ),
    },
    uuid.UUID("fea59e7e-30f5-4139-b634-97b291c88e14"): {
        "case": "named_caregiving",
        "content_sha256": (
            "895146b94431f7e0ec3292e757e30fc4c782af222bd39598e610654adf08ccec"
        ),
    },
    uuid.UUID("dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5"): {
        "case": "former_profession",
        "content_sha256": (
            "fb65fe592059bace88a4daea571ac1ba7df2b5aeb5136233dd01071cb6085182"
        ),
    },
}


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("clone DSN scheme is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("clone DSN must be loopback-only")
    if parsed.path in {"", "/", "/memory"}:
        raise RuntimeError("verification requires a disposable clone database")
    return value


def entity_map(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["entity_ref"]: item
        for item in packet["entity_mentions"]
    }


def canonical_external_id(
    value: Any,
    *,
    evidence_id: uuid.UUID,
) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return str(evidence_id)


def named_entity(
    entities: dict[str, dict[str, Any]],
    reference: str,
) -> str | None:
    entity = entities.get(reference)
    return entity.get("name_text") if entity is not None else None


def entity_object_name(
    entities: dict[str, dict[str, Any]],
    observation: dict[str, Any],
) -> str | None:
    obj = observation["object"]
    if obj["kind"] != "entity":
        return None
    return named_entity(entities, obj["entity_ref"])


def source_span_text(content: str, span: dict[str, Any]) -> str:
    start = span.get("start")
    end = span.get("end")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start < end <= len(content)
    ):
        raise RuntimeError("packet source span is invalid")
    return content[start:end]


def verify_correction(
    packet: dict[str, Any],
    local_calls: int,
) -> dict[str, Any]:
    entities = entity_map(packet)
    observations = [
        item
        for item in packet["observations"]
        if item["predicate"] == "identity.name_canonical"
    ]
    if len(observations) != 1 or local_calls != 0:
        raise RuntimeError("correction did not use deterministic canonical path")
    observation = observations[0]
    if (
        observation["modality"] != "corrective"
        or observation["projection_class"] != "correction"
        or observation["surface_policy"] != "normalization_only"
        or observation["object"].get("value") != "Neko"
        or named_entity(entities, observation["subject_entity_ref"]) != "Neko"
    ):
        raise RuntimeError("correction semantic contract changed")
    comparisons = {
        (item["relation_type"], item["target_lookup_key"])
        for item in packet["comparison_hints"]
    }
    if comparisons != {
        ("corrects", "identity.name:nemo"),
        ("supersedes", "identity.name:nemo"),
    }:
        raise RuntimeError("correction comparison contract changed")
    return {
        "case": "pet_name_correction",
        "local_model_calls": local_calls,
        "predicates": ["identity.name_canonical"],
        "manual_review_required": False,
    }


def verify_caregiving(
    packet: dict[str, Any],
    local_calls: int,
    source_content: str,
) -> dict[str, Any]:
    entities = entity_map(packet)
    observations = [
        item
        for item in packet["observations"]
        if item["predicate"] == "relationship.caregiver_for"
    ]
    if len(observations) != 1 or local_calls != 0:
        raise RuntimeError("caregiving extraction call or cardinality changed")
    observation = observations[0]
    subject = entities[observation["subject_entity_ref"]]
    object_name = entity_object_name(entities, observation)
    caregiver_text = source_span_text(
        source_content,
        observation["source_spans"][0],
    )
    if (
        subject["entity_type"] != "self"
        or object_name != "Monika"
        or observation["sensitivity"] != "high"
        or observation["surface_policy"] != "explicit_recall_only"
        or not caregiver_text.casefold().startswith("caring for ")
        or "my wife" not in caregiver_text.casefold()
        or not caregiver_text.casefold().endswith("monika")
        or "psychotic break" in caregiver_text.casefold()
        or len(caregiver_text) >= 100
    ):
        raise RuntimeError("caregiving direction or policy changed")
    spouse = [
        item
        for item in packet["observations"]
        if item["predicate"] == "relationship.spouse_of"
    ]
    spouse_text = (
        source_span_text(
            source_content,
            spouse[0]["source_spans"][0],
        )
        if len(spouse) == 1
        else ""
    )
    if (
        len(spouse) != 1
        or spouse_text.casefold().replace(",", "")
        != "my wife monika"
        or len(spouse_text) >= 40
    ):
        raise RuntimeError("caregiving spouse evidence span changed")
    for entity in entities.values():
        role = entity.get("relationship_role")
        if isinstance(role, str):
            parts = role.split("|")
            if len(parts) != len(set(parts)):
                raise RuntimeError("duplicate relationship roles remain")
    return {
        "case": "named_caregiving",
        "local_model_calls": local_calls,
        "predicates": sorted(
            {item["predicate"] for item in packet["observations"]}
        ),
        "manual_review_required": True,
    }


def verify_profession(
    packet: dict[str, Any],
    local_calls: int,
) -> dict[str, Any]:
    entities = entity_map(packet)
    observations = [
        item
        for item in packet["observations"]
        if item["predicate"] == "occupation.works_as"
    ]
    if len(observations) != 2 or local_calls != 1:
        raise RuntimeError("former profession extraction changed")
    if {
        entity_object_name(entities, observation)
        for observation in observations
    } != {"clinical psychologist", "BCBA"}:
        raise RuntimeError("former profession role split changed")
    for observation in observations:
        if (
            entities[observation["subject_entity_ref"]]["entity_type"]
            != "self"
            or observation["temporal"]["shape"] != "open_interval"
            or observation["temporal"]["instant_range"]["lower"] is not None
            or observation["temporal"]["instant_range"]["upper"] is None
            or "historical_relationship_ended_before_source"
            not in observation["temporal"]["reason_codes"]
        ):
            raise RuntimeError("former profession temporal contract changed")
    if any(
        item["predicate"] == "credential.reported"
        for item in packet["observations"]
    ):
        raise RuntimeError("former profession retained current credential")
    if any(
        item["reason_code"] == "project_scope_unresolved"
        for item in packet["deferrals"]
    ):
        raise RuntimeError("orphan project deferral remains")
    return {
        "case": "former_profession",
        "local_model_calls": local_calls,
        "predicates": sorted(
            {item["predicate"] for item in packet["observations"]}
        ),
        "manual_review_required": False,
    }


async def run() -> int:
    dsn = loopback_dsn(os.environ["POSTGRES_DSN"])
    root = Path(os.environ["MEMORY_V1_REPO_ROOT"]).resolve()
    profile = load_runtime_profile_v2(root, "v5_2")
    registry = load_registry(
        profile.registry_path,
        profile.registry_artifact_sha256,
    )
    schema = load_schema(
        profile.schema_path,
        profile.schema_artifact_sha256,
        expected_contract_version=profile.contract_version,
    )
    conn = await asyncpg.connect(dsn, command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("clone verification requires brains_app")
        async with conn.transaction(readonly=True):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(OTHER_OWNER),
            )
            visible = await conn.fetchval(
                """
                SELECT count(*)
                FROM memory.evidence
                WHERE owner_user_id=$1
                  AND evidence_id=ANY($2::uuid[])
                """,
                OWNER,
                list(CASES),
            )
        if visible != 0:
            raise RuntimeError("cross-owner evidence became visible")
        async with conn.transaction(readonly=True):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(OWNER),
            )
            rows = await conn.fetch(
                """
                SELECT evidence_id,source_system,external_id,content,
                       content_sha256,recorded_at,status::text AS status
                FROM memory.evidence
                WHERE owner_user_id=$1
                  AND evidence_id=ANY($2::uuid[])
                ORDER BY evidence_id
                """,
                OWNER,
                list(CASES),
            )
    finally:
        await conn.close()
    if len(rows) != len(CASES):
        raise RuntimeError("exact evidence set is incomplete")

    results: list[dict[str, Any]] = []
    total_local_calls = 0
    for row in rows:
        case = CASES[row["evidence_id"]]
        if (
            row["status"] != "active"
            or row["source_system"] != "public.chat_log"
            or row["content_sha256"] != case["content_sha256"]
        ):
            raise RuntimeError("exact evidence binding changed")
        source = TrustedExtractionSource.create(
            job_id=str(uuid.uuid5(row["evidence_id"], CONTRACT_VERSION)),
            source_system=row["source_system"],
            source_external_id=canonical_external_id(
                row["external_id"],
                evidence_id=row["evidence_id"],
            ),
            source_sha256=row["content_sha256"],
            source_recorded_at=row["recorded_at"],
            content=row["content"],
        )
        provider = LocalLlamaCppProvider(
            model=MODEL,
            model_file_sha256=MODEL_FILE_SHA256,
            runtime_revision=RUNTIME_REVISION,
            registry=registry,
            transport=LlamaCppSecureTransport(
                endpoint="http://127.0.0.1:18080/v1/chat/completions",
                enable_token=LOCAL_CALL_ENABLE_TOKEN,
                api_key=os.environ["MEMORY_V1_LOCAL_INFERENCE_API_KEY"],
                allow_loopback_http=True,
                allow_unauthenticated_loopback=False,
            ),
            max_output_tokens=4096,
            timeout_seconds=600.0,
        )
        validated = validate_and_normalize(
            provider,
            source=source,
            registry=registry,
            schema=schema,
            allowed_provider_versions={
                LOCAL_PROVIDER_ID: LOCAL_PROVIDER_VERSION,
            },
            max_external_model_calls=0,
        )
        if provider.external_model_calls != 0:
            raise RuntimeError("external model capability was exercised")
        local_calls = provider.local_model_calls
        total_local_calls += local_calls
        packet = validated.normalized_packet
        if case["case"] == "pet_name_correction":
            result = verify_correction(packet, local_calls)
        elif case["case"] == "named_caregiving":
            result = verify_caregiving(
                packet,
                local_calls,
                row["content"],
            )
        elif case["case"] == "former_profession":
            result = verify_profession(packet, local_calls)
        else:
            raise RuntimeError("unknown verification case")
        result.update(
            {
                "evidence_id_sha256": uuid.uuid5(
                    row["evidence_id"],
                    "sanitized-evidence-reference",
                ).hex,
                "content_sha256": row["content_sha256"],
                "normalized_packet_sha256": (
                    validated.normalized_packet_sha256
                ),
            }
        )
        results.append(result)

    if total_local_calls != 1:
        raise RuntimeError("bounded local model call count changed")
    print(
        json.dumps(
            {
                "contract_version": CONTRACT_VERSION,
                "passed": True,
                "compiler_version": SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
                "owner_isolation_visible_rows": 0,
                "external_model_calls": 0,
                "local_model_calls": total_local_calls,
                "cases": sorted(results, key=lambda item: item["case"]),
                "effects": {
                    "production_database_writes": 0,
                    "clone_database_writes": 0,
                    "qdrant_writes": 0,
                    "redis_writes": 0,
                    "prompt_influence": 0,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
