#!/usr/bin/env python3
"""Generic owner-scoped runner for reviewed Memory V1 V5.2 stage bundles."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import datetime as dt
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_v5_1_stage_preflight import (
    V5_2_EXTRACTION_CONTRACT,
    V5_2_REGISTRY_VERSION,
    V5_2_RESOLUTION_CONTRACT,
    _schema_hash,
    _validate_extraction_packet,
    _validate_resolution_packet,
)


MANIFEST_CONTRACT = "memory_v1_v5_2_stage_batch_manifest_v1"
PLAN_CONTRACT = "memory_v1_v5_2_stage_batch_plan_v1"
AUTHORIZATION_CONTRACT = "memory_v1_v5_2_stage_batch_authorization_v1"
REPORT_CONTRACT = "memory_v1_v5_2_stage_batch_apply_report_v1"
BUNDLE_CONTRACT = "memory_v1_v5_2_stage_preflight_v1"
CONFIRMATION = "STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY"
DEFAULT_REVIEW_ROOT = "/home/ubuntu/memory-v1-reviews"
MAX_BUNDLES = 25
MAX_NEW_ROWS = 1500
COUNT_KEYS = (
    "mentions",
    "resolutions",
    "candidates",
    "observations",
    "temporals",
)
BUNDLE_KEYS = {
    "authorized_stage",
    "case_id",
    "contract_version",
    "database_writes",
    "evidence_id",
    "external_model_calls",
    "extraction_packet_sha256",
    "extraction_packet_text",
    "extractor",
    "extractor_version",
    "generated_at",
    "mode",
    "owner_user_id",
    "qdrant_writes",
    "request_id",
    "resolution_packet_sha256",
    "resolution_packet_text",
    "resolution_summary",
    "schemas",
    "server",
    "source_report",
}
SOURCE_KEYS = {
    "job_id",
    "source_system",
    "source_external_id",
    "source_sha256",
    "source_recorded_at",
}


class StageBatchError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or apply one reviewed owner-scoped V5.2 staging batch."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--manifest", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--review-root", default=DEFAULT_REVIEW_ROOT)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--authorization", required=True)
    apply.add_argument("--output", required=True)
    apply.add_argument("--review-root", default=DEFAULT_REVIEW_ROOT)
    apply.add_argument("--confirm", required=True)
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_valid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def parse_utc(value: Any, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise StageBatchError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise StageBatchError(f"{field} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def repository_state() -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[1]
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise StageBatchError("stage batch operation requires a clean Git worktree")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, head


def secure_input(path_value: str, *, root: Path | None = None) -> Path:
    path = Path(path_value).resolve(strict=True)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise StageBatchError(f"input must be a mode-0600 regular file: {path}")
    if root is not None and not path.is_relative_to(root):
        raise StageBatchError(f"input is outside the approved review root: {path}")
    return path


def review_root(path_value: str) -> Path:
    root = Path(path_value).resolve(strict=True)
    if not root.is_dir() or stat.S_IMODE(root.stat().st_mode) & 0o022:
        raise StageBatchError("review root must be a non-writable directory for peers")
    return root


def secure_write(path_value: str, value: dict[str, Any]) -> tuple[Path, str]:
    path = Path(path_value).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path, sha256_bytes(payload)


def exact_object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise StageBatchError(f"{field} fields do not match the contract")
    return value


@lru_cache(maxsize=1)
def expected_schema_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    return {
        "extraction_sha256": _schema_hash(
            root / "specs/memory_v1_relational_extraction_v5_2.schema.json"
        ),
        "resolution_sha256": _schema_hash(
            root / "specs/memory_v1_entity_resolution_review_v5_2.schema.json"
        ),
    }


def validate_packet_integrity(
    extraction: dict[str, Any], resolution: dict[str, Any]
) -> None:
    try:
        _validate_extraction_packet(
            extraction,
            extraction_contract=V5_2_EXTRACTION_CONTRACT,
            registry_version=V5_2_REGISTRY_VERSION,
        )
        _validate_resolution_packet(
            resolution,
            extraction,
            resolution_contract=V5_2_RESOLUTION_CONTRACT,
            registry_version=V5_2_REGISTRY_VERSION,
        )
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        raise StageBatchError(f"bundle packet validation failed: {exc}") from exc
    resolution_body = {
        key: value
        for key, value in resolution.items()
        if key != "packet_sha256"
    }
    if resolution["packet_sha256"] != sha256_text(stable_json(resolution_body)):
        raise StageBatchError("resolution packet internal hash mismatch")


def validated_resolution_summary(
    value: Any, resolution: dict[str, Any]
) -> dict[str, int]:
    summary = exact_object(
        value,
        {"auto_link_eligible", "manual_review_required", "deferred", "rejected"},
        "resolution_summary",
    )
    if any(
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for count in summary.values()
    ):
        raise StageBatchError("resolution summary counts are invalid")
    states = Counter(item["decision_state"] for item in resolution["resolutions"])
    unknown = set(states) - set(summary)
    if unknown:
        raise StageBatchError("resolution summary contains an unknown decision state")
    expected = {key: states.get(key, 0) for key in summary}
    if summary != expected:
        raise StageBatchError("resolution summary differs from resolution packet")
    return summary



def packet_source(packet: dict[str, Any], field: str) -> dict[str, Any]:
    source = exact_object(packet.get("source_envelope"), SOURCE_KEYS, field)
    if source["source_system"] != "public.chat_log":
        raise StageBatchError(f"{field} source system is not public.chat_log")
    try:
        uuid.UUID(str(source["source_external_id"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise StageBatchError(f"{field} source external ID must be a UUID") from exc
    if not sha256_valid(source["source_sha256"]):
        raise StageBatchError(f"{field} source SHA-256 is invalid")
    parse_utc(source["source_recorded_at"], f"{field}.source_recorded_at")
    return source


def structural_counts(
    extraction: dict[str, Any], resolution: dict[str, Any]
) -> dict[str, int]:
    mentions = extraction.get("entity_mentions")
    observations = extraction.get("observations")
    resolutions = resolution.get("resolutions")
    if not isinstance(mentions, list) or len(mentions) > 24:
        raise StageBatchError("entity_mentions must be an array of at most 24")
    if not isinstance(observations, list) or len(observations) > 32:
        raise StageBatchError("observations must be an array of at most 32")
    if not isinstance(resolutions, list) or len(resolutions) != len(mentions):
        raise StageBatchError("each entity mention requires one resolution")
    candidates = 0
    mention_refs = {
        mention.get("entity_ref") for mention in mentions if isinstance(mention, dict)
    }
    resolution_refs: set[Any] = set()
    for item in resolutions:
        if not isinstance(item, dict) or not isinstance(item.get("candidate_set"), list):
            raise StageBatchError("resolution candidate_set must be an array")
        candidates += len(item["candidate_set"])
        resolution_refs.add(item.get("entity_ref"))
    if (
        len(mention_refs) != len(mentions)
        or len(resolution_refs) != len(resolutions)
        or mention_refs != resolution_refs
    ):
        raise StageBatchError("entity mention and resolution references do not match")
    return {
        "mentions": len(mentions),
        "resolutions": len(resolutions),
        "candidates": candidates,
        "observations": len(observations),
        "temporals": len(observations),
    }


def load_bundle(
    spec: dict[str, Any], *, owner: uuid.UUID, root: Path
) -> dict[str, Any]:
    exact_object(
        spec,
        {"path", "sha256", "expected_outcome", "expected_counts"},
        "bundle manifest entry",
    )
    path = secure_input(str(spec["path"]), root=root)
    if not sha256_valid(spec["sha256"]) or sha256_file(path) != spec["sha256"]:
        raise StageBatchError(f"bundle SHA-256 mismatch: {path}")
    expected_outcome = spec["expected_outcome"]
    if expected_outcome not in {"applied", "replayed"}:
        raise StageBatchError("bundle expected_outcome must be applied or replayed")
    expected_counts = exact_object(
        spec["expected_counts"], set(COUNT_KEYS), "bundle expected_counts"
    )
    if any(
        not isinstance(expected_counts[key], int)
        or isinstance(expected_counts[key], bool)
        or not 0 <= expected_counts[key] <= 1000
        for key in COUNT_KEYS
    ):
        raise StageBatchError("bundle expected counts are invalid")

    raw = path.read_bytes()
    value = exact_object(json.loads(raw), BUNDLE_KEYS, "stage bundle")
    if (
        value["contract_version"] != BUNDLE_CONTRACT
        or value["mode"] != "preflight_only_zero_write"
        or value["server"] != "seebx"
        or value["authorized_stage"] is not False
        or any(
            value[key] != 0
            for key in ("database_writes", "qdrant_writes", "external_model_calls")
        )
    ):
        raise StageBatchError(f"bundle is not a zero-write V5.2 preflight: {path}")
    if value["owner_user_id"] != str(owner):
        raise StageBatchError(f"bundle owner differs from manifest owner: {path}")
    if not isinstance(value["case_id"], str) or not 1 <= len(value["case_id"]) <= 120:
        raise StageBatchError("bundle case_id is invalid")
    request_id = uuid.UUID(str(value["request_id"]))
    evidence_id = uuid.UUID(str(value["evidence_id"]))
    if (
        not isinstance(value["extractor"], str)
        or not 1 <= len(value["extractor"].strip()) <= 120
        or not isinstance(value["extractor_version"], str)
        or not 1 <= len(value["extractor_version"].strip()) <= 120
    ):
        raise StageBatchError("bundle extractor identity is invalid")
    for text_key, hash_key in (
        ("extraction_packet_text", "extraction_packet_sha256"),
        ("resolution_packet_text", "resolution_packet_sha256"),
    ):
        text = value[text_key]
        if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
            raise StageBatchError(f"{text_key} is invalid")
        if not sha256_valid(value[hash_key]) or sha256_text(text) != value[hash_key]:
            raise StageBatchError(f"{text_key} hash mismatch")
    extraction = json.loads(value["extraction_packet_text"])
    resolution = json.loads(value["resolution_packet_text"])
    if (
        not isinstance(extraction, dict)
        or extraction.get("contract_version") != "memory_v1_relational_extraction_v5_2"
        or extraction.get("predicate_registry_version")
        != "memory_predicate_registry_v5_2"
        or not isinstance(resolution, dict)
        or resolution.get("contract_version")
        != "memory_v1_entity_resolution_review_v5_2"
        or resolution.get("predicate_registry_version")
        != "memory_predicate_registry_v5_2"
    ):
        raise StageBatchError("bundle packet contract is invalid")
    validate_packet_integrity(extraction, resolution)
    source = packet_source(extraction, "extraction source")
    if packet_source(resolution, "resolution source") != source:
        raise StageBatchError("extraction and resolution sources differ")
    derived_counts = structural_counts(extraction, resolution)
    if expected_counts != derived_counts:
        raise StageBatchError(
            f"bundle expected counts differ from packet structure: {path}"
        )
    validated_resolution_summary(value["resolution_summary"], resolution)
    schemas = exact_object(
        value["schemas"], {"extraction_sha256", "resolution_sha256"}, "schemas"
    )
    if schemas != expected_schema_hashes():
        raise StageBatchError("bundle schemas differ from checked-in contracts")
    source_report = exact_object(
        value["source_report"], {"path", "sha256"}, "source_report"
    )
    if not sha256_valid(source_report["sha256"]):
        raise StageBatchError("source report SHA-256 is invalid")
    source_report_path = secure_input(source_report["path"], root=root)
    if sha256_file(source_report_path) != source_report["sha256"]:
        raise StageBatchError("source report content hash mismatch")
    return {
        "path": str(path),
        "sha256": spec["sha256"],
        "case_id": value["case_id"],
        "owner_user_id": owner,
        "request_id": request_id,
        "evidence_id": evidence_id,
        "extractor": value["extractor"].strip(),
        "extractor_version": value["extractor_version"].strip(),
        "extraction_packet_text": value["extraction_packet_text"],
        "resolution_packet_text": value["resolution_packet_text"],
        "extraction_packet_sha256": value["extraction_packet_sha256"],
        "resolution_packet_sha256": value["resolution_packet_sha256"],
        "source": source,
        "expected_outcome": expected_outcome,
        "expected_counts": derived_counts,
    }


def load_manifest(
    path_value: str, *, root: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], Path, str]:
    path = secure_input(path_value, root=root)
    raw = path.read_bytes()
    value = exact_object(
        json.loads(raw),
        {"contract_version", "target_server", "owner_user_id", "bundles"},
        "stage batch manifest",
    )
    if (
        value["contract_version"] != MANIFEST_CONTRACT
        or value["target_server"] != "seebx"
    ):
        raise StageBatchError("stage batch manifest contract or server is invalid")
    owner = uuid.UUID(str(value["owner_user_id"]))
    specs = value["bundles"]
    if not isinstance(specs, list) or not 1 <= len(specs) <= MAX_BUNDLES:
        raise StageBatchError(f"manifest must contain 1 to {MAX_BUNDLES} bundles")
    bundles = [load_bundle(spec, owner=owner, root=root) for spec in specs]
    bundles.sort(key=lambda bundle: str(bundle["request_id"]))
    for field in ("request_id", "case_id", "path", "sha256"):
        values = [str(bundle[field]) for bundle in bundles]
        if len(values) != len(set(values)):
            raise StageBatchError(f"manifest contains duplicate bundle {field}")
    expected_new_rows = sum(
        sum(bundle["expected_counts"].values()) + 2
        for bundle in bundles
        if bundle["expected_outcome"] == "applied"
    )
    if expected_new_rows > MAX_NEW_ROWS:
        raise StageBatchError("manifest exceeds the new-row budget")
    metadata = {
        "owner_user_id": owner,
        "bundle_count": len(bundles),
        "expected_new_rows": expected_new_rows,
    }
    return metadata, bundles, path, sha256_bytes(raw)


def bundle_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": bundle["path"],
        "sha256": bundle["sha256"],
        "case_id": bundle["case_id"],
        "evidence_id": str(bundle["evidence_id"]),
        "request_id": str(bundle["request_id"]),
        "expected_outcome": bundle["expected_outcome"],
        "expected_counts": bundle["expected_counts"],
    }


async def verify_evidence(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    bundles: list[dict[str, Any]],
) -> None:
    if await conn.fetchval("SELECT session_user") != "brains_app":
        raise StageBatchError("POSTGRES_DSN must authenticate as brains_app")
    if not await conn.fetchval(
        """
        SELECT has_function_privilege(
          session_user,
          'memory.stage_relational_packet_v5_2(uuid,uuid,text,text,text,text,text,text)',
          'EXECUTE'
        )
        """
    ):
        raise StageBatchError("brains_app cannot execute the controlled stage API")
    if not await conn.fetchval(
        """
        SELECT has_function_privilege(
          session_user,
          'memory.preflight_relational_stage_bundle_v5_2(uuid,text,text,timestamptz)',
          'EXECUTE'
        )
        """
    ):
        raise StageBatchError(
            "brains_app cannot execute the controlled stage preflight API"
        )
    if await conn.fetchval(
        """
        SELECT has_table_privilege(
          session_user,'memory.relational_stage_batch','SELECT'
        )
        """
    ):
        raise StageBatchError("brains_app unexpectedly has direct stage-table access")
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        for bundle in bundles:
            row = await conn.fetchrow(
                """
                SELECT evidence_id,verified
                FROM memory.preflight_relational_stage_bundle_v5_2(
                  $1::uuid,$2,$3,$4::timestamptz
                )
                """,
                bundle["evidence_id"],
                bundle["source"]["source_external_id"],
                bundle["source"]["source_sha256"],
                parse_utc(
                    bundle["source"]["source_recorded_at"],
                    "source_recorded_at",
                ),
            )
            if (
                row is None
                or row["evidence_id"] != bundle["evidence_id"]
                or row["verified"] is not True
            ):
                raise StageBatchError(
                    f"active owner-scoped evidence mismatch: {bundle['case_id']}"
                )


async def create_plan(args: argparse.Namespace) -> dict[str, Any]:
    _, head = repository_state()
    root = review_root(args.review_root)
    metadata, bundles, manifest_path, manifest_sha = load_manifest(
        args.manifest, root=root
    )
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise StageBatchError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        await verify_evidence(conn, metadata["owner_user_id"], bundles)
    finally:
        await conn.close()
    return {
        "contract_version": PLAN_CONTRACT,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "preflight_only_zero_write",
        "target_server": "seebx",
        "owner_user_id": str(metadata["owner_user_id"]),
        "required_head_commit": head,
        "review_root": str(root),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "bundle_count": metadata["bundle_count"],
        "expected_new_rows": metadata["expected_new_rows"],
        "bundles": [bundle_summary(bundle) for bundle in bundles],
        "apply_authorized": False,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


def load_plan(
    path_value: str, *, root: Path, head: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    path = secure_input(path_value)
    raw = path.read_bytes()
    plan = exact_object(
        json.loads(raw),
        {
            "contract_version",
            "generated_at",
            "mode",
            "target_server",
            "owner_user_id",
            "required_head_commit",
            "review_root",
            "manifest_path",
            "manifest_sha256",
            "bundle_count",
            "expected_new_rows",
            "bundles",
            "apply_authorized",
            "database_writes",
            "qdrant_writes",
            "external_model_calls",
        },
        "stage batch plan",
    )
    if (
        plan["contract_version"] != PLAN_CONTRACT
        or plan["mode"] != "preflight_only_zero_write"
        or plan["target_server"] != "seebx"
        or plan["required_head_commit"] != head
        or plan["review_root"] != str(root)
        or plan["apply_authorized"] is not False
        or any(
            plan[key] != 0
            for key in ("database_writes", "qdrant_writes", "external_model_calls")
        )
    ):
        raise StageBatchError("stage batch plan is stale or invalid")
    metadata, bundles, manifest_path, manifest_sha = load_manifest(
        plan["manifest_path"], root=root
    )
    if (
        manifest_sha != plan["manifest_sha256"]
        or str(manifest_path) != plan["manifest_path"]
        or str(metadata["owner_user_id"]) != plan["owner_user_id"]
        or metadata["bundle_count"] != plan["bundle_count"]
        or metadata["expected_new_rows"] != plan["expected_new_rows"]
        or [bundle_summary(bundle) for bundle in bundles] != plan["bundles"]
    ):
        raise StageBatchError("manifest or bundles changed after plan creation")
    return plan, bundles, sha256_bytes(raw)


def load_authorization(
    path_value: str, *, plan: dict[str, Any], plan_sha: str, head: str
) -> tuple[dict[str, Any], str]:
    path = secure_input(path_value)
    raw = path.read_bytes()
    value = exact_object(
        json.loads(raw),
        {
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
            "plan_sha256",
            "expected_bundle_count",
            "expected_new_rows",
            "confirmation",
        },
        "stage batch authorization",
    )
    if (
        value["contract_version"] != AUTHORIZATION_CONTRACT
        or value["authorized"] is not True
        or value["authorized_by"] != "Eric Lund"
        or value["expected_head_commit"] != head
        or value["target_server"] != "seebx"
        or value["scope"] != "stage_reviewed_owner_v5_2_packets_only"
        or value["owner_user_id"] != plan["owner_user_id"]
        or value["plan_sha256"] != plan_sha
        or value["expected_bundle_count"] != plan["bundle_count"]
        or value["expected_new_rows"] != plan["expected_new_rows"]
        or value["confirmation"] != CONFIRMATION
    ):
        raise StageBatchError("stage batch authorization is invalid")
    uuid.UUID(str(value["authorization_id"]))
    authorized_at = parse_utc(value["authorized_at"], "authorized_at")
    expires_at = parse_utc(value["expires_at"], "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    if (
        expires_at <= authorized_at
        or expires_at - authorized_at > dt.timedelta(minutes=30)
        or now < authorized_at - dt.timedelta(seconds=30)
        or now >= expires_at
    ):
        raise StageBatchError("stage batch authorization is expired or overbroad")
    return value, sha256_bytes(raw)


async def call_stage(
    conn: asyncpg.Connection, bundle: dict[str, Any]
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT batch_id,outcome,mentions_inserted,resolutions_inserted,
               candidates_inserted,observations_inserted,temporals_inserted,result
        FROM memory.stage_relational_packet_v5_2(
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
        raise StageBatchError("controlled stage API returned no row")
    return dict(row)


def returned_counts(row: dict[str, Any]) -> dict[str, int]:
    return {
        "mentions": int(row["mentions_inserted"]),
        "resolutions": int(row["resolutions_inserted"]),
        "candidates": int(row["candidates_inserted"]),
        "observations": int(row["observations_inserted"]),
        "temporals": int(row["temporals_inserted"]),
    }


async def apply_once(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    bundles: list[dict[str, Any]],
    *,
    replay: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"{owner}|relational_stage_v5_2",
        )
        for bundle in bundles:
            row = await call_stage(conn, bundle)
            counts = returned_counts(row)
            expected_outcome = "replayed" if replay else bundle["expected_outcome"]
            expected_counts = (
                {key: 0 for key in COUNT_KEYS}
                if expected_outcome == "replayed"
                else bundle["expected_counts"]
            )
            if row["outcome"] != expected_outcome or counts != expected_counts:
                raise StageBatchError(
                    f"stage outcome/count mismatch: {bundle['case_id']}"
                )
            results.append(
                {
                    "case_id": bundle["case_id"],
                    "request_id": str(bundle["request_id"]),
                    "evidence_id": str(bundle["evidence_id"]),
                    "batch_id": str(row["batch_id"]),
                    "outcome": row["outcome"],
                    "counts": counts,
                }
            )
    return results


async def apply_plan(args: argparse.Namespace) -> dict[str, Any]:
    if os.getenv("MEMORY_V1_V5_2_STAGE_BATCH_APPLY") != "authorized":
        raise StageBatchError(
            "apply requires MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized"
        )
    if args.confirm != CONFIRMATION:
        raise StageBatchError(f"--confirm must equal {CONFIRMATION}")
    _, head = repository_state()
    root = review_root(args.review_root)
    plan, bundles, plan_sha = load_plan(args.plan, root=root, head=head)
    authorization, authorization_sha = load_authorization(
        args.authorization, plan=plan, plan_sha=plan_sha, head=head
    )
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise StageBatchError("POSTGRES_DSN is required")
    owner = uuid.UUID(plan["owner_user_id"])
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        await verify_evidence(conn, owner, bundles)
        applied = await apply_once(conn, owner, bundles, replay=False)
        replayed = await apply_once(conn, owner, bundles, replay=True)
    finally:
        await conn.close()
    return {
        "contract_version": REPORT_CONTRACT,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "transactional_apply_and_zero_write_replay",
        "target_server": "seebx",
        "owner_user_id": str(owner),
        "head_commit": head,
        "plan_sha256": plan_sha,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": authorization_sha,
        "bundle_count": plan["bundle_count"],
        "database_rows_created": plan["expected_new_rows"],
        "applied": applied,
        "replayed": replayed,
        "checks": {
            "one_owner_per_transaction": True,
            "owner_advisory_lock": True,
            "atomic_batch": True,
            "exact_row_budget": True,
            "replay_rows_written": 0,
            "external_model_calls": 0,
            "qdrant_calls": 0,
            "entity_resolution_apply_invoked": False,
            "projection_invoked": False,
            "retrieval_invoked": False,
            "prompt_influence": False,
        },
        "hard_stop": "before_entity_resolution_review_or_apply",
    }


async def async_main() -> int:
    args = arguments()
    if args.command == "plan":
        value = await create_plan(args)
    else:
        value = await apply_plan(args)
    output, digest = secure_write(args.output, value)
    print(
        stable_json(
            {
                "contract_version": value["contract_version"],
                "mode": value["mode"],
                "output": str(output),
                "sha256": digest,
                "owner_user_id": value["owner_user_id"],
                "bundle_count": value["bundle_count"],
                "database_writes": value.get("database_rows_created", 0),
                "qdrant_calls": 0,
                "external_model_calls": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main()))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}:{exc}", file=sys.stderr)
        raise SystemExit(1)
