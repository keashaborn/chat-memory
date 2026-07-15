#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import asyncpg
from openai import OpenAI
from qdrant_client.http import models as qmodels

from rag_engine.memory_v1_consolidation import (
    EXTRACTOR_VERSION,
    ConsolidationError,
    ExtractedCandidate,
    _normalize_sensitivity_candidate,
    _normalize_temporal_candidate,
    _normalize_vocabulary_candidate,
    _validate_candidate,
    extract_with_openai,
    looks_like_artifact,
)
from rag_engine.qdrant_compat import make_qdrant_client


MANIFEST_VERSION = "memory_v1_consolidation_packet_eval_v1"
DEFAULT_COLLECTION = "memory_claim_v1"
SOURCE_SYSTEM = "public.chat_log"
SOURCE_TYPE = "frontend/chat:user"
MAX_PACKET_SIZE = 30
IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_rows(values: Iterable[str]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def secure_write_json(path: Path, value: Any) -> None:
    if not path.parent.is_dir():
        raise RuntimeError(f"output directory does not exist: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def parse_timestamp(value: str) -> datetime:
    raw = str(value).strip()
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise RuntimeError("manifest timestamp must be timezone-aware")
    return parsed


def validate_digest(value: str, field: str) -> str:
    digest = str(value).strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise RuntimeError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def validate_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "manifest_version",
        "owner_user_id",
        "source_pipeline_version",
        "evaluator_pipeline_version",
        "selection",
        "sources",
    }:
        raise RuntimeError("manifest keys do not match the packet-eval v1 contract")
    if payload["manifest_version"] != MANIFEST_VERSION:
        raise RuntimeError("unsupported manifest_version")
    owner = str(uuid.UUID(str(payload["owner_user_id"])))
    if payload["evaluator_pipeline_version"] != EXTRACTOR_VERSION:
        raise RuntimeError("manifest evaluator_pipeline_version mismatch")
    source_pipeline = str(payload["source_pipeline_version"]).strip()
    if not source_pipeline or len(source_pipeline) > 100:
        raise RuntimeError("invalid source_pipeline_version")

    selection = payload["selection"]
    if not isinstance(selection, dict) or set(selection) != {"algorithm", "limit"}:
        raise RuntimeError("invalid selection contract")
    if selection["algorithm"] != "newest_pending_source_time_desc_job_id_desc":
        raise RuntimeError("unsupported selection algorithm")
    limit = int(selection["limit"])
    if not 1 <= limit <= MAX_PACKET_SIZE:
        raise RuntimeError(f"selection limit must be between 1 and {MAX_PACKET_SIZE}")

    sources = payload["sources"]
    if not isinstance(sources, list) or len(sources) != limit:
        raise RuntimeError("manifest source count must equal selection limit")
    seen_jobs: set[str] = set()
    seen_sources: set[str] = set()
    previous_order: tuple[datetime, str] | None = None
    for item in sources:
        if not isinstance(item, dict) or set(item) != {
            "job_id",
            "source_external_id",
            "source_sha256",
            "source_recorded_at",
        }:
            raise RuntimeError("invalid manifest source row")
        job_id = str(uuid.UUID(str(item["job_id"])))
        source_id = str(uuid.UUID(str(item["source_external_id"])))
        if job_id in seen_jobs or source_id in seen_sources:
            raise RuntimeError("manifest contains a duplicate job or source")
        seen_jobs.add(job_id)
        seen_sources.add(source_id)
        validate_digest(item["source_sha256"], "source_sha256")
        recorded_at = parse_timestamp(item["source_recorded_at"])
        order_key = (recorded_at, job_id)
        if previous_order is not None and order_key >= previous_order:
            raise RuntimeError("manifest sources are not in strict newest-first order")
        previous_order = order_key
    return {
        **payload,
        "owner_user_id": owner,
        "source_pipeline_version": source_pipeline,
    }


def load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    payload = validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    return payload, sha256_text(stable_json(payload))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hash-lock and zero-write evaluate a Memory V1 consolidation packet."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-manifest")
    build.add_argument("--owner-user-id", required=True)
    build.add_argument("--source-pipeline-version", required=True)
    build.add_argument("--limit", type=int, default=25)
    build.add_argument("--output", required=True)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--manifest", required=True)
    evaluate.add_argument("--output", required=True)
    evaluate.add_argument(
        "--collection",
        default=os.getenv("MEMORY_V1_COLLECTION", DEFAULT_COLLECTION),
    )

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--manifest", required=True)
    preflight.add_argument("--output", required=True)
    preflight.add_argument(
        "--collection",
        default=os.getenv("MEMORY_V1_COLLECTION", DEFAULT_COLLECTION),
    )
    return parser.parse_args()


