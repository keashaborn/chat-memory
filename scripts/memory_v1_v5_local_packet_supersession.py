#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlparse
import uuid

import asyncpg


WORKER_VERSION = "memory_v1_v5_local_packet_supersession_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_packet_supersession_apply_v1"
REASON_CODE = "temporal_persistence_matrix_reextracted"
IDENTITY_NAMESPACE = uuid.UUID("9cc1c137-bc31-521b-a9e0-b93d0c8e48df")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("packet supersession database DSN scheme is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("packet supersession database DSN must be loopback-only")
    return value


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or append one owner-scoped packet supersession. Reports "
            "contain hashes and counts only."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--prior-packet-id", required=True)
    parser.add_argument("--replacement-packet-id", required=True)
    parser.add_argument("--expected-prior-storage-sha256", required=True)
    parser.add_argument("--expected-replacement-storage-sha256", required=True)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validated_arguments(
    args: argparse.Namespace,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    try:
        owner = uuid.UUID(args.owner_user_id)
        prior = uuid.UUID(args.prior_packet_id)
        replacement = uuid.UUID(args.replacement_packet_id)
    except ValueError as exc:
        raise RuntimeError("owner and packet ids must be UUIDs") from exc
    if prior == replacement:
        raise RuntimeError("prior and replacement packet ids must differ")
    if not SHA256_RE.fullmatch(args.expected_prior_storage_sha256):
        raise RuntimeError("expected prior packet hash is invalid")
    if not SHA256_RE.fullmatch(args.expected_replacement_storage_sha256):
        raise RuntimeError("expected replacement packet hash is invalid")
    if args.expected_plan_sha256 is not None and not SHA256_RE.fullmatch(
        args.expected_plan_sha256
    ):
        raise RuntimeError("expected plan hash is invalid")
    if args.apply and args.expected_plan_sha256 is None:
        raise RuntimeError("apply requires an expected plan hash")
    if args.apply and os.getenv("MEMORY_V1_V5_LOCAL_PACKET_SUPERSESSION_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("packet supersession apply capability is absent")
    return owner, prior, replacement


def operation_ids(
    owner: uuid.UUID,
    prior: uuid.UUID,
    replacement: uuid.UUID,
    prior_storage_sha256: str,
    replacement_storage_sha256: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    identity = (
        f"{owner}|{prior}|{replacement}|{prior_storage_sha256}|"
        f"{replacement_storage_sha256}|{REASON_CODE}"
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"supersession|{identity}"),
    )


def plan_contract(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "prior_packet_storage_sha256": row["prior_packet_storage_sha256"],
        "replacement_packet_storage_sha256": row[
            "replacement_packet_storage_sha256"
        ],
        "prior_policy_compiler_sha256": row["prior_policy_compiler_sha256"],
        "replacement_policy_compiler_sha256": row[
            "replacement_policy_compiler_sha256"
        ],
        "prior_selector_version_sha256": sha256_text(
            row["prior_selector_version"]
        ),
        "replacement_selector_version_sha256": sha256_text(
            row["replacement_selector_version"]
        ),
        "reason_code": row["reason_code"],
        "prior_observation_count": row["prior_observation_count"],
        "replacement_observation_count": row[
            "replacement_observation_count"
        ],
    }


async def read_plan(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    prior: uuid.UUID,
    replacement: uuid.UUID,
) -> dict[str, Any] | None:
    async with conn.transaction(readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        row = await conn.fetchrow(
            "SELECT * FROM "
            "memory.plan_owner_v5_local_packet_supersession_v1($1,$2)",
            prior,
            replacement,
        )
    return dict(row) if row else None


async def invoke(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    prior: uuid.UUID,
    replacement: uuid.UUID,
    operation_id: uuid.UUID,
    supersession_id: uuid.UUID,
    prior_storage_sha256: str,
    replacement_storage_sha256: str,
) -> dict[str, Any]:
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        row = await conn.fetchrow(
            """
            SELECT * FROM
              memory.finalize_owner_v5_local_packet_supersession_v1(
                $1,$2,$3,$4,$5,$6,$7
              )
            """,
            operation_id,
            supersession_id,
            prior,
            replacement,
            prior_storage_sha256,
            replacement_storage_sha256,
            REASON_CODE,
        )
    if row is None:
        raise RuntimeError("packet supersession apply returned no row")
    return dict(row)


async def run() -> int:
    args = arguments()
    owner, prior, replacement = validated_arguments(args)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=30, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("packet supersession requires brains_app session")
        plan = await read_plan(conn, owner, prior, replacement)
        if plan is None:
            raise RuntimeError("exact packet supersession plan is absent")
        if (
            plan["prior_packet_id"] != prior
            or plan["replacement_packet_id"] != replacement
            or plan["prior_packet_storage_sha256"]
            != args.expected_prior_storage_sha256
            or plan["replacement_packet_storage_sha256"]
            != args.expected_replacement_storage_sha256
            or plan["reason_code"] != REASON_CODE
        ):
            raise RuntimeError("exact packet supersession plan changed")
        contract = plan_contract(plan)
        plan_sha256 = sha256_text(stable_json(contract))
        if args.expected_plan_sha256 is not None and plan_sha256 != (
            args.expected_plan_sha256
        ):
            raise RuntimeError("packet supersession plan hash changed")
        report = {
            "contract_version": WORKER_VERSION,
            "apply": args.apply,
            "owner_user_id_sha256": sha256_text(str(owner)),
            "prior_packet_id_sha256": sha256_text(str(prior)),
            "replacement_packet_id_sha256": sha256_text(str(replacement)),
            "plan_sha256": plan_sha256,
            "reason_code": REASON_CODE,
            "prior_observation_count": plan["prior_observation_count"],
            "replacement_observation_count": plan[
                "replacement_observation_count"
            ],
            "external_model_calls": 0,
            "local_model_calls": 0,
            "claim_writes": 0,
            "qdrant_writes": 0,
            "prompt_influence": 0,
        }
        if not args.apply:
            report.update(
                {
                    "outcome": "eligible",
                    "write_counts": {"supersessions": 0},
                    "zero_write_replay_proved": True,
                }
            )
            print(stable_json(report))
            return 0

        operation_id, supersession_id = operation_ids(
            owner,
            prior,
            replacement,
            args.expected_prior_storage_sha256,
            args.expected_replacement_storage_sha256,
        )
        applied = await invoke(
            conn,
            owner=owner,
            prior=prior,
            replacement=replacement,
            operation_id=operation_id,
            supersession_id=supersession_id,
            prior_storage_sha256=args.expected_prior_storage_sha256,
            replacement_storage_sha256=(
                args.expected_replacement_storage_sha256
            ),
        )
        replayed = await invoke(
            conn,
            owner=owner,
            prior=prior,
            replacement=replacement,
            operation_id=operation_id,
            supersession_id=supersession_id,
            prior_storage_sha256=args.expected_prior_storage_sha256,
            replacement_storage_sha256=(
                args.expected_replacement_storage_sha256
            ),
        )
        if applied["apply_outcome"] not in {"applied", "replayed"}:
            raise RuntimeError("packet supersession apply outcome is invalid")
        if replayed["apply_outcome"] != "replayed":
            raise RuntimeError("packet supersession replay wrote again")
        report.update(
            {
                "outcome": "superseded",
                "apply_outcome": applied["apply_outcome"],
                "write_counts": {
                    "supersessions": int(
                        applied["apply_outcome"] == "applied"
                    )
                },
                "zero_write_replay_proved": True,
            }
        )
        print(stable_json(report))
        return 0
    finally:
        await conn.close()


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        print(
            stable_json(
                {
                    "contract_version": WORKER_VERSION,
                    "apply": False,
                    "outcome": "rejected",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "external_model_calls": 0,
                    "claim_writes": 0,
                    "qdrant_writes": 0,
                    "prompt_influence": 0,
                }
            ),
            file=__import__("sys").stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
