from __future__ import annotations

"""Private, capability-bound access to the AI Operations monitor inbox."""

import json
from typing import Any, Mapping, Protocol
from uuid import UUID


LIST_SCHEMA = "admin_ai_operations_incidents_v1"
LIST_SOURCE_CONTRACT = "ai_operations_monitor_inbox_v1"
MUTATION_SCHEMA = "admin_ai_operations_mutation_v1"
MUTATION_SOURCE_CONTRACT = "ai_operations_monitor_mutation_v1"
READ_CAPABILITY = "inspector.view"
MANAGE_CAPABILITY = "incident.manage"
VALID_STATES = {"open", "acknowledged", "resolved"}
VALID_SEVERITIES = {"info", "warning", "critical", "test"}
VALID_OBSERVATION_STATUSES = {"violated", "unavailable", "drill"}
MAX_LIMIT = 100
MAX_JSON_BYTES = 1_048_576
INCIDENT_KEYS = {
    "incident_id",
    "monitor_name",
    "state",
    "severity",
    "is_drill",
    "observation_status",
    "first_seen_at",
    "last_seen_at",
    "acknowledged_at",
    "acknowledged_by",
    "resolved_at",
    "resolved_by",
    "observation_count",
    "reason_codes",
    "window_hours",
    "request_count",
    "completed_count",
    "fail_closed_count",
    "relevance_fail_closed_count",
    "dependency_failure_count",
    "fail_closed_rate",
}


