from __future__ import annotations

"""Static-SQL owner API adapter for the clean governed-Memory successor.

The HTTP layer supplies a cryptographically verified owner.  This adapter
binds that owner and the authentication receipt to a single short database
transaction before invoking the closed ``memory_private`` API.  It never
accepts caller SQL, a caller-selected database role, or an owner identifier
from an HTTP field.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import math
from typing import Any, AsyncIterator, Mapping, Sequence
from uuid import UUID

from .auth import ActorRole, ActorScope, VerifiedActor, require_scope
from .contracts import ContractViolation, require_sha256, require_uuid
from .postgres_adapter import normalize_postgres_record


_STATUS_SQL = "SELECT * FROM memory_private.read_status()"
_LIST_CLAIMS_SQL = (
    "SELECT * FROM memory_private.list_claims("
    "$1::uuid,$2::integer,NULL::timestamptz,NULL::uuid)"
)
_READ_CLAIM_SQL = "SELECT * FROM memory_private.read_claim($1::uuid)"
_LIST_PROPOSALS_SQL = (
    "SELECT * FROM memory_private.list_proposals("
    "$1::integer,NULL::timestamptz,NULL::uuid)"
)
_READ_OPERATION_SQL = (
    "SELECT * FROM memory_private.read_operation($1::uuid)"
)
_REVIEW_PROPOSAL_SQL = (
    "SELECT * FROM memory_private.review_proposal("
    "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::text,$7::text,"
    "$8::text,$9::text[])"
)
_CORRECT_CLAIM_SQL = (
    "SELECT * FROM memory_private.correct_claim("
    "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::text::jsonb)"
)
_RETRACT_CLAIM_SQL = (
    "SELECT * FROM memory_private.retract_claim("
    "$1::uuid,$2::uuid,$3::text,$4::text)"
)
_DELETE_CLAIM_SQL = (
    "SELECT * FROM memory_private.request_claim_deletion("
    "$1::uuid,$2::uuid,$3::text,$4::text)"
)


class OwnerStoreError(RuntimeError):
    """A stable, content-free database-boundary failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _authorized_owner(actor: VerifiedActor, scope: ActorScope) -> None:
    if not isinstance(actor, VerifiedActor):
        raise ContractViolation("unverified_actor")
    if actor.role is not ActorRole.OWNER or actor.actor_id != actor.owner_user_id:
        raise ContractViolation("owner_actor_required")
    require_scope(actor, scope)


def _uuid_field(body: Mapping[str, Any], name: str) -> UUID:
    value = body.get(name)
    try:
        parsed = value if isinstance(value, UUID) else UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ContractViolation(f"invalid_{name}") from None
    return require_uuid(parsed, f"invalid_{name}")


