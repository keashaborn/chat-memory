#!/usr/bin/env python3
"""Derive one reviewed, trusted-scope V5 stage bundle from an immutable packet."""

from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_relational_extraction_v5_provider import (
    NormalizedPacket,
    _project_name_key,
    canonical_sha256,
)
from scripts.memory_v1_v5_stage_preflight import (
    REQUEST_NAMESPACE,
    RESOLVER,
    RESOLVER_VERSION,
    _candidates,
    _schema_hash,
    _validate_extraction_packet,
    _validate_resolution_packet,
    _verify_source,
    resolve_mention,
    sha256_file,
    sha256_text,
    stable_json,
)


REVIEW_CONTRACT = "memory_v1_v5_extraction_packet_review_v1"
BUNDLE_CONTRACT = "memory_v1_v5_stage_preflight_v1"
REVIEW_NAMESPACE = uuid.UUID("c4272acf-e550-5a28-99f9-c9341d1ba55f")
DEFAULT_REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")


class PacketReviewError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve one immutable V5 packet through the trusted project/component "
            "registry and emit a zero-write reviewed stage bundle."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--packet-id", required=True)
    parser.add_argument("--expected-project-key", required=True)
    parser.add_argument("--expected-component-key", required=True)
    parser.add_argument("--review-report", required=True)
    parser.add_argument("--stage-bundle", required=True)
    parser.add_argument(
        "--extraction-schema",
        default="specs/memory_v1_relational_extraction_v5.schema.json",
    )
    parser.add_argument(
        "--resolution-schema",
        default="specs/memory_v1_entity_resolution_review_v5.schema.json",
    )
    parser.add_argument("--review-root", default=str(DEFAULT_REVIEW_ROOT))
    return parser.parse_args()