class AiOperationsError(RuntimeError):
    def __init__(
        self,
        code: str,
        status_code: int = 500,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class AiOperationsRepositoryError(RuntimeError):
    def __init__(self, sqlstate: str = "") -> None:
        super().__init__("ai_operations_repository_error")
        self.sqlstate = str(sqlstate or "")


class AiOperationsRepository(Protocol):
    async def list_incidents(
        self,
        *,
        actor_user_id: str,
        capability: str,
        state: str | None,
        limit: int,
    ) -> Any: ...

    async def acknowledge_incident(
        self,
        *,
        actor_user_id: str,
        capability: str,
        incident_id: str,
    ) -> Any: ...

    async def resolve_incident(
        self,
        *,
        actor_user_id: str,
        capability: str,
        incident_id: str,
    ) -> Any: ...


def _uuid(value: str | UUID, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _decode_jsonb(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        if not value or len(value.encode("utf-8")) > MAX_JSON_BYTES:
            raise AiOperationsError("ai_operations_contract_invalid")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AiOperationsError(
                "ai_operations_contract_invalid"
            ) from exc
    if not isinstance(value, Mapping):
        raise AiOperationsError("ai_operations_contract_invalid")
    return value


def _timestamp_or_none(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and 1 <= len(value) <= 64
    )


def _uuid_or_none(value: Any) -> bool:
    if value is None:
        return True
    try:
        UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def _bounded_integer(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 9_223_372_036_854_775_807
    )


def _validate_incident(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping) or set(item) != INCIDENT_KEYS:
        raise AiOperationsError("ai_operations_contract_invalid")
    if item["incident_id"] is None or not _uuid_or_none(item["incident_id"]):
        raise AiOperationsError("ai_operations_contract_invalid")
    monitor_name = item["monitor_name"]
    if not isinstance(monitor_name, str) or not 3 <= len(monitor_name) <= 64:
        raise AiOperationsError("ai_operations_contract_invalid")
    if item["state"] not in VALID_STATES:
        raise AiOperationsError("ai_operations_contract_invalid")
    if item["severity"] not in VALID_SEVERITIES:
        raise AiOperationsError("ai_operations_contract_invalid")
    if not isinstance(item["is_drill"], bool):
        raise AiOperationsError("ai_operations_contract_invalid")
    if item["observation_status"] not in VALID_OBSERVATION_STATUSES:
        raise AiOperationsError("ai_operations_contract_invalid")
    for key in (
        "first_seen_at",
        "last_seen_at",
        "acknowledged_at",
        "resolved_at",
    ):
        if not _timestamp_or_none(item[key]):
            raise AiOperationsError("ai_operations_contract_invalid")
    for key in ("acknowledged_by", "resolved_by"):
        if not _uuid_or_none(item[key]):
            raise AiOperationsError("ai_operations_contract_invalid")
    for key in (
        "observation_count",
        "window_hours",
        "request_count",
        "completed_count",
        "fail_closed_count",
        "relevance_fail_closed_count",
        "dependency_failure_count",
    ):
        if not _bounded_integer(item[key]):
            raise AiOperationsError("ai_operations_contract_invalid")
    reasons = item["reason_codes"]
    if (
        not isinstance(reasons, list)
        or len(reasons) > 16
        or any(
            not isinstance(reason, str) or len(reason) > 128
            for reason in reasons
        )
    ):
        raise AiOperationsError("ai_operations_contract_invalid")
    rate = item["fail_closed_rate"]
    if (
        isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not 0 <= float(rate) <= 1
    ):
        raise AiOperationsError("ai_operations_contract_invalid")
    return dict(item)


def _translate_repository_error(
    exc: AiOperationsRepositoryError,
) -> AiOperationsError:
    if exc.sqlstate == "P0002":
        return AiOperationsError("monitor_incident_not_found", 404)
    if exc.sqlstate == "22023":
        return AiOperationsError("invalid_incident_transition", 409)
    return AiOperationsError("ai_operations_unavailable")


async def list_admin_ai_operations_incidents_v1(
    *,
    repository: AiOperationsRepository,
    actor_user_id: str | UUID,
    state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    actor = _uuid(actor_user_id, "actor_user_id")
    normalized_state = str(state).strip() if state is not None else None
    if normalized_state not in VALID_STATES | {None}:
        raise ValueError("invalid incident state")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("invalid incident limit")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError("invalid incident limit")

    try:
        raw = await repository.list_incidents(
            actor_user_id=actor,
            capability=READ_CAPABILITY,
            state=normalized_state,
            limit=limit,
        )
    except AiOperationsRepositoryError as exc:
        raise _translate_repository_error(exc) from exc
    except Exception as exc:
        raise AiOperationsError("ai_operations_unavailable") from exc

    payload = _decode_jsonb(raw)
    if payload.get("contract_version") != LIST_SOURCE_CONTRACT:
        raise AiOperationsError("ai_operations_contract_invalid")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > limit:
        raise AiOperationsError("ai_operations_contract_invalid")
    return {
        "ok": True,
        "schema": LIST_SCHEMA,
        "source_contract_version": LIST_SOURCE_CONTRACT,
        "items": [_validate_incident(item) for item in items],
    }


async def _mutate_incident(
    *,
    repository: AiOperationsRepository,
    actor_user_id: str | UUID,
    incident_id: str | UUID,
    action: str,
) -> dict[str, Any]:
    actor = _uuid(actor_user_id, "actor_user_id")
    incident = _uuid(incident_id, "incident_id")
    if action not in {"acknowledged", "resolved"}:
        raise ValueError("invalid incident action")

    operation = (
        repository.acknowledge_incident
        if action == "acknowledged"
        else repository.resolve_incident
    )
    try:
        raw = await operation(
            actor_user_id=actor,
            capability=MANAGE_CAPABILITY,
            incident_id=incident,
        )
    except AiOperationsRepositoryError as exc:
        raise _translate_repository_error(exc) from exc
    except Exception as exc:
        raise AiOperationsError("ai_operations_unavailable") from exc

    payload = _decode_jsonb(raw)
    if (
        payload.get("contract_version") != MUTATION_SOURCE_CONTRACT
        or payload.get("action") != action
        or str(payload.get("incident_id") or "") != incident
        or payload.get("state") != action
        or set(payload)
        != {"contract_version", "action", "incident_id", "state"}
    ):
        raise AiOperationsError("ai_operations_contract_invalid")
    return {
        "ok": True,
        "schema": MUTATION_SCHEMA,
        "source_contract_version": MUTATION_SOURCE_CONTRACT,
        "action": action,
        "incident_id": incident,
        "state": action,
    }


async def acknowledge_admin_ai_operations_incident_v1(
    *,
    repository: AiOperationsRepository,
    actor_user_id: str | UUID,
    incident_id: str | UUID,
) -> dict[str, Any]:
    return await _mutate_incident(
        repository=repository,
        actor_user_id=actor_user_id,
        incident_id=incident_id,
        action="acknowledged",
    )


async def resolve_admin_ai_operations_incident_v1(
    *,
    repository: AiOperationsRepository,
    actor_user_id: str | UUID,
    incident_id: str | UUID,
) -> dict[str, Any]:
    return await _mutate_incident(
        repository=repository,
        actor_user_id=actor_user_id,
        incident_id=incident_id,
        action="resolved",
    )
