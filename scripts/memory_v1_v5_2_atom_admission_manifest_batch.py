#!/usr/bin/env python3
"""Build a bounded owner-scoped V5.2 atom-admission manifest from a spec."""

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


SPEC_CONTRACT = "memory_v1_v5_2_atom_admission_spec_v1"
MANIFEST_CONTRACT = "memory_v1_v5_2_atom_admission_apply_manifest_v2"
POLICY = "memory_v1_v5_2_atom_admission_policy_v2"
NAMESPACE = uuid.UUID("1d5d69fc-cbec-53d2-97cc-c0f553977e2e")
MAX_ITEMS = 16
COUNT_KEYS = {
    "source_atom_count",
    "admitted_entity_mention_count",
    "admitted_observation_count",
    "admitted_comparison_hint_count",
    "deferred_atom_count",
    "retained_source_only_count",
    "rejected_atom_count",
}


class AtomManifestError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def exact_object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AtomManifestError(f"{field} fields differ from the contract")
    return value


def repository_state() -> tuple[Path, str]:
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
        raise AtomManifestError("atom manifest generation requires a clean worktree")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    return root, head


def repository_input(path_value: str, root: Path) -> tuple[Path, bytes]:
    path = Path(path_value).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(root):
        raise AtomManifestError("atom admission spec must be inside the repository")
    if path.stat().st_mode & 0o022:
        raise AtomManifestError("atom admission spec is writable by a peer")
    return path, path.read_bytes()


def parse_uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise AtomManifestError(f"{field} must be a UUID") from exc


def load_spec(path_value: str, root: Path) -> tuple[dict[str, Any], str]:
    _, raw = repository_input(path_value, root)
    spec = exact_object(
        json.loads(raw),
        {
            "contract_version",
            "target_server",
            "owner_user_id",
            "items",
        },
        "atom admission spec",
    )
    owner = parse_uuid(spec["owner_user_id"], "owner_user_id")
    if (
        spec["contract_version"] != SPEC_CONTRACT
        or spec["target_server"] != "seebx"
        or not isinstance(spec["items"], list)
        or not 1 <= len(spec["items"]) <= MAX_ITEMS
    ):
        raise AtomManifestError("atom admission spec is invalid")
    seen_cases: set[str] = set()
    seen_packets: set[str] = set()
    items: list[dict[str, Any]] = []
    required_keys = {
        "case_id",
        "packet_id",
        "evidence_id",
        "expected_counts",
        "expected_entity_refs",
        "expected_predicates",
        "review_decision",
        "review_reason_codes",
    }
    for index, raw_item in enumerate(spec["items"]):
        item = exact_object(
            raw_item, required_keys, f"atom admission spec item {index}"
        )
        case_id = item["case_id"]
        packet_id = parse_uuid(item["packet_id"], f"items[{index}].packet_id")
        evidence_id = parse_uuid(item["evidence_id"], f"items[{index}].evidence_id")
        counts = item["expected_counts"]
        refs = item["expected_entity_refs"]
        predicates = item["expected_predicates"]
        reasons = item["review_reason_codes"]
        if (
            not isinstance(case_id, str)
            or not case_id
            or len(case_id) > 100
            or case_id in seen_cases
            or packet_id in seen_packets
            or not isinstance(counts, dict)
            or set(counts) != COUNT_KEYS
            or any(
                not isinstance(count, int) or isinstance(count, bool) or count < 0
                for count in counts.values()
            )
            or not isinstance(refs, list)
            or not refs
            or len(set(refs)) != len(refs)
            or any(not isinstance(ref, str) or not ref for ref in refs)
            or not isinstance(predicates, list)
            or not predicates
            or any(not isinstance(predicate, str) or not predicate for predicate in predicates)
            or item["review_decision"] not in {"authorized", "deferred"}
            or not isinstance(reasons, list)
            or not reasons
            or len(set(reasons)) != len(reasons)
            or any(not isinstance(reason, str) or not reason for reason in reasons)
        ):
            raise AtomManifestError(f"atom admission spec item {index} is invalid")
        if (
            item["review_decision"] == "authorized"
            and counts["admitted_observation_count"] < 1
        ):
            raise AtomManifestError("authorized atom admission has no observation")
        seen_cases.add(case_id)
        seen_packets.add(packet_id)
        items.append(
            {
                **item,
                "packet_id": packet_id,
                "evidence_id": evidence_id,
            }
        )
    return {"owner_user_id": owner, "items": items}, sha256_bytes(raw)


