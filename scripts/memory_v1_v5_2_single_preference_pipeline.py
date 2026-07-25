#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from scripts.memory_v1_projection_v5_2_contract import (
    owner_manifest_sha256,
    sha256,
    validate_packet,
    validate_projection_schema,
    validate_registry,
)
from scripts.memory_v1_v5_2_projection_dispatch import build_packet, stable_json


MANIFEST_CONTRACT = "memory_v1_v5_2_single_preference_manifest_v1"
AUTHORIZATION_CONTRACT = "memory_v1_v5_2_single_preference_authorization_v1"
RESULT_CONTRACT = "memory_v1_v5_2_single_preference_result_v1"
REGISTRY_VERSION = "memory_predicate_registry_v5_2"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
ID_NAMESPACE = uuid.UUID("dfb63ae0-afde-4b41-86e3-f57195f78f51")
ASSESSOR_TYPE = "system"
ASSESSOR_REF = "controlled_v5_2_single_preference_20260725"
REVIEWER_TYPE = "system"
REVIEWER_REF = "controlled_v5_2_single_preference_20260725"
REVIEW_REASON = (
    "Owner-authored evidence explicitly states a stable free-time preference "
    "for listening to audiobooks."
)
REASON_CODES = [
    "active_exact_owner_evidence",
    "explicit_life_preference",
    "stable_free_time_preference",
]
CONFIRMATION = "APPLY_ONE_OWNER_V5_2_AUDIOBOOK_PREFERENCE_ONLY"
EXPECTED_TABLE_ROWS = {
    "observation_entailment_v5": 1,
    "relational_operation_request": 1,
    "projection_plan": 1,
    "projection_plan_item": 1,
    "projection_preference_payload": 1,
    "projection_plan_observation": 1,
    "projection_review": 1,
    "preference_head_v5": 1,
    "preference_revision_v5": 1,
    "preference_revision_observation": 1,
    "projection_apply_event": 1,
    "projection_dispatch_v5": 1,
}
EXPECTED_INSERT_ROWS = sum(EXPECTED_TABLE_ROWS.values())
EXPECTED_MUTATED_ROWS = EXPECTED_INSERT_ROWS + 1


class PipelineError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest")
    manifest.add_argument("--owner", required=True)
    manifest.add_argument("--evidence", required=True)
    manifest.add_argument("--observation", required=True)
    manifest.add_argument("--required-head", required=True)
    manifest.add_argument("--output", required=True)

    authorize = commands.add_parser("authorize")
    authorize.add_argument("--manifest", required=True)
    authorize.add_argument("--output", required=True)

    apply = commands.add_parser("apply")
    apply.add_argument("--manifest", required=True)
    apply.add_argument("--authorization", required=True)
    apply.add_argument("--confirm", required=True)
    apply.add_argument("--output", required=True)

    replay = commands.add_parser("replay")
    replay.add_argument("--manifest", required=True)
    replay.add_argument("--authorization", required=True)
    replay.add_argument("--apply-result", required=True)
    replay.add_argument("--confirm", required=True)
    replay.add_argument("--output", required=True)

    cross_owner = commands.add_parser("cross-owner")
    cross_owner.add_argument("--manifest", required=True)
    cross_owner.add_argument("--other-owner", required=True)
    cross_owner.add_argument("--output", required=True)
    return parser.parse_args()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise PipelineError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise PipelineError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PipelineError("input must be a private mode-0600 file")
    return path


