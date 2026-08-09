#!/usr/bin/env python3
"""Build one owner-scoped V5.2 review bundle from an OpenAI extraction packet."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import uuid
from typing import Any

import asyncpg

from scripts.memory_v1_relational_extraction_v5_provider import (
    NormalizedPacket,
    canonical_sha256,
)
from scripts.memory_v1_v5_1_review_local_packet import (
    LocalPacketReviewError,
    _candidates,
    _json_value,
    _output_path,
    _repository_state,
    _schema_hash,
    _secure_root,
    _secure_write,
    _validate_extraction_packet,
    _validate_resolution_packet,
    _verify_source,
    normalize_review_packet,
    packet_quality_findings,
    resolve_mention,
    review_artifact_eligible,
    review_profile,
    sha256_text,
    stable_json,
    REQUEST_NAMESPACE,
    RESOLVER,
    RESOLVER_VERSION,
)


REVIEW_CONTRACT = "memory_v1_v5_2_openai_packet_review_v1"
BUNDLE_CONTRACT = "memory_v1_v5_2_stage_preflight_v1"
REVIEW_NAMESPACE = uuid.UUID("b8cb8e91-92b6-5acd-a188-8345125f9b50")
DEFAULT_REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
EXPECTED_PROVIDER_ID = "openai_responses"
EXPECTED_PROVIDER_VERSION = "v1"


class OpenAIPacketReviewError(LocalPacketReviewError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read one immutable OpenAI V5.2 packet through the owner-scoped API "
            "and emit a deterministic, zero-write entity-resolution review bundle."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--packet-id", required=True)
    parser.add_argument("--review-report", required=True)
    parser.add_argument("--stage-bundle", required=True)
    parser.add_argument("--review-root", default=str(DEFAULT_REVIEW_ROOT))
    parser.add_argument(
        "--extraction-schema",
        default="specs/memory_v1_relational_extraction_v5_2.schema.json",
    )
    parser.add_argument(
        "--resolution-schema",
        default="specs/memory_v1_entity_resolution_review_v5_2.schema.json",
    )
    return parser.parse_args()


def _sha256_valid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def openai_model_call_provenance_valid(
    *, local_model_calls: Any, external_model_calls: Any
) -> bool:
    return local_model_calls == 0 and external_model_calls == 1


def validate_exact_route_preflight(
    rows: list[dict[str, Any]], *, packet_id: uuid.UUID
) -> None:
    if (
        len(rows) != 1
        or uuid.UUID(str(rows[0].get("packet_id"))) != packet_id
        or rows[0].get("route") != "manual_review_artifact_ready"
    ):
        raise OpenAIPacketReviewError(
            "OpenAI packet is not eligible for exact review routing"
        )


def validate_openai_row(row: dict[str, Any], packet: dict[str, Any]) -> None:
    hash_fields = (
        "evidence_content_sha256",
        "provider_model_sha256",
        "provider_output_sha256",
        "validator_packet_sha256",
        "packet_storage_sha256",
        "evidence_authority_sha256",
    )
    if any(not _sha256_valid(row.get(field)) for field in hash_fields):
        raise OpenAIPacketReviewError("OpenAI packet provenance contains an invalid hash")
    if (
        row.get("provider_id") != EXPECTED_PROVIDER_ID
        or row.get("provider_version") != EXPECTED_PROVIDER_VERSION
    ):
        raise OpenAIPacketReviewError("packet is not from the approved OpenAI provider")
    if not openai_model_call_provenance_valid(
        local_model_calls=row.get("local_model_calls"),
        external_model_calls=row.get("external_model_calls"),
    ):
        raise OpenAIPacketReviewError("OpenAI packet model-call provenance is invalid")
    if row.get("storage_integrity_verified") is not True:
        raise OpenAIPacketReviewError("OpenAI packet storage integrity was not verified")
    if canonical_sha256(packet) != row.get("validator_packet_sha256"):
        raise OpenAIPacketReviewError("immutable normalized packet hash mismatch")
    if not review_artifact_eligible(row):
        raise OpenAIPacketReviewError("OpenAI packet has no reviewable relational content")
    if (
        row.get("job_status") != "review_required"
        or row.get("job_route") != "relational_extraction"
        or row.get("job_lease_present")
        or row.get("job_error_present")
    ):
        raise OpenAIPacketReviewError("OpenAI packet job is not review-ready")
    if row.get("evidence_status") != "active":
        raise OpenAIPacketReviewError("OpenAI packet evidence is not active")
    if row.get("evidence_content_sha256") != row.get("evidence_authority_sha256"):
        raise OpenAIPacketReviewError("packet and evidence authority hashes differ")
    if row.get("exact_stage_batch_count") or row.get("evidence_stage_batch_count"):
        raise OpenAIPacketReviewError("evidence already has relational staging state")
    expected_counts = (
        len(packet["entity_mentions"]),
        len(packet["observations"]),
        len(packet["comparison_hints"]),
        len(packet["deferrals"]),
    )
    actual_counts = (
        row.get("entity_mention_count"),
        row.get("observation_count"),
        row.get("comparison_hint_count"),
        row.get("deferral_count"),
    )
    if actual_counts != expected_counts:
        raise OpenAIPacketReviewError("OpenAI packet row counts do not match payload")
    if packet["source_envelope"]["job_id"] != str(row.get("job_id")):
        raise OpenAIPacketReviewError("source envelope job ID is not packet-bound")


async def build(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise OpenAIPacketReviewError("POSTGRES_DSN is required")
    owner = uuid.UUID(args.owner_user_id)
    packet_id = uuid.UUID(args.packet_id)
    profile = review_profile("v5_2")
    repo_root, commit = _repository_state()

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise OpenAIPacketReviewError("review requires a brains_app session")
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            route_rows = await conn.fetch(
                "SELECT * FROM memory.plan_owner_v5_2_exact_packet_route_v1($1)",
                packet_id,
            )
            validate_exact_route_preflight(
                [dict(route_row) for route_row in route_rows], packet_id=packet_id
            )
            rows = await conn.fetch(
                """
                SELECT
                  packet.*,
                  0::smallint AS local_model_calls,
                  job.status::text AS job_status,
                  job.route AS job_route,
                  (job.lease_token IS NOT NULL OR job.lease_expires_at IS NOT NULL)
                    AS job_lease_present,
                  (job.last_error IS NOT NULL) AS job_error_present,
                  evidence.source_system AS evidence_source_system,
                  CASE
                    WHEN evidence.external_id ~*
                      '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                      THEN lower(evidence.external_id)
                    ELSE evidence.evidence_id::text
                  END AS evidence_external_id,
                  evidence.content AS evidence_content,
                  evidence.content_sha256 AS evidence_authority_sha256,
                  evidence.status::text AS evidence_status,
                  (
                    encode(public.digest(convert_to(
                      packet.normalized_packet::text,'UTF8'
                    ),'sha256'),'hex')=packet.packet_storage_sha256
                  ) AS storage_integrity_verified,
                  0::bigint AS exact_stage_batch_count,
                  0::bigint AS evidence_stage_batch_count
                FROM memory.evidence_extraction_packet_v5 AS packet
                JOIN memory.evidence_extraction_job AS job
                  ON job.owner_user_id=packet.owner_user_id
                 AND job.job_id=packet.job_id
                 AND job.evidence_id=packet.evidence_id
                JOIN memory.evidence AS evidence
                  ON evidence.owner_user_id=packet.owner_user_id
                 AND evidence.evidence_id=packet.evidence_id
                WHERE packet.packet_id=$1
                """,
                packet_id,
            )
            if len(rows) != 1:
                raise OpenAIPacketReviewError("owner-scoped immutable packet not found")
            row = dict(rows[0])
            packet = _json_value(row["normalized_packet"], "normalized packet")
            validate_openai_row(row, packet)
            immutable = NormalizedPacket.model_validate(packet).model_dump(mode="json")
            if canonical_sha256(immutable) != row["validator_packet_sha256"]:
                raise OpenAIPacketReviewError("validated packet differs from immutable packet")
            evidence = {
                "status": row["evidence_status"],
                "source_system": row["evidence_source_system"],
                "external_id": row["evidence_external_id"],
                "content": row["evidence_content"],
                "content_sha256": row["evidence_authority_sha256"],
            }
            _verify_source(immutable, evidence)
            reviewed, transformations = normalize_review_packet(
                immutable, evidence["content"]
            )
            _validate_extraction_packet(
                reviewed,
                extraction_contract=profile.extraction_contract,
                registry_version=profile.registry_version,
            )
            validated = NormalizedPacket.model_validate(reviewed).model_dump(mode="json")
            resolutions = [
                resolve_mention(
                    mention,
                    validated["observations"],
                    await _candidates(
                        conn, owner, mention, validated["observations"]
                    ),
                    validated["deferrals"],
                )
                for mention in validated["entity_mentions"]
            ]
            txid_assigned = await conn.fetchval("SELECT txid_current_if_assigned()")
    finally:
        await conn.close()

    resolution_body = {
        "contract_version": profile.resolution_contract,
        "source_envelope": validated["source_envelope"],
        "predicate_registry_version": profile.registry_version,
        "entity_normalization_version": "memory_entity_normalization_v5",
        "resolver": RESOLVER,
        "resolver_version": RESOLVER_VERSION,
        "resolutions": resolutions,
    }
    resolution = dict(resolution_body)
    resolution["packet_sha256"] = sha256_text(stable_json(resolution_body))
    _validate_resolution_packet(
        resolution,
        validated,
        resolution_contract=profile.resolution_contract,
        registry_version=profile.registry_version,
    )
    extraction_text = stable_json(validated)
    resolution_text = stable_json(resolution)
    extraction_sha = sha256_text(extraction_text)
    resolution_sha = sha256_text(resolution_text)
    evidence_id = str(row["evidence_id"])
    request_id = str(
        uuid.uuid5(
            REQUEST_NAMESPACE,
            "|".join(
                (str(owner), evidence_id, extraction_sha, resolution_sha, RESOLVER_VERSION)
            ),
        )
    )
    review_id = str(
        uuid.uuid5(
            REVIEW_NAMESPACE,
            "|".join(
                (
                    str(owner),
                    str(packet_id),
                    row["packet_storage_sha256"],
                    extraction_sha,
                    resolution_sha,
                )
            ),
        )
    )
    resolution_summary = {
        state: sum(item["decision_state"] == state for item in resolutions)
        for state in (
            "auto_link_eligible",
            "manual_review_required",
            "deferred",
            "rejected",
        )
    }
    findings = packet_quality_findings(validated)
    blocking_codes = sorted({item["code"] for item in findings if item["blocking"]})
    created_at = row["created_at"]
    if not isinstance(created_at, dt.datetime):
        raise OpenAIPacketReviewError("packet creation time is invalid")
    now = created_at.astimezone(dt.timezone.utc).isoformat()
    review = {
        "contract_version": REVIEW_CONTRACT,
        "mode": "owner_scoped_openai_packet_review_zero_write",
        "generated_at": now,
        "owner_user_id": str(owner),
        "review_id": review_id,
        "packet_id": str(packet_id),
        "operation_id": str(row["operation_id"]),
        "job_id": str(row["job_id"]),
        "evidence_id": evidence_id,
        "evidence_content_sha256": row["evidence_content_sha256"],
        "validator_packet_sha256": row["validator_packet_sha256"],
        "packet_storage_sha256": row["packet_storage_sha256"],
        "derived_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "provider_provenance": {
            field: row[field]
            for field in (
                "provider_id",
                "provider_version",
                "provider_model_sha256",
                "provider_output_sha256",
            )
        },
        "repository_commit": commit,
        "deterministic_transformations": transformations,
        "resolution_summary": resolution_summary,
        "quality_findings": findings,
        "blocking_codes": blocking_codes,
        "review_disposition": "manual_review_required",
        "zero_write_proof": {
            "database_writes": 0,
            "qdrant_writes": 0,
            "external_model_calls": 0,
            "readonly_repeatable_read_transaction": True,
            "transaction_id_assigned": txid_assigned is not None,
        },
    }
    bundle = {
        "contract_version": BUNDLE_CONTRACT,
        "generated_at": now,
        "mode": "preflight_only_zero_write",
        "server": "seebx",
        "owner_user_id": str(owner),
        "case_id": f"openai-packet-{packet_id}",
        "evidence_id": evidence_id,
        "request_id": request_id,
        "extractor": "memory_v1_v5_2_openai_packet_review",
        "extractor_version": commit,
        "source_report": {},
        "schemas": {
            "extraction_sha256": _schema_hash(
                (repo_root / args.extraction_schema).resolve()
            ),
            "resolution_sha256": _schema_hash(
                (repo_root / args.resolution_schema).resolve()
            ),
        },
        "extraction_packet_text": extraction_text,
        "resolution_packet_text": resolution_text,
        "extraction_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "resolution_summary": resolution_summary,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "authorized_stage": False,
    }
    return review, bundle


async def main() -> int:
    args = arguments()
    root = _secure_root(args.review_root)
    report_path = _output_path(args.review_report, root)
    bundle_path = _output_path(args.stage_bundle, root)
    review, bundle = await build(args)
    report_sha = _secure_write(report_path, review)
    try:
        bundle["source_report"] = {"path": str(report_path), "sha256": report_sha}
        bundle_sha = _secure_write(bundle_path, bundle)
    except BaseException:
        report_path.unlink(missing_ok=True)
        raise
    print(
        stable_json(
            {
                "contract_version": REVIEW_CONTRACT,
                "predicate_contract_profile": "v5_2",
                "review_report": str(report_path),
                "review_report_sha256": report_sha,
                "stage_bundle": str(bundle_path),
                "stage_bundle_sha256": bundle_sha,
                "owner_user_id": review["owner_user_id"],
                "packet_id": review["packet_id"],
                "resolution_summary": review["resolution_summary"],
                "blocking_codes": review["blocking_codes"],
                "review_disposition": review["review_disposition"],
                "database_writes": 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
