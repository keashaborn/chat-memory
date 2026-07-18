#!/usr/bin/env python3
"""Build one zero-write, owner-scoped V5 project-current-state projection."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any
import uuid

from scripts.memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    semantic_key_sha256,
    sha256,
    stable_json,
    validate_packet,
    validate_projection_schema,
    validate_registry,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "memory_v1_project_projection_stage_bundle_v1"
PROJECTOR = "memory_v1_deterministic_project_projection_v5"
PROJECTOR_VERSION = "project_current_state_v1"
KNOWLEDGE_KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,239}$")


class ProjectProjectionPreflightError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--knowledge-key", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def repository_state() -> str:
    status = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise ProjectProjectionPreflightError("project preflight requires a clean worktree")
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def load_contract() -> dict[str, dict[str, Any]]:
    schema = json.loads(
        (ROOT / "specs/memory_v1_projection_plan_v5.schema.json").read_text()
    )
    registry = json.loads(
        (ROOT / "specs/memory_v1_predicate_registry_v5.json").read_text()
    )
    validate_projection_schema(schema)
    return validate_registry(registry)


def object_literal_sha256(value: dict[str, Any]) -> str:
    return sha256(value)


def validate_source(row: dict[str, Any]) -> None:
    expected = {
        "predicate": "project.current_state",
        "predicate_registry_version": "memory_predicate_registry_v5",
        "polarity": "affirmed",
        "modality": "asserted",
        "projection_class": "project_knowledge",
        "surface_policy": "exact_project_scope_only",
        "subject_entity_type": "project",
        "subject_entity_status": "active",
        "evidence_status": "active",
        "binding_source": "trusted_component_registry",
    }
    for field, value in expected.items():
        if str(row[field]) != value:
            raise ProjectProjectionPreflightError(
                f"project source {field} drifted from {value!r}"
            )
    literal = row["object_literal"]
    if isinstance(literal, str):
        literal = json.loads(literal)
        row["object_literal"] = literal
    if not isinstance(literal, dict) or set(literal) != {
        "kind", "datatype", "value", "unit", "approximate"
    }:
        raise ProjectProjectionPreflightError("project object literal shape is invalid")
    if (
        literal["kind"] != "literal"
        or literal["datatype"] != "text"
        or not isinstance(literal["value"], str)
        or not literal["value"].strip()
        or len(literal["value"]) > 4000
        or literal["unit"] is not None
        or literal["approximate"] is not False
    ):
        raise ProjectProjectionPreflightError("project object literal is not exact text")
    if object_literal_sha256(literal) != row["object_literal_sha256"]:
        raise ProjectProjectionPreflightError("project object literal hash mismatch")
    for field in (
        "observation_sha256",
        "evidence_content_sha256",
        "temporal_normalized_sha256",
    ):
        if not isinstance(row[field], str) or not re.fullmatch(
            r"[0-9a-f]{64}", row[field]
        ):
            raise ProjectProjectionPreflightError(f"project source {field} is invalid")
    for field in (
        "temporal_semantic",
        "temporal_shape",
        "temporal_basis",
        "temporal_source_form",
        "temporal_certainty",
        "temporal_precision",
    ):
        if not isinstance(row[field], str) or not row[field]:
            raise ProjectProjectionPreflightError(f"project source {field} is absent")
    if row["evidence_observed_at"] is None:
        raise ProjectProjectionPreflightError("project evidence observed_at is absent")
    uuid.UUID(str(row["project_id"]))
    if not isinstance(row["project_key"], str) or not row["project_key"]:
        raise ProjectProjectionPreflightError("project key is absent")
    if not isinstance(row["component_key"], str) or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,99}", row["component_key"]
    ):
        raise ProjectProjectionPreflightError("component key is invalid")


def build_projection(
    owner: str,
    row: dict[str, Any],
    knowledge_key: str,
) -> dict[str, Any]:
    if not KNOWLEDGE_KEY_RE.fullmatch(knowledge_key):
        raise ProjectProjectionPreflightError("knowledge key is invalid")
    payload = {
        "kind": "project_knowledge",
        "project_id": str(row["project_id"]),
        "component_key": row["component_key"],
        "binding_source": row["binding_source"],
        "knowledge_kind": "current_state",
        "knowledge_key": knowledge_key,
        "canonical_text": row["object_literal"]["value"],
        "document_state": "working",
        "authority_level": "user_reported",
        "surface_policy": "exact_project_scope_only",
    }
    identity = {
        "subject_entity_id": str(row["subject_entity_id"]),
        "predicate": row["predicate"],
        "object_kind": "literal",
        "object_entity_id": None,
        "object_literal_sha256": row["object_literal_sha256"],
        "polarity": row["polarity"],
        "modality": row["modality"],
        "semantic_key_sha256": "",
    }
    projection = {
        "projection_ref": "p01",
        "lane": "project_knowledge",
        "observation_inputs": [{
            "observation_id": str(row["observation_id"]),
            "observation_sha256": row["observation_sha256"],
            "stance": "supports",
        }],
        "identity": identity,
        "target": {
            "action": "create",
            "aggregate_id": None,
            "expected_revision_number": None,
            "reason_codes": [],
        },
        "temporal_policy": {
            "canonical_source": "memory.observation_temporal",
            "materialization": "link_only",
            "source_observation_id": None,
        },
        "review": {
            "state": "manual_review_required",
            "authorization_required": True,
            "reason_codes": ["project_policy_requires_review"],
        },
        "relations": [],
        "payload": payload,
    }
    identity["semantic_key_sha256"] = semantic_key_sha256(
        owner, "project_knowledge", identity, payload
    )
    return projection


def build_packet(projection: dict[str, Any]) -> dict[str, Any]:
    packet = {
        "contract_version": "memory_v1_projection_plan_v5",
        "predicate_registry_version": "memory_predicate_registry_v5",
        "projection_policy_version": "memory_projection_policy_v5",
        "projector": PROJECTOR,
        "projector_version": PROJECTOR_VERSION,
        "projections": [projection],
        "packet_sha256": "",
    }
    packet["packet_sha256"] = sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    return packet


async def load_source(conn: Any, observation_id: uuid.UUID) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_project_projection_source_v5($1)",
        observation_id,
    )
    if row is None:
        raise ProjectProjectionPreflightError("project projection source is absent")
    return dict(row)


async def assert_database_hashes(
    conn: Any,
    owner: uuid.UUID,
    plan_id: uuid.UUID,
    projection: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, str]:
    packet_text = stable_json(packet)
    expected = {
        "packet_text_sha256": hashlib.sha256(packet_text.encode()).hexdigest(),
        "semantic_key_sha256": projection["identity"]["semantic_key_sha256"],
        "projection_sha256": sha256(projection),
        "packet_sha256": packet["packet_sha256"],
        "owner_manifest_sha256": owner_manifest_sha256(
            str(owner), packet["packet_sha256"]
        ),
    }
    database = await conn.fetchrow(
        "SELECT * FROM memory.preflight_project_projection_packet_v5($1,$2)",
        plan_id,
        packet_text,
    )
    for key, value in expected.items():
        if database[key] != value:
            raise ProjectProjectionPreflightError(f"database {key} mismatch")
    if database["existing_aggregates"] != 0 or database["existing_plans"] != 0:
        raise ProjectProjectionPreflightError("project target or plan already exists")
    return {
        "packet_text": packet_text,
        **expected,
    }


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(payload).hexdigest()


async def async_main() -> int:
    import asyncpg

    args = arguments()
    owner = uuid.UUID(args.owner)
    observation_id = uuid.UUID(args.observation_id)
    plan_id = uuid.UUID(args.plan_id)
    commit = repository_state()
    registry = load_contract()
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ProjectProjectionPreflightError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ProjectProjectionPreflightError("POSTGRES_DSN must use brains_app")
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            source = await load_source(conn, observation_id)
            validate_source(source)
            projection = build_projection(str(owner), source, args.knowledge_key)
            packet = build_packet(projection)
            validate_packet(packet, str(owner), registry)
            hashes = await assert_database_hashes(
                conn, owner, plan_id, projection, packet
            )
    finally:
        await conn.close()
    bundle = {
        "contract_version": CONTRACT,
        "owner_user_id": str(owner),
        "plan_id": str(plan_id),
        "projection_ref": "p01",
        "projector_commit": commit,
        "packet": packet,
        **hashes,
        "lane_scope": {
            "project_id": str(source["project_id"]),
            "component_key": source["component_key"],
            "binding_source": source["binding_source"],
            "knowledge_kind": "current_state",
            "knowledge_key": args.knowledge_key,
        },
        "source_snapshot": {
            "observation_id": str(source["observation_id"]),
            "observation_sha256": source["observation_sha256"],
            "evidence_id": str(source["evidence_id"]),
            "evidence_content_sha256": source["evidence_content_sha256"],
            "subject_entity_id": str(source["subject_entity_id"]),
            "object_literal_sha256": source["object_literal_sha256"],
            "temporal_normalized_sha256": source["temporal_normalized_sha256"],
        },
        "database_writes": 0,
        "external_model_calls": 0,
        "qdrant_writes": 0,
    }
    bundle["bundle_sha256"] = sha256(bundle)
    output = Path(args.output).resolve()
    file_sha = secure_write(output, bundle)
    print(stable_json({
        "contract_version": CONTRACT,
        "output": str(output),
        "file_sha256": file_sha,
        "bundle_sha256": bundle["bundle_sha256"],
        "packet_sha256": bundle["packet_sha256"],
        "owner_manifest_sha256": bundle["owner_manifest_sha256"],
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
