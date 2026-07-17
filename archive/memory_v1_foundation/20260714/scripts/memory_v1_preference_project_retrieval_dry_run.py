#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

import asyncpg

from rag_engine.memory_v1_preference_project_retrieval import (
    SpecializedRetrievalError,
    evaluate_specialized_memory,
    load_specialized_snapshot,
)


MANIFEST_VERSION = "memory_v1_preference_project_shadow_retrieval_20260714_v1"
OTHER_OWNER = "00000000-0000-4000-8000-000000000001"


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_object(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecializedRetrievalError(f"cannot read {field}: {path}") from exc
    if not isinstance(value, dict):
        raise SpecializedRetrievalError(f"{field} must be a JSON object")
    return value


def load_cases(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SpecializedRetrievalError(
                f"evaluation case line {number} is invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise SpecializedRetrievalError(
                f"evaluation case line {number} must be an object"
            )
        result.append(value)
    return result


def locked_records(
    rows: list[Mapping[str, Any]],
    *,
    id_field: str,
    key_field: str,
) -> list[dict[str, str]]:
    return sorted(
        [
            {
                id_field: str(row[id_field]),
                key_field: str(row[key_field]),
                "revision_id": str(row["revision_id"]),
                "content_sha256": str(row["content_sha256"]),
            }
            for row in rows
        ],
        key=lambda item: item[key_field],
    )


async def owner_counts(
    conn: asyncpg.Connection, owner: str, project_key: str
) -> dict[str, int]:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        row = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM memory.user_preference
               WHERE owner_user_id=$1) AS preference_records,
              (SELECT count(*)
               FROM memory.project_knowledge_head h
               JOIN memory.project_space s
                 ON s.owner_user_id=h.owner_user_id AND s.project_id=h.project_id
               WHERE h.owner_user_id=$1 AND s.project_key=$2) AS project_records,
              (SELECT count(*) FROM memory.retrieval_trace
               WHERE owner_user_id=$1) AS retrieval_traces,
              (SELECT count(*) FROM memory.projection_outbox
               WHERE owner_user_id=$1) AS projection_outbox
            """,
            uuid.UUID(owner),
            project_key,
        )
    return {key: int(value) for key, value in dict(row).items()}


def case_result(case: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    controls = [
        {
            "preference_key": item["preference_key"],
            "would_suppress": item["would_suppress"],
        }
        for item in result["policy_controls"]
    ]
    return {
        "case_id": case["case_id"],
        "policy_controls": controls,
        "preference_keys": [
            item["preference_key"] for item in result["selected_preferences"]
        ],
        "project_keys": [
            item["knowledge_key"] for item in result["selected_project_records"]
        ],
        "selected_content_count": result["selected_content_count"],
        "token_estimate": result["token_estimate"],
        "rejected_counts": result["rejected_counts"],
        "prompt_injection": result["prompt_injection"],
        "retrieval_activation": result["retrieval_activation"],
        "database_writes": result["database_writes"],
    }


def verify_case(case: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    control_keys = [item["preference_key"] for item in result["policy_controls"]]
    preference_keys = [
        item["preference_key"] for item in result["selected_preferences"]
    ]
    project_keys = [
        item["knowledge_key"] for item in result["selected_project_records"]
    ]
    if control_keys != case.get("expected_controls"):
        raise SpecializedRetrievalError(f"{case['case_id']} control result changed")
    if preference_keys != case.get("expected_preferences"):
        raise SpecializedRetrievalError(f"{case['case_id']} preference result changed")
    if project_keys != case.get("expected_project_records"):
        raise SpecializedRetrievalError(f"{case['case_id']} project result changed")
    if "expected_control_suppression" in case:
        if len(result["policy_controls"]) != 1 or bool(
            result["policy_controls"][0]["would_suppress"]
        ) != bool(case["expected_control_suppression"]):
            raise SpecializedRetrievalError(
                f"{case['case_id']} control suppression changed"
            )
    if result["prompt_injection"] or result["retrieval_activation"]:
        raise SpecializedRetrievalError(f"{case['case_id']} activated retrieval")
    if result["database_writes"] != 0:
        raise SpecializedRetrievalError(f"{case['case_id']} reported a write")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest).resolve()
    cases_path = Path(args.cases).resolve()
    source_manifest_path = Path(args.source_apply_manifest).resolve()
    source_report_path = Path(args.source_apply_report).resolve()
    manifest = load_object(manifest_path, "retrieval manifest")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise SpecializedRetrievalError("retrieval manifest version changed")
    if file_sha(source_manifest_path) != manifest.get("source_apply_manifest_sha256"):
        raise SpecializedRetrievalError("source apply manifest SHA-256 changed")
    if file_sha(source_report_path) != manifest.get("source_apply_report_sha256"):
        raise SpecializedRetrievalError("source apply report SHA-256 changed")
    source_report = load_object(source_report_path, "source apply report")
    if (
        source_report.get("status") != "committed"
        or source_report.get("database_writes") != 32
        or source_report.get("retrieval_activation") is not False
    ):
        raise SpecializedRetrievalError("source apply report contract changed")
    if file_sha(cases_path) != manifest.get("evaluation_cases_sha256"):
        raise SpecializedRetrievalError("evaluation case SHA-256 changed")
    cases = load_cases(cases_path)
    expected = manifest.get("expected")
    if not isinstance(expected, dict):
        raise SpecializedRetrievalError("manifest expected contract is missing")
    if len(cases) != int(expected.get("evaluation_cases") or -1):
        raise SpecializedRetrievalError("evaluation case count changed")

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise SpecializedRetrievalError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        owner = str(manifest["owner_user_id"])
        project_key = str(manifest["project_key"])
        before = await owner_counts(conn, owner, project_key)
        snapshot = await load_specialized_snapshot(
            conn, owner, project_key=project_key
        )
        if len(snapshot["preferences"]) != int(expected["preference_records"]):
            raise SpecializedRetrievalError("preference record count changed")
        if len(snapshot["project_records"]) != int(expected["project_records"]):
            raise SpecializedRetrievalError("project record count changed")
        actual_preferences = locked_records(
            snapshot["preferences"],
            id_field="preference_id",
            key_field="preference_key",
        )
        actual_projects = locked_records(
            snapshot["project_records"],
            id_field="knowledge_id",
            key_field="knowledge_key",
        )
        if actual_preferences != sorted(
            manifest["preference_records"], key=lambda item: item["preference_key"]
        ):
            raise SpecializedRetrievalError("locked preference snapshot changed")
        if actual_projects != sorted(
            manifest["project_records"], key=lambda item: item["knowledge_key"]
        ):
            raise SpecializedRetrievalError("locked project snapshot changed")

        results: list[dict[str, Any]] = []
        for case in cases:
            evaluated = evaluate_specialized_memory(
                preferences=snapshot["preferences"],
                project_records=snapshot["project_records"],
                query=case["query"],
                memory_intent=case["memory_intent"],
                domains=case["domains"],
                project_key=case["project_key"],
                candidate_entities=case["candidate_entities"],
                direct_relevance=case["direct_relevance"],
                max_sensitivity=case["max_sensitivity"],
                as_of=manifest["evaluation_time"],
            )
            verify_case(case, evaluated)
            results.append(case_result(case, evaluated))

        after = await owner_counts(conn, owner, project_key)
        if after != before:
            raise SpecializedRetrievalError("production counts changed during dry run")
        other_snapshot = await load_specialized_snapshot(
            conn, OTHER_OWNER, project_key=project_key
        )
        if other_snapshot["preferences"] or other_snapshot["project_records"]:
            raise SpecializedRetrievalError("cross-owner specialized rows were visible")
        other_counts = await owner_counts(conn, OTHER_OWNER, project_key)
        if any(other_counts.values()):
            raise SpecializedRetrievalError("cross-owner derived rows were visible")

        return {
            "status": "dry_run_verified",
            "manifest_version": MANIFEST_VERSION,
            "manifest_sha256": file_sha(manifest_path),
            "evaluation_cases_sha256": file_sha(cases_path),
            "source_apply_report_sha256": file_sha(source_report_path),
            "owner_user_id": owner,
            "project_id": manifest["project_id"],
            "project_key": project_key,
            "record_counts": {
                "preferences": len(snapshot["preferences"]),
                "project_records": len(snapshot["project_records"]),
            },
            "production_counts_before": before,
            "production_counts_after": after,
            "other_owner_counts": other_counts,
            "controls": snapshot["controls"],
            "evaluation_case_count": len(results),
            "cases": results,
            "database_writes": 0,
            "qdrant_writes": 0,
            "retrieval_traces_written": 0,
            "prompt_injection": False,
            "retrieval_activation": False,
        }
    finally:
        await conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Memory V1 preference/project retrieval dry run"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--source-apply-manifest", required=True)
    parser.add_argument("--source-apply-report", required=True)
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = asyncio.run(run(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
