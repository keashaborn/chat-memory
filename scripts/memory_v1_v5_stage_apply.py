#!/usr/bin/env python3
"""Controlled two-bundle V5 relational staging transaction for seebx."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any
import urllib.request
import uuid

import asyncpg


AUTH_CONTRACT = "memory_v1_v5_stage_apply_authorization_v1"
REPORT_CONTRACT = "memory_v1_v5_stage_apply_report_v1"
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
EXPECTED_CASES = {
    "v5-04": {
        "request_id": "16438b3b-30c5-53ed-b35c-959109df91d8",
        "evidence_id": "fca9e5dc-83c2-4456-8db8-1fe6102eb74d",
        "bundle_sha256": "33b09441ac89fa4dbc51ba952562f421d704c9bd109bbb598b213a09cc55b867",
        "counts": [2, 2, 1, 1, 1],
        "durable_rows": 9,
    },
    "v5-15": {
        "request_id": "b32e5347-7b8e-5505-ac67-db43704a3ea2",
        "evidence_id": "36e92633-08fd-441f-8d1d-27f16c2a3479",
        "bundle_sha256": "7194f66416ba1771f2c8a5386d39913a15a7be9ed469a6b31229e5f1be178393",
        "counts": [1, 1, 0, 1, 1],
        "durable_rows": 6,
    },
}
STAGE_TABLES = [
    "entity_mention",
    "entity_resolution_plan",
    "entity_resolution_candidate",
    "observation",
    "observation_temporal",
    "relational_stage_batch",
    "relational_operation_request",
]
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class StageError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--backup-catalog", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--collection", default="memory_claim_v1")
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def repository_state() -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[1]
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise StageError("stage apply requires a clean Git worktree")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, head


def parse_utc(value: str, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StageError(f"authorization {field} must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise StageError(f"authorization {field} must use UTC")
    return parsed


def load_authorization(path: Path, head: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise StageError("authorization file mode must be 0600")
    value = json.loads(path.read_text(encoding="utf-8"))
    expected_keys = {
        "contract_version",
        "authorization_id",
        "authorized",
        "authorized_by",
        "authorized_at",
        "expires_at",
        "expected_head_commit",
        "target_server",
        "scope",
        "owner_user_id",
        "expected_durable_rows",
        "bundles",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise StageError("authorization keys do not exactly match the contract")
    if value["contract_version"] != AUTH_CONTRACT or value["authorized"] is not True:
        raise StageError("authorization contract or flag mismatch")
    if value["authorized_by"] != "Eric Lund":
        raise StageError("authorization identity mismatch")
    uuid.UUID(value["authorization_id"])
    if value["expected_head_commit"] != head:
        raise StageError("authorization commit mismatch")
    if value["target_server"] != "seebx":
        raise StageError("authorization server mismatch")
    if value["scope"] != "stage_exact_v5_04_and_v5_15_then_zero_write_replay":
        raise StageError("authorization scope mismatch")
    if value["owner_user_id"] != OWNER or value["expected_durable_rows"] != 15:
        raise StageError("authorization owner or row budget mismatch")
    issued = parse_utc(value["authorized_at"], "authorized_at")
    expires = parse_utc(value["expires_at"], "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    if expires <= issued or expires - issued > dt.timedelta(minutes=30):
        raise StageError("authorization validity window is invalid")
    if now < issued - dt.timedelta(seconds=30) or now >= expires:
        raise StageError("authorization is not currently valid")

    specs = value["bundles"]
    if not isinstance(specs, list) or len(specs) != 2:
        raise StageError("authorization must bind exactly two bundles")
    bundles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) != {"case_id", "path", "sha256"}:
            raise StageError("bundle authorization fields mismatch")
        case_id = spec["case_id"]
        if case_id in seen or case_id not in EXPECTED_CASES:
            raise StageError("bundle case set mismatch")
        seen.add(case_id)
        path_value = spec["path"]
        if not isinstance(path_value, str) or not path_value.startswith(
            "/home/ubuntu/memory-v1-reviews/"
        ):
            raise StageError("bundle path is outside the review directory")
        path = Path(path_value)
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise StageError(f"bundle file mode must be 0600: {case_id}")
        expected = EXPECTED_CASES[case_id]
        if spec["sha256"] != expected["bundle_sha256"]:
            raise StageError(f"authorized bundle hash mismatch: {case_id}")
        if file_sha256(path) != spec["sha256"]:
            raise StageError(f"live bundle hash mismatch: {case_id}")
        bundle = json.loads(path.read_text(encoding="utf-8"))
        if bundle.get("case_id") != case_id or bundle.get("owner_user_id") != OWNER:
            raise StageError(f"bundle owner/case mismatch: {case_id}")
        if bundle.get("request_id") != expected["request_id"]:
            raise StageError(f"bundle request mismatch: {case_id}")
        if bundle.get("evidence_id") != expected["evidence_id"]:
            raise StageError(f"bundle evidence mismatch: {case_id}")
        if any(bundle.get(key) != 0 for key in ("database_writes", "external_model_calls", "qdrant_writes")):
            raise StageError(f"bundle was not generated zero-write: {case_id}")
        if bundle.get("authorized_stage") is not False:
            raise StageError(f"bundle authorization marker must remain false: {case_id}")
        for key in ("extraction_packet_sha256", "resolution_packet_sha256"):
            if not SHA_RE.fullmatch(str(bundle.get(key, ""))):
                raise StageError(f"bundle packet hash invalid: {case_id}/{key}")
        if sha256_bytes(bundle["extraction_packet_text"].encode()) != bundle["extraction_packet_sha256"]:
            raise StageError(f"extraction packet hash mismatch: {case_id}")
        if sha256_bytes(bundle["resolution_packet_text"].encode()) != bundle["resolution_packet_sha256"]:
            raise StageError(f"resolution packet hash mismatch: {case_id}")
        bundle["_path"] = str(path)
        bundles.append(bundle)
    if seen != set(EXPECTED_CASES):
        raise StageError("authorization case set is incomplete")
    bundles.sort(key=lambda item: item["case_id"])
    return value, bundles


def admin_scalar(sql: str) -> str:
    result = subprocess.run(
        [
            "docker", "exec", "brains-postgres-1", "psql", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1", "-U", "sage", "-d", "memory", "-c", sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def owner_state() -> dict[str, Any]:
    counts = {}
    for table in STAGE_TABLES:
        counts[table] = int(
            admin_scalar(
                f"SELECT count(*) FROM memory.{table} "
                f"WHERE owner_user_id='{OWNER}'::uuid"
            )
        )
    protected_sha = admin_scalar(
        "SELECT encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),"
        "'sha256'),'hex') FROM ("
        "SELECT 'entity|' || to_jsonb(e)::text AS row_json FROM memory.entity e "
        f"WHERE e.owner_user_id <> '{OWNER}'::uuid UNION ALL "
        "SELECT 'mention|' || to_jsonb(m)::text FROM memory.entity_mention m "
        f"WHERE m.owner_user_id <> '{OWNER}'::uuid UNION ALL "
        "SELECT 'plan|' || to_jsonb(p)::text FROM memory.entity_resolution_plan p "
        f"WHERE p.owner_user_id <> '{OWNER}'::uuid UNION ALL "
        "SELECT 'candidate|' || to_jsonb(c)::text FROM memory.entity_resolution_candidate c "
        f"WHERE c.owner_user_id <> '{OWNER}'::uuid UNION ALL "
        "SELECT 'observation|' || to_jsonb(o)::text FROM memory.observation o "
        f"WHERE o.owner_user_id <> '{OWNER}'::uuid UNION ALL "
        "SELECT 'temporal|' || to_jsonb(t)::text FROM memory.observation_temporal t "
        f"WHERE t.owner_user_id <> '{OWNER}'::uuid UNION ALL "
        "SELECT 'batch|' || to_jsonb(b)::text FROM memory.relational_stage_batch b "
        f"WHERE b.owner_user_id <> '{OWNER}'::uuid UNION ALL "
        "SELECT 'request|' || to_jsonb(r)::text FROM memory.relational_operation_request r "
        f"WHERE r.owner_user_id <> '{OWNER}'::uuid) rows"
    )
    entity_sha = admin_scalar(
        "SELECT encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),"
        "'sha256'),'hex') FROM (SELECT to_jsonb(e)::text row_json FROM memory.entity e) rows"
    )
    return {"counts": counts, "other_owner_sha256": protected_sha, "entity_sha256": entity_sha}


def qdrant_signature(url: str, collection: str) -> str:
    endpoint = f"{url.rstrip('/')}/collections/{collection}/points/scroll"
    body = json.dumps({"limit": 10000, "with_payload": True, "with_vector": True}).encode()
    headers = {"content-type": "application/json"}
    api_key = os.getenv("QDRANT_API_KEY", "").strip()
    if api_key:
        headers["api-key"] = api_key
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        points = json.load(response)["result"]["points"]
    points.sort(key=lambda point: str(point["id"]))
    return sha256_bytes(stable_json(points).encode())


def validate_backup(path: Path, catalog: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise StageError("backup is missing or empty")
    if not catalog.is_file() or catalog.stat().st_size <= 0:
        raise StageError("backup catalog is missing or empty")
    if stat.S_IMODE(path.stat().st_mode) != 0o600 or stat.S_IMODE(catalog.stat().st_mode) != 0o600:
        raise StageError("backup and catalog modes must be 0600")
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "restore_catalog": str(catalog),
        "restore_catalog_sha256": file_sha256(catalog),
    }


async def call_stage(conn: asyncpg.Connection, bundle: dict[str, Any]) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT batch_id,outcome,mentions_inserted,resolutions_inserted,
               candidates_inserted,observations_inserted,temporals_inserted,result
          FROM memory.stage_relational_packet_v5(
            $1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8
          )
        """,
        bundle["request_id"],
        bundle["evidence_id"],
        bundle["extractor"],
        bundle["extractor_version"],
        bundle["extraction_packet_text"],
        bundle["resolution_packet_text"],
        bundle["extraction_packet_sha256"],
        bundle["resolution_packet_sha256"],
    )
    if row is None:
        raise StageError("stage function returned no row")
    return dict(row)


