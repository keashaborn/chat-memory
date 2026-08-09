#!/usr/bin/env python3
from __future__ import annotations

"""Networkless disposable-clone proof for governed claim lifecycle V1."""

import asyncio
import json
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncpg


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")
_DATABASE = re.compile(r"^memory_claim_lifecycle_[a-z0-9_]+$")


def _deny_internet_sockets(event: str, args: tuple[object, ...]) -> None:
    if event == "socket.__new__" and len(args) > 1:
        if args[1] in {socket.AF_INET, socket.AF_INET6}:
            raise RuntimeError("Internet sockets are forbidden in clone proof")


sys.addaudithook(_deny_internet_sockets)


def _isolated_dsn(name: str, username: str) -> str:
    if os.environ.get("MEMORY_V1_CLAIM_LIFECYCLE_CLONE") != "authorized":
        raise RuntimeError("disposable clone token is absent")
    dsn = os.environ[name]
    parsed = urlparse(dsn)
    socket_path = os.environ["MEMORY_V1_CLAIM_LIFECYCLE_CLONE_SOCKET"]
    socket_dir = Path(socket_path)
    query = parse_qs(parsed.query, strict_parsing=True)
    database = parsed.path.removeprefix("/")
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname is not None
        or parsed.port is not None
        or parsed.username != username
        or not parsed.password
        or parsed.params
        or parsed.fragment
        or query != {"host": [socket_path]}
        or not socket_dir.is_absolute()
        or not socket_dir.is_dir()
        or socket_dir.is_symlink()
        or socket_dir.resolve(strict=True) != socket_dir
        or database != os.environ["MEMORY_V1_CLAIM_LIFECYCLE_CLONE_DATABASE"]
        or not _DATABASE.fullmatch(database)
    ):
        raise RuntimeError("clone DSN is not an exact disposable Unix socket")
    return dsn


async def _bind(conn: asyncpg.Connection, owner: UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,false)", str(owner))


async def _owner_state(conn: asyncpg.Connection, owner: UUID) -> str:
    return str(
        await conn.fetchval(
            """
            SELECT encode(public.digest(convert_to(jsonb_build_object(
              'claims',(SELECT count(*) FROM memory.claim WHERE owner_user_id=$1),
              'revisions',(SELECT count(*) FROM memory.claim_revision WHERE owner_user_id=$1),
              'assessments',(SELECT count(*) FROM memory.claim_assessment WHERE owner_user_id=$1),
              'review_events',(SELECT count(*) FROM memory.claim_assessment_review_v5 WHERE owner_user_id=$1),
              'apply_events',(SELECT count(*) FROM memory.claim_assessment_apply_v5 WHERE owner_user_id=$1),
              'requests',(SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id=$1),
              'evidence',(SELECT count(*) FROM memory.evidence WHERE owner_user_id=$1),
              'outbox',(SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id=$1)
            )::text,'UTF8'),'sha256'),'hex')
            """,
            owner,
        )
    )


