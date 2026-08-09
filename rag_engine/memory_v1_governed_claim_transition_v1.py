from __future__ import annotations

"""Two-stage, review-consuming governed-claim transition contract.

Stage A consumes an already-authorized manual projection review and commits a
candidate claim with immutable observation provenance.  Stage B is separate:
it consumes an already-persisted latest owner-user assessment review, commits
the supported revision, and creates one *held* derived-index eligibility row.

This module never creates either review, never authenticates a user UUID,
never calls a model/provider, and never writes Qdrant.  A trusted adapter must
bind a previously verified actor through ``VerifiedActorBinderV1``.  The held
outbox row uses ``available_at='infinity'`` and is not dispatchable until a
separate explicitly authorized release changes that exact row.

Both operations own their top-level SERIALIZABLE transaction.  Nested use is
rejected, and receipts are constructed only after the transaction commits.
"""

import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Mapping, Protocol, Sequence
from uuid import UUID


MATERIALIZATION_COMMAND_VERSION = (
    "memory_v1_governed_claim_materialization_command_v1"
)
MATERIALIZATION_RECEIPT_VERSION = (
    "memory_v1_governed_claim_materialization_receipt_v1"
)
SUPPORT_COMMAND_VERSION = "memory_v1_governed_claim_support_command_v1"
SUPPORT_RECEIPT_VERSION = "memory_v1_governed_claim_support_receipt_v1"
ACTOR_CONTEXT_VERSION = "memory_v1_verified_actor_context_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROJECTION_REF = re.compile(r"^p[0-9]{2}$")