def write_private(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def load_hashed(path: Path, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get(hash_field) != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise PipelineError(f"{path.name} content hash mismatch")
    return value


def stable_id(kind: str, owner: str, evidence: str, observation: str) -> str:
    return str(
        uuid.uuid5(
            ID_NAMESPACE,
            "|".join((kind, owner, evidence, observation)),
        )
    )


def load_registry() -> dict[str, dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "specs/memory_v1_projection_plan_v5_2.schema.json").read_text()
    )
    registry = json.loads(
        (root / "specs/memory_v1_predicate_registry_v5_2.json").read_text()
    )
    validate_projection_schema(schema)
    return validate_registry(registry)


def normalize_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def validate_source(
    source: dict[str, Any],
    spans: list[dict[str, Any]],
    observation_ref: str,
    *,
    owner: str,
    evidence: str,
    observation: str,
) -> None:
    literal = normalize_json(source.get("object_literal"))
    expected_literal = {
        "kind": "literal",
        "unit": None,
        "value": {
            "domain": "music",
            "target": "audiobooks",
            "context": None,
            "polarity": "likes",
        },
        "datatype": "json",
        "approximate": False,
    }
    if (
        str(source["owner_user_id"]) != owner
        or str(source["evidence_id"]) != evidence
        or str(source["observation_id"]) != observation
        or source["predicate_registry_version"] != REGISTRY_VERSION
        or source["predicate"] != "preference.life"
        or source["polarity"] != "affirmed"
        or source["modality"] != "endorsed"
        or source["projection_class"] != "life_preference"
        or source["surface_policy"]
        != "relevant_recommendation_or_explicit_recall"
        or source["subject_entity_type"] != "self"
        or source["subject_entity_status"] != "active"
        or source["object_kind"] != "literal"
        or source["object_entity_id"] is not None
        or source["evidence_status"] != "active"
        or literal != expected_literal
        or observation_ref != "o00"
        or not isinstance(spans, list)
        or not spans
    ):
        raise PipelineError("source is outside the exact audiobook preference boundary")


async def source_snapshot(
    conn: Any,
    *,
    owner: str,
    evidence: str,
    observation: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    source_row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
        uuid.UUID(observation),
    )
    entailment_row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_entailment_source_v5_2($1)",
        uuid.UUID(observation),
    )
    if source_row is None or entailment_row is None:
        raise PipelineError("exact owner-scoped projection source not found")
    source = dict(source_row)
    for field in ("object_literal", "project_scope", "temporal"):
        source[field] = normalize_json(source.get(field))
    spans = normalize_json(entailment_row["source_spans"])
    observation_ref = entailment_row["observation_ref"]
    if (
        str(entailment_row["observation_id"]) != observation
        or str(entailment_row["evidence_id"]) != evidence
        or entailment_row["observation_sha256"] != source["observation_sha256"]
    ):
        raise PipelineError("entailment source differs from projection source")
    validate_source(
        source,
        spans,
        observation_ref,
        owner=owner,
        evidence=evidence,
        observation=observation,
    )
    return source, spans, observation_ref


def validate_packet_boundary(packet: dict[str, Any]) -> dict[str, Any]:
    projections = packet.get("projections")
    if not isinstance(projections, list) or len(projections) != 1:
        raise PipelineError("packet must contain one projection")
    projection = projections[0]
    expected_payload = {
        "kind": "preference",
        "preference_class": "life",
        "domain": "music",
        "preference_key": (
            "life.music."
            + hashlib.sha256("audiobooks".encode()).hexdigest()[:24]
        ),
        "value": {"target": "audiobooks", "context": None},
        "preference_polarity": "likes",
        "scope": "user_global",
        "stability": "stable",
        "surface_policy": "relevant_recommendation_or_explicit_recall",
    }
    if (
        projection.get("projection_ref") != "p01"
        or projection.get("lane") != "preference"
        or projection.get("payload") != expected_payload
        or projection.get("target")
        != {
            "action": "create",
            "aggregate_id": None,
            "expected_revision_number": None,
            "reason_codes": [],
        }
        or projection.get("review")
        != {
            "state": "manual_review_required",
            "authorization_required": True,
            "reason_codes": ["initial_v5_2_projection_requires_review"],
        }
    ):
        raise PipelineError("deterministic audiobook projection drifted")
    return projection