def decode_jsonb(value: Any, field: str) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise AtomManifestError(f"{field} did not return an object")
    return decoded


def validate_plan(expected: dict[str, Any], plan: dict[str, Any]) -> None:
    if (
        plan.get("source_packet_id") != expected["packet_id"]
        or plan.get("source_evidence_id") != expected["evidence_id"]
        or plan.get("counts") != expected["expected_counts"]
    ):
        raise AtomManifestError(f"{expected['case_id']}: atom plan binding drifted")
    projection = plan.get("stage_projection")
    if not isinstance(projection, dict):
        raise AtomManifestError(f"{expected['case_id']}: stage projection is absent")
    if projection.get("deferrals") != [] or projection.get("packet_findings") != []:
        raise AtomManifestError(
            f"{expected['case_id']}: stage projection retained deferred material"
        )
    refs = sorted(
        mention.get("entity_ref")
        for mention in projection.get("entity_mentions", [])
        if isinstance(mention, dict)
    )
    predicates = sorted(
        observation.get("predicate")
        for observation in projection.get("observations", [])
        if isinstance(observation, dict)
    )
    if refs != sorted(expected["expected_entity_refs"]):
        raise AtomManifestError(f"{expected['case_id']}: entity refs drifted")
    if predicates != sorted(expected["expected_predicates"]):
        raise AtomManifestError(f"{expected['case_id']}: predicates drifted")
    for field in ("source_packet_storage_sha256", "proposal_sha256"):
        if not sha256_valid(plan.get(field)):
            raise AtomManifestError(f"{expected['case_id']}: {field} is invalid")


def deterministic_id(
    case: dict[str, Any], proposal_sha256: str, role: str
) -> str:
    return str(
        uuid.uuid5(
            NAMESPACE,
            "|".join(
                (
                    POLICY,
                    case["case_id"],
                    case["packet_id"],
                    proposal_sha256,
                    role,
                )
            ),
        )
    )


async def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    root, head = repository_state()
    spec, spec_sha = load_spec(args.spec, root)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise AtomManifestError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=60)
    items: list[dict[str, Any]] = []
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            if await connection.fetchval("SELECT session_user") != "brains_app":
                raise AtomManifestError("manifest builder requires brains_app")
            await connection.execute(
                "SELECT set_config('app.user_id',$1,true)",
                spec["owner_user_id"],
            )
            for case in spec["items"]:
                plan = decode_jsonb(
                    await connection.fetchval(
                        "SELECT memory.plan_owner_v5_2_atom_admission_v2($1::uuid)",
                        uuid.UUID(case["packet_id"]),
                    ),
                    case["case_id"],
                )
                validate_plan(case, plan)
                proposal_sha256 = plan["proposal_sha256"]
                authorized = case["review_decision"] == "authorized"
                items.append(
                    {
                        "case_id": case["case_id"],
                        "packet_id": case["packet_id"],
                        "evidence_id": case["evidence_id"],
                        "packet_storage_sha256": plan[
                            "source_packet_storage_sha256"
                        ],
                        "proposal_sha256": proposal_sha256,
                        "expected_counts": case["expected_counts"],
                        "review_decision": case["review_decision"],
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
                        "review_id": deterministic_id(
                            case, proposal_sha256, "review"
                        ),
                        "apply_operation_id": deterministic_id(
                            case, proposal_sha256, "apply-operation"
                        )
                        if authorized
                        else None,
                        "apply_id": deterministic_id(
                            case, proposal_sha256, "apply"
                        )
                        if authorized
                        else None,
                    }
                )
    finally:
        await connection.close()
    authorized_count = sum(
        1 for item in items if item["review_decision"] == "authorized"
    )
    manifest = {
        "contract_version": MANIFEST_CONTRACT,
        "owner_user_id_sha256": sha256_text(spec["owner_user_id"]),
        "policy_version": POLICY,
        "required_base_commit": head,
        "items": items,
        "expected_new_rows": {
            "v5_2_atom_admission_proposal": len(items),
            "v5_2_atom_admission_review": len(items),
            "v5_2_atom_admission_apply": authorized_count,
            "v5_2_atom_admission_operation": (len(items) * 2) + authorized_count,
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
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "contract_version": "memory_v1_v5_2_atom_admission_manifest_report_v1",
        "spec_sha256": spec_sha,
        "manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(items),
        "authorized_count": authorized_count,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    result = await build_manifest(parser.parse_args())
    print(stable_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
