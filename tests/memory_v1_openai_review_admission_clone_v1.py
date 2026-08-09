#!/usr/bin/env python3
from __future__ import annotations

"""Networkless disposable-clone proof for one reviewed OpenAI observation.

The controller must provide a schema-only PostgreSQL clone containing synthetic
owner/evidence/job/packet/route rows.  This program admits exactly that route,
materializes one candidate claim, records a separate owner review, promotes the
claim to supported, and proves the derived-index outbox item remains held.
"""

import asyncio
import hashlib
import json
import os
import re
import socket
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncpg

from rag_engine.memory_v1_governed_claim_transition_v1 import (
    GovernedClaimTransitionError,
    ReviewedProjectionMaterializationV1,
    ReviewedSupportPromotionV1,
    VerifiedActorContextV1,
    materialize_reviewed_claim_v1,
    promote_independently_reviewed_claim_v1,
)
from rag_engine.memory_v1_openai_review_admission_v1 import (
    OpenAIReviewAdmissionCommandV1,
    OpenAIReviewAdmissionError,
    VerifiedReviewerContextV1,
    admit_reviewed_openai_observation_v1,
)


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_DATABASE = re.compile(r"^memory_review_admission_[a-z0-9_]+$")
_REASON_CODES = ("explicit_owner_review", "single_observation_mvp")


def _deny_internet_sockets(event: str, args: tuple[object, ...]) -> None:
    if event == "socket.__new__" and len(args) > 1:
        if args[1] in {socket.AF_INET, socket.AF_INET6}:
            raise RuntimeError("Internet sockets are forbidden in clone proof")


sys.addaudithook(_deny_internet_sockets)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def required_uuid(name: str) -> UUID:
    return UUID(os.environ[name])


def required_sha(name: str) -> str:
    value = os.environ[name]
    if not _SHA.fullmatch(value):
        raise RuntimeError(f"{name} is not a lowercase SHA-256")
    return value


def operation_id(value: UUID, label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"memory-review-admission-clone|{value}|{label}")


def _isolated_dsn(name: str, username: str) -> str:
    if os.environ.get("MEMORY_V1_REVIEW_ADMISSION_CLONE") != "authorized":
        raise RuntimeError("disposable clone token is absent")
    dsn = os.environ[name]
    parsed = urlparse(dsn)
    socket_path = os.environ["MEMORY_V1_REVIEW_ADMISSION_CLONE_SOCKET"]
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
        or database != os.environ["MEMORY_V1_REVIEW_ADMISSION_CLONE_DATABASE"]
        or not _DATABASE.fullmatch(database)
    ):
        raise RuntimeError("clone DSN is not an exact disposable Unix socket")
    return dsn


class ReviewerBinder:
    async def bind_verified_reviewer(
        self,
        conn: asyncpg.Connection,
        reviewer: VerifiedReviewerContextV1,
    ) -> None:
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)",
            str(reviewer.owner_user_id),
        )


class ActorBinder:
    def __init__(self, owner: UUID, label: str) -> None:
        self.owner = owner
        self.authentication_manifest_sha256 = sha256_text(
            f"clone-verified-actor|{owner}|{label}"
        )

    async def bind_verified_actor(
        self, conn: asyncpg.Connection
    ) -> VerifiedActorContextV1:
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(self.owner))
        return VerifiedActorContextV1(
            owner_user_id=self.owner,
            authentication_manifest_sha256=self.authentication_manifest_sha256,
        )


async def state_sha(conn: asyncpg.Connection, owner: UUID) -> str:
    value = await conn.fetchval(
        """
        SELECT encode(public.digest(convert_to(jsonb_build_object(
          'operations',(SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id=$1),
          'batches',(SELECT count(*) FROM memory.relational_stage_batch WHERE owner_user_id=$1),
          'observations',(SELECT count(*) FROM memory.observation WHERE owner_user_id=$1),
          'plans',(SELECT count(*) FROM memory.projection_plan WHERE owner_user_id=$1),
          'reviews',(SELECT count(*) FROM memory.projection_review WHERE owner_user_id=$1),
          'claims',(SELECT count(*) FROM memory.claim WHERE owner_user_id=$1),
          'revisions',(SELECT count(*) FROM memory.claim_revision WHERE owner_user_id=$1),
          'assessments',(SELECT count(*) FROM memory.claim_assessment WHERE owner_user_id=$1),
          'outbox',(SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id=$1)
        )::text,'UTF8'),'sha256'),'hex')
        """,
        owner,
    )
    return str(value)


async def state_counts(conn: asyncpg.Connection, owner: UUID) -> dict[str, int]:
    relations = (
        "relational_operation_request",
        "entity_resolution_apply",
        "observation_entailment_v5",
        "projection_plan",
        "projection_review",
        "claim",
        "projection_outbox",
    )
    counts: dict[str, int] = {}
    for relation in relations:
        counts[relation] = int(
            await conn.fetchval(
                f"SELECT count(*) FROM memory.{relation} WHERE owner_user_id=$1",
                owner,
            )
        )
    return counts


