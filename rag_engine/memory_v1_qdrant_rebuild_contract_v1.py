from __future__ import annotations

import hashlib
import errno
import fcntl
import json
import math
import os
import pathlib
import re
import stat
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


CONTRACT_VERSION = "memory_v1_qdrant_rebuild_contract_v1"
SOURCE_SNAPSHOT_VERSION = "memory_v1_qdrant_source_snapshot_v1"
POINT_PROVENANCE_VERSION = "memory_claim_projection_v2"
INCREMENTAL_POINT_PROVENANCE_VERSION_V3 = "memory_claim_projection_v3"
QUALITY_VERSION = "memory_v1_qdrant_conversational_quality_v1"
EMBEDDING_MODEL = "text-embedding-3-large"
VECTOR_DIMENSIONS = 3072
SHADOW_PREFIX = "memory_claim_v1_shadow_rebuild_"
ACTIVE_ALIAS = "memory_claim_v1_active"
PRODUCTION_WORKTREE = pathlib.Path("/opt/chat-memory")
LEASE_GUARD = pathlib.Path(
    "/var/lib/chat-memory-change-leases-v1/control/bin/chat_memory_lease_guard.py"
)
LEASE_EVENTS_ROOT = pathlib.Path("/var/lib/chat-memory-change-leases-v1/events")
QDRANT_MUTATION_LOCK_PATH = pathlib.Path(
    "/run/lock/chat-memory-qdrant-alias-v1.lock"
)
LEASE_FIELDS = frozenset(
    {
        "lease_id",
        "task_id",
        "thread_id",
        "acquire_event_sha256",
        "registry_revision",
    }
)
POINT_PROVENANCE_FIELDS = frozenset(
    {
        "claim_id",
        "dimensions",
        "domains",
        "embedding_model",
        "intents",
        "owner_user_id",
        "predicate",
        "rebuild_run_id",
        "renderer_sha256",
        "requires_explicit",
        "revision_number",
        "schema_version",
        "sensitivity",
        "source_sha256",
        "source_snapshot_sha256",
        "status",
        "surface",
        "updated_at",
        "vector_sha256",
    }
)
INCREMENTAL_POINT_PROVENANCE_VERSION = "memory_claim_projection_v1"
INCREMENTAL_POINT_FIELDS = frozenset(
    {
        "claim_id",
        "domains",
        "intents",
        "owner_user_id",
        "predicate",
        "requires_explicit",
        "revision_number",
        "schema_version",
        "sensitivity",
        "status",
        "surface",
        "updated_at",
    }
)
INCREMENTAL_POINT_FIELDS_V3 = frozenset(
    {
        *INCREMENTAL_POINT_FIELDS,
        "dimensions",
        "embedding_model",
        "projection_manifest_sha256",
        "renderer_sha256",
        "source_sha256",
        "vector_sha256",
    }
)
RETRIEVABLE_POINT_STATUSES = frozenset({"supported", "uncertain", "disputed"})
REBUILD_PAYLOAD_INDEX_FIELDS = frozenset(
    {
        "domains",
        "embedding_model",
        "intents",
        "owner_user_id",
        "rebuild_run_id",
        "renderer_sha256",
        "sensitivity",
        "source_snapshot_sha256",
        "status",
    }
)
SOURCE_FIELDS = frozenset(
    {
        "owner_user_id",
        "claim_id",
        "canonical_text",
        "predicate",
        "qualifiers",
        "status",
        "sensitivity",
        "retrieval_policy",
        "updated_at",
        "revision_number",
    }
)
STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "because",
        "been",
        "before",
        "being",
        "could",
        "does",
        "from",
        "have",
        "into",
        "just",
        "more",
        "most",
        "other",
        "should",
        "some",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "very",
        "what",
        "when",
        "where",
        "which",
        "while",
        "with",
        "would",
        "your",
    }
)
NEGATIVE_QUERY_BANK = (
    "How do I factor a quadratic polynomial?",
    "Explain photosynthesis in oak trees.",
    "What is the orbital period of Neptune?",
    "How is medieval stained glass manufactured?",
    "Describe the rules of international cricket.",
    "What causes a solar eclipse?",
)