async def apply_bundles(
    dsn: str, bundles: list[dict[str, Any]], *, replay: bool
) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(dsn)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise StageError("POSTGRES_DSN must authenticate as brains_app")
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            results = []
            for bundle in bundles:
                row = await call_stage(conn, bundle)
                expected = EXPECTED_CASES[bundle["case_id"]]
                counts = [
                    row["mentions_inserted"], row["resolutions_inserted"],
                    row["candidates_inserted"], row["observations_inserted"],
                    row["temporals_inserted"],
                ]
                if replay:
                    if row["outcome"] != "replayed" or counts != [0, 0, 0, 0, 0]:
                        raise StageError(f"zero-write replay mismatch: {bundle['case_id']}")
                elif row["outcome"] != "applied" or counts != expected["counts"]:
                    raise StageError(f"stage row budget mismatch: {bundle['case_id']}")
                results.append({
                    "case_id": bundle["case_id"],
                    "batch_id": str(row["batch_id"]),
                    "outcome": row["outcome"],
                    "counts": counts,
                })
            return results
    finally:
        await conn.close()


def expected_post_counts(before: dict[str, int]) -> dict[str, int]:
    deltas = {
        "entity_mention": 3,
        "entity_resolution_plan": 3,
        "entity_resolution_candidate": 1,
        "observation": 2,
        "observation_temporal": 2,
        "relational_stage_batch": 2,
        "relational_operation_request": 2,
    }
    return {key: before[key] + deltas[key] for key in STAGE_TABLES}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    _, head = repository_state()
    authorization, bundles = load_authorization(Path(args.authorization), head)
    backup = validate_backup(Path(args.backup), Path(args.backup_catalog))
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise StageError("POSTGRES_DSN and QDRANT_URL are required")
    if admin_scalar("SELECT to_regprocedure('memory.stage_relational_packet_v5(uuid,uuid,text,text,text,text,text,text)') IS NOT NULL") != "t":
        raise StageError("controlled V5 stage function is missing")
    if admin_scalar(
        f"SELECT count(*) FROM memory.entity WHERE owner_user_id='{OWNER}'::uuid "
        "AND entity_type='self' AND status='active'"
    ) != "1":
        raise StageError("trusted owner-self entity prerequisite mismatch")
    before = owner_state()
    if any(before["counts"].values()):
        raise StageError("target owner already has V5 relational staging state")
    qdrant_before = qdrant_signature(qdrant_url, args.collection)
    if args.preflight_only:
        return {
            "contract_version": REPORT_CONTRACT,
            "mode": "preflight_only",
            "authorization_id": authorization["authorization_id"],
            "head_commit": head,
            "owner_user_id": OWNER,
            "expected_durable_rows": 15,
            "bundles": [item["case_id"] for item in bundles],
            "backup": backup,
            "before": before,
            "qdrant_before_sha256": qdrant_before,
            "database_writes": 0,
        }

    applied = await apply_bundles(dsn, bundles, replay=False)
    replayed = await apply_bundles(dsn, bundles, replay=True)
    after = owner_state()
    qdrant_after = qdrant_signature(qdrant_url, args.collection)
    if after["counts"] != expected_post_counts(before["counts"]):
        raise StageError("post-stage table counts do not match the 15-row budget")
    if after["other_owner_sha256"] != before["other_owner_sha256"]:
        raise StageError("other-owner relational/entity state changed")
    if after["entity_sha256"] != before["entity_sha256"]:
        raise StageError("durable entity state changed")
    if qdrant_after != qdrant_before:
        raise StageError("Qdrant changed during relational staging")
    if admin_scalar(
        f"SELECT count(*) FROM memory.entity_resolution_apply WHERE owner_user_id='{OWNER}'::uuid"
    ) != "0" or admin_scalar(
        f"SELECT count(*) FROM memory.observation_entity_binding WHERE owner_user_id='{OWNER}'::uuid"
    ) != "0":
        raise StageError("entity resolution or observation binding was unexpectedly applied")
    return {
        "contract_version": REPORT_CONTRACT,
        "mode": "transactional_apply_and_zero_write_replay",
        "authorization_id": authorization["authorization_id"],
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "head_commit": head,
        "owner_user_id": OWNER,
        "expected_durable_rows": 15,
        "applied": applied,
        "replayed": replayed,
        "backup": backup,
        "before": before,
        "after": after,
        "qdrant_before_sha256": qdrant_before,
        "qdrant_after_sha256": qdrant_after,
        "checks": {
            "exact_staging_and_audit_rows_created": 15,
            "replay_rows_written": 0,
            "other_owner_state_unchanged": True,
            "durable_entity_state_unchanged": True,
            "resolution_apply_rows": 0,
            "observation_binding_rows": 0,
            "qdrant_unchanged": True,
            "external_model_calls": 0,
            "projection_or_retrieval_invoked": False,
        },
        "hard_stop": "before_entity_resolution_review_or_apply",
    }


async def main() -> int:
    args = arguments()
    report = await run(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(stable_json({
        "mode": report["mode"],
        "output": str(output),
        "expected_durable_rows": report["expected_durable_rows"],
        "database_writes": report.get("database_writes", 15),
    }))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}:{exc}", file=sys.stderr)
        raise SystemExit(1)