def validate_head(value: str) -> str:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise PipelineError("required head must be a full lowercase commit")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    value = load_hashed(path, "manifest_sha256")
    expected_keys = {
        "contract_version",
        "target_server",
        "owner_user_id",
        "evidence_id",
        "observation_id",
        "observation_ref",
        "observation_sha256",
        "required_head_commit",
        "predicate_registry_version",
        "assessor_type",
        "assessor_ref",
        "reviewer_type",
        "reviewer_ref",
        "review_decision",
        "review_reason",
        "review_reason_codes",
        "plan_id",
        "entailment_request_id",
        "projection_request_id",
        "source_spans",
        "source_spans_sha256",
        "packet",
        "packet_text_sha256",
        "packet_sha256",
        "owner_manifest_sha256",
        "entailment_authorization_manifest_sha256",
        "projection_sha256",
        "semantic_key_sha256",
        "payload_sha256",
        "expected_insert_rows",
        "expected_mutated_rows",
        "expected_table_rows",
        "manifest_sha256",
    }
    if set(value) != expected_keys:
        raise PipelineError("manifest fields mismatch")
    for field in (
        "owner_user_id",
        "evidence_id",
        "observation_id",
        "plan_id",
        "entailment_request_id",
        "projection_request_id",
    ):
        uuid.UUID(value[field])
    validate_head(value["required_head_commit"])
    packet_text = stable_json(value["packet"])
    projection = validate_packet_boundary(value["packet"])
    if (
        value["contract_version"] != MANIFEST_CONTRACT
        or value["target_server"] != "seebx"
        or value["predicate_registry_version"] != REGISTRY_VERSION
        or value["assessor_type"] != ASSESSOR_TYPE
        or value["assessor_ref"] != ASSESSOR_REF
        or value["reviewer_type"] != REVIEWER_TYPE
        or value["reviewer_ref"] != REVIEWER_REF
        or value["review_decision"] != "authorized"
        or value["review_reason"] != REVIEW_REASON
        or value["review_reason_codes"] != REASON_CODES
        or value["expected_insert_rows"] != EXPECTED_INSERT_ROWS
        or value["expected_mutated_rows"] != EXPECTED_MUTATED_ROWS
        or value["expected_table_rows"] != EXPECTED_TABLE_ROWS
        or value["packet_text_sha256"]
        != hashlib.sha256(packet_text.encode()).hexdigest()
        or value["packet_sha256"] != value["packet"]["packet_sha256"]
        or value["owner_manifest_sha256"]
        != owner_manifest_sha256(value["owner_user_id"], value["packet_sha256"])
        or value["projection_sha256"] != sha256(projection)
        or value["semantic_key_sha256"]
        != projection["identity"]["semantic_key_sha256"]
        or value["payload_sha256"] != sha256(projection["payload"])
        or value["source_spans_sha256"] != sha256(value["source_spans"])
    ):
        raise PipelineError("manifest boundary or hash mismatch")
    return value


def load_authorization(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    value = load_hashed(path, "authorization_sha256")
    if (
        set(value)
        != {
            "contract_version",
            "owner_user_id",
            "required_head_commit",
            "manifest_sha256",
            "authorized_action",
            "expected_insert_rows",
            "expected_mutated_rows",
            "authorization_sha256",
        }
        or value["contract_version"] != AUTHORIZATION_CONTRACT
        or value["owner_user_id"] != manifest["owner_user_id"]
        or value["required_head_commit"] != manifest["required_head_commit"]
        or value["manifest_sha256"] != manifest["manifest_sha256"]
        or value["authorized_action"] != CONFIRMATION
        or value["expected_insert_rows"] != EXPECTED_INSERT_ROWS
        or value["expected_mutated_rows"] != EXPECTED_MUTATED_ROWS
    ):
        raise PipelineError("authorization mismatch")
    return value


async def set_actor(conn: Any, owner: str) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)


async def build_manifest(args: argparse.Namespace, conn: Any) -> dict[str, Any]:
    owner = str(uuid.UUID(args.owner))
    evidence = str(uuid.UUID(args.evidence))
    observation = str(uuid.UUID(args.observation))
    head = validate_head(args.required_head)
    registry = load_registry()
    tx = conn.transaction(readonly=True, isolation="repeatable_read")
    await tx.start()
    try:
        await set_actor(conn, owner)
        source, spans, observation_ref = await source_snapshot(
            conn,
            owner=owner,
            evidence=evidence,
            observation=observation,
        )
        packet = build_packet(owner, source)
        validate_packet(packet, owner, registry)
        projection = validate_packet_boundary(packet)
        packet_text = stable_json(packet)
        plan_id = stable_id("plan", owner, evidence, observation)
        entailment_request_id = stable_id("entailment", owner, evidence, observation)
        projection_request_id = stable_id("apply", owner, evidence, observation)
        packet_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
            uuid.UUID(plan_id),
            packet_text,
        )
        entailment_preflight = await conn.fetchrow(
            """SELECT * FROM memory.preflight_observation_entailment_v5(
                 $1,'accepted'::memory.observation_entailment_decision_v5,
                 'predicate_entailment_v5_1_accepted',$2::jsonb,$3,$4)""",
            uuid.UUID(observation),
            stable_json(spans),
            ASSESSOR_TYPE,
            ASSESSOR_REF,
        )
        if (
            packet_preflight is None
            or packet_preflight["existing_aggregates"] != 0
            or packet_preflight["existing_plans"] != 0
            or entailment_preflight is None
        ):
            raise PipelineError("preference source is not empty and stageable")
    finally:
        await tx.rollback()
    value = {
        "contract_version": MANIFEST_CONTRACT,
        "target_server": "seebx",
        "owner_user_id": owner,
        "evidence_id": evidence,
        "observation_id": observation,
        "observation_ref": observation_ref,
        "observation_sha256": source["observation_sha256"],
        "required_head_commit": head,
        "predicate_registry_version": REGISTRY_VERSION,
        "assessor_type": ASSESSOR_TYPE,
        "assessor_ref": ASSESSOR_REF,
        "reviewer_type": REVIEWER_TYPE,
        "reviewer_ref": REVIEWER_REF,
        "review_decision": "authorized",
        "review_reason": REVIEW_REASON,
        "review_reason_codes": REASON_CODES,
        "plan_id": plan_id,
        "entailment_request_id": entailment_request_id,
        "projection_request_id": projection_request_id,
        "source_spans": spans,
        "source_spans_sha256": sha256(spans),
        "packet": packet,
        "packet_text_sha256": hashlib.sha256(packet_text.encode()).hexdigest(),
        "packet_sha256": packet["packet_sha256"],
        "owner_manifest_sha256": packet_preflight["owner_manifest_sha256"],
        "entailment_authorization_manifest_sha256": entailment_preflight[
            "authorization_manifest_sha256"
        ],
        "projection_sha256": sha256(projection),
        "semantic_key_sha256": projection["identity"]["semantic_key_sha256"],
        "payload_sha256": sha256(projection["payload"]),
        "expected_insert_rows": EXPECTED_INSERT_ROWS,
        "expected_mutated_rows": EXPECTED_MUTATED_ROWS,
        "expected_table_rows": EXPECTED_TABLE_ROWS,
    }
    value["manifest_sha256"] = sha256(value)
    return value