async def persist_owner_support_review(
    conn: asyncpg.Connection,
    materialization: object,
) -> ReviewedSupportPromotionV1:
    receipt = materialization.receipt
    reason_codes = json.dumps(list(_REASON_CODES), separators=(",", ":"))
    review_request_id = operation_id(receipt.claim_id, "support-review")
    apply_request_id = operation_id(receipt.claim_id, "support-apply")
    async with conn.transaction(isolation="serializable"):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(OWNER_A))
        preflight = await conn.fetchrow(
            """
            SELECT * FROM memory.preflight_claim_assessment_review_v5(
              $1,'promote_supported'::memory.claim_assessment_action_v5,
              $2::numeric,$3::numeric,$4::numeric,$5::numeric,
              $6::jsonb,$7,'user',$8)
            """,
            receipt.claim_id,
            Decimal("0.750"),
            Decimal("0.000"),
            Decimal("0.875"),
            Decimal("0.900"),
            reason_codes,
            "explicit owner authorization for one synthetic clone proposition",
            str(OWNER_A),
        )
        reviewed = await conn.fetchrow(
            """
            SELECT * FROM memory.review_claim_assessment_v5(
              $1,$2,'promote_supported'::memory.claim_assessment_action_v5,
              $3::numeric,$4::numeric,$5::numeric,$6::numeric,
              $7::jsonb,$8,'user',$9,$10)
            """,
            review_request_id,
            receipt.claim_id,
            Decimal("0.750"),
            Decimal("0.000"),
            Decimal("0.875"),
            Decimal("0.900"),
            reason_codes,
            "explicit owner authorization for one synthetic clone proposition",
            str(OWNER_A),
            preflight["authorization_manifest_sha256"],
        )
        apply_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_claim_assessment_apply_v5($1,$2)",
            receipt.claim_id,
            reviewed["review_id"],
        )
    return ReviewedSupportPromotionV1(
        materialization_receipt_sha256=receipt.receipt_sha256,
        claim_id=receipt.claim_id,
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


async def main() -> int:
    runtime = await asyncpg.connect(
        _isolated_dsn("MEMORY_V1_REVIEW_ADMISSION_CLONE_DSN", "brains_app"),
        command_timeout=60,
    )
    audit = await asyncpg.connect(
        _isolated_dsn("MEMORY_V1_REVIEW_ADMISSION_CLONE_AUDIT_DSN", "sage"),
        command_timeout=60,
    )
    try:
        if await audit.fetchval("SELECT current_setting('server_version_num')") != os.environ["MEMORY_V1_REVIEW_ADMISSION_SERVER_VERSION"]:
            raise RuntimeError("clone PostgreSQL version drifted")
        if await audit.fetchval("SELECT system_identifier::text FROM pg_control_system()") == os.environ["MEMORY_V1_REVIEW_ADMISSION_PRODUCTION_SYSTEM_ID"]:
            raise RuntimeError("clone is the production PostgreSQL system")
        route_id = required_uuid("MEMORY_V1_REVIEW_ADMISSION_ROUTE_ID")
        stage_sha = required_sha("MEMORY_V1_REVIEW_ADMISSION_STAGE_SHA256")
        reviewer = VerifiedReviewerContextV1(
            owner_user_id=OWNER_A,
            reviewer_type="user",
            reviewer_ref=str(OWNER_A),
            authentication_manifest_sha256=sha256_text(
                "synthetic-owner-browser-authentication"
            ),
        )
        command = OpenAIReviewAdmissionCommandV1(
            entity_resolution_request_id=operation_id(
                route_id, "entity-resolution-apply"
            ),
            route_event_id=route_id,
            expected_stage_bundle_sha256=stage_sha,
            plan_id=operation_id(route_id, "projection-plan"),
            entailment_request_id=operation_id(route_id, "entailment"),
            reason="explicit owner authorization for one synthetic clone proposition",
            reason_codes=_REASON_CODES,
        )
        initial_a = await state_sha(audit, OWNER_A)
        initial_b = await state_sha(audit, OWNER_B)
        applied = await admit_reviewed_openai_observation_v1(
            runtime,
            binder=ReviewerBinder(),
            reviewer=reviewer,
            command=command,
        )
        if applied.outcome != "applied" or applied.rows_written <= 0:
            raise RuntimeError("one-item admission did not apply")
        if await audit.fetchval(
            "SELECT count(*) FROM memory.claim WHERE owner_user_id=$1", OWNER_A
        ) != 0:
            raise RuntimeError("admission auto-materialized a claim")
        if await audit.fetchval(
            "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id=$1",
            OWNER_A,
        ) != 0:
            raise RuntimeError("admission released projection work")
        replay_state = await state_sha(audit, OWNER_A)
        replay_counts_before = await state_counts(audit, OWNER_A)
        replay = await admit_reviewed_openai_observation_v1(
            runtime,
            binder=ReviewerBinder(),
            reviewer=reviewer,
            command=command,
        )
        replay_state_after = await state_sha(audit, OWNER_A)
        replay_counts_after = await state_counts(audit, OWNER_A)
        replay_checks = {
            "outcome": replay.outcome,
            "rows_written": replay.rows_written,
            "receipt_equal": replay.receipt_sha256 == applied.receipt_sha256,
            "state_equal": replay_state_after == replay_state,
            "count_delta": {
                key: replay_counts_after[key] - replay_counts_before[key]
                for key in replay_counts_before
            },
        }
        if (
            replay_checks["outcome"] != "replayed"
            or replay_checks["rows_written"] != 0
            or replay_checks["receipt_equal"] is not True
            or replay_checks["state_equal"] is not True
            or any(replay_checks["count_delta"].values())
        ):
            raise RuntimeError(
                "admission replay invariant failed: "
                + json.dumps(replay_checks, sort_keys=True)
            )

        other_reviewer = VerifiedReviewerContextV1(
            owner_user_id=OWNER_B,
            reviewer_type="user",
            reviewer_ref=str(OWNER_B),
            authentication_manifest_sha256=sha256_text(
                "synthetic-other-owner-authentication"
            ),
        )
        try:
            await admit_reviewed_openai_observation_v1(
                runtime,
                binder=ReviewerBinder(),
                reviewer=other_reviewer,
                command=command,
            )
        except (OpenAIReviewAdmissionError, asyncpg.PostgresError):
            pass
        else:
            raise RuntimeError("cross-owner admission unexpectedly succeeded")
        if (
            await state_sha(audit, OWNER_A) != replay_state
            or await state_sha(audit, OWNER_B) != initial_b
        ):
            raise RuntimeError("cross-owner denial changed durable state")

        materialization_command = ReviewedProjectionMaterializationV1(
            plan_id=applied.plan_id,
            projection_ref="p01",
            projection_review_id=applied.projection_review_id,
            projection_request_id=operation_id(applied.plan_id, "projection-apply"),
            projection_apply_manifest_sha256=(
                applied.projection_apply_manifest_sha256
            ),
        )
        materialized = await materialize_reviewed_claim_v1(
            runtime,
            ActorBinder(OWNER_A, "materialize"),
            materialization_command,
        )
        if materialized.outcome != "applied" or materialized.rows_written <= 0:
            raise RuntimeError("reviewed projection did not materialize")
        support_command = await persist_owner_support_review(runtime, materialized)
        promoted = await promote_independently_reviewed_claim_v1(
            runtime,
            ActorBinder(OWNER_A, "support"),
            materialized.receipt,
            support_command,
        )
        if (
            promoted.outcome != "applied"
            or promoted.receipt.claim_revision_number != 2
            or promoted.derived_index_eligibility_events_created != 1
            or promoted.derived_index_eligibility_events_held != 1
        ):
            raise RuntimeError("independently reviewed support transition failed")
        outbox = await audit.fetchrow(
            """
            SELECT aggregate_id,available_at='infinity'::timestamptz AS held,
              attempts,lease_token
            FROM memory.projection_outbox
            WHERE owner_user_id=$1 AND outbox_id=$2
            """,
            OWNER_A,
            promoted.receipt.outbox_id,
        )
        if (
            outbox is None
            or outbox["aggregate_id"] != promoted.receipt.claim_id
            or outbox["held"] is not True
            or outbox["attempts"] != 0
            or outbox["lease_token"] is not None
        ):
            raise RuntimeError("derived-index outbox is not exactly held")
        claim = await audit.fetchrow(
            """
            SELECT claim.status,
              (SELECT max(revision.revision_number)
               FROM memory.claim_revision AS revision
               WHERE revision.owner_user_id=claim.owner_user_id
                 AND revision.claim_id=claim.claim_id) AS current_revision_number
            FROM memory.claim AS claim
            WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
            """,
            OWNER_A,
            promoted.receipt.claim_id,
        )
        if claim is None or str(claim["status"]) != "supported" or claim["current_revision_number"] != 2:
            raise RuntimeError("canonical supported claim state drifted")
        output = {
            "contract_version": "memory_v1_openai_review_admission_clone_v1",
            "admission_outcome": applied.outcome,
            "admission_replay_outcome": replay.outcome,
            "cross_owner_denied": True,
            "materialization_outcome": materialized.outcome,
            "support_outcome": promoted.outcome,
            "supported_claim_revision": 2,
            "held_outbox": True,
            "provider_calls": 0,
            "qdrant_reads": 0,
            "qdrant_writes": 0,
            "production_rows_copied": 0,
            "initial_owner_state_sha256": initial_a,
            "final_owner_state_sha256": await state_sha(audit, OWNER_A),
        }
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return 0
    finally:
        await runtime.close()
        await audit.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
