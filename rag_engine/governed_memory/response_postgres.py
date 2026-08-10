from __future__ import annotations

"""Static-SQL PostgreSQL authority adapter for successor chat retrieval."""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping, Sequence
from uuid import UUID

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    require_sha256,
    require_uuid,
)
from rag_engine.governed_memory.postgres_adapter import normalize_postgres_record
from rag_engine.governed_memory.response_provider import SuccessorResponseActorBinding


_READ_CLAIMS_SQL = (
    "SELECT * FROM memory_private.read_claim_candidates($1::uuid[])"
)
_PERSIST_BINDING_SQL = (
    "SELECT * FROM memory_private.record_answer_binding("
    "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text[],$7::text[],"
    "$8::text[],$9::integer,$10::integer,$11::text,$12::text,$13::boolean,"
    "$14::uuid[],$15::uuid[],$16::text,$17::text,$18::text,$19::text,$20::text)"
)


class SuccessorResponsePostgresError(RuntimeError):
    pass


class PostgresSuccessorResponseRepository:
    def __init__(self, pool: Any, *, timeout_seconds: float = 8.0) -> None:
        if pool is None:
            raise ContractViolation("response_memory_pool_required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 60
        ):
            raise ContractViolation("invalid_response_memory_timeout")
        self._pool = pool
        self._timeout_seconds = float(timeout_seconds)

    @asynccontextmanager
    async def _transaction(
        self,
        actor: SuccessorResponseActorBinding,
    ) -> AsyncIterator[Any]:
        if not isinstance(actor, SuccessorResponseActorBinding):
            raise ContractViolation("invalid_response_memory_actor")
        try:
            async with (
                asyncio.timeout(self._timeout_seconds),
                self._pool.acquire() as connection,
            ):
                async with connection.transaction():
                    await connection.execute("SET LOCAL statement_timeout = '5s'")
                    await connection.execute("SET LOCAL lock_timeout = '2s'")
                    await connection.execute(
                        "SELECT pg_catalog.set_config('app.user_id',$1::text,true)",
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
                    if bound_owner != actor.owner_user_id:
                        raise SuccessorResponsePostgresError(
                            "response_memory_owner_context_mismatch"
                        )
                    yield connection
        except (ContractViolation, SuccessorResponsePostgresError):
            raise
        except Exception:
            raise SuccessorResponsePostgresError(
                "response_memory_postgres_unavailable"
            ) from None

    async def read_claims(
        self,
        *,
        actor: SuccessorResponseActorBinding,
        claim_ids: Sequence[UUID],
    ) -> tuple[Mapping[str, object], ...]:
        values = tuple(require_uuid(item, "invalid_response_memory_claim") for item in claim_ids)
        if not 1 <= len(values) <= 8 or len(set(values)) != len(values):
            raise ContractViolation("invalid_response_memory_claim_ids")
        async with self._transaction(actor) as connection:
            rows = await connection.fetch(_READ_CLAIMS_SQL, list(values))
        return tuple(normalize_postgres_record(dict(row)) for row in rows)

    async def persist_answer_binding(
        self,
        *,
        actor: SuccessorResponseActorBinding,
        operation_id: UUID,
        thread_id: UUID,
        binding: Mapping[str, object],
        memory_block: str,
        outbound_request: str,
    ) -> None:
        operation = require_uuid(operation_id, "invalid_response_memory_operation")
        thread = require_uuid(thread_id, "invalid_response_memory_thread")
        if thread != actor.thread_id:
            raise ContractViolation("response_memory_thread_binding_mismatch")
        if not isinstance(binding, Mapping):
            raise ContractViolation("invalid_response_memory_answer_binding")
        if (
            binding.get("owner_user_id") != str(actor.owner_user_id)
            or binding.get("dispatch_state") != "dispatched"
            or binding.get("outcome") != "exposed"
        ):
            raise ContractViolation("response_memory_answer_binding_mismatch")
        response_id = require_uuid(
            binding.get("response_id"),
            "invalid_response_memory_answer",
        )
        for field in (
            "query_sha256",
            "policy_sha256",
            "renderer_sha256",
            "prompt_sha256",
            "selection_manifest_sha256",
            "injection_manifest_sha256",
        ):
            require_sha256(binding.get(field), f"invalid_response_memory_{field}")
        selected = tuple(
            require_uuid(value, "invalid_response_memory_selected_claim")
            for value in binding.get("selected_claim_ids", ())
        )
        injected = tuple(
            require_uuid(value, "invalid_response_memory_injected_claim")
            for value in binding.get("injected_claim_ids", ())
        )
        if not selected or not injected:
            raise ContractViolation("response_memory_answer_selection_empty")
        arguments = (
            operation,
            response_id,
            thread,
            binding["query_sha256"],
            binding["policy_sha256"],
            list(binding["allowed_predicates"]),
            list(binding["domains"]),
            list(binding["intents"]),
            binding["max_records"],
            binding["policy_revision"],
            binding["renderer_sha256"],
            binding["prompt_sha256"],
            binding["explicit_recall"],
            list(selected),
            list(injected),
            "exposed",
            binding["selection_manifest_sha256"],
            binding["injection_manifest_sha256"],
            memory_block,
            outbound_request,
        )
        async with self._transaction(actor) as connection:
            row = await connection.fetchrow(_PERSIST_BINDING_SQL, *arguments)
            if row is None:
                raise SuccessorResponsePostgresError(
                    "response_memory_binding_receipt_missing"
                )
            receipt = dict(row)
            if set(receipt) != {
                "binding_id",
                "selection_manifest_sha256",
                "injection_manifest_sha256",
            }:
                raise SuccessorResponsePostgresError(
                    "response_memory_binding_receipt_invalid"
                )
            try:
                require_uuid(
                    receipt.get("binding_id"),
                    "invalid_response_memory_binding_receipt",
                )
                receipt_selection = require_sha256(
                    receipt.get("selection_manifest_sha256"),
                    "invalid_response_memory_binding_receipt",
                )
                receipt_injection = require_sha256(
                    receipt.get("injection_manifest_sha256"),
                    "invalid_response_memory_binding_receipt",
                )
            except ContractViolation as exc:
                raise SuccessorResponsePostgresError(
                    "response_memory_binding_receipt_invalid"
                ) from exc
            if (
                receipt_selection != binding["selection_manifest_sha256"]
                or receipt_injection != binding["injection_manifest_sha256"]
            ):
                raise SuccessorResponsePostgresError(
                    "response_memory_binding_receipt_mismatch"
                )


__all__ = [
    "PostgresSuccessorResponseRepository",
    "SuccessorResponsePostgresError",
]
