from __future__ import annotations

"""Concrete, read-only Postgres inputs for the governed Memory lane adapters.

The loaders return source rows plus proof of the effective read controls.  They
do not repair missing source fields or weaken the frozen envelope: the lane
adapters continue to report an exact schema gap until the corresponding
database read function is extended through separately authorized migrations.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID


class GovernedPostgresLoaderError(RuntimeError):
    pass


MAX_CLAIM_IDS = 100
MAX_PROJECT_ROWS = 8
MAX_PREFERENCE_ROWS = 200


def _uuid(value: Any, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise GovernedPostgresLoaderError(f"{field} must be a UUID") from None
    if str(parsed) != str(value):
        raise GovernedPostgresLoaderError(f"{field} must be a canonical UUID")
    return parsed


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise GovernedPostgresLoaderError(f"{field} must be a JSON object")


async def _establish_read_controls(conn: Any, actor: UUID) -> dict[str, bool]:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(actor))
    role = str(await conn.fetchval("SELECT current_user"))
    read_only = str(
        await conn.fetchval("SELECT current_setting('transaction_read_only')")
    )
    if role != "brains_app" or read_only != "on":
        raise GovernedPostgresLoaderError(
            "governed loader requires brains_app in a read-only transaction"
        )
    return {
        "effective_role_brains_app": True,
        "transaction_read_only": True,
    }


async def _verify_restricted_read_function(conn: Any, identity: str) -> None:
    row = await conn.fetchrow(
        """
        SELECT p.prosecdef,p.provolatile,
               pg_get_userbyid(p.proowner) AS owner_name,
               COALESCE(array_to_string(p.proconfig,','),'') AS settings
        FROM pg_proc AS p
        WHERE p.oid=$1::regprocedure
        """,
        identity,
    )
    value = dict(row) if row is not None else {}
    volatility = value.get("provolatile")
    if isinstance(volatility, bytes):
        try:
            volatility = volatility.decode("ascii")
        except UnicodeDecodeError:
            volatility = None
    if (
        value.get("prosecdef") is not True
        or volatility != "s"
        or value.get("owner_name") != "memory_v5_reader"
        or "search_path=" not in str(value.get("settings") or "")
    ):
        raise GovernedPostgresLoaderError(
            "governed read function contract changed"
        )


async def load_governed_v5_claim_rows_v1(
    conn: Any,
    owner_user_id: UUID,
    claim_ids: Sequence[UUID],
) -> Mapping[str, Any]:
    actor = _uuid(owner_user_id, "owner_user_id")
    requested = tuple(_uuid(item, "claim_id") for item in claim_ids)
    if not 1 <= len(requested) <= MAX_CLAIM_IDS or len(set(requested)) != len(requested):
        raise GovernedPostgresLoaderError(
            "claim ids must contain 1 to 100 unique UUIDs"
        )
    requested = tuple(sorted(requested, key=str))
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        controls = await _establish_read_controls(conn, actor)
        await _verify_restricted_read_function(
            conn,
            "memory.read_governed_claims_v1(uuid[])",
        )
        controls["restricted_read_contract"] = True
        rows = list(
            await conn.fetch(
                "SELECT * FROM memory.read_governed_claims_v1($1::uuid[])",
                list(requested),
            )
        )
    records: list[dict[str, Any]] = []
    seen: set[UUID] = set()
    for row in rows:
        value = dict(row)
        if _uuid(value.get("owner_user_id"), "claim.owner_user_id") != actor:
            raise GovernedPostgresLoaderError("claim read returned a cross-owner row")
        claim_id = _uuid(value.get("claim_id"), "claim.claim_id")
        if claim_id not in requested or claim_id in seen:
            raise GovernedPostgresLoaderError(
                "claim read returned an unexpected or duplicate row"
            )
        seen.add(claim_id)
        for field in ("metadata", "retrieval_policy", "evidence_by_stance"):
            value[field] = _json_object(value.get(field), f"claim.{field}")
        records.append(value)
    records.sort(key=lambda item: str(item["claim_id"]))
    return {
        "owner_user_id": actor,
        "claim_ids": requested,
        "records": records,
        "controls": controls,
        "database_writes": 0,
    }


class GovernedV5ProjectRowLoaderV1:
    """Bind the existing project reader to an already trusted project scope."""

    def __init__(
        self,
        conn: Any,
        *,
        project_key: str,
        component_key: str,
    ) -> None:
        if not isinstance(project_key, str) or not project_key.strip():
            raise GovernedPostgresLoaderError("project key is required")
        if not isinstance(component_key, str) or not component_key.strip():
            raise GovernedPostgresLoaderError("component key is required")
        self._conn = conn
        self._project_key = project_key.strip()
        self._component_key = component_key.strip()

    async def __call__(
        self,
        owner_user_id: UUID,
        thread_id: UUID,
        *,
        limit: int,
    ) -> Mapping[str, Any]:
        actor = _uuid(owner_user_id, "owner_user_id")
        thread = _uuid(thread_id, "thread_id")
        if type(limit) is not int or not 1 <= limit <= MAX_PROJECT_ROWS:
            raise GovernedPostgresLoaderError("project limit must be between 1 and 8")
        async with self._conn.transaction(
            isolation="repeatable_read", readonly=True
        ):
            controls = await _establish_read_controls(self._conn, actor)
            await _verify_restricted_read_function(
                self._conn,
                "memory.read_v5_shadow_project_knowledge(uuid,integer)",
            )
            controls["restricted_read_contract"] = True
            owns_thread = bool(
                await self._conn.fetchval(
                    """
                    SELECT EXISTS(
                      SELECT 1 FROM public.threads
                      WHERE owner_user_id=$1 AND id=$2
                    )
                    """,
                    actor,
                    thread,
                )
            )
            if not owns_thread:
                raise GovernedPostgresLoaderError("owner thread is absent")
            rows = list(
                await self._conn.fetch(
                    "SELECT * FROM memory.read_v5_shadow_project_knowledge($1,$2)",
                    thread,
                    limit,
                )
            )
        records: list[dict[str, Any]] = []
        seen: set[UUID] = set()
        for row in rows:
            value = dict(row)
            if _uuid(value.get("owner_user_id"), "project.owner_user_id") != actor:
                raise GovernedPostgresLoaderError(
                    "project read returned a cross-owner row"
                )
            if _uuid(value.get("thread_id"), "project.thread_id") != thread:
                raise GovernedPostgresLoaderError(
                    "project read returned a cross-thread row"
                )
            if value.get("project_key") != self._project_key or value.get(
                "component_key"
            ) != self._component_key:
                raise GovernedPostgresLoaderError(
                    "project read returned a different trusted scope"
                )
            knowledge_id = _uuid(value.get("knowledge_id"), "project.knowledge_id")
            if knowledge_id in seen:
                raise GovernedPostgresLoaderError(
                    "project read returned a duplicate knowledge head"
                )
            seen.add(knowledge_id)
            records.append(value)
        return {
            "owner_user_id": actor,
            "thread_id": thread,
            "project_key": self._project_key,
            "component_key": self._component_key,
            "records": records,
            "controls": controls,
            "database_writes": 0,
        }


async def load_governed_preference_snapshot_v1(
    conn: Any,
    owner_user_id: UUID,
) -> Mapping[str, Any]:
    actor = _uuid(owner_user_id, "owner_user_id")
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        controls = await _establish_read_controls(conn, actor)
        rls_rows = list(
            await conn.fetch(
                """
                SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='memory'
                  AND c.relname=ANY($1::text[])
                """,
                [
                    "evidence",
                    "preference_revision",
                    "preference_revision_evidence",
                    "user_preference",
                ],
            )
        )
        rls = {
            str(dict(row).get("relname")): bool(dict(row).get("relrowsecurity"))
            and bool(dict(row).get("relforcerowsecurity"))
            for row in rls_rows
        }
        if set(rls) != {
            "evidence",
            "preference_revision",
            "preference_revision_evidence",
            "user_preference",
        } or not all(rls.values()):
            raise GovernedPostgresLoaderError("preference forced-RLS controls changed")
        controls["forced_rls"] = True
        rows = list(
            await conn.fetch(
                """
                SELECT h.preference_id,h.status::text,h.preference_class,
                       h.preference_domain,h.preference_key,h.value,h.polarity,
                       h.scope,h.stability,h.surface_policy,h.sensitivity::text,
                       h.current_revision_id,
                       h.revision_number AS head_revision_number,
                       h.content_sha256 AS head_content_sha256,
                       h.accepted_review_id AS head_accepted_review_id,
                       r.revision_id,r.revision_number,r.content_sha256,
                       r.accepted_review_id,
                       ARRAY(
                         SELECT link.evidence_id
                         FROM memory.preference_revision_evidence AS link
                         WHERE link.owner_user_id=h.owner_user_id
                           AND link.revision_id=r.revision_id
                         ORDER BY link.evidence_id
                       ) AS evidence_ids,
                       (SELECT count(*)
                        FROM memory.preference_revision_evidence AS link
                        WHERE link.owner_user_id=h.owner_user_id
                          AND link.revision_id=r.revision_id) AS evidence_link_count,
                       (SELECT count(*)
                        FROM memory.preference_revision_evidence AS link
                        JOIN memory.evidence AS evidence
                          ON evidence.owner_user_id=link.owner_user_id
                         AND evidence.evidence_id=link.evidence_id
                        WHERE link.owner_user_id=h.owner_user_id
                          AND link.revision_id=r.revision_id
                          AND evidence.status='active') AS active_evidence_count
                FROM memory.user_preference AS h
                JOIN memory.preference_revision AS r
                  ON r.owner_user_id=h.owner_user_id
                 AND r.preference_id=h.preference_id
                 AND r.revision_id=h.current_revision_id
                WHERE h.owner_user_id=$1
                ORDER BY h.preference_key,h.preference_id
                LIMIT $2
                """,
                actor,
                MAX_PREFERENCE_ROWS + 1,
            )
        )
    if len(rows) > MAX_PREFERENCE_ROWS:
        raise GovernedPostgresLoaderError("preference scan limit exceeded")
    records = [dict(row) for row in rows]
    seen: set[UUID] = set()
    for row in records:
        preference_id = _uuid(row.get("preference_id"), "preference_id")
        if preference_id in seen:
            raise GovernedPostgresLoaderError(
                "preference read returned a duplicate head"
            )
        seen.add(preference_id)
    return {
        "owner_user_id": actor,
        "preferences": records,
        "controls": controls,
        "database_writes": 0,
    }


__all__ = [
    "GovernedPostgresLoaderError",
    "GovernedV5ProjectRowLoaderV1",
    "load_governed_preference_snapshot_v1",
    "load_governed_v5_claim_rows_v1",
]
