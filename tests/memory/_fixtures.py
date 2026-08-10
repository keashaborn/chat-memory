"""Synthetic, content-free-adjacent fixtures for the Phase 1 offline suite.

Nothing in this module is copied from production conversations, accounts,
attachments, preferences, claims, jobs, vectors, or review artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any
from uuid import UUID

from rag_engine.governed_memory.admission import (
    claim_identity_sha256,
    recompute_claim_state_sha256,
    recompute_revision_sha256,
)
from rag_engine.governed_memory.auth import ActorRole, ActorScope, VerifiedActor
from rag_engine.governed_memory.contracts import (
    ContractViolation,
    ObjectKind,
    canonical_sha256,
    render_relational_fact,
    selection_binding_sha256,
    semantic_key_sha256,
    framed_sha256,
    sha256_text,
    utc_text,
)
from rag_engine.governed_memory.eligibility import EligibilityPolicy
from rag_engine.governed_memory.extraction import (
    build_provider_request,
    validate_provider_result,
)
from rag_engine.governed_memory.projection import (
    PROJECTION_CONTRACT_SHA256,
    ProjectionOperation,
    projection_manifest_sha256,
)


FIXTURE_PROVENANCE = "synthetic-governed-memory-phase1"

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
CUTOVER = datetime(2030, 1, 1, tzinfo=timezone.utc)

OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")
THREAD_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
MESSAGE_A = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
MESSAGE_BEFORE_CUTOVER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1")
EVIDENCE_A = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
JOB_A = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
PROVIDER_CALL_A = UUID("abababab-abab-4bab-8bab-abababababab")
PROPOSAL_A = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
CLAIM_A = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
REVISION_A = UUID("12345678-1234-4234-8234-123456789abc")
REVISION_B = UUID("12345678-1234-4234-8234-123456789abd")
PROJECTION_A = UUID("87654321-4321-4321-8321-cba987654321")
RESPONSE_A = UUID("99999999-9999-4999-8999-999999999999")
OPERATION_A = UUID("77777777-7777-4777-8777-777777777777")
ATTACHMENT_A = UUID("66666666-6666-4666-8666-666666666666")
EXCHANGE_A = UUID("55555555-5555-4555-8555-555555555555")
WINDOW_A = UUID("44444444-4444-4444-8444-444444444444")
WORKER_A = UUID("33333333-3333-4333-8333-333333333333")
BRIDGE_OUTBOX_A = UUID("32323232-3232-4232-8232-323232323232")
BRIDGE_LEASE_A = UUID("31313131-3131-4131-8131-313131313131")

SOURCE_TEXT = "Synthetic owner prefers the cobalt interface theme."
SOURCE_SHA256 = sha256(SOURCE_TEXT.encode("utf-8")).hexdigest()
REPLACEMENT_TEXT = "Synthetic owner prefers the amber interface theme."
REPLACEMENT_SHA256 = sha256(REPLACEMENT_TEXT.encode("utf-8")).hexdigest()
ATTACHMENT_TEXT = "SYNTHETIC ATTACHMENT CONTENT MUST NOT BE READ"

PREDICATE_CATALOG = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "governed-memory-migrations"
        / "predicate_catalog.json"
    ).read_text(encoding="utf-8")
)
SYNTHETIC_PREDICATE_CATALOG_SHA256 = (
    "5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded"
)


def make_worker_actor(*, owner_user_id: UUID = OWNER_A) -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=owner_user_id,
        actor_id=WORKER_A,
        role=ActorRole.WORKER,
        scopes=(ActorScope.PROCESS_MEMORY_INGEST,),
        authentication_manifest_sha256="a" * 64,
        authenticated_at=NOW,
    )


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def digest(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def make_ingest_payload(
    *,
    owner_user_id: UUID = OWNER_A,
    message_id: UUID = MESSAGE_A,
    created_at: datetime = NOW,
    role: str = "user",
    text: str = SOURCE_TEXT,
    attachment_ids: tuple[UUID, ...] = (),
    window_ordinal: int = 10,
) -> dict[str, Any]:
    content_sha256 = sha256(text.encode("utf-8")).hexdigest()
    payload = {
        "fixture_provenance": FIXTURE_PROVENANCE,
        "owner_user_id": str(owner_user_id),
        "thread_id": str(THREAD_A),
        "message_id": str(message_id),
        "role": role,
        "created_at": created_at.isoformat(),
        "content_sha256": content_sha256,
        "content": text,
        "attachment_ids": [str(value) for value in attachment_ids],
        "exchange_id": str(EXCHANGE_A),
        "window_id": str(WINDOW_A),
        "window_ordinal": window_ordinal,
    }
    payload["window_sha256"] = canonical_sha256(
        "governed_memory.ingest_window",
        {
            "owner_user_id": owner_user_id,
            "thread_id": THREAD_A,
            "exchange_id": EXCHANGE_A,
            "window_id": WINDOW_A,
            "message_id": message_id,
            "content_sha256": content_sha256,
        },
    )
    return payload


def make_bridge_lease(
    payload: dict[str, Any],
    *,
    ingest_after: datetime = CUTOVER,
    context_review_count: int = 0,
) -> dict[str, Any]:
    source_created_at = datetime.fromisoformat(
        str(payload["created_at"]).replace("Z", "+00:00")
    )
    policy_sha256 = EligibilityPolicy(ingest_after=ingest_after).policy_sha256
    source_binding_sha256 = framed_sha256(
        "governed_memory.bridge_source.v1",
        (
            ("owner_user_id", str(payload["owner_user_id"])),
            ("message_id", str(payload["message_id"])),
            ("thread_id", str(payload["thread_id"])),
            ("exchange_id", str(payload["exchange_id"])),
            ("window_id", str(payload["window_id"])),
            ("window_ordinal", str(payload["window_ordinal"])),
            ("window_sha256", str(payload["window_sha256"])),
            ("content_sha256", str(payload["content_sha256"])),
            ("policy_sha256", policy_sha256),
            ("source_created_at", utc_text(source_created_at)),
        ),
    )
    return {
        "outbox_id": str(BRIDGE_OUTBOX_A),
        "owner_user_id": payload["owner_user_id"],
        "message_id": payload["message_id"],
        "thread_id": payload["thread_id"],
        "exchange_id": payload["exchange_id"],
        "window_id": payload["window_id"],
        "window_ordinal": payload["window_ordinal"],
        "window_sha256": payload["window_sha256"],
        "content_sha256": payload["content_sha256"],
        "source_binding_sha256": source_binding_sha256,
        "policy_sha256": policy_sha256,
        "source_created_at": source_created_at,
        "ingest_after": ingest_after,
        "context_review_count": context_review_count,
        "eligibility_decision": (
            "review_context" if context_review_count == 1 else None
        ),
        "lease_token": str(BRIDGE_LEASE_A),
        "lease_expires_at": NOW + timedelta(minutes=1),
    }


def make_selected_evidence(*, text: str = SOURCE_TEXT) -> dict[str, Any]:
    encoded = text.encode("utf-8")
    source_sha256 = sha256(encoded).hexdigest()
    window_sha256 = canonical_sha256(
        "governed_memory.ingest_window",
        {
            "owner_user_id": OWNER_A,
            "thread_id": THREAD_A,
            "exchange_id": EXCHANGE_A,
            "window_id": WINDOW_A,
            "message_id": MESSAGE_A,
            "content_sha256": source_sha256,
        },
    )
    binding_sha256 = selection_binding_sha256(
        owner_user_id=OWNER_A,
        source_kind="conversation_message",
        source_message_id=MESSAGE_A,
        source_thread_id=THREAD_A,
        source_window_id=WINDOW_A,
        window_sha256=window_sha256,
        source_sha256=source_sha256,
        selected_sha256=source_sha256,
        start_utf8=0,
        end_utf8=len(encoded),
        context_message_id=None,
        context_sha256=None,
    )
    return {
        "fixture_provenance": FIXTURE_PROVENANCE,
        "owner_user_id": str(OWNER_A),
        "evidence_id": str(EVIDENCE_A),
        "source_kind": "conversation_message",
        "source_message_id": str(MESSAGE_A),
        "source_thread_id": str(THREAD_A),
        "source_window_id": str(WINDOW_A),
        "start_utf8": 0,
        "end_utf8": len(encoded),
        "selected_text": text,
        "selected_sha256": source_sha256,
        "source_sha256": source_sha256,
        "window_sha256": window_sha256,
        "context_message_id": None,
        "context_sha256": None,
        "selection_binding_sha256": binding_sha256,
        "category": "life_preference",
        "assertion_mode": "endorsed",
        "subject_hint": "owner",
        "sensitivity": "ordinary",
    }


def make_provider_output() -> dict[str, Any]:
    return {
        "schema": "governed-memory-extraction",
        "usage": {"input_tokens": 128, "output_tokens": 16},
        "facts": [
            {
                "subject": {"kind": "self"},
                "predicate": "preference.personal",
                "object": {"kind": "literal", "value": "cobalt"},
                "epistemic_status": "supported",
                "sensitivity": "ordinary",
                "selected_sha256": make_selected_evidence()["selected_sha256"],
            }
        ],
    }


def make_extraction_job(*, state: str = "claimed", attempt_number: int = 1) -> dict[str, Any]:
    return {
        "fixture_provenance": FIXTURE_PROVENANCE,
        "owner_user_id": str(OWNER_A),
        "job_id": str(JOB_A),
        "evidence_id": str(EVIDENCE_A),
        "provider_call_id": str(PROVIDER_CALL_A),
        "source_sha256": SOURCE_SHA256,
        "state": state,
        "attempt_number": attempt_number,
        "operation_id": str(OPERATION_A),
    }


def make_proposal(*, state: str = "pending_review") -> dict[str, Any]:
    request = build_provider_request(
        make_extraction_job(),
        make_selected_evidence(),
        "synthetic-extraction-model",
        "governed-memory-extraction",
        PREDICATE_CATALOG,
    )
    batch = validate_provider_result(request, make_provider_output())
    return {
        **batch["proposals"][0],
        **batch["proposal_hash_binding"],
        "review_state": state,
        "expires_at": NOW + timedelta(hours=24),
    }


def make_claim_row(
    *,
    owner_user_id: UUID = OWNER_A,
    claim_id: UUID = CLAIM_A,
    revision_id: UUID = REVISION_A,
    lifecycle_state: str = "active",
    epistemic_status: str = "supported",
    source_sha256: str = SOURCE_SHA256,
    current: bool = True,
    revision_number: int = 1,
    projection_sequence: int = 1,
    object_literal: str = "cobalt",
) -> dict[str, Any]:
    selection = make_selected_evidence()["selection_binding_sha256"]
    selected_sha256 = make_selected_evidence()["selected_sha256"]
    subject = {
        "display_name": None,
        "entity_key": "self",
        "entity_type": "self",
    }
    object_value = {"kind": "literal", "literal": object_literal}
    retrieval_text = render_relational_fact(
        subject=subject,
        predicate="preference.personal",
        object_value=object_value,
    )
    semantic_sha256 = semantic_key_sha256(
        subject_entity_key="self",
        predicate="preference.personal",
        object_kind=ObjectKind.LITERAL,
        object_entity_key=None,
        object_literal=object_literal,
    )
    identity_sha256 = claim_identity_sha256(
        subject_entity_key="self",
        predicate="preference.personal",
    )
    row = {
        "owner_user_id": str(owner_user_id),
        "claim_id": str(claim_id),
        "revision_id": str(revision_id),
        "revision_number": revision_number,
        "lifecycle_state": lifecycle_state,
        "epistemic_state": epistemic_status,
        "predicate": "preference.personal",
        "subject_entity_type": "self",
        "subject_entity_key": "self",
        "subject_display_name": None,
        "object_kind": "literal",
        "object_entity_type": None,
        "object_entity_key": None,
        "object_display_name": None,
        "object_literal": object_literal,
        "sensitivity": "ordinary",
        "domains": [],
        "intents": [],
        "surface": "normal",
        "requires_explicit": False,
        "projectable": True,
        "source_sha256": source_sha256,
        "selection_binding_sha256": selection,
        "semantic_key_sha256": semantic_sha256,
        "claim_identity_sha256": identity_sha256,
        "is_current": current,
        "updated_at": NOW.isoformat(),
        "valid_from": None,
        "valid_to": None,
        "projection_sequence": projection_sequence,
        "retrieval_text": retrieval_text,
        "retrieval_text_sha256": sha256_text(retrieval_text),
        "predicate_catalog_sha256": SYNTHETIC_PREDICATE_CATALOG_SHA256,
    }
    revision_material = {
        **row,
        "proposal_id": str(PROPOSAL_A),
        "proposal_sha256": "a" * 64,
        "selected_sha256": selected_sha256,
    }
    row["revision_sha256"] = recompute_revision_sha256(revision_material)
    row["state_sha256"] = recompute_claim_state_sha256(row)
    return row


def make_projection_outbox(
    claim: dict[str, Any] | None = None,
    *,
    operation: str = "upsert",
    operation_id: UUID = PROJECTION_A,
    sequence_number: int | None = None,
    state: str = "claimed",
) -> dict[str, Any]:
    """Build the exact authoritative projection-outbox row used by workers."""

    claim = make_claim_row() if claim is None else claim
    sequence = (
        int(claim["projection_sequence"])
        if sequence_number is None
        else sequence_number
    )
    manifest = projection_manifest_sha256(
        owner_user_id=UUID(str(claim["owner_user_id"])),
        claim_id=UUID(str(claim["claim_id"])),
        revision_id=UUID(str(claim["revision_id"])),
        operation_id=operation_id,
        operation=operation,
        sequence_number=sequence,
        revision_sha256=str(claim["revision_sha256"]),
        selection_binding_sha256=str(claim["selection_binding_sha256"]),
        retrieval_text_sha256=str(claim["retrieval_text_sha256"]),
        embedding_input_sha256=str(claim["retrieval_text_sha256"]),
    )
    return {
        "owner_user_id": claim["owner_user_id"],
        "claim_id": claim["claim_id"],
        "revision_id": claim["revision_id"],
        "operation_id": str(operation_id),
        "operation": operation,
        "sequence_number": sequence,
        "revision_sha256": claim["revision_sha256"],
        "selection_binding_sha256": claim["selection_binding_sha256"],
        "retrieval_text_sha256": claim["retrieval_text_sha256"],
        "embedding_input_sha256": claim["retrieval_text_sha256"],
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
        "projection_manifest_sha256": manifest,
        "state": state,
    }


def make_candidate(**overrides: Any) -> dict[str, Any]:
    claim = make_claim_row()
    candidate = {
        "owner_user_id": claim["owner_user_id"],
        "claim_id": claim["claim_id"],
        "revision_id": claim["revision_id"],
        "revision_number": claim["revision_number"],
        "revision_sha256": claim["revision_sha256"],
        "selection_binding_sha256": claim["selection_binding_sha256"],
        "retrieval_text_sha256": claim["retrieval_text_sha256"],
        "projection_sequence": claim["projection_sequence"],
        "projection_operation_id": str(PROJECTION_A),
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
        "score": 0.91,
    }
    explicit_manifest = "projection_manifest_sha256" in overrides
    candidate.update(overrides)
    if not explicit_manifest:
        try:
            candidate["projection_manifest_sha256"] = projection_manifest_sha256(
                owner_user_id=UUID(str(candidate["owner_user_id"])),
                claim_id=UUID(str(candidate["claim_id"])),
                revision_id=UUID(str(candidate["revision_id"])),
                operation_id=UUID(str(candidate["projection_operation_id"])),
                operation=ProjectionOperation.UPSERT,
                sequence_number=int(candidate["projection_sequence"]),
                revision_sha256=str(candidate["revision_sha256"]),
                selection_binding_sha256=str(
                    candidate["selection_binding_sha256"]
                ),
                retrieval_text_sha256=str(candidate["retrieval_text_sha256"]),
                embedding_input_sha256=str(candidate["retrieval_text_sha256"]),
            )
        except (ContractViolation, TypeError, ValueError):
            candidate["projection_manifest_sha256"] = projection_manifest_sha256(
                owner_user_id=OWNER_A,
                claim_id=CLAIM_A,
                revision_id=REVISION_A,
                operation_id=PROJECTION_A,
                operation=ProjectionOperation.UPSERT,
                sequence_number=1,
                revision_sha256=claim["revision_sha256"],
                selection_binding_sha256=claim["selection_binding_sha256"],
                retrieval_text_sha256=claim["retrieval_text_sha256"],
                embedding_input_sha256=claim["retrieval_text_sha256"],
            )
    return candidate


def make_retrieved_claim_row(**claim_overrides: Any) -> dict[str, Any]:
    claim = make_claim_row(**claim_overrides)
    candidate = make_candidate(
        owner_user_id=claim["owner_user_id"],
        claim_id=claim["claim_id"],
        revision_id=claim["revision_id"],
        revision_number=claim["revision_number"],
        revision_sha256=claim["revision_sha256"],
        selection_binding_sha256=claim["selection_binding_sha256"],
        retrieval_text_sha256=claim["retrieval_text_sha256"],
        projection_sequence=claim["projection_sequence"],
    )
    return {
        **claim,
        "score": candidate["score"],
        "projection_operation_id": candidate["projection_operation_id"],
        "projection_manifest_sha256": candidate["projection_manifest_sha256"],
    }


def deterministic_vector(dimensions: int = 3072) -> list[float]:
    if dimensions < 2:
        raise ValueError("dimensions must be at least two")
    return [3.0, 4.0] + [0.0] * (dimensions - 2)


@dataclass
class FrozenClock:
    current: datetime = NOW

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass
class RecordingProvider:
    output: dict[str, Any] = field(default_factory=make_provider_output)
    error: BaseException | None = None
    calls: list[Any] = field(default_factory=list)

    async def complete(self, request: Any) -> dict[str, Any]:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return json.loads(json.dumps(self.output))


@dataclass
class RecordingEmbedder:
    vector: list[float] = field(default_factory=deterministic_vector)
    calls: list[str] = field(default_factory=list)

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self.vector)


@dataclass
class FakeVectorIndex:
    search_results: list[dict[str, Any]] = field(default_factory=list)
    points: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)

    async def search(self, *, owner_user_id: UUID, vector: list[float], limit: int) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "search",
                {
                    "owner_user_id": str(owner_user_id),
                    "dimensions": len(vector),
                    "limit": limit,
                },
            )
        )
        return json.loads(json.dumps(self.search_results))

    async def upsert(self, *, point_id: UUID, vector: list[float], payload: dict[str, Any]) -> None:
        self.calls.append(("upsert", str(point_id)))
        self.points[str(point_id)] = {
            "vector": list(vector),
            "payload": json.loads(json.dumps(payload)),
        }

    async def delete(self, *, point_id: UUID) -> None:
        self.calls.append(("delete", str(point_id)))
        self.points.pop(str(point_id), None)


@dataclass
class AttachmentReadTrap:
    reads: int = 0

    async def read(self, _attachment_id: UUID) -> bytes:
        self.reads += 1
        raise AssertionError("ordinary attachment content crossed the Memory boundary")


@dataclass
class CallLedger:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def record(self, event_type: str, **detail: Any) -> None:
        self.events.append((event_type, dict(detail)))

    def count(self, event_type: str) -> int:
        return sum(1 for current, _ in self.events if current == event_type)


def assert_unit_vector(vector: list[float]) -> None:
    magnitude = math.sqrt(sum(value * value for value in vector))
    if not math.isclose(magnitude, 1.0, rel_tol=0.0, abs_tol=1e-7):
        raise AssertionError(f"vector magnitude is not one: {magnitude}")
