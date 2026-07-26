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


WORKER_VERSION = "memory_v1_v5_2_evidence_context_reextract_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_2_evidence_context_reextract_apply_v1"
SELECTOR_VERSION = "20260726_v5_2_evidence_context_reextract_v1"
NEXT_COMPILER_VERSION = "memory_v1_semantic_policy_compiler_v8"
REASON_CODE = "evidence_context_coreference_available"
IDENTITY_NAMESPACE = uuid.UUID("7fda4a41-910e-48ac-9065-ab48db4478e9")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("V5.2 evidence context reextract database DSN scheme is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("V5.2 evidence context reextract database DSN must be loopback-only")
    return value


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or append one owner-scoped extraction job when bounded "
            "sibling evidence can resolve a prior contextual deferral. "
            "Reports contain hashes and counts only."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--prior-packet-id", required=True)
    parser.add_argument("--expected-content-sha256", required=True)
    parser.add_argument("--expected-packet-storage-sha256", required=True)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validated_arguments(args: argparse.Namespace) -> tuple[uuid.UUID, uuid.UUID]:
    try:
        owner = uuid.UUID(args.owner_user_id)
        packet = uuid.UUID(args.prior_packet_id)
    except ValueError as exc:
        raise RuntimeError("owner and prior packet ids must be UUIDs") from exc
    if not SHA256_RE.fullmatch(args.expected_content_sha256):
        raise RuntimeError("expected content hash is invalid")
    if not SHA256_RE.fullmatch(args.expected_packet_storage_sha256):
        raise RuntimeError("expected packet storage hash is invalid")
    if args.expected_plan_sha256 is not None and not SHA256_RE.fullmatch(
        args.expected_plan_sha256
    ):
        raise RuntimeError("expected plan hash is invalid")
    if args.apply and args.expected_plan_sha256 is None:
        raise RuntimeError("apply requires an expected plan hash")
    if args.apply and os.getenv("MEMORY_V1_V5_2_EVIDENCE_CONTEXT_REEXTRACT_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("V5.2 evidence context reextract apply capability is absent")
    return owner, packet


def operation_ids(
    owner: uuid.UUID,
    packet: uuid.UUID,
    content_sha256: str,
    storage_sha256: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    identity = (
        f"{owner}|{packet}|{content_sha256}|{storage_sha256}|"
        f"{SELECTOR_VERSION}|{NEXT_COMPILER_VERSION}"
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"job|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"terminal|{identity}"),
    )


def plan_contract(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_content_sha256": row["evidence_content_sha256"],
        "prior_packet_storage_sha256": row[
            "prior_packet_storage_sha256"
        ],
        "prior_policy_compiler_sha256": row[
            "prior_policy_compiler_sha256"
        ],
        "prior_selector_version_sha256": sha256_text(
            row["prior_selector_version"]
        ),
        "prior_disposition_count": row["prior_disposition_count"],
        "reason_code": row["reason_code"],
        "next_selector_version": row["next_selector_version"],
        "next_policy_compiler_version": row[
            "next_policy_compiler_version"
        ],
    }


async def read_plan(
    conn: asyncpg.Connection, owner: uuid.UUID, packet: uuid.UUID
) -> dict[str, Any] | None:
    async with conn.transaction(readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        row = await conn.fetchrow(
            "SELECT * FROM memory.plan_owner_v5_2_evidence_context_reextract_v1($1)",
            packet,
        )
    return dict(row) if row else None


async def invoke(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    packet: uuid.UUID,
    operation_id: uuid.UUID,
    job_id: uuid.UUID,
    terminal_id: uuid.UUID,
    content_sha256: str,
    storage_sha256: str,
) -> dict[str, Any]:
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.enqueue_owner_v5_2_evidence_context_reextract_v1(
              $1,$2,$3,$4,$5,$6,$7,$8
            )
            """,
            operation_id,
            job_id,
            terminal_id,
            packet,
            content_sha256,
            storage_sha256,
            SELECTOR_VERSION,
            NEXT_COMPILER_VERSION,
        )
    if row is None:
        raise RuntimeError("V5.2 evidence context reextract apply returned no row")
    return dict(row)


async def run() -> int:
    args = arguments()
    owner, packet = validated_arguments(args)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=30, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("V5.2 evidence context reextract requires brains_app session")
        plan = await read_plan(conn, owner, packet)
        if plan is None:
            raise RuntimeError("exact V5.2 evidence context reextract plan is absent")
        if (
            plan["prior_packet_id"] != packet
            or plan["evidence_content_sha256"]
            != args.expected_content_sha256
            or plan["prior_packet_storage_sha256"]
            != args.expected_packet_storage_sha256
            or plan["reason_code"] != REASON_CODE
            or plan["next_selector_version"] != SELECTOR_VERSION
            or plan["next_policy_compiler_version"] != NEXT_COMPILER_VERSION
        ):
            raise RuntimeError("exact V5.2 evidence context reextract plan changed")
        contract = plan_contract(plan)
        plan_sha256 = sha256_text(stable_json(contract))
        if args.expected_plan_sha256 is not None and plan_sha256 != (
            args.expected_plan_sha256
        ):
            raise RuntimeError("V5.2 evidence context reextract plan hash changed")
        report = {
            "contract_version": WORKER_VERSION,
            "apply": args.apply,
            "owner_user_id_sha256": sha256_text(str(owner)),
            "prior_packet_id_sha256": sha256_text(str(packet)),
            "plan_sha256": plan_sha256,
            "reason_code": REASON_CODE,
            "next_selector_version_sha256": sha256_text(SELECTOR_VERSION),
            "next_policy_compiler_version": NEXT_COMPILER_VERSION,
            "prior_disposition_count": plan["prior_disposition_count"],
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
                    "write_counts": {"jobs": 0, "terminals": 0, "events": 0},
                    "zero_write_replay_proved": True,
                }
            )
            print(stable_json(report))
            return 0

        operation_id, job_id, terminal_id = operation_ids(
            owner,
            packet,
            args.expected_content_sha256,
            args.expected_packet_storage_sha256,
        )
        applied = await invoke(
            conn,
            owner=owner,
            packet=packet,
            operation_id=operation_id,
            job_id=job_id,
            terminal_id=terminal_id,
            content_sha256=args.expected_content_sha256,
            storage_sha256=args.expected_packet_storage_sha256,
        )
        replayed = await invoke(
            conn,
            owner=owner,
            packet=packet,
            operation_id=operation_id,
            job_id=job_id,
            terminal_id=terminal_id,
            content_sha256=args.expected_content_sha256,
            storage_sha256=args.expected_packet_storage_sha256,
        )
        if applied["apply_outcome"] not in {"applied", "replayed"}:
            raise RuntimeError("V5.2 evidence context reextract apply outcome is invalid")
        if replayed["apply_outcome"] != "replayed":
            raise RuntimeError("V5.2 evidence context reextract replay wrote again")
        report.update(
            {
                "outcome": "queued",
                "job_id_sha256": sha256_text(str(job_id)),
                "apply_outcome": applied["apply_outcome"],
                "write_counts": {
                    "jobs": int(applied["apply_outcome"] == "applied"),
                    "terminals": int(applied["apply_outcome"] == "applied"),
                    "events": int(applied["apply_outcome"] == "applied"),
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