async def require_application_role(conn: asyncpg.Connection) -> None:
    current_user = await conn.fetchval("SELECT current_user")
    if current_user != "brains_app":
        raise RuntimeError(f"packet evaluator requires brains_app, got {current_user}")


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))


async def verify_forced_rls(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    rows = await conn.fetch(
        """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid=c.relnamespace
        WHERE (n.nspname,c.relname) IN (
          ('memory','consolidation_job'),
          ('public','chat_log')
        )
        ORDER BY n.nspname,c.relname
        """
    )
    if len(rows) != 2 or any(
        not row["relrowsecurity"] or not row["relforcerowsecurity"] for row in rows
    ):
        raise RuntimeError("consolidation_job and chat_log must use forced RLS")
    for relation in ("memory.consolidation_job", "public.chat_log"):
        foreign = await conn.fetchval(
            f"SELECT count(*) FROM {relation} WHERE owner_user_id <> $1", owner
        )
        if int(foreign) != 0:
            raise RuntimeError(f"cross-owner visibility detected in {relation}")


async def build_manifest(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    source_pipeline_version: str,
    limit: int,
) -> dict[str, Any]:
    if not 1 <= limit <= MAX_PACKET_SIZE:
        raise RuntimeError(f"limit must be between 1 and {MAX_PACKET_SIZE}")
    if not source_pipeline_version.strip() or len(source_pipeline_version) > 100:
        raise RuntimeError("invalid source_pipeline_version")
    async with conn.transaction(readonly=True, isolation="repeatable_read"):
        await set_actor(conn, owner)
        await verify_forced_rls(conn, owner)
        rows = await conn.fetch(
            """
            SELECT job.job_id, job.source_external_id, job.source_sha256,
                   job.source_recorded_at, job.attempts,
                   source.id AS source_id, source.text, source.created_at,
                   source.source AS source_type, source.owner_user_id AS source_owner
            FROM memory.consolidation_job AS job
            LEFT JOIN public.chat_log AS source
              ON source.owner_user_id=job.owner_user_id
             AND source.id::text=job.source_external_id
            WHERE job.owner_user_id=$1
              AND job.source_system=$2
              AND job.pipeline_version=$3
              AND job.status='pending'
            ORDER BY job.source_recorded_at DESC, job.job_id DESC
            LIMIT $4
            """,
            owner,
            SOURCE_SYSTEM,
            source_pipeline_version,
            limit,
        )
        if len(rows) != limit:
            raise RuntimeError(f"expected {limit} pending rows, found {len(rows)}")
        sources: list[dict[str, str]] = []
        for row in rows:
            if row["source_id"] is None or row["source_owner"] != owner:
                raise RuntimeError(f"owner-scoped source missing for job {row['job_id']}")
            if row["source_type"] != SOURCE_TYPE:
                raise RuntimeError(f"unexpected source type for job {row['job_id']}")
            if int(row["attempts"]) != 0:
                raise RuntimeError(f"packet contains attempted job {row['job_id']}")
            if row["source_recorded_at"] != row["created_at"]:
                raise RuntimeError(f"source timestamp mismatch for job {row['job_id']}")
            actual_sha = sha256_text(str(row["text"] or ""))
            if actual_sha != row["source_sha256"]:
                raise RuntimeError(f"source hash mismatch for job {row['job_id']}")
            sources.append(
                {
                    "job_id": str(row["job_id"]),
                    "source_external_id": str(row["source_id"]),
                    "source_sha256": actual_sha,
                    "source_recorded_at": row["created_at"].isoformat(),
                }
            )
    return validate_manifest(
        {
            "manifest_version": MANIFEST_VERSION,
            "owner_user_id": str(owner),
            "source_pipeline_version": source_pipeline_version,
            "evaluator_pipeline_version": EXTRACTOR_VERSION,
            "selection": {
                "algorithm": "newest_pending_source_time_desc_job_id_desc",
                "limit": limit,
            },
            "sources": sources,
        }
    )


async def verify_and_load_sources(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in manifest["sources"]:
        row = await conn.fetchrow(
            """
            SELECT job.job_id, job.owner_user_id, job.source_system,
                   job.source_external_id, job.source_sha256,
                   job.source_recorded_at, job.pipeline_version,
                   job.status::text AS status, job.attempts,
                   source.id AS source_id, source.text, source.created_at,
                   source.source AS source_type,
                   source.owner_user_id AS source_owner
            FROM memory.consolidation_job AS job
            LEFT JOIN public.chat_log AS source
              ON source.owner_user_id=job.owner_user_id
             AND source.id::text=job.source_external_id
            WHERE job.owner_user_id=$1 AND job.job_id=$2
            """,
            owner,
            uuid.UUID(item["job_id"]),
        )
        if row is None:
            raise RuntimeError(f"manifest job is not visible: {item['job_id']}")
        checks = {
            "source_system": row["source_system"] == SOURCE_SYSTEM,
            "source_external_id": row["source_external_id"]
            == item["source_external_id"],
            "source_sha256": row["source_sha256"] == item["source_sha256"],
            "pipeline_version": row["pipeline_version"]
            == manifest["source_pipeline_version"],
            "status": row["status"] == "pending",
            "attempts": int(row["attempts"]) == 0,
            "source_exists": row["source_id"] is not None,
            "source_owner": row["source_owner"] == owner,
            "source_type": row["source_type"] == SOURCE_TYPE,
            "recorded_at": row["source_recorded_at"]
            == parse_timestamp(item["source_recorded_at"]),
            "source_created_at": row["created_at"]
            == parse_timestamp(item["source_recorded_at"]),
        }
        failed = sorted(key for key, passed in checks.items() if not passed)
        if failed:
            raise RuntimeError(
                f"manifest job verification failed for {item['job_id']}: {','.join(failed)}"
            )
        actual_sha = sha256_text(str(row["text"] or ""))
        if actual_sha != item["source_sha256"]:
            raise RuntimeError(f"live source hash mismatch: {item['source_external_id']}")
        output.append(dict(row))
    return output


async def database_signature(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    source_ids: list[uuid.UUID],
) -> dict[str, Any]:
    relation_rows = await conn.fetch(
        """
        SELECT table_name,
               has_table_privilege(current_user, format('%I.%I', table_schema, table_name), 'SELECT') AS readable
        FROM information_schema.tables
        WHERE table_schema='memory' AND table_type='BASE TABLE'
        ORDER BY table_name
        """
    )
    unreadable = [row["table_name"] for row in relation_rows if not row["readable"]]
    if unreadable:
        raise RuntimeError(f"cannot prove zero-write for unreadable tables: {unreadable}")
    tables: dict[str, dict[str, Any]] = {}
    for relation in relation_rows:
        table_name = str(relation["table_name"])
        if not IDENTIFIER_RE.fullmatch(table_name):
            raise RuntimeError(f"unsafe memory table identifier: {table_name}")
        rows = await conn.fetch(
            f"SELECT to_jsonb(t)::text AS row_json FROM memory.{table_name} AS t "
            "ORDER BY to_jsonb(t)::text"
        )
        count, digest = digest_rows(str(row["row_json"]) for row in rows)
        tables[f"memory.{table_name}"] = {"rows": count, "sha256": digest}
    source_rows = await conn.fetch(
        """
        SELECT to_jsonb(t)::text AS row_json
        FROM public.chat_log AS t
        WHERE owner_user_id=$1 AND id=ANY($2::uuid[])
        ORDER BY id
        """,
        owner,
        source_ids,
    )
    count, digest = digest_rows(str(row["row_json"]) for row in source_rows)
    tables["public.chat_log:manifest_sources"] = {"rows": count, "sha256": digest}
    return {
        "relation_count": len(tables),
        "relations": tables,
        "sha256": sha256_text(stable_json(tables)),
    }


def qdrant_signature(client: Any, *, collection: str, owner: uuid.UUID) -> dict[str, Any]:
    owner_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="owner_user_id",
                match=qmodels.MatchValue(value=str(owner)),
            )
        ]
    )
    points: list[str] = []
    offset: Any = None
    while True:
        records, offset = client.scroll(
            collection_name=collection,
            scroll_filter=owner_filter,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for record in records:
            payload = record.payload or {}
            if str(payload.get("owner_user_id") or "") != str(owner):
                raise RuntimeError("Qdrant owner filter returned a foreign-owner point")
            points.append(
                stable_json(
                    {
                        "id": str(record.id),
                        "payload": payload,
                        "vector": record.vector,
                    }
                )
            )
        if offset is None:
            break
    points.sort()
    count, digest = digest_rows(points)
    exact_count = int(
        client.count(
            collection_name=collection,
            count_filter=owner_filter,
            exact=True,
        ).count
    )
    if exact_count != count:
        raise RuntimeError("Qdrant owner-filtered count and scroll disagree")
    return {"collection": collection, "points": count, "sha256": digest}


async def state_snapshot(
    *,
    dsn: str,
    qdrant: Any,
    collection: str,
    owner: uuid.UUID,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        await require_application_role(conn)
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            await set_actor(conn, owner)
            await verify_forced_rls(conn, owner)
            sources = await verify_and_load_sources(
                conn, owner=owner, manifest=manifest
            )
            database = await database_signature(
                conn,
                owner=owner,
                source_ids=[uuid.UUID(item["source_external_id"]) for item in manifest["sources"]],
            )
    finally:
        await conn.close()
    qdrant_state = qdrant_signature(qdrant, collection=collection, owner=owner)
    return {"database": database, "qdrant": qdrant_state}, sources


def normalize_candidates(
    candidates: list[ExtractedCandidate],
    *,
    source_created_at: datetime,
    source_text: str,
) -> tuple[list[ExtractedCandidate], list[dict[str, str]], list[str]]:
    valid: list[ExtractedCandidate] = []
    rejected: list[dict[str, str]] = []
    findings: list[str] = []
    identities: set[str] = set()
    for candidate in candidates:
        candidate = _normalize_vocabulary_candidate(candidate)
        candidate = _normalize_temporal_candidate(
            candidate, source_created_at, source_text
        )
        candidate = _normalize_sensitivity_candidate(candidate)
        try:
            _validate_candidate(candidate)
        except ConsolidationError as exc:
            rejected.append(
                {
                    "lane": candidate.lane,
                    "canonical_text": candidate.canonical_text,
                    "reason": str(exc),
                }
            )
            continue
        identity = sha256_text(
            stable_json(
                {
                    "lane": candidate.lane,
                    "subject_entity_key": candidate.subject_entity_key,
                    "predicate": candidate.predicate,
                    "object_literal": candidate.object_literal,
                    "preference_domain": candidate.preference_domain,
                    "preference_key": candidate.preference_key,
                    "project_key": candidate.project_key,
                    "canonical_text": candidate.canonical_text,
                }
            )
        )
        if identity in identities:
            findings.append("duplicate_normalized_candidate")
        identities.add(identity)
        valid.append(candidate)
    if rejected:
        findings.append("deterministic_validation_rejection")
    return valid, rejected, sorted(set(findings))


def zero_write_proof(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    database_unchanged = before["database"] == after["database"]
    qdrant_unchanged = before["qdrant"] == after["qdrant"]
    changed_relations = sorted(
        relation
        for relation in set(before["database"]["relations"])
        | set(after["database"]["relations"])
        if before["database"]["relations"].get(relation)
        != after["database"]["relations"].get(relation)
    )
    return {
        "passed": database_unchanged and qdrant_unchanged,
        "database_unchanged": database_unchanged,
        "qdrant_unchanged": qdrant_unchanged,
        "changed_relations": changed_relations,
        "before": {
            "database_sha256": before["database"]["sha256"],
            "database_relation_count": before["database"]["relation_count"],
            "qdrant_sha256": before["qdrant"]["sha256"],
            "qdrant_points": before["qdrant"]["points"],
        },
        "after": {
            "database_sha256": after["database"]["sha256"],
            "database_relation_count": after["database"]["relation_count"],
            "qdrant_sha256": after["qdrant"]["sha256"],
            "qdrant_points": after["qdrant"]["points"],
        },
    }


async def preflight_packet(
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    dsn: str,
    qdrant_url: str,
    collection: str,
) -> dict[str, Any]:
    owner = uuid.UUID(manifest["owner_user_id"])
    qdrant = make_qdrant_client(url=qdrant_url, timeout=30.0)
    try:
        before, sources = await state_snapshot(
            dsn=dsn,
            qdrant=qdrant,
            collection=collection,
            owner=owner,
            manifest=manifest,
        )
        after, _ = await state_snapshot(
            dsn=dsn,
            qdrant=qdrant,
            collection=collection,
            owner=owner,
            manifest=manifest,
        )
    finally:
        qdrant.close()
    proof = zero_write_proof(before, after)
    return {
        "mode": "local_zero_write_packet_preflight",
        "manifest_sha256": manifest_sha256,
        "owner_user_id": str(owner),
        "source_pipeline_version": manifest["source_pipeline_version"],
        "evaluator_pipeline_version": manifest["evaluator_pipeline_version"],
        "source_count": len(sources),
        "artifact_route_count": sum(
            int(looks_like_artifact(str(source["text"] or ""))) for source in sources
        ),
        "external_model_calls": 0,
        "zero_write_proof": proof,
    }


async def evaluate_packet(
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    dsn: str,
    qdrant_url: str,
    collection: str,
) -> dict[str, Any]:
    owner = uuid.UUID(manifest["owner_user_id"])
    qdrant = make_qdrant_client(url=qdrant_url, timeout=30.0)
    try:
        before, sources = await state_snapshot(
            dsn=dsn,
            qdrant=qdrant,
            collection=collection,
            owner=owner,
            manifest=manifest,
        )
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        model = (
            os.getenv("MEMORY_V1_CONSOLIDATION_MODEL")
            or os.getenv("VANTAGE_MODEL")
            or "gpt-5.2"
        ).strip()
        source_reports: list[dict[str, Any]] = []
        totals = {
            "artifact_routes": 0,
            "model_candidates": 0,
            "valid_candidates": 0,
            "validation_rejections": 0,
            "claim_candidates": 0,
            "preference_candidates": 0,
            "project_candidates": 0,
            "sources_with_findings": 0,
        }
        for source, manifest_source in zip(sources, manifest["sources"], strict=True):
            text = str(source["text"] or "")
            if looks_like_artifact(text):
                totals["artifact_routes"] += 1
                source_reports.append(
                    {
                        "job_id": manifest_source["job_id"],
                        "source_external_id": manifest_source["source_external_id"],
                        "source_sha256": manifest_source["source_sha256"],
                        "source_chars": len(text),
                        "route": "artifact_assessment",
                        "model_response_id": None,
                        "model_candidate_count": 0,
                        "valid_candidates": [],
                        "validation_rejections": [],
                        "quality_findings": [],
                    }
                )
                continue
            extraction, response_id = await asyncio.to_thread(
                extract_with_openai,
                client,
                model=model,
                owner_user_id=owner,
                source_external_id=manifest_source["source_external_id"],
                source_observed_at=source["created_at"],
                text=text,
            )
            valid, rejected, findings = normalize_candidates(
                extraction.candidates,
                source_created_at=source["created_at"],
                source_text=text,
            )
            totals["model_candidates"] += len(extraction.candidates)
            totals["valid_candidates"] += len(valid)
            totals["validation_rejections"] += len(rejected)
            totals["sources_with_findings"] += int(bool(findings))
            for candidate in valid:
                key = f"{candidate.lane}_candidates"
                if key in totals:
                    totals[key] += 1
            source_reports.append(
                {
                    "job_id": manifest_source["job_id"],
                    "source_external_id": manifest_source["source_external_id"],
                    "source_sha256": manifest_source["source_sha256"],
                    "source_chars": len(text),
                    "route": "structured_extraction",
                    "model_response_id": response_id,
                    "model_candidate_count": len(extraction.candidates),
                    "valid_candidates": [
                        candidate.model_dump(mode="json") for candidate in valid
                    ],
                    "validation_rejections": rejected,
                    "quality_findings": findings,
                }
            )
        after, _ = await state_snapshot(
            dsn=dsn,
            qdrant=qdrant,
            collection=collection,
            owner=owner,
            manifest=manifest,
        )
    finally:
        qdrant.close()

    proof = zero_write_proof(before, after)
    return {
        "mode": "zero_write_consolidation_packet_eval",
        "manifest_sha256": manifest_sha256,
        "owner_user_id": str(owner),
        "source_pipeline_version": manifest["source_pipeline_version"],
        "evaluator_pipeline_version": manifest["evaluator_pipeline_version"],
        "model": model,
        "totals": totals,
        "zero_write_proof": proof,
        "sources": source_reports,
    }


async def main() -> int:
    args = arguments()
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is not configured")
    if args.command == "build-manifest":
        owner = uuid.UUID(args.owner_user_id)
        conn = await asyncpg.connect(dsn, command_timeout=60)
        try:
            await require_application_role(conn)
            manifest = await build_manifest(
                conn,
                owner=owner,
                source_pipeline_version=args.source_pipeline_version,
                limit=args.limit,
            )
        finally:
            await conn.close()
        output = Path(args.output)
        secure_write_json(output, manifest)
        print(
            stable_json(
                {
                    "mode": "manifest_created",
                    "manifest": str(output),
                    "manifest_sha256": sha256_text(stable_json(manifest)),
                    "owner_user_id": str(owner),
                    "source_count": len(manifest["sources"]),
                    "source_pipeline_version": manifest["source_pipeline_version"],
                    "evaluator_pipeline_version": manifest["evaluator_pipeline_version"],
                }
            )
        )
        return 0

    manifest, manifest_sha256 = load_manifest(Path(args.manifest))
    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    if not qdrant_url:
        raise RuntimeError("QDRANT_URL is not configured")
    if args.command == "preflight":
        report = await preflight_packet(
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            dsn=dsn,
            qdrant_url=qdrant_url,
            collection=args.collection,
        )
    else:
        report = await evaluate_packet(
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            dsn=dsn,
            qdrant_url=qdrant_url,
            collection=args.collection,
        )
    output = Path(args.output)
    secure_write_json(output, report)
    print(
        stable_json(
            {
                "mode": report["mode"],
                "report": str(output),
                "manifest_sha256": manifest_sha256,
                "totals": report.get("totals"),
                "zero_write_proof": report["zero_write_proof"],
            }
        )
    )
    return 0 if report["zero_write_proof"]["passed"] else 3


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
