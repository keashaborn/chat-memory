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


MANIFEST_CONTRACT = "memory_v1_v5_2_projection_stage_batch_manifest_v1"
AUTHORIZATION_CONTRACT = "memory_v1_v5_2_projection_stage_batch_authorization_v1"
RESULT_CONTRACT = "memory_v1_v5_2_projection_stage_batch_result_v1"
REGISTRY_VERSION = "memory_predicate_registry_v5_2"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
ID_NAMESPACE = uuid.UUID("88c25cb9-9f98-4db8-a28c-0b0884fa8205")
EXPECTED_TABLE_ROWS = {
    "observation_entailment_v5": 4,
    "relational_operation_request": 4,
    "projection_plan": 4,
    "projection_plan_item": 4,
    "projection_claim_payload": 4,
    "projection_plan_observation": 4,
}
EXPECTED_NEW_ROWS = sum(EXPECTED_TABLE_ROWS.values())
ASSESSOR_TYPE = "system"
ASSESSOR_REF = "controlled_v5_2_reported_stance_stage"
CONFIRMATION = "STAGE_OWNER_V5_2_REPORTED_STANCE_PROJECTIONS_ONLY"


class ProjectionStageError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--owner", required=True)
    manifest.add_argument("--evidence", required=True)
    manifest.add_argument("--observation", action="append", required=True)
    manifest.add_argument("--required-head", required=True)
    manifest.add_argument("--output", required=True)

    authorize = subparsers.add_parser("authorize")
    authorize.add_argument("--manifest", required=True)
    authorize.add_argument("--output", required=True)

    for command in ("apply", "replay"):
        action = subparsers.add_parser(command)
        action.add_argument("--manifest", required=True)
        action.add_argument("--authorization", required=True)
        action.add_argument("--output", required=True)
        action.add_argument("--confirm", required=True)

    cross_owner = subparsers.add_parser("cross-owner")
    cross_owner.add_argument("--manifest", required=True)
    cross_owner.add_argument("--other-owner", required=True)
    cross_owner.add_argument("--output", required=True)
    return parser.parse_args()


def review_path(value: str, *, exists: bool) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ProjectionStageError("artifact is outside the private review root")
    if exists:
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ProjectionStageError("review input must be a mode-0600 regular file")
    elif path.exists():
        raise ProjectionStageError("output path already exists")
    return path


def write_private(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def stable_id(kind: str, owner: str, evidence: str, observation: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, "|".join((kind, owner, evidence, observation))))


