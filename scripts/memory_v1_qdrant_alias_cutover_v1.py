#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import stat
import struct
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from qdrant_client.http import models as qmodels

from rag_engine.memory_v1_qdrant_rebuild_contract_v1 import (
    ACTIVE_ALIAS,
    INCREMENTAL_POINT_FIELDS,
    INCREMENTAL_POINT_FIELDS_V3,
    INCREMENTAL_POINT_PROVENANCE_VERSION,
    INCREMENTAL_POINT_PROVENANCE_VERSION_V3,
    POINT_PROVENANCE_FIELDS,
    POINT_PROVENANCE_VERSION,
    QUALITY_VERSION,
    REBUILD_PAYLOAD_INDEX_FIELDS,
    RETRIEVABLE_POINT_STATUSES,
    RebuildContractError,
    SupportedClaimSnapshotV1,
    alias_transition,
    canonical_bytes,
    collection_fingerprint_sha256,
    projection_source_sha256,
    qdrant_mutation_lock,
    require_production_write_guard,
    sha256_bytes,
    source_snapshot_sha256,
    validate_production_write_lease,
    validate_shadow_collection,
)
from rag_engine.qdrant_compat import make_qdrant_client


SPEC_VERSION = "memory_v1_qdrant_alias_transition_spec_v1"
REPORT_VERSION = "memory_v1_qdrant_alias_transition_report_v1"
REBUILD_REPORT_VERSION = "memory_v1_qdrant_shadow_rebuild_report_v1"
LIVE_COLLECTION = "memory_claim_v1"
SNAPSHOT_ROOT = pathlib.PurePosixPath("/home/ubuntu/brains/snapshots")
SNAPSHOT_ROOT_PATH = pathlib.Path(SNAPSHOT_ROOT)
MAX_FINGERPRINT_POINTS = 100_000
REBUILD_REPORT_FIELDS = frozenset(
    {
        "claim_count",
        "collection",
        "collection_fingerprint_sha256",
        "contract_version",
        "cross_owner_check_count",
        "dimensions",
        "distance",
        "embedding_input_bytes",
        "embedding_input_tokens",
        "embedding_model",
        "embedding_request_count",
        "expected_git",
        "lifecycle_check_count",
        "negative_check_count",
        "owner_count",
        "point_inventory_sha256",
        "payload_index_schema_sha256",
        "production_postgres_read_only",
        "quality",
        "renderer_sha256",
        "run_id",
        "source_snapshot_sha256",
        "spec_sha256",
    }
)
QUALITY_FIELDS = frozenset(
    {
        "accepted",
        "contract_version",
        "evaluated_query_count",
        "margin_ppm",
        "mrr_ppm",
        "negative_query_count",
        "positive_above_threshold_ppm",
        "recommended_score_threshold_ppm",
        "top1_count",
        "top1_ppm",
        "top5_count",
        "top5_ppm",
    }
)
AUDIT_STATES = frozenset(
    {
        "prepared",
        "completed",
        "failed_no_change",
        "indeterminate",
        "rolled_back_verified",
    }
)


class AliasCutoverError(RuntimeError):
    pass


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_collection(value: Any, *, live_allowed: bool) -> bool:
    text = str(value or "")
    if live_allowed and text == LIVE_COLLECTION:
        return True
    try:
        validate_shadow_collection(text)
    except RebuildContractError:
        return False
    return True


def _snapshot_path(value: Any, name: str) -> pathlib.PurePosixPath:
    path = pathlib.PurePosixPath(str(value or ""))
    if (
        not path.is_absolute()
        or ".." in path.parts
        or SNAPSHOT_ROOT not in path.parents
        or path.parent == SNAPSHOT_ROOT
        or not path.name
    ):
        raise AliasCutoverError(f"{name} path rejected")
    return path


