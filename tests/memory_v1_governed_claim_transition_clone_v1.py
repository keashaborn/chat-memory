#!/usr/bin/env python3
from __future__ import annotations

"""Disposable PostgreSQL proof for the two-stage governed-claim transition.

The caller supplies a schema-only, synthetic-only PostgreSQL 16 clone with
three staged create-only projection plans.  Stage A consumes a pre-existing
projection review.  A separate transaction then persists an owner-user claim
assessment review using the existing V5 review functions.  Only after that
transaction commits does Stage B consume the review and create one held,
non-dispatchable derived-index eligibility row.

The script permits Unix-domain PostgreSQL sockets only.  It has no Qdrant,
provider, model, production-data, or Internet client.
"""

import asyncio
import dataclasses
import hashlib
import inspect
import json
import os
import re
import socket
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncpg

from rag_engine.memory_v1_governed_claim_transition_v1 import (
    GovernedClaimMaterializationReceiptV1,
    GovernedClaimSupportReceiptV1,
    GovernedClaimTransitionError,
    ReviewedProjectionMaterializationV1,
    ReviewedSupportPromotionV1,
    VerifiedActorContextV1,
    materialize_reviewed_claim_v1,
    promote_independently_reviewed_claim_v1,
)


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_SYSTEM_IDENTIFIER = re.compile(r"^[0-9]{10,30}$")
_TARGET_VIEWS = (
    "memory.read_governed_claim_projection_apply_ready_v1",
    "memory.read_governed_claim_materialization_authority_v1",
    "memory.read_governed_claim_assessment_apply_ready_v1",
    "memory.read_governed_claim_supported_held_v1",
)
_HELPER_SIGNATURES = (
    "memory.v5_sha256_valid(text)",
    "memory.v5_digest_text(text)",
    "memory.v5_canonical_json_text(jsonb)",
    "memory.v5_projection_owner_manifest_sha256(uuid,text)",
    "memory.v5_projection_semantic_key_sha256(uuid,memory.projection_lane_v5,uuid,text,text,uuid,text,memory.observation_polarity,memory.observation_modality,jsonb)",
    "memory.v5_projection_review_manifest_sha256(uuid,uuid,text,text,text,integer,memory.projection_review_decision_v5,text,text,text,jsonb)",
    "memory.v5_projection_apply_manifest_sha256(uuid,text,uuid,text,text,text,memory.projection_lane_v5,memory.projection_target_action_v5,integer,uuid,text)",
)
_AFFECTED_TABLES = (
    "claim",
    "claim_revision",
    "claim_observation",
    "claim_relation_v5",
    "projection_apply_event",
    "projection_dispatch_v5",
    "claim_assessment_review_v5",
    "relational_operation_request",
    "claim_assessment",
    "claim_assessment_apply_v5",
    "projection_outbox",
    "projection_plan",
    "projection_plan_item",
    "projection_review",
    "projection_plan_observation",
    "projection_plan_relation",
    "observation",
    "evidence",
)
_REASON_CODES = (
    "accepted_predicate_entailment",
    "synthetic_clone_fixture",
)
_RATIONALE = "Synthetic owner review accepted exact clone evidence."
_CLONE_SOCKET_PATH_PATTERN = (
    r"/home/ubuntu/phase6-postcommit-v1\.[0-9a-f]{24}/child-work/socket"
)
_CLONE_SOCKET_PATH = re.compile(_CLONE_SOCKET_PATH_PATTERN)


def _deny_internet_sockets(event: str, args: tuple[object, ...]) -> None:
    if event == "socket.getaddrinfo":
        raise RuntimeError("network name resolution is forbidden in clone proof")
    if event == "socket.__new__" and len(args) >= 2:
        if args[1] in {socket.AF_INET, socket.AF_INET6}:
            raise RuntimeError("Internet sockets are forbidden in clone proof")


sys.addaudithook(_deny_internet_sockets)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def required_uuid(name: str) -> UUID:
    return UUID(os.environ[name])


def required_sha(name: str) -> str:
    value = os.environ[name]
    if not _SHA.fullmatch(value):
        raise RuntimeError(f"{name} is not a lowercase SHA-256")
    return value


def operation_id(plan_id: UUID, label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"memory-transition-clone|{plan_id}|{label}")


def _clone_socket_path_is_exact(socket_path: str) -> bool:
    return _CLONE_SOCKET_PATH.fullmatch(socket_path) is not None


def _clone_socket_components_are_safe(socket_dir: Path) -> bool:
    child_work = socket_dir.parent
    run_root = child_work.parent
    try:
        return (
            socket_dir.is_absolute()
            and socket_dir.is_dir()
            and child_work.is_dir()
            and run_root.is_dir()
            and not socket_dir.is_symlink()
            and not child_work.is_symlink()
            and not run_root.is_symlink()
            and socket_dir.resolve(strict=True) == socket_dir
            and child_work.resolve(strict=True) == child_work
            and run_root.resolve(strict=True) == run_root
        )
    except (OSError, RuntimeError):
        return False


def _clone_socket_directory_is_safe(socket_path: str) -> bool:
    if not _clone_socket_path_is_exact(socket_path):
        return False
    return _clone_socket_components_are_safe(Path(socket_path))


def _isolated_dsn(name: str, expected_username: str) -> str:
    if os.environ.get("MEMORY_V1_TRANSITION_CLONE") != "authorized":
        raise RuntimeError("disposable clone enable token is absent")
    dsn = os.environ[name]
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("clone DSN scheme is invalid")
    socket_path = os.environ["MEMORY_V1_TRANSITION_CLONE_SOCKET"]
    query = parse_qs(parsed.query, strict_parsing=True)
    if (
        not _clone_socket_directory_is_safe(socket_path)
        or query != {"host": [socket_path]}
        or parsed.hostname is not None
    ):
        raise RuntimeError("clone DSN must use the exact disposable Unix socket")
    if (
        parsed.username != expected_username
        or not parsed.password
        or parsed.port is not None
        or parsed.params
        or parsed.fragment
    ):
        raise RuntimeError("clone DSN authority or transport is not exact")
    database = parsed.path.removeprefix("/")
    if (
        database != os.environ["MEMORY_V1_TRANSITION_CLONE_DATABASE"]
        or not database.startswith("memory_transition_clone_")
        or not re.fullmatch(r"[a-z0-9_]+", database)
    ):
        raise RuntimeError("clone database name is not disposable")
    return dsn


