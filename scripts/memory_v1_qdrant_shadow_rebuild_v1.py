#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import inspect
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
from typing import Any, Iterable, Mapping, Sequence


REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from openai import OpenAI
from qdrant_client.http import models as qmodels

from rag_engine.memory_v1_projection import (
    projection_renderer_sha256,
    render_claim_for_embedding,
)
from rag_engine.memory_v1_qdrant_rebuild_contract_v1 import (
    CONTRACT_VERSION,
    EMBEDDING_MODEL,
    NEGATIVE_QUERY_BANK,
    POINT_PROVENANCE_FIELDS,
    POINT_PROVENANCE_VERSION,
    REBUILD_PAYLOAD_INDEX_FIELDS,
    VECTOR_DIMENSIONS,
    QualityThresholdsV1,
    RebuildContractError,
    SupportedClaimSnapshotV1,
    canonical_bytes,
    collection_fingerprint_sha256,
    conversational_quality_report,
    conversational_queries,
    normalize_cosine_vector,
    point_payload,
    qdrant_mutation_lock,
    require_production_write_guard,
    sha256_bytes,
    source_snapshot_sha256,
    validate_production_write_lease,
    validate_shadow_collection,
)
from rag_engine.qdrant_compat import make_qdrant_client


SPEC_VERSION = "memory_v1_qdrant_shadow_rebuild_spec_v1"
REPORT_VERSION = "memory_v1_qdrant_shadow_rebuild_report_v1"
MAX_SOURCE_ROWS = 5_000
MAX_EMBEDDING_INPUTS_PER_REQUEST = 64
ALLOWED_SOURCE_TABLES = ("memory.claim", "memory.claim_revision")
SNAPSHOT_ROOT = pathlib.Path("/home/ubuntu/brains/snapshots")
EXPECTED_RENDERER_PATH = REPOSITORY / "rag_engine/memory_v1_projection.py"
EXPECTED_PAYLOAD_INDEX_SCHEMA = {
    field: "keyword" for field in sorted(REBUILD_PAYLOAD_INDEX_FIELDS)
}


class ShadowRebuildError(RuntimeError):
    pass


class ShadowRetainError(ShadowRebuildError):
    pass


@dataclass(frozen=True)
class Spec:
    value: dict[str, Any]
    raw: bytes

    @classmethod
    def load(cls, path: pathlib.Path, expected_sha256: str) -> "Spec":
        raw = path.read_bytes()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or sha256_bytes(raw) != expected_sha256
        ):
            raise ShadowRebuildError("spec digest mismatch")
        try:
            value = json.loads(raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ShadowRebuildError("spec parse failed") from exc
        if raw != canonical_bytes(value):
            raise ShadowRebuildError("spec is not canonical")
        expected = {
            "schema_version",
            "run_id",
            "expected_git",
            "expected_claim_count",
            "expected_owner_count",
            "expected_source_snapshot_sha256",
            "lease",
            "postgres_container",
            "database",
            "database_user",
            "renderer_path",
            "renderer_sha256",
            "embedding_model",
            "dimensions",
            "max_embedding_requests",
            "max_embedding_input_bytes",
            "max_embedding_input_tokens",
            "qdrant_url",
            "shadow_collection",
            "report_path",
            "quality_thresholds",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ShadowRebuildError("spec fields differ")
        if value.get("schema_version") != SPEC_VERSION:
            raise ShadowRebuildError("spec version differs")
        if not re.fullmatch(
            r"memory-qdrant-rebuild-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}",
            str(value.get("run_id") or ""),
        ):
            raise ShadowRebuildError("run id rejected")
        git = value.get("expected_git")
        if (
            not isinstance(git, dict)
            or set(git) != {"head", "tree"}
            or any(
                not re.fullmatch(r"[0-9a-f]{40}", str(item))
                for item in git.values()
            )
        ):
            raise ShadowRebuildError("Git identity rejected")
        if (
            type(value.get("expected_claim_count")) is not int
            or not 1 <= value["expected_claim_count"] <= MAX_SOURCE_ROWS
            or type(value.get("expected_owner_count")) is not int
            or not 1 <= value["expected_owner_count"] <= value["expected_claim_count"]
            or value.get("postgres_container") != "brains-postgres-1"
            or value.get("database") != "memory"
            or value.get("database_user") != "sage"
            or value.get("embedding_model") != EMBEDDING_MODEL
            or value.get("dimensions") != VECTOR_DIMENSIONS
            or value.get("qdrant_url") != "http://127.0.0.1:6333"
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(value.get("expected_source_snapshot_sha256") or ""),
            )
        ):
            raise ShadowRebuildError("source or target contract rejected")
        try:
            validate_production_write_lease(value.get("lease"))
        except RebuildContractError as exc:
            raise ShadowRebuildError("production-write lease rejected") from exc
        try:
            validate_shadow_collection(str(value.get("shadow_collection") or ""))
        except RebuildContractError as exc:
            raise ShadowRebuildError("shadow collection rejected") from exc
        if (
            type(value.get("max_embedding_requests")) is not int
            or not 1 <= value["max_embedding_requests"] <= 100
            or type(value.get("max_embedding_input_bytes")) is not int
            or not 1_024 <= value["max_embedding_input_bytes"] <= 100_000_000
            or type(value.get("max_embedding_input_tokens")) is not int
            or not 256 <= value["max_embedding_input_tokens"] <= 20_000_000
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("renderer_sha256") or ""))
        ):
            raise ShadowRebuildError("embedding bound rejected")
        renderer = pathlib.Path(str(value.get("renderer_path") or ""))
        report = pathlib.PurePosixPath(str(value.get("report_path") or ""))
        snapshot_root = pathlib.PurePosixPath(str(SNAPSHOT_ROOT))
        if (
            renderer.resolve() != EXPECTED_RENDERER_PATH.resolve()
            or ".." in renderer.parts
            or not report.is_absolute()
            or ".." in report.parts
            or snapshot_root not in report.parents
            or report.parent == snapshot_root
        ):
            raise ShadowRebuildError("path boundary rejected")
        thresholds = value.get("quality_thresholds")
        if not isinstance(thresholds, dict) or set(thresholds) != {
            "top1_ppm",
            "top5_ppm",
            "mrr_ppm",
            "minimum_margin_ppm",
            "positive_above_threshold_ppm",
        }:
            raise ShadowRebuildError("quality thresholds rejected")
        if any(type(item) is not int or not 0 <= item <= 1_000_000 for item in thresholds.values()):
            raise ShadowRebuildError("quality threshold value rejected")
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
        timeout=300,
    )
    if result.returncode != 0:
        raise ShadowRebuildError("bounded subprocess failed")
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
        raise ShadowRebuildError("candidate checkout is dirty")
    return {"head": head, "tree": tree}