async def validate_live_source(
    conn: Any,
    manifest: dict[str, Any],
    *,
    replay: bool,
) -> str:
    source, spans, observation_ref = await source_snapshot(
        conn,
        owner=manifest["owner_user_id"],
        evidence=manifest["evidence_id"],
        observation=manifest["observation_id"],
    )
    packet = build_packet(manifest["owner_user_id"], source)
    validate_packet(packet, manifest["owner_user_id"], load_registry())
    projection = validate_packet_boundary(packet)
    packet_text = stable_json(packet)
    if (
        observation_ref != manifest["observation_ref"]
        or source["observation_sha256"] != manifest["observation_sha256"]
        or spans != manifest["source_spans"]
        or packet != manifest["packet"]
        or sha256(projection) != manifest["projection_sha256"]
    ):
        raise PipelineError("live deterministic preference source drifted")
    if not replay:
        packet_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
            uuid.UUID(manifest["plan_id"]),
            packet_text,
        )
        entailment_preflight = await conn.fetchrow(
            """SELECT * FROM memory.preflight_observation_entailment_v5(
                 $1,'accepted'::memory.observation_entailment_decision_v5,
                 'predicate_entailment_v5_1_accepted',$2::jsonb,$3,$4)""",
            uuid.UUID(manifest["observation_id"]),
            stable_json(spans),
            ASSESSOR_TYPE,
            ASSESSOR_REF,
        )
        if (
            packet_preflight["existing_aggregates"] != 0
            or packet_preflight["existing_plans"] != 0
            or packet_preflight["owner_manifest_sha256"]
            != manifest["owner_manifest_sha256"]
            or entailment_preflight["authorization_manifest_sha256"]
            != manifest["entailment_authorization_manifest_sha256"]
        ):
            raise PipelineError("live preference preflight drifted")
    return packet_text


