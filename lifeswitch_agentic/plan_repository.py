from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from .command_idempotency import begin_command, command_fingerprint, complete_command
from .identifiers import uuid7
from .outbox_repository import OutboxRepository
from .plan_domain import (
    PLAN_VALIDATION_VERSION,
    PlanDocumentV1,
    PlanDomainError,
    RevisionState,
    RevisionTrigger,
    diff_plan_documents,
    plan_validation_result,
    validate_activation,
    validate_revision_base,
)


SCHEMA = "lifeswitch_agentic"
IdFactory = Callable[[], uuid.UUID]


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    revision_id: uuid.UUID
    owner_user_id: uuid.UUID
    base_plan_version_id: uuid.UUID | None
    state: RevisionState
    document_sha256: str


@dataclass(frozen=True, slots=True)
class ProposalResult:
    revision_id: uuid.UUID
    state: RevisionState
    validation_status: str
    change_count: int


@dataclass(frozen=True, slots=True)
class ActivationResult:
    revision_id: uuid.UUID
    plan_version_id: uuid.UUID
    version_number: int
    prior_plan_version_id: uuid.UUID | None


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise PlanDomainError("invalid_stored_json", f"{field} must be a JSON object")
    return dict(value)


def _request_id(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > 200:
        raise PlanDomainError("request_id_too_long", "request_id exceeds 200 characters")
    return cleaned


def _owner_timezone(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 80:
        raise PlanDomainError("invalid_owner_timezone", "owner timezone is required")
    try:
        ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as exc:
        raise PlanDomainError("invalid_owner_timezone", "owner timezone is not recognized") from exc
    return cleaned


def _author_values(
    *,
    author_type: str,
    author_actor_user_id: uuid.UUID | None,
    author_agent_task_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    if author_type in {"owner", "coach"}:
        if author_actor_user_id is None or author_agent_task_id is not None:
            raise PlanDomainError("invalid_author", "human authors require only an actor user ID")
    elif author_type == "agent":
        if author_agent_task_id is None or author_actor_user_id is not None:
            raise PlanDomainError("invalid_author", "agent authors require only an agent task ID")
    else:
        raise PlanDomainError("invalid_author", "author_type must be owner|coach|agent")
    return author_actor_user_id, author_agent_task_id


def _review_cadence_days(document: PlanDocumentV1) -> int | None:
    value = document.monitoring_rules.get("review_every_days")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _revision_response(result: RevisionRecord) -> dict[str, Any]:
    return {
        "outcome": "ok",
        "revision_id": str(result.revision_id),
        "owner_user_id": str(result.owner_user_id),
        "base_plan_version_id": (
            str(result.base_plan_version_id) if result.base_plan_version_id else None
        ),
        "state": result.state.value,
        "document_sha256": result.document_sha256,
    }


def _revision_from_response(value: Mapping[str, Any]) -> RevisionRecord:
    return RevisionRecord(
        revision_id=uuid.UUID(str(value["revision_id"])),
        owner_user_id=uuid.UUID(str(value["owner_user_id"])),
        base_plan_version_id=(
            uuid.UUID(str(value["base_plan_version_id"]))
            if value.get("base_plan_version_id")
            else None
        ),
        state=RevisionState(str(value["state"])),
        document_sha256=str(value["document_sha256"]),
    )


def _proposal_response(result: ProposalResult) -> dict[str, Any]:
    return {
        "outcome": "ok",
        "revision_id": str(result.revision_id),
        "state": result.state.value,
        "validation_status": result.validation_status,
        "change_count": result.change_count,
    }


def _proposal_from_response(value: Mapping[str, Any]) -> ProposalResult:
    return ProposalResult(
        revision_id=uuid.UUID(str(value["revision_id"])),
        state=RevisionState(str(value["state"])),
        validation_status=str(value["validation_status"]),
        change_count=int(value["change_count"]),
    )


def _activation_response(result: ActivationResult) -> dict[str, Any]:
    return {
        "outcome": "ok",
        "revision_id": str(result.revision_id),
        "plan_version_id": str(result.plan_version_id),
        "version_number": result.version_number,
        "prior_plan_version_id": (
            str(result.prior_plan_version_id) if result.prior_plan_version_id else None
        ),
    }


def _activation_from_response(value: Mapping[str, Any]) -> ActivationResult:
    return ActivationResult(
        revision_id=uuid.UUID(str(value["revision_id"])),
        plan_version_id=uuid.UUID(str(value["plan_version_id"])),
        version_number=int(value["version_number"]),
        prior_plan_version_id=(
            uuid.UUID(str(value["prior_plan_version_id"]))
            if value.get("prior_plan_version_id")
            else None
        ),
    )


class PlanRepository:
    def __init__(
        self,
        *,
        id_factory: IdFactory = uuid7,
        outbox_repository: OutboxRepository | None = None,
    ) -> None:
        self._id_factory = id_factory
        self._outbox = outbox_repository or OutboxRepository(id_factory=id_factory)

    async def _lock_owner_state(
        self,
        conn: asyncpg.Connection,
        owner_user_id: uuid.UUID,
    ) -> asyncpg.Record:
        await conn.execute(
            f"""
            insert into {SCHEMA}.plan_owner_state (owner_user_id)
            values ($1)
            on conflict (owner_user_id) do nothing
            """,
            owner_user_id,
        )
        row = await conn.fetchrow(
            f"""
            select owner_user_id, active_plan_version_id, next_version_number
            from {SCHEMA}.plan_owner_state
            where owner_user_id = $1
            for update
            """,
            owner_user_id,
        )
        if row is None:
            raise RuntimeError("plan owner state lock failed")
        return row

    async def _insert_event(
        self,
        conn: asyncpg.Connection,
        *,
        revision_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        event_type: str,
        actor_user_id: uuid.UUID | None,
        agent_task_id: uuid.UUID | None,
        prior_state: RevisionState | None,
        new_state: RevisionState | None,
        reason_code: str | None,
        detail: Mapping[str, Any] | None,
        request_id: str | None,
    ) -> None:
        await conn.execute(
            f"""
            insert into {SCHEMA}.plan_revision_events (
              plan_revision_id, owner_user_id, event_type,
              actor_user_id, agent_task_id, prior_state, new_state,
              reason_code, detail, request_id
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
            """,
            revision_id,
            owner_user_id,
            event_type,
            actor_user_id,
            agent_task_id,
            prior_state.value if prior_state else None,
            new_state.value if new_state else None,
            reason_code,
            json.dumps(dict(detail or {}), sort_keys=True, separators=(",", ":")),
            _request_id(request_id),
        )

    async def create_draft(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        document: PlanDocumentV1,
        trigger: RevisionTrigger,
        base_plan_version_id: uuid.UUID | None,
        author_type: str,
        idempotency_key: str,
        author_actor_user_id: uuid.UUID | None = None,
        author_agent_task_id: uuid.UUID | None = None,
        supersedes_revision_id: uuid.UUID | None = None,
        recommendation_id: uuid.UUID | None = None,
        request_id: str | None = None,
    ) -> RevisionRecord:
        actor_id, agent_task_id = _author_values(
            author_type=author_type,
            author_actor_user_id=author_actor_user_id,
            author_agent_task_id=author_agent_task_id,
        )
        if author_type == "owner" and actor_id != owner_user_id:
            raise PlanDomainError("owner_actor_mismatch", "owner-authored draft requires the owner actor")
        revision_id = self._id_factory()
        document_json = document.canonical_json()
        document_sha256 = document.sha256()
        command_name = "plan.create_draft.v1"
        request_sha256 = command_fingerprint(
            {
                "document_sha256": document_sha256,
                "trigger": trigger,
                "base_plan_version_id": base_plan_version_id,
                "author_type": author_type,
                "author_actor_user_id": actor_id,
                "author_agent_task_id": agent_task_id,
                "supersedes_revision_id": supersedes_revision_id,
                "recommendation_id": recommendation_id,
            }
        )
        result: RevisionRecord | None = None

        async with conn.transaction():
            replay = await begin_command(
                conn,
                owner_user_id=owner_user_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                result = _revision_from_response(replay)
            else:
                owner_state = await self._lock_owner_state(conn, owner_user_id)
                current_active = owner_state["active_plan_version_id"]
                validate_revision_base(
                    trigger=trigger,
                    base_plan_version_id=base_plan_version_id,
                    current_active_plan_version_id=current_active,
                )

                if supersedes_revision_id is not None:
                    prior = await conn.fetchrow(
                        f"""
                        select base_plan_version_id, state
                        from {SCHEMA}.plan_revisions
                        where owner_user_id = $1 and id = $2
                        for update
                        """,
                        owner_user_id,
                        supersedes_revision_id,
                    )
                    if prior is None:
                        raise PlanDomainError("entity_out_of_scope", "superseded revision is unavailable")
                    if prior["state"] != RevisionState.NEEDS_CHANGES.value:
                        raise PlanDomainError(
                            "state_conflict",
                            "only a needs-changes revision can be superseded",
                        )
                    if prior["base_plan_version_id"] != base_plan_version_id:
                        raise PlanDomainError(
                            "state_conflict",
                            "superseding draft must preserve the plan base",
                        )

                await conn.execute(
                    f"""
                    insert into {SCHEMA}.plan_revisions (
                      id, owner_user_id, base_plan_version_id,
                      supersedes_revision_id, state, author_type,
                      author_actor_user_id, author_agent_task_id,
                      trigger_type, recommendation_id,
                      proposed_document, proposed_document_sha256, schema_version
                    )
                    values (
                      $1, $2, $3, $4, 'draft', $5,
                      $6, $7, $8, $9, $10::jsonb, $11, $12
                    )
                    """,
                    revision_id,
                    owner_user_id,
                    base_plan_version_id,
                    supersedes_revision_id,
                    author_type,
                    actor_id,
                    agent_task_id,
                    trigger.value,
                    recommendation_id,
                    document_json,
                    document_sha256,
                    document.schema_version,
                )
                await self._insert_event(
                    conn,
                    revision_id=revision_id,
                    owner_user_id=owner_user_id,
                    event_type="plan_revision_created",
                    actor_user_id=actor_id,
                    agent_task_id=agent_task_id,
                    prior_state=None,
                    new_state=RevisionState.DRAFT,
                    reason_code=trigger.value,
                    detail={
                        "base_plan_version_id": (
                            str(base_plan_version_id) if base_plan_version_id else None
                        )
                    },
                    request_id=request_id,
                )
                result = RevisionRecord(
                    revision_id=revision_id,
                    owner_user_id=owner_user_id,
                    base_plan_version_id=base_plan_version_id,
                    state=RevisionState.DRAFT,
                    document_sha256=document_sha256,
                )
                await complete_command(
                    conn,
                    owner_user_id=owner_user_id,
                    command_name=command_name,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    response=_revision_response(result),
                )

        if result is None:
            raise RuntimeError("create draft command produced no result")
        return result

    async def save_draft(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        revision_id: uuid.UUID,
        document: PlanDocumentV1,
        idempotency_key: str,
        actor_user_id: uuid.UUID | None = None,
        agent_task_id: uuid.UUID | None = None,
        request_id: str | None = None,
    ) -> RevisionRecord:
        if (actor_user_id is None) == (agent_task_id is None):
            raise PlanDomainError("invalid_actor", "provide exactly one actor or agent task")
        document_json = document.canonical_json()
        document_sha256 = document.sha256()
        command_name = "plan.save_draft.v1"
        request_sha256 = command_fingerprint(
            {
                "revision_id": revision_id,
                "document_sha256": document_sha256,
                "actor_user_id": actor_user_id,
                "agent_task_id": agent_task_id,
            }
        )
        result: RevisionRecord | None = None

        async with conn.transaction():
            replay = await begin_command(
                conn,
                owner_user_id=owner_user_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                result = _revision_from_response(replay)
            else:
                row = await conn.fetchrow(
                    f"""
                    select base_plan_version_id, state
                    from {SCHEMA}.plan_revisions
                    where owner_user_id = $1 and id = $2
                    for update
                    """,
                    owner_user_id,
                    revision_id,
                )
                if row is None:
                    raise PlanDomainError("entity_out_of_scope", "revision is unavailable")
                if row["state"] != RevisionState.DRAFT.value:
                    raise PlanDomainError("state_conflict", "only a draft revision can be edited")
                await conn.execute(
                    f"""
                    update {SCHEMA}.plan_revisions
                    set proposed_document = $3::jsonb,
                        proposed_document_sha256 = $4,
                        schema_version = $5,
                        updated_at = now()
                    where owner_user_id = $1 and id = $2
                    """,
                    owner_user_id,
                    revision_id,
                    document_json,
                    document_sha256,
                    document.schema_version,
                )
                await self._insert_event(
                    conn,
                    revision_id=revision_id,
                    owner_user_id=owner_user_id,
                    event_type="plan_revision_draft_saved",
                    actor_user_id=actor_user_id,
                    agent_task_id=agent_task_id,
                    prior_state=RevisionState.DRAFT,
                    new_state=RevisionState.DRAFT,
                    reason_code=None,
                    detail={},
                    request_id=request_id,
                )
                result = RevisionRecord(
                    revision_id=revision_id,
                    owner_user_id=owner_user_id,
                    base_plan_version_id=row["base_plan_version_id"],
                    state=RevisionState.DRAFT,
                    document_sha256=document_sha256,
                )
                await complete_command(
                    conn,
                    owner_user_id=owner_user_id,
                    command_name=command_name,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    response=_revision_response(result),
                )

        if result is None:
            raise RuntimeError("save draft command produced no result")
        return result

    async def propose_revision(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        revision_id: uuid.UUID,
        idempotency_key: str,
        actor_user_id: uuid.UUID | None = None,
        agent_task_id: uuid.UUID | None = None,
        request_id: str | None = None,
    ) -> ProposalResult:
        if (actor_user_id is None) == (agent_task_id is None):
            raise PlanDomainError("invalid_actor", "provide exactly one actor or agent task")

        command_name = "plan.propose_revision.v1"
        request_sha256 = command_fingerprint(
            {
                "revision_id": revision_id,
                "actor_user_id": actor_user_id,
                "agent_task_id": agent_task_id,
            }
        )

        snapshot = await conn.fetchrow(
            f"""
            select base_plan_version_id, state, trigger_type,
                   proposed_document, proposed_document_sha256
            from {SCHEMA}.plan_revisions
            where owner_user_id = $1 and id = $2
            """,
            owner_user_id,
            revision_id,
        )
        if snapshot is None:
            raise PlanDomainError("entity_out_of_scope", "revision is unavailable")
        document: PlanDocumentV1 | None = None
        validation: dict[str, Any] | None = None
        base_document: PlanDocumentV1 | None = None
        changes = ()
        change_rows: list[tuple[Any, ...]] = []
        existing_result: ProposalResult | None = None
        if snapshot["state"] == RevisionState.PROPOSED.value:
            existing_result = await self._proposal_result(conn, owner_user_id, revision_id)
        elif snapshot["state"] == RevisionState.DRAFT.value:
            document = PlanDocumentV1.from_mapping(
                _json_object(snapshot["proposed_document"], field="proposed_document")
            )
            validation = plan_validation_result(document)
            if validation["status"] == "invalid":
                raise PlanDomainError("revision_not_valid", "draft has deterministic validation errors")

            if snapshot["base_plan_version_id"] is not None:
                base_value = await conn.fetchval(
                    f"""
                    select document
                    from {SCHEMA}.plan_versions
                    where owner_user_id = $1 and id = $2
                    """,
                    owner_user_id,
                    snapshot["base_plan_version_id"],
                )
                if base_value is None:
                    raise PlanDomainError("entity_out_of_scope", "base plan version is unavailable")
                base_document = PlanDocumentV1.from_mapping(
                    _json_object(base_value, field="base document")
                )
            changes = diff_plan_documents(base_document, document)
            change_rows = [
                (
                    self._id_factory(),
                    revision_id,
                    change.field_path,
                    change.old_present,
                    json.dumps(change.old_value) if change.old_present else None,
                    change.new_present,
                    json.dumps(change.new_value) if change.new_present else None,
                )
                for change in changes
            ]

        result: ProposalResult | None = None

        async with conn.transaction():
            replay = await begin_command(
                conn,
                owner_user_id=owner_user_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                result = _proposal_from_response(replay)
            elif existing_result is not None:
                result = existing_result
                await complete_command(
                    conn,
                    owner_user_id=owner_user_id,
                    command_name=command_name,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    response=_proposal_response(result),
                )
            else:
                owner_state = await self._lock_owner_state(conn, owner_user_id)
                row = await conn.fetchrow(
                    f"""
                    select base_plan_version_id, state, trigger_type,
                           proposed_document_sha256
                    from {SCHEMA}.plan_revisions
                    where owner_user_id = $1 and id = $2
                    for update
                    """,
                    owner_user_id,
                    revision_id,
                )
                if row is None:
                    raise PlanDomainError("entity_out_of_scope", "revision is unavailable")
                if row["state"] == RevisionState.PROPOSED.value:
                    result = await self._proposal_result(conn, owner_user_id, revision_id)
                else:
                    if row["state"] != RevisionState.DRAFT.value:
                        raise PlanDomainError("state_conflict", "revision changed during validation")
                    if row["proposed_document_sha256"] != snapshot["proposed_document_sha256"]:
                        raise PlanDomainError("state_conflict", "draft changed during validation")
                    validate_revision_base(
                        trigger=RevisionTrigger(row["trigger_type"]),
                        base_plan_version_id=row["base_plan_version_id"],
                        current_active_plan_version_id=owner_state["active_plan_version_id"],
                    )
                    assert validation is not None
                    await conn.execute(
                        f"""
                        update {SCHEMA}.plan_revisions
                        set state = 'proposed', validation_version = $3,
                            validation_result = $4::jsonb,
                            proposed_at = now(), updated_at = now()
                        where owner_user_id = $1 and id = $2
                        """,
                        owner_user_id,
                        revision_id,
                        PLAN_VALIDATION_VERSION,
                        json.dumps(validation, sort_keys=True, separators=(",", ":")),
                    )
                    if change_rows:
                        await conn.executemany(
                            f"""
                            insert into {SCHEMA}.plan_revision_changes (
                              id, plan_revision_id, field_path,
                              old_present, old_value, new_present, new_value
                            )
                            values ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb)
                            """,
                            change_rows,
                        )
                    await self._insert_event(
                        conn,
                        revision_id=revision_id,
                        owner_user_id=owner_user_id,
                        event_type="plan_revision_proposed",
                        actor_user_id=actor_user_id,
                        agent_task_id=agent_task_id,
                        prior_state=RevisionState.DRAFT,
                        new_state=RevisionState.PROPOSED,
                        reason_code=None,
                        detail={
                            "change_count": len(changes),
                            "validation_status": validation["status"],
                        },
                        request_id=request_id,
                    )
                    await self._outbox.enqueue(
                        conn,
                        owner_user_id=owner_user_id,
                        aggregate_type="plan_revision",
                        aggregate_id=revision_id,
                        event_type="plan_revision.proposed",
                        payload={
                            "revision_id": str(revision_id),
                            "validation_status": validation["status"],
                            "change_count": len(changes),
                        },
                        request_id=_request_id(request_id),
                    )
                    result = ProposalResult(
                        revision_id=revision_id,
                        state=RevisionState.PROPOSED,
                        validation_status=validation["status"],
                        change_count=len(changes),
                    )
                await complete_command(
                    conn,
                    owner_user_id=owner_user_id,
                    command_name=command_name,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    response=_proposal_response(result),
                )

        if result is None:
            raise RuntimeError("propose revision command produced no result")
        return result

    async def approve_and_activate(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        revision_id: uuid.UUID,
        approving_actor_user_id: uuid.UUID,
        owner_timezone: str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> ActivationResult:
        if approving_actor_user_id != owner_user_id:
            raise PlanDomainError("owner_approval_required", "only the owner can approve activation")
        timezone_name = _owner_timezone(owner_timezone)
        command_name = "plan.approve_and_activate.v1"
        request_sha256 = command_fingerprint(
            {
                "revision_id": revision_id,
                "approving_actor_user_id": approving_actor_user_id,
                "owner_timezone": timezone_name,
            }
        )

        snapshot = await conn.fetchrow(
            f"""
            select state, proposed_document, proposed_document_sha256
            from {SCHEMA}.plan_revisions
            where owner_user_id = $1 and id = $2
            """,
            owner_user_id,
            revision_id,
        )
        if snapshot is None:
            raise PlanDomainError("entity_out_of_scope", "revision is unavailable")

        existing_result: ActivationResult | None = None
        document: PlanDocumentV1 | None = None
        current_validation: dict[str, Any] | None = None
        if snapshot["state"] == RevisionState.ACTIVATED.value:
            existing_result = await self._activation_result(conn, owner_user_id, revision_id)
        elif snapshot["state"] == RevisionState.PROPOSED.value:
            document = PlanDocumentV1.from_mapping(
                _json_object(snapshot["proposed_document"], field="proposed_document")
            )
            current_validation = plan_validation_result(document)
            if current_validation["status"] == "invalid":
                raise PlanDomainError("revision_not_valid", "revision no longer passes validation")

        plan_version_id = self._id_factory()
        error_code: str | None = None
        result: ActivationResult | None = None

        async with conn.transaction():
            replay = await begin_command(
                conn,
                owner_user_id=owner_user_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                if replay.get("outcome") == "error":
                    error_code = str(replay.get("error_code") or "state_conflict")
                else:
                    result = _activation_from_response(replay)
            elif existing_result is not None:
                result = existing_result
                await complete_command(
                    conn,
                    owner_user_id=owner_user_id,
                    command_name=command_name,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    response=_activation_response(result),
                )
            else:
                owner_state = await self._lock_owner_state(conn, owner_user_id)
                row = await conn.fetchrow(
                    f"""
                    select state, trigger_type, base_plan_version_id,
                           proposed_document_sha256, validation_version,
                           validation_result, activated_plan_version_id
                    from {SCHEMA}.plan_revisions
                    where owner_user_id = $1 and id = $2
                    for update
                    """,
                    owner_user_id,
                    revision_id,
                )
                if row is None:
                    raise PlanDomainError("entity_out_of_scope", "revision is unavailable")
                if row["state"] == RevisionState.ACTIVATED.value:
                    result = await self._activation_result(conn, owner_user_id, revision_id)
                else:
                    if row["state"] != RevisionState.PROPOSED.value:
                        raise PlanDomainError(
                            "revision_not_proposed",
                            "only a proposed revision can activate",
                        )
                    if document is None or current_validation is None:
                        raise PlanDomainError("state_conflict", "proposal changed during approval")
                    if row["proposed_document_sha256"] != snapshot["proposed_document_sha256"]:
                        raise PlanDomainError("state_conflict", "proposal payload changed unexpectedly")
                    if row["validation_version"] != PLAN_VALIDATION_VERSION:
                        raise PlanDomainError(
                            "validation_version_stale",
                            "proposal requires current validation",
                        )
                    stored_validation = _json_object(
                        row["validation_result"],
                        field="validation_result",
                    )
                    if stored_validation.get("status") != current_validation["status"]:
                        raise PlanDomainError(
                            "validation_version_stale",
                            "proposal validation result changed",
                        )

                    current_active = owner_state["active_plan_version_id"]
                    if (
                        RevisionTrigger(row["trigger_type"]) is RevisionTrigger.INITIAL_PLAN
                        and current_active is not None
                    ) or (
                        RevisionTrigger(row["trigger_type"]) is not RevisionTrigger.INITIAL_PLAN
                        and current_active != row["base_plan_version_id"]
                    ):
                        await conn.execute(
                            f"""
                            update {SCHEMA}.plan_revisions
                            set state = 'conflicted', resolved_at = now(), updated_at = now()
                            where owner_user_id = $1 and id = $2
                            """,
                            owner_user_id,
                            revision_id,
                        )
                        await self._insert_event(
                            conn,
                            revision_id=revision_id,
                            owner_user_id=owner_user_id,
                            event_type="plan_revision_conflicted",
                            actor_user_id=approving_actor_user_id,
                            agent_task_id=None,
                            prior_state=RevisionState.PROPOSED,
                            new_state=RevisionState.CONFLICTED,
                            reason_code="active_plan_changed",
                            detail={
                                "current_active_plan_version_id": (
                                    str(current_active) if current_active else None
                                )
                            },
                            request_id=request_id,
                        )
                        await self._outbox.enqueue(
                            conn,
                            owner_user_id=owner_user_id,
                            aggregate_type="plan_revision",
                            aggregate_id=revision_id,
                            event_type="plan_revision.conflicted",
                            payload={
                                "revision_id": str(revision_id),
                                "reason_code": "active_plan_changed",
                            },
                            request_id=_request_id(request_id),
                        )
                        error_code = "state_conflict"
                    else:
                        result = await self._activate_locked(
                            conn,
                            owner_user_id=owner_user_id,
                            revision_id=revision_id,
                            approving_actor_user_id=approving_actor_user_id,
                            owner_timezone=timezone_name,
                            document=document,
                            current_validation=current_validation,
                            owner_state=owner_state,
                            revision_row=row,
                            plan_version_id=plan_version_id,
                            request_id=request_id,
                        )

                response: Mapping[str, Any]
                if error_code is not None:
                    response = {
                        "outcome": "error",
                        "error_code": error_code,
                        "revision_id": str(revision_id),
                    }
                else:
                    if result is None:
                        raise RuntimeError("activation transaction produced no result")
                    response = _activation_response(result)
                await complete_command(
                    conn,
                    owner_user_id=owner_user_id,
                    command_name=command_name,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    response=response,
                )

        if error_code is not None:
            raise PlanDomainError(error_code, "active plan changed before approval")
        if result is None:
            raise RuntimeError("activation transaction produced no result")
        return result

    async def _proposal_result(
        self,
        conn: asyncpg.Connection,
        owner_user_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> ProposalResult:
        row = await conn.fetchrow(
            f"""
            select validation_result,
                   (
                     select count(*)
                     from {SCHEMA}.plan_revision_changes change
                     where change.plan_revision_id = revision.id
                   ) as change_count
            from {SCHEMA}.plan_revisions revision
            where revision.owner_user_id = $1
              and revision.id = $2
              and revision.state = 'proposed'
            """,
            owner_user_id,
            revision_id,
        )
        if row is None:
            raise PlanDomainError("state_conflict", "revision is not proposed")
        validation = _json_object(row["validation_result"], field="validation_result")
        return ProposalResult(
            revision_id=revision_id,
            state=RevisionState.PROPOSED,
            validation_status=str(validation["status"]),
            change_count=int(row["change_count"]),
        )

    async def _activate_locked(
        self,
        conn: asyncpg.Connection,
        *,
        owner_user_id: uuid.UUID,
        revision_id: uuid.UUID,
        approving_actor_user_id: uuid.UUID,
        owner_timezone: str,
        document: PlanDocumentV1,
        current_validation: Mapping[str, Any],
        owner_state: asyncpg.Record,
        revision_row: asyncpg.Record,
        plan_version_id: uuid.UUID,
        request_id: str | None,
    ) -> ActivationResult:
        decision = validate_activation(
            proposal_state=RevisionState.PROPOSED,
            trigger=RevisionTrigger(revision_row["trigger_type"]),
            base_plan_version_id=revision_row["base_plan_version_id"],
            current_active_plan_version_id=owner_state["active_plan_version_id"],
            owner_user_id=owner_user_id,
            approving_actor_user_id=approving_actor_user_id,
            validation_status=str(current_validation["status"]),
            next_version_number=int(owner_state["next_version_number"]),
        )
        if decision.prior_active_plan_version_id is not None:
            await conn.execute(
                f"""
                update {SCHEMA}.plan_versions
                set status = 'superseded', superseded_at = now()
                where owner_user_id = $1 and id = $2 and status = 'active'
                """,
                owner_user_id,
                decision.prior_active_plan_version_id,
            )
        await conn.execute(
            f"""
            insert into {SCHEMA}.plan_versions (
              id, owner_user_id, version_number, status,
              phase_code, goal_summary, starts_on, target_date,
              owner_timezone, review_cadence_days,
              document, document_sha256, schema_version,
              validation_version, source_revision_id,
              activation_type, activated_by_actor_user_id, activated_at
            )
            values (
              $1, $2, $3, 'active', $4, $5, $6, $7,
              $8, $9, $10::jsonb, $11, $12, $13, $14,
              'owner_approval', $15, now()
            )
            """,
            plan_version_id,
            owner_user_id,
            decision.next_version_number,
            document.phase,
            document.primary_goal,
            document.start_date,
            None,
            owner_timezone,
            _review_cadence_days(document),
            document.canonical_json(),
            document.sha256(),
            document.schema_version,
            PLAN_VALIDATION_VERSION,
            revision_id,
            approving_actor_user_id,
        )
        await conn.execute(
            f"""
            update {SCHEMA}.plan_owner_state
            set active_plan_version_id = $2,
                next_version_number = $3,
                updated_at = now()
            where owner_user_id = $1
            """,
            owner_user_id,
            plan_version_id,
            decision.next_version_number + 1,
        )
        await conn.execute(
            f"""
            update {SCHEMA}.plan_revisions
            set state = 'activated', activated_plan_version_id = $3,
                resolved_at = now(), updated_at = now()
            where owner_user_id = $1 and id = $2
            """,
            owner_user_id,
            revision_id,
            plan_version_id,
        )
        await self._insert_event(
            conn,
            revision_id=revision_id,
            owner_user_id=owner_user_id,
            event_type="plan_revision_activated",
            actor_user_id=approving_actor_user_id,
            agent_task_id=None,
            prior_state=RevisionState.PROPOSED,
            new_state=RevisionState.ACTIVATED,
            reason_code="owner_approval",
            detail={
                "plan_version_id": str(plan_version_id),
                "version_number": decision.next_version_number,
            },
            request_id=request_id,
        )
        await self._outbox.enqueue(
            conn,
            owner_user_id=owner_user_id,
            aggregate_type="plan_version",
            aggregate_id=plan_version_id,
            event_type="plan_version.activated",
            payload={
                "plan_version_id": str(plan_version_id),
                "revision_id": str(revision_id),
                "version_number": decision.next_version_number,
            },
            request_id=_request_id(request_id),
        )
        return ActivationResult(
            revision_id=revision_id,
            plan_version_id=plan_version_id,
            version_number=decision.next_version_number,
            prior_plan_version_id=decision.prior_active_plan_version_id,
        )

    async def _activation_result(
        self,
        conn: asyncpg.Connection,
        owner_user_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> ActivationResult:
        row = await conn.fetchrow(
            f"""
            select pv.id, pv.version_number, pr.base_plan_version_id
            from {SCHEMA}.plan_revisions pr
            join {SCHEMA}.plan_versions pv
              on pv.owner_user_id = pr.owner_user_id
             and pv.id = pr.activated_plan_version_id
            where pr.owner_user_id = $1 and pr.id = $2
              and pr.state = 'activated'
            """,
            owner_user_id,
            revision_id,
        )
        if row is None:
            raise PlanDomainError("state_conflict", "activated revision has no plan version")
        return ActivationResult(
            revision_id=revision_id,
            plan_version_id=row["id"],
            version_number=int(row["version_number"]),
            prior_plan_version_id=row["base_plan_version_id"],
        )
