#!/usr/bin/env python3
"""Revalidate and stage reviewed V5.2 claim projection targets."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import stat
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_projection_v5_2_contract import sha256
from scripts.memory_v1_v5_2_claim_target_resolver import (
    resolve_claim_projection_target,
)
from scripts.memory_v1_v5_2_claim_target_review import (
    CONTRACT as REVIEW_CONTRACT,
    load_source,
    verify_runtime,
)
from scripts.memory_v1_v5_2_projection_stage_batch import load_contracts


CONTRACT = "memory_v1_v5_2_claim_target_stage_v1"


class ClaimTargetStageError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def private_input(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ClaimTargetStageError("review must be a private regular file")
    return path


def private_output(value: str) -> Path:
    path = Path(value).resolve()
    if path.exists():
        raise ClaimTargetStageError("output already exists")
    if not path.parent.is_dir() or stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise ClaimTargetStageError("output parent must be private")
    return path


def write_private(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def load_review(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    supplied_sha = value.pop("report_sha256", None)
    if (
        value.get("contract_version") != REVIEW_CONTRACT
        or supplied_sha != sha256(value)
        or not isinstance(value.get("items"), list)
        or not 1 <= len(value["items"]) <= 32
    ):
        raise ClaimTargetStageError("review contract or hash is invalid")
    value["report_sha256"] = supplied_sha
    seen: set[str] = set()
    for item in value["items"]:
        observation = str(uuid.UUID(str(item["observation_id"])))
        if (
            observation in seen
            or item.get("action") not in {"create", "reinforce", "manual_review"}
            or not isinstance(item.get("reason_codes"), list)
            or str(uuid.UUID(str(item["plan_id"]))) != item["plan_id"]
        ):
            raise ClaimTargetStageError("review item is invalid")
        seen.add(observation)
    return value


def _same_resolution(item: dict[str, Any], resolved: dict[str, Any]) -> bool:
    return all(
        (
            item["action"] == resolved["action"],
            item["reason_codes"] == resolved["reason_codes"],
            item["semantic_key_sha256"] == resolved["semantic_key_sha256"],
            item["target_claim_id"] == resolved["target_claim_id"],
            item["expected_revision_number"]
            == resolved["expected_revision_number"],
            item["packet_sha256"] == resolved["packet_sha256"],
            item["owner_manifest_sha256"]
            == resolved["owner_manifest_sha256"],
        )
    )


async def run() -> int:
    args = arguments()
    review_path = private_input(args.review)
    output = private_output(args.output)
    review = load_review(review_path)
    owner = str(uuid.UUID(str(review["owner_user_id"])))
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ClaimTargetStageError("POSTGRES_DSN is required")
    _, registry = load_contracts()
    conn = await asyncpg.connect(dsn, command_timeout=60)
    rows_written = 0
    outcomes: list[dict[str, Any]] = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ClaimTargetStageError("stage requires brains_app")
        if not await verify_runtime(conn):
            raise ClaimTargetStageError("stage is restricted to a disposable clone")
        async with conn.transaction(isolation="serializable"):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
            for item in sorted(
                review["items"], key=lambda value: value["observation_id"]
            ):
                source = await load_source(conn, item["observation_id"])
                resolved = await resolve_claim_projection_target(
                    conn,
                    owner_user_id=owner,
                    source=source,
                    plan_id=item["plan_id"],
                    registry=registry,
                    allow_existing_plan=True,
                )
                if not _same_resolution(item, resolved):
                    raise ClaimTargetStageError(
                        "claim target review is stale or mismatched"
                    )
                if resolved["action"] == "manual_review":
                    outcomes.append(
                        {
                            "observation_id": item["observation_id"],
                            "plan_id": item["plan_id"],
                            "action": "held",
                            "outcome": "manual_review",
                            "rows_written": 0,
                            "reason_codes": item["reason_codes"],
                        }
                    )
                    continue
                entailment_allowed = await conn.fetchval(
                    """
                    SELECT memory.observation_entailment_allows_projection_v5(
                      $1,$2
                    )
                    """,
                    uuid.UUID(item["observation_id"]),
                    source["observation_sha256"],
                )
                if entailment_allowed is not True:
                    outcomes.append(
                        {
                            "observation_id": item["observation_id"],
                            "plan_id": item["plan_id"],
                            "action": "blocked",
                            "outcome": "missing_entailment",
                            "rows_written": 0,
                            "reason_codes": [
                                "accepted_observation_entailment_required"
                            ],
                        }
                    )
                    continue
                packet = resolved["packet"]
                function = (
                    "memory.stage_projection_plan_v5_2"
                    if resolved["action"] == "create"
                    else "memory.stage_projection_reinforcement_v5_2"
                )
                row = await conn.fetchrow(
                    f"SELECT * FROM {function}($1,$2,$3)",
                    uuid.UUID(item["plan_id"]),
                    json.dumps(packet, sort_keys=True, separators=(",", ":")),
                    resolved["owner_manifest_sha256"],
                )
                if row is None:
                    raise ClaimTargetStageError("stage returned no row")
                row_value = dict(row)
                written = int(row_value["rows_written"])
                if row_value["outcome"] not in {"applied", "replayed"}:
                    raise ClaimTargetStageError("stage outcome is invalid")
                rows_written += written
                outcomes.append(
                    {
                        "observation_id": item["observation_id"],
                        "plan_id": item["plan_id"],
                        "action": resolved["action"],
                        "outcome": row_value["outcome"],
                        "rows_written": written,
                        "reason_codes": item["reason_codes"],
                    }
                )
    finally:
        await conn.close()
    outcome_counts = dict(
        sorted(Counter(item["outcome"] for item in outcomes).items())
    )
    result: dict[str, Any] = {
        "contract_version": CONTRACT,
        "owner_user_id": owner,
        "source_review_sha256": review["report_sha256"],
        "item_count": len(outcomes),
        "rows_written": rows_written,
        "outcome_counts": outcome_counts,
        "items": outcomes,
        "proofs": {
            "disposable_clone_verified": True,
            "local_model_calls": 0,
            "external_model_calls": 0,
            "claim_writes": 0,
            "qdrant_writes": 0,
            "retrieval_changes": 0,
            "prompt_influence": 0,
        },
    }
    result["result_sha256"] = sha256(result)
    write_private(output, result)
    print(
        json.dumps(
            {
                "contract_version": CONTRACT,
                "item_count": len(outcomes),
                "rows_written": rows_written,
                "outcome_counts": outcome_counts,
                "result_sha256": result["result_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