def load_contracts() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "specs/memory_v1_projection_plan_v5_2.schema.json").read_text()
    )
    registry = json.loads(
        (root / "specs/memory_v1_predicate_registry_v5_2.json").read_text()
    )
    validate_projection_schema(schema)
    return schema, validate_registry(registry)


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    expected_keys = {
        "contract_version",
        "target_server",
        "owner_user_id",
        "evidence_id",
        "required_head_commit",
        "predicate_registry_version",
        "assessor_type",
        "assessor_ref",
        "expected_new_rows",
        "expected_table_rows",
        "items",
        "manifest_sha256",
    }
    if set(value) != expected_keys:
        raise ProjectionStageError("manifest fields mismatch")
    if (
        value["contract_version"] != MANIFEST_CONTRACT
        or value["target_server"] != "seebx"
        or value["predicate_registry_version"] != REGISTRY_VERSION
        or value["assessor_type"] != ASSESSOR_TYPE
        or value["assessor_ref"] != ASSESSOR_REF
        or value["expected_new_rows"] != EXPECTED_NEW_ROWS
        or value["expected_table_rows"] != EXPECTED_TABLE_ROWS
        or len(value["items"]) != 4
    ):
        raise ProjectionStageError("manifest boundary mismatch")
    uuid.UUID(value["owner_user_id"])
    uuid.UUID(value["evidence_id"])
    if value["manifest_sha256"] != sha256(
        {key: item for key, item in value.items() if key != "manifest_sha256"}
    ):
        raise ProjectionStageError("manifest hash mismatch")
    item_keys = {
        "observation_ref",
        "observation_id",
        "observation_sha256",
        "plan_id",
        "entailment_request_id",
        "source_spans_sha256",
        "packet",
        "packet_text_sha256",
        "packet_sha256",
        "owner_manifest_sha256",
        "entailment_authorization_manifest_sha256",
        "canonical_text_sha256",
    }
    seen: dict[str, set[str]] = {
        "observation_id": set(),
        "plan_id": set(),
        "entailment_request_id": set(),
        "observation_ref": set(),
    }
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != item_keys:
            raise ProjectionStageError("manifest item fields mismatch")
        for key in seen:
            if item[key] in seen[key]:
                raise ProjectionStageError(f"duplicate {key}")
            seen[key].add(item[key])
        for key in ("observation_id", "plan_id", "entailment_request_id"):
            uuid.UUID(item[key])
        packet_text = stable_json(item["packet"])
        if (
            item["packet_text_sha256"]
            != hashlib.sha256(packet_text.encode("utf-8")).hexdigest()
            or item["packet_sha256"] != item["packet"]["packet_sha256"]
            or item["owner_manifest_sha256"]
            != owner_manifest_sha256(value["owner_user_id"], item["packet_sha256"])
            or item["canonical_text_sha256"]
            != canonical_hash(
                item["packet"]["projections"][0]["payload"]["canonical_text"]
            )
        ):
            raise ProjectionStageError("manifest item hash mismatch")
    if sorted(seen["observation_ref"]) != ["o00", "o01", "o02", "o03"]:
        raise ProjectionStageError("exact four-observation reference set mismatch")
    return value


def load_authorization(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(path.read_text())
    expected_keys = {
        "contract_version",
        "owner_user_id",
        "required_head_commit",
        "manifest_sha256",
        "authorized_action",
        "expected_new_rows",
        "authorization_sha256",
    }
    if set(value) != expected_keys:
        raise ProjectionStageError("authorization fields mismatch")
    if (
        value["contract_version"] != AUTHORIZATION_CONTRACT
        or value["owner_user_id"] != manifest["owner_user_id"]
        or value["required_head_commit"] != manifest["required_head_commit"]
        or value["manifest_sha256"] != manifest["manifest_sha256"]
        or value["authorized_action"] != CONFIRMATION
        or value["expected_new_rows"] != EXPECTED_NEW_ROWS
        or value["authorization_sha256"]
        != sha256(
            {key: item for key, item in value.items() if key != "authorization_sha256"}
        )
    ):
        raise ProjectionStageError("authorization mismatch")
    return value


async def set_actor(conn: Any, owner: str) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)


async def source_snapshot(
    conn: Any, observation_id: str, expected_owner: str, expected_evidence: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    source_row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
        uuid.UUID(observation_id),
    )
    entailment_row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_entailment_source_v5_2($1)",
        uuid.UUID(observation_id),
    )
    if source_row is None or entailment_row is None:
        raise ProjectionStageError("exact owner-scoped projection source not found")
    source = dict(source_row)
    spans = entailment_row["source_spans"]
    for field in ("object_literal", "project_scope", "temporal"):
        if isinstance(source.get(field), str):
            source[field] = json.loads(source[field])
    if isinstance(spans, str):
        spans = json.loads(spans)
    if not isinstance(spans, list) or not spans:
        raise ProjectionStageError("source spans are absent")
    if (
        str(source["owner_user_id"]) != expected_owner
        or str(source["evidence_id"]) != expected_evidence
        or str(entailment_row["evidence_id"]) != expected_evidence
        or entailment_row["observation_sha256"] != source["observation_sha256"]
        or source["predicate_registry_version"] != REGISTRY_VERSION
        or source["predicate"] != "stance.reported"
        or source["projection_class"] != "reported_stance"
        or source["surface_policy"] != "relevant_recall_or_explicit_recall"
        or source["modality"] != "reported_belief"
        or source["polarity"] != "affirmed"
        or source["object_kind"] != "literal"
        or source["object_entity_id"] is not None
        or source["subject_entity_type"] != "self"
        or source["subject_entity_status"] != "active"
        or source["evidence_status"] != "active"
    ):
        raise ProjectionStageError("source is outside the exact reported-stance boundary")
    return source, spans, entailment_row["observation_ref"]