class GovernedClaimTransitionError(RuntimeError):
    """Raised when authority, state, replay, or durability invariants fail."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _require_sha(value: object, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise GovernedClaimTransitionError(f"{label} must be a lowercase SHA-256")


def _require_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise GovernedClaimTransitionError(f"{label} must be a UUID")


def _require_exact_int(value: object, label: str, *, minimum: int = 0) -> None:
    if type(value) is not int:
        raise GovernedClaimTransitionError(f"{label} must be an integer")
    if value < minimum:
        raise GovernedClaimTransitionError(f"{label} is below its minimum")


def _row_value(row: Mapping[str, Any] | Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError) as exc:
        raise GovernedClaimTransitionError(
            f"database result is missing required field {key}"
        ) from exc


def _row_uuid(row: Mapping[str, Any] | Any, key: str) -> UUID:
    try:
        return UUID(str(_row_value(row, key)))
    except (TypeError, ValueError) as exc:
        raise GovernedClaimTransitionError(
            f"database result field {key} is not a UUID"
        ) from exc


def _row_exact_int(
    row: Mapping[str, Any] | Any,
    key: str,
    *,
    minimum: int = 0,
) -> int:
    value = _row_value(row, key)
    _require_exact_int(value, key, minimum=minimum)
    return value


@dataclass(frozen=True, slots=True)
class VerifiedActorContextV1:
    owner_user_id: UUID
    authentication_manifest_sha256: str
    contract_version: Literal["memory_v1_verified_actor_context_v1"] = (
        ACTOR_CONTEXT_VERSION
    )

    def __post_init__(self) -> None:
        _require_uuid(self.owner_user_id, "verified actor owner_user_id")
        _require_sha(
            self.authentication_manifest_sha256,
            "verified actor authentication_manifest_sha256",
        )
        if self.contract_version != ACTOR_CONTEXT_VERSION:
            raise GovernedClaimTransitionError("verified actor contract is invalid")

    @property
    def owner_user_id_sha256(self) -> str:
        return _sha256_text(str(self.owner_user_id))


@dataclass(frozen=True, slots=True)
class ReviewedProjectionMaterializationV1:
    plan_id: UUID
    projection_ref: str
    projection_review_id: UUID
    projection_request_id: UUID
    projection_apply_manifest_sha256: str

    def __post_init__(self) -> None:
        for label, value in (
            ("plan_id", self.plan_id),
            ("projection_review_id", self.projection_review_id),
            ("projection_request_id", self.projection_request_id),
        ):
            _require_uuid(value, label)
        if not isinstance(self.projection_ref, str) or not _PROJECTION_REF.fullmatch(
            self.projection_ref
        ):
            raise GovernedClaimTransitionError("projection_ref is invalid")
        _require_sha(
            self.projection_apply_manifest_sha256,
            "projection_apply_manifest_sha256",
        )
        if len({self.plan_id, self.projection_review_id, self.projection_request_id}) != 3:
            raise GovernedClaimTransitionError(
                "materialization identifiers must be distinct"
            )

    def material(self) -> dict[str, object]:
        return {
            "contract_version": MATERIALIZATION_COMMAND_VERSION,
            "plan_id": str(self.plan_id),
            "projection_ref": self.projection_ref,
            "projection_review_id": str(self.projection_review_id),
            "projection_request_id": str(self.projection_request_id),
            "projection_apply_manifest_sha256": (
                self.projection_apply_manifest_sha256
            ),
        }

    @property
    def command_manifest_sha256(self) -> str:
        return _sha256_text(_canonical(self.material()))


@dataclass(frozen=True, slots=True)
class GovernedClaimMaterializationReceiptV1:
    owner_user_id_sha256: str
    actor_authentication_manifest_sha256: str
    command_manifest_sha256: str
    plan_id: UUID
    projection_ref: str
    projection_review_id: UUID
    projection_request_id: UUID
    claim_id: UUID
    claim_revision_number: Literal[1]
    projection_apply_event_id: UUID
    projection_apply_manifest_sha256: str
    observation_count: int
    observation_authority_sha256: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        for label, value in (
            ("owner_user_id_sha256", self.owner_user_id_sha256),
            (
                "actor_authentication_manifest_sha256",
                self.actor_authentication_manifest_sha256,
            ),
            ("command_manifest_sha256", self.command_manifest_sha256),
            (
                "projection_apply_manifest_sha256",
                self.projection_apply_manifest_sha256,
            ),
            (
                "observation_authority_sha256",
                self.observation_authority_sha256,
            ),
            ("receipt_sha256", self.receipt_sha256),
        ):
            _require_sha(value, label)
        for label, value in (
            ("plan_id", self.plan_id),
            ("projection_review_id", self.projection_review_id),
            ("projection_request_id", self.projection_request_id),
            ("claim_id", self.claim_id),
            ("projection_apply_event_id", self.projection_apply_event_id),
        ):
            _require_uuid(value, label)
        if not isinstance(self.projection_ref, str) or not _PROJECTION_REF.fullmatch(
            self.projection_ref
        ):
            raise GovernedClaimTransitionError("receipt projection_ref is invalid")
        expected_command = ReviewedProjectionMaterializationV1(
            plan_id=self.plan_id,
            projection_ref=self.projection_ref,
            projection_review_id=self.projection_review_id,
            projection_request_id=self.projection_request_id,
            projection_apply_manifest_sha256=(
                self.projection_apply_manifest_sha256
            ),
        ).command_manifest_sha256
        if self.command_manifest_sha256 != expected_command:
            raise GovernedClaimTransitionError(
                "materialization receipt command manifest mismatch"
            )
        _require_exact_int(
            self.claim_revision_number,
            "claim_revision_number",
            minimum=1,
        )
        if self.claim_revision_number != 1:
            raise GovernedClaimTransitionError(
                "materialization receipt must bind candidate revision 1"
            )
        _require_exact_int(self.observation_count, "observation_count", minimum=1)
        if self.receipt_sha256 != self.expected_receipt_sha256():
            raise GovernedClaimTransitionError("materialization receipt SHA-256 mismatch")

    def material(self) -> dict[str, object]:
        return {
            "contract_version": MATERIALIZATION_RECEIPT_VERSION,
            "owner_user_id_sha256": self.owner_user_id_sha256,
            "actor_authentication_manifest_sha256": (
                self.actor_authentication_manifest_sha256
            ),
            "command_manifest_sha256": self.command_manifest_sha256,
            "plan_id": str(self.plan_id),
            "projection_ref": self.projection_ref,
            "projection_review_id": str(self.projection_review_id),
            "projection_request_id": str(self.projection_request_id),
            "claim_id": str(self.claim_id),
            "claim_revision_number": self.claim_revision_number,
            "projection_apply_event_id": str(self.projection_apply_event_id),
            "projection_apply_manifest_sha256": (
                self.projection_apply_manifest_sha256
            ),
            "observation_count": self.observation_count,
            "observation_authority_sha256": self.observation_authority_sha256,
        }

    def expected_receipt_sha256(self) -> str:
        return _sha256_text(_canonical(self.material()))

    def canonical_json_bytes(self) -> bytes:
        return _canonical(
            {**self.material(), "receipt_sha256": self.receipt_sha256}
        ).encode("utf-8")

    @classmethod
    def create(
        cls,
        *,
        owner_user_id_sha256: str,
        actor_authentication_manifest_sha256: str,
        command_manifest_sha256: str,
        plan_id: UUID,
        projection_ref: str,
        projection_review_id: UUID,
        projection_request_id: UUID,
        claim_id: UUID,
        projection_apply_event_id: UUID,
        projection_apply_manifest_sha256: str,
        observation_count: int,
        observation_authority_sha256: str,
    ) -> "GovernedClaimMaterializationReceiptV1":
        material = {
            "contract_version": MATERIALIZATION_RECEIPT_VERSION,
            "owner_user_id_sha256": owner_user_id_sha256,
            "actor_authentication_manifest_sha256": (
                actor_authentication_manifest_sha256
            ),
            "command_manifest_sha256": command_manifest_sha256,
            "plan_id": str(plan_id),
            "projection_ref": projection_ref,
            "projection_review_id": str(projection_review_id),
            "projection_request_id": str(projection_request_id),
            "claim_id": str(claim_id),
            "claim_revision_number": 1,
            "projection_apply_event_id": str(projection_apply_event_id),
            "projection_apply_manifest_sha256": (
                projection_apply_manifest_sha256
            ),
            "observation_count": observation_count,
            "observation_authority_sha256": observation_authority_sha256,
        }
        return cls(
            owner_user_id_sha256=owner_user_id_sha256,
            actor_authentication_manifest_sha256=(
                actor_authentication_manifest_sha256
            ),
            command_manifest_sha256=command_manifest_sha256,
            plan_id=plan_id,
            projection_ref=projection_ref,
            projection_review_id=projection_review_id,
            projection_request_id=projection_request_id,
            claim_id=claim_id,
            claim_revision_number=1,
            projection_apply_event_id=projection_apply_event_id,
            projection_apply_manifest_sha256=projection_apply_manifest_sha256,
            observation_count=observation_count,
            observation_authority_sha256=observation_authority_sha256,
            receipt_sha256=_sha256_text(_canonical(material)),
        )


@dataclass(frozen=True, slots=True)
class ReviewedSupportPromotionV1:
    materialization_receipt_sha256: str
    claim_id: UUID
    assessment_review_request_id: UUID
    assessment_review_id: UUID
    assessment_review_manifest_sha256: str
    assessment_apply_request_id: UUID
    assessment_apply_manifest_sha256: str

    def __post_init__(self) -> None:
        for label, value in (
            ("materialization_receipt_sha256", self.materialization_receipt_sha256),
            (
                "assessment_review_manifest_sha256",
                self.assessment_review_manifest_sha256,
            ),
            (
                "assessment_apply_manifest_sha256",
                self.assessment_apply_manifest_sha256,
            ),
        ):
            _require_sha(value, label)
        for label, value in (
            ("claim_id", self.claim_id),
            ("assessment_review_request_id", self.assessment_review_request_id),
            ("assessment_review_id", self.assessment_review_id),
            ("assessment_apply_request_id", self.assessment_apply_request_id),
        ):
            _require_uuid(value, label)
        if len(
            {
                self.claim_id,
                self.assessment_review_request_id,
                self.assessment_review_id,
                self.assessment_apply_request_id,
            }
        ) != 4:
            raise GovernedClaimTransitionError("support identifiers must be distinct")

    def material(self) -> dict[str, object]:
        return {
            "contract_version": SUPPORT_COMMAND_VERSION,
            "materialization_receipt_sha256": (
                self.materialization_receipt_sha256
            ),
            "claim_id": str(self.claim_id),
            "assessment_review_request_id": str(
                self.assessment_review_request_id
            ),
            "assessment_review_id": str(self.assessment_review_id),
            "assessment_review_manifest_sha256": (
                self.assessment_review_manifest_sha256
            ),
            "assessment_apply_request_id": str(self.assessment_apply_request_id),
            "assessment_apply_manifest_sha256": (
                self.assessment_apply_manifest_sha256
            ),
        }

    @property
    def command_manifest_sha256(self) -> str:
        return _sha256_text(_canonical(self.material()))


@dataclass(frozen=True, slots=True)
class GovernedClaimSupportReceiptV1:
    owner_user_id_sha256: str
    actor_authentication_manifest_sha256: str
    materialization_receipt_sha256: str
    command_manifest_sha256: str
    claim_id: UUID
    claim_revision_number: Literal[2]
    projection_apply_event_id: UUID
    projection_apply_manifest_sha256: str
    observation_authority_sha256: str
    assessment_review_request_id: UUID
    assessment_review_id: UUID
    assessment_review_manifest_sha256: str
    assessment_apply_request_id: UUID
    assessment_apply_event_id: UUID
    assessment_id: UUID
    assessment_apply_manifest_sha256: str
    outbox_id: UUID
    outbox_dispatch_held: Literal[True]
    receipt_sha256: str

    def __post_init__(self) -> None:
        for label, value in (
            ("owner_user_id_sha256", self.owner_user_id_sha256),
            (
                "actor_authentication_manifest_sha256",
                self.actor_authentication_manifest_sha256,
            ),
            (
                "materialization_receipt_sha256",
                self.materialization_receipt_sha256,
            ),
            ("command_manifest_sha256", self.command_manifest_sha256),
            (
                "projection_apply_manifest_sha256",
                self.projection_apply_manifest_sha256,
            ),
            (
                "observation_authority_sha256",
                self.observation_authority_sha256,
            ),
            (
                "assessment_review_manifest_sha256",
                self.assessment_review_manifest_sha256,
            ),
            (
                "assessment_apply_manifest_sha256",
                self.assessment_apply_manifest_sha256,
            ),
            ("receipt_sha256", self.receipt_sha256),
        ):
            _require_sha(value, label)
        for label, value in (
            ("claim_id", self.claim_id),
            ("projection_apply_event_id", self.projection_apply_event_id),
            ("assessment_review_request_id", self.assessment_review_request_id),
            ("assessment_review_id", self.assessment_review_id),
            ("assessment_apply_request_id", self.assessment_apply_request_id),
            ("assessment_apply_event_id", self.assessment_apply_event_id),
            ("assessment_id", self.assessment_id),
            ("outbox_id", self.outbox_id),
        ):
            _require_uuid(value, label)
        _require_exact_int(
            self.claim_revision_number,
            "claim_revision_number",
            minimum=2,
        )
        if self.claim_revision_number != 2:
            raise GovernedClaimTransitionError(
                "support receipt must bind supported revision 2"
            )
        if self.outbox_dispatch_held is not True:
            raise GovernedClaimTransitionError(
                "support receipt must bind a held index eligibility event"
            )
        expected_command = ReviewedSupportPromotionV1(
            materialization_receipt_sha256=(
                self.materialization_receipt_sha256
            ),
            claim_id=self.claim_id,
            assessment_review_request_id=self.assessment_review_request_id,
            assessment_review_id=self.assessment_review_id,
            assessment_review_manifest_sha256=(
                self.assessment_review_manifest_sha256
            ),
            assessment_apply_request_id=self.assessment_apply_request_id,
            assessment_apply_manifest_sha256=(
                self.assessment_apply_manifest_sha256
            ),
        ).command_manifest_sha256
        if self.command_manifest_sha256 != expected_command:
            raise GovernedClaimTransitionError(
                "support receipt command manifest mismatch"
            )
        if self.receipt_sha256 != self.expected_receipt_sha256():
            raise GovernedClaimTransitionError("support receipt SHA-256 mismatch")

    def material(self) -> dict[str, object]:
        return {
            "contract_version": SUPPORT_RECEIPT_VERSION,
            "owner_user_id_sha256": self.owner_user_id_sha256,
            "actor_authentication_manifest_sha256": (
                self.actor_authentication_manifest_sha256
            ),
            "materialization_receipt_sha256": (
                self.materialization_receipt_sha256
            ),
            "command_manifest_sha256": self.command_manifest_sha256,
            "claim_id": str(self.claim_id),
            "claim_revision_number": self.claim_revision_number,
            "projection_apply_event_id": str(self.projection_apply_event_id),
            "projection_apply_manifest_sha256": (
                self.projection_apply_manifest_sha256
            ),
            "observation_authority_sha256": self.observation_authority_sha256,
            "assessment_review_request_id": str(
                self.assessment_review_request_id
            ),
            "assessment_review_id": str(self.assessment_review_id),
            "assessment_review_manifest_sha256": (
                self.assessment_review_manifest_sha256
            ),
            "assessment_apply_request_id": str(self.assessment_apply_request_id),
            "assessment_apply_event_id": str(self.assessment_apply_event_id),
            "assessment_id": str(self.assessment_id),
            "assessment_apply_manifest_sha256": (
                self.assessment_apply_manifest_sha256
            ),
            "outbox_id": str(self.outbox_id),
            "outbox_dispatch_held": self.outbox_dispatch_held,
        }

    def expected_receipt_sha256(self) -> str:
        return _sha256_text(_canonical(self.material()))

    def canonical_json_bytes(self) -> bytes:
        return _canonical(
            {**self.material(), "receipt_sha256": self.receipt_sha256}
        ).encode("utf-8")

    @classmethod
    def create(
        cls,
        *,
        owner_user_id_sha256: str,
        actor_authentication_manifest_sha256: str,
        materialization_receipt_sha256: str,
        command_manifest_sha256: str,
        claim_id: UUID,
        projection_apply_event_id: UUID,
        projection_apply_manifest_sha256: str,
        observation_authority_sha256: str,
        assessment_review_request_id: UUID,
        assessment_review_id: UUID,
        assessment_review_manifest_sha256: str,
        assessment_apply_request_id: UUID,
        assessment_apply_event_id: UUID,
        assessment_id: UUID,
        assessment_apply_manifest_sha256: str,
        outbox_id: UUID,
    ) -> "GovernedClaimSupportReceiptV1":
        material = {
            "contract_version": SUPPORT_RECEIPT_VERSION,
            "owner_user_id_sha256": owner_user_id_sha256,
            "actor_authentication_manifest_sha256": (
                actor_authentication_manifest_sha256
            ),
            "materialization_receipt_sha256": materialization_receipt_sha256,
            "command_manifest_sha256": command_manifest_sha256,
            "claim_id": str(claim_id),
            "claim_revision_number": 2,
            "projection_apply_event_id": str(projection_apply_event_id),
            "projection_apply_manifest_sha256": projection_apply_manifest_sha256,
            "observation_authority_sha256": observation_authority_sha256,
            "assessment_review_request_id": str(assessment_review_request_id),
            "assessment_review_id": str(assessment_review_id),
            "assessment_review_manifest_sha256": (
                assessment_review_manifest_sha256
            ),
            "assessment_apply_request_id": str(assessment_apply_request_id),
            "assessment_apply_event_id": str(assessment_apply_event_id),
            "assessment_id": str(assessment_id),
            "assessment_apply_manifest_sha256": (
                assessment_apply_manifest_sha256
            ),
            "outbox_id": str(outbox_id),
            "outbox_dispatch_held": True,
        }
        return cls(
            owner_user_id_sha256=owner_user_id_sha256,
            actor_authentication_manifest_sha256=(
                actor_authentication_manifest_sha256
            ),
            materialization_receipt_sha256=materialization_receipt_sha256,
            command_manifest_sha256=command_manifest_sha256,
            claim_id=claim_id,
            claim_revision_number=2,
            projection_apply_event_id=projection_apply_event_id,
            projection_apply_manifest_sha256=projection_apply_manifest_sha256,
            observation_authority_sha256=observation_authority_sha256,
            assessment_review_request_id=assessment_review_request_id,
            assessment_review_id=assessment_review_id,
            assessment_review_manifest_sha256=(
                assessment_review_manifest_sha256
            ),
            assessment_apply_request_id=assessment_apply_request_id,
            assessment_apply_event_id=assessment_apply_event_id,
            assessment_id=assessment_id,
            assessment_apply_manifest_sha256=assessment_apply_manifest_sha256,
            outbox_id=outbox_id,
            outbox_dispatch_held=True,
            receipt_sha256=_sha256_text(_canonical(material)),
        )


@dataclass(frozen=True, slots=True)
class GovernedClaimMaterializationResultV1:
    outcome: Literal["applied", "replayed"]
    rows_written: int
    receipt: GovernedClaimMaterializationReceiptV1
    external_model_calls: Literal[0] = 0
    qdrant_direct_writes: Literal[0] = 0
    automatic_promotions: Literal[0] = 0
    derived_index_eligibility_events_created: Literal[0] = 0

    def __post_init__(self) -> None:
        if self.outcome not in {"applied", "replayed"}:
            raise GovernedClaimTransitionError("materialization outcome is invalid")
        if not isinstance(self.receipt, GovernedClaimMaterializationReceiptV1):
            raise GovernedClaimTransitionError("materialization receipt is invalid")
        for label, value in (
            ("rows_written", self.rows_written),
            ("external_model_calls", self.external_model_calls),
            ("qdrant_direct_writes", self.qdrant_direct_writes),
            ("automatic_promotions", self.automatic_promotions),
            (
                "derived_index_eligibility_events_created",
                self.derived_index_eligibility_events_created,
            ),
        ):
            _require_exact_int(value, label)
        if (
            self.external_model_calls != 0
            or self.qdrant_direct_writes != 0
            or self.automatic_promotions != 0
            or self.derived_index_eligibility_events_created != 0
        ):
            raise GovernedClaimTransitionError(
                "materialization safety counters must remain zero"
            )
        if self.outcome == "replayed" and self.rows_written != 0:
            raise GovernedClaimTransitionError("materialization replay must be zero-write")
        if self.outcome == "applied" and self.rows_written <= 0:
            raise GovernedClaimTransitionError("materialization apply wrote no rows")


@dataclass(frozen=True, slots=True)
class GovernedClaimSupportResultV1:
    outcome: Literal["applied", "replayed"]
    rows_written: int
    receipt: GovernedClaimSupportReceiptV1
    external_model_calls: Literal[0] = 0
    qdrant_direct_writes: Literal[0] = 0
    automatic_promotions: Literal[0] = 0
    derived_index_eligibility_events_created: Literal[0, 1] = 1
    derived_index_eligibility_events_held: Literal[1] = 1

    def __post_init__(self) -> None:
        if self.outcome not in {"applied", "replayed"}:
            raise GovernedClaimTransitionError("support outcome is invalid")
        if not isinstance(self.receipt, GovernedClaimSupportReceiptV1):
            raise GovernedClaimTransitionError("support receipt is invalid")
        for label, value in (
            ("rows_written", self.rows_written),
            ("external_model_calls", self.external_model_calls),
            ("qdrant_direct_writes", self.qdrant_direct_writes),
            ("automatic_promotions", self.automatic_promotions),
            (
                "derived_index_eligibility_events_created",
                self.derived_index_eligibility_events_created,
            ),
            (
                "derived_index_eligibility_events_held",
                self.derived_index_eligibility_events_held,
            ),
        ):
            _require_exact_int(value, label)
        if (
            self.external_model_calls != 0
            or self.qdrant_direct_writes != 0
            or self.automatic_promotions != 0
        ):
            raise GovernedClaimTransitionError(
                "support safety counters must remain zero"
            )
        expected_created = 1 if self.outcome == "applied" else 0
        if self.derived_index_eligibility_events_created != expected_created:
            raise GovernedClaimTransitionError(
                "support eligibility-event counter is inconsistent"
            )
        if self.derived_index_eligibility_events_held != 1:
            raise GovernedClaimTransitionError("support eligibility must remain held")
        if self.outcome == "replayed" and self.rows_written != 0:
            raise GovernedClaimTransitionError("support replay must be zero-write")
        if self.outcome == "applied" and self.rows_written <= 0:
            raise GovernedClaimTransitionError("support apply wrote no rows")


class _Transaction(Protocol):
    async def __aenter__(self) -> object: ...
    async def __aexit__(self, *args: object) -> object: ...


class GovernedClaimTransitionConnectionV1(Protocol):
    def is_in_transaction(self) -> bool: ...
    def transaction(self, **kwargs: object) -> _Transaction: ...
    async def execute(self, query: str, *args: object) -> object: ...
    async def fetchval(self, query: str, *args: object) -> object: ...
    async def fetchrow(self, query: str, *args: object) -> Mapping[str, Any] | None: ...
    async def fetch(self, query: str, *args: object) -> Sequence[Mapping[str, Any]]: ...


class VerifiedActorBinderV1(Protocol):
    """Trusted boundary; derives and binds actor without command owner input."""

    async def bind_verified_actor(
        self,
        conn: GovernedClaimTransitionConnectionV1,
    ) -> VerifiedActorContextV1: ...


def _require_top_level_connection(
    conn: GovernedClaimTransitionConnectionV1,
) -> None:
    state = conn.is_in_transaction()
    if type(state) is not bool:
        raise GovernedClaimTransitionError("connection transaction state is invalid")
    if state:
        raise GovernedClaimTransitionError(
            "governed transition requires a top-level transaction boundary"
        )


@asynccontextmanager
async def _top_level_transaction(
    conn: GovernedClaimTransitionConnectionV1,
    *,
    readonly: bool,
    label: str,
) -> AsyncIterator[None]:
    try:
        async with conn.transaction(isolation="serializable", readonly=readonly):
            yield
    except GovernedClaimTransitionError:
        raise
    except Exception:
        raise GovernedClaimTransitionError(f"{label} transaction failed") from None


async def _bind_actor(
    conn: GovernedClaimTransitionConnectionV1,
    binder: VerifiedActorBinderV1,
) -> VerifiedActorContextV1:
    try:
        context = await binder.bind_verified_actor(conn)
    except Exception:
        raise GovernedClaimTransitionError("verified actor binding failed") from None
    if not isinstance(context, VerifiedActorContextV1):
        raise GovernedClaimTransitionError("verified actor binder returned invalid context")
    context.__post_init__()
    session_user = await conn.fetchval("SELECT session_user")
    if session_user != "brains_app":
        raise GovernedClaimTransitionError("connection must authenticate as brains_app")
    bound_actor = await conn.fetchval("SELECT memory.current_actor_user_id()")
    try:
        bound_uuid = UUID(str(bound_actor))
    except (TypeError, ValueError) as exc:
        raise GovernedClaimTransitionError("database actor context is unavailable") from exc
    if bound_uuid != context.owner_user_id:
        raise GovernedClaimTransitionError("database actor differs from verified actor")
    return context


def _require_single_row(
    rows: Sequence[Mapping[str, Any]],
    label: str,
) -> Mapping[str, Any]:
    if len(rows) != 1:
        raise GovernedClaimTransitionError(f"{label} is absent or ambiguous")
    return rows[0]


def _optional_single_row(
    rows: Sequence[Mapping[str, Any]],
    label: str,
) -> Mapping[str, Any] | None:
    if len(rows) > 1:
        raise GovernedClaimTransitionError(f"{label} is ambiguous")
    return rows[0] if rows else None


async def _materialization_authority(
    conn: GovernedClaimTransitionConnectionV1,
    *,
    actor: VerifiedActorContextV1,
    command: ReviewedProjectionMaterializationV1,
    claim_id: UUID,
    projection_apply_event_id: UUID,
    allow_supported: bool = False,
) -> Mapping[str, Any]:
    rows = await conn.fetch(
        """/* governed_claim_transition:materialization_authority */
        SELECT contract_version,owner_user_id,claim_id,
          claim_status,current_revision_number,
          projection_request_id,plan_id,projection_ref,projection_review_id,
          projection_apply_event_id,projection_apply_manifest_sha256,
          observation_count,observation_authority_sha256
        FROM memory.read_governed_claim_materialization_authority_v1
        WHERE owner_user_id=$1 AND claim_id=$2
          AND projection_apply_event_id=$3 AND projection_request_id=$4
          AND plan_id=$5 AND projection_ref=$6 AND projection_review_id=$7
          AND projection_apply_manifest_sha256=$8
        """,
        actor.owner_user_id,
        claim_id,
        projection_apply_event_id,
        command.projection_request_id,
        command.plan_id,
        command.projection_ref,
        command.projection_review_id,
        command.projection_apply_manifest_sha256,
    )
    row = _require_single_row(rows, "materialization authority")
    expected_text = {
        "contract_version": "memory_v1_governed_claim_materialization_authority_v1",
        "projection_ref": command.projection_ref,
        "projection_apply_manifest_sha256": (
            command.projection_apply_manifest_sha256
        ),
    }
    for key, expected in expected_text.items():
        if str(_row_value(row, key)) != expected:
            raise GovernedClaimTransitionError(
                f"materialization authority field {key} drifted"
            )
    expected_uuid = {
        "owner_user_id": actor.owner_user_id,
        "claim_id": claim_id,
        "projection_request_id": command.projection_request_id,
        "plan_id": command.plan_id,
        "projection_review_id": command.projection_review_id,
        "projection_apply_event_id": projection_apply_event_id,
    }
    for key, expected in expected_uuid.items():
        if _row_uuid(row, key) != expected:
            raise GovernedClaimTransitionError(
                f"materialization authority field {key} drifted"
            )
    claim_status = str(_row_value(row, "claim_status"))
    current_revision_number = _row_exact_int(
        row,
        "current_revision_number",
        minimum=1,
    )
    allowed_states = {("candidate", 1)}
    if allow_supported:
        allowed_states.add(("supported", 2))
    allowed_statuses = {status for status, _ in allowed_states}
    if claim_status not in allowed_statuses:
        raise GovernedClaimTransitionError(
            "materialization authority field claim_status drifted"
        )
    if (claim_status, current_revision_number) not in allowed_states:
        raise GovernedClaimTransitionError("materialization revision drifted")
    _row_exact_int(row, "observation_count", minimum=1)
    _require_sha(
        str(_row_value(row, "observation_authority_sha256")),
        "observation_authority_sha256",
    )
    return row


async def _materialization_authority_by_request(
    conn: GovernedClaimTransitionConnectionV1,
    *,
    actor: VerifiedActorContextV1,
    command: ReviewedProjectionMaterializationV1,
) -> Mapping[str, Any] | None:
    rows = await conn.fetch(
        """/* governed_claim_transition:reconcile_materialization_request */
        SELECT claim_id,projection_apply_event_id
        FROM memory.read_governed_claim_materialization_authority_v1
        WHERE owner_user_id=$1 AND projection_request_id=$2
        """,
        actor.owner_user_id,
        command.projection_request_id,
    )
    probe = _optional_single_row(rows, "materialization request reconciliation")
    if probe is None:
        return None
    return await _materialization_authority(
        conn,
        actor=actor,
        command=command,
        claim_id=_row_uuid(probe, "claim_id"),
        projection_apply_event_id=_row_uuid(probe, "projection_apply_event_id"),
        allow_supported=True,
    )


async def _projection_apply_ready_authority(
    conn: GovernedClaimTransitionConnectionV1,
    *,
    actor: VerifiedActorContextV1,
    command: ReviewedProjectionMaterializationV1,
) -> Mapping[str, Any]:
    rows = await conn.fetch(
        """/* governed_claim_transition:projection_apply_ready */
        SELECT contract_version,owner_user_id,plan_id,projection_ref,
          lane,target_action,review_state,current_revision_number,
          projection_review_id,projection_review_manifest_sha256,
          projection_apply_manifest_sha256,observation_count,
          observation_authority_sha256
        FROM memory.read_governed_claim_projection_apply_ready_v1
        WHERE owner_user_id=$1 AND plan_id=$2 AND projection_ref=$3
          AND projection_review_id=$4
          AND projection_apply_manifest_sha256=$5
        """,
        actor.owner_user_id,
        command.plan_id,
        command.projection_ref,
        command.projection_review_id,
        command.projection_apply_manifest_sha256,
    )
    row = _require_single_row(rows, "projection apply-ready authority")
    expected_text = {
        "contract_version": "memory_v1_governed_claim_projection_apply_ready_v1",
        "projection_ref": command.projection_ref,
        "lane": "claim",
        "target_action": "create",
        "review_state": "manual_review_required",
        "projection_apply_manifest_sha256": (
            command.projection_apply_manifest_sha256
        ),
    }
    for key, expected in expected_text.items():
        if str(_row_value(row, key)) != expected:
            raise GovernedClaimTransitionError(
                f"projection apply-ready authority field {key} drifted"
            )
    expected_uuid = {
        "owner_user_id": actor.owner_user_id,
        "plan_id": command.plan_id,
        "projection_review_id": command.projection_review_id,
    }
    for key, expected in expected_uuid.items():
        if _row_uuid(row, key) != expected:
            raise GovernedClaimTransitionError(
                f"projection apply-ready authority field {key} drifted"
            )
    if _row_exact_int(row, "current_revision_number") != 0:
        raise GovernedClaimTransitionError(
            "projection apply-ready current revision drifted"
        )
    _row_exact_int(row, "observation_count", minimum=1)
    for key in (
        "projection_review_manifest_sha256",
        "observation_authority_sha256",
    ):
        _require_sha(str(_row_value(row, key)), key)
    return row


def _materialization_receipt_from_row(
    *,
    actor: VerifiedActorContextV1,
    command: ReviewedProjectionMaterializationV1,
    row: Mapping[str, Any],
    authentication_manifest_sha256: str | None = None,
) -> GovernedClaimMaterializationReceiptV1:
    return GovernedClaimMaterializationReceiptV1.create(
        owner_user_id_sha256=actor.owner_user_id_sha256,
        actor_authentication_manifest_sha256=(
            authentication_manifest_sha256
            if authentication_manifest_sha256 is not None
            else actor.authentication_manifest_sha256
        ),
        command_manifest_sha256=command.command_manifest_sha256,
        plan_id=command.plan_id,
        projection_ref=command.projection_ref,
        projection_review_id=command.projection_review_id,
        projection_request_id=command.projection_request_id,
        claim_id=_row_uuid(row, "claim_id"),
        projection_apply_event_id=_row_uuid(row, "projection_apply_event_id"),
        projection_apply_manifest_sha256=str(
            _row_value(row, "projection_apply_manifest_sha256")
        ),
        observation_count=_row_exact_int(row, "observation_count", minimum=1),
        observation_authority_sha256=str(
            _row_value(row, "observation_authority_sha256")
        ),
    )


async def materialize_reviewed_claim_v1(
    conn: GovernedClaimTransitionConnectionV1,
    binder: VerifiedActorBinderV1,
    command: ReviewedProjectionMaterializationV1,
    *,
    prior_receipt: GovernedClaimMaterializationReceiptV1 | None = None,
) -> GovernedClaimMaterializationResultV1:
    """Commit a candidate from a pre-existing projection review, or verify replay."""

    if not isinstance(command, ReviewedProjectionMaterializationV1):
        raise GovernedClaimTransitionError("materialization command is invalid")
    command.__post_init__()
    if prior_receipt is not None:
        if not isinstance(prior_receipt, GovernedClaimMaterializationReceiptV1):
            raise GovernedClaimTransitionError("prior materialization receipt is invalid")
        prior_receipt.__post_init__()
        if (
            prior_receipt.command_manifest_sha256
            != command.command_manifest_sha256
            or prior_receipt.projection_apply_manifest_sha256
            != command.projection_apply_manifest_sha256
        ):
            raise GovernedClaimTransitionError(
                "prior materialization receipt does not bind this command"
            )
    _require_top_level_connection(conn)

    actor: VerifiedActorContextV1 | None = None
    authority_row: Mapping[str, Any] | None = None
    rows_written = 0
    reconciled = False
    readonly = prior_receipt is not None
    async with _top_level_transaction(
        conn,
        readonly=readonly,
        label="materialization",
    ):
        actor = await _bind_actor(conn, binder)
        if prior_receipt is not None:
            if prior_receipt.owner_user_id_sha256 != actor.owner_user_id_sha256:
                raise GovernedClaimTransitionError(
                    "prior materialization receipt belongs to another owner"
                )
            authority_row = await _materialization_authority(
                conn,
                actor=actor,
                command=command,
                claim_id=prior_receipt.claim_id,
                projection_apply_event_id=(
                    prior_receipt.projection_apply_event_id
                ),
                allow_supported=True,
            )
        else:
            authority_row = await _materialization_authority_by_request(
                conn,
                actor=actor,
                command=command,
            )
            if authority_row is not None:
                reconciled = True
            else:
                await _projection_apply_ready_authority(
                    conn,
                    actor=actor,
                    command=command,
                )
                projected = await conn.fetchrow(
                    "SELECT * FROM memory.apply_projection_v5($1,$2,$3,$4,$5)",
                    command.projection_request_id,
                    command.plan_id,
                    command.projection_ref,
                    command.projection_review_id,
                    command.projection_apply_manifest_sha256,
                )
                if projected is None or (
                    str(_row_value(projected, "outcome")) != "applied"
                    or str(_row_value(projected, "lane")) != "claim"
                    or _row_exact_int(projected, "revision_number", minimum=1) != 1
                ):
                    raise GovernedClaimTransitionError("claim projection apply drifted")
                rows_written = _row_exact_int(
                    projected,
                    "rows_written",
                    minimum=1,
                )
                claim_id = _row_uuid(projected, "aggregate_id")
                projection_event_id = _row_uuid(projected, "apply_event_id")
                authority_row = await _materialization_authority(
                    conn,
                    actor=actor,
                    command=command,
                    claim_id=claim_id,
                    projection_apply_event_id=projection_event_id,
                )
                observation_count = _row_exact_int(
                    authority_row,
                    "observation_count",
                    minimum=1,
                )
                if rows_written != 4 + observation_count:
                    raise GovernedClaimTransitionError(
                        "claim projection row budget drifted"
                    )

    _require_top_level_connection(conn)
    if actor is None or authority_row is None:
        raise GovernedClaimTransitionError("materialization transaction produced no result")
    receipt = _materialization_receipt_from_row(
        actor=actor,
        command=command,
        row=authority_row,
        authentication_manifest_sha256=(
            prior_receipt.actor_authentication_manifest_sha256
            if prior_receipt is not None
            else None
        ),
    )
    if prior_receipt is not None:
        if receipt.canonical_json_bytes() != prior_receipt.canonical_json_bytes():
            raise GovernedClaimTransitionError(
                "durable materialization differs from prior receipt"
            )
        receipt = prior_receipt
    return GovernedClaimMaterializationResultV1(
        outcome="replayed" if prior_receipt is not None or reconciled else "applied",
        rows_written=0 if prior_receipt is not None or reconciled else rows_written,
        receipt=receipt,
    )


async def _support_review_authority(
    conn: GovernedClaimTransitionConnectionV1,
    *,
    actor: VerifiedActorContextV1,
    materialization: GovernedClaimMaterializationReceiptV1,
    command: ReviewedSupportPromotionV1,
) -> Mapping[str, Any]:
    rows = await conn.fetch(
        """/* governed_claim_transition:support_review_authority */
        SELECT contract_version,owner_user_id,claim_id,claim_status,
          claim_revision_number,projection_request_id,plan_id,projection_ref,
          projection_review_id,projection_apply_event_id,
          projection_apply_manifest_sha256,observation_count,
          observation_authority_sha256,assessment_review_request_id,
          assessment_review_id,assessment_review_manifest_sha256,
          assessment_apply_manifest_sha256,reviewer_type,reviewer_ref,action,
          expected_from_status,target_status
        FROM memory.read_governed_claim_assessment_apply_ready_v1
        WHERE owner_user_id=$1 AND claim_id=$2
          AND projection_request_id=$3 AND plan_id=$4
          AND projection_ref=$5 AND projection_review_id=$6
          AND projection_apply_event_id=$7
          AND projection_apply_manifest_sha256=$8
          AND observation_authority_sha256=$9
          AND assessment_review_request_id=$10
          AND assessment_review_id=$11
          AND assessment_review_manifest_sha256=$12
          AND assessment_apply_manifest_sha256=$13
        """,
        actor.owner_user_id,
        command.claim_id,
        materialization.projection_request_id,
        materialization.plan_id,
        materialization.projection_ref,
        materialization.projection_review_id,
        materialization.projection_apply_event_id,
        materialization.projection_apply_manifest_sha256,
        materialization.observation_authority_sha256,
        command.assessment_review_request_id,
        command.assessment_review_id,
        command.assessment_review_manifest_sha256,
        command.assessment_apply_manifest_sha256,
    )
    row = _require_single_row(rows, "support review authority")
    expected_text = {
        "contract_version": "memory_v1_governed_claim_assessment_apply_ready_v1",
        "claim_status": "candidate",
        "projection_apply_manifest_sha256": (
            materialization.projection_apply_manifest_sha256
        ),
        "observation_authority_sha256": (
            materialization.observation_authority_sha256
        ),
        "assessment_review_manifest_sha256": (
            command.assessment_review_manifest_sha256
        ),
        "assessment_apply_manifest_sha256": (
            command.assessment_apply_manifest_sha256
        ),
        "action": "promote_supported",
        "expected_from_status": "candidate",
        "target_status": "supported",
    }
    for key, expected in expected_text.items():
        if str(_row_value(row, key)) != expected:
            raise GovernedClaimTransitionError(
                f"support review authority field {key} drifted"
            )
    if str(_row_value(row, "reviewer_type")) != "user" or str(
        _row_value(row, "reviewer_ref")
    ) != str(actor.owner_user_id):
        raise GovernedClaimTransitionError(
            "support review must be independently authorized by the owner"
        )
    expected_uuid = {
        "owner_user_id": actor.owner_user_id,
        "claim_id": command.claim_id,
        "projection_request_id": materialization.projection_request_id,
        "plan_id": materialization.plan_id,
        "projection_review_id": materialization.projection_review_id,
        "projection_apply_event_id": materialization.projection_apply_event_id,
        "assessment_review_request_id": command.assessment_review_request_id,
        "assessment_review_id": command.assessment_review_id,
    }
    for key, expected in expected_uuid.items():
        if _row_uuid(row, key) != expected:
            raise GovernedClaimTransitionError(
                f"support review authority field {key} drifted"
            )
    if str(_row_value(row, "projection_ref")) != materialization.projection_ref:
        raise GovernedClaimTransitionError(
            "support review authority field projection_ref drifted"
        )
    if _row_exact_int(row, "claim_revision_number", minimum=1) != 1:
        raise GovernedClaimTransitionError("support review revision drifted")
    if (
        _row_exact_int(row, "observation_count", minimum=1)
        != materialization.observation_count
    ):
        raise GovernedClaimTransitionError("support review observations drifted")
    return row


async def _supported_authority(
    conn: GovernedClaimTransitionConnectionV1,
    *,
    actor: VerifiedActorContextV1,
    materialization: GovernedClaimMaterializationReceiptV1,
    command: ReviewedSupportPromotionV1,
    assessment_apply_event_id: UUID,
    assessment_id: UUID,
    outbox_id: UUID,
) -> Mapping[str, Any]:
    rows = await conn.fetch(
        """/* governed_claim_transition:supported_authority */
        SELECT contract_version,owner_user_id,claim_id,claim_status,
          claim_revision_number,projection_request_id,plan_id,projection_ref,
          projection_review_id,projection_apply_event_id,
          projection_apply_manifest_sha256,observation_count,
          observation_authority_sha256,assessment_review_request_id,
          assessment_review_id,assessment_review_manifest_sha256,
          assessment_apply_request_id,assessment_apply_event_id,assessment_id,
          assessment_apply_manifest_sha256,outbox_id,
          projection_eligibility_held,projection_worker_claimable
        FROM memory.read_governed_claim_supported_held_v1
        WHERE owner_user_id=$1 AND claim_id=$2
          AND projection_request_id=$3 AND plan_id=$4
          AND projection_ref=$5 AND projection_review_id=$6
          AND projection_apply_event_id=$7
          AND projection_apply_manifest_sha256=$8
          AND observation_authority_sha256=$9
          AND assessment_review_request_id=$10
          AND assessment_review_id=$11
          AND assessment_review_manifest_sha256=$12
          AND assessment_apply_request_id=$13
          AND assessment_apply_event_id=$14 AND assessment_id=$15
          AND assessment_apply_manifest_sha256=$16 AND outbox_id=$17
        """,
        actor.owner_user_id,
        command.claim_id,
        materialization.projection_request_id,
        materialization.plan_id,
        materialization.projection_ref,
        materialization.projection_review_id,
        materialization.projection_apply_event_id,
        materialization.projection_apply_manifest_sha256,
        materialization.observation_authority_sha256,
        command.assessment_review_request_id,
        command.assessment_review_id,
        command.assessment_review_manifest_sha256,
        command.assessment_apply_request_id,
        assessment_apply_event_id,
        assessment_id,
        command.assessment_apply_manifest_sha256,
        outbox_id,
    )
    row = _require_single_row(rows, "supported transition authority")
    if str(_row_value(row, "contract_version")) != (
        "memory_v1_governed_claim_supported_held_v1"
    ):
        raise GovernedClaimTransitionError("supported authority contract drifted")
    if str(_row_value(row, "claim_status")) != "supported":
        raise GovernedClaimTransitionError("supported claim status drifted")
    if _row_exact_int(row, "claim_revision_number", minimum=2) != 2:
        raise GovernedClaimTransitionError("supported claim revision drifted")
    if (
        _row_exact_int(row, "observation_count", minimum=1)
        != materialization.observation_count
    ):
        raise GovernedClaimTransitionError("supported observations drifted")
    if _row_value(row, "projection_eligibility_held") is not True:
        raise GovernedClaimTransitionError("derived-index eligibility is not held")
    if _row_value(row, "projection_worker_claimable") is not False:
        raise GovernedClaimTransitionError("derived-index eligibility is claimable")
    expected_uuid = {
        "owner_user_id": actor.owner_user_id,
        "claim_id": command.claim_id,
        "projection_request_id": materialization.projection_request_id,
        "plan_id": materialization.plan_id,
        "projection_review_id": materialization.projection_review_id,
        "projection_apply_event_id": materialization.projection_apply_event_id,
        "assessment_review_request_id": command.assessment_review_request_id,
        "assessment_review_id": command.assessment_review_id,
        "assessment_apply_request_id": command.assessment_apply_request_id,
        "assessment_apply_event_id": assessment_apply_event_id,
        "assessment_id": assessment_id,
        "outbox_id": outbox_id,
    }
    for key, expected in expected_uuid.items():
        if _row_uuid(row, key) != expected:
            raise GovernedClaimTransitionError(
                f"supported authority field {key} drifted"
            )
    if str(_row_value(row, "projection_ref")) != materialization.projection_ref:
        raise GovernedClaimTransitionError(
            "supported authority field projection_ref drifted"
        )
    expected_sha = {
        "projection_apply_manifest_sha256": (
            materialization.projection_apply_manifest_sha256
        ),
        "observation_authority_sha256": (
            materialization.observation_authority_sha256
        ),
        "assessment_review_manifest_sha256": (
            command.assessment_review_manifest_sha256
        ),
        "assessment_apply_manifest_sha256": (
            command.assessment_apply_manifest_sha256
        ),
    }
    for key, expected in expected_sha.items():
        actual = str(_row_value(row, key))
        _require_sha(actual, key)
        if actual != expected:
            raise GovernedClaimTransitionError(
                f"supported authority field {key} drifted"
            )
    return row


async def _supported_authority_by_request(
    conn: GovernedClaimTransitionConnectionV1,
    *,
    actor: VerifiedActorContextV1,
    materialization: GovernedClaimMaterializationReceiptV1,
    command: ReviewedSupportPromotionV1,
) -> Mapping[str, Any] | None:
    rows = await conn.fetch(
        """/* governed_claim_transition:reconcile_supported_request */
        SELECT assessment_apply_event_id,assessment_id,outbox_id
        FROM memory.read_governed_claim_supported_held_v1
        WHERE owner_user_id=$1 AND claim_id=$2
          AND assessment_apply_request_id=$3
        """,
        actor.owner_user_id,
        command.claim_id,
        command.assessment_apply_request_id,
    )
    probe = _optional_single_row(rows, "support request reconciliation")
    if probe is None:
        return None
    return await _supported_authority(
        conn,
        actor=actor,
        materialization=materialization,
        command=command,
        assessment_apply_event_id=_row_uuid(probe, "assessment_apply_event_id"),
        assessment_id=_row_uuid(probe, "assessment_id"),
        outbox_id=_row_uuid(probe, "outbox_id"),
    )


def _support_receipt_from_row(
    *,
    actor: VerifiedActorContextV1,
    materialization: GovernedClaimMaterializationReceiptV1,
    command: ReviewedSupportPromotionV1,
    row: Mapping[str, Any],
    authentication_manifest_sha256: str | None = None,
) -> GovernedClaimSupportReceiptV1:
    return GovernedClaimSupportReceiptV1.create(
        owner_user_id_sha256=actor.owner_user_id_sha256,
        actor_authentication_manifest_sha256=(
            authentication_manifest_sha256
            if authentication_manifest_sha256 is not None
            else actor.authentication_manifest_sha256
        ),
        materialization_receipt_sha256=materialization.receipt_sha256,
        command_manifest_sha256=command.command_manifest_sha256,
        claim_id=_row_uuid(row, "claim_id"),
        projection_apply_event_id=_row_uuid(row, "projection_apply_event_id"),
        projection_apply_manifest_sha256=str(
            _row_value(row, "projection_apply_manifest_sha256")
        ),
        observation_authority_sha256=str(
            _row_value(row, "observation_authority_sha256")
        ),
        assessment_review_request_id=_row_uuid(
            row,
            "assessment_review_request_id",
        ),
        assessment_review_id=_row_uuid(row, "assessment_review_id"),
        assessment_review_manifest_sha256=str(
            _row_value(row, "assessment_review_manifest_sha256")
        ),
        assessment_apply_request_id=_row_uuid(
            row,
            "assessment_apply_request_id",
        ),
        assessment_apply_event_id=_row_uuid(row, "assessment_apply_event_id"),
        assessment_id=_row_uuid(row, "assessment_id"),
        assessment_apply_manifest_sha256=str(
            _row_value(row, "assessment_apply_manifest_sha256")
        ),
        outbox_id=_row_uuid(row, "outbox_id"),
    )


async def promote_independently_reviewed_claim_v1(
    conn: GovernedClaimTransitionConnectionV1,
    binder: VerifiedActorBinderV1,
    materialization: GovernedClaimMaterializationReceiptV1,
    command: ReviewedSupportPromotionV1,
    *,
    prior_receipt: GovernedClaimSupportReceiptV1 | None = None,
) -> GovernedClaimSupportResultV1:
    """Apply a pre-existing owner-user support review and hold index dispatch."""

    if not isinstance(materialization, GovernedClaimMaterializationReceiptV1):
        raise GovernedClaimTransitionError("materialization receipt is invalid")
    materialization.__post_init__()
    if not isinstance(command, ReviewedSupportPromotionV1):
        raise GovernedClaimTransitionError("support command is invalid")
    command.__post_init__()
    if (
        command.materialization_receipt_sha256
        != materialization.receipt_sha256
        or command.claim_id != materialization.claim_id
    ):
        raise GovernedClaimTransitionError(
            "support command does not bind materialization receipt"
        )
    if prior_receipt is not None:
        if not isinstance(prior_receipt, GovernedClaimSupportReceiptV1):
            raise GovernedClaimTransitionError("prior support receipt is invalid")
        prior_receipt.__post_init__()
        if (
            prior_receipt.materialization_receipt_sha256
            != materialization.receipt_sha256
            or prior_receipt.command_manifest_sha256
            != command.command_manifest_sha256
            or prior_receipt.claim_id != command.claim_id
        ):
            raise GovernedClaimTransitionError(
                "prior support receipt does not bind this operation"
            )
    _require_top_level_connection(conn)

    actor: VerifiedActorContextV1 | None = None
    authority_row: Mapping[str, Any] | None = None
    rows_written = 0
    reconciled = False
    readonly = prior_receipt is not None
    async with _top_level_transaction(
        conn,
        readonly=readonly,
        label="support",
    ):
        actor = await _bind_actor(conn, binder)
        if actor.owner_user_id_sha256 != materialization.owner_user_id_sha256:
            raise GovernedClaimTransitionError(
                "support actor differs from materialized claim owner"
            )
        if prior_receipt is not None:
            authority_row = await _supported_authority(
                conn,
                actor=actor,
                materialization=materialization,
                command=command,
                assessment_apply_event_id=(
                    prior_receipt.assessment_apply_event_id
                ),
                assessment_id=prior_receipt.assessment_id,
                outbox_id=prior_receipt.outbox_id,
            )
        else:
            authority_row = await _supported_authority_by_request(
                conn,
                actor=actor,
                materialization=materialization,
                command=command,
            )
            if authority_row is not None:
                reconciled = True
            else:
                await _support_review_authority(
                    conn,
                    actor=actor,
                    materialization=materialization,
                    command=command,
                )
                applied = await conn.fetchrow(
                    "SELECT * FROM memory.apply_claim_assessment_v5($1,$2,$3,$4)",
                    command.assessment_apply_request_id,
                    command.claim_id,
                    command.assessment_review_id,
                    command.assessment_apply_manifest_sha256,
                )
                if applied is None or (
                    str(_row_value(applied, "outcome")) != "applied"
                    or _row_exact_int(
                        applied,
                        "resulting_revision_number",
                        minimum=2,
                    )
                    != 2
                ):
                    raise GovernedClaimTransitionError(
                        "supported assessment apply drifted"
                    )
                assessment_rows = _row_exact_int(
                    applied,
                    "rows_written",
                    minimum=1,
                )
                if assessment_rows != 5:
                    raise GovernedClaimTransitionError(
                        "supported assessment row budget drifted"
                    )
                assessment_apply_event_id = _row_uuid(applied, "event_id")
                assessment_id = _row_uuid(applied, "assessment_id")
                outbox_id_raw = await conn.fetchval(
                    """/* governed_claim_transition:held_outbox_insert */
                    INSERT INTO memory.projection_outbox(
                      owner_user_id,aggregate_type,aggregate_id,operation,payload,
                      status,attempts,available_at,last_error,
                      lease_token,lease_expires_at,worker_id
                    ) VALUES(
                      $1,'claim',$2::uuid,'upsert',
                      jsonb_build_object(
                        'claim_id',$2::uuid,'revision_number',2
                      ),
                    'pending'::memory.outbox_status,0,'infinity'::timestamptz,
                    NULL,NULL,NULL,NULL
                    )
                    RETURNING outbox_id
                    """,
                    actor.owner_user_id,
                    command.claim_id,
                )
                try:
                    outbox_id = UUID(str(outbox_id_raw))
                except (TypeError, ValueError) as exc:
                    raise GovernedClaimTransitionError(
                        "held derived-index eligibility event was not created"
                    ) from exc
                rows_written = assessment_rows + 1
                authority_row = await _supported_authority(
                    conn,
                    actor=actor,
                    materialization=materialization,
                    command=command,
                    assessment_apply_event_id=assessment_apply_event_id,
                    assessment_id=assessment_id,
                    outbox_id=outbox_id,
                )

    _require_top_level_connection(conn)
    if actor is None or authority_row is None:
        raise GovernedClaimTransitionError("support transaction produced no result")
    receipt = _support_receipt_from_row(
        actor=actor,
        materialization=materialization,
        command=command,
        row=authority_row,
        authentication_manifest_sha256=(
            prior_receipt.actor_authentication_manifest_sha256
            if prior_receipt is not None
            else None
        ),
    )
    if prior_receipt is not None:
        if receipt.canonical_json_bytes() != prior_receipt.canonical_json_bytes():
            raise GovernedClaimTransitionError(
                "durable support state differs from prior receipt"
            )
        receipt = prior_receipt
    return GovernedClaimSupportResultV1(
        outcome="replayed" if prior_receipt is not None or reconciled else "applied",
        rows_written=0 if prior_receipt is not None or reconciled else rows_written,
        receipt=receipt,
        derived_index_eligibility_events_created=(
            0 if prior_receipt is not None or reconciled else 1
        ),
    )