@dataclass(frozen=True)
class Spec:
    value: dict[str, Any]
    raw: bytes

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.raw)

    @classmethod
    def load(cls, path: pathlib.Path, expected_sha256: str) -> "Spec":
        raw = path.read_bytes()
        if not _is_sha256(expected_sha256) or sha256_bytes(raw) != expected_sha256:
            raise AliasCutoverError("spec digest mismatch")
        try:
            value = json.loads(raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AliasCutoverError("spec parse failed") from exc
        if raw != canonical_bytes(value):
            raise AliasCutoverError("spec is not canonical")
        expected = {
            "schema_version",
            "run_id",
            "operation",
            "expected_git",
            "lease",
            "qdrant_url",
            "alias_name",
            "expected_alias_source",
            "expected_alias_source_fingerprint_sha256",
            "expected_postgres_projection_inventory_sha256",
            "expected_postgres_supported_snapshot_sha256",
            "target_collection",
            "target_collection_fingerprint_sha256",
            "rebuild_report_path",
            "rebuild_report_sha256",
            "output_path",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise AliasCutoverError("spec fields differ")
        operation = value.get("operation")
        if value.get("schema_version") != SPEC_VERSION or operation not in {
            "bootstrap",
            "cutover",
            "rollback",
        }:
            raise AliasCutoverError("spec operation rejected")
        if not re.fullmatch(
            r"memory-qdrant-alias-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}",
            str(value.get("run_id") or ""),
        ):
            raise AliasCutoverError("run id rejected")
        git = value.get("expected_git")
        lease = value.get("lease")
        if (
            not isinstance(git, dict)
            or set(git) != {"head", "tree"}
            or any(not re.fullmatch(r"[0-9a-f]{40}", str(item)) for item in git.values())
            or value.get("qdrant_url") != "http://127.0.0.1:6333"
            or value.get("alias_name") != ACTIVE_ALIAS
            or not _is_sha256(value.get("target_collection_fingerprint_sha256"))
            or not _is_sha256(
                value.get("expected_postgres_projection_inventory_sha256")
            )
            or not _is_sha256(
                value.get("expected_postgres_supported_snapshot_sha256")
            )
        ):
            raise AliasCutoverError("identity contract rejected")
        try:
            validate_production_write_lease(lease)
        except RebuildContractError as exc:
            raise AliasCutoverError("production-write lease rejected") from exc

        source = value.get("expected_alias_source")
        target = str(value.get("target_collection") or "")
        report_path = value.get("rebuild_report_path")
        report_sha = value.get("rebuild_report_sha256")
        source_fingerprint = value.get("expected_alias_source_fingerprint_sha256")
        if operation == "bootstrap":
            if source is not None or target != LIVE_COLLECTION:
                raise AliasCutoverError("bootstrap boundary rejected")
            if report_path is not None or report_sha is not None:
                raise AliasCutoverError("bootstrap does not accept a rebuild report")
            if source_fingerprint is not None:
                raise AliasCutoverError("bootstrap does not accept a source fingerprint")
        elif operation == "cutover":
            if not _is_collection(source, live_allowed=True):
                raise AliasCutoverError("cutover source rejected")
            if not _is_collection(target, live_allowed=False) or target == source:
                raise AliasCutoverError("cutover target rejected")
            if not _is_sha256(source_fingerprint):
                raise AliasCutoverError("cutover source fingerprint rejected")
        else:
            if not _is_collection(source, live_allowed=False):
                raise AliasCutoverError("rollback source rejected")
            if not _is_collection(target, live_allowed=True) or target == source:
                raise AliasCutoverError("rollback target rejected")
            if not _is_sha256(source_fingerprint):
                raise AliasCutoverError("rollback source fingerprint rejected")

        output = _snapshot_path(value.get("output_path"), "output")
        if target == LIVE_COLLECTION:
            if report_path is not None or report_sha is not None:
                raise AliasCutoverError("live target does not accept a rebuild report")
        else:
            report = _snapshot_path(report_path, "rebuild report")
            if not _is_sha256(report_sha) or report == output:
                raise AliasCutoverError("rebuild report binding rejected")
        return cls(value=value, raw=raw)


def _run(argv: Sequence[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        list(argv),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        env={"LANG": "C", "LC_ALL": "C"},
        timeout=60,
    )
    if result.returncode != 0:
        raise AliasCutoverError("bounded subprocess failed")
    return result.stdout


def git_identity() -> dict[str, str]:
    prefix = [
        "/usr/bin/git",
        "--no-optional-locks",
        "--no-pager",
        "-c",
        f"safe.directory={REPOSITORY}",
        "-C",
        str(REPOSITORY),
    ]
    head = _run([*prefix, "rev-parse", "HEAD"]).strip().decode("ascii")
    tree = _run([*prefix, "rev-parse", "HEAD^{tree}"]).strip().decode("ascii")
    if _run([*prefix, "status", "--porcelain=v1", "--untracked-files=normal"]):
        raise AliasCutoverError("checkout is dirty")
    return {"head": head, "tree": tree}


SOURCE_SQL = r"""BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT json_build_object('_kind','proof','isolation',current_setting('transaction_isolation'),'read_only',current_setting('transaction_read_only'))::text;
SELECT json_build_object(
 '_kind','claim',
 'owner_user_id',claim.owner_user_id,
 'claim_id',claim.claim_id,
 'canonical_text',claim.canonical_text,
 'predicate',claim.predicate,
 'qualifiers',claim.qualifiers,
 'status',claim.status::text,
 'sensitivity',claim.sensitivity::text,
 'retrieval_policy',claim.retrieval_policy,
 'updated_at',claim.updated_at,
 'revision_number',COALESCE(max(revision.revision_number),0))::text
FROM memory.claim AS claim
LEFT JOIN memory.claim_revision AS revision
 ON revision.owner_user_id=claim.owner_user_id
AND revision.claim_id=claim.claim_id
WHERE claim.status::text IN ('supported','uncertain','disputed')
GROUP BY claim.owner_user_id,claim.claim_id
ORDER BY claim.owner_user_id,claim.claim_id;
ROLLBACK;
"""


def current_source_state() -> dict[str, Any]:
    raw = _run(
        [
            "/usr/bin/docker",
            "exec",
            "-i",
            "brains-postgres-1",
            "/usr/bin/psql",
            "-X",
            "--no-psqlrc",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "sage",
            "-d",
            "memory",
        ],
        input_bytes=SOURCE_SQL.encode("utf-8"),
    ).decode("utf-8", "strict")
    rows = [json.loads(line) for line in raw.splitlines() if line.startswith("{")]
    if not rows or rows[0] != {
        "_kind": "proof",
        "isolation": "repeatable read",
        "read_only": "on",
    }:
        raise AliasCutoverError("read-only source snapshot proof rejected")
    supported_claims: list[SupportedClaimSnapshotV1] = []
    projection_inventory: list[tuple[str, str, str, int, str]] = []
    for row in rows[1:]:
        if not isinstance(row, dict) or row.pop("_kind", None) != "claim":
            raise AliasCutoverError("source row framing rejected")
        try:
            source_sha = projection_source_sha256(row)
            if str(row.get("status") or "").strip().lower() == "supported":
                supported_claims.append(SupportedClaimSnapshotV1.from_mapping(row))
        except RebuildContractError as exc:
            raise AliasCutoverError("source row contract rejected") from exc
        projection_inventory.append(
            (
                str(row["owner_user_id"]),
                str(row["claim_id"]),
                str(row["status"]).strip().lower(),
                int(row["revision_number"]),
                source_sha,
            )
        )
    try:
        snapshot = source_snapshot_sha256(supported_claims)
    except RebuildContractError as exc:
        raise AliasCutoverError("source snapshot rejected") from exc
    identity_inventory = [item[:4] for item in projection_inventory]
    return {
        "claim_count": len(supported_claims),
        "owner_count": len({claim.owner_user_id for claim in supported_claims}),
        "projection_identity_inventory_sha256": sha256_bytes(
            canonical_bytes(sorted(identity_inventory))
        ),
        "projection_inventory_sha256": sha256_bytes(
            canonical_bytes(sorted(projection_inventory))
        ),
        "_projection_inventory": sorted(projection_inventory),
        "supported_projection_inventory_sha256": sha256_bytes(
            canonical_bytes(
                sorted(
                    (
                        str(claim.owner_user_id),
                        str(claim.claim_id),
                        claim.revision_number,
                    )
                    for claim in supported_claims
                )
            )
        ),
        "source_snapshot_sha256": snapshot,
    }


def require_guard(spec: Spec) -> None:
    try:
        require_production_write_guard(spec.value["lease"])
    except RebuildContractError as exc:
        raise AliasCutoverError("production-write guard denied") from exc


def _read_private_snapshot_file(path: pathlib.Path) -> bytes:
    try:
        parent_descriptor, name = _open_snapshot_parent(path, SNAPSHOT_ROOT_PATH)
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        raise AliasCutoverError("snapshot evidence open failed") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise AliasCutoverError("snapshot evidence metadata rejected")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > 2_000_000:
                raise AliasCutoverError("snapshot evidence exceeds bounds")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_rebuild_report(spec: Spec) -> dict[str, Any] | None:
    target = spec.value["target_collection"]
    if target == LIVE_COLLECTION:
        return None
    raw = _read_private_snapshot_file(pathlib.Path(spec.value["rebuild_report_path"]))
    if sha256_bytes(raw) != spec.value["rebuild_report_sha256"]:
        raise AliasCutoverError("rebuild report digest changed")
    try:
        report = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AliasCutoverError("rebuild report parse failed") from exc
    if raw != canonical_bytes(report) or not isinstance(report, dict) or set(report) != REBUILD_REPORT_FIELDS:
        raise AliasCutoverError("rebuild report contract differs")
    quality = report.get("quality")
    integer_fields = (
        "claim_count",
        "cross_owner_check_count",
        "dimensions",
        "embedding_input_bytes",
        "embedding_input_tokens",
        "embedding_request_count",
        "lifecycle_check_count",
        "negative_check_count",
        "owner_count",
    )
    quality_integer_fields = tuple(QUALITY_FIELDS - {"accepted", "contract_version"})
    if (
        report.get("contract_version") != REBUILD_REPORT_VERSION
        or report.get("collection") != target
        or report.get("production_postgres_read_only") is not True
        or report.get("embedding_model") != "text-embedding-3-large"
        or report.get("dimensions") != 3072
        or report.get("distance") != "dot"
        or not isinstance(report.get("expected_git"), dict)
        or set(report["expected_git"]) != {"head", "tree"}
        or any(
            not re.fullmatch(r"[0-9a-f]{40}", str(item))
            for item in report["expected_git"].values()
        )
        or not re.fullmatch(
            r"memory-qdrant-rebuild-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}",
            str(report.get("run_id") or ""),
        )
        or not _is_sha256(report.get("spec_sha256"))
        or any(type(report.get(name)) is not int or report[name] < 0 for name in integer_fields)
        or report["claim_count"] < 1
        or report["owner_count"] < 1
        or report["embedding_request_count"] < 1
        or report["lifecycle_check_count"] != 4
        or not _is_sha256(report.get("point_inventory_sha256"))
        or not _is_sha256(report.get("payload_index_schema_sha256"))
        or not _is_sha256(report.get("collection_fingerprint_sha256"))
        or not _is_sha256(report.get("renderer_sha256"))
        or not _is_sha256(report.get("source_snapshot_sha256"))
        or not isinstance(quality, dict)
        or set(quality) != QUALITY_FIELDS
        or quality.get("accepted") is not True
        or quality.get("contract_version") != QUALITY_VERSION
        or any(type(quality.get(name)) is not int for name in quality_integer_fields)
        or quality["evaluated_query_count"] != report["claim_count"] * 3
        or quality["negative_query_count"] != report["negative_check_count"]
        or report["cross_owner_check_count"]
        != report["claim_count"] * 3 * (report["owner_count"] - 1)
    ):
        raise AliasCutoverError("rebuild report acceptance proof rejected")
    return report


def observed_aliases(qdrant: Any) -> dict[str, str]:
    response = qdrant.get_aliases()
    aliases: dict[str, str] = {}
    for item in response.aliases:
        name = str(item.alias_name)
        collection = str(item.collection_name)
        if name in aliases:
            raise AliasCutoverError("duplicate alias state")
        aliases[name] = collection
    return aliases


def alias_state_sha256(aliases: Mapping[str, str]) -> str:
    return sha256_bytes(canonical_bytes(dict(sorted(aliases.items()))))


def operations(spec: Spec, aliases: Mapping[str, str]) -> list[Any]:
    if spec.value["operation"] == "bootstrap":
        if ACTIVE_ALIAS in aliases:
            raise AliasCutoverError("active alias already exists")
        return [
            qmodels.CreateAliasOperation(
                create_alias=qmodels.CreateAlias(
                    collection_name=LIVE_COLLECTION, alias_name=ACTIVE_ALIAS
                )
            )
        ]
    relevant = {ACTIVE_ALIAS: aliases[ACTIVE_ALIAS]} if ACTIVE_ALIAS in aliases else {}
    try:
        raw = alias_transition(
            alias_name=ACTIVE_ALIAS,
            expected_source=spec.value["expected_alias_source"],
            target=spec.value["target_collection"],
            observed=relevant,
        )
    except RebuildContractError as exc:
        raise AliasCutoverError("active alias state differs from approved source") from exc
    return [
        qmodels.DeleteAliasOperation(
            delete_alias=qmodels.DeleteAlias(alias_name=raw[0]["delete_alias"]["alias_name"])
        ),
        qmodels.CreateAliasOperation(
            create_alias=qmodels.CreateAlias(
                collection_name=raw[1]["create_alias"]["collection_name"],
                alias_name=raw[1]["create_alias"]["alias_name"],
            )
        ),
    ]


def _model_attribute(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _vector_config(qdrant: Any, collection: str) -> tuple[int, str, int | None]:
    info = qdrant.get_collection(collection_name=collection)
    config = _model_attribute(info, "config")
    params = _model_attribute(config, "params")
    vectors = _model_attribute(params, "vectors")
    if isinstance(vectors, Mapping):
        raise AliasCutoverError("named-vector collection rejected")
    size = _model_attribute(vectors, "size")
    distance = _enum_text(_model_attribute(vectors, "distance"))
    point_count = _model_attribute(info, "points_count")
    if type(size) is not int or size < 1 or distance not in {"cosine", "dot", "euclid", "manhattan"}:
        raise AliasCutoverError("collection vector configuration rejected")
    if point_count is not None and (type(point_count) is not int or point_count < 0):
        raise AliasCutoverError("collection point count rejected")
    return size, distance, point_count


def _payload_index_schema(qdrant: Any, collection: str) -> dict[str, str]:
    info = qdrant.get_collection(collection_name=collection)
    raw = _model_attribute(info, "payload_schema") or {}
    if not isinstance(raw, Mapping):
        raise AliasCutoverError("collection payload index schema rejected")
    result: dict[str, str] = {}
    for field, schema in raw.items():
        name = str(field)
        data_type = _enum_text(_model_attribute(schema, "data_type") or schema)
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", name)
            or data_type
            not in {"keyword", "integer", "float", "geo", "text", "bool", "datetime", "uuid"}
        ):
            raise AliasCutoverError("collection payload index schema rejected")
        result[name] = data_type
    return dict(sorted(result.items()))


def _vector_sha256(vector: Any, dimensions: int) -> str:
    if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
        raise AliasCutoverError("point vector dimensions differ")
    values = [float(item) for item in vector]
    if any(not math.isfinite(item) for item in values):
        raise AliasCutoverError("point vector is non-finite")
    return sha256_bytes(struct.pack("<" + "f" * dimensions, *values))


def collection_evidence(
    qdrant: Any,
    collection: str,
    *,
    require_rebuild_provenance: bool,
) -> dict[str, Any]:
    dimensions, distance, reported_count = _vector_config(qdrant, collection)
    payload_indexes = _payload_index_schema(qdrant, collection)
    if collection == LIVE_COLLECTION:
        if distance != "cosine":
            raise AliasCutoverError("legacy collection distance differs")
    elif _is_collection(collection, live_allowed=False):
        if distance != "dot":
            raise AliasCutoverError("shadow collection distance differs")
    else:
        raise AliasCutoverError("collection identity rejected")
    if require_rebuild_provenance and payload_indexes != {
        field: "keyword" for field in sorted(REBUILD_PAYLOAD_INDEX_FIELDS)
    }:
        raise AliasCutoverError("shadow payload index schema differs")
    if require_rebuild_provenance and distance != "dot":
        raise AliasCutoverError("shadow distance is not dot")
    points: list[dict[str, Any]] = []
    rebuild_inventory: list[tuple[str, str, str]] = []
    supported_inventory: list[tuple[str, str, int]] = []
    projection_inventory: list[tuple[str, str, str, int, str | None]] = []
    owners: set[str] = set()
    models: set[str] = set()
    renderers: set[str] = set()
    rebuild_runs: set[str] = set()
    snapshots: set[str] = set()
    rebuild_point_count = 0
    incremental_v1_point_count = 0
    incremental_v3_point_count = 0
    offset: Any = None
    seen_offsets: set[str] = set()
    while True:
        records, next_offset = qdrant.scroll(
            collection_name=collection,
            scroll_filter=None,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for record in records:
            if len(points) >= MAX_FINGERPRINT_POINTS:
                raise AliasCutoverError("collection fingerprint point bound exceeded")
            point_id = str(_model_attribute(record, "id") or "")
            if not point_id:
                raise AliasCutoverError("point identifier rejected")
            payload = _model_attribute(record, "payload") or {}
            if not isinstance(payload, Mapping):
                raise AliasCutoverError("point provenance fields rejected")
            payload = dict(payload)
            version = payload.get("schema_version")
            expected_fields = {
                INCREMENTAL_POINT_PROVENANCE_VERSION: INCREMENTAL_POINT_FIELDS,
                INCREMENTAL_POINT_PROVENANCE_VERSION_V3: INCREMENTAL_POINT_FIELDS_V3,
                POINT_PROVENANCE_VERSION: POINT_PROVENANCE_FIELDS,
            }.get(version)
            if distance == "cosine":
                allowed_versions = {INCREMENTAL_POINT_PROVENANCE_VERSION}
            elif distance == "dot":
                allowed_versions = {
                    POINT_PROVENANCE_VERSION,
                    INCREMENTAL_POINT_PROVENANCE_VERSION_V3,
                }
            else:
                raise AliasCutoverError("collection distance is unsupported")
            try:
                uuid.UUID(str(payload.get("owner_user_id")))
                uuid.UUID(str(payload.get("claim_id")))
            except (ValueError, TypeError, AttributeError) as exc:
                raise AliasCutoverError("point identity rejected") from exc
            if (
                expected_fields is None
                or version not in allowed_versions
                or set(payload) != expected_fields
                or str(payload["claim_id"]) != point_id
                or payload.get("status") not in RETRIEVABLE_POINT_STATUSES
                or type(payload.get("revision_number")) is not int
                or payload["revision_number"] < 0
                or type(payload.get("requires_explicit")) is not bool
            ):
                raise AliasCutoverError("point provenance rejected")
            if require_rebuild_provenance and version != POINT_PROVENANCE_VERSION:
                raise AliasCutoverError("shadow point provenance is not rebuild-exact")
            if version == INCREMENTAL_POINT_PROVENANCE_VERSION:
                incremental_v1_point_count += 1
            elif version == POINT_PROVENANCE_VERSION:
                rebuild_point_count += 1
                if (
                    payload["status"] != "supported"
                    or payload["dimensions"] != dimensions
                    or payload["embedding_model"] != "text-embedding-3-large"
                    or not re.fullmatch(
                        r"memory-qdrant-rebuild-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}",
                        str(payload["rebuild_run_id"]),
                    )
                    or not _is_sha256(payload["renderer_sha256"])
                    or not _is_sha256(payload["source_sha256"])
                    or not _is_sha256(payload["source_snapshot_sha256"])
                    or not _is_sha256(payload["vector_sha256"])
                ):
                    raise AliasCutoverError("shadow point provenance rejected")
                models.add(str(payload["embedding_model"]))
                renderers.add(str(payload["renderer_sha256"]))
                rebuild_runs.add(str(payload["rebuild_run_id"]))
                snapshots.add(str(payload["source_snapshot_sha256"]))
            elif version == INCREMENTAL_POINT_PROVENANCE_VERSION_V3:
                incremental_v3_point_count += 1
                manifest_payload = dict(payload)
                manifest_sha256 = manifest_payload.pop(
                    "projection_manifest_sha256", None
                )
                if (
                    payload["dimensions"] != dimensions
                    or payload["embedding_model"] != "text-embedding-3-large"
                    or not _is_sha256(payload["renderer_sha256"])
                    or not _is_sha256(payload["source_sha256"])
                    or not _is_sha256(payload["vector_sha256"])
                    or not _is_sha256(manifest_sha256)
                    or sha256_bytes(canonical_bytes(manifest_payload))
                    != manifest_sha256
                ):
                    raise AliasCutoverError("incremental point provenance rejected")
                models.add(str(payload["embedding_model"]))
            owners.add(str(payload["owner_user_id"]))
            source_sha = (
                str(payload["source_sha256"])
                if version
                in {
                    POINT_PROVENANCE_VERSION,
                    INCREMENTAL_POINT_PROVENANCE_VERSION_V3,
                }
                else None
            )
            projection_inventory.append(
                (
                    str(payload["owner_user_id"]),
                    point_id,
                    str(payload["status"]),
                    payload["revision_number"],
                    source_sha,
                )
            )
            if payload["status"] == "supported":
                supported_inventory.append(
                    (
                        str(payload["owner_user_id"]),
                        point_id,
                        payload["revision_number"],
                    )
                )
            if version == POINT_PROVENANCE_VERSION:
                models.add(str(payload["embedding_model"]))
                rebuild_inventory.append(
                    (point_id, str(payload["owner_user_id"]), str(payload["vector_sha256"]))
                )
            stored_vector_sha256 = _vector_sha256(
                _model_attribute(record, "vector"), dimensions
            )
            if distance == "dot":
                vector_values = [
                    float(item) for item in _model_attribute(record, "vector")
                ]
                squared_norm = math.fsum(item * item for item in vector_values)
                if not math.isclose(squared_norm, 1.0, rel_tol=0.0, abs_tol=2e-6):
                    raise AliasCutoverError("dot point vector is not normalized")
            if version in {
                POINT_PROVENANCE_VERSION,
                INCREMENTAL_POINT_PROVENANCE_VERSION_V3,
            } and stored_vector_sha256 != payload["vector_sha256"]:
                raise AliasCutoverError("stored vector digest differs from provenance")
            points.append(
                {
                    "id": point_id,
                    "payload": payload,
                    "stored_vector_sha256": stored_vector_sha256,
                }
            )
        if next_offset is None:
            break
        marker = repr(next_offset)
        if marker in seen_offsets:
            raise AliasCutoverError("collection pagination repeated")
        seen_offsets.add(marker)
        offset = next_offset
    if len({item["id"] for item in points}) != len(points):
        raise AliasCutoverError("collection point identifiers are duplicated")
    if reported_count is not None and reported_count != len(points):
        raise AliasCutoverError("collection point count changed during fingerprint")
    return {
        "collection_fingerprint_sha256": collection_fingerprint_sha256(
            collection=collection,
            dimensions=dimensions,
            distance=distance,
            points=points,
            payload_indexes=payload_indexes,
        ),
        "dimensions": dimensions,
        "distance": distance,
        "embedding_model": next(iter(models)) if len(models) == 1 else None,
        "owner_count": len(owners),
        "payload_index_schema_sha256": sha256_bytes(canonical_bytes(payload_indexes)),
        "point_count": len(points),
        "rebuild_point_count": rebuild_point_count,
        "incremental_v1_point_count": incremental_v1_point_count,
        "incremental_v3_point_count": incremental_v3_point_count,
        "point_inventory_sha256": (
            sha256_bytes(canonical_bytes(sorted(rebuild_inventory)))
            if require_rebuild_provenance
            else None
        ),
        "renderer_sha256": next(iter(renderers)) if len(renderers) == 1 else None,
        "rebuild_run_id": (
            next(iter(rebuild_runs)) if len(rebuild_runs) == 1 else None
        ),
        "source_snapshot_sha256": next(iter(snapshots)) if len(snapshots) == 1 else None,
        "supported_point_count": len(supported_inventory),
        "supported_projection_inventory_sha256": sha256_bytes(
            canonical_bytes(sorted(supported_inventory))
        ),
        "projection_identity_inventory_sha256": sha256_bytes(
            canonical_bytes(sorted(item[:4] for item in projection_inventory))
        ),
        "projection_inventory_sha256": sha256_bytes(
            canonical_bytes(sorted(projection_inventory, key=lambda item: item[:4]))
        ),
        "_projection_inventory": sorted(
            projection_inventory, key=lambda item: item[:4]
        ),
    }


def require_current_source_parity(
    evidence: Mapping[str, Any], source_state: Mapping[str, Any]
) -> None:
    qdrant_inventory = evidence.get("_projection_inventory")
    postgres_inventory = source_state.get("_projection_inventory")
    if not isinstance(qdrant_inventory, list) or not isinstance(
        postgres_inventory, list
    ):
        raise AliasCutoverError("collection supported inventory differs from PostgreSQL")
    qdrant_by_identity = {tuple(item[:4]): item[4] for item in qdrant_inventory}
    postgres_by_identity = {tuple(item[:4]): item[4] for item in postgres_inventory}
    if (
        len(qdrant_by_identity) != len(qdrant_inventory)
        or len(postgres_by_identity) != len(postgres_inventory)
        or set(qdrant_by_identity) != set(postgres_by_identity)
        or any(
            source_sha is not None
            and source_sha != postgres_by_identity[identity]
            for identity, source_sha in qdrant_by_identity.items()
        )
    ):
        raise AliasCutoverError("collection projection inventory differs from PostgreSQL")


def validate_target_evidence(
    spec: Spec, report: Mapping[str, Any] | None, evidence: Mapping[str, Any]
) -> None:
    if evidence.get("collection_fingerprint_sha256") != spec.value["target_collection_fingerprint_sha256"]:
        raise AliasCutoverError("target collection fingerprint changed")
    if report is None:
        if spec.value["target_collection"] != LIVE_COLLECTION:
            raise AliasCutoverError("shadow target lacks acceptance report")
        if (
            evidence.get("distance") != "cosine"
            or evidence.get("incremental_v1_point_count")
            != evidence.get("point_count")
            or evidence.get("rebuild_point_count") != 0
            or evidence.get("incremental_v3_point_count") != 0
        ):
            raise AliasCutoverError("legacy target provenance differs")
        return
    if report.get("collection") != spec.value["target_collection"]:
        raise AliasCutoverError("rebuild report collection differs from target")
    if spec.value["operation"] == "rollback":
        # A retained shadow may contain truthful v3 points created by normal
        # projection after its strict-v2 acceptance. Require an intact v2
        # lineage, exact collection structure, exact expected current
        # fingerprint, and (in execute) exact current-PostgreSQL parity.
        if (
            evidence.get("dimensions") != report["dimensions"]
            or evidence.get("distance") != report["distance"]
            or report["distance"] != "dot"
            or evidence.get("embedding_model") != report["embedding_model"]
            or evidence.get("payload_index_schema_sha256")
            != report["payload_index_schema_sha256"]
            or evidence.get("renderer_sha256") != report["renderer_sha256"]
            or evidence.get("rebuild_run_id") != report["run_id"]
            or evidence.get("source_snapshot_sha256")
            != report["source_snapshot_sha256"]
            or type(evidence.get("rebuild_point_count")) is not int
            or evidence["rebuild_point_count"] < 1
            or type(evidence.get("incremental_v3_point_count")) is not int
            or evidence["incremental_v3_point_count"] < 0
            or evidence["rebuild_point_count"]
            + evidence["incremental_v3_point_count"]
            != evidence.get("point_count")
        ):
            raise AliasCutoverError("rollback target lineage differs from rebuild")
        return
    expected = {
        "collection_fingerprint_sha256": report[
            "collection_fingerprint_sha256"
        ],
        "dimensions": report["dimensions"],
        "distance": report["distance"],
        "embedding_model": report["embedding_model"],
        "owner_count": report["owner_count"],
        "point_count": report["claim_count"],
        "point_inventory_sha256": report["point_inventory_sha256"],
        "payload_index_schema_sha256": report["payload_index_schema_sha256"],
        "renderer_sha256": report["renderer_sha256"],
        "rebuild_run_id": report["run_id"],
        "source_snapshot_sha256": report["source_snapshot_sha256"],
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise AliasCutoverError("target collection no longer matches rebuild report")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise AliasCutoverError("audit write failed")
        offset += written


def _validate_directory(descriptor: int, *, private: bool) -> None:
    info = os.fstat(descriptor)
    forbidden = 0o077 if private else 0o022
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & forbidden
    ):
        raise AliasCutoverError("audit directory rejected")


def _open_snapshot_parent(path: pathlib.Path, trusted_root: pathlib.Path) -> tuple[int, str]:
    try:
        relative = path.relative_to(trusted_root)
    except ValueError as exc:
        raise AliasCutoverError("audit path escaped trusted root") from exc
    if len(relative.parts) < 2 or any(part in {"", ".", ".."} for part in relative.parts):
        raise AliasCutoverError("audit path rejected")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(trusted_root, flags)
    try:
        _validate_directory(descriptor, private=False)
        for index, component in enumerate(relative.parts[:-1]):
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            _validate_directory(
                descriptor, private=index == len(relative.parts[:-1]) - 1
            )
        return descriptor, relative.parts[-1]
    except Exception:
        os.close(descriptor)
        raise


@dataclass
class AliasAudit:
    descriptor: int
    prior_event_sha256: str | None = None
    sequence: int = 0

    @classmethod
    def create(
        cls, path: pathlib.Path, *, trusted_root: pathlib.Path = SNAPSHOT_ROOT_PATH
    ) -> "AliasAudit":
        try:
            parent_descriptor, name = _open_snapshot_parent(path, trusted_root)
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_APPEND
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_descriptor,
                )
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except OSError as exc:
            raise AliasCutoverError("audit file creation failed") from exc
        return cls(descriptor=descriptor)

    def append(self, state: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        if state not in AUDIT_STATES:
            raise AliasCutoverError("audit state rejected")
        next_sequence = self.sequence + 1
        value = {
            **dict(fields),
            "audit_sequence": next_sequence,
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "prior_event_sha256": self.prior_event_sha256,
            "state": state,
        }
        event_sha256 = sha256_bytes(canonical_bytes(value))
        event = {**value, "event_sha256": event_sha256}
        opening_size = os.lseek(self.descriptor, 0, os.SEEK_END)
        try:
            _write_all(self.descriptor, canonical_bytes(event))
            os.fsync(self.descriptor)
        except Exception as exc:
            try:
                os.ftruncate(self.descriptor, opening_size)
                os.fsync(self.descriptor)
            except Exception as rollback_exc:
                raise AliasCutoverError(
                    "audit append durability is indeterminate"
                ) from rollback_exc
            raise AliasCutoverError("audit append failed without durable change") from exc
        self.sequence = next_sequence
        self.prior_event_sha256 = event_sha256
        return event

    def close(self) -> None:
        os.close(self.descriptor)


def _unrelated_aliases(aliases: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in aliases.items() if key != ACTIVE_ALIAS}


def _prior_alias_operations(before: Mapping[str, str]) -> list[Any]:
    operations: list[Any] = [
        qmodels.DeleteAliasOperation(
            delete_alias=qmodels.DeleteAlias(alias_name=ACTIVE_ALIAS)
        )
    ]
    if ACTIVE_ALIAS in before:
        operations.append(
            qmodels.CreateAliasOperation(
                create_alias=qmodels.CreateAlias(
                    collection_name=before[ACTIVE_ALIAS], alias_name=ACTIVE_ALIAS
                )
            )
        )
    return operations


def _bounded_failure_recovery(
    qdrant: Any,
    *,
    spec: Spec,
    before: Mapping[str, str],
    target: str,
    attempted: bool,
) -> tuple[str, dict[str, Any]]:
    fields: dict[str, Any] = {"postcondition_verified": False}
    try:
        aliases = observed_aliases(qdrant)
        fields["observed_alias_state_sha256"] = alias_state_sha256(aliases)
    except Exception:
        fields["observed_alias_state_sha256"] = None
        return "indeterminate", fields
    if aliases == dict(before):
        return "failed_no_change", fields
    if not attempted:
        return "indeterminate", fields
    target_applied = (
        aliases.get(ACTIVE_ALIAS) == target
        and _unrelated_aliases(aliases) == _unrelated_aliases(before)
    )
    if not target_applied:
        return "indeterminate", fields
    prior_source = before.get(ACTIVE_ALIAS)
    try:
        if prior_source is not None:
            source_state = current_source_state()
            if (
                source_state["projection_inventory_sha256"]
                != spec.value["expected_postgres_projection_inventory_sha256"]
                or source_state["source_snapshot_sha256"]
                != spec.value["expected_postgres_supported_snapshot_sha256"]
            ):
                raise AliasCutoverError("PostgreSQL source baseline changed")
            source_evidence = collection_evidence(
                qdrant, prior_source, require_rebuild_provenance=False
            )
            require_current_source_parity(source_evidence, source_state)
            if (
                source_evidence["collection_fingerprint_sha256"]
                != spec.value["expected_alias_source_fingerprint_sha256"]
            ):
                raise AliasCutoverError("prior alias source fingerprint changed")
        require_guard(spec)
        confirmed = observed_aliases(qdrant)
        if confirmed != aliases:
            raise AliasCutoverError("alias state changed before counter-transition")
        try:
            qdrant.update_collection_aliases(
                change_aliases_operations=_prior_alias_operations(before), timeout=30
            )
        except Exception:
            pass
        restored = observed_aliases(qdrant)
        if restored != dict(before):
            raise AliasCutoverError("prior alias state was not restored")
        if prior_source is not None:
            restored_state = current_source_state()
            if restored_state != source_state:
                raise AliasCutoverError("PostgreSQL source changed during counter-transition")
            restored_source = collection_evidence(
                qdrant, prior_source, require_rebuild_provenance=False
            )
            require_current_source_parity(restored_source, restored_state)
            if (
                restored_source["collection_fingerprint_sha256"]
                != spec.value["expected_alias_source_fingerprint_sha256"]
            ):
                raise AliasCutoverError("restored alias source fingerprint changed")
        fields.update(
            {
                "observed_alias_state_sha256": alias_state_sha256(restored),
                "postcondition_verified": True,
                "restored_alias_state_sha256": alias_state_sha256(restored),
            }
        )
        return "rolled_back_verified", fields
    except Exception:
        return "indeterminate", fields


def execute(spec: Spec) -> dict[str, Any]:
    if git_identity() != spec.value["expected_git"]:
        raise AliasCutoverError("Git identity changed")
    rebuild_report = load_rebuild_report(spec)
    qdrant = make_qdrant_client(url=spec.value["qdrant_url"], timeout=30.0)
    lock = qdrant_mutation_lock(exclusive=True, timeout_seconds=10.0)
    try:
        lock.acquire()
    except Exception:
        qdrant.close()
        raise
    try:
        collections = {item.name for item in qdrant.get_collections().collections}
        target = spec.value["target_collection"]
        if target not in collections:
            raise AliasCutoverError("target collection is absent")
        strict_target = (
            rebuild_report is not None and spec.value["operation"] != "rollback"
        )
        initial_evidence = collection_evidence(
            qdrant,
            target,
            require_rebuild_provenance=strict_target,
        )
        validate_target_evidence(spec, rebuild_report, initial_evidence)
        before = observed_aliases(qdrant)
        planned = operations(spec, before)
        source = before.get(ACTIVE_ALIAS)
        if spec.value["operation"] == "bootstrap":
            if source is not None:
                raise AliasCutoverError("bootstrap alias unexpectedly exists")
            source_evidence = None
        else:
            if source != spec.value["expected_alias_source"] or source not in collections:
                raise AliasCutoverError("alias source collection differs")
            source_evidence = collection_evidence(
                qdrant, source, require_rebuild_provenance=False
            )
            if (
                source_evidence["collection_fingerprint_sha256"]
                != spec.value["expected_alias_source_fingerprint_sha256"]
            ):
                raise AliasCutoverError("alias source fingerprint changed")
        source_state = current_source_state()
        if (
            source_state["projection_inventory_sha256"]
            != spec.value["expected_postgres_projection_inventory_sha256"]
            or source_state["source_snapshot_sha256"]
            != spec.value["expected_postgres_supported_snapshot_sha256"]
        ):
            raise AliasCutoverError("PostgreSQL source differs from the approved baseline")
        if rebuild_report is not None:
            if rebuild_report["expected_git"] != spec.value["expected_git"]:
                raise AliasCutoverError("rebuild Git identity differs from activation")
            if any(
                source_state.get(key) != rebuild_report[key]
                for key in ("claim_count", "owner_count", "source_snapshot_sha256")
            ):
                raise AliasCutoverError("current PostgreSQL source differs from rebuild")
        require_current_source_parity(initial_evidence, source_state)
        if source_evidence is not None:
            require_current_source_parity(source_evidence, source_state)
        common_fields = {
            "active_alias": ACTIVE_ALIAS,
            "before_alias_state_sha256": alias_state_sha256(before),
            "contract_version": REPORT_VERSION,
            "expected_alias_source": spec.value["expected_alias_source"],
            "expected_alias_source_fingerprint_sha256": spec.value[
                "expected_alias_source_fingerprint_sha256"
            ],
            "expected_git": spec.value["expected_git"],
            "lease": spec.value["lease"],
            "operation": spec.value["operation"],
            "rebuild_report_sha256": spec.value["rebuild_report_sha256"],
            "run_id": spec.value["run_id"],
            "spec_sha256": spec.sha256,
            "target_collection": target,
            "target_rebuild_expected_git": (
                rebuild_report["expected_git"] if rebuild_report is not None else None
            ),
            "target_rebuild_run_id": (
                rebuild_report["run_id"] if rebuild_report is not None else None
            ),
            "target_rebuild_spec_sha256": (
                rebuild_report["spec_sha256"] if rebuild_report is not None else None
            ),
            "target_collection_fingerprint_sha256": spec.value[
                "target_collection_fingerprint_sha256"
            ],
            "source_state": {
                key: value
                for key, value in source_state.items()
                if not key.startswith("_")
            },
        }
        require_guard(spec)
        audit = AliasAudit.create(
            pathlib.Path(spec.value["output_path"]), trusted_root=SNAPSHOT_ROOT_PATH
        )
        try:
            audit.append("prepared", common_fields)
            attempted = False
            try:
                confirmed_aliases = observed_aliases(qdrant)
                if confirmed_aliases != before:
                    raise AliasCutoverError("alias state changed before transition")
                confirmed_evidence = collection_evidence(
                    qdrant,
                    target,
                    require_rebuild_provenance=strict_target,
                )
                validate_target_evidence(spec, rebuild_report, confirmed_evidence)
                require_current_source_parity(confirmed_evidence, source_state)
                if source is not None:
                    confirmed_source = collection_evidence(
                        qdrant, source, require_rebuild_provenance=False
                    )
                    if (
                        confirmed_source["collection_fingerprint_sha256"]
                        != spec.value["expected_alias_source_fingerprint_sha256"]
                    ):
                        raise AliasCutoverError("alias source changed before transition")
                    require_current_source_parity(confirmed_source, source_state)
                if current_source_state() != source_state:
                    raise AliasCutoverError("PostgreSQL source changed before transition")
                require_guard(spec)
                attempted = True
                qdrant.update_collection_aliases(
                    change_aliases_operations=planned, timeout=30
                )
                after = observed_aliases(qdrant)
                if (
                    after.get(ACTIVE_ALIAS) != target
                    or _unrelated_aliases(after) != _unrelated_aliases(before)
                ):
                    raise AliasCutoverError("alias transition did not converge exactly")
                closing_evidence = collection_evidence(
                    qdrant,
                    target,
                    require_rebuild_provenance=strict_target,
                )
                validate_target_evidence(spec, rebuild_report, closing_evidence)
                closing_source_state = current_source_state()
                if closing_source_state != source_state:
                    raise AliasCutoverError("PostgreSQL source changed after transition")
                require_current_source_parity(closing_evidence, closing_source_state)
                completed = audit.append(
                    "completed",
                    {
                        **common_fields,
                        "after_alias_state_sha256": alias_state_sha256(after),
                        "postcondition_verified": True,
                    },
                )
                return completed
            except Exception as exc:
                state, outcome_fields = _bounded_failure_recovery(
                    qdrant,
                    spec=spec,
                    before=before,
                    target=target,
                    attempted=attempted,
                )
                try:
                    audit.append(state, {**common_fields, **outcome_fields})
                except Exception as audit_exc:
                    raise AliasCutoverError(
                        f"alias transition outcome:{state}; audit outcome:indeterminate"
                    ) from audit_exc
                raise AliasCutoverError(f"alias transition outcome:{state}") from exc
        finally:
            audit.close()
    finally:
        try:
            qdrant.close()
        finally:
            lock.release()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec")
    parser.add_argument("--spec-sha256")
    parser.add_argument("--execute-transition", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    if not args.execute_transition or not args.spec or not args.spec_sha256:
        print("alias transition execution disabled", file=sys.stderr)
        return 64
    stage = "spec"
    try:
        spec = Spec.load(pathlib.Path(args.spec), args.spec_sha256)
        stage = "transition"
        report = execute(spec)
        sys.stdout.buffer.write(canonical_bytes(report))
        return 0
    except AliasCutoverError:
        print(f"alias transition failed:{stage}", file=sys.stderr)
        return 2
    except Exception:
        print(f"alias transition failed:{stage}:internal", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