async def prepare_item(
    conn: Any,
    owner: str,
    evidence: str,
    observation_id: str,
    plan_id: str,
    entailment_request_id: str,
    registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source, spans, observation_ref = await source_snapshot(
        conn, observation_id, owner, evidence
    )
    packet = build_packet(owner, source)
    validate_packet(packet, owner, registry)
    packet_text = stable_json(packet)
    packet_preflight = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
        uuid.UUID(plan_id),
        packet_text,
    )
    entailment_preflight = await conn.fetchrow(
        """SELECT * FROM memory.preflight_observation_entailment_v5(
             $1,'accepted'::memory.observation_entailment_decision_v5,
             'predicate_entailment_v5_1_accepted',$2::jsonb,$3,$4)""",
        uuid.UUID(observation_id),
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
        raise ProjectionStageError("projection source is not empty and stageable")
    return {
        "observation_ref": observation_ref,
        "observation_id": observation_id,
        "observation_sha256": source["observation_sha256"],
        "plan_id": plan_id,
        "entailment_request_id": entailment_request_id,
        "source_spans_sha256": canonical_hash(spans),
        "packet": packet,
        "packet_text_sha256": hashlib.sha256(packet_text.encode("utf-8")).hexdigest(),
        "packet_sha256": packet["packet_sha256"],
        "owner_manifest_sha256": packet_preflight["owner_manifest_sha256"],
        "entailment_authorization_manifest_sha256": entailment_preflight[
            "authorization_manifest_sha256"
        ],
        "canonical_text_sha256": canonical_hash(
            packet["projections"][0]["payload"]["canonical_text"]
        ),
    }


async def build_manifest(args: argparse.Namespace, conn: Any) -> dict[str, Any]:
    owner = str(uuid.UUID(args.owner))
    evidence = str(uuid.UUID(args.evidence))
    _, registry = load_contracts()
    transaction = conn.transaction(isolation="repeatable_read", readonly=True)
    await transaction.start()
    try:
        await set_actor(conn, owner)
        if len(args.observation) != 4:
            raise ProjectionStageError("exactly four observation IDs are required")
        observations = [str(uuid.UUID(value)) for value in args.observation]
        if len(set(observations)) != 4:
            raise ProjectionStageError("observation IDs must be unique")
        items = []
        for observation in observations:
            items.append(
                await prepare_item(
                    conn,
                    owner,
                    evidence,
                    observation,
                    stable_id("plan", owner, evidence, observation),
                    stable_id("entailment", owner, evidence, observation),
                    registry,
                )
            )
        items.sort(key=lambda item: item["observation_ref"])
        if [item["observation_ref"] for item in items] != ["o00", "o01", "o02", "o03"]:
            raise ProjectionStageError("evidence does not contain the exact four stances")
    finally:
        await transaction.rollback()
    manifest = {
        "contract_version": MANIFEST_CONTRACT,
        "target_server": "seebx",
        "owner_user_id": owner,
        "evidence_id": evidence,
        "required_head_commit": args.required_head,
        "predicate_registry_version": REGISTRY_VERSION,
        "assessor_type": ASSESSOR_TYPE,
        "assessor_ref": ASSESSOR_REF,
        "expected_new_rows": EXPECTED_NEW_ROWS,
        "expected_table_rows": EXPECTED_TABLE_ROWS,
        "items": items,
    }
    manifest["manifest_sha256"] = sha256(manifest)
    return manifest


async def validate_live_item(
    conn: Any,
    manifest: dict[str, Any],
    item: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    *,
    replay: bool,
) -> tuple[list[dict[str, Any]], str]:
    source, spans, observation_ref = await source_snapshot(
        conn,
        item["observation_id"],
        manifest["owner_user_id"],
        manifest["evidence_id"],
    )
    packet = build_packet(manifest["owner_user_id"], source)
    validate_packet(packet, manifest["owner_user_id"], registry)
    packet_text = stable_json(packet)
    if (
        observation_ref != item["observation_ref"]
        or source["observation_sha256"] != item["observation_sha256"]
        or canonical_hash(spans) != item["source_spans_sha256"]
        or packet != item["packet"]
        or hashlib.sha256(packet_text.encode("utf-8")).hexdigest()
        != item["packet_text_sha256"]
    ):
        raise ProjectionStageError("live deterministic source drifted")
    packet_preflight = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
        uuid.UUID(item["plan_id"]),
        packet_text,
    )
    entailment_preflight = await conn.fetchrow(
        """SELECT * FROM memory.preflight_observation_entailment_v5(
             $1,'accepted'::memory.observation_entailment_decision_v5,
             'predicate_entailment_v5_1_accepted',$2::jsonb,$3,$4)""",
        uuid.UUID(item["observation_id"]),
        stable_json(spans),
        ASSESSOR_TYPE,
        ASSESSOR_REF,
    )
    expected_plans = 1 if replay else 0
    if (
        packet_preflight["existing_aggregates"] != 0
        or packet_preflight["existing_plans"] != expected_plans
        or packet_preflight["owner_manifest_sha256"] != item["owner_manifest_sha256"]
        or entailment_preflight["authorization_manifest_sha256"]
        != item["entailment_authorization_manifest_sha256"]
    ):
        raise ProjectionStageError("live database preflight drifted")
    return spans, packet_text


