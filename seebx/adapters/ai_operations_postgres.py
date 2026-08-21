from __future__ import annotations

"""PostgreSQL effect boundary for the AI Operations incident inbox."""

from typing import Any

from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.capabilities.operations.ai_operations import (
    AiOperationsRepositoryError,
)


SET_ACTOR_SQL = "SELECT set_config('app.user_id',$1,true)"
SET_CAPABILITY_SQL = (
    "SELECT set_config('app.ai_operations_capability',$1,true)"
)
LIST_INCIDENTS_SQL = (
    "SELECT ai_operations.list_monitor_incidents_v1($1,$2)"
)
ACKNOWLEDGE_INCIDENT_SQL = (
    "SELECT ai_operations.acknowledge_monitor_incident_v1($1,$2)"
)
RESOLVE_INCIDENT_SQL = (
    "SELECT ai_operations.resolve_monitor_incident_v1($1,$2)"
)


class PostgresAiOperationsRepository:
    """Own all six AI Operations query and transaction effects."""

    def __init__(self, provider: PostgresConnectionProvider) -> None:
        self._provider = provider

    @staticmethod
    async def _set_authority(
        connection: Any,
        *,
        actor_user_id: str,
        capability: str,
    ) -> None:
        await connection.execute(SET_ACTOR_SQL, actor_user_id)
        await connection.execute(SET_CAPABILITY_SQL, capability)

    @staticmethod
    def _repository_error(exc: Exception) -> AiOperationsRepositoryError:
        return AiOperationsRepositoryError(
            str(getattr(exc, "sqlstate", "") or "")
        )

    async def list_incidents(
        self,
        *,
        actor_user_id: str,
        capability: str,
        state: str | None,
        limit: int,
    ) -> Any:
        try:
            async with self._provider.connection() as connection:
                async with connection.transaction():
                    await self._set_authority(
                        connection,
                        actor_user_id=actor_user_id,
                        capability=capability,
                    )
                    return await connection.fetchval(
                        LIST_INCIDENTS_SQL,
                        state,
                        limit,
                    )
        except Exception as exc:
            raise self._repository_error(exc) from exc

    async def _transition_incident(
        self,
        *,
        sql: str,
        actor_user_id: str,
        capability: str,
        incident_id: str,
    ) -> Any:
        try:
            async with self._provider.connection() as connection:
                async with connection.transaction():
                    await self._set_authority(
                        connection,
                        actor_user_id=actor_user_id,
                        capability=capability,
                    )
                    return await connection.fetchval(
                        sql,
                        incident_id,
                        actor_user_id,
                    )
        except Exception as exc:
            raise self._repository_error(exc) from exc

    async def acknowledge_incident(
        self,
        *,
        actor_user_id: str,
        capability: str,
        incident_id: str,
    ) -> Any:
        return await self._transition_incident(
            sql=ACKNOWLEDGE_INCIDENT_SQL,
            actor_user_id=actor_user_id,
            capability=capability,
            incident_id=incident_id,
        )

    async def resolve_incident(
        self,
        *,
        actor_user_id: str,
        capability: str,
        incident_id: str,
    ) -> Any:
        return await self._transition_incident(
            sql=RESOLVE_INCIDENT_SQL,
            actor_user_id=actor_user_id,
            capability=capability,
            incident_id=incident_id,
        )


__all__ = [
    "ACKNOWLEDGE_INCIDENT_SQL",
    "LIST_INCIDENTS_SQL",
    "PostgresAiOperationsRepository",
    "RESOLVE_INCIDENT_SQL",
    "SET_ACTOR_SQL",
    "SET_CAPABILITY_SQL",
]