def service_environment() -> dict[str, str]:
    pid = _run(
        ["/usr/bin/systemctl", "show", "-p", "MainPID", "--value", "brains.service"]
    ).strip().decode("ascii")
    if not pid.isdigit() or int(pid) <= 1:
        raise ShadowRebuildError("Brains service identity rejected")
    raw = (pathlib.Path("/proc") / pid / "environ").read_bytes()
    values: dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if separator and key in {b"OPENAI_API_KEY", b"OPENAI_BASE_URL", b"EMBED_MODEL"}:
            values[key.decode("ascii")] = value.decode("utf-8", "strict")
    if (
        not values.get("OPENAI_API_KEY")
        or values.get("OPENAI_BASE_URL") not in (None, "", "https://api.openai.com/v1")
        or values.get("EMBED_MODEL") != EMBEDDING_MODEL
    ):
        raise ShadowRebuildError("configured embedding provider rejected")
    return values


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
WHERE claim.status::text='supported'
GROUP BY claim.owner_user_id,claim.claim_id
ORDER BY claim.owner_user_id,claim.claim_id;
ROLLBACK;
"""


def _load_current_supported_claims(
    *, postgres_container: str, database_user: str, database: str
) -> list[SupportedClaimSnapshotV1]:
    raw = _run(
        [
            "/usr/bin/docker",
            "exec",
            "-i",
            postgres_container,
            "/usr/bin/psql",
            "-X",
            "--no-psqlrc",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            database_user,
            "-d",
            database,
        ],
        input_bytes=SOURCE_SQL.encode("utf-8"),
    ).decode("utf-8", "strict")
    rows = [json.loads(line) for line in raw.splitlines() if line.startswith("{")]
    if not rows or rows[0] != {
        "_kind": "proof",
        "isolation": "repeatable read",
        "read_only": "on",
    }:
        raise ShadowRebuildError("read-only snapshot proof rejected")
    claims: list[SupportedClaimSnapshotV1] = []
    for row in rows[1:]:
        if not isinstance(row, dict) or row.pop("_kind", None) != "claim":
            raise ShadowRebuildError("source row framing rejected")
        claims.append(SupportedClaimSnapshotV1.from_mapping(row))
    if not claims or len(claims) > MAX_SOURCE_ROWS:
        raise ShadowRebuildError("source aggregate inventory is empty or unbounded")
    return claims


def load_supported_claims(spec: Spec) -> list[SupportedClaimSnapshotV1]:
    claims = _load_current_supported_claims(
        postgres_container=spec.value["postgres_container"],
        database_user=spec.value["database_user"],
        database=spec.value["database"],
    )
    if (
        len(claims) != spec.value["expected_claim_count"]
        or len({claim.owner_user_id for claim in claims})
        != spec.value["expected_owner_count"]
    ):
        raise ShadowRebuildError("source aggregate inventory changed")
    return claims


def source_plan() -> dict[str, Any]:
    claims = _load_current_supported_claims(
        postgres_container="brains-postgres-1",
        database_user="sage",
        database="memory",
    )
    rendered = [render_claim_for_embedding(claim.source_record()) for claim in claims]
    queries = [query for claim in claims for query in conversational_queries(claim)]
    inputs = [*rendered, *queries, *NEGATIVE_QUERY_BANK]
    return {
        "claim_count": len(claims),
        "embedding_input_bytes": sum(len(item.encode("utf-8")) for item in inputs),
        "owner_count": len({claim.owner_user_id for claim in claims}),
        "renderer_sha256": loaded_renderer_sha256(),
        "required_embedding_request_count": math.ceil(
            len(inputs) / MAX_EMBEDDING_INPUTS_PER_REQUEST
        ),
        "source_snapshot_sha256": source_snapshot_sha256(claims),
    }


def loaded_renderer_sha256() -> str:
    loaded_digest = projection_renderer_sha256()
    disk_digest = sha256_bytes(EXPECTED_RENDERER_PATH.read_bytes())
    if (
        not re.fullmatch(r"[0-9a-f]{64}", loaded_digest)
        or loaded_digest != disk_digest
    ):
        raise ShadowRebuildError("loaded renderer differs from release bytes")
    return loaded_digest


def _vector(values: Iterable[float]) -> list[float]:
    vector = [float(value) for value in values]
    if len(vector) != VECTOR_DIMENSIONS or any(not math.isfinite(value) for value in vector):
        raise ShadowRebuildError("embedding vector rejected")
    return vector


def vector_sha256(vector: Sequence[float]) -> str:
    packed = struct.pack("<" + "f" * VECTOR_DIMENSIONS, *vector)
    return hashlib.sha256(packed).hexdigest()


def payload_index_schema(qdrant: Any, collection: str) -> dict[str, str]:
    info = qdrant.get_collection(collection_name=collection)
    raw = getattr(info, "payload_schema", None) or {}
    if not isinstance(raw, Mapping):
        raise ShadowRebuildError("payload index schema rejected")
    result: dict[str, str] = {}
    for field, schema in raw.items():
        value = getattr(schema, "data_type", schema)
        data_type = str(getattr(value, "value", value) or "").strip().lower()
        name = str(field)
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", name)
            or data_type
            not in {"keyword", "integer", "float", "geo", "text", "bool", "datetime", "uuid"}
        ):
            raise ShadowRebuildError("payload index schema rejected")
        result[name] = data_type
    return dict(sorted(result.items()))


def embed_inputs(
    client: OpenAI, inputs: Sequence[str], *, maximum_requests: int
) -> tuple[list[list[float]], int, int]:
    vectors: list[list[float]] = []
    requests = 0
    input_tokens = 0
    for offset in range(0, len(inputs), MAX_EMBEDDING_INPUTS_PER_REQUEST):
        if requests >= maximum_requests:
            raise ShadowRebuildError("embedding request bound exceeded")
        batch = list(inputs[offset : offset + MAX_EMBEDDING_INPUTS_PER_REQUEST])
        response = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        requests += 1
        ordered = sorted(response.data, key=lambda item: item.index)
        if (
            len(ordered) != len(batch)
            or [item.index for item in ordered] != list(range(len(batch)))
        ):
            raise ShadowRebuildError("embedding response count differs")
        vectors.extend(_vector(item.embedding) for item in ordered)
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        if type(prompt_tokens) is not int or prompt_tokens < 1:
            raise ShadowRebuildError("embedding usage prompt token count rejected")
        input_tokens += prompt_tokens
    if len(vectors) != len(inputs):
        raise ShadowRebuildError("embedding total count differs")
    return vectors, requests, input_tokens


def owner_filter(owner: uuid.UUID) -> qmodels.Filter:
    return qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="owner_user_id", match=qmodels.MatchValue(value=str(owner))
            ),
            qmodels.FieldCondition(
                key="status", match=qmodels.MatchValue(value="supported")
            ),
        ]
    )


def exact_point_inventory(
    qdrant: Any, collection: str
) -> tuple[list[tuple[str, str, str]], str, str]:
    info = qdrant.get_collection(collection_name=collection)
    config = _model_attribute(info, "config")
    params = _model_attribute(config, "params")
    vectors = _model_attribute(params, "vectors")
    size = _model_attribute(vectors, "size")
    distance = str(
        getattr(
            _model_attribute(vectors, "distance"),
            "value",
            _model_attribute(vectors, "distance"),
        )
        or ""
    ).lower()
    if size != VECTOR_DIMENSIONS or distance != "dot":
        raise ShadowRebuildError("shadow vector configuration differs")
    payload_indexes = payload_index_schema(qdrant, collection)
    if payload_indexes != EXPECTED_PAYLOAD_INDEX_SCHEMA:
        raise ShadowRebuildError("shadow payload index schema differs")
    offset: Any = None
    inventory: list[tuple[str, str, str]] = []
    fingerprint_points: list[dict[str, Any]] = []
    while True:
        points, offset = qdrant.scroll(
            collection_name=collection,
            scroll_filter=None,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for point in points:
            payload = point.payload or {}
            if (
                not isinstance(payload, Mapping)
                or set(payload) != POINT_PROVENANCE_FIELDS
                or payload.get("schema_version") != POINT_PROVENANCE_VERSION
            ):
                raise ShadowRebuildError("point provenance version differs")
            stored_vector_sha256 = vector_sha256(point.vector or [])
            if stored_vector_sha256 != payload.get("vector_sha256"):
                raise ShadowRebuildError("stored vector digest differs from provenance")
            inventory.append(
                (
                    str(point.id),
                    str(payload.get("owner_user_id") or ""),
                    str(payload.get("vector_sha256") or ""),
                )
            )
            fingerprint_points.append(
                {
                    "id": str(point.id),
                    "payload": dict(payload),
                    "stored_vector_sha256": stored_vector_sha256,
                }
            )
        if offset is None:
            break
    ordered = sorted(inventory)
    fingerprint = collection_fingerprint_sha256(
        collection=collection,
        dimensions=VECTOR_DIMENSIONS,
        distance="dot",
        points=fingerprint_points,
        payload_indexes=payload_indexes,
    )
    return ordered, fingerprint, sha256_bytes(canonical_bytes(payload_indexes))


def _retrieved_point(qdrant: Any, collection: str, point_id: str) -> Any | None:
    records = qdrant.retrieve(
        collection_name=collection,
        ids=[point_id],
        with_payload=True,
        with_vectors=True,
    )
    if not isinstance(records, list) or len(records) > 1:
        raise ShadowRebuildError("lifecycle point retrieval rejected")
    return records[0] if records else None


def _require_exact_point(
    record: Any | None,
    *,
    point_id: str,
    payload: Mapping[str, Any],
    vector: Sequence[float],
) -> None:
    if record is None:
        raise ShadowRebuildError("lifecycle point is absent")
    observed_payload = _model_attribute(record, "payload") or {}
    observed_vector = _model_attribute(record, "vector") or []
    if (
        str(_model_attribute(record, "id") or "") != point_id
        or not isinstance(observed_payload, Mapping)
        or dict(observed_payload) != dict(payload)
        or vector_sha256(observed_vector) != vector_sha256(vector)
    ):
        raise ShadowRebuildError("lifecycle point identity differs")


def reversible_lifecycle_gate(
    qdrant: Any,
    collection: str,
    point: Any,
    spec: Spec,
    *,
    original_inventory: Sequence[tuple[str, str, str]],
    original_fingerprint: str,
    original_payload_index_sha256: str,
) -> int:
    point_id = str(_model_attribute(point, "id") or "")
    original_payload = dict(_model_attribute(point, "payload") or {})
    original_vector = list(_model_attribute(point, "vector") or [])
    if (
        not point_id
        or set(original_payload) != POINT_PROVENANCE_FIELDS
        or len(original_vector) != VECTOR_DIMENSIONS
    ):
        raise ShadowRebuildError("lifecycle opening point rejected")
    temporary_vector = list(original_vector)
    temporary_vector[0] = temporary_vector[0] + 0.000_001
    try:
        temporary_vector = normalize_cosine_vector(
            temporary_vector, dimensions=VECTOR_DIMENSIONS
        )
    except RebuildContractError as exc:
        raise ShadowRebuildError("lifecycle vector normalization failed") from exc
    temporary_payload = dict(original_payload)
    temporary_payload["revision_number"] = int(original_payload["revision_number"]) + 1
    temporary_payload["source_sha256"] = sha256_bytes(
        canonical_bytes(
            {
                "contract_version": "memory_v1_qdrant_reversible_lifecycle_v1",
                "point_id_sha256": sha256_bytes(point_id.encode("utf-8")),
                "prior_source_sha256": original_payload["source_sha256"],
            }
        )
    )
    temporary_payload["vector_sha256"] = vector_sha256(temporary_vector)

    def restore_original() -> None:
        require_production_write_guard(spec.value["lease"])
        qdrant.upsert(
            collection_name=collection,
            wait=True,
            points=[
                qmodels.PointStruct(
                    id=point_id,
                    vector=original_vector,
                    payload=original_payload,
                )
            ],
        )
        _require_exact_point(
            _retrieved_point(qdrant, collection, point_id),
            point_id=point_id,
            payload=original_payload,
            vector=original_vector,
        )

    try:
        require_production_write_guard(spec.value["lease"])
        qdrant.upsert(
            collection_name=collection,
            wait=True,
            points=[
                qmodels.PointStruct(
                    id=point_id,
                    vector=temporary_vector,
                    payload=temporary_payload,
                )
            ],
        )
        _require_exact_point(
            _retrieved_point(qdrant, collection, point_id),
            point_id=point_id,
            payload=temporary_payload,
            vector=temporary_vector,
        )
        restore_original()
        require_production_write_guard(spec.value["lease"])
        qdrant.delete(
            collection_name=collection,
            wait=True,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="owner_user_id",
                            match=qmodels.MatchValue(
                                value=str(original_payload["owner_user_id"])
                            ),
                        ),
                        qmodels.HasIdCondition(has_id=[point_id]),
                    ]
                )
            ),
        )
        if _retrieved_point(qdrant, collection, point_id) is not None:
            raise ShadowRebuildError("lifecycle owner-bound deletion did not converge")
        restore_original()
        inventory, fingerprint, payload_index_sha256 = exact_point_inventory(
            qdrant, collection
        )
        if (
            list(inventory) != list(original_inventory)
            or fingerprint != original_fingerprint
            or payload_index_sha256 != original_payload_index_sha256
        ):
            raise ShadowRebuildError("lifecycle restoration differs from opening")
        return 4
    except Exception as exc:
        try:
            restore_original()
            inventory, fingerprint, payload_index_sha256 = exact_point_inventory(
                qdrant, collection
            )
            if (
                list(inventory) != list(original_inventory)
                or fingerprint != original_fingerprint
                or payload_index_sha256 != original_payload_index_sha256
            ):
                raise ShadowRebuildError("lifecycle emergency restoration differs")
        except Exception as restoration_error:
            raise ShadowRetainError(
                "lifecycle restoration is unverified; retain shadow"
            ) from restoration_error
        raise ShadowRebuildError("reversible lifecycle gate failed") from exc


def evaluate(
    qdrant: Any,
    collection: str,
    claims: Sequence[SupportedClaimSnapshotV1],
    query_vectors: Sequence[Sequence[float]],
    negative_vectors: Sequence[Sequence[float]],
    thresholds: QualityThresholdsV1,
) -> tuple[dict[str, Any], int, int]:
    expected_query_count = len(claims) * 3
    if len(query_vectors) != expected_query_count:
        raise ShadowRebuildError("conversational query vector count differs")
    ranks: list[int | None] = []
    scores: list[float] = []
    cross_owner_checks = 0
    owners = sorted({claim.owner_user_id for claim in claims}, key=lambda item: item.bytes)
    for index, claim in enumerate(claims):
        for query_vector in query_vectors[index * 3 : index * 3 + 3]:
            try:
                normalized_query = normalize_cosine_vector(
                    query_vector, dimensions=VECTOR_DIMENSIONS
                )
            except RebuildContractError as exc:
                raise ShadowRebuildError("query vector normalization failed") from exc
            hits = qdrant.search(
                collection_name=collection,
                query_vector=normalized_query,
                query_filter=owner_filter(claim.owner_user_id),
                limit=20,
                with_payload=True,
                with_vectors=False,
            )
            target = [
                (rank + 1, float(hit.score))
                for rank, hit in enumerate(hits)
                if str(hit.id) == str(claim.claim_id)
            ]
            ranks.append(target[0][0] if target else None)
            scores.append(target[0][1] if target else 0.0)
            for other_owner in owners:
                if other_owner == claim.owner_user_id:
                    continue
                wrong = qdrant.search(
                    collection_name=collection,
                    query_vector=normalized_query,
                    query_filter=owner_filter(other_owner),
                    limit=20,
                    with_payload=True,
                    with_vectors=False,
                )
                cross_owner_checks += 1
                if any(str(hit.id) == str(claim.claim_id) for hit in wrong):
                    raise ShadowRebuildError("cross-owner retrieval exposed a claim")
    negative_scores: list[float] = []
    for query_vector in negative_vectors:
        try:
            normalized_query = normalize_cosine_vector(
                query_vector, dimensions=VECTOR_DIMENSIONS
            )
        except RebuildContractError as exc:
            raise ShadowRebuildError("negative query vector normalization failed") from exc
        for owner in owners:
            hits = qdrant.search(
                collection_name=collection,
                query_vector=normalized_query,
                query_filter=owner_filter(owner),
                limit=1,
                with_payload=False,
                with_vectors=False,
            )
            negative_scores.append(float(hits[0].score) if hits else 0.0)
    report = conversational_quality_report(
        ranks=ranks,
        target_scores=scores,
        negative_scores=negative_scores,
        thresholds=thresholds,
    )
    if not report["accepted"]:
        raise ShadowRebuildError("conversational retrieval quality gate failed")
    return report, cross_owner_checks, len(negative_scores)


def _validate_snapshot_directory(descriptor: int, *, private: bool) -> None:
    info = os.fstat(descriptor)
    forbidden = 0o077 if private else 0o022
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & forbidden
    ):
        raise ShadowRebuildError("snapshot directory rejected")


def _open_snapshot_parent(
    path: pathlib.Path, trusted_root: pathlib.Path
) -> tuple[int, str]:
    try:
        relative = path.relative_to(trusted_root)
    except ValueError as exc:
        raise ShadowRebuildError("report path escaped snapshot root") from exc
    if len(relative.parts) < 2 or any(part in {"", ".", ".."} for part in relative.parts):
        raise ShadowRebuildError("report path rejected")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(trusted_root, flags)
    except OSError as exc:
        raise ShadowRebuildError("snapshot root open failed") from exc
    try:
        _validate_snapshot_directory(descriptor, private=False)
        for index, component in enumerate(relative.parts[:-1]):
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            _validate_snapshot_directory(
                descriptor, private=index == len(relative.parts[:-1]) - 1
            )
        return descriptor, relative.parts[-1]
    except OSError as exc:
        os.close(descriptor)
        raise ShadowRebuildError("snapshot directory traversal failed") from exc
    except Exception:
        os.close(descriptor)
        raise


def write_new_private(
    path: pathlib.Path,
    value: Mapping[str, Any],
    *,
    trusted_root: pathlib.Path = SNAPSHOT_ROOT,
) -> None:
    parent_descriptor, name = _open_snapshot_parent(path, trusted_root)
    try:
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ShadowRebuildError("report file creation failed") from exc
        try:
            payload = canonical_bytes(dict(value))
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise ShadowRebuildError("report write failed")
                offset += written
            os.fsync(descriptor)
            os.fsync(parent_descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _model_attribute(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def require_completed_update(result: Any, operation: str) -> None:
    status = _model_attribute(result, "status")
    status_value = getattr(status, "value", status)
    if result is True or str(status_value or "").lower() == "completed":
        return
    raise ShadowRebuildError(f"{operation} was not completed")


def cleanup_source_manifest(
    claims: Sequence[SupportedClaimSnapshotV1], spec: Spec
) -> frozenset[tuple[str, str, str]]:
    manifest = frozenset(
        (
            str(claim.claim_id),
            str(claim.owner_user_id),
            claim.source_sha256(),
        )
        for claim in claims
    )
    if (
        len(manifest) != spec.value["expected_claim_count"]
        or len({owner for _, owner, _ in manifest})
        != spec.value["expected_owner_count"]
    ):
        raise ShadowRebuildError("cleanup source manifest differs")
    return manifest


def current_cleanup_source_manifest(spec: Spec) -> frozenset[tuple[str, str, str]]:
    return cleanup_source_manifest(load_supported_claims(spec), spec)


def cleanup_point_manifest(
    points: Sequence[qmodels.PointStruct], spec: Spec
) -> frozenset[tuple[str, str, str, str, str]]:
    manifest: set[tuple[str, str, str, str, str]] = set()
    for point in points:
        payload = _model_attribute(point, "payload")
        vector = _model_attribute(point, "vector")
        point_id = str(_model_attribute(point, "id") or "")
        if not isinstance(payload, Mapping) or vector is None:
            raise ShadowRebuildError("cleanup point manifest rejected")
        stored_vector_sha256 = vector_sha256(vector)
        if (
            str(payload.get("claim_id") or "") != point_id
            or str(payload.get("vector_sha256") or "") != stored_vector_sha256
        ):
            raise ShadowRebuildError("cleanup point manifest rejected")
        manifest.add(
            (
                point_id,
                str(payload.get("owner_user_id") or ""),
                str(payload.get("source_sha256") or ""),
                sha256_bytes(canonical_bytes(dict(payload))),
                stored_vector_sha256,
            )
        )
    if (
        len(manifest) != spec.value["expected_claim_count"]
        or len({owner for _, owner, _, _, _ in manifest})
        != spec.value["expected_owner_count"]
    ):
        raise ShadowRebuildError("cleanup point manifest differs")
    return frozenset(manifest)


def _cleanup_identity(
    qdrant: Any,
    collection: str,
    spec: Spec,
    expected_manifest: frozenset[tuple[str, str, str, str, str]],
    expected_payload_indexes: Mapping[str, str],
) -> dict[str, Any]:
    names = {str(item.name) for item in qdrant.get_collections().collections}
    if collection not in names:
        raise ShadowRebuildError("cleanup collection is absent")
    aliases = qdrant.get_aliases().aliases
    if any(str(item.collection_name) == collection for item in aliases):
        raise ShadowRebuildError("cleanup collection has an alias")
    info = qdrant.get_collection(collection_name=collection)
    config = _model_attribute(info, "config")
    params = _model_attribute(config, "params")
    vectors = _model_attribute(params, "vectors")
    size = _model_attribute(vectors, "size")
    distance = str(
        getattr(_model_attribute(vectors, "distance"), "value", _model_attribute(vectors, "distance"))
        or ""
    ).lower()
    if size != VECTOR_DIMENSIONS or distance != "dot":
        raise ShadowRebuildError("cleanup collection configuration differs")
    reported_count = _model_attribute(info, "points_count")
    if type(reported_count) is not int or not 0 <= reported_count <= len(expected_manifest):
        raise ShadowRebuildError("cleanup reported point count differs")
    payload_indexes = payload_index_schema(qdrant, collection)
    if (
        any(
            field not in EXPECTED_PAYLOAD_INDEX_SCHEMA
            or data_type != EXPECTED_PAYLOAD_INDEX_SCHEMA[field]
            for field, data_type in expected_payload_indexes.items()
        )
        or payload_indexes != dict(sorted(expected_payload_indexes.items()))
    ):
        raise ShadowRebuildError("cleanup payload index schema differs")
    offset: Any = None
    points: list[tuple[str, str, str, str, str]] = []
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
            payload = _model_attribute(record, "payload") or {}
            point_id = str(_model_attribute(record, "id") or "")
            vector = _model_attribute(record, "vector")
            try:
                uuid.UUID(point_id)
                uuid.UUID(str(payload.get("claim_id")))
                uuid.UUID(str(payload.get("owner_user_id")))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ShadowRebuildError("cleanup point identity differs") from exc
            if (
                not point_id
                or not isinstance(payload, Mapping)
                or set(payload) != POINT_PROVENANCE_FIELDS
                or str(payload.get("claim_id")) != point_id
                or payload.get("schema_version") != POINT_PROVENANCE_VERSION
                or payload.get("status") != "supported"
                or payload.get("dimensions") != VECTOR_DIMENSIONS
                or payload.get("rebuild_run_id") != spec.value["run_id"]
                or payload.get("embedding_model") != EMBEDDING_MODEL
                or payload.get("renderer_sha256") != spec.value["renderer_sha256"]
                or payload.get("source_snapshot_sha256")
                != spec.value["expected_source_snapshot_sha256"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("source_sha256") or ""))
                or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("vector_sha256") or ""))
            ):
                raise ShadowRebuildError("cleanup point identity differs")
            stored_vector_sha256 = vector_sha256(vector or [])
            if stored_vector_sha256 != payload["vector_sha256"]:
                raise ShadowRebuildError("cleanup stored vector digest differs")
            exact_identity = (
                point_id,
                str(payload["owner_user_id"]),
                str(payload["source_sha256"]),
                sha256_bytes(canonical_bytes(dict(payload))),
                stored_vector_sha256,
            )
            if exact_identity not in expected_manifest:
                raise ShadowRebuildError("cleanup point differs from the built point")
            points.append(exact_identity)
            if len(points) > spec.value["expected_claim_count"]:
                raise ShadowRebuildError("cleanup point count exceeds source")
        if next_offset is None:
            break
        marker = repr(next_offset)
        if marker in seen_offsets:
            raise ShadowRebuildError("cleanup pagination repeated")
        seen_offsets.add(marker)
        offset = next_offset
    if len({point_id for point_id, _, _, _, _ in points}) != len(points):
        raise ShadowRebuildError("cleanup point identifiers repeat")
    if reported_count != len(points):
        raise ShadowRebuildError("cleanup scanned point count differs")
    return {
        "aliases": sorted(
            (str(item.alias_name), str(item.collection_name)) for item in aliases
        ),
        "collection": collection,
        "payload_indexes": payload_indexes,
        "points": sorted(points),
        "reported_count": reported_count,
    }


def remove_created_shadow(
    qdrant: Any,
    collection: str,
    spec: Spec,
    expected_manifest: frozenset[tuple[str, str, str, str, str]],
    opening_source_manifest: frozenset[tuple[str, str, str]],
    expected_payload_indexes: Mapping[str, str],
    *,
    lock_held: bool = False,
) -> None:
    try:
        validate_shadow_collection(collection)
    except RebuildContractError as exc:
        raise ShadowRebuildError("cleanup target rejected") from exc
    lock = None
    if not lock_held:
        lock = qdrant_mutation_lock(exclusive=True, timeout_seconds=10.0)
        try:
            lock.acquire()
        except Exception as exc:
            raise ShadowRebuildError("shadow cleanup lock unavailable") from exc
    try:
        if current_cleanup_source_manifest(spec) != opening_source_manifest:
            raise ShadowRebuildError("cleanup PostgreSQL source changed")
        opening = _cleanup_identity(
            qdrant,
            collection,
            spec,
            expected_manifest,
            expected_payload_indexes,
        )
        require_production_write_guard(spec.value["lease"])
        closing_identity = _cleanup_identity(
            qdrant,
            collection,
            spec,
            expected_manifest,
            expected_payload_indexes,
        )
        if closing_identity != opening:
            raise ShadowRebuildError("cleanup identity changed")
        if current_cleanup_source_manifest(spec) != opening_source_manifest:
            raise ShadowRebuildError("cleanup PostgreSQL source changed")
        require_production_write_guard(spec.value["lease"])
        removed = qdrant.delete_collection(collection_name=collection)
        if removed is False:
            raise ShadowRebuildError("shadow cleanup returned false")
        closing = {item.name for item in qdrant.get_collections().collections}
        if any(
            str(item.collection_name) == collection
            for item in qdrant.get_aliases().aliases
        ):
            raise ShadowRebuildError("shadow cleanup alias remains")
    except Exception as exc:
        raise ShadowRebuildError("shadow cleanup unverified") from exc
    finally:
        if lock is not None:
            lock.release()
    if collection in closing:
        raise ShadowRebuildError("shadow cleanup unverified")


def reconcile_ambiguous_create(
    qdrant: Any,
    collection: str,
    spec: Spec,
    expected_manifest: frozenset[tuple[str, str, str]],
) -> bool:
    names = {str(item.name) for item in qdrant.get_collections().collections}
    if collection not in names:
        return False
    if current_cleanup_source_manifest(spec) != expected_manifest:
        raise ShadowRebuildError("ambiguous creation PostgreSQL source changed")
    opening = _cleanup_identity(
        qdrant, collection, spec, frozenset(), {}
    )
    if opening["points"] or opening["reported_count"] != 0:
        raise ShadowRebuildError("ambiguous shadow creation is not empty")
    require_production_write_guard(spec.value["lease"])
    closing = _cleanup_identity(
        qdrant, collection, spec, frozenset(), {}
    )
    if closing != opening:
        raise ShadowRebuildError("ambiguous shadow creation identity changed")
    if current_cleanup_source_manifest(spec) != expected_manifest:
        raise ShadowRebuildError("ambiguous creation PostgreSQL source changed")
    raise ShadowRetainError(
        "ambiguous shadow creation is present; retain collection for review"
    )


def execute(spec: Spec) -> dict[str, Any]:
    if git_identity() != spec.value["expected_git"]:
        raise ShadowRebuildError("Git identity changed")
    renderer_path = pathlib.Path(spec.value["renderer_path"])
    imported_renderer = pathlib.Path(
        inspect.getsourcefile(render_claim_for_embedding) or ""
    ).resolve()
    if (
        renderer_path.resolve() != EXPECTED_RENDERER_PATH.resolve()
        or imported_renderer != EXPECTED_RENDERER_PATH.resolve()
        or loaded_renderer_sha256() != spec.value["renderer_sha256"]
    ):
        raise ShadowRebuildError("renderer identity changed")
    claims = load_supported_claims(spec)
    source_manifest = cleanup_source_manifest(claims, spec)
    snapshot_sha = source_snapshot_sha256(claims)
    if snapshot_sha != spec.value["expected_source_snapshot_sha256"]:
        raise ShadowRebuildError("source snapshot differs from the approved source")
    rendered = [render_claim_for_embedding(claim.source_record()) for claim in claims]
    queries = [query for claim in claims for query in conversational_queries(claim)]
    inputs = [*rendered, *queries, *NEGATIVE_QUERY_BANK]
    input_bytes = sum(len(item.encode("utf-8")) for item in inputs)
    if input_bytes > spec.value["max_embedding_input_bytes"]:
        raise ShadowRebuildError("embedding input byte bound exceeded")
    required_requests = math.ceil(len(inputs) / MAX_EMBEDDING_INPUTS_PER_REQUEST)
    if required_requests != spec.value["max_embedding_requests"]:
        raise ShadowRebuildError("embedding request plan differs")
    require_production_write_guard(spec.value["lease"])
    environment = service_environment()
    provider = OpenAI(
        api_key=environment["OPENAI_API_KEY"],
        base_url="https://api.openai.com/v1",
        max_retries=0,
        timeout=30.0,
    )
    vectors, request_count, input_tokens = embed_inputs(
        provider, inputs, maximum_requests=spec.value["max_embedding_requests"]
    )
    if input_tokens > spec.value["max_embedding_input_tokens"]:
        raise ShadowRebuildError("embedding input token bound exceeded")
    try:
        claim_vectors = [
            normalize_cosine_vector(vector, dimensions=VECTOR_DIMENSIONS)
            for vector in vectors[: len(claims)]
        ]
    except RebuildContractError as exc:
        raise ShadowRebuildError("embedding vector normalization failed") from exc
    query_vectors = vectors[len(claims) : len(claims) + len(queries)]
    negative_vectors = vectors[-len(NEGATIVE_QUERY_BANK) :]
    qdrant = make_qdrant_client(url=spec.value["qdrant_url"], timeout=30.0)
    mutation_lock = qdrant_mutation_lock(exclusive=True, timeout_seconds=10.0)
    try:
        mutation_lock.acquire()
    except Exception:
        qdrant.close()
        raise
    collection = spec.value["shadow_collection"]
    created = False
    created_index_fields: set[str] = set()
    cleanup_manifest: frozenset[tuple[str, str, str, str, str]] = frozenset()
    try:
        names = {item.name for item in qdrant.get_collections().collections}
        if collection in names:
            raise ShadowRebuildError("shadow collection already exists")
        require_production_write_guard(spec.value["lease"])
        creation_failure: Exception | None = None
        try:
            created_result = qdrant.create_collection(
                collection_name=collection,
                vectors_config=qmodels.VectorParams(
                    size=VECTOR_DIMENSIONS,
                    distance=qmodels.Distance.DOT,
                    on_disk=False,
                ),
                on_disk_payload=True,
            )
            if created_result is not True:
                creation_failure = ShadowRebuildError(
                    "shadow collection creation was not acknowledged"
                )
        except Exception as exc:
            creation_failure = exc
        if creation_failure is not None:
            reconcile_ambiguous_create(qdrant, collection, spec, source_manifest)
            raise ShadowRebuildError("shadow collection creation was ambiguous") from creation_failure
        created = True
        for field in (
            "owner_user_id",
            "status",
            "sensitivity",
            "domains",
            "intents",
            "embedding_model",
            "rebuild_run_id",
            "renderer_sha256",
            "source_snapshot_sha256",
        ):
            require_production_write_guard(spec.value["lease"])
            index_result = qdrant.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
                wait=True,
            )
            require_completed_update(index_result, "payload index creation")
            created_index_fields.add(field)
        if payload_index_schema(qdrant, collection) != EXPECTED_PAYLOAD_INDEX_SCHEMA:
            raise ShadowRebuildError("payload index schema differs after creation")
        points: list[qmodels.PointStruct] = []
        for claim, vector in zip(claims, claim_vectors, strict=True):
            digest = vector_sha256(vector)
            points.append(
                qmodels.PointStruct(
                    id=str(claim.claim_id),
                    vector=vector,
                    payload=point_payload(
                        claim,
                        renderer_sha256=spec.value["renderer_sha256"],
                        rebuild_run_id=spec.value["run_id"],
                        vector_sha256=digest,
                        source_snapshot_sha256_value=snapshot_sha,
                    ),
                )
            )
        cleanup_manifest = cleanup_point_manifest(points, spec)
        for offset in range(0, len(points), 32):
            require_production_write_guard(spec.value["lease"])
            qdrant.upsert(
                collection_name=collection,
                wait=True,
                points=points[offset : offset + 32],
            )
        inventory, collection_fingerprint, payload_index_schema_sha256 = (
            exact_point_inventory(qdrant, collection)
        )
        expected_ids = {str(claim.claim_id) for claim in claims}
        if len(inventory) != len(claims) or {item[0] for item in inventory} != expected_ids:
            raise ShadowRebuildError("shadow point inventory differs")
        thresholds = QualityThresholdsV1(**spec.value["quality_thresholds"])
        quality, isolation_checks, negative_checks = evaluate(
            qdrant,
            collection,
            claims,
            query_vectors,
            negative_vectors,
            thresholds,
        )
        lifecycle_point = _retrieved_point(qdrant, collection, str(points[0].id))
        lifecycle_checks = reversible_lifecycle_gate(
            qdrant,
            collection,
            lifecycle_point,
            spec,
            original_inventory=inventory,
            original_fingerprint=collection_fingerprint,
            original_payload_index_sha256=payload_index_schema_sha256,
        )
        closing_claims = load_supported_claims(spec)
        if (
            source_snapshot_sha256(closing_claims) != snapshot_sha
            or snapshot_sha != spec.value["expected_source_snapshot_sha256"]
        ):
            raise ShadowRebuildError("source changed during shadow rebuild")
        report = {
            "claim_count": len(claims),
            "collection": collection,
            "collection_fingerprint_sha256": collection_fingerprint,
            "contract_version": REPORT_VERSION,
            "cross_owner_check_count": isolation_checks,
            "dimensions": VECTOR_DIMENSIONS,
            "distance": "dot",
            "embedding_input_bytes": input_bytes,
            "embedding_input_tokens": input_tokens,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_request_count": request_count,
            "expected_git": spec.value["expected_git"],
            "lifecycle_check_count": lifecycle_checks,
            "negative_check_count": negative_checks,
            "owner_count": len({claim.owner_user_id for claim in claims}),
            "payload_index_schema_sha256": payload_index_schema_sha256,
            "point_inventory_sha256": sha256_bytes(canonical_bytes(inventory)),
            "production_postgres_read_only": True,
            "quality": quality,
            "renderer_sha256": spec.value["renderer_sha256"],
            "run_id": spec.value["run_id"],
            "source_snapshot_sha256": snapshot_sha,
            "spec_sha256": sha256_bytes(spec.raw),
        }
        require_production_write_guard(spec.value["lease"])
        write_new_private(pathlib.Path(spec.value["report_path"]), report)
        return report
    except ShadowRetainError:
        raise
    except Exception:
        if created:
            try:
                remove_created_shadow(
                    qdrant,
                    collection,
                    spec,
                    cleanup_manifest,
                    source_manifest,
                    {
                        field: EXPECTED_PAYLOAD_INDEX_SCHEMA[field]
                        for field in sorted(created_index_fields)
                    },
                    lock_held=True,
                )
            except Exception as cleanup_failure:
                raise ShadowRebuildError("shadow cleanup unverified") from cleanup_failure
        raise
    finally:
        mutation_lock.release()
        qdrant.close()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec")
    parser.add_argument("--spec-sha256")
    parser.add_argument("--execute-build", action="store_true")
    parser.add_argument("--source-plan", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    if args.source_plan:
        if args.execute_build or args.spec or args.spec_sha256:
            print("source plan arguments rejected", file=sys.stderr)
            return 64
        try:
            sys.stdout.buffer.write(canonical_bytes(source_plan()))
            return 0
        except Exception:
            print("source plan failed", file=sys.stderr)
            return 2
    if not args.execute_build or not args.spec or not args.spec_sha256:
        print("shadow rebuild execution disabled", file=sys.stderr)
        return 64
    stage = "spec"
    try:
        spec = Spec.load(pathlib.Path(args.spec), args.spec_sha256)
        stage = "build"
        report = execute(spec)
        sys.stdout.buffer.write(canonical_bytes(report))
        return 0
    except (ShadowRebuildError, RebuildContractError):
        print(f"shadow rebuild failed:{stage}", file=sys.stderr)
        return 2
    except Exception:
        print(f"shadow rebuild failed:{stage}:internal", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