async def apply_or_replay(
    args: argparse.Namespace,
    conn: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    authorization = load_authorization(
        review_path(args.authorization, exists=True), manifest
    )
    del authorization
    if args.confirm != CONFIRMATION:
        raise ProjectionStageError("confirmation phrase mismatch")
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD") != manifest["required_head_commit"]:
        raise ProjectionStageError("runtime head does not match manifest")
    if os.environ.get("MEMORY_V1_V5_2_PROJECTION_STAGE_BATCH_APPLY") != "authorized":
        raise ProjectionStageError("production apply environment authorization missing")
    _, registry = load_contracts()
    replay = args.command == "replay"
    transaction = conn.transaction(isolation="serializable")
    await transaction.start()
    rows_written = 0
    outcomes: list[dict[str, Any]] = []
    try:
        await set_actor(conn, manifest["owner_user_id"])
        prepared = []
        for item in manifest["items"]:
            spans, packet_text = await validate_live_item(
                conn, manifest, item, registry, replay=replay
            )
            prepared.append((item, spans, packet_text))
        for item, spans, packet_text in prepared:
            entailment = await conn.fetchrow(
                """SELECT * FROM memory.record_observation_entailment_v5(
                     $1,$2,'accepted'::memory.observation_entailment_decision_v5,
                     'predicate_entailment_v5_1_accepted',$3::jsonb,$4,$5,$6)""",
                uuid.UUID(item["entailment_request_id"]),
                uuid.UUID(item["observation_id"]),
                stable_json(spans),
                ASSESSOR_TYPE,
                ASSESSOR_REF,
                item["entailment_authorization_manifest_sha256"],
            )
            staged = await conn.fetchrow(
                "SELECT * FROM memory.stage_projection_plan_v5_2($1,$2,$3)",
                uuid.UUID(item["plan_id"]),
                packet_text,
                item["owner_manifest_sha256"],
            )
            # The single-plan stage function validates by switching every
            # deferred constraint to IMMEDIATE. Restore the transaction's
            # deferred mode before the next plan in this atomic batch.
            await conn.execute("SET CONSTRAINTS ALL DEFERRED")
            expected_outcome = "replayed" if replay else "applied"
            expected_rows = 0 if replay else 6
            item_rows = entailment["rows_written"] + staged["rows_written"]
            if (
                entailment["outcome"] != expected_outcome
                or staged["outcome"] != expected_outcome
                or item_rows != expected_rows
            ):
                raise ProjectionStageError("bounded stage outcome mismatch")
            rows_written += item_rows
            outcomes.append(
                {
                    "observation_id": item["observation_id"],
                    "plan_id": item["plan_id"],
                    "entailment_outcome": entailment["outcome"],
                    "projection_outcome": staged["outcome"],
                    "rows_written": item_rows,
                }
            )
        expected_total = 0 if replay else EXPECTED_NEW_ROWS
        if rows_written != expected_total:
            raise ProjectionStageError("transaction row budget mismatch")
        await transaction.commit()
    except BaseException:
        await transaction.rollback()
        raise
    return {
        "contract_version": RESULT_CONTRACT,
        "mode": args.command,
        "owner_user_id": manifest["owner_user_id"],
        "evidence_id": manifest["evidence_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(outcomes),
        "rows_written": rows_written,
        "outcomes": outcomes,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }


async def cross_owner_probe(
    args: argparse.Namespace, conn: Any, manifest: dict[str, Any]
) -> dict[str, Any]:
    other_owner = str(uuid.UUID(args.other_owner))
    if other_owner == manifest["owner_user_id"]:
        raise ProjectionStageError("cross-owner probe requires another owner")
    transaction = conn.transaction(isolation="repeatable_read", readonly=True)
    await transaction.start()
    rejected = False
    try:
        await set_actor(conn, other_owner)
        first = manifest["items"][0]
        try:
            await conn.fetchrow(
                "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
                uuid.UUID(first["plan_id"]),
                stable_json(first["packet"]),
            )
        except Exception:
            rejected = True
    finally:
        await transaction.rollback()
    if not rejected:
        raise ProjectionStageError("cross-owner projection preflight passed")
    return {
        "contract_version": RESULT_CONTRACT,
        "mode": "cross-owner",
        "owner_user_id": manifest["owner_user_id"],
        "other_owner_user_id": other_owner,
        "manifest_sha256": manifest["manifest_sha256"],
        "cross_owner_rejected": True,
        "rows_written": 0,
    }


async def run() -> int:
    import asyncpg

    args = arguments()
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ProjectionStageError("POSTGRES_DSN is required")
    output = review_path(args.output, exists=False)
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ProjectionStageError("POSTGRES_DSN must authenticate as brains_app")
        if args.command == "manifest":
            result = await build_manifest(args, conn)
        elif args.command == "authorize":
            raise AssertionError("authorize does not use the database path")
        else:
            manifest = load_manifest(review_path(args.manifest, exists=True))
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


def authorize_without_database(args: argparse.Namespace) -> int:
    manifest = load_manifest(review_path(args.manifest, exists=True))
    output = review_path(args.output, exists=False)
    authorization = {
        "contract_version": AUTHORIZATION_CONTRACT,
        "owner_user_id": manifest["owner_user_id"],
        "required_head_commit": manifest["required_head_commit"],
        "manifest_sha256": manifest["manifest_sha256"],
        "authorized_action": CONFIRMATION,
        "expected_new_rows": EXPECTED_NEW_ROWS,
    }
    authorization["authorization_sha256"] = sha256(authorization)
    write_private(output, authorization)
    print(f"authorization={output}")
    print(f"authorization_sha256={authorization['authorization_sha256']}")
    return 0


if __name__ == "__main__":
    parsed = arguments()
    if parsed.command == "authorize":
        raise SystemExit(authorize_without_database(parsed))
    # Reparse inside run so the async path remains import-testable.
    raise SystemExit(asyncio.run(run()))
