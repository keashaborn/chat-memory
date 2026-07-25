#!/usr/bin/env python3
"""Build the two exact compiler-v8 V5.2 atom-stage manifests without writes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any
import uuid

import asyncpg


CONTRACT = "memory_v1_v5_2_atom_stage_build_manifest_v2"
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
SELF_ENTITY = "35029129-27bd-457b-8cb5-82dd37ba32ba"
CASES: dict[str, dict[str, Any]] = {
    "compiler_v8_caregiving": {
        "apply_id": "190f0b21-6e54-5c1d-8a99-6b08358d4846",
        "proposal_id": "9a6f2ebb-4a20-5a11-a2d2-c14b0ac471c5",
        "review_id": "5ac4b908-35da-5891-8fbc-8cdddd5b672c",
        "packet_id": "b76915b8-0603-50e8-b263-761da39f5651",
        "evidence_id": "fea59e7e-30f5-4139-b634-97b291c88e14",
        "apply_manifest_sha256":
            "ee031ede24df9fa56472db622e9b6fc6f663dddc2caee24922ca613f23e79132",
        "proposal_sha256":
            "4f1a0651034e64a83e6e6f47c6f6c03cd3b4d6f148e9aca5b84e4986f90939ea",
        "stage_projection_sha256":
            "d5505566a10db55825269bffbdec65275265cd3a70912f8a5ba7fb747856d67c",
        "expected_counts": {
            "entity_mentions": 2,
            "observations": 2,
            "comparison_hints": 0,
            "deferrals": 0,
        },
        "expected_resolutions": {
            "e00": {
                "action": "link_existing",
                "decision_state": "auto_link_eligible",
                "selected_entity_id": SELF_ENTITY,
                "proposed_entity": None,
                "review_reason_codes": ["trusted_owner_self_binding"],
            },
            "e01": {
                "action": "create_new",
                "decision_state": "manual_review_required",
                "selected_entity_id": None,
                "proposed_entity": {
                    "entity_type": "person",
                    "identity_state": "named",
                    "canonical_name": "Monika",
                    "display_label": "Monika",
                    "creation_reason": "new_named_entity_no_exact_owner_match",
                },
                "review_reason_codes": ["new_named_entity_requires_review"],
            },
        },
    },
    "compiler_v8_former_profession": {
        "apply_id": "f87ae2b4-57ee-5969-8ce1-bc80a6bfec83",
        "proposal_id": "d45902c5-e52d-59b5-9c80-738709911d57",
        "review_id": "4832d9d6-318b-5dcf-b623-4e23e2659e89",
        "packet_id": "6ae4a8e6-b207-5201-a997-53fb2363fc9d",
        "evidence_id": "dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5",
        "apply_manifest_sha256":
            "76ad266fc616406966b3212b085abacb126152ff63fc4de1e8cf499e7eee23e3",
        "proposal_sha256":
            "3bba1fd4bf3baf1fdce286709565378625392d566ec4eb5005d1071df2c9f4a5",
        "stage_projection_sha256":
            "d998dee37fa278f18838015c687ca4a440764184e8f060b69b868329e042ae9f",
        "expected_counts": {
            "entity_mentions": 3,
            "observations": 2,
            "comparison_hints": 0,
            "deferrals": 0,
        },
        "expected_resolutions": {
            "e00": {
                "action": "link_existing",
                "decision_state": "auto_link_eligible",
                "selected_entity_id": SELF_ENTITY,
                "proposed_entity": None,
                "review_reason_codes": ["trusted_owner_self_binding"],
            },
            "e01": {
                "action": "create_new",
                "decision_state": "manual_review_required",
                "selected_entity_id": None,
                "proposed_entity": {
                    "entity_type": "concept",
                    "identity_state": "named",
                    "canonical_name": "clinical psychologist",
                    "display_label": "clinical psychologist",
                    "creation_reason": "new_named_entity_no_exact_owner_match",
                },
                "review_reason_codes": ["new_named_entity_requires_review"],
            },
            "e02": {
                "action": "create_new",
                "decision_state": "manual_review_required",
                "selected_entity_id": None,
                "proposed_entity": {
                    "entity_type": "concept",
                    "identity_state": "named",
                    "canonical_name": "BCBA",
                    "display_label": "BCBA",
                    "creation_reason": "new_named_entity_no_exact_owner_match",
                },
                "review_reason_codes": ["new_named_entity_requires_review"],
            },
        },
    },
}


class ManifestError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def repository_head() -> str:
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    if status.strip():
        raise ManifestError("manifest build requires a clean Git worktree")
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def secure_write(path: Path, value: dict[str, Any]) -> str:
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
    return sha256_bytes(payload)


def decode_jsonb(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ManifestError("stage planner did not return an object")
    return value


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    head = repository_head()
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_root, 0o700)
    if stat.S_IMODE(output_root.stat().st_mode) != 0o700:
        raise ManifestError("output root is not mode 0700")
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ManifestError("POSTGRES_DSN is required")
    owner = uuid.UUID(OWNER)
    conn = await asyncpg.connect(dsn, command_timeout=30)
    outputs: list[dict[str, Any]] = []
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            if await conn.fetchval("SELECT session_user") != "brains_app":
                raise ManifestError("POSTGRES_DSN must authenticate as brains_app")
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            for case_id, locked in CASES.items():
                raw = await conn.fetchval(
                    "SELECT memory.plan_owner_v5_2_atom_stage_v2($1::uuid)",
                    uuid.UUID(locked["apply_id"]),
                )
                plan = decode_jsonb(raw)
                expected_plan = {
                    "apply_id": locked["apply_id"],
                    "proposal_id": locked["proposal_id"],
                    "review_id": locked["review_id"],
                    "packet_id": locked["packet_id"],
                    "evidence_id": locked["evidence_id"],
                    "apply_manifest_sha256": locked["apply_manifest_sha256"],
                    "proposal_sha256": locked["proposal_sha256"],
                    "stage_projection_sha256": locked["stage_projection_sha256"],
                    "counts": locked["expected_counts"],
                }
                if (
                    plan.get("contract_version")
                    != "memory_v1_v5_2_atom_stage_plan_v2"
                    or plan.get("policy_version")
                    != "memory_v1_v5_2_atom_stage_policy_v2"
                    or plan.get("owner_user_id") != OWNER
                    or any(plan.get(key) != value for key, value in expected_plan.items())
                ):
                    raise ManifestError(f"locked stage plan drifted: {case_id}")
                projection = plan.get("stage_projection") or {}
                if (
                    {item.get("entity_ref") for item in projection.get("entity_mentions", [])}
                    != set(locked["expected_resolutions"])
                    or projection.get("deferrals") != []
                    or projection.get("packet_findings") != []
                ):
                    raise ManifestError(f"stage projection is no longer eligible: {case_id}")
                manifest = {
                    "contract_version": CONTRACT,
                    "target_server": "seebx",
                    "owner_user_id": str(owner),
                    "case_id": case_id,
                    **locked,
                }
                path = output_root / f"{case_id}.json"
                digest = secure_write(path, manifest)
                outputs.append({"case_id": case_id, "path": str(path), "sha256": digest})
    finally:
        await conn.close()
    print(stable_json({
        "contract_version": "memory_v1_v5_2_compiler_v8_stage_manifest_report_v1",
        "required_head_commit": head,
        "owner_user_id": OWNER,
        "manifests": outputs,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