def _sha256_field(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    require_sha256(value, f"invalid_{name}")
    assert isinstance(value, str)
    return value


def _database_error(error: Exception) -> OwnerStoreError:
    sqlstate = getattr(error, "sqlstate", None)
    code = {
        "22023": "database_request_rejected",
        "23503": "database_conflict",
        "23505": "database_conflict",
        "23514": "database_conflict",
        "40001": "database_conflict",
        "42501": "database_authority_denied",
        "P0001": "database_operation_rejected",
        "P0002": "database_resource_not_found",
    }.get(sqlstate, "database_unavailable")
    return OwnerStoreError(code)


class PostgresOwnerStore:
    """Owner-only facade over a transaction-mode compatible async pool."""

    def __init__(
        self,
        pool: Any,
        *,
        page_limit: int = 100,
        operation_timeout_seconds: float = 8.0,
    ) -> None:
        if pool is None:
            raise ContractViolation("owner_store_pool_required")
        if type(page_limit) is not int or not 1 <= page_limit <= 100:
            raise ContractViolation("invalid_owner_store_page_limit")
        if (
            type(operation_timeout_seconds) not in (int, float)
            or not math.isfinite(operation_timeout_seconds)
            or not 0 < operation_timeout_seconds <= 60
        ):
            raise ContractViolation("invalid_owner_store_operation_timeout")
        self._pool = pool
        self._page_limit = page_limit
        self._operation_timeout_seconds = float(operation_timeout_seconds)

    @asynccontextmanager
    async def _owner_transaction(
        self,
        actor: VerifiedActor,
        scope: ActorScope,
    ) -> AsyncIterator[Any]:
        _authorized_owner(actor, scope)
        try:
            async with (
                asyncio.timeout(self._operation_timeout_seconds),
                self._pool.acquire() as connection,
            ):
                async with connection.transaction():
                    await connection.execute(
                        "SET LOCAL statement_timeout = '5s'"
                    )
                    await connection.execute("SET LOCAL lock_timeout = '2s'")
                    await connection.execute(
                        "SELECT pg_catalog.set_config("
                        "'app.user_id',$1::text,true)",
                        str(actor.owner_user_id),
                    )
                    await connection.execute(
                        "SELECT pg_catalog.set_config("
                        "'app.auth_context_sha256',$1::text,true)",
                        actor.authentication_manifest_sha256,
                    )
                    bound_owner = await connection.fetchval(
                        "SELECT memory_private.current_owner_id()"
                    )
                    if not isinstance(bound_owner, UUID):
                        raise OwnerStoreError("database_unavailable")
                    if bound_owner != actor.owner_user_id:
                        raise OwnerStoreError("database_owner_context_mismatch")
                    yield connection
        except (ContractViolation, OwnerStoreError):
            raise
        except Exception as error:
            raise _database_error(error) from None

    @staticmethod
    def _rows(values: Sequence[Any]) -> list[dict[str, Any]]:
        return [
            normalize_postgres_record(dict(value))
            for value in values
        ]

    @staticmethod
    def _row(value: Any) -> dict[str, Any]:
        if value is None:
            raise OwnerStoreError("database_operation_missing_receipt")
        return normalize_postgres_record(dict(value))

    async def status(self, actor: VerifiedActor) -> dict[str, Any]:
        async with self._owner_transaction(actor, ActorScope.READ_CLAIMS) as connection:
            return self._row(await connection.fetchrow(_STATUS_SQL))

    async def list_claims(self, actor: VerifiedActor) -> list[dict[str, Any]]:
        async with self._owner_transaction(actor, ActorScope.READ_CLAIMS) as connection:
            rows = await connection.fetch(
                _LIST_CLAIMS_SQL,
                None,
                self._page_limit,
            )
        return self._rows(rows)

    async def get_claim(
        self, actor: VerifiedActor, claim_id: UUID
    ) -> dict[str, Any] | None:
        claim = require_uuid(claim_id, "invalid_claim_id")
        async with self._owner_transaction(actor, ActorScope.READ_CLAIMS) as connection:
            rows = await connection.fetch(_READ_CLAIM_SQL, claim)
        normalized = self._rows(rows)
        if len(normalized) > 1:
            raise OwnerStoreError("database_claim_cardinality_violation")
        return normalized[0] if normalized else None

    async def list_proposals(self, actor: VerifiedActor) -> list[dict[str, Any]]:
        async with self._owner_transaction(
            actor, ActorScope.REVIEW_PROPOSALS
        ) as connection:
            rows = await connection.fetch(
                _LIST_PROPOSALS_SQL,
                self._page_limit,
            )
        return self._rows(rows)

    async def review_proposal(
        self,
        actor: VerifiedActor,
        proposal_id: UUID,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        proposal = require_uuid(proposal_id, "invalid_proposal_id")
        operation = _uuid_field(body, "operation_id")
        reason_codes = body.get("reason_codes")
        if (
            not isinstance(reason_codes, list)
            or any(not isinstance(item, str) for item in reason_codes)
            or reason_codes != sorted(set(reason_codes))
        ):
            raise ContractViolation("invalid_reason_codes")
        public_decision = body.get("decision")
        decision = {"admit": "admitted", "reject": "rejected"}.get(
            public_decision
        )
        if decision is None:
            raise ContractViolation("invalid_review_decision")
        if decision == "admitted" and reason_codes != ["explicit_owner_review"]:
            raise ContractViolation("invalid_reason_codes")
        if decision == "rejected" and (
            not reason_codes
            or not set(reason_codes).issubset(
                {"duplicate_existing", "not_durable", "proposal_incorrect"}
            )
        ):
            raise ContractViolation("invalid_reason_codes")
        arguments = (
            operation,
            proposal,
            decision,
            _sha256_field(body, "expected_proposal_sha256"),
            _sha256_field(body, "expected_source_sha256"),
            _sha256_field(body, "expected_selected_sha256"),
            _sha256_field(body, "expected_selection_binding_sha256"),
            _sha256_field(body, "expected_predicate_catalog_sha256"),
            reason_codes,
        )
        async with self._owner_transaction(
            actor, ActorScope.REVIEW_PROPOSALS
        ) as connection:
            return self._row(
                await connection.fetchrow(_REVIEW_PROPOSAL_SQL, *arguments)
            )

    async def correct_claim(
        self,
        actor: VerifiedActor,
        claim_id: UUID,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        claim = require_uuid(claim_id, "invalid_claim_id")
        replacement = body.get("replacement")
        if not isinstance(replacement, Mapping):
            raise ContractViolation("invalid_replacement")
        try:
            replacement_json = json.dumps(
                dict(replacement),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise ContractViolation("invalid_replacement") from None
        arguments = (
            _uuid_field(body, "operation_id"),
            claim,
            _sha256_field(body, "expected_revision_sha256"),
            _sha256_field(body, "expected_state_sha256"),
            _sha256_field(body, "expected_predicate_catalog_sha256"),
            replacement_json,
        )
        async with self._owner_transaction(
            actor, ActorScope.MUTATE_CLAIMS
        ) as connection:
            return self._row(
                await connection.fetchrow(_CORRECT_CLAIM_SQL, *arguments)
            )

    async def retract_claim(
        self,
        actor: VerifiedActor,
        claim_id: UUID,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._claim_transition(
            actor,
            claim_id,
            body,
            query=_RETRACT_CLAIM_SQL,
        )

    async def delete_claim(
        self,
        actor: VerifiedActor,
        claim_id: UUID,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._claim_transition(
            actor,
            claim_id,
            body,
            query=_DELETE_CLAIM_SQL,
        )

    async def _claim_transition(
        self,
        actor: VerifiedActor,
        claim_id: UUID,
        body: Mapping[str, Any],
        *,
        query: str,
    ) -> dict[str, Any]:
        claim = require_uuid(claim_id, "invalid_claim_id")
        arguments = (
            _uuid_field(body, "operation_id"),
            claim,
            _sha256_field(body, "expected_revision_sha256"),
            _sha256_field(body, "expected_state_sha256"),
        )
        async with self._owner_transaction(
            actor, ActorScope.MUTATE_CLAIMS
        ) as connection:
            return self._row(await connection.fetchrow(query, *arguments))

    async def get_operation(
        self,
        actor: VerifiedActor,
        operation_id: UUID,
    ) -> dict[str, Any] | None:
        operation = require_uuid(operation_id, "invalid_operation_id")
        async with self._owner_transaction(actor, ActorScope.READ_CLAIMS) as connection:
            rows = await connection.fetch(_READ_OPERATION_SQL, operation)
        events = self._rows(rows)
        if not events:
            return None
        return {"operation_id": str(operation), "events": events}


__all__ = ["OwnerStoreError", "PostgresOwnerStore"]
