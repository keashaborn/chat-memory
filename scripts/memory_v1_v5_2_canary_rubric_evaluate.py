#!/usr/bin/env python3
"""Evaluate one immutable V5.2 canary packet without exposing source prose."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_relational_extraction_v5_provider import (
    NormalizedPacket,
    canonical_sha256,
)


PACKET_NAMESPACE = uuid.UUID("a1ab4c90-2b6a-4a4b-a8e9-746c03917721")
SENSITIVITY_RANK = {"low": 0, "medium": 1, "high": 2, "restricted": 3}
EVALUATION_CONTRACT = "memory_v1_v5_2_canary_rubric_evaluation_v1"


class RubricEvaluationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read and evaluate one owner-scoped V5.2 canary packet."
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--rubric", required=True)
    return parser.parse_args()


def _load_rubric(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RubricEvaluationError("rubric_unreadable") from exc
    if not isinstance(value, dict):
        raise RubricEvaluationError("rubric_not_object")
    if value.get("contract_version") != "memory_v1_v5_2_canary_rubric_v1":
        raise RubricEvaluationError("rubric_contract_mismatch")
    if value.get("source_binding", {}).get("source_prose_included") is not False:
        raise RubricEvaluationError("rubric_contains_source_prose")
    return value


def _uuid(value: Any, code: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RubricEvaluationError(code) from exc


def _failures(
    *,
    rubric: dict[str, Any],
    owner: uuid.UUID,
    row: dict[str, Any],
    packet: dict[str, Any],
) -> tuple[list[str], dict[str, int], dict[str, int]]:
    failures: list[str] = []
    binding = rubric["source_binding"]
    expected = rubric["expected_packet"]
    boundary = rubric["write_boundary"]

    owner_hash = hashlib.sha256(str(owner).encode("utf-8")).hexdigest()
    if owner_hash != binding["owner_user_id_sha256"]:
        failures.append("owner_binding_mismatch")
    if str(row["job_id"]) != binding["job_id"]:
        failures.append("job_binding_mismatch")
    if str(row["evidence_id"]) != binding["evidence_id"]:
        failures.append("evidence_binding_mismatch")
    if row["evidence_content_sha256"] != binding["evidence_content_sha256"]:
        failures.append("evidence_hash_mismatch")
    if row["evidence_authority_sha256"] != binding["evidence_content_sha256"]:
        failures.append("evidence_authority_hash_mismatch")
    if row["evidence_status"] != "active":
        failures.append("evidence_not_active")
    if not row["storage_integrity_verified"]:
        failures.append("packet_storage_integrity_failed")
    if row["local_model_calls"] > boundary["local_model_calls_maximum"]:
        failures.append("local_model_call_limit_exceeded")
    if row["external_model_calls"] != boundary["external_model_calls"]:
        failures.append("external_model_call_boundary_failed")
    if row["exact_stage_batch_count"] or row["evidence_stage_batch_count"]:
        failures.append("staging_boundary_failed")

    if packet.get("contract_version") != expected["contract_version"]:
        failures.append("packet_contract_mismatch")
    if packet.get("predicate_registry_version") != expected[
        "predicate_registry_version"
    ]:
        failures.append("predicate_registry_mismatch")
    source = packet.get("source_envelope", {})
    if source.get("job_id") != binding["job_id"]:
        failures.append("packet_job_binding_mismatch")
    if source.get("source_sha256") != binding["evidence_content_sha256"]:
        failures.append("packet_source_hash_mismatch")

    mentions = packet.get("entity_mentions", [])
    self_count = sum(item.get("mention_kind") == "self_reference" for item in mentions)
    other_count = len(mentions) - self_count
    if self_count != expected["entity_counts"]["self"]:
        failures.append("self_entity_count_failed")
    if other_count != expected["entity_counts"]["other"]:
        failures.append("other_entity_count_failed")

    observations = packet.get("observations", [])
    predicate_counts = Counter(item.get("predicate", "missing") for item in observations)
    allowed_predicates = set(expected["allowed_predicates"])
    if set(predicate_counts) - allowed_predicates:
        failures.append("unexpected_predicate")
    if set(predicate_counts) & set(expected["forbidden_predicates"]):
        failures.append("forbidden_predicate")
    for predicate, limits in expected["predicate_counts"].items():
        count = predicate_counts[predicate]
        if count < limits["minimum"] or count > limits["maximum"]:
            failures.append(f"predicate_count_failed:{predicate}")

    stance_contract = expected["required_stance_contract"]
    minimum_rank = SENSITIVITY_RANK[stance_contract["minimum_sensitivity"]]
    required_fields = set(stance_contract["required_object_fields"])
    for observation in observations:
        if observation.get("predicate") != "stance.reported":
            continue
        if observation.get("modality") != stance_contract["modality"]:
            failures.append("stance_modality_failed")
        if observation.get("projection_class") != stance_contract["projection_class"]:
            failures.append("stance_projection_failed")
        if observation.get("surface_policy") != stance_contract["surface_policy"]:
            failures.append("stance_surface_policy_failed")
        if SENSITIVITY_RANK.get(observation.get("sensitivity"), -1) < minimum_rank:
            failures.append("stance_sensitivity_failed")
        object_value = observation.get("object", {})
        if object_value.get("kind") != "literal":
            failures.append("stance_object_kind_failed")
        if object_value.get("datatype") != stance_contract["object_datatype"]:
            failures.append("stance_object_datatype_failed")
        value = object_value.get("value")
        if not isinstance(value, dict) or not required_fields <= set(value):
            failures.append("stance_object_fields_failed")

    deferrals = packet.get("deferrals", [])
    deferral_counts = Counter(item.get("reason_code", "missing") for item in deferrals)
    if set(deferral_counts) & set(expected["forbidden_deferrals"]):
        failures.append("forbidden_deferral")
    if set(deferral_counts) - set(expected["allowed_deferrals"]):
        failures.append("unexpected_deferral")

    actual_counts = (
        len(mentions),
        len(observations),
        len(packet.get("comparison_hints", [])),
        len(deferrals),
    )
    stored_counts = (
        row["entity_mention_count"],
        row["observation_count"],
        row["comparison_hint_count"],
        row["deferral_count"],
    )
    if actual_counts != stored_counts:
        failures.append("stored_counts_mismatch")

    return sorted(set(failures)), dict(sorted(predicate_counts.items())), dict(
        sorted(deferral_counts.items())
    )


async def _evaluate(args: argparse.Namespace) -> dict[str, Any]:
    rubric = _load_rubric(args.rubric)
    owner = _uuid(args.owner_user_id, "owner_id_invalid")
    binding = rubric["source_binding"]
    job_id = _uuid(binding.get("job_id"), "rubric_job_id_invalid")
    packet_id = uuid.uuid5(PACKET_NAMESPACE, f"packet:{job_id}")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RubricEvaluationError("postgres_dsn_missing")

    try:
        conn = await asyncpg.connect(dsn, command_timeout=30)
    except Exception as exc:
        raise RubricEvaluationError("database_connection_failed") from exc
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RubricEvaluationError("database_role_failed")
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            rows = await conn.fetch(
                """
                SELECT packet_id,job_id,evidence_id,evidence_content_sha256,
                       validator_packet_sha256,normalized_packet,
                       local_model_calls,external_model_calls,
                       entity_mention_count,observation_count,
                       comparison_hint_count,deferral_count,
                       evidence_authority_sha256,evidence_status,
                       storage_integrity_verified,exact_stage_batch_count,
                       evidence_stage_batch_count
                FROM memory.read_owner_v5_local_packet_review_v1($1)
                """,
                packet_id,
            )
            if len(rows) != 1:
                raise RubricEvaluationError("owner_scoped_packet_not_found")
            row = dict(rows[0])
            packet = row["normalized_packet"]
            if isinstance(packet, str):
                packet = json.loads(packet)
            validated = NormalizedPacket.model_validate(packet).model_dump(mode="json")
            if canonical_sha256(validated) != row["validator_packet_sha256"]:
                raise RubricEvaluationError("validator_packet_hash_mismatch")
            txid = await conn.fetchval("SELECT txid_current_if_assigned()")
            if txid is not None:
                raise RubricEvaluationError("read_only_transaction_wrote")
    except RubricEvaluationError:
        raise
    except Exception as exc:
        raise RubricEvaluationError("owner_scoped_packet_read_failed") from exc
    finally:
        await conn.close()

    failures, predicates, deferrals = _failures(
        rubric=rubric,
        owner=owner,
        row=row,
        packet=validated,
    )
    return {
        "contract_version": EVALUATION_CONTRACT,
        "case_id": rubric["case_id"],
        "passed": not failures,
        "rejection_codes": failures,
        "bindings": {
            "owner_user_id_sha256": binding["owner_user_id_sha256"],
            "job_id_sha256": hashlib.sha256(str(job_id).encode()).hexdigest(),
            "evidence_id_sha256": hashlib.sha256(
                binding["evidence_id"].encode()
            ).hexdigest(),
            "packet_id_sha256": hashlib.sha256(str(packet_id).encode()).hexdigest(),
            "packet_sha256": row["validator_packet_sha256"],
        },
        "sanitized_counts": {
            "entities": row["entity_mention_count"],
            "observations": row["observation_count"],
            "comparison_hints": row["comparison_hint_count"],
            "deferrals": row["deferral_count"],
            "predicates": predicates,
            "deferral_reasons": deferrals,
        },
        "boundaries": {
            "read_only": True,
            "source_prose_emitted": False,
            "external_model_calls": row["external_model_calls"],
            "stage_batches": row["exact_stage_batch_count"],
            "qdrant_writes": 0,
            "prompt_influence": 0,
        },
    }


def main() -> int:
    args = arguments()
    try:
        report = asyncio.run(_evaluate(args))
    except RubricEvaluationError as exc:
        report = {
            "contract_version": EVALUATION_CONTRACT,
            "passed": False,
            "rejection_codes": [exc.code],
            "source_prose_emitted": False,
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