class RebuildContractError(ValueError):
    pass


def normalize_cosine_vector(
    values: Sequence[float], *, dimensions: int = VECTOR_DIMENSIONS
) -> list[float]:
    if type(dimensions) is not int or dimensions < 1 or len(values) != dimensions:
        raise RebuildContractError("cosine vector dimensions differ")
    try:
        vector = list(
            struct.unpack(
                "<" + "f" * dimensions,
                struct.pack(
                    "<" + "f" * dimensions, *[float(item) for item in values]
                ),
            )
        )
    except (OverflowError, struct.error, TypeError, ValueError) as exc:
        raise RebuildContractError("cosine vector encoding rejected") from exc
    if any(not math.isfinite(item) for item in vector):
        raise RebuildContractError("cosine vector contains a non-finite value")
    norm = math.sqrt(math.fsum(item * item for item in vector))
    if not math.isfinite(norm) or norm <= 0.0:
        raise RebuildContractError("cosine vector norm rejected")
    return list(
        struct.unpack(
            "<" + "f" * dimensions,
            struct.pack("<" + "f" * dimensions, *[item / norm for item in vector]),
        )
    )


def vector_sha256_float32(
    values: Sequence[float], *, dimensions: int = VECTOR_DIMENSIONS
) -> str:
    if len(values) != dimensions:
        raise RebuildContractError("vector digest dimensions differ")
    try:
        payload = struct.pack(
            "<" + "f" * dimensions, *[float(item) for item in values]
        )
    except (OverflowError, struct.error, TypeError, ValueError) as exc:
        raise RebuildContractError("vector digest encoding rejected") from exc
    return sha256_bytes(payload)


class QdrantMutationLock:
    def __init__(
        self,
        *,
        exclusive: bool,
        timeout_seconds: float = 10.0,
        path: pathlib.Path = QDRANT_MUTATION_LOCK_PATH,
    ) -> None:
        if not 0.0 <= timeout_seconds <= 30.0 or not path.is_absolute():
            raise RebuildContractError("Qdrant mutation lock arguments rejected")
        self.exclusive = exclusive
        self.timeout_seconds = timeout_seconds
        self.path = path
        self.descriptor: int | None = None

    def acquire(self) -> "QdrantMutationLock":
        if self.descriptor is not None:
            raise RebuildContractError("Qdrant mutation lock is already held")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise RebuildContractError("Qdrant mutation lock open failed") from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise RebuildContractError("Qdrant mutation lock identity rejected")
            operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if time.monotonic() >= deadline:
                        raise RebuildContractError(
                            "Qdrant mutation lock acquisition timed out"
                        ) from exc
                    time.sleep(0.05)
            self.descriptor = descriptor
            return self
        except Exception:
            os.close(descriptor)
            raise

    def release(self) -> None:
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "QdrantMutationLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def qdrant_mutation_lock(
    *,
    exclusive: bool,
    timeout_seconds: float = 10.0,
    path: pathlib.Path = QDRANT_MUTATION_LOCK_PATH,
) -> QdrantMutationLock:
    return QdrantMutationLock(
        exclusive=exclusive, timeout_seconds=timeout_seconds, path=path
    )


def validate_production_write_lease(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != LEASE_FIELDS:
        raise RebuildContractError("production-write lease fields differ")
    lease = dict(value)
    for field in ("lease_id", "task_id", "thread_id"):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,200}", str(lease.get(field) or "")):
            raise RebuildContractError(f"production-write lease {field} rejected")
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(lease.get("acquire_event_sha256") or "")
    ):
        raise RebuildContractError("production-write lease event rejected")
    if type(lease.get("registry_revision")) is not int or lease["registry_revision"] < 1:
        raise RebuildContractError("production-write lease revision rejected")
    return lease


