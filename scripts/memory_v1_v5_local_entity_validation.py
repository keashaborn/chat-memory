#!/usr/bin/env python3
"""Validate, stage, and resolve one new named entity on private infrastructure."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LlamaCppSecureTransport,
    LocalProviderAdapterError,
)
from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256
from scripts.memory_v1_v5_local_entity_validation_provider import (
    POLICY_VERSION,
    assess,
    governed_decision,
)
from scripts.memory_v1_v5_local_inference_canary import (
    PINNED_MODEL_ALIAS,
    PINNED_MODEL_FILE_SHA256,
    PINNED_RUNTIME_REVISION,
)
from scripts.memory_v1_v5_local_packet_disposition import (
    canonical_owners,
    loopback_dsn,
    sha256_text,
    stable_json,
)
from scripts.memory_v1_v5_local_packet_router import artifact_paths
from scripts.memory_v1_v5_stage_batch import (
    load_bundle,
    returned_counts,
    structural_counts,
)
from scripts.memory_v1_v5_stage_preflight import stable_json as packet_json


WORKER_VERSION = "memory_v1_v5_local_entity_validation_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_entity_validation_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("26bcb9f7-a05d-526f-9fcf-48bf37018fee")
DEFAULT_REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
REVIEW_REASON = "private source validation accepted at high confidence"


class LocalEntityValidationError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--review-root", default=str(DEFAULT_REVIEW_ROOT))
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:18080/v1/chat/completions"
    )
    parser.add_argument("--credential-name", default="local_api_key")
    parser.add_argument("--model", default=PINNED_MODEL_ALIAS)
    parser.add_argument("--model-file-sha256", default=PINNED_MODEL_FILE_SHA256)
    parser.add_argument("--runtime-revision", default=PINNED_RUNTIME_REVISION)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--max-output-tokens", type=int, default=128)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> None:
    if not 1.0 <= args.timeout_seconds <= 600.0:
        raise LocalEntityValidationError("timeout must be between 1 and 600 seconds")
    if not 64 <= args.max_output_tokens <= 512:
        raise LocalEntityValidationError("output token limit is invalid")
    if args.model != PINNED_MODEL_ALIAS:
        raise LocalEntityValidationError("local model alias is not pinned")
    if args.model_file_sha256 != PINNED_MODEL_FILE_SHA256:
        raise LocalEntityValidationError("local model file hash is not pinned")
    if args.runtime_revision != PINNED_RUNTIME_REVISION:
        raise LocalEntityValidationError("local runtime revision is not pinned")
    if args.apply and os.getenv("MEMORY_V1_V5_LOCAL_ENTITY_VALIDATION_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise LocalEntityValidationError("entity-validation capability is absent")


def secure_review_root(value: str) -> Path:
    root = Path(value).resolve(strict=True)
    metadata = root.stat()
    if (
        not root.is_dir()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise LocalEntityValidationError("review root must be owner-only mode 0700")
    return root


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_api_key(credential_name: str) -> str:
    directory = os.getenv("CREDENTIALS_DIRECTORY")
    if not directory:
        raise LocalEntityValidationError("systemd credential directory is unavailable")
    if not credential_name or "/" in credential_name or "\\" in credential_name:
        raise LocalEntityValidationError("credential name is invalid")
    key = (Path(directory) / credential_name).read_text(encoding="utf-8").strip()
    if not 32 <= len(key) <= 500 or any(character.isspace() for character in key):
        raise LocalEntityValidationError("local inference credential is invalid")
    return key


async def plan_owner(
    conn: asyncpg.Connection, owner: uuid.UUID
) -> dict[str, Any] | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        rows = await conn.fetch(
            "SELECT * FROM memory.plan_owner_v5_local_entity_validation_v1(1)"
        )
        assigned = await conn.fetchval("SELECT txid_current_if_assigned()")
    if assigned is not None:
        raise LocalEntityValidationError("read-only planning assigned a txid")
    if len(rows) > 1:
        raise LocalEntityValidationError("entity-validation planner exceeded budget")
    return dict(rows[0]) if rows else None


async def read_packet(
    conn: asyncpg.Connection, owner: uuid.UUID, packet_id: uuid.UUID
) -> dict[str, Any]:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        row = await conn.fetchrow(
            "SELECT * FROM memory.read_owner_v5_local_packet_review_v1($1)",
            packet_id,
        )
    if row is None:
        raise LocalEntityValidationError("owner-scoped packet read returned no row")
    return dict(row)


def load_target_bundle(
    *,
    root: Path,
    owner: uuid.UUID,
    target: dict[str, Any],
    packet_row: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    packet_id = uuid.UUID(str(target["packet_id"]))
    report_path, bundle_path = artifact_paths(root, packet_id)
    for path in (report_path, bundle_path):
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
        if (
            not resolved.is_relative_to(root)
            or not resolved.is_file()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise LocalEntityValidationError("review artifact security invariant failed")
    if sha256_file(report_path) != target["review_report_sha256"]:
        raise LocalEntityValidationError("review report differs from ledger")
    if sha256_file(bundle_path) != target["stage_bundle_sha256"]:
        raise LocalEntityValidationError("stage bundle differs from ledger")
    raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    extraction = json.loads(raw["extraction_packet_text"])
    resolution = json.loads(raw["resolution_packet_text"])
    counts = structural_counts(extraction, resolution)
    bundle = load_bundle(
        {
            "path": str(bundle_path),
            "sha256": target["stage_bundle_sha256"],
            "expected_outcome": "applied",
            "expected_counts": counts,
        },
        owner=owner,
        root=root,
    )
    summary = raw.get("resolution_summary")
    manual = [
        item
        for item in resolution["resolutions"]
        if item["decision_state"] == "manual_review_required"
    ]
    if (
        summary
        != {
            "auto_link_eligible": len(resolution["resolutions"]) - 1,
            "manual_review_required": 1,
            "deferred": 0,
            "rejected": 0,
        }
        or len(manual) != 1
        or manual[0]["action"] != "create_new"
        or manual[0]["selected_entity_id"] is not None
        or manual[0]["candidate_set"] != []
        or manual[0]["review_reason_codes"] != ["new_named_entity_requires_review"]
    ):
        raise LocalEntityValidationError("packet is outside named-entity policy")
    entity_ref = manual[0]["entity_ref"]
    mentions = [
        item for item in extraction["entity_mentions"] if item["entity_ref"] == entity_ref
    ]
    if len(mentions) != 1:
        raise LocalEntityValidationError("manual entity mention is not unique")
    mention = mentions[0]
    if (
        mention["mention_kind"] != "named"
        or mention["entity_type"] in {"self", "project"}
        or not str(mention.get("name_text") or "").strip()
        or manual[0]["mention_sha256"]
        != sha256_text(packet_json(mention))
    ):
        raise LocalEntityValidationError("manual entity mention is not admissible")
    evidence = str(packet_row["evidence_content"])
    span_text = " ".join(
        evidence[int(span["start"]) : int(span["end"])]
        for span in mention["source_spans"]
    )
    if str(mention["name_text"]).casefold() not in span_text.casefold():
        raise LocalEntityValidationError("named entity is absent from declared source spans")
    if (
        packet_row["packet_storage_sha256"] != target["packet_storage_sha256"]
        or packet_row["evidence_id"] != target["evidence_id"]
        or packet_row["evidence_authority_sha256"]
        != packet_row["evidence_content_sha256"]
        or bundle["evidence_id"] != target["evidence_id"]
    ):
        raise LocalEntityValidationError("packet, evidence, and bundle identities differ")
    return bundle, mention, manual[0]


def operation_ids(
    owner: uuid.UUID, artifact_id: uuid.UUID, entity_ref: str, mention_sha256: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    identity = f"{owner}|{artifact_id}|{entity_ref}|{mention_sha256}|{POLICY_VERSION}"
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"assessment|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"admission|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"stage-operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"review|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"apply|{identity}"),
    )


def auto_apply_request_id(
    owner: uuid.UUID, artifact_id: uuid.UUID, resolution_id: uuid.UUID
) -> uuid.UUID:
    return uuid.uuid5(
        IDENTITY_NAMESPACE,
        f"auto-apply|{owner}|{artifact_id}|{resolution_id}|{POLICY_VERSION}",
    )


async def register_assessment(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
    packet_row: dict[str, Any],
    mention: dict[str, Any],
    resolution: dict[str, Any],
    assessment: Any,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact_id = uuid.UUID(str(target["artifact_id"]))
    assessment_id, _, _, _, _ = operation_ids(
        owner, artifact_id, mention["entity_ref"], resolution["mention_sha256"]
    )
    decision, reason = governed_decision(assessment)

    async def invoke() -> dict[str, Any]:
        async with conn.transaction(isolation="serializable"):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            row = await conn.fetchrow(
                """
                SELECT * FROM memory.register_owner_v5_local_entity_validation_v1(
                  $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                  $16,$17,$18,$19,$20::jsonb,$21
                )
                """,
                assessment_id,
                artifact_id,
                target["packet_id"],
                target["evidence_id"],
                mention["entity_ref"],
                resolution["mention_sha256"],
                packet_row["evidence_content_sha256"],
                target["packet_storage_sha256"],
                target["stage_bundle_sha256"],
                canonical_sha256(args.model),
                args.model_file_sha256,
                canonical_sha256(args.runtime_revision),
                assessment.request_sha256,
                assessment.response_sha256,
                assessment.output_schema_sha256,
                assessment.decision,
                assessment.confidence,
                decision,
                reason,
                json.dumps(mention["source_spans"], separators=(",", ":")),
                POLICY_VERSION,
            )
        if row is None:
            raise LocalEntityValidationError("assessment register returned no row")
        return dict(row)

    return await invoke(), await invoke()


async def stage_and_resolve(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
    bundle: dict[str, Any],
    mention: dict[str, Any],
    resolution: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact_id = uuid.UUID(str(target["artifact_id"]))
    assessment_id = uuid.UUID(str(target["assessment_id"]))
    _, admission_id, stage_operation_id, review_request_id, apply_request_id = operation_ids(
        owner, artifact_id, mention["entity_ref"], resolution["mention_sha256"]
    )

    async with conn.transaction(isolation="serializable"):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"{owner}|{artifact_id}|validated_new_entity_v1",
        )
        staged = await conn.fetchrow(
            """SELECT batch_id,outcome,mentions_inserted,resolutions_inserted,
                      candidates_inserted,observations_inserted,
                      temporals_inserted,result
               FROM memory.stage_relational_packet_v5(
                 $1,$2,$3,$4,$5,$6,$7,$8
               )""",
            bundle["request_id"],
            bundle["evidence_id"],
            bundle["extractor"],
            bundle["extractor_version"],
            bundle["extraction_packet_text"],
            bundle["resolution_packet_text"],
            bundle["extraction_packet_sha256"],
            bundle["resolution_packet_sha256"],
        )
        if staged is None or staged["outcome"] != "applied":
            raise LocalEntityValidationError("validated packet did not stage")
        if returned_counts(dict(staged)) != bundle["expected_counts"]:
            raise LocalEntityValidationError("validated stage row budget drifted")
        admitted = await conn.fetchrow(
            """SELECT * FROM memory.register_owner_v5_local_validated_stage_v1(
                 $1,$2,$3,$4,$5,$6,$7
               )""",
            admission_id,
            stage_operation_id,
            assessment_id,
            artifact_id,
            staged["batch_id"],
            target["stage_bundle_sha256"],
            POLICY_VERSION,
        )
        if admitted is None or admitted["outcome"] != "applied":
            raise LocalEntityValidationError("validated stage was not admitted")
        auto_values = admitted["auto_resolution_ids"]
        if isinstance(auto_values, str):
            auto_values = json.loads(auto_values)
        auto_ids = [uuid.UUID(str(value)) for value in auto_values]
        auto_results: list[dict[str, Any]] = []
        for auto_resolution_id in auto_ids:
            auto_preflight = await conn.fetchrow(
                "SELECT * FROM memory.preflight_entity_resolution_apply_v5($1,NULL)",
                auto_resolution_id,
            )
            auto_applied = await conn.fetchrow(
                "SELECT * FROM memory.apply_entity_resolution_v5($1,$2,NULL,$3)",
                auto_apply_request_id(owner, artifact_id, auto_resolution_id),
                auto_resolution_id,
                auto_preflight["apply_manifest_sha256"],
            )
            if auto_applied is None or auto_applied["outcome"] != "applied":
                raise LocalEntityValidationError("auto entity resolution failed")
            auto_results.append({
                "resolution_id":auto_resolution_id,
                "apply_manifest":auto_preflight["apply_manifest_sha256"],
                "entity_id":auto_applied["applied_entity_id"],
            })
        resolution_id = admitted["resolution_id"]
        review_preflight = await conn.fetchrow(
            """SELECT * FROM memory.preflight_entity_resolution_review_v5(
                 $1,'approved'::memory.entity_review_decision,$2
               )""",
            resolution_id,
            REVIEW_REASON,
        )
        reviewed = await conn.fetchrow(
            """SELECT * FROM memory.review_entity_resolution_v5(
                 $1,$2,'approved'::memory.entity_review_decision,$3,$4
               )""",
            review_request_id,
            resolution_id,
            REVIEW_REASON,
            review_preflight["authorization_manifest_sha256"],
        )
        if reviewed is None or reviewed["outcome"] != "applied":
            raise LocalEntityValidationError("entity resolution review failed")
        review_id = reviewed["review_id"]
        apply_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_entity_resolution_apply_v5($1,$2)",
            resolution_id,
            review_id,
        )
        applied = await conn.fetchrow(
            "SELECT * FROM memory.apply_entity_resolution_v5($1,$2,$3,$4)",
            apply_request_id,
            resolution_id,
            review_id,
            apply_preflight["apply_manifest_sha256"],
        )
        if applied is None or applied["outcome"] != "applied":
            raise LocalEntityValidationError("validated entity resolution failed")
        first = {
            "batch_id": staged["batch_id"],
            "resolution_id": resolution_id,
            "review_id": review_id,
            "review_manifest": review_preflight["authorization_manifest_sha256"],
            "apply_manifest": apply_preflight["apply_manifest_sha256"],
            "entity_id": applied["applied_entity_id"],
            "bindings_created": int(applied["bindings_created"]),
            "auto_results": auto_results,
        }

    async with conn.transaction(isolation="serializable"):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        replay_stage = await conn.fetchrow(
            """SELECT batch_id,outcome,mentions_inserted,resolutions_inserted,
                      candidates_inserted,observations_inserted,
                      temporals_inserted,result
               FROM memory.stage_relational_packet_v5(
                 $1,$2,$3,$4,$5,$6,$7,$8
               )""",
            bundle["request_id"],bundle["evidence_id"],bundle["extractor"],
            bundle["extractor_version"],bundle["extraction_packet_text"],
            bundle["resolution_packet_text"],bundle["extraction_packet_sha256"],
            bundle["resolution_packet_sha256"],
        )
        replay_admission = await conn.fetchrow(
            "SELECT * FROM memory.register_owner_v5_local_validated_stage_v1($1,$2,$3,$4,$5,$6,$7)",
            admission_id,stage_operation_id,assessment_id,artifact_id,first["batch_id"],
            target["stage_bundle_sha256"],POLICY_VERSION,
        )
        replay_auto: list[Any] = []
        for auto_result in first["auto_results"]:
            replay_auto.append(await conn.fetchrow(
                "SELECT * FROM memory.apply_entity_resolution_v5($1,$2,NULL,$3)",
                auto_apply_request_id(
                    owner,artifact_id,auto_result["resolution_id"]
                ),
                auto_result["resolution_id"],auto_result["apply_manifest"],
            ))
        replay_review = await conn.fetchrow(
            """SELECT * FROM memory.review_entity_resolution_v5(
                 $1,$2,'approved'::memory.entity_review_decision,$3,$4
               )""",
            review_request_id,first["resolution_id"],REVIEW_REASON,
            first["review_manifest"],
        )
        replay_apply = await conn.fetchrow(
            "SELECT * FROM memory.apply_entity_resolution_v5($1,$2,$3,$4)",
            apply_request_id,first["resolution_id"],first["review_id"],
            first["apply_manifest"],
        )
    if (
        replay_stage["outcome"] != "replayed"
        or replay_admission["outcome"] != "replayed"
        or int(replay_admission["rows_written"]) != 0
        or replay_review["outcome"] != "replayed"
        or replay_apply["outcome"] != "replayed"
        or any(
            row["outcome"] != "replayed" or int(row["bindings_created"]) != 0
            for row in replay_auto
        )
        or int(replay_apply["bindings_created"]) != 0
        or replay_apply["applied_entity_id"] != first["entity_id"]
    ):
        raise LocalEntityValidationError("validated entity replay invariant failed")
    return first, {
        "stage": replay_stage["outcome"],
        "admission": replay_admission["outcome"],
        "review": replay_review["outcome"],
        "apply": replay_apply["outcome"],
    }


def sanitized_plan(owner: uuid.UUID, row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "artifact_id_sha256": sha256_text(str(row["artifact_id"])) if row else None,
        "route": str(row["route"]) if row else "no_work",
    }


async def run() -> int:
    args = arguments()
    validate_arguments(args)
    owners = canonical_owners(args.owner_user_id)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise LocalEntityValidationError("POSTGRES_DSN is required")
    root = secure_review_root(args.review_root)
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=90, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise LocalEntityValidationError("worker requires brains_app")
        plans = [(owner, await plan_owner(conn, owner)) for owner in owners]
        selected = next(((owner, row) for owner, row in plans if row), None)
        safe_plans = [sanitized_plan(owner, row) for owner, row in plans]
        if not args.apply:
            print(stable_json({
                "worker_version":WORKER_VERSION,"apply":False,"plans":safe_plans,
                "database_writes":0,"local_model_calls":0,
                "external_model_calls":0,"entity_writes":0,"claim_writes":0,
                "qdrant_writes":0,"prompt_influence":0,"filesystem_writes":0,
            }))
            return 0
        if selected is None:
            print(stable_json({
                "worker_version":WORKER_VERSION,"apply":True,"outcome":"no_work",
                "plans":safe_plans,"database_rows_created":0,
                "local_model_calls":0,"external_model_calls":0,
                "entity_writes":0,"claim_writes":0,"qdrant_writes":0,
                "prompt_influence":0,"filesystem_writes":0,
                "zero_write_replay_proved":True,
            }))
            return 0
        owner, target = selected
        packet_row = await read_packet(conn, owner, uuid.UUID(str(target["packet_id"])))
        bundle, mention, resolution = load_target_bundle(
            root=root,owner=owner,target=target,packet_row=packet_row
        )
        if target["route"] == "validate_new_entity":
            transport = LlamaCppSecureTransport(
                endpoint=args.endpoint,
                enable_token=LOCAL_CALL_ENABLE_TOKEN,
                api_key=load_api_key(args.credential_name),
                allow_loopback_http=True,
            )
            assessment = assess(
                transport,model=args.model,
                evidence_content=str(packet_row["evidence_content"]),
                entity_mention=mention,
                timeout_seconds=args.timeout_seconds,
                max_output_tokens=args.max_output_tokens,
            )
            first,replay = await register_assessment(
                conn,owner=owner,target=target,packet_row=packet_row,
                mention=mention,resolution=resolution,assessment=assessment,args=args,
            )
            if (
                first["outcome"] != "applied" or replay["outcome"] != "replayed"
                or int(first["rows_written"]) != 1 or int(replay["rows_written"]) != 0
            ):
                raise LocalEntityValidationError("assessment replay invariant failed")
            print(stable_json({
                "worker_version":WORKER_VERSION,"apply":True,
                "outcome":"entity_validation_recorded","plans":safe_plans,
                "governed_decision":first["decision"],"database_rows_created":1,
                "local_model_calls":assessment.local_model_calls,
                "external_model_calls":0,"entity_writes":0,"claim_writes":0,
                "qdrant_writes":0,"prompt_influence":0,"filesystem_writes":0,
                "zero_write_replay_proved":True,
            }))
            return 0
        if target["route"] != "stage_validated_entity":
            raise LocalEntityValidationError("unknown entity-validation route")
        first,replay = await stage_and_resolve(
            conn,owner=owner,target=target,bundle=bundle,
            mention=mention,resolution=resolution,
        )
        stage_rows = sum(bundle["expected_counts"].values()) + 2
        print(stable_json({
            "worker_version":WORKER_VERSION,"apply":True,
            "outcome":"validated_entity_staged_and_resolved","plans":safe_plans,
            "database_rows_created":stage_rows + 8
              + 2 * len(first["auto_results"]) + first["bindings_created"],
            "bindings_created":first["bindings_created"],
            "local_model_calls":0,"external_model_calls":0,
            "entity_writes":1,"claim_writes":0,"qdrant_writes":0,
            "prompt_influence":0,"filesystem_writes":0,
            "zero_write_replay_proved":all(value=="replayed" for value in replay.values()),
        }))
        return 0
    finally:
        await conn.close()


def guarded_main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        code = exc.code if isinstance(exc, LocalProviderAdapterError) else None
        print(stable_json({
            "worker_version":WORKER_VERSION,"apply":False,
            "outcome":"local_entity_validation_error",
            "error_code":code or type(exc).__name__,
            "error_sha256":sha256_text(str(exc)),"external_model_calls":0,
            "claim_writes":0,"qdrant_writes":0,"prompt_influence":0,
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