def validate_apply_result(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    value = load_hashed(path, "result_sha256")
    if (
        value.get("contract_version") != RESULT_CONTRACT
        or value.get("mode") != "apply"
        or value.get("owner_user_id") != manifest["owner_user_id"]
        or value.get("manifest_sha256") != manifest["manifest_sha256"]
        or value.get("insert_rows") != EXPECTED_INSERT_ROWS
        or value.get("mutated_rows") != EXPECTED_MUTATED_ROWS
        or value.get("rows_written") != EXPECTED_MUTATED_ROWS
        or value.get("lane") != "preference"
    ):
        raise PipelineError("apply result cannot authorize replay")
    for field in (
        "review_id",
        "preference_id",
        "revision_id",
    ):
        uuid.UUID(value[field])
    for field in (
        "review_authorization_manifest_sha256",
        "projection_apply_manifest_sha256",
    ):
        if len(value[field]) != 64:
            raise PipelineError("apply result hash is invalid")
    return value


async def apply_or_replay(
    args: argparse.Namespace,
    conn: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    load_authorization(private_path(args.authorization), manifest)
    if args.confirm != CONFIRMATION:
        raise PipelineError("confirmation phrase mismatch")
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD") != manifest["required_head_commit"]:
        raise PipelineError("runtime head does not match manifest")
    if os.environ.get("MEMORY_V1_V5_2_SINGLE_PREFERENCE_APPLY") != "authorized":
        raise PipelineError("bounded preference apply authorization is absent")
    replay = args.command == "replay"
    prior = (
        validate_apply_result(private_path(args.apply_result), manifest)
        if replay
        else None
    )
    tx = conn.transaction(isolation="serializable")
    await tx.start()
    try:
        await set_actor(conn, manifest["owner_user_id"])
        packet_text = await validate_live_source(conn, manifest, replay=replay)
        entailment = await conn.fetchrow(
            """SELECT * FROM memory.record_observation_entailment_v5(
                 $1,$2,'accepted'::memory.observation_entailment_decision_v5,
                 'predicate_entailment_v5_1_accepted',$3::jsonb,$4,$5,$6)""",
            uuid.UUID(manifest["entailment_request_id"]),
            uuid.UUID(manifest["observation_id"]),
            stable_json(manifest["source_spans"]),
            ASSESSOR_TYPE,
            ASSESSOR_REF,
            manifest["entailment_authorization_manifest_sha256"],
        )
        staged = await conn.fetchrow(
            "SELECT * FROM memory.stage_projection_plan_v5_2($1,$2,$3)",
            uuid.UUID(manifest["plan_id"]),
            packet_text,
            manifest["owner_manifest_sha256"],
        )
        await conn.execute("SET CONSTRAINTS ALL DEFERRED")
        if replay:
            review_authorization = prior["review_authorization_manifest_sha256"]
            review_id = prior["review_id"]
        else:
            review_preflight = await conn.fetchrow(
                """SELECT * FROM memory.preflight_projection_review_v5(
                     $1,'p01','authorized'::memory.projection_review_decision_v5,
                     $2,$3,$4,$5::jsonb)""",
                uuid.UUID(manifest["plan_id"]),
                REVIEWER_TYPE,
                REVIEWER_REF,
                REVIEW_REASON,
                stable_json(REASON_CODES),
            )
            if (
                review_preflight["review_number"] != 1
                or review_preflight["projection_sha256"]
                != manifest["projection_sha256"]
                or review_preflight["semantic_key_sha256"]
                != manifest["semantic_key_sha256"]
            ):
                raise PipelineError("preference review preflight drifted")
            review_authorization = review_preflight[
                "authorization_manifest_sha256"
            ]
            review_id = None
        reviewed = await conn.fetchrow(
            """SELECT * FROM memory.review_projection_v5(
                 $1,'p01','authorized'::memory.projection_review_decision_v5,
                 $2,$3,$4,$5::jsonb,$6)""",
            uuid.UUID(manifest["plan_id"]),
            REVIEWER_TYPE,
            REVIEWER_REF,
            REVIEW_REASON,
            stable_json(REASON_CODES),
            review_authorization,
        )
        if review_id is not None and str(reviewed["review_id"]) != review_id:
            raise PipelineError("replayed preference review identity drifted")
        review_id = str(reviewed["review_id"])
        if replay:
            apply_manifest = prior["projection_apply_manifest_sha256"]
        else:
            apply_preflight = await conn.fetchrow(
                "SELECT * FROM memory.preflight_projection_apply_v5($1,'p01',$2)",
                uuid.UUID(manifest["plan_id"]),
                uuid.UUID(review_id),
            )
            if (
                apply_preflight["lane"] != "preference"
                or apply_preflight["target_action"] != "create"
                or apply_preflight["review_state"] != "manual_review_required"
                or apply_preflight["current_revision_number"] != 0
                or str(apply_preflight["review_id"]) != review_id
            ):
                raise PipelineError("preference materialization preflight drifted")
            apply_manifest = apply_preflight["apply_manifest_sha256"]
        projected = await conn.fetchrow(
            "SELECT * FROM memory.apply_projection_v5($1,$2,'p01',$3,$4)",
            uuid.UUID(manifest["projection_request_id"]),
            uuid.UUID(manifest["plan_id"]),
            uuid.UUID(review_id),
            apply_manifest,
        )
        expected_outcome = "replayed" if replay else "applied"
        expected_rows = 0 if replay else EXPECTED_MUTATED_ROWS
        rows_written = (
            entailment["rows_written"]
            + staged["rows_written"]
            + reviewed["rows_written"]
            + projected["rows_written"]
        )
        if (
            entailment["outcome"] != expected_outcome
            or staged["outcome"] != expected_outcome
            or reviewed["outcome"] != expected_outcome
            or projected["outcome"] != expected_outcome
            or projected["lane"] != "preference"
            or projected["revision_number"] != 1
            or rows_written != expected_rows
        ):
            raise PipelineError("bounded preference pipeline outcome mismatch")
        if replay and (
            str(projected["aggregate_id"]) != prior["preference_id"]
            or str(projected["revision_id"]) != prior["revision_id"]
        ):
            raise PipelineError("replayed preference result identity drifted")
        await tx.commit()
    except BaseException:
        await tx.rollback()
        raise
    return {
        "contract_version": RESULT_CONTRACT,
        "mode": args.command,
        "owner_user_id": manifest["owner_user_id"],
        "evidence_id": manifest["evidence_id"],
        "observation_id": manifest["observation_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "plan_id": manifest["plan_id"],
        "review_id": review_id,
        "review_authorization_manifest_sha256": review_authorization,
        "projection_apply_manifest_sha256": apply_manifest,
        "preference_id": str(projected["aggregate_id"]),
        "revision_id": str(projected["revision_id"]),
        "lane": "preference",
        "insert_rows": 0 if replay else EXPECTED_INSERT_ROWS,
        "mutated_rows": 0 if replay else EXPECTED_MUTATED_ROWS,
        "rows_written": rows_written,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "claim_writes": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }


async def cross_owner_probe(
    args: argparse.Namespace,
    conn: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    other_owner = str(uuid.UUID(args.other_owner))
    if other_owner == manifest["owner_user_id"]:
        raise PipelineError("cross-owner probe requires another owner")
    tx = conn.transaction(readonly=True, isolation="repeatable_read")
    await tx.start()
    rejected = False
    try:
        await set_actor(conn, other_owner)
        try:
            await conn.fetchrow(
                "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
                uuid.UUID(manifest["observation_id"]),
            )
        except Exception:
            rejected = True
    finally:
        await tx.rollback()
    if not rejected:
        raise PipelineError("cross-owner preference source was visible")
    return {
        "contract_version": RESULT_CONTRACT,
        "mode": "cross-owner",
        "owner_user_id": manifest["owner_user_id"],
        "other_owner_user_id": other_owner,
        "manifest_sha256": manifest["manifest_sha256"],
        "cross_owner_rejected": True,
        "rows_written": 0,
    }


async def run(args: argparse.Namespace) -> int:
    import asyncpg

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise PipelineError("POSTGRES_DSN is required")
    output = private_path(args.output, output=True)
    conn = await asyncpg.connect(dsn, command_timeout=90)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise PipelineError("POSTGRES_DSN must authenticate as brains_app")
        if args.command == "manifest":
            result = await build_manifest(args, conn)
        else:
            manifest = load_manifest(private_path(args.manifest))
            if args.command in {"apply", "replay"}:
                result = await apply_or_replay(args, conn, manifest)
            else:
                result = await cross_owner_probe(args, conn, manifest)
    finally:
        await conn.close()
    if args.command != "manifest":
        result["result_sha256"] = sha256(result)
    write_private(output, result)
    print(f"mode={args.command}")
    print(f"rows_written={result.get('rows_written', 0)}")
    print(f"result={output}")
    print(
        f"result_sha256={result.get('result_sha256', result['manifest_sha256'])}"
    )
    return 0


def authorize(args: argparse.Namespace) -> int:
    manifest = load_manifest(private_path(args.manifest))
    output = private_path(args.output, output=True)
    value = {
        "contract_version": AUTHORIZATION_CONTRACT,
        "owner_user_id": manifest["owner_user_id"],
        "required_head_commit": manifest["required_head_commit"],
        "manifest_sha256": manifest["manifest_sha256"],
        "authorized_action": CONFIRMATION,
        "expected_insert_rows": EXPECTED_INSERT_ROWS,
        "expected_mutated_rows": EXPECTED_MUTATED_ROWS,
    }
    value["authorization_sha256"] = sha256(value)
    write_private(output, value)
    print(f"authorization={output}")
    print(f"authorization_sha256={value['authorization_sha256']}")
    return 0


if __name__ == "__main__":
    parsed = arguments()
    if parsed.command == "authorize":
        raise SystemExit(authorize(parsed))
    raise SystemExit(asyncio.run(run(parsed)))
