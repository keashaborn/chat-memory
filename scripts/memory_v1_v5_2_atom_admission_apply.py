#!/usr/bin/env python3
"""Apply or replay a bounded, reviewed V5.2 atom-admission manifest."""

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


CONTRACT = "memory_v1_v5_2_atom_admission_apply_manifest_v1"
RESULT_CONTRACT = "memory_v1_v5_2_atom_admission_apply_result_v1"
POLICY = "memory_v1_v5_2_atom_admission_policy_v1"
OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
COUNT_KEYS = {
    "source_atom_count",
    "admitted_entity_mention_count",
    "admitted_observation_count",
    "admitted_comparison_hint_count",
    "deferred_atom_count",
    "retained_source_only_count",
    "rejected_atom_count",
}
ITEM_KEYS = {
    "case_id",
    "packet_id",
    "evidence_id",
    "packet_storage_sha256",
    "proposal_sha256",
    "expected_counts",
    "review_decision",
    "review_reason_codes",
    "proposal_operation_id",
    "proposal_id",
    "review_operation_id",
    "review_id",
    "apply_operation_id",
    "apply_id",
}
MANIFEST_KEYS = {
    "contract_version",
    "owner_user_id_sha256",
    "policy_version",
    "required_base_commit",
    "items",
    "expected_new_rows",
    "manifest_sha256",
}


class AdmissionError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AdmissionError(f"{field} is not a UUID") from exc


def repository_state(required_base: str) -> str:
    root = Path(__file__).resolve().parents[1]
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise AdmissionError("atom admission requires a clean Git worktree")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", required_base, head],
        check=True,
        capture_output=True,
        text=True,
    )
    return head


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if set(value) != MANIFEST_KEYS or value.get("contract_version") != CONTRACT:
        raise AdmissionError("atom-admission manifest envelope is invalid")
    expected_sha = sha256_text(
        stable_json({key: item for key, item in value.items() if key != "manifest_sha256"})
    )
    if value.get("manifest_sha256") != expected_sha:
        raise AdmissionError("atom-admission manifest hash mismatch")
    if value.get("owner_user_id_sha256") != sha256_text(str(OWNER)):
        raise AdmissionError("atom-admission manifest owner mismatch")
    if value.get("policy_version") != POLICY:
        raise AdmissionError("atom-admission policy mismatch")
    required_base = value.get("required_base_commit")
    if (
        not isinstance(required_base, str)
        or len(required_base) != 40
        or any(character not in "0123456789abcdef" for character in required_base)
    ):
        raise AdmissionError("required base commit is invalid")
    if not isinstance(value.get("items"), list) or len(value["items"]) != 2:
        raise AdmissionError("atom-admission manifest must contain exactly two items")
    seen: set[str] = set()
    authorized = 0
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != ITEM_KEYS:
            raise AdmissionError("atom-admission item shape is invalid")
        case_id = item.get("case_id")
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in seen
            or len(case_id) > 100
        ):
            raise AdmissionError("atom-admission case_id is invalid")
        seen.add(case_id)
        for field in (
            "packet_id",
            "evidence_id",
            "proposal_operation_id",
            "proposal_id",
            "review_operation_id",
            "review_id",
        ):
            parse_uuid(item.get(field), f"{case_id}.{field}")
        for field in ("packet_storage_sha256", "proposal_sha256"):
            if not sha256_valid(item.get(field)):
                raise AdmissionError(f"{case_id}.{field} is invalid")
        if (
            not isinstance(item.get("expected_counts"), dict)
            or set(item["expected_counts"]) != COUNT_KEYS
            or any(
                not isinstance(count, int) or count < 0
                for count in item["expected_counts"].values()
            )
        ):
            raise AdmissionError(f"{case_id}.expected_counts is invalid")
        decision = item.get("review_decision")
        if decision not in {"authorized", "deferred"}:
            raise AdmissionError(f"{case_id}.review_decision is invalid")
        reasons = item.get("review_reason_codes")
        if (
            not isinstance(reasons, list)
            or not 1 <= len(reasons) <= 32
            or any(not isinstance(reason, str) or not reason for reason in reasons)
            or len(set(reasons)) != len(reasons)
        ):
            raise AdmissionError(f"{case_id}.review_reason_codes is invalid")
        if decision == "authorized":
            authorized += 1
            parse_uuid(item.get("apply_operation_id"), f"{case_id}.apply_operation_id")
            parse_uuid(item.get("apply_id"), f"{case_id}.apply_id")
            if item["expected_counts"]["admitted_observation_count"] < 1:
                raise AdmissionError("authorized atom plan has no observations")
        elif item.get("apply_operation_id") is not None or item.get("apply_id") is not None:
            raise AdmissionError("deferred atom plan contains apply identifiers")
    if authorized != 1:
        raise AdmissionError("manifest must authorize exactly one item")
    expected_rows = {
        "v5_2_atom_admission_proposal": 2,
        "v5_2_atom_admission_review": 2,
        "v5_2_atom_admission_apply": 1,
        "v5_2_atom_admission_operation": 5,
        "relational_stage_batch": 0,
        "entity_mention": 0,
        "observation": 0,
        "claim": 0,
        "qdrant": 0,
    }
    if value.get("expected_new_rows") != expected_rows:
        raise AdmissionError("atom-admission row budget is invalid")
    return value


def decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
    else:
        decoded = value
    if not isinstance(decoded, dict):
        raise AdmissionError("database returned a non-object atom plan")
    return decoded


