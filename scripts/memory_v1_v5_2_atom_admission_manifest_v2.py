#!/usr/bin/env python3
"""Build the exact two-packet owner-reviewed V5.2 atom-admission manifest."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any
import uuid

import asyncpg


CONTRACT = "memory_v1_v5_2_atom_admission_apply_manifest_v2"
POLICY = "memory_v1_v5_2_atom_admission_policy_v2"
OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
NAMESPACE = uuid.UUID("1d5d69fc-cbec-53d2-97cc-c0f553977e2e")
CASES = (
    {
        "case_id": "compiler_v8_former_profession",
        "packet_id": uuid.UUID("6ae4a8e6-b207-5201-a997-53fb2363fc9d"),
        "evidence_id": uuid.UUID("dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5"),
        "expected_counts": {
            "source_atom_count": 6,
            "admitted_entity_mention_count": 3,
            "admitted_observation_count": 2,
            "admitted_comparison_hint_count": 0,
            "deferred_atom_count": 1,
            "retained_source_only_count": 0,
            "rejected_atom_count": 0,
        },
        "predicates": ["occupation.works_as", "occupation.works_as"],
        "review_reason_codes": [
            "explicit_former_occupation_atoms_reviewed",
            "historical_upper_bound_preserved",
            "structured_domain_retained_as_source_deferral",
        ],
    },
    {
        "case_id": "compiler_v8_caregiving_relationships",
        "packet_id": uuid.UUID("b76915b8-0603-50e8-b263-761da39f5651"),
        "evidence_id": uuid.UUID("fea59e7e-30f5-4139-b634-97b291c88e14"),
        "expected_counts": {
            "source_atom_count": 6,
            "admitted_entity_mention_count": 2,
            "admitted_observation_count": 2,
            "admitted_comparison_hint_count": 0,
            "deferred_atom_count": 2,
            "retained_source_only_count": 0,
            "rejected_atom_count": 0,
        },
        "predicates": [
            "relationship.caregiver_for",
            "relationship.spouse_of",
        ],
        "review_reason_codes": [
            "explicit_caregiving_and_spouse_atoms_reviewed",
            "sensitive_direct_claim_owner_authorized",
            "compound_supportive_context_retained_as_source_deferral",
        ],
    },
)


class ManifestError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deterministic_id(case: dict[str, Any], proposal_sha256: str, role: str) -> str:
    return str(
        uuid.uuid5(
            NAMESPACE,
            "|".join(
                (
                    POLICY,
                    case["case_id"],
                    str(case["packet_id"]),
                    proposal_sha256,
                    role,
                )
            ),
        )
    )


def repository_head() -> str:
    root = Path(__file__).resolve().parents[1]
    git_env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout
    if status.strip():
        raise ManifestError("manifest generation requires a clean Git worktree")
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout.strip()


def decode_json(value: Any) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ManifestError("database returned a non-object atom plan")
    return decoded


def validate_plan(case: dict[str, Any], plan: dict[str, Any]) -> None:
    if plan.get("source_packet_id") != str(case["packet_id"]):
        raise ManifestError(f"{case['case_id']}: packet binding mismatch")
    if plan.get("source_evidence_id") != str(case["evidence_id"]):
        raise ManifestError(f"{case['case_id']}: evidence binding mismatch")
    if plan.get("counts") != case["expected_counts"]:
        raise ManifestError(f"{case['case_id']}: exact count contract mismatch")
    projection = plan.get("stage_projection")
    if not isinstance(projection, dict):
        raise ManifestError(f"{case['case_id']}: stage projection is absent")
    predicates = sorted(
        observation.get("predicate")
        for observation in projection.get("observations", [])
        if isinstance(observation, dict)
    )
    if predicates != sorted(case["predicates"]):
        raise ManifestError(f"{case['case_id']}: predicate contract mismatch")
    if projection.get("deferrals") != [] or projection.get("packet_findings") != []:
        raise ManifestError(f"{case['case_id']}: projection retained source-only data")
    for field in ("source_packet_storage_sha256", "proposal_sha256"):
        value = plan.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ManifestError(f"{case['case_id']}: {field} is invalid")


async def build_manifest() -> dict[str, Any]:
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ManifestError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=60)
    transaction = connection.transaction(isolation="repeatable_read", readonly=True)
    items: list[dict[str, Any]] = []
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise ManifestError("manifest builder requires brains_app")
        await transaction.start()
        await connection.execute(
            "SELECT set_config('app.user_id',$1,true)", str(OWNER)
        )
        for case in CASES:
            plan = decode_json(
                await connection.fetchval(
                    "SELECT memory.plan_owner_v5_2_atom_admission_v2($1)",
                    case["packet_id"],
                )
            )
            validate_plan(case, plan)
            proposal_sha256 = plan["proposal_sha256"]
            items.append(
                {
                    "case_id": case["case_id"],
                    "packet_id": str(case["packet_id"]),
                    "evidence_id": str(case["evidence_id"]),
                    "packet_storage_sha256": plan[
                        "source_packet_storage_sha256"
                    ],
                    "proposal_sha256": proposal_sha256,
                    "expected_counts": case["expected_counts"],
                    "review_decision": "authorized",
                    "review_reason_codes": case["review_reason_codes"],
                    "proposal_operation_id": deterministic_id(
                        case, proposal_sha256, "proposal-operation"
                    ),
                    "proposal_id": deterministic_id(
                        case, proposal_sha256, "proposal"
                    ),
                    "review_operation_id": deterministic_id(
                        case, proposal_sha256, "review-operation"
                    ),
                    "review_id": deterministic_id(case, proposal_sha256, "review"),
                    "apply_operation_id": deterministic_id(
                        case, proposal_sha256, "apply-operation"
                    ),
                    "apply_id": deterministic_id(case, proposal_sha256, "apply"),
                }
            )
        await transaction.rollback()
    finally:
        await connection.close()

    manifest: dict[str, Any] = {
        "contract_version": CONTRACT,
        "owner_user_id_sha256": sha256_text(str(OWNER)),
        "policy_version": POLICY,
        "required_base_commit": repository_head(),
        "items": items,
        "expected_new_rows": {
            "v5_2_atom_admission_proposal": 2,
            "v5_2_atom_admission_review": 2,
            "v5_2_atom_admission_apply": 2,
            "v5_2_atom_admission_operation": 6,
            "relational_stage_batch": 0,
            "entity_mention": 0,
            "observation": 0,
            "claim": 0,
            "qdrant": 0,
        },
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = sha256_text(
        stable_json(
            {
                key: value
                for key, value in manifest.items()
                if key != "manifest_sha256"
            }
        )
    )
    return manifest


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = await build_manifest()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    print(
        stable_json(
            {
                "items": len(manifest["items"]),
                "manifest_sha256": manifest["manifest_sha256"],
                "owner_user_id_sha256": manifest["owner_user_id_sha256"],
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