def _json_value(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise PacketReviewError(f"{label} is not a JSON object")
    return value


def _sha256_valid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _secure_root(path_value: str) -> Path:
    root = Path(path_value).resolve(strict=True)
    mode = stat.S_IMODE(root.stat().st_mode)
    if not root.is_dir() or mode & 0o022:
        raise PacketReviewError("review root must not be writable by group or peers")
    return root


def _output_path(path_value: str, root: Path) -> Path:
    path = Path(path_value).resolve()
    if not path.is_relative_to(root):
        raise PacketReviewError(f"output is outside the review root: {path}")
    if path.exists():
        raise PacketReviewError(f"immutable output already exists: {path}")
    return path


def _secure_write(path: Path, value: dict[str, Any]) -> str:
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
    return hashlib.sha256(payload).hexdigest()


def _repository_state() -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[1]
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise PacketReviewError("packet review requires a clean Git worktree")
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, commit


def _resolve_project_mention(
    mention: dict[str, Any],
    *,
    project_key: str,
    components: list[dict[str, Any]],
) -> tuple[str | None, str] | None:
    if mention.get("entity_type") != "project":
        return None
    if (
        mention.get("mention_kind") == "anonymous"
        and mention.get("name_text") is None
        and mention.get("relationship_role") == "project:current_thread"
    ):
        return None, "trusted_thread_binding"
    name = mention.get("name_text")
    if mention.get("mention_kind") != "named" or not isinstance(name, str):
        return None
    key = _project_name_key(name)
    if key == _project_name_key(project_key):
        return None, "trusted_thread_binding"
    matches = [
        component
        for component in components
        if key in component["aliases"]
    ]
    if len(matches) != 1:
        return None
    return matches[0]["component_key"], "trusted_component_registry"


def resolve_packet_project_scopes(
    packet: dict[str, Any],
    *,
    project_key: str,
    components: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    derived = copy.deepcopy(packet)
    mentions = {
        mention["entity_ref"]: mention
        for mention in derived["entity_mentions"]
    }
    transformations: list[dict[str, Any]] = []
    project_observations = [
        observation
        for observation in derived["observations"]
        if observation["projection_class"] == "project_knowledge"
    ]
    if not project_observations:
        raise PacketReviewError("packet has no project_knowledge observation")

    for observation in project_observations:
        mention = mentions.get(observation["subject_entity_ref"])
        if mention is None:
            raise PacketReviewError("project observation subject mention is absent")
        resolution = _resolve_project_mention(
            mention,
            project_key=project_key,
            components=components,
        )
        if resolution is None:
            raise PacketReviewError("project mention is not uniquely registry-resolved")
        resolved_scope = {
            "state": "resolved",
            "project_key": project_key,
            "component_key": resolution[0],
            "binding_source": resolution[1],
        }
        prior_scope = observation["project_scope"]
        if prior_scope["state"] not in {"unresolved", "resolved"}:
            raise PacketReviewError("project observation has an invalid prior scope")
        if prior_scope["state"] == "resolved" and prior_scope != resolved_scope:
            raise PacketReviewError("existing project scope conflicts with registry")
        observation["project_scope"] = resolved_scope
        transformations.append(
            {
                "kind": "resolve_project_scope",
                "observation_ref": observation["observation_ref"],
                "entity_ref": mention["entity_ref"],
                "prior_scope_sha256": canonical_sha256(prior_scope),
                "resolved_scope": resolved_scope,
            }
        )

    if not all(
        item["project_scope"]["state"] == "resolved"
        for item in project_observations
    ):
        raise PacketReviewError("not every project observation was resolved")

    retained_deferrals = []
    for deferral in derived["deferrals"]:
        if (
            deferral["reason_code"] == "project_scope_unresolved"
            and deferral["memory_shape"] == "project_knowledge"
        ):
            transformations.append(
                {
                    "kind": "discharge_deferral",
                    "reason_code": deferral["reason_code"],
                    "deferral_sha256": canonical_sha256(deferral),
                }
            )
            continue
        retained_deferrals.append(deferral)
    derived["deferrals"] = retained_deferrals
    derived["packet_findings"] = [
        finding
        for finding in derived["packet_findings"]
        if finding != "project_scope_unresolved"
    ]
    if any(
        item["reason_code"] == "project_scope_unresolved"
        for item in derived["deferrals"]
    ):
        raise PacketReviewError("resolved packet retained project-scope deferral")
    return derived, transformations


async def _build(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise PacketReviewError("POSTGRES_DSN is required")
    owner = uuid.UUID(args.owner_user_id)
    packet_id = uuid.UUID(args.packet_id)
    expected_project_key = _project_name_key(args.expected_project_key)
    expected_component_key = _project_name_key(args.expected_component_key)
    repo_root, commit = _repository_state()

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            packet_row = await conn.fetchrow(
                """
                SELECT packet_id,evidence_id,job_id,provider_id,provider_version,
                       validator_packet_sha256,packet_storage_sha256,
                       normalized_packet,manual_review_required,
                       project_binding_event_id,entity_mention_count,
                       observation_count,comparison_hint_count,deferral_count
                  FROM memory.evidence_extraction_packet_v5
                 WHERE owner_user_id=$1 AND packet_id=$2
                """,
                owner,
                packet_id,
            )
            if packet_row is None:
                raise PacketReviewError("owner-scoped immutable packet not found")
            packet = _json_value(packet_row["normalized_packet"], "normalized packet")
            if canonical_sha256(packet) != packet_row["validator_packet_sha256"]:
                raise PacketReviewError("immutable normalized packet hash mismatch")
            if not packet_row["manual_review_required"]:
                raise PacketReviewError("packet is not marked for manual review")
            if packet_row["project_binding_event_id"] is not None:
                raise PacketReviewError("packet already has a project binding event")
            if (
                len(packet["entity_mentions"]) != packet_row["entity_mention_count"]
                or len(packet["observations"]) != packet_row["observation_count"]
                or len(packet["comparison_hints"])
                != packet_row["comparison_hint_count"]
                or len(packet["deferrals"]) != packet_row["deferral_count"]
            ):
                raise PacketReviewError("packet row counts do not match payload")

            evidence_row = await conn.fetchrow(
                """
                SELECT evidence_id,source_system,external_id,content,
                       content_sha256,status::text,metadata
                  FROM memory.evidence
                 WHERE owner_user_id=$1 AND evidence_id=$2
                """,
                owner,
                packet_row["evidence_id"],
            )
            if evidence_row is None:
                raise PacketReviewError("owner-scoped evidence not found")
            evidence = dict(evidence_row)
            evidence["metadata"] = _json_value(evidence["metadata"], "evidence metadata")
            _verify_source(packet, evidence)
            thread_id = uuid.UUID(str(evidence["metadata"].get("thread_id")))
            binding = await conn.fetchrow(
                """
                SELECT binding_event_id,thread_id,project_id,project_key,bound_at
                  FROM memory.current_project_thread_binding_v5
                 WHERE owner_user_id=$1 AND thread_id=$2
                """,
                owner,
                thread_id,
            )
            if binding is None:
                raise PacketReviewError("trusted owner/thread project binding is absent")
            if _project_name_key(binding["project_key"]) != expected_project_key:
                raise PacketReviewError("trusted project binding differs from expectation")
            component_rows = await conn.fetch(
                """
                SELECT component_id,component_key,display_name,
                       parent_component_id,aliases
                  FROM memory.read_owner_project_components_v5($1)
                """,
                binding["project_id"],
            )
            components = [
                {
                    "component_id": str(row["component_id"]),
                    "component_key": row["component_key"],
                    "display_name": row["display_name"],
                    "parent_component_id": (
                        str(row["parent_component_id"])
                        if row["parent_component_id"] is not None
                        else None
                    ),
                    "aliases": list(row["aliases"]),
                }
                for row in component_rows
            ]
            if sum(
                component["component_key"] == expected_component_key
                for component in components
            ) != 1:
                raise PacketReviewError("expected component is not uniquely registered")

            derived, transformations = resolve_packet_project_scopes(
                packet,
                project_key=binding["project_key"],
                components=components,
            )
            derived = NormalizedPacket.model_validate(derived).model_dump(mode="json")
            _validate_extraction_packet(derived)
            _verify_source(derived, evidence)
            resolved_components = {
                observation["project_scope"].get("component_key")
                for observation in derived["observations"]
                if observation["projection_class"] == "project_knowledge"
            }
            if resolved_components != {expected_component_key}:
                raise PacketReviewError("derived packet resolved outside expected component")

            resolutions = [
                resolve_mention(
                    mention,
                    derived["observations"],
                    await _candidates(
                        conn, owner, mention, derived["observations"]
                    ),
                    derived["deferrals"],
                )
                for mention in derived["entity_mentions"]
            ]
    finally:
        await conn.close()

    resolution_body = {
        "contract_version": "memory_v1_entity_resolution_review_v5",
        "source_envelope": derived["source_envelope"],
        "predicate_registry_version": "memory_predicate_registry_v5",
        "entity_normalization_version": "memory_entity_normalization_v5",
        "resolver": RESOLVER,
        "resolver_version": RESOLVER_VERSION,
        "resolutions": resolutions,
    }
    resolution = dict(resolution_body)
    resolution["packet_sha256"] = sha256_text(stable_json(resolution_body))
    _validate_resolution_packet(resolution, derived)
    extraction_text = stable_json(derived)
    resolution_text = stable_json(resolution)
    extraction_sha = sha256_text(extraction_text)
    resolution_sha = sha256_text(resolution_text)
    evidence_id = str(packet_row["evidence_id"])
    request_id = str(
        uuid.uuid5(
            REQUEST_NAMESPACE,
            "|".join(
                (
                    str(owner),evidence_id,extraction_sha,resolution_sha,
                    RESOLVER_VERSION,
                )
            ),
        )
    )
    review_id = str(
        uuid.uuid5(
            REVIEW_NAMESPACE,
            "|".join(
                (
                    str(owner),str(packet_id),packet_row["packet_storage_sha256"],
                    extraction_sha,resolution_sha,
                )
            ),
        )
    )
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    review = {
        "contract_version": REVIEW_CONTRACT,
        "mode": "owner_scoped_deterministic_review_zero_write",
        "generated_at": now,
        "owner_user_id": str(owner),
        "review_id": review_id,
        "packet_id": str(packet_id),
        "packet_storage_sha256": packet_row["packet_storage_sha256"],
        "original_packet_sha256": packet_row["validator_packet_sha256"],
        "derived_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "evidence_id": evidence_id,
        "evidence_content_sha256": evidence["content_sha256"],
        "thread_id": str(thread_id),
        "project_binding_event_id": str(binding["binding_event_id"]),
        "project_id": str(binding["project_id"]),
        "project_key": binding["project_key"],
        "component_key": expected_component_key,
        "repository_commit": commit,
        "transformations": transformations,
        "resolution_summary": {
            state: sum(
                item["decision_state"] == state for item in resolutions
            )
            for state in (
                "auto_link_eligible","manual_review_required","deferred","rejected"
            )
        },
        "zero_write_proof": {
            "database_writes": 0,
            "qdrant_writes": 0,
            "external_model_calls": 0,
            "readonly_repeatable_read_transaction": True,
        },
    }
    bundle = {
        "contract_version": BUNDLE_CONTRACT,
        "generated_at": now,
        "mode": "preflight_only_zero_write",
        "server": "seebx",
        "owner_user_id": str(owner),
        "case_id": f"packet-{packet_id}",
        "evidence_id": evidence_id,
        "request_id": request_id,
        "extractor": "memory_v1_v5_reviewed_packet_resolution",
        "extractor_version": commit,
        "source_report": {},
        "schemas": {
            "extraction_sha256": _schema_hash(
                (repo_root / args.extraction_schema).resolve()
            ),
            "resolution_sha256": _schema_hash(
                (repo_root / args.resolution_schema).resolve()
            ),
        },
        "extraction_packet_text": extraction_text,
        "resolution_packet_text": resolution_text,
        "extraction_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "resolution_summary": review["resolution_summary"],
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "authorized_stage": False,
    }
    return review, bundle


async def main() -> int:
    args = arguments()
    root = _secure_root(args.review_root)
    report_path = _output_path(args.review_report, root)
    bundle_path = _output_path(args.stage_bundle, root)
    review, bundle = await _build(args)
    report_sha = _secure_write(report_path, review)
    try:
        bundle["source_report"] = {
            "path": str(report_path),
            "sha256": report_sha,
        }
        bundle_sha = _secure_write(bundle_path, bundle)
    except BaseException:
        report_path.unlink(missing_ok=True)
        raise
    print(stable_json({
        "contract_version": REVIEW_CONTRACT,
        "review_report": str(report_path),
        "review_report_sha256": report_sha,
        "stage_bundle": str(bundle_path),
        "stage_bundle_sha256": bundle_sha,
        "owner_user_id": review["owner_user_id"],
        "packet_id": review["packet_id"],
        "project_key": review["project_key"],
        "component_key": review["component_key"],
        "resolution_summary": review["resolution_summary"],
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