async def run_item(
    connection: asyncpg.Connection,
    item: dict[str, Any],
    expected_first_outcome: str,
) -> dict[str, Any]:
    packet_id = parse_uuid(item["packet_id"], "packet_id")
    plan = decode_json(
        await connection.fetchval(
            "SELECT memory.plan_owner_v5_2_atom_admission_v1($1)", packet_id
        )
    )
    if plan.get("source_packet_id") != item["packet_id"]:
        raise AdmissionError("atom plan packet binding mismatch")
    if plan.get("source_evidence_id") != item["evidence_id"]:
        raise AdmissionError("atom plan evidence binding mismatch")
    if plan.get("source_packet_storage_sha256") != item["packet_storage_sha256"]:
        raise AdmissionError("atom plan source hash mismatch")
    if plan.get("proposal_sha256") != item["proposal_sha256"]:
        raise AdmissionError("atom plan hash differs from reviewed manifest")
    if plan.get("counts") != item["expected_counts"]:
        raise AdmissionError("atom plan counts differ from reviewed manifest")

    proposal = await connection.fetchrow(
        """
        SELECT * FROM memory.record_owner_v5_2_atom_proposal_v1(
          $1,$2,$3,$4,$5
        )
        """,
        parse_uuid(item["proposal_operation_id"], "proposal_operation_id"),
        parse_uuid(item["proposal_id"], "proposal_id"),
        packet_id,
        item["packet_storage_sha256"],
        item["proposal_sha256"],
    )
    if proposal["outcome"] != expected_first_outcome:
        raise AdmissionError("unexpected proposal persistence outcome")
    if proposal["admitted_observations"] != item["expected_counts"][
        "admitted_observation_count"
    ]:
        raise AdmissionError("proposal persistence count mismatch")

    reasons = stable_json(item["review_reason_codes"])
    review_preflight = await connection.fetchrow(
        """
        SELECT * FROM memory.preflight_owner_v5_2_atom_review_v1(
          $1,$2::memory.v5_2_atom_review_decision,$3,$4,$5::jsonb
        )
        """,
        parse_uuid(item["proposal_id"], "proposal_id"),
        item["review_decision"],
        "system",
        POLICY,
        reasons,
    )
    review = await connection.fetchrow(
        """
        SELECT * FROM memory.review_owner_v5_2_atom_proposal_v1(
          $1,$2,$3,$4::memory.v5_2_atom_review_decision,$5,$6,$7::jsonb,$8
        )
        """,
        parse_uuid(item["review_operation_id"], "review_operation_id"),
        parse_uuid(item["review_id"], "review_id"),
        parse_uuid(item["proposal_id"], "proposal_id"),
        item["review_decision"],
        "system",
        POLICY,
        reasons,
        review_preflight["authorization_manifest_sha256"],
    )
    if review["outcome"] != expected_first_outcome:
        raise AdmissionError("unexpected review persistence outcome")
    if str(review["decision"]) != item["review_decision"]:
        raise AdmissionError("review decision mismatch")

    result: dict[str, Any] = {
        "case_id": item["case_id"],
        "packet_id": item["packet_id"],
        "proposal_id": str(proposal["proposal_id"]),
        "proposal_outcome": proposal["outcome"],
        "review_id": str(review["review_id"]),
        "review_outcome": review["outcome"],
        "review_decision": str(review["decision"]),
        "apply_id": None,
        "apply_outcome": None,
        "admitted_observation_count": proposal["admitted_observations"],
        "deferred_atom_count": proposal["deferred_atoms"],
    }
    if item["review_decision"] == "authorized":
        apply_preflight = await connection.fetchrow(
            "SELECT * FROM memory.preflight_owner_v5_2_atom_apply_v1($1)",
            parse_uuid(item["review_id"], "review_id"),
        )
        applied = await connection.fetchrow(
            """
            SELECT * FROM memory.apply_owner_v5_2_atom_review_v1($1,$2,$3,$4)
            """,
            parse_uuid(item["apply_operation_id"], "apply_operation_id"),
            parse_uuid(item["apply_id"], "apply_id"),
            parse_uuid(item["review_id"], "review_id"),
            apply_preflight["apply_manifest_sha256"],
        )
        if applied["outcome"] != expected_first_outcome:
            raise AdmissionError("unexpected apply persistence outcome")
        result["apply_id"] = str(applied["apply_id"])
        result["apply_outcome"] = applied["outcome"]
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("preflight", "apply", "replay"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    manifest = load_manifest(args.manifest.resolve(strict=True))
    head = repository_state(manifest["required_base_commit"])
    if args.mode == "apply" and os.getenv(
        "MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY"
    ) != "authorized":
        raise AdmissionError("durable atom-admission sentinel is absent")

    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise AdmissionError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=60)
    transaction = connection.transaction(isolation="serializable")
    results: list[dict[str, Any]] = []
    replay_results: list[dict[str, Any]] = []
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise AdmissionError("atom-admission runner requires brains_app")
        await transaction.start()
        await connection.execute(
            "SELECT set_config('app.user_id',$1,true)", str(OWNER)
        )
        first_outcome = "replayed" if args.mode == "replay" else "applied"
        for item in manifest["items"]:
            results.append(await run_item(connection, item, first_outcome))
        if args.mode != "replay":
            for item in manifest["items"]:
                replay_results.append(await run_item(connection, item, "replayed"))
        if args.mode == "preflight":
            await transaction.rollback()
        else:
            await transaction.commit()
    except BaseException:
        if connection.is_in_transaction():
            await transaction.rollback()
        raise
    finally:
        await connection.close()

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "head": head,
        "owner_user_id_sha256": manifest["owner_user_id_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "results": results,
        "same_transaction_replay": replay_results,
        "persistent_writes": 0
        if args.mode in {"preflight", "replay"}
        else sum(manifest["expected_new_rows"].values()),
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    print(
        stable_json(
            {
                "mode": args.mode,
                "manifest_sha256": manifest["manifest_sha256"],
                "items": len(results),
                "persistent_writes": payload["persistent_writes"],
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