def clone_dsn() -> str:
    return _isolated_dsn("MEMORY_V1_TRANSITION_CLONE_DSN", "brains_app")


def audit_dsn() -> str:
    return _isolated_dsn("MEMORY_V1_TRANSITION_CLONE_AUDIT_DSN", "sage")


class SyntheticVerifiedActorBinder:
    """Clone-only stand-in for the upstream authenticated owner boundary."""

    def __init__(self, owner_user_id: UUID, label: str) -> None:
        self.owner_user_id = owner_user_id
        self.authentication_manifest_sha256 = sha256_text(
            f"synthetic-verified-actor|{owner_user_id}|{label}"
        )
        self.transaction_readonly_observations: list[str] = []

    async def bind_verified_actor(
        self, conn: asyncpg.Connection
    ) -> VerifiedActorContextV1:
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)",
            str(self.owner_user_id),
        )
        readonly = await conn.fetchval(
            "SELECT current_setting('transaction_read_only')"
        )
        self.transaction_readonly_observations.append(str(readonly))
        return VerifiedActorContextV1(
            owner_user_id=self.owner_user_id,
            authentication_manifest_sha256=self.authentication_manifest_sha256,
        )


class ReplayWriteProbeConnection:
    """Attempts one no-op write from the runtime's replay read path."""

    def __init__(self, conn: asyncpg.Connection, marker: str) -> None:
        self.conn = conn
        self.marker = marker
        self.probed = False

    def is_in_transaction(self) -> bool:
        return self.conn.is_in_transaction()

    def transaction(self, **kwargs: object):
        return self.conn.transaction(**kwargs)

    async def execute(self, query: str, *args: object):
        return await self.conn.execute(query, *args)

    async def fetchval(self, query: str, *args: object):
        return await self.conn.fetchval(query, *args)

    async def fetchrow(self, query: str, *args: object):
        return await self.conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args: object):
        if self.marker in query and not self.probed:
            self.probed = True
            await self.conn.execute(
                "UPDATE memory.projection_outbox SET attempts=attempts WHERE false"
            )
        return await self.conn.fetch(query, *args)


class _RaiseAfterCommitTransaction:
    def __init__(self, owner: "AmbiguousCommitConnection", transaction: Any) -> None:
        self.owner = owner
        self.transaction = transaction

    async def __aenter__(self) -> "_RaiseAfterCommitTransaction":
        await self.transaction.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        if exc_type is not None:
            await self.transaction.rollback()
            return False
        await self.transaction.commit()
        self.owner.commit_completed = True
        raise RuntimeError("synthetic acknowledgement loss after COMMIT")


