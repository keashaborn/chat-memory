#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLAN_VERSION = "memory_v1_v5_source_evidence_plan_v1"
AUTH_VERSION = "memory_v1_v5_source_evidence_authorization_v1"
APPLY_VERSION = "memory_v1_v5_source_evidence_apply_v1"
CONFIRMATION = "APPEND_ONLY_CANONICAL_EVIDENCE_NO_V5_STAGE"
BATCH_NAMESPACE = uuid.UUID("4011ee0d-758c-50ef-a4c4-fbca0a844b78")
SENSITIVITY_RANK = {"low": 0, "medium": 1, "high": 2, "restricted": 3}
LIVE_REPORT_MODE = "zero_write_relational_v5_specialized_live_evaluation"
MATERIALIZED_REPORT_MODE = (
    "zero_write_relational_v5_specialized_materialized_evaluation"
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Owner-scoped canonical evidence preflight/apply for V5 sources"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--report", required=True)
    preflight.add_argument("--case-id", action="append", required=True)
    preflight.add_argument("--owner-user-id", required=True)
    preflight.add_argument("--output", required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--authorization", required=True)
    apply.add_argument("--output", required=True)
    apply.add_argument("--confirm", required=True)
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return sha256_bytes(payload)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RuntimeError("authorization timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def git_head_clean() -> str:
    root = Path(__file__).resolve().parents[1]
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("evidence operation requires a clean worktree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def max_sensitivity(packet: dict[str, Any]) -> str:
    values = [item["sensitivity"] for item in packet.get("observations", [])]
    values.extend(item["sensitivity"] for item in packet.get("deferrals", []))
    return max(values or ["medium"], key=lambda value: SENSITIVITY_RANK[value])


def load_report_sources(
    report: dict[str, Any], *, owner: uuid.UUID, case_ids: list[str]
) -> list[dict[str, Any]]:
    mode = report.get("mode")
    if mode not in {LIVE_REPORT_MODE, MATERIALIZED_REPORT_MODE}:
        raise RuntimeError("source report mode is not approved")
    if report.get("owner_user_id") != str(owner):
        raise RuntimeError("report owner mismatch")
    if report.get("store") is not False or not (report.get("zero_write_proof") or {}).get(
        "passed"
    ):
        raise RuntimeError("report is not a passed store=false zero-write evaluation")
    if mode == MATERIALIZED_REPORT_MODE:
        if report.get("external_model_calls") != 0 or not _valid_digest(
            report.get("source_replay_report_sha256"), 64
        ):
            raise RuntimeError("materialized report provenance is invalid")
    if not case_ids or len(case_ids) != len(set(case_ids)):
        raise RuntimeError("case IDs must be a unique nonempty list")
    indexed = {item["case_id"]: item for item in report.get("sources", [])}
    if set(case_ids) - set(indexed):
        raise RuntimeError("requested case is absent from report")
    output: list[dict[str, Any]] = []
    for case_id in case_ids:
        source = indexed[case_id]
        if (source.get("evaluation") or {}).get("passed") is not True:
            raise RuntimeError(f"case did not pass evaluation: {case_id}")
        if mode == MATERIALIZED_REPORT_MODE:
            provenance = source.get("materialization_provenance") or {}
            if (
                provenance.get("source_replay_report_sha256")
                != report["source_replay_report_sha256"]
                or not _valid_digest(provenance.get("materializer_commit"), 40)
                or provenance.get("normalization_policy_version")
                != "memory_v1_relational_specialized_v5_1"
            ):
                raise RuntimeError("materialized source provenance is invalid")
        packet = source["packet"]
        envelope = packet["source_envelope"]
        output.append(
            {
                "case_id": case_id,
                "source_external_id": str(uuid.UUID(envelope["source_external_id"])),
                "source_sha256": envelope["source_sha256"],
                "source_recorded_at": envelope["source_recorded_at"],
                "sensitivity": max_sensitivity(packet),
            }
        )
    return output


def _valid_digest(value: Any, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(
        character in "0123456789abcdef" for character in value
    )


def validate_authorization(
    path: Path, *, plan_sha256: str, head_commit: str, owner: str
) -> tuple[dict[str, Any], str]:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise RuntimeError("authorization file mode must be 0600")
    raw = path.read_bytes()
    value = json.loads(raw)
    expected_keys = {
        "contract_version", "authorization_id", "authorized", "authorized_by",
        "authorized_at", "expires_at", "plan_sha256", "expected_head_commit",
        "owner_user_id", "target_server", "confirmation",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise RuntimeError("authorization fields mismatch")
    if value["contract_version"] != AUTH_VERSION or value["authorized"] is not True:
        raise RuntimeError("authorization contract is not active")
    uuid.UUID(value["authorization_id"])
    if value["authorized_by"] != "Eric Lund":
        raise RuntimeError("authorization actor mismatch")
    if value["plan_sha256"] != plan_sha256:
        raise RuntimeError("authorization plan hash mismatch")
    if value["expected_head_commit"] != head_commit:
        raise RuntimeError("authorization Git commit mismatch")
    if value["owner_user_id"] != owner or value["target_server"] != "seebx":
        raise RuntimeError("authorization owner/server mismatch")
    if value["confirmation"] != CONFIRMATION:
        raise RuntimeError("authorization confirmation mismatch")
    now = datetime.now(timezone.utc)
    authorized_at = parse_time(value["authorized_at"])
    expires_at = parse_time(value["expires_at"])
    if not authorized_at <= now <= expires_at:
        raise RuntimeError("authorization is not currently valid")
    if (expires_at - authorized_at).total_seconds() > 1800:
        raise RuntimeError("authorization validity exceeds 30 minutes")
    return value, sha256_bytes(raw)


async def _set_actor(conn: Any, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))


async def _verified_sources(
    conn: Any, *, owner: uuid.UUID, sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for source in sources:
        source_id = uuid.UUID(source["source_external_id"])
        row = await conn.fetchrow(
            """
            SELECT id, owner_user_id, source, text, created_at
              FROM public.chat_log
             WHERE owner_user_id=$1 AND id=$2
            """,
            owner,
            source_id,
        )
        if row is None or row["owner_user_id"] != owner:
            raise RuntimeError(f"owner-scoped chat source missing: {source_id}")
        if row["source"] != "frontend/chat:user":
            raise RuntimeError(f"chat source type mismatch: {source_id}")
        content = str(row["text"] or "")
        if sha256_text(content) != source["source_sha256"]:
            raise RuntimeError(f"chat source hash mismatch: {source_id}")
        if row["created_at"] != parse_time(source["source_recorded_at"]):
            raise RuntimeError(f"chat source timestamp mismatch: {source_id}")
        evidence = await conn.fetchrow(
            """
            SELECT evidence_id, content_sha256, status::text
              FROM memory.evidence
             WHERE owner_user_id=$1 AND source_system='public.chat_log'
               AND external_id=$2
            """,
            owner,
            str(source_id),
        )
        if evidence and (
            evidence["content_sha256"] != source["source_sha256"]
            or evidence["status"] != "active"
        ):
            raise RuntimeError(f"canonical evidence conflicts with source: {source_id}")
        output.append(
            {
                **source,
                "content": content,
                "operation": "reused" if evidence else "inserted",
                "existing_evidence_id": str(evidence["evidence_id"]) if evidence else None,
            }
        )
    return output


async def preflight(args: argparse.Namespace) -> int:
    import asyncpg

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owner = uuid.UUID(args.owner_user_id)
    report_path = Path(args.report).resolve()
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    sources = load_report_sources(report, owner=owner, case_ids=args.case_id)
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT current_user") != "brains_app":
            raise RuntimeError("source evidence preflight requires brains_app")
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await _set_actor(conn, owner)
            verified = await _verified_sources(conn, owner=owner, sources=sources)
    finally:
        await conn.close()
    head = git_head_clean()
    public_sources = [
        {key: value for key, value in source.items() if key != "content"}
        for source in verified
    ]
    plan = {
        "contract_version": PLAN_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "preflight_only_zero_write",
        "target_server": "seebx",
        "owner_user_id": str(owner),
        "required_head_commit": head,
        "source_report_path": str(report_path),
        "source_report_sha256": sha256_bytes(report_bytes),
        "source_count": len(public_sources),
        "sources": public_sources,
        "authorization_required": True,
        "apply_authorized": False,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }
    output_path = Path(args.output).resolve()
    digest = secure_write(output_path, plan)
    print(stable_json({
        "version": PLAN_VERSION, "output": str(output_path), "sha256": digest,
        "sources": len(public_sources),
        "insert_preview": sum(item["operation"] == "inserted" for item in public_sources),
        "reuse_preview": sum(item["operation"] == "reused" for item in public_sources),
        "database_writes": 0, "qdrant_writes": 0, "external_model_calls": 0,
    }))
    return 0


async def apply(args: argparse.Namespace) -> int:
    import asyncpg

    if os.getenv("MEMORY_V1_V5_EVIDENCE_APPLY") != "authorized":
        raise RuntimeError("apply requires MEMORY_V1_V5_EVIDENCE_APPLY=authorized")
    if args.confirm != CONFIRMATION:
        raise RuntimeError(f"--confirm must equal {CONFIRMATION}")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    plan_path = Path(args.plan).resolve()
    plan_bytes = plan_path.read_bytes()
    plan_sha = sha256_bytes(plan_bytes)
    plan = json.loads(plan_bytes)
    if plan.get("contract_version") != PLAN_VERSION or plan.get("apply_authorized") is not False:
        raise RuntimeError("invalid or mutated evidence plan")
    head = git_head_clean()
    if plan["required_head_commit"] != head:
        raise RuntimeError("plan Git commit is stale")
    owner = uuid.UUID(plan["owner_user_id"])
    authorization, authorization_sha = validate_authorization(
        Path(args.authorization).resolve(),
        plan_sha256=plan_sha,
        head_commit=head,
        owner=str(owner),
    )
    report_path = Path(plan["source_report_path"])
    if sha256_bytes(report_path.read_bytes()) != plan["source_report_sha256"]:
        raise RuntimeError("source report changed after preflight")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    inserted = reused = 0
    evidence_rows: list[dict[str, Any]] = []
    batch_id = uuid.uuid5(BATCH_NAMESPACE, f"{owner}|{plan_sha}")
    batch_key = f"v5_source_evidence:{plan_sha}"
    try:
        if await conn.fetchval("SELECT current_user") != "brains_app":
            raise RuntimeError("source evidence apply requires brains_app")
        async with conn.transaction():
            await _set_actor(conn, owner)
            verified = await _verified_sources(conn, owner=owner, sources=plan["sources"])
            existing_batch = await conn.fetchrow(
                """SELECT batch_id,inserted_count,reused_count FROM memory.evidence_ingest_batch
                    WHERE owner_user_id=$1 AND batch_key=$2""",
                owner, batch_key,
            )
            if existing_batch:
                rows = await conn.fetch(
                    """SELECT evidence_id,external_id,content_sha256,operation
                          FROM memory.evidence_ingest_batch_row
                         WHERE owner_user_id=$1 AND batch_id=$2 ORDER BY external_id""",
                    owner, existing_batch["batch_id"],
                )
                evidence_rows = [
                    {**dict(row), "evidence_id": str(row["evidence_id"])} for row in rows
                ]
                outcome = "replayed"
            else:
                for source in verified:
                    recorded = await conn.fetchrow(
                        """
                        SELECT evidence_id,outcome,content_sha256
                        FROM memory.record_owner_evidence_v1(
                          'user_statement'::memory.evidence_kind,$1,$2,$3,$4,1,1,$5,
                          $6::memory.sensitivity_level,$7::jsonb
                        )
                        """,
                        "public.chat_log",
                        source["source_external_id"],
                        source["content"],
                        parse_time(source["source_recorded_at"]),
                        f"public.chat_log:{source['source_external_id']}",
                        source["sensitivity"],
                        stable_json({
                            "contract_version": PLAN_VERSION,
                            "case_id": source["case_id"],
                            "source_report_sha256": plan["source_report_sha256"],
                        }),
                    )
                    if (
                        recorded is None
                        or recorded["outcome"] not in {"applied", "replayed"}
                        or recorded["content_sha256"] != source["source_sha256"]
                    ):
                        raise RuntimeError(
                            f"controlled evidence result mismatch: {source['case_id']}"
                        )
                    evidence_id = recorded["evidence_id"]
                    operation = (
                        "inserted" if recorded["outcome"] == "applied" else "reused"
                    )
                    if operation == "inserted":
                        inserted += 1
                    else:
                        reused += 1
                    evidence_rows.append({
                        "evidence_id": str(evidence_id),
                        "external_id": source["source_external_id"],
                        "content_sha256": source["source_sha256"],
                        "operation": operation,
                    })
                source_snapshot_sha = sha256_text(stable_json([
                    {key: value for key, value in source.items() if key != "content"}
                    for source in verified
                ]))
                await conn.execute(
                    """INSERT INTO memory.evidence_ingest_batch(
                         batch_id,owner_user_id,batch_key,manifest_version,plan_version,
                         input_fingerprint_sha256,reviewed_report_sha256,
                         authorization_manifest_sha256,source_snapshot_sha256,
                         source_row_count,expected_evidence_count,inserted_count,reused_count,
                         actor_user_id,invoked_by_role,metadata)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10,$11,$12,$2,current_user,$13::jsonb)""",
                    batch_id, owner, batch_key, AUTH_VERSION, PLAN_VERSION, plan_sha,
                    plan["source_report_sha256"], authorization_sha, source_snapshot_sha,
                    len(evidence_rows), inserted, reused,
                    stable_json({
                        "authorization_id": authorization["authorization_id"],
                        "case_ids": [item["case_id"] for item in plan["sources"]],
                        "v5_stage_invoked": False,
                    }),
                )
                for row in evidence_rows:
                    await conn.execute(
                        """INSERT INTO memory.evidence_ingest_batch_row(
                             owner_user_id,batch_id,evidence_id,external_id,content_sha256,operation)
                           VALUES($1,$2,$3,$4,$5,$6)""",
                        owner, batch_id, uuid.UUID(row["evidence_id"]), row["external_id"],
                        row["content_sha256"], row["operation"],
                    )
                outcome = "applied"
        if outcome == "replayed":
            inserted = reused = 0
    finally:
        await conn.close()
    result = {
        "contract_version": APPLY_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_server": "seebx",
        "owner_user_id": str(owner),
        "plan_sha256": plan_sha,
        "authorization_sha256": authorization_sha,
        "batch_id": str(batch_id),
        "outcome": outcome,
        "inserted": inserted,
        "reused": reused,
        "evidence_rows": sorted(evidence_rows, key=lambda item: item["external_id"]),
        "v5_stage_invoked": False,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }
    output_path = Path(args.output).resolve()
    output_sha = secure_write(output_path, result)
    print(stable_json({
        "version": APPLY_VERSION, "output": str(output_path), "sha256": output_sha,
        "outcome": outcome, "inserted": inserted, "reused": reused,
        "v5_stage_invoked": False, "qdrant_writes": 0, "external_model_calls": 0,
    }))
    return 0


async def main() -> int:
    args = arguments()
    return await (preflight(args) if args.command == "preflight" else apply(args))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
