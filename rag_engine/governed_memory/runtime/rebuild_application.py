from __future__ import annotations

"""Operator entrypoint for prepare, cutover, and rollback of one rebuild."""

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..contracts import ContractViolation, canonical_json_bytes
from .https_transport import RetryFreeBoundedHttpsTransport
from .openai_adapters import OpenAIEmbeddingAdapter, OpenAIEndpointConfig
from .rebuild_adapters import (
    ExactRebuildQdrantStore,
    OpenAIRebuildEmbeddingProvider,
    PostgresProjectionRebuildRepository,
)
from .rebuild_controller import (
    RebuildAliasReceipt,
    RebuildPrepareReceipt,
    SuccessorRebuildController,
)
from .rebuild_qdrant_transport import LoopbackRebuildQdrantTransport
from .worker_application import (
    PostgresAdvisoryGuard,
    WorkerRuntimeConfig,
    WorkerRuntimeRefusal,
    _configure_connection,
    _connect,
)


REBUILD_APPLICATION_NAME = "governed-memory-successor-rebuild"
MAX_RECEIPT_BYTES = 16_384


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def prepare_receipt_from_json(raw: bytes) -> RebuildPrepareReceipt:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RECEIPT_BYTES:
        raise ContractViolation("invalid_rebuild_prepare_receipt_json")
    try:
        value = json.loads(raw, object_pairs_hook=_closed_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractViolation("invalid_rebuild_prepare_receipt_json") from exc
    fields = {
        "alias",
        "source_collection",
        "target_collection",
        "point_count",
        "manifest_sha256",
        "target_verification_receipt_sha256",
        "receipt_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractViolation("invalid_rebuild_prepare_receipt_json")
    try:
        return RebuildPrepareReceipt(**value)
    except TypeError as exc:
        raise ContractViolation("invalid_rebuild_prepare_receipt_json") from exc


def _store(config: WorkerRuntimeConfig) -> ExactRebuildQdrantStore:
    return ExactRebuildQdrantStore(
        LoopbackRebuildQdrantTransport(
            base_url=config.qdrant_url,
            api_key=config.qdrant_api_key,
        )
    )


async def _default_connection(config: WorkerRuntimeConfig) -> Any:
    return await _connect(config.postgres_dsn, REBUILD_APPLICATION_NAME)


ConnectionFactory = Callable[[WorkerRuntimeConfig], Awaitable[Any]]


async def prepare_rebuild(
    config: WorkerRuntimeConfig,
    *,
    source_collection: str,
    target_collection: str,
    connection_factory: ConnectionFactory = _default_connection,
    qdrant: ExactRebuildQdrantStore | None = None,
    embedding_provider: OpenAIRebuildEmbeddingProvider | None = None,
) -> RebuildPrepareReceipt:
    if not isinstance(config, WorkerRuntimeConfig):
        raise ContractViolation("invalid_rebuild_runtime_config")
    connection = await connection_factory(config)
    guard: PostgresAdvisoryGuard | None = None
    guard_acquired = False
    try:
        await _configure_connection(connection)
        guard = PostgresAdvisoryGuard(connection)
        if await guard.acquire() is not True:
            raise WorkerRuntimeRefusal("governed_memory_worker_lock_contended")
        guard_acquired = True
        store = qdrant or _store(config)
        provider = embedding_provider or OpenAIRebuildEmbeddingProvider(
            OpenAIEmbeddingAdapter(
                config=OpenAIEndpointConfig(
                    api_key=config.openai_api_key,
                    extraction_model=config.extraction_model,
                ),
                transport=RetryFreeBoundedHttpsTransport(timeout_seconds=30),
            )
        )
        async with connection.transaction(
            isolation="repeatable_read",
            readonly=True,
        ):
            return await SuccessorRebuildController(
                repository=PostgresProjectionRebuildRepository(connection),
                embedding_provider=provider,
                qdrant=store,
            ).prepare(
                source_collection=source_collection,
                target_collection=target_collection,
            )
    finally:
        try:
            if guard is not None and guard_acquired:
                await guard.release()
        finally:
            await connection.close()


async def move_rebuild_alias(
    config: WorkerRuntimeConfig,
    prepared: RebuildPrepareReceipt,
    *,
    rollback: bool,
    qdrant: ExactRebuildQdrantStore | None = None,
) -> RebuildAliasReceipt:
    if not isinstance(config, WorkerRuntimeConfig) or type(rollback) is not bool:
        raise ContractViolation("invalid_rebuild_runtime_config")
    store = qdrant or _store(config)
    await store.bind_existing_generations(
        source_collection=prepared.source_collection,
        target_collection=prepared.target_collection,
        expected_active_collection=(
            prepared.target_collection if rollback else prepared.source_collection
        ),
    )
    controller = SuccessorRebuildController(
        repository=object(),  # unused for receipt-bound alias actions
        embedding_provider=object(),  # unused for receipt-bound alias actions
        qdrant=store,
    )
    return (
        await controller.rollback(prepared)
        if rollback
        else await controller.cutover(prepared)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="governed-memory-rebuild")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--target", required=True)
    for command in ("cutover", "rollback"):
        action = subparsers.add_parser(command)
        action.add_argument("--receipt", required=True)
    return parser


async def _run(arguments: argparse.Namespace) -> object:
    config = WorkerRuntimeConfig.from_environment(os.environ)
    if arguments.command == "prepare":
        return await prepare_rebuild(
            config,
            source_collection=arguments.source,
            target_collection=arguments.target,
        )
    raw = Path(arguments.receipt).read_bytes()
    prepared = prepare_receipt_from_json(raw)
    return await move_rebuild_alias(
        config,
        prepared,
        rollback=arguments.command == "rollback",
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        receipt = asyncio.run(_run(arguments))
    except Exception as exc:
        code = str(exc)
        if not code or any(character.isspace() for character in code):
            code = "governed_memory_rebuild_failed"
        print(code, file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(asdict(receipt)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "main",
    "move_rebuild_alias",
    "prepare_rebuild",
    "prepare_receipt_from_json",
    "REBUILD_APPLICATION_NAME",
]