def validate_lease_acquire_event(
    lease_value: Mapping[str, Any],
    *,
    events_root: pathlib.Path = LEASE_EVENTS_ROOT,
    expected_uid: int = 1000,
) -> dict[str, Any]:
    lease = validate_production_write_lease(lease_value)
    if not pathlib.Path(events_root).is_absolute():
        raise RebuildContractError("lease event registry identity rejected")
    root_descriptor: int | None = None
    descriptor: int | None = None
    try:
        root_descriptor = os.open(
            events_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        root_info = os.fstat(root_descriptor)
        prefix = f"{lease['registry_revision']:020d}-"
        candidates: list[str] = []
        with os.scandir(root_descriptor) as entries:
            for entry in entries:
                if entry.name.startswith(prefix) and entry.name.endswith(".json"):
                    candidates.append(entry.name)
                    if len(candidates) > 1:
                        break
    except OSError as exc:
        if root_descriptor is not None:
            os.close(root_descriptor)
        raise RebuildContractError("lease event registry is unavailable") from exc
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != expected_uid
        or stat.S_IMODE(root_info.st_mode) & 0o077
        or len(candidates) != 1
    ):
        os.close(root_descriptor)
        raise RebuildContractError("lease event registry identity rejected")
    name = candidates[0]
    if not re.fullmatch(
        rf"{lease['registry_revision']:020d}-[0-9a-f]{{32}}\.json", name
    ):
        os.close(root_descriptor)
        raise RebuildContractError("lease event filename rejected")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=root_descriptor,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 1 <= info.st_size <= 262_144
        ):
            raise RebuildContractError("lease event file identity rejected")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, 262_145 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 262_144:
                raise RebuildContractError("lease event file identity rejected")
        raw = b"".join(chunks)
        closing_info = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != info.st_size
            or any(
                getattr(info, field) != getattr(closing_info, field)
                for field in stable_fields
            )
        ):
            raise RebuildContractError("lease event changed while reading")
    except OSError as exc:
        raise RebuildContractError("lease event read failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)
    try:
        event = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RebuildContractError("lease event parse failed") from exc
    expected_fields = {
        "actor",
        "event_id",
        "event_sha256",
        "event_type",
        "lease_id",
        "occurred_at",
        "payload",
        "previous_event_sha256",
        "request_id",
        "request_sha256",
        "schema_version",
        "sequence",
    }
    if (
        not isinstance(event, dict)
        or set(event) != expected_fields
        or raw != canonical_bytes(event)
        or event.get("schema_version") != "chat-memory-change-lease-event-v1"
        or event.get("event_type") != "acquire"
        or event.get("sequence") != lease["registry_revision"]
        or event.get("lease_id") != lease["lease_id"]
        or event.get("event_sha256") != lease["acquire_event_sha256"]
    ):
        raise RebuildContractError("lease acquisition event differs")
    body = dict(event)
    stored_sha256 = body.pop("event_sha256")
    if sha256_bytes(canonical_bytes(body)) != stored_sha256:
        raise RebuildContractError("lease acquisition event digest rejected")
    actor = event.get("actor")
    payload = event.get("payload")
    if (
        not isinstance(actor, dict)
        or actor.get("uid") != expected_uid
        or actor.get("task_id") != lease["task_id"]
        or actor.get("thread_id") != lease["thread_id"]
        or not isinstance(payload, dict)
        or payload.get("worktree") != str(PRODUCTION_WORKTREE)
        or "production-write" not in (payload.get("change_types") or [])
    ):
        raise RebuildContractError("lease acquisition authority differs")
    return event


def require_production_write_guard(
    lease_value: Mapping[str, Any],
    *,
    worktree: pathlib.Path = PRODUCTION_WORKTREE,
    guard_path: pathlib.Path = LEASE_GUARD,
) -> None:
    lease = validate_production_write_lease(lease_value)
    validate_lease_acquire_event(lease)
    required_environment = {
        "CHAT_MEMORY_LEASE_ID": lease["lease_id"],
        "CODEX_TASK_ID": lease["task_id"],
        "CODEX_THREAD_ID": lease["thread_id"],
    }
    if any(os.environ.get(name) != value for name, value in required_environment.items()):
        raise RebuildContractError("production-write lease runtime identity differs")
    if (
        worktree != PRODUCTION_WORKTREE
        or guard_path != LEASE_GUARD
        or not guard_path.is_file()
    ):
        raise RebuildContractError("production-write guard installation rejected")
    guard_environment = {
        **required_environment,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        [
            "/usr/bin/python3.12",
            str(guard_path),
            "--operation",
            "production-write",
            "--worktree",
            str(worktree),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        env=guard_environment,
        timeout=30,
    )
    if result.returncode != 0:
        raise RebuildContractError("production-write guard denied")


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RebuildContractError(f"{name} must be JSON object") from exc
    if not isinstance(value, dict):
        raise RebuildContractError(f"{name} must be an object")
    try:
        encoded = canonical_bytes(value)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise RebuildContractError(f"{name} is not canonical JSON") from exc
    if not isinstance(decoded, dict):
        raise RebuildContractError(f"{name} must be an object")
    return decoded


def _policy_values(policy: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = policy.get(key, [])
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RebuildContractError(f"retrieval_policy.{key} must be text or list")
    normalized = sorted({item.strip().lower() for item in value if item.strip()})
    if len(normalized) > 32 or any(len(item) > 96 for item in normalized):
        raise RebuildContractError(f"retrieval_policy.{key} exceeds bounds")
    return tuple(normalized)


def projection_source_record(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != SOURCE_FIELDS:
        raise RebuildContractError("projection source fields differ")
    try:
        owner = uuid.UUID(str(value["owner_user_id"]))
        claim = uuid.UUID(str(value["claim_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RebuildContractError("projection source identity rejected") from exc
    text = " ".join(str(value["canonical_text"] or "").split()).strip()
    predicate = str(value["predicate"] or "").strip().lower()
    status = str(value["status"] or "").strip().lower()
    sensitivity = str(value["sensitivity"] or "").strip().lower()
    updated_at = str(value["updated_at"] or "").strip()
    revision = value["revision_number"]
    if (
        status not in RETRIEVABLE_POINT_STATUSES
        or not text
        or len(text.encode("utf-8")) > 16_384
        or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,127}", predicate)
        or not sensitivity
        or len(sensitivity) > 64
        or not updated_at
        or type(revision) is not int
        or revision < 0
    ):
        raise RebuildContractError("projection source is not retrievable and bounded")
    return {
        "claim_id": str(claim),
        "canonical_text": text,
        "owner_user_id": str(owner),
        "predicate": predicate,
        "qualifiers": _json_object(value["qualifiers"], "qualifiers"),
        "retrieval_policy": _json_object(
            value["retrieval_policy"], "retrieval_policy"
        ),
        "revision_number": revision,
        "sensitivity": sensitivity,
        "status": status,
        "updated_at": updated_at,
    }


def projection_source_sha256(value: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(projection_source_record(value)))


@dataclass(frozen=True)
class SupportedClaimSnapshotV1:
    owner_user_id: uuid.UUID
    claim_id: uuid.UUID
    canonical_text: str
    predicate: str
    qualifiers: dict[str, Any]
    status: str
    sensitivity: str
    retrieval_policy: dict[str, Any]
    updated_at: str
    revision_number: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SupportedClaimSnapshotV1":
        if not isinstance(value, Mapping) or set(value) != SOURCE_FIELDS:
            raise RebuildContractError("claim source fields differ from the allowlist")
        try:
            owner = uuid.UUID(str(value["owner_user_id"]))
            claim = uuid.UUID(str(value["claim_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise RebuildContractError("claim identifiers are malformed") from exc
        text = " ".join(str(value["canonical_text"] or "").split()).strip()
        predicate = str(value["predicate"] or "").strip().lower()
        status = str(value["status"] or "").strip().lower()
        sensitivity = str(value["sensitivity"] or "").strip().lower()
        updated_at = str(value["updated_at"] or "").strip()
        revision = value["revision_number"]
        if (
            status != "supported"
            or not text
            or len(text.encode("utf-8")) > 16_384
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,127}", predicate)
            or not sensitivity
            or len(sensitivity) > 64
            or not updated_at
            or type(revision) is not int
            or revision < 0
        ):
            raise RebuildContractError("claim is not currently supported and bounded")
        return cls(
            owner_user_id=owner,
            claim_id=claim,
            canonical_text=text,
            predicate=predicate,
            qualifiers=_json_object(value["qualifiers"], "qualifiers"),
            status=status,
            sensitivity=sensitivity,
            retrieval_policy=_json_object(
                value["retrieval_policy"], "retrieval_policy"
            ),
            updated_at=updated_at,
            revision_number=revision,
        )

    def source_record(self) -> dict[str, Any]:
        return {
            "claim_id": str(self.claim_id),
            "canonical_text": self.canonical_text,
            "owner_user_id": str(self.owner_user_id),
            "predicate": self.predicate,
            "qualifiers": self.qualifiers,
            "retrieval_policy": self.retrieval_policy,
            "revision_number": self.revision_number,
            "sensitivity": self.sensitivity,
            "status": self.status,
            "updated_at": self.updated_at,
        }

    def source_sha256(self) -> str:
        return sha256_bytes(canonical_bytes(self.source_record()))


def source_snapshot_sha256(claims: Sequence[SupportedClaimSnapshotV1]) -> str:
    ordered = sorted(claims, key=lambda item: (item.owner_user_id.bytes, item.claim_id.bytes))
    if not ordered or len({(item.owner_user_id, item.claim_id) for item in ordered}) != len(ordered):
        raise RebuildContractError("source snapshot is empty or contains duplicates")
    manifest = {
        "claim_count": len(ordered),
        "contract_version": SOURCE_SNAPSHOT_VERSION,
        "records": [
            {
                "claim_id": str(item.claim_id),
                "owner_user_id": str(item.owner_user_id),
                "source_sha256": item.source_sha256(),
            }
            for item in ordered
        ],
    }
    return sha256_bytes(canonical_bytes(manifest))


def topic_terms(claim: SupportedClaimSnapshotV1, limit: int = 10) -> tuple[str, ...]:
    if not 3 <= limit <= 16:
        raise RebuildContractError("topic term limit rejected")
    values: list[str] = []
    for token in re.findall(
        r"[^\W_][\w'-]{2,}", claim.canonical_text.lower(), flags=re.UNICODE
    ):
        token = token.strip("'-")
        if not token or token in STOP_WORDS or token.isdigit() or token in values:
            continue
        values.append(token)
        if len(values) == limit:
            break
    if len(values) < 2:
        predicate_terms = claim.predicate.replace("_", ".").split(".")
        for token in predicate_terms:
            if len(token) >= 3 and token not in values:
                values.append(token)
    if not values:
        raise RebuildContractError("claim cannot produce a conversational topic")
    return tuple(values[:limit])


def conversational_queries(
    claim: SupportedClaimSnapshotV1,
) -> tuple[str, str, str]:
    topic = " ".join(topic_terms(claim))
    predicate = " ".join(claim.predicate.replace("_", ".").split("."))
    candidates = (
        f"What do you remember about {topic}?",
        f"Have I told you anything involving {topic}?",
        f"Can you recall my {predicate} details related to {topic}?",
    )
    queries = tuple(" ".join(item.split())[:512] for item in candidates)
    if len(set(queries)) != 3 or any(not item.endswith("?") for item in queries):
        raise RebuildContractError("conversational query generation failed")
    return queries


def point_payload(
    claim: SupportedClaimSnapshotV1,
    *,
    renderer_sha256: str,
    rebuild_run_id: str,
    vector_sha256: str,
    source_snapshot_sha256_value: str,
) -> dict[str, Any]:
    for name, value in {
        "renderer_sha256": renderer_sha256,
        "vector_sha256": vector_sha256,
        "source_snapshot_sha256": source_snapshot_sha256_value,
    }.items():
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise RebuildContractError(f"{name} is malformed")
    if not re.fullmatch(
        r"memory-qdrant-rebuild-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}",
        str(rebuild_run_id or ""),
    ):
        raise RebuildContractError("rebuild_run_id is malformed")
    policy = claim.retrieval_policy
    surface = str(policy.get("surface_policy") or policy.get("surface") or "support").strip().lower()
    if not surface or len(surface) > 96:
        raise RebuildContractError("surface policy is malformed")
    return {
        "claim_id": str(claim.claim_id),
        "dimensions": VECTOR_DIMENSIONS,
        "domains": list(_policy_values(policy, "domains")),
        "embedding_model": EMBEDDING_MODEL,
        "intents": list(_policy_values(policy, "intents")),
        "owner_user_id": str(claim.owner_user_id),
        "predicate": claim.predicate,
        "rebuild_run_id": rebuild_run_id,
        "renderer_sha256": renderer_sha256,
        "requires_explicit": bool(policy.get("requires_explicit"))
        or surface in {"explicit_recall_only", "restricted_explicit_recall_only"},
        "revision_number": claim.revision_number,
        "schema_version": POINT_PROVENANCE_VERSION,
        "sensitivity": claim.sensitivity,
        "source_sha256": claim.source_sha256(),
        "source_snapshot_sha256": source_snapshot_sha256_value,
        "status": claim.status,
        "surface": surface,
        "updated_at": claim.updated_at,
        "vector_sha256": vector_sha256,
    }


@dataclass(frozen=True)
class QualityThresholdsV1:
    top1_ppm: int = 650_000
    top5_ppm: int = 900_000
    mrr_ppm: int = 750_000
    minimum_margin_ppm: int = 20_000
    positive_above_threshold_ppm: int = 900_000


def conversational_quality_report(
    *,
    ranks: Sequence[int | None],
    target_scores: Sequence[float],
    negative_scores: Sequence[float],
    thresholds: QualityThresholdsV1 = QualityThresholdsV1(),
) -> dict[str, Any]:
    if (
        not ranks
        or len(ranks) != len(target_scores)
        or not negative_scores
        or any(rank is not None and (type(rank) is not int or rank < 1) for rank in ranks)
        or any(not math.isfinite(float(score)) for score in (*target_scores, *negative_scores))
    ):
        raise RebuildContractError("quality observations are malformed")
    total = len(ranks)
    top1 = sum(rank == 1 for rank in ranks)
    top5 = sum(rank is not None and rank <= 5 for rank in ranks)
    reciprocal = sum(0.0 if rank is None else 1.0 / rank for rank in ranks)
    top1_ppm = top1 * 1_000_000 // total
    top5_ppm = top5 * 1_000_000 // total
    mrr_ppm = int(reciprocal * 1_000_000 / total)
    ordered_positive = sorted(float(score) for score in target_scores)
    positive_floor = ordered_positive[max(0, math.ceil(len(ordered_positive) * 0.10) - 1)]
    negative_ceiling = max(float(score) for score in negative_scores)
    margin = positive_floor - negative_ceiling
    recommended = (positive_floor + negative_ceiling) / 2.0
    positive_above = sum(float(score) >= recommended for score in target_scores)
    positive_above_ppm = positive_above * 1_000_000 // total
    margin_ppm = int(margin * 1_000_000)
    accepted = (
        top1_ppm >= thresholds.top1_ppm
        and top5_ppm >= thresholds.top5_ppm
        and mrr_ppm >= thresholds.mrr_ppm
        and margin_ppm >= thresholds.minimum_margin_ppm
        and positive_above_ppm >= thresholds.positive_above_threshold_ppm
    )
    return {
        "accepted": accepted,
        "contract_version": QUALITY_VERSION,
        "evaluated_query_count": total,
        "margin_ppm": margin_ppm,
        "mrr_ppm": mrr_ppm,
        "negative_query_count": len(negative_scores),
        "positive_above_threshold_ppm": positive_above_ppm,
        "recommended_score_threshold_ppm": int(recommended * 1_000_000),
        "top1_count": top1,
        "top1_ppm": top1_ppm,
        "top5_count": top5,
        "top5_ppm": top5_ppm,
    }


def validate_shadow_collection(name: str) -> str:
    value = str(name or "").strip()
    if not re.fullmatch(r"memory_claim_v1_shadow_rebuild_[a-z0-9]{12,40}", value):
        raise RebuildContractError("shadow collection name rejected")
    return value


def collection_fingerprint_sha256(
    *,
    collection: str,
    dimensions: int,
    distance: str,
    points: Sequence[Mapping[str, Any]],
    payload_indexes: Mapping[str, str] | None = None,
) -> str:
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{3,255}", str(collection or ""))
        or type(dimensions) is not int
        or dimensions < 1
        or distance not in {"cosine", "dot", "euclid", "manhattan"}
    ):
        raise RebuildContractError("collection fingerprint configuration rejected")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for point in points:
        if not isinstance(point, Mapping) or set(point) != {
            "id",
            "payload",
            "stored_vector_sha256",
        }:
            raise RebuildContractError("collection fingerprint point rejected")
        point_id = str(point["id"] or "")
        payload = point["payload"]
        vector_digest = str(point["stored_vector_sha256"] or "")
        if (
            not point_id
            or point_id in seen
            or not isinstance(payload, Mapping)
            or not re.fullmatch(r"[0-9a-f]{64}", vector_digest)
        ):
            raise RebuildContractError("collection fingerprint point identity rejected")
        seen.add(point_id)
        normalized.append(
            {
                "id": point_id,
                "payload": dict(payload),
                "stored_vector_sha256": vector_digest,
            }
        )
    index_values = dict(sorted((payload_indexes or {}).items()))
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", str(key))
        or str(value) not in {"keyword", "integer", "float", "geo", "text", "bool", "datetime", "uuid"}
        for key, value in index_values.items()
    ):
        raise RebuildContractError("collection fingerprint payload index rejected")
    manifest = {
        "collection": collection,
        "dimensions": dimensions,
        "distance": distance,
        "payload_indexes": index_values,
        "points": sorted(normalized, key=lambda item: item["id"]),
    }
    return sha256_bytes(canonical_bytes(manifest))


def alias_transition(
    *, alias_name: str, expected_source: str, target: str, observed: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if alias_name != ACTIVE_ALIAS or observed != {ACTIVE_ALIAS: expected_source}:
        raise RebuildContractError("active alias state differs from the approved source")
    if target == expected_source:
        raise RebuildContractError("alias target does not change")
    if expected_source != "memory_claim_v1" and not expected_source.startswith(SHADOW_PREFIX):
        raise RebuildContractError("alias source collection rejected")
    if target == "memory_claim_v1":
        pass
    elif target.startswith(SHADOW_PREFIX):
        validate_shadow_collection(target)
    else:
        raise RebuildContractError("alias target collection rejected")
    return (
        {"delete_alias": {"alias_name": ACTIVE_ALIAS}},
        {"create_alias": {"alias_name": ACTIVE_ALIAS, "collection_name": target}},
    )


def manifest_sha256(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping) or value.get("contract_version") != CONTRACT_VERSION:
        raise RebuildContractError("rebuild manifest contract rejected")
    return sha256_bytes(canonical_bytes(dict(value)))