async def main() -> int:
    runtime = await asyncpg.connect(
        _isolated_dsn("MEMORY_V1_CLAIM_LIFECYCLE_CLONE_DSN", "brains_app"),
        command_timeout=30,
    )
    audit = await asyncpg.connect(
        _isolated_dsn("MEMORY_V1_CLAIM_LIFECYCLE_CLONE_AUDIT_DSN", "postgres"),
        command_timeout=30,
    )
    try:
        if await audit.fetchval("SELECT current_setting('server_version_num')") != os.environ["MEMORY_V1_CLAIM_LIFECYCLE_SERVER_VERSION"]:
            raise RuntimeError("clone PostgreSQL version drifted")
        if await audit.fetchval("SELECT system_identifier::text FROM pg_control_system()") == os.environ["MEMORY_V1_CLAIM_LIFECYCLE_PRODUCTION_SYSTEM_ID"]:
            raise RuntimeError("clone is the production PostgreSQL system")

        claim_id = await audit.fetchval(
            """
            SELECT claim_id FROM memory.claim
            WHERE owner_user_id=$1 AND status='supported'
              AND canonical_key LIKE 'v5:%'
              AND metadata->>'memory_contract'='memory_projection_v5'
            ORDER BY claim_id
            """,
            OWNER_A,
        )
        if claim_id is None:
            raise RuntimeError("synthetic supported V5 claim is absent")
        if await audit.fetchval(
            "SELECT count(*) FROM memory.claim WHERE owner_user_id=$1 AND status='supported' AND canonical_key LIKE 'v5:%'",
            OWNER_A,
        ) != 1:
            raise RuntimeError("clone must contain exactly one synthetic supported V5 claim")

        await _bind(runtime, OWNER_A)
        rows = await runtime.fetch(
            "SELECT * FROM memory.read_owner_governed_claim_lifecycle_v1(100)"
        )
        selected = [row for row in rows if row["claim_id"] == claim_id]
        if len(selected) != 1 or selected[0]["status"] != "supported" or selected[0]["current_revision_number"] != 2:
            raise RuntimeError("owner lifecycle read did not return the exact supported claim")
        expected_sha = str(selected[0]["claim_state_sha256"])

        before_a = await _owner_state(audit, OWNER_A)
        before_b = await _owner_state(audit, OWNER_B)
        review_id = uuid5(NAMESPACE_URL, f"claim-lifecycle-clone|{claim_id}|review")
        apply_id = uuid5(NAMESPACE_URL, f"claim-lifecycle-clone|{claim_id}|apply")

        try:
            await runtime.fetchrow(
                "SELECT * FROM memory.retract_owner_governed_claim_v1($1,$2,$3,$4,$5,$6)",
                review_id,
                apply_id,
                claim_id,
                1,
                expected_sha,
                "stale revision probe",
            )
        except asyncpg.PostgresError:
            pass
        else:
            raise RuntimeError("stale revision unexpectedly retracted the claim")
        if await _owner_state(audit, OWNER_A) != before_a:
            raise RuntimeError("stale-state denial changed owner state")

        await _bind(runtime, OWNER_B)
        try:
            await runtime.fetchrow(
                "SELECT * FROM memory.retract_owner_governed_claim_v1($1,$2,$3,$4,$5,$6)",
                uuid5(NAMESPACE_URL, "claim-lifecycle-cross-owner-review"),
                uuid5(NAMESPACE_URL, "claim-lifecycle-cross-owner-apply"),
                claim_id,
                2,
                expected_sha,
                "cross-owner probe",
            )
        except asyncpg.PostgresError:
            pass
        else:
            raise RuntimeError("cross-owner retraction unexpectedly succeeded")
        if await _owner_state(audit, OWNER_A) != before_a or await _owner_state(audit, OWNER_B) != before_b:
            raise RuntimeError("cross-owner denial changed durable state")

        await _bind(runtime, OWNER_A)
        rollback_review_id = uuid5(
            NAMESPACE_URL,
            f"claim-lifecycle-clone|{claim_id}|rollback-review",
        )
        rollback_apply_id = uuid5(
            NAMESPACE_URL,
            f"claim-lifecycle-clone|{claim_id}|rollback-apply",
        )
        try:
            async with runtime.transaction(isolation="serializable"):
                await _bind(runtime, OWNER_A)
                await runtime.fetchrow(
                    "SELECT * FROM memory.retract_owner_governed_claim_v1($1,$2,$3,$4,$5,$6)",
                    rollback_review_id,
                    rollback_apply_id,
                    claim_id,
                    2,
                    expected_sha,
                    "atomic rollback probe",
                )
                await runtime.fetchrow(
                    """SELECT * FROM memory.record_owner_evidence_v1(
                         'user_statement'::memory.evidence_kind,$1,$2,$3,
                         clock_timestamp(),1,1,$4,
                         'high'::memory.sensitivity_level,$5::jsonb
                       )""",
                    "",
                    "invalid-atomic-rollback-probe",
                    "This insert must fail.",
                    "invalid-atomic-rollback-probe",
                    "{}",
                )
        except asyncpg.PostgresError:
            pass
        else:
            raise RuntimeError("failed correction evidence did not abort the transaction")
        if await _owner_state(audit, OWNER_A) != before_a:
            raise RuntimeError("failed correction transaction changed durable state")

        correction_external_id = f"governed-claim-correction:{claim_id}:{apply_id}"
        correction_metadata = json.dumps(
            {
                "contract_version": "memory_v1_governed_claim_correction_evidence_v1",
                "corrected_claim_id": str(claim_id),
                "correction_operation_id": str(apply_id),
                "expected_prior_revision_number": 2,
                "expected_prior_claim_state_sha256": expected_sha,
                "replacement_requires_governed_review": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        async with runtime.transaction(isolation="serializable"):
            await _bind(runtime, OWNER_A)
            applied = await runtime.fetchrow(
                "SELECT * FROM memory.retract_owner_governed_claim_v1($1,$2,$3,$4,$5,$6)",
                review_id,
                apply_id,
                claim_id,
                2,
                expected_sha,
                "owner corrected the governed claim",
            )
            evidence = await runtime.fetchrow(
                """SELECT * FROM memory.record_owner_evidence_v1(
                     'user_statement'::memory.evidence_kind,$1,$2,$3,
                     clock_timestamp(),1,1,$4,
                     'high'::memory.sensitivity_level,$5::jsonb
                   )""",
                "frontend/governed-claim-correction:user",
                correction_external_id,
                "The owner now prefers detailed answers.",
                f"governed-claim-correction:{claim_id}",
                correction_metadata,
            )
        if applied is None or applied["outcome"] != "applied" or applied["resulting_revision_number"] != 3 or applied["outbox_dispatch_held"] is not True:
            raise RuntimeError("owner retraction did not apply exactly once")
        if evidence is None or evidence["outcome"] != "applied":
            raise RuntimeError("replacement evidence did not apply exactly once")
        state_after_apply = await _owner_state(audit, OWNER_A)
        async with runtime.transaction(isolation="serializable"):
            await _bind(runtime, OWNER_A)
            replay = await runtime.fetchrow(
                "SELECT * FROM memory.retract_owner_governed_claim_v1($1,$2,$3,$4,$5,$6)",
                review_id,
                apply_id,
                claim_id,
                2,
                expected_sha,
                "owner corrected the governed claim",
            )
            evidence_replay = await runtime.fetchrow(
                """SELECT * FROM memory.record_owner_evidence_v1(
                     'user_statement'::memory.evidence_kind,$1,$2,$3,
                     clock_timestamp(),1,1,$4,
                     'high'::memory.sensitivity_level,$5::jsonb
                   )""",
                "frontend/governed-claim-correction:user",
                correction_external_id,
                "The owner now prefers detailed answers.",
                f"governed-claim-correction:{claim_id}",
                correction_metadata,
            )
        if replay is None or replay["outcome"] != "replayed" or replay["event_id"] != applied["event_id"] or replay["outbox_id"] != applied["outbox_id"]:
            raise RuntimeError("exact retraction replay drifted")
        if evidence_replay is None or evidence_replay["outcome"] != "replayed" or evidence_replay["evidence_id"] != evidence["evidence_id"]:
            raise RuntimeError("exact replacement-evidence replay drifted")
        if await _owner_state(audit, OWNER_A) != state_after_apply:
            raise RuntimeError("exact replay changed durable state")

        terminal = await audit.fetchrow(
            """
            SELECT claim.status,
              (SELECT max(revision_number) FROM memory.claim_revision WHERE owner_user_id=claim.owner_user_id AND claim_id=claim.claim_id) AS revision_number,
              (SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id=claim.owner_user_id AND aggregate_id=claim.claim_id AND operation='delete' AND available_at='infinity'::timestamptz) AS held_delete_count
            FROM memory.claim AS claim
            WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
            """,
            OWNER_A,
            claim_id,
        )
        if terminal is None or str(terminal["status"]) != "retracted" or terminal["revision_number"] != 3 or terminal["held_delete_count"] != 1:
            raise RuntimeError("semantic deletion did not retain an exact retracted history")

        replacement = await audit.fetchrow(
            """SELECT kind::text,source_system,external_id,status::text,metadata
               FROM memory.evidence
               WHERE owner_user_id=$1 AND evidence_id=$2""",
            OWNER_A,
            evidence["evidence_id"],
        )
        replacement_metadata = None if replacement is None else replacement["metadata"]
        if isinstance(replacement_metadata, str):
            replacement_metadata = json.loads(replacement_metadata)
        if (
            replacement is None
            or replacement["kind"] != "user_statement"
            or replacement["source_system"] != "frontend/governed-claim-correction:user"
            or replacement["external_id"] != correction_external_id
            or replacement["status"] != "active"
            or not isinstance(replacement_metadata, dict)
            or replacement_metadata.get("replacement_requires_governed_review") is not True
        ):
            raise RuntimeError("replacement evidence lost governed correction lineage")

        print(json.dumps({
            "contract_version": "memory_v1_governed_claim_lifecycle_clone_v1",
            "atomic_correction_rollback": True,
            "cross_owner_denied": True,
            "history_retained": True,
            "outbox_dispatch_held": True,
            "physical_claim_deleted": False,
            "provider_calls": 0,
            "qdrant_reads": 0,
            "qdrant_writes": 0,
            "replay_outcome": replay["outcome"],
            "replacement_evidence_outcome": evidence["outcome"],
            "replacement_evidence_replay_outcome": evidence_replay["outcome"],
            "replacement_requires_governed_review": True,
            "resulting_revision_number": terminal["revision_number"],
            "stale_state_denied": True,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    finally:
        await runtime.close()
        await audit.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
