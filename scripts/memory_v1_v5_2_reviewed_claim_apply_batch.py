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

from memory_v1_projection_v5_contract_test import sha256
from memory_v1_v5_2_reviewed_claim_apply_manifest import (
    CONTRACT as MANIFEST_CONTRACT,
    CREATE_ROWS,
    REINFORCE_ROWS,
)


RESULT_CONTRACT = "memory_v1_v5_2_reviewed_claim_apply_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")


class ApplyError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply-result")
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ApplyError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ApplyError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ApplyError("input must be a private regular file")
    return path


def load_hashed(path: Path, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get(hash_field) != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise ApplyError(f"{path.name} content hash mismatch")
    return value


def expected_rows(items: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
    rows: dict[str, int] = {}
    create_count = 0
    for item in items:
        action = item["target_action"]
        source = CREATE_ROWS if action == "create" else REINFORCE_ROWS
        if action == "create":
            create_count += 1
        for table, count in source.items():
            rows[table] = rows.get(table, 0) + count
        rows["claim_observation"] += item["observation_count"] - 1
    rows["projection_outbox"] = 0
    return dict(sorted(rows.items())), create_count


def load_manifest(path: Path) -> dict[str, Any]:
    value = load_hashed(path, "manifest_sha256")
    required_keys = {
        "contract_version",
        "owner_user_id",
        "required_head_commit",
        "review_manifest_path",
        "review_manifest_file_sha256",
        "review_manifest_sha256",
        "review_result_path",
        "review_result_file_sha256",
        "review_result_sha256",
        "source_stage_manifest_path",
        "source_stage_manifest_file_sha256",
        "source_stage_manifest_sha256",
        "assessment",
        "action_counts",
        "expected_insert_rows",
        "expected_mutated_rows",
        "expected_table_rows",
        "items",
        "manifest_sha256",
    }
    if set(value) != required_keys or value.get("contract_version") != MANIFEST_CONTRACT:
        raise ApplyError("manifest contract or fields mismatch")
    uuid.UUID(value["owner_user_id"])
    if (
        not isinstance(value.get("items"), list)
        or not 1 <= len(value["items"]) <= 32
    ):
        raise ApplyError("manifest item count is invalid")
    seen: set[str] = set()
    create_count = 0
    reinforce_count = 0
    item_keys = {
        "plan_id",
        "projection_ref",
        "predicate",
        "target_action",
        "current_revision_number",
        "target_claim_id",
        "observation_count",
        "canonical_text_sha256",
        "projection_sha256",
        "semantic_key_sha256",
        "review_id",
        "projection_request_id",
        "projection_apply_manifest_sha256",
        "assessment_review_request_id",
        "assessment_apply_request_id",
    }
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != item_keys:
            raise ApplyError("manifest item fields mismatch")
        plan_id = str(uuid.UUID(item["plan_id"]))
        if plan_id in seen or item["projection_ref"] != "p01":
            raise ApplyError("duplicate or invalid apply identity")
        seen.add(plan_id)
        for field in ("review_id", "projection_request_id"):
            uuid.UUID(item[field])
        for field in (
            "canonical_text_sha256",
            "projection_sha256",
            "semantic_key_sha256",
            "projection_apply_manifest_sha256",
        ):
            if (
                not isinstance(item[field], str)
                or len(item[field]) != 64
                or any(character not in "0123456789abcdef" for character in item[field])
            ):
                raise ApplyError("manifest item hash is invalid")
        if (
            not isinstance(item["observation_count"], int)
            or not 1 <= item["observation_count"] <= 100
        ):
            raise ApplyError("manifest observation count is invalid")
        action = item["target_action"]
        if action == "create":
            create_count += 1
            if (
                item["current_revision_number"] != 0
                or item["target_claim_id"] is not None
                or item["assessment_review_request_id"] is None
                or item["assessment_apply_request_id"] is None
            ):
                raise ApplyError("create item boundary is invalid")
            uuid.UUID(item["assessment_review_request_id"])
            uuid.UUID(item["assessment_apply_request_id"])
        elif action == "reinforce":
            reinforce_count += 1
            if (
                not isinstance(item["current_revision_number"], int)
                or item["current_revision_number"] < 1
                or item["target_claim_id"] is None
                or item["assessment_review_request_id"] is not None
                or item["assessment_apply_request_id"] is not None
            ):
                raise ApplyError("reinforce item boundary is invalid")
            uuid.UUID(item["target_claim_id"])
        else:
            raise ApplyError("unsupported reviewed action")

    table_rows, expected_create_count = expected_rows(value["items"])
    if (
        value["action_counts"]
        != {"create": create_count, "reinforce": reinforce_count}
        or expected_create_count != create_count
        or value["expected_table_rows"] != table_rows
        or value["expected_insert_rows"] != sum(table_rows.values())
        or value["expected_mutated_rows"] != sum(table_rows.values()) + create_count
    ):
        raise ApplyError("manifest row budget mismatch")
    for field in (
        "review_manifest_path",
        "review_result_path",
        "source_stage_manifest_path",
    ):
        source = private_path(value[field])
        if file_sha256(source) != value[field.replace("_path", "_file_sha256")]:
            raise ApplyError("source review artifact file hash mismatch")
    return value


def validate_apply_result(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    value = load_hashed(path, "result_sha256")
    if (
        value.get("contract_version") != RESULT_CONTRACT
        or value.get("mode") != "apply"
        or value.get("manifest_sha256") != manifest["manifest_sha256"]
        or value.get("insert_rows") != manifest["expected_insert_rows"]
        or value.get("mutated_rows") != manifest["expected_mutated_rows"]
        or len(value.get("outcomes", [])) != len(manifest["items"])
    ):
        raise ApplyError("apply result cannot authorize replay")
    return value


async def projection_preflight(connection: Any, item: dict[str, Any]) -> Any:
    state = await connection.fetchrow(
        "SELECT * FROM memory.preflight_projection_apply_v5($1,$2,$3)",
        uuid.UUID(item["plan_id"]),
        "p01",
        uuid.UUID(item["review_id"]),
    )
    if (
        state is None
        or state["lane"] != "claim"
        or state["target_action"] != item["target_action"]
        or state["review_state"] != "manual_review_required"
        or state["current_revision_number"] != item["current_revision_number"]
        or str(state["review_id"]) != item["review_id"]
        or state["apply_manifest_sha256"]
        != item["projection_apply_manifest_sha256"]
    ):
        raise ApplyError("projection apply preflight drifted")
    return state


async def run() -> int:
    import asyncpg

    args = arguments()
    manifest_path = private_path(args.manifest)
    output = private_path(args.output, output=True)
    manifest = load_manifest(manifest_path)
    items = sorted(manifest["items"], key=lambda item: item["plan_id"])
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD", "") != manifest["required_head_commit"]:
        raise ApplyError("runtime head does not match manifest")
    if (
        args.mode == "apply"
        and os.environ.get("MEMORY_V1_V5_2_REVIEWED_CLAIM_APPLY", "")
        != "authorized"
    ):
        raise ApplyError("apply mode requires the bounded authorization gate")

    prior: dict[str, dict[str, Any]] = {}
    prior_path: Path | None = None
    if args.mode == "replay":
        if not args.apply_result:
            raise ApplyError("replay requires the immutable apply result")
        prior_path = private_path(args.apply_result)
        prior_value = validate_apply_result(prior_path, manifest)
        prior = {item["plan_id"]: item for item in prior_value["outcomes"]}
        if prior.keys() != {item["plan_id"] for item in items}:
            raise ApplyError("replay result plan set mismatch")
    elif args.apply_result:
        raise ApplyError("apply result is accepted only in replay mode")

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ApplyError("POSTGRES_DSN is required")
    assessment = manifest["assessment"]
    reason_codes = json.dumps(assessment["reason_codes"], separators=(",", ":"))
    connection = await asyncpg.connect(dsn, command_timeout=90)
    outcomes: list[dict[str, Any]] = []
    insert_rows = 0
    mutated_rows = 0
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise ApplyError("POSTGRES_DSN must authenticate as brains_app")
        transaction = connection.transaction(
            readonly=args.mode == "preflight", isolation="serializable"
        )
        await transaction.start()
        await connection.execute(
            "SELECT set_config('app.user_id',$1,true)", manifest["owner_user_id"]
        )
        if args.mode != "replay":
            for item in items:
                await projection_preflight(connection, item)

        if args.mode in {"apply", "replay"}:
            for item in items:
                target_before = None
                revisions_before = None
                if item["target_action"] == "reinforce":
                    target_before = await connection.fetchval(
                        """SELECT to_jsonb(stored)::text
                           FROM memory.claim AS stored
                           WHERE owner_user_id=$1 AND claim_id=$2
                             AND status='supported'""",
                        uuid.UUID(manifest["owner_user_id"]),
                        uuid.UUID(item["target_claim_id"]),
                    )
                    revisions_before = await connection.fetchval(
                        """SELECT count(*) FROM memory.claim_revision
                           WHERE owner_user_id=$1 AND claim_id=$2""",
                        uuid.UUID(manifest["owner_user_id"]),
                        uuid.UUID(item["target_claim_id"]),
                    )
                    if (
                        target_before is None
                        or revisions_before != item["current_revision_number"]
                    ):
                        raise ApplyError("reinforcement target state drifted")

                projected = await connection.fetchrow(
                    "SELECT * FROM memory.apply_projection_v5($1,$2,$3,$4,$5)",
                    uuid.UUID(item["projection_request_id"]),
                    uuid.UUID(item["plan_id"]),
                    "p01",
                    uuid.UUID(item["review_id"]),
                    item["projection_apply_manifest_sha256"],
                )
                expected_outcome = "applied" if args.mode == "apply" else "replayed"
                expected_projection_rows = (
                    (4 if item["target_action"] == "create" else 2)
                    + item["observation_count"]
                    if args.mode == "apply"
                    else 0
                )
                if (
                    projected is None
                    or projected["outcome"] != expected_outcome
                    or projected["lane"] != "claim"
                    or projected["rows_written"] != expected_projection_rows
                ):
                    raise ApplyError("projection apply outcome mismatch")
                claim_id = str(projected["aggregate_id"])
                assessment_review_id = None
                assessment_review_manifest = None
                assessment_apply_manifest = None
                assessment_id = None
                assessment_event_id = None

                if item["target_action"] == "create":
                    if projected["revision_number"] != 1:
                        raise ApplyError("created claim revision mismatch")
                    if args.mode == "apply":
                        review_preflight = await connection.fetchrow(
                            """SELECT * FROM memory.preflight_claim_assessment_review_v5(
                              $1,'promote_supported'::memory.claim_assessment_action_v5,
                              $2::numeric,$3::numeric,$4::numeric,$5::numeric,
                              $6::jsonb,$7,$8,$9)""",
                            uuid.UUID(claim_id),
                            assessment["support_score"],
                            assessment["opposition_score"],
                            assessment["claim_confidence"],
                            assessment["assessment_confidence"],
                            reason_codes,
                            assessment["rationale"],
                            assessment["reviewer_type"],
                            assessment["reviewer_ref"],
                        )
                        if (
                            review_preflight["from_status"] != "candidate"
                            or review_preflight["target_status"] != "supported"
                        ):
                            raise ApplyError("claim assessment transition mismatch")
                        reviewed = await connection.fetchrow(
                            """SELECT * FROM memory.review_claim_assessment_v5(
                              $1,$2,'promote_supported'::memory.claim_assessment_action_v5,
                              $3::numeric,$4::numeric,$5::numeric,$6::numeric,
                              $7::jsonb,$8,$9,$10,$11)""",
                            uuid.UUID(item["assessment_review_request_id"]),
                            uuid.UUID(claim_id),
                            assessment["support_score"],
                            assessment["opposition_score"],
                            assessment["claim_confidence"],
                            assessment["assessment_confidence"],
                            reason_codes,
                            assessment["rationale"],
                            assessment["reviewer_type"],
                            assessment["reviewer_ref"],
                            review_preflight["authorization_manifest_sha256"],
                        )
                        if reviewed["outcome"] != "applied":
                            raise ApplyError("claim assessment review was not applied")
                        assessment_review_id = str(reviewed["review_id"])
                        assessment_review_manifest = review_preflight[
                            "authorization_manifest_sha256"
                        ]
                        assessment_preflight = await connection.fetchrow(
                            "SELECT * FROM memory.preflight_claim_assessment_apply_v5($1,$2)",
                            uuid.UUID(claim_id),
                            uuid.UUID(assessment_review_id),
                        )
                        assessment_apply_manifest = assessment_preflight[
                            "apply_manifest_sha256"
                        ]
                    else:
                        previous = prior[item["plan_id"]]
                        assessment_review_id = previous["assessment_review_id"]
                        assessment_review_manifest = previous[
                            "assessment_review_manifest_sha256"
                        ]
                        assessment_apply_manifest = previous[
                            "assessment_apply_manifest_sha256"
                        ]
                    assessed = await connection.fetchrow(
                        "SELECT * FROM memory.apply_claim_assessment_v5($1,$2,$3,$4)",
                        uuid.UUID(item["assessment_apply_request_id"]),
                        uuid.UUID(claim_id),
                        uuid.UUID(assessment_review_id),
                        assessment_apply_manifest,
                    )
                    expected_assessment = "applied" if args.mode == "apply" else "replayed"
                    if (
                        assessed["outcome"] != expected_assessment
                        or assessed["resulting_revision_number"] != 2
                        or assessed["rows_written"] != (5 if args.mode == "apply" else 0)
                    ):
                        raise ApplyError("claim assessment apply outcome mismatch")
                    assessment_id = str(assessed["assessment_id"])
                    assessment_event_id = str(assessed["event_id"])
                    resulting_revision = 2
                else:
                    if (
                        claim_id != item["target_claim_id"]
                        or projected["revision_number"]
                        != item["current_revision_number"]
                    ):
                        raise ApplyError("reinforcement target identity mismatch")
                    target_after = await connection.fetchval(
                        """SELECT to_jsonb(stored)::text
                           FROM memory.claim AS stored
                           WHERE owner_user_id=$1 AND claim_id=$2""",
                        uuid.UUID(manifest["owner_user_id"]),
                        uuid.UUID(claim_id),
                    )
                    revisions_after = await connection.fetchval(
                        """SELECT count(*) FROM memory.claim_revision
                           WHERE owner_user_id=$1 AND claim_id=$2""",
                        uuid.UUID(manifest["owner_user_id"]),
                        uuid.UUID(claim_id),
                    )
                    if target_after != target_before or revisions_after != revisions_before:
                        raise ApplyError("reinforcement mutated claim content or revisions")
                    resulting_revision = item["current_revision_number"]

                if args.mode == "apply":
                    item_rows = (
                        sum(CREATE_ROWS.values())
                        + item["observation_count"]
                        - 1
                        if item["target_action"] == "create"
                        else sum(REINFORCE_ROWS.values())
                        + item["observation_count"]
                        - 1
                    )
                    insert_rows += item_rows
                    mutated_rows += item_rows + (
                        1 if item["target_action"] == "create" else 0
                    )
                outcomes.append(
                    {
                        "plan_id": item["plan_id"],
                        "predicate": item["predicate"],
                        "target_action": item["target_action"],
                        "claim_id": claim_id,
                        "claim_revision_number": resulting_revision,
                        "projection_apply_event_id": str(projected["apply_event_id"]),
                        "assessment_review_id": assessment_review_id,
                        "assessment_review_manifest_sha256": assessment_review_manifest,
                        "assessment_apply_manifest_sha256": assessment_apply_manifest,
                        "assessment_id": assessment_id,
                        "assessment_event_id": assessment_event_id,
                        "outcome": expected_outcome,
                    }
                )

        if args.mode == "preflight":
            await transaction.rollback()
        else:
            expected_insert = manifest["expected_insert_rows"] if args.mode == "apply" else 0
            expected_mutated = manifest["expected_mutated_rows"] if args.mode == "apply" else 0
            if insert_rows != expected_insert or mutated_rows != expected_mutated:
                raise ApplyError("aggregate row budget mismatch")
            await transaction.commit()
    finally:
        await connection.close()

    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "apply_result_file_sha256": file_sha256(prior_path) if prior_path else None,
        "action_counts": manifest["action_counts"],
        "insert_rows": insert_rows,
        "mutated_rows": mutated_rows,
        "outcomes": outcomes,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "projection_outbox_rows_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"mode={args.mode}")
    print(f"insert_rows={insert_rows}")
    print(f"mutated_rows={mutated_rows}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