class AmbiguousCommitConnection:
    """Commits once, then makes the client observe an ambiguous failure."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self.commit_completed = False
        self.transaction_created = False

    def is_in_transaction(self) -> bool:
        return self.conn.is_in_transaction()

    def transaction(self, **kwargs: object):
        if self.transaction_created:
            raise RuntimeError("ambiguous-commit wrapper is single use")
        self.transaction_created = True
        return _RaiseAfterCommitTransaction(
            self,
            self.conn.transaction(**kwargs),
        )

    async def execute(self, query: str, *args: object):
        return await self.conn.execute(query, *args)

    async def fetchval(self, query: str, *args: object):
        return await self.conn.fetchval(query, *args)

    async def fetchrow(self, query: str, *args: object):
        return await self.conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args: object):
        return await self.conn.fetch(query, *args)


async def verify_clone_boundary(conn: asyncpg.Connection) -> None:
    expected = {
        "run_nonce_sha256": required_sha(
            "MEMORY_V1_TRANSITION_CLONE_NONCE_SHA256"
        ),
        "lease_context_sha256": required_sha(
            "MEMORY_V1_TRANSITION_CLONE_LEASE_CONTEXT_SHA256"
        ),
        "controller_script_sha256": required_sha(
            "MEMORY_V1_TRANSITION_CLONE_CONTROLLER_SCRIPT_SHA256"
        ),
        "controller_manifest_sha256": required_sha(
            "MEMORY_V1_TRANSITION_CLONE_CONTROLLER_MANIFEST_SHA256"
        ),
        "governance_validation_sha256": required_sha(
            "MEMORY_V1_TRANSITION_CLONE_GOVERNANCE_SHA256"
        ),
        "schema_only_baseline_sha256": required_sha(
            "MEMORY_V1_TRANSITION_CLONE_SCHEMA_BASELINE_SHA256"
        ),
        "synthetic_inventory_sha256": required_sha(
            "MEMORY_V1_TRANSITION_CLONE_SYNTHETIC_INVENTORY_SHA256"
        ),
        "transition_fixture_inventory_sha256": required_sha(
            "MEMORY_V1_TRANSITION_CLONE_FIXTURE_INVENTORY_SHA256"
        ),
        "server_system_identifier": os.environ[
            "MEMORY_V1_TRANSITION_CLONE_SYSTEM_IDENTIFIER"
        ],
    }
    production_system = os.environ[
        "MEMORY_V1_TRANSITION_PRODUCTION_SYSTEM_IDENTIFIER"
    ]
    expected_version = os.environ[
        "MEMORY_V1_TRANSITION_CLONE_SERVER_VERSION_NUM"
    ]
    if (
        not _SYSTEM_IDENTIFIER.fullmatch(expected["server_system_identifier"])
        or not _SYSTEM_IDENTIFIER.fullmatch(production_system)
        or production_system == expected["server_system_identifier"]
        or not re.fullmatch(r"16[0-9]{4}", expected_version)
    ):
        raise RuntimeError("clone system/version identity is invalid")
    rows = await conn.fetch(
        """SELECT sentinel.*,
                  control.system_identifier::text AS actual_system_identifier,
                  current_database()::text AS actual_database_name,
                  current_setting('server_version_num')::text
                    AS actual_server_version
           FROM public.memory_v1_transition_clone_sentinel_v1 AS sentinel
           CROSS JOIN pg_catalog.pg_control_system() AS control"""
    )
    if len(rows) != 1:
        raise RuntimeError("clone boundary sentinel cardinality is invalid")
    row = rows[0]
    for key, value in expected.items():
        if str(row[key]) != value:
            raise RuntimeError(f"clone boundary sentinel {key} mismatch")
    if (
        str(row["actual_system_identifier"])
        != expected["server_system_identifier"]
        or str(row["database_name"]) != str(row["actual_database_name"])
        or not str(row["actual_database_name"]).startswith(
            "memory_transition_clone_"
        )
        or str(row["actual_server_version"]) != expected_version
        or row["isolated_container"] is not True
    ):
        raise RuntimeError("clone boundary identity mismatch")


async def verify_two_session_boundary(
    runtime: asyncpg.Connection, audit: asyncpg.Connection
) -> None:
    observed: list[Mapping[str, Any]] = []
    for conn, expected_user in ((runtime, "brains_app"), (audit, "sage")):
        row = await conn.fetchrow(
            """SELECT session_user::text,current_user::text,
                      current_database()::text,
                      pg_catalog.pg_backend_pid() AS backend_pid,
                      inet_server_addr() IS NULL AS unix_server,
                      inet_client_addr() IS NULL AS unix_client,
                      current_setting('server_version_num')::text AS server_version,
                      control.system_identifier::text AS system_identifier,
                      role.rolsuper,role.rolbypassrls
               FROM pg_catalog.pg_control_system() AS control
               CROSS JOIN pg_catalog.pg_roles AS role
               WHERE role.rolname=current_user"""
        )
        if (
            row is None
            or str(row["session_user"]) != expected_user
            or str(row["current_user"]) != expected_user
            or row["unix_server"] is not True
            or row["unix_client"] is not True
            or row["rolsuper"] is not (expected_user == "sage")
            or row["rolbypassrls"] is not (expected_user == "sage")
        ):
            raise RuntimeError("clone session authority or transport is invalid")
        observed.append(row)
    if (
        observed[0]["backend_pid"] == observed[1]["backend_pid"]
        or observed[0]["current_database"] != observed[1]["current_database"]
        or observed[0]["server_version"] != observed[1]["server_version"]
        or observed[0]["system_identifier"] != observed[1]["system_identifier"]
    ):
        raise RuntimeError("runtime and audit clone sessions are not co-isolated")


async def verify_view_contracts(conn: asyncpg.Connection) -> None:
    banned = {
        "payload",
        "rationale",
        "reason_code",
        "claim_text",
        "observation_text",
        "evidence_text",
        "content",
        "source_span",
    }
    for view in _TARGET_VIEWS:
        metadata = await conn.fetchrow(
            """SELECT relation.relkind,
                      pg_catalog.pg_get_userbyid(relation.relowner) AS owner,
                      relation.reloptions,
                      pg_catalog.has_table_privilege(
                        'brains_app',relation.oid,'SELECT'
                      ) AS brains_select,
                      EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                          COALESCE(
                            relation.relacl,
                            pg_catalog.acldefault('r',relation.relowner)
                          )
                        ) AS acl
                        WHERE acl.grantee=0 AND acl.privilege_type='SELECT'
                      ) AS public_select
               FROM pg_catalog.pg_class AS relation
               WHERE relation.oid=pg_catalog.to_regclass($1)""",
            view,
        )
        columns = tuple(
            str(row["attname"])
            for row in await conn.fetch(
                """SELECT attribute.attname
                   FROM pg_catalog.pg_attribute AS attribute
                   WHERE attribute.attrelid=pg_catalog.to_regclass($1)
                     AND attribute.attnum>0 AND NOT attribute.attisdropped
                   ORDER BY attribute.attnum""",
                view,
            )
        )
        if (
            metadata is None
            or str(metadata["relkind"]) != "v"
            or str(metadata["owner"]) != "memory_v5_writer"
            or set(metadata["reloptions"] or ())
            != {"security_barrier=true", "security_invoker=false"}
            or metadata["brains_select"] is not True
            or metadata["public_select"] is not False
            or not columns
            or any(any(token in column for token in banned) for column in columns)
        ):
            raise RuntimeError(f"authority view contract is not exact: {view}")
        acl = await conn.fetch(
            """SELECT CASE WHEN exploded.grantee=0 THEN 'PUBLIC'
                            ELSE pg_catalog.pg_get_userbyid(exploded.grantee) END
                        AS grantee,
                      exploded.privilege_type,exploded.is_grantable
               FROM pg_catalog.pg_class AS relation
               CROSS JOIN LATERAL pg_catalog.aclexplode(
                 COALESCE(
                   relation.relacl,
                   pg_catalog.acldefault('r',relation.relowner)
                 )
               ) AS exploded
               WHERE relation.oid=pg_catalog.to_regclass($1)
                 AND exploded.grantee<>relation.relowner
               ORDER BY grantee,exploded.privilege_type,exploded.is_grantable""",
            view,
        )
        if [tuple(row) for row in acl] != [("brains_app", "SELECT", False)]:
            raise RuntimeError(f"authority view ACL is not exact: {view}")

    for signature in _HELPER_SIGNATURES:
        rows = await conn.fetch(
            """SELECT acl.privilege_type,acl.is_grantable,
                      pg_catalog.pg_get_userbyid(acl.grantee) AS grantee
               FROM pg_catalog.pg_proc AS procedure
               CROSS JOIN LATERAL pg_catalog.aclexplode(
                 COALESCE(procedure.proacl,'{}'::aclitem[])
               ) AS acl
               WHERE procedure.oid=pg_catalog.to_regprocedure($1)
                 AND acl.grantee=(
                   SELECT role.oid FROM pg_catalog.pg_roles AS role
                   WHERE role.rolname='brains_app'
                 )""",
            signature,
        )
        if [tuple(row) for row in rows] != [("EXECUTE", False, "brains_app")]:
            raise RuntimeError(f"helper EXECUTE ACL is not exact: {signature}")


async def view_count(
    conn: asyncpg.Connection,
    view: str,
    owner: UUID | None,
    *,
    claim_id: UUID | None = None,
) -> int:
    async with conn.transaction(isolation="serializable", readonly=True):
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)",
            "" if owner is None else str(owner),
        )
        query = f"SELECT count(*) FROM {view}"
        args: tuple[object, ...] = ()
        if claim_id is not None:
            query += " WHERE claim_id=$1"
            args = (claim_id,)
        count = await conn.fetchval(query, *args)
    if type(count) is not int:
        raise RuntimeError("authority view count is not an integer")
    return count


async def verify_owner_bound_views(conn: asyncpg.Connection) -> None:
    for view in _TARGET_VIEWS:
        if await view_count(conn, view, None) != 0:
            raise RuntimeError("unset actor can read transition authority")
        if await view_count(conn, view, OWNER_B) != 0:
            raise RuntimeError("other owner can read transition authority")


async def verify_audit_session_view_denied(conn: asyncpg.Connection) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,false)", str(OWNER_A))
    for view in _TARGET_VIEWS:
        count = await conn.fetchval(f"SELECT count(*) FROM {view}")
        if type(count) is not int or count != 0:
            raise RuntimeError("non-brains session bypassed authority view gate")


async def state_fingerprint(conn: asyncpg.Connection, owner: UUID) -> str:
    material: dict[str, dict[str, object]] = {}
    for table in _AFFECTED_TABLES:
        row = await conn.fetchrow(
            f"""SELECT count(*) AS row_count,
                       memory.v5_digest_text(COALESCE(
                         pg_catalog.string_agg(
                           memory.v5_canonical_json_text(
                             pg_catalog.to_jsonb(source)
                           ),E'\\n'
                           ORDER BY memory.v5_canonical_json_text(
                             pg_catalog.to_jsonb(source)
                           )
                         ),''
                       )) AS row_sha256
                FROM memory.{table} AS source
                WHERE owner_user_id=$1""",
            owner,
        )
        if row is None or type(row["row_count"]) is not int:
            raise RuntimeError("state fingerprint relation is invalid")
        material[table] = {
            "row_count": row["row_count"],
            "row_sha256": str(row["row_sha256"]),
        }
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


async def direct_dml_is_denied(conn: asyncpg.Connection) -> None:
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)", str(OWNER_A)
            )
            await conn.execute(
                "DELETE FROM memory.claim WHERE owner_user_id=$1 AND false",
                OWNER_A,
            )
    except asyncpg.InsufficientPrivilegeError:
        return
    raise RuntimeError("brains_app unexpectedly has direct claim DELETE authority")


async def authorize_projection_outside_transition_runtime(
    conn: asyncpg.Connection,
    plan_id: UUID,
) -> ReviewedProjectionMaterializationV1:
    reason_codes = json.dumps(list(_REASON_CODES), separators=(",", ":"))
    async with conn.transaction(isolation="serializable"):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(OWNER_A))
        preflight = await conn.fetchrow(
            """SELECT * FROM memory.preflight_projection_review_v5(
              $1,'p01','authorized'::memory.projection_review_decision_v5,
              'system','memory_v1_governed_claim_transition_clone_v1',
              'synthetic create-only transition authorization',$2::jsonb)""",
            plan_id,
            reason_codes,
        )
        if preflight is None:
            raise RuntimeError("projection review preflight returned no row")
        reviewed = await conn.fetchrow(
            """SELECT * FROM memory.review_projection_v5(
              $1,'p01','authorized'::memory.projection_review_decision_v5,
              'system','memory_v1_governed_claim_transition_clone_v1',
              'synthetic create-only transition authorization',$2::jsonb,$3)""",
            plan_id,
            reason_codes,
            preflight["authorization_manifest_sha256"],
        )
        if (
            reviewed is None
            or str(reviewed["outcome"]) != "applied"
            or reviewed["rows_written"] != 1
        ):
            raise RuntimeError("projection authorization did not apply once")
        apply_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_apply_v5($1,'p01',$2)",
            plan_id,
            reviewed["review_id"],
        )
        if apply_preflight is None:
            raise RuntimeError("projection apply preflight returned no row")
    return ReviewedProjectionMaterializationV1(
        plan_id=plan_id,
        projection_ref="p01",
        projection_review_id=reviewed["review_id"],
        projection_request_id=operation_id(plan_id, "projection-apply"),
        projection_apply_manifest_sha256=str(
            apply_preflight["apply_manifest_sha256"]
        ),
    )


async def persist_owner_review_outside_transition_runtime(
    conn: asyncpg.Connection,
    materialization: GovernedClaimMaterializationReceiptV1,
    *,
    reviewer_type: str,
    reviewer_ref: str,
    label: str,
) -> ReviewedSupportPromotionV1:
    """Persist the independent review in its own committed transaction."""

    reason_codes = json.dumps(list(_REASON_CODES), separators=(",", ":"))
    review_request_id = operation_id(materialization.claim_id, f"{label}-review")
    apply_request_id = operation_id(materialization.claim_id, f"{label}-apply")
    async with conn.transaction(isolation="serializable"):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(OWNER_A))
        preflight = await conn.fetchrow(
            """SELECT * FROM memory.preflight_claim_assessment_review_v5(
              $1,'promote_supported'::memory.claim_assessment_action_v5,
              $2::numeric,$3::numeric,$4::numeric,$5::numeric,
              $6::jsonb,$7,$8,$9)""",
            materialization.claim_id,
            Decimal("0.750"),
            Decimal("0.000"),
            Decimal("0.875"),
            Decimal("0.900"),
            reason_codes,
            _RATIONALE,
            reviewer_type,
            reviewer_ref,
        )
        if (
            preflight is None
            or str(preflight["from_status"]) != "candidate"
            or str(preflight["target_status"]) != "supported"
        ):
            raise RuntimeError("owner review preflight did not bind candidate")
        reviewed = await conn.fetchrow(
            """SELECT * FROM memory.review_claim_assessment_v5(
              $1,$2,'promote_supported'::memory.claim_assessment_action_v5,
              $3::numeric,$4::numeric,$5::numeric,$6::numeric,
              $7::jsonb,$8,$9,$10,$11)""",
            review_request_id,
            materialization.claim_id,
            Decimal("0.750"),
            Decimal("0.000"),
            Decimal("0.875"),
            Decimal("0.900"),
            reason_codes,
            _RATIONALE,
            reviewer_type,
            reviewer_ref,
            preflight["authorization_manifest_sha256"],
        )
        if (
            reviewed is None
            or str(reviewed["outcome"]) != "applied"
        ):
            raise RuntimeError("independent owner review did not apply")
        apply_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_claim_assessment_apply_v5($1,$2)",
            materialization.claim_id,
            reviewed["review_id"],
        )
        if apply_preflight is None:
            raise RuntimeError("assessment apply preflight returned no row")
    return ReviewedSupportPromotionV1(
        materialization_receipt_sha256=materialization.receipt_sha256,
        claim_id=materialization.claim_id,
        assessment_review_request_id=review_request_id,
        assessment_review_id=reviewed["review_id"],
        assessment_review_manifest_sha256=str(
            preflight["authorization_manifest_sha256"]
        ),
        assessment_apply_request_id=apply_request_id,
        assessment_apply_manifest_sha256=str(
            apply_preflight["apply_manifest_sha256"]
        ),
    )


async def assert_nested_transaction_denied(
    conn: asyncpg.Connection,
    operation: Any,
    *args: object,
    **kwargs: object,
) -> None:
    async with conn.transaction(isolation="serializable"):
        try:
            await operation(conn, *args, **kwargs)
        except GovernedClaimTransitionError as exc:
            if str(exc) != (
                "governed transition requires a top-level transaction boundary"
            ):
                raise
        else:
            raise RuntimeError("nested governed transition unexpectedly succeeded")


async def assert_stage_b_denied_without_writes(
    runtime: asyncpg.Connection,
    audit: asyncpg.Connection,
    materialization: GovernedClaimMaterializationReceiptV1,
    command: ReviewedSupportPromotionV1,
) -> None:
    before = await state_fingerprint(audit, OWNER_A)
    try:
        await promote_independently_reviewed_claim_v1(
            runtime,
            SyntheticVerifiedActorBinder(OWNER_A, "denial"),
            materialization,
            command,
        )
    except GovernedClaimTransitionError:
        pass
    else:
        raise RuntimeError("non-owner-user support review was consumed")
    if before != await state_fingerprint(audit, OWNER_A):
        raise RuntimeError("denied support review changed durable state")


async def worker_claimable_count(
    conn: asyncpg.Connection,
    owner: UUID,
    outbox_id: UUID,
) -> int:
    count = await conn.fetchval(
        """SELECT count(*)
           FROM memory.projection_outbox
           WHERE owner_user_id=$1 AND outbox_id=$2
             AND aggregate_type='claim' AND attempts<8
             AND (
               (status IN ('pending','error')
                AND available_at<=clock_timestamp())
               OR
               (status='processing'
                AND lease_expires_at<=clock_timestamp())
             )""",
        owner,
        outbox_id,
    )
    if type(count) is not int:
        raise RuntimeError("worker claimable count is invalid")
    return count


async def verify_held_outbox_exact(
    conn: asyncpg.Connection,
    receipt: GovernedClaimSupportReceiptV1,
) -> None:
    row = await conn.fetchrow(
        """SELECT status::text,attempts,available_at='infinity'::timestamptz
                    AS available_at_infinity,
                  last_error IS NULL AS error_null,
                  lease_token IS NULL AS token_null,
                  lease_expires_at IS NULL AS expiry_null,
                  worker_id IS NULL AS worker_null,
                  payload=jsonb_build_object(
                    'claim_id',$2::uuid,'revision_number',2
                  ) AS payload_exact
           FROM memory.projection_outbox
           WHERE owner_user_id=$1 AND outbox_id=$3
             AND aggregate_type='claim' AND aggregate_id=$2
             AND operation='upsert'""",
        OWNER_A,
        receipt.claim_id,
        receipt.outbox_id,
    )
    if row is None or (
        str(row["status"]) != "pending"
        or row["attempts"] != 0
        or row["available_at_infinity"] is not True
        or row["error_null"] is not True
        or row["token_null"] is not True
        or row["expiry_null"] is not True
        or row["worker_null"] is not True
        or row["payload_exact"] is not True
        or await worker_claimable_count(conn, OWNER_A, receipt.outbox_id) != 0
    ):
        raise RuntimeError("held outbox is not exact or is worker-claimable")


async def assert_tamper_hides_supported_authority(
    audit: asyncpg.Connection,
    runtime: asyncpg.Connection,
    receipt: GovernedClaimSupportReceiptV1,
    statement: str,
    *args: object,
) -> None:
    before = await state_fingerprint(audit, OWNER_A)
    transaction = audit.transaction(isolation="serializable")
    await transaction.start()
    changed_authorization = False
    try:
        await audit.execute("SET LOCAL session_replication_role='replica'")
        result = await audit.execute(statement, *args)
        if result != "UPDATE 1":
            raise RuntimeError("synthetic authority tamper cardinality is not exact")
        await audit.execute("SET SESSION AUTHORIZATION brains_app")
        changed_authorization = True
        await audit.execute(
            "SELECT set_config('app.user_id',$1,true)", str(OWNER_A)
        )
        count = await audit.fetchval(
            """SELECT count(*)
               FROM memory.read_governed_claim_supported_held_v1
               WHERE claim_id=$1 AND outbox_id=$2""",
            receipt.claim_id,
            receipt.outbox_id,
        )
        if type(count) is not int or count != 0:
            raise RuntimeError("tampered authority remained selectable")
    finally:
        await transaction.rollback()
        if changed_authorization:
            await audit.execute("RESET SESSION AUTHORIZATION")
    if before != await state_fingerprint(audit, OWNER_A):
        raise RuntimeError("tamper rollback changed durable state")
    if await view_count(
        runtime,
        _TARGET_VIEWS[3],
        OWNER_A,
        claim_id=receipt.claim_id,
    ) != 1:
        raise RuntimeError("supported authority did not recover after tamper")


async def verify_replay_write_probe(
    conn: asyncpg.Connection,
    operation: Any,
    marker: str,
    *args: object,
    **kwargs: object,
) -> None:
    probe = ReplayWriteProbeConnection(conn, marker)
    try:
        await operation(probe, *args, **kwargs)
    except GovernedClaimTransitionError as exc:
        if "transaction failed" not in str(exc):
            raise
    else:
        raise RuntimeError("read-only replay accepted an injected write")
    if not probe.probed:
        raise RuntimeError("read-only replay write probe did not execute")


async def main() -> int:
    runtime = await asyncpg.connect(clone_dsn(), command_timeout=60)
    audit = await asyncpg.connect(audit_dsn(), command_timeout=60)
    try:
        await verify_two_session_boundary(runtime, audit)
        await verify_clone_boundary(runtime)
        await verify_clone_boundary(audit)
        await verify_view_contracts(runtime)
        await verify_owner_bound_views(runtime)
        await verify_audit_session_view_denied(audit)
        await direct_dml_is_denied(runtime)

        support_source = inspect.getsource(promote_independently_reviewed_claim_v1)
        runtime_source = inspect.getsource(
            sys.modules[promote_independently_reviewed_claim_v1.__module__]
        )
        if any(
            forbidden in support_source
            for forbidden in (
                "review_claim_assessment_v5(",
                "preflight_claim_assessment_review_v5(",
                "reviewer_type=",
                "reviewer_ref=",
            )
        ):
            raise RuntimeError("Stage B runtime violates consume-only insert semantics")
        if "ON CONFLICT" in runtime_source:
            raise RuntimeError("transition runtime uses conflict-masking write semantics")
        if any(
            forbidden in runtime_source
            for forbidden in (
                "review_claim_assessment_v5(",
                "preflight_claim_assessment_review_v5(",
            )
        ):
            raise RuntimeError("transition runtime creates its own review authority")
        for command_type in (
            ReviewedProjectionMaterializationV1,
            ReviewedSupportPromotionV1,
        ):
            if any(
                field.name in {"owner_user_id", "actor_user_id", "reviewer_ref"}
                for field in dataclasses.fields(command_type)
            ):
                raise RuntimeError("transition command is allowed to choose an owner")

        plan_ids = (
            required_uuid("MEMORY_V1_TRANSITION_PLAN_ID"),
            required_uuid("MEMORY_V1_TRANSITION_WRONG_REF_PLAN_ID"),
            required_uuid("MEMORY_V1_TRANSITION_SYSTEM_REVIEW_PLAN_ID"),
        )
        if len(set(plan_ids)) != 3:
            raise RuntimeError("synthetic plan IDs are not distinct")

        # Cross-owner denial happens before any Stage A write.
        positive_command = await authorize_projection_outside_transition_runtime(
            runtime, plan_ids[0]
        )
        owner_a_before = await state_fingerprint(audit, OWNER_A)
        owner_b_before = await state_fingerprint(audit, OWNER_B)
        try:
            await materialize_reviewed_claim_v1(
                runtime,
                SyntheticVerifiedActorBinder(OWNER_B, "cross-owner"),
                positive_command,
            )
        except GovernedClaimTransitionError:
            pass
        else:
            raise RuntimeError("cross-owner Stage A unexpectedly succeeded")
        if (
            owner_a_before != await state_fingerprint(audit, OWNER_A)
            or owner_b_before != await state_fingerprint(audit, OWNER_B)
        ):
            raise RuntimeError("cross-owner denial changed durable state")

        stage_a_binder = SyntheticVerifiedActorBinder(OWNER_A, "stage-a")
        stage_a = await materialize_reviewed_claim_v1(
            runtime, stage_a_binder, positive_command
        )
        if (
            stage_a.outcome != "applied"
            or stage_a.rows_written <= 0
            or stage_a_binder.transaction_readonly_observations != ["off"]
            or await view_count(
                runtime,
                _TARGET_VIEWS[1],
                OWNER_A,
                claim_id=stage_a.receipt.claim_id,
            )
            != 1
        ):
            raise RuntimeError("Stage A materialization invariant failed")
        if await audit.fetchval(
            "SELECT count(*) FROM memory.claim_assessment_review_v5 WHERE owner_user_id=$1 AND claim_id=$2",
            OWNER_A,
            stage_a.receipt.claim_id,
        ) != 0:
            raise RuntimeError("Stage A manufactured an assessment review")

        replay_a_before = await state_fingerprint(audit, OWNER_A)
        replay_a_binder = SyntheticVerifiedActorBinder(OWNER_A, "stage-a-replay")
        replay_a = await materialize_reviewed_claim_v1(
            runtime,
            replay_a_binder,
            positive_command,
            prior_receipt=stage_a.receipt,
        )
        if (
            replay_a.outcome != "replayed"
            or replay_a.rows_written != 0
            or replay_a.receipt.canonical_json_bytes()
            != stage_a.receipt.canonical_json_bytes()
            or replay_a.receipt.actor_authentication_manifest_sha256
            != stage_a_binder.authentication_manifest_sha256
            or replay_a_binder.authentication_manifest_sha256
            == stage_a_binder.authentication_manifest_sha256
            or replay_a_binder.transaction_readonly_observations != ["on"]
            or replay_a_before != await state_fingerprint(audit, OWNER_A)
        ):
            raise RuntimeError("Stage A replay is not zero-write/read-only")
        await verify_replay_write_probe(
            runtime,
            materialize_reviewed_claim_v1,
            "governed_claim_transition:materialization_authority",
            SyntheticVerifiedActorBinder(OWNER_A, "stage-a-probe"),
            positive_command,
            prior_receipt=stage_a.receipt,
        )

        support_command = await persist_owner_review_outside_transition_runtime(
            runtime,
            stage_a.receipt,
            reviewer_type="user",
            reviewer_ref=str(OWNER_A),
            label="owner",
        )
        await assert_nested_transaction_denied(
            runtime,
            promote_independently_reviewed_claim_v1,
            SyntheticVerifiedActorBinder(OWNER_A, "nested-support"),
            stage_a.receipt,
            support_command,
        )
        stage_b_ambiguous = AmbiguousCommitConnection(runtime)
        try:
            await promote_independently_reviewed_claim_v1(
                stage_b_ambiguous,
                SyntheticVerifiedActorBinder(OWNER_A, "stage-b-ambiguous"),
                stage_a.receipt,
                support_command,
            )
        except GovernedClaimTransitionError as exc:
            if str(exc) != "support transaction failed":
                raise
        else:
            raise RuntimeError("ambiguous Stage B COMMIT returned success")
        if not stage_b_ambiguous.commit_completed:
            raise RuntimeError("ambiguous Stage B did not commit durable state")
        ambiguous_stage_b_state = await state_fingerprint(audit, OWNER_A)
        if await audit.fetchval(
            """SELECT count(*) FROM memory.projection_outbox
               WHERE owner_user_id=$1 AND aggregate_id=$2""",
            OWNER_A,
            stage_a.receipt.claim_id,
        ) != 1:
            raise RuntimeError("ambiguous Stage B COMMIT did not persist held outbox")

        stage_b_binder = SyntheticVerifiedActorBinder(OWNER_A, "stage-b-recovery")
        stage_b = await promote_independently_reviewed_claim_v1(
            runtime,
            stage_b_binder,
            stage_a.receipt,
            support_command,
        )
        if (
            stage_b.outcome != "replayed"
            or stage_b.rows_written != 0
            or stage_b.derived_index_eligibility_events_created != 0
            or stage_b.derived_index_eligibility_events_held != 1
            or stage_b_binder.transaction_readonly_observations != ["off"]
            or stage_b.receipt.actor_authentication_manifest_sha256
            != stage_b_binder.authentication_manifest_sha256
            or ambiguous_stage_b_state != await state_fingerprint(audit, OWNER_A)
        ):
            raise RuntimeError("receiptless Stage B COMMIT recovery failed")
        await verify_held_outbox_exact(audit, stage_b.receipt)

        post_support_a_before = await state_fingerprint(audit, OWNER_A)
        post_support_receiptless_binder = SyntheticVerifiedActorBinder(
            OWNER_A, "stage-a-receiptless-after-support"
        )
        post_support_receiptless = await materialize_reviewed_claim_v1(
            runtime,
            post_support_receiptless_binder,
            positive_command,
        )
        if (
            post_support_receiptless.outcome != "replayed"
            or post_support_receiptless.rows_written != 0
            or post_support_receiptless.receipt.actor_authentication_manifest_sha256
            != post_support_receiptless_binder.authentication_manifest_sha256
            or post_support_receiptless_binder.transaction_readonly_observations
            != ["off"]
            or post_support_a_before != await state_fingerprint(audit, OWNER_A)
        ):
            raise RuntimeError(
                "receiptless Stage A replay failed after valid Stage B"
            )
        post_support_a_binder = SyntheticVerifiedActorBinder(
            OWNER_A, "stage-a-after-support"
        )
        post_support_a = await materialize_reviewed_claim_v1(
            runtime,
            post_support_a_binder,
            positive_command,
            prior_receipt=stage_a.receipt,
        )
        if (
            post_support_a.outcome != "replayed"
            or post_support_a.rows_written != 0
            or post_support_a.receipt.canonical_json_bytes()
            != stage_a.receipt.canonical_json_bytes()
            or post_support_a_binder.transaction_readonly_observations != ["on"]
            or post_support_a_before != await state_fingerprint(audit, OWNER_A)
        ):
            raise RuntimeError(
                "Stage A replay failed after the valid Stage B transition"
            )

        replay_b_before = await state_fingerprint(audit, OWNER_A)
        replay_b_binder = SyntheticVerifiedActorBinder(OWNER_A, "stage-b-replay")
        replay_b = await promote_independently_reviewed_claim_v1(
            runtime,
            replay_b_binder,
            stage_a.receipt,
            support_command,
            prior_receipt=stage_b.receipt,
        )
        if (
            replay_b.outcome != "replayed"
            or replay_b.rows_written != 0
            or replay_b.derived_index_eligibility_events_created != 0
            or replay_b.receipt.canonical_json_bytes()
            != stage_b.receipt.canonical_json_bytes()
            or replay_b.receipt.actor_authentication_manifest_sha256
            != stage_b_binder.authentication_manifest_sha256
            or replay_b_binder.authentication_manifest_sha256
            == stage_b_binder.authentication_manifest_sha256
            or replay_b_binder.transaction_readonly_observations != ["on"]
            or replay_b_before != await state_fingerprint(audit, OWNER_A)
        ):
            raise RuntimeError("Stage B replay is not zero-write/read-only")
        await verify_replay_write_probe(
            runtime,
            promote_independently_reviewed_claim_v1,
            "governed_claim_transition:supported_authority",
            SyntheticVerifiedActorBinder(OWNER_A, "stage-b-probe"),
            stage_a.receipt,
            support_command,
            prior_receipt=stage_b.receipt,
        )

        # Two separately materialized candidates prove reviewer constraints.
        wrong_command = await authorize_projection_outside_transition_runtime(
            runtime, plan_ids[1]
        )
        wrong_ambiguous = AmbiguousCommitConnection(runtime)
        try:
            await materialize_reviewed_claim_v1(
                wrong_ambiguous,
                SyntheticVerifiedActorBinder(OWNER_A, "stage-a-ambiguous"),
                wrong_command,
            )
        except GovernedClaimTransitionError as exc:
            if str(exc) != "materialization transaction failed":
                raise
        else:
            raise RuntimeError("ambiguous Stage A COMMIT returned success")
        if not wrong_ambiguous.commit_completed:
            raise RuntimeError("ambiguous Stage A did not commit durable state")
        ambiguous_stage_a_state = await state_fingerprint(audit, OWNER_A)
        wrong_recovery_binder = SyntheticVerifiedActorBinder(
            OWNER_A, "stage-a-recovery"
        )
        wrong_materialized = await materialize_reviewed_claim_v1(
            runtime,
            wrong_recovery_binder,
            wrong_command,
        )
        if (
            wrong_materialized.outcome != "replayed"
            or wrong_materialized.rows_written != 0
            or wrong_materialized.receipt.actor_authentication_manifest_sha256
            != wrong_recovery_binder.authentication_manifest_sha256
            or wrong_recovery_binder.transaction_readonly_observations != ["off"]
            or ambiguous_stage_a_state != await state_fingerprint(audit, OWNER_A)
        ):
            raise RuntimeError("receiptless Stage A COMMIT recovery failed")
        wrong_support = await persist_owner_review_outside_transition_runtime(
            runtime,
            wrong_materialized.receipt,
            reviewer_type="user",
            reviewer_ref=str(OWNER_B),
            label="wrong-ref",
        )
        await assert_stage_b_denied_without_writes(
            runtime,
            audit,
            wrong_materialized.receipt,
            wrong_support,
        )

        system_command = await authorize_projection_outside_transition_runtime(
            runtime, plan_ids[2]
        )
        system_materialized = await materialize_reviewed_claim_v1(
            runtime,
            SyntheticVerifiedActorBinder(OWNER_A, "system-stage-a"),
            system_command,
        )
        system_support = await persist_owner_review_outside_transition_runtime(
            runtime,
            system_materialized.receipt,
            reviewer_type="system",
            reviewer_ref="synthetic-system-reviewer",
            label="system",
        )
        await assert_stage_b_denied_without_writes(
            runtime,
            audit,
            system_materialized.receipt,
            system_support,
        )

        await assert_nested_transaction_denied(
            runtime,
            materialize_reviewed_claim_v1,
            SyntheticVerifiedActorBinder(OWNER_A, "nested-materialize"),
            positive_command,
            prior_receipt=stage_a.receipt,
        )

        tamper_cases: Sequence[tuple[str, tuple[object, ...]]] = (
            (
                """UPDATE memory.projection_review
                   SET authorization_manifest_sha256=repeat('0',64)
                   WHERE owner_user_id=$1 AND review_id=$2""",
                (OWNER_A, stage_a.receipt.projection_review_id),
            ),
            (
                """UPDATE memory.claim_assessment_review_v5
                   SET authorization_manifest_sha256=repeat('0',64)
                   WHERE owner_user_id=$1 AND review_id=$2""",
                (OWNER_A, stage_b.receipt.assessment_review_id),
            ),
            (
                """UPDATE memory.projection_outbox
                   SET attempts=1
                   WHERE owner_user_id=$1 AND outbox_id=$2""",
                (OWNER_A, stage_b.receipt.outbox_id),
            ),
        )
        for statement, args in tamper_cases:
            await assert_tamper_hides_supported_authority(
                audit,
                runtime,
                stage_b.receipt,
                statement,
                *args,
            )

        final_view_counts = {
            view: await view_count(runtime, view, OWNER_A)
            for view in _TARGET_VIEWS
        }
        expected_counts = {
            _TARGET_VIEWS[0]: 0,
            _TARGET_VIEWS[1]: 3,
            _TARGET_VIEWS[2]: 0,
            _TARGET_VIEWS[3]: 1,
        }
        if final_view_counts != expected_counts:
            raise RuntimeError("final four-view authority counts drifted")

        output = {
            "contract_version": (
                "memory_v1_governed_claim_transition_clone_two_stage_v1"
            ),
            "stage_a_outcome": stage_a.outcome,
            "stage_b_outcome": stage_b.outcome,
            "stage_a_ambiguous_commit_recovery_outcome": (
                wrong_materialized.outcome
            ),
            "stage_b_ambiguous_commit_recovery_outcome": stage_b.outcome,
            "ambiguous_commit_cases_recovered": 2,
            "stage_a_replay_outcome": replay_a.outcome,
            "stage_a_post_support_replay_outcome": post_support_a.outcome,
            "stage_a_post_support_receiptless_recovery_outcome": (
                post_support_receiptless.outcome
            ),
            "stage_b_replay_outcome": replay_b.outcome,
            "stage_a_replay_rows_written": replay_a.rows_written,
            "stage_b_replay_rows_written": replay_b.rows_written,
            "separate_owner_review_transaction": True,
            "stage_b_review_creation_calls": 0,
            "cross_owner_denied": True,
            "wrong_reviewer_ref_denied": True,
            "system_reviewer_denied": True,
            "nested_transaction_cases_denied": 2,
            "durable_hash_tamper_cases_denied": len(tamper_cases),
            "held_outbox_exact": True,
            "worker_claimable_rows": 0,
            "final_projection_ready_rows": final_view_counts[_TARGET_VIEWS[0]],
            "final_materialization_rows": final_view_counts[_TARGET_VIEWS[1]],
            "final_assessment_ready_rows": final_view_counts[_TARGET_VIEWS[2]],
            "final_supported_held_rows": final_view_counts[_TARGET_VIEWS[3]],
            "four_target_views_verified": True,
            "qdrant_metadata_reads": False,
            "qdrant_point_reads": 0,
            "qdrant_payload_reads": 0,
            "qdrant_search_reads": 0,
            "qdrant_scroll_reads": 0,
            "qdrant_writes": 0,
            "provider_calls": 0,
            "raw_application_values_in_output": False,
        }
        print(
            json.dumps(
                output,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    finally:
        await runtime.close()
        await audit.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
