from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import asyncpg


AUTO_APPROVED_EXTRACTORS = {
    "structured_system_v1",
    "explicit_user_correction_v1",
    "reviewed_seed_v1",
}

ALLOWED_CLAIM_STATUSES = {"supported", "uncertain", "disputed"}
ALLOWED_EVIDENCE_STANCES = {"supports", "opposes", "qualifies", "context"}
ALLOWED_SENSITIVITIES = {"low", "medium", "high", "restricted"}
PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_.]{1,127}$")


class MemoryV1Error(RuntimeError):
    pass


class InvalidActor(MemoryV1Error):
    pass


class EvidenceConflict(MemoryV1Error):
    pass


class CandidateNotFound(MemoryV1Error):
    pass


class CandidateConflict(MemoryV1Error):
    pass


class CandidateNotApproved(MemoryV1Error):
    pass


class ProposalValidationError(MemoryV1Error):
    pass


def actor_uuid(value: str | uuid.UUID) -> uuid.UUID:
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidActor("actor_user_id must be a UUID") from exc
    if parsed.int == 0:
        raise InvalidActor("actor_user_id must not be the nil UUID")
    return parsed


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: Any, field: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MemoryV1Error(f"stored {field} is not valid JSON") from exc
        if isinstance(decoded, dict):
            return decoded
    raise MemoryV1Error(f"stored {field} must be a JSON object")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def proposal_hash(proposal: Dict[str, Any]) -> str:
    normalized = normalize_proposal(proposal)
    return _sha256_text(_stable_json(normalized))


def _bounded_score(value: Any, field: str, default: float) -> float:
    if value is None:
        return default
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ProposalValidationError(f"{field} must be numeric") from exc
    if not 0.0 <= score <= 1.0:
        raise ProposalValidationError(f"{field} must be between 0 and 1")
    return score


def _required_text(value: Any, field: str, *, max_length: int = 500) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise ProposalValidationError(f"{field} is required")
    if len(text) > max_length:
        raise ProposalValidationError(f"{field} exceeds {max_length} characters")
    return text


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalize_entity(raw: Any, field: str) -> Dict[str, str]:
    if not isinstance(raw, dict):
        raise ProposalValidationError(f"{field} must be an object")
    entity_key = _required_text(raw.get("entity_key"), f"{field}.entity_key", max_length=240)
    entity_type = _required_text(raw.get("entity_type"), f"{field}.entity_type", max_length=120)
    canonical_name = _required_text(
        raw.get("canonical_name"), f"{field}.canonical_name", max_length=300
    )
    return {
        "entity_key": entity_key,
        "entity_type": entity_type,
        "canonical_name": canonical_name,
        "normalized_name": _normalize_name(canonical_name),
    }


def _normalize_timestamp(value: Any, field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProposalValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProposalValidationError(f"{field} must include a timezone")
    return parsed.isoformat()


def _timestamp_parameter(value: Any, field: str) -> Optional[datetime]:
    normalized = _normalize_timestamp(value, field)
    return datetime.fromisoformat(normalized) if normalized else None


def normalize_proposal(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProposalValidationError("proposal must be an object")

    forbidden = {"owner_user_id", "user_id", "vantage_id"}.intersection(raw)
    if forbidden:
        raise ProposalValidationError(
            "proposal must not contain identity fields: " + ", ".join(sorted(forbidden))
        )

    subject = _normalize_entity(raw.get("subject"), "subject")
    predicate = _required_text(raw.get("predicate"), "predicate", max_length=128).lower()
    if not PREDICATE_RE.fullmatch(predicate):
        raise ProposalValidationError("predicate has an invalid format")

    has_literal = "object_literal" in raw and raw.get("object_literal") is not None
    has_entity = "object_entity" in raw and raw.get("object_entity") is not None
    if has_literal == has_entity:
        raise ProposalValidationError(
            "proposal must contain exactly one of object_literal or object_entity"
        )

    object_literal = raw.get("object_literal") if has_literal else None
    object_entity = _normalize_entity(raw.get("object_entity"), "object_entity") if has_entity else None

    status = str(raw.get("status") or "supported").strip().lower()
    if status not in ALLOWED_CLAIM_STATUSES:
        raise ProposalValidationError(f"unsupported claim status: {status}")

    stance = str(raw.get("evidence_stance") or "supports").strip().lower()
    if stance not in ALLOWED_EVIDENCE_STANCES:
        raise ProposalValidationError(f"unsupported evidence stance: {stance}")

    sensitivity = str(raw.get("sensitivity") or "medium").strip().lower()
    if sensitivity not in ALLOWED_SENSITIVITIES:
        raise ProposalValidationError(f"unsupported sensitivity: {sensitivity}")

    retrieval_policy = raw.get("retrieval_policy") or {}
    qualifiers = raw.get("qualifiers") or {}
    metadata = raw.get("metadata") or {}
    if not isinstance(retrieval_policy, dict):
        raise ProposalValidationError("retrieval_policy must be an object")
    if not isinstance(qualifiers, dict):
        raise ProposalValidationError("qualifiers must be an object")
    if not isinstance(metadata, dict):
        raise ProposalValidationError("metadata must be an object")

    supersedes_claim_id = raw.get("supersedes_claim_id")
    if supersedes_claim_id not in (None, ""):
        try:
            supersedes_claim_id = str(uuid.UUID(str(supersedes_claim_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProposalValidationError("supersedes_claim_id must be a UUID") from exc
    else:
        supersedes_claim_id = None

    valid_from = _normalize_timestamp(raw.get("valid_from"), "valid_from")
    valid_to = _normalize_timestamp(raw.get("valid_to"), "valid_to")
    if valid_from and valid_to and datetime.fromisoformat(valid_to) < datetime.fromisoformat(valid_from):
        raise ProposalValidationError("valid_to must not precede valid_from")

    normalized: Dict[str, Any] = {
        "subject": subject,
        "predicate": predicate,
        "object_literal": object_literal,
        "object_entity": object_entity,
        "canonical_text": _required_text(
            raw.get("canonical_text"), "canonical_text", max_length=2000
        ),
        "qualifiers": qualifiers,
        "status": status,
        "confidence": _bounded_score(raw.get("confidence"), "confidence", 0.5),
        "importance": _bounded_score(raw.get("importance"), "importance", 0.5),
        "salience": _bounded_score(raw.get("salience"), "salience", 0.5),
        "sensitivity": sensitivity,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "retrieval_policy": retrieval_policy,
        "metadata": metadata,
        "evidence_stance": stance,
        "evidence_relevance": _bounded_score(
            raw.get("evidence_relevance"), "evidence_relevance", 1.0
        ),
        "support_score": _bounded_score(raw.get("support_score"), "support_score", 0.5),
        "opposition_score": _bounded_score(
            raw.get("opposition_score"), "opposition_score", 0.0
        ),
        "assessment_method": _required_text(
            raw.get("assessment_method") or "reviewed_candidate",
            "assessment_method",
            max_length=120,
        ),
        "assessment_method_version": _required_text(
            raw.get("assessment_method_version") or "v1",
            "assessment_method_version",
            max_length=120,
        ),
        "supersedes_claim_id": supersedes_claim_id,
    }
    return normalized


def canonical_claim_key(proposal: Dict[str, Any]) -> str:
    normalized = normalize_proposal(proposal)
    identity = {
        "subject_entity_key": normalized["subject"]["entity_key"],
        "predicate": normalized["predicate"],
        "object_literal": normalized["object_literal"],
        "object_entity_key": (
            normalized["object_entity"]["entity_key"] if normalized["object_entity"] else None
        ),
        "qualifiers": normalized["qualifiers"],
        "valid_from": normalized["valid_from"],
        "valid_to": normalized["valid_to"],
    }
    return _sha256_text(_stable_json(identity))


async def _set_actor(conn: asyncpg.Connection, actor: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(actor))


async def record_evidence(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    *,
    kind: str,
    source_system: str,
    external_id: str,
    content: Optional[str],
    observed_at: Optional[datetime] = None,
    directness: Optional[float] = None,
    source_reliability: Optional[float] = None,
    independence_key: Optional[str] = None,
    sensitivity: str = "medium",
    metadata: Optional[Dict[str, Any]] = None,
) -> uuid.UUID:
    actor = actor_uuid(actor_user_id)
    source_system = _required_text(source_system, "source_system", max_length=200)
    external_id = _required_text(external_id, "external_id", max_length=500)
    sensitivity = str(sensitivity or "medium").strip().lower()
    if sensitivity not in ALLOWED_SENSITIVITIES:
        raise ProposalValidationError(f"unsupported sensitivity: {sensitivity}")
    if directness is not None:
        directness = _bounded_score(directness, "directness", 0.0)
    if source_reliability is not None:
        source_reliability = _bounded_score(source_reliability, "source_reliability", 0.0)
    metadata = metadata or {}
    if not isinstance(metadata, dict):
        raise ProposalValidationError("metadata must be an object")

    content_hash = _sha256_text(content) if content is not None else None

    async with conn.transaction():
        await _set_actor(conn, actor)
        row = await conn.fetchrow(
            """
            INSERT INTO memory.evidence(
              owner_user_id, kind, source_system, external_id, content,
              content_sha256, observed_at, directness, source_reliability,
              independence_key, sensitivity, metadata
            )
            VALUES(
              $1, $2::memory.evidence_kind, $3, $4, $5,
              $6, $7, $8, $9, $10, $11::memory.sensitivity_level, $12::jsonb
            )
            ON CONFLICT (owner_user_id, source_system, external_id) DO NOTHING
            RETURNING evidence_id, content_sha256
            """,
            actor,
            kind,
            source_system,
            external_id,
            content,
            content_hash,
            observed_at,
            directness,
            source_reliability,
            independence_key,
            sensitivity,
            _stable_json(metadata),
        )
        if row:
            return uuid.UUID(str(row["evidence_id"]))

        existing = await conn.fetchrow(
            """
            SELECT evidence_id, content_sha256, status::text
            FROM memory.evidence
            WHERE owner_user_id=$1 AND source_system=$2 AND external_id=$3
            """,
            actor,
            source_system,
            external_id,
        )
        if not existing:
            raise EvidenceConflict("evidence conflict could not be resolved")
        if existing["status"] in {"redacted", "deleted"}:
            raise EvidenceConflict(
                "evidence external_id is tombstoned and cannot be reinserted"
            )
        if (existing["content_sha256"] or None) != content_hash:
            raise EvidenceConflict("evidence external_id already exists with different content")
        return uuid.UUID(str(existing["evidence_id"]))


async def propose_candidate(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    *,
    evidence_id: str | uuid.UUID,
    proposal: Dict[str, Any],
    extractor: str,
    extractor_version: str,
    comparison: Optional[Dict[str, Any]] = None,
    auto_approve: bool = False,
) -> Dict[str, Any]:
    actor = actor_uuid(actor_user_id)
    evidence_uuid = uuid.UUID(str(evidence_id))
    normalized = normalize_proposal(proposal)
    digest = _sha256_text(_stable_json(normalized))
    extractor = _required_text(extractor, "extractor", max_length=120)
    extractor_version = _required_text(
        extractor_version, "extractor_version", max_length=120
    )
    comparison = comparison or {}
    if not isinstance(comparison, dict):
        raise ProposalValidationError("comparison must be an object")
    if auto_approve and extractor not in AUTO_APPROVED_EXTRACTORS:
        raise ProposalValidationError(f"extractor is not auto-approved: {extractor}")
    status = "approved" if auto_approve else "review_required"

    async with conn.transaction():
        await _set_actor(conn, actor)
        if not await conn.fetchval(
            """
            SELECT 1
            FROM memory.evidence
            WHERE owner_user_id=$1 AND evidence_id=$2 AND status='active'
            """,
            actor,
            evidence_uuid,
        ):
            raise ProposalValidationError(
                "evidence_id is not active and visible to the actor"
            )

        row = await conn.fetchrow(
            """
            INSERT INTO memory.candidate(
              owner_user_id, evidence_id, status, proposal, comparison,
              proposal_hash, extractor, extractor_version
            )
            VALUES(
              $1, $2, $3::memory.candidate_status, $4::jsonb, $5::jsonb,
              $6, $7, $8
            )
            ON CONFLICT (owner_user_id, evidence_id, proposal_hash)
            DO UPDATE SET updated_at=now()
            RETURNING candidate_id, status::text, proposal_hash, applied_claim_id
            """,
            actor,
            evidence_uuid,
            status,
            _stable_json(normalized),
            _stable_json(comparison),
            digest,
            extractor,
            extractor_version,
        )
        return dict(row)


async def review_candidate(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    *,
    candidate_id: str | uuid.UUID,
    expected_proposal_hash: str,
    approve: bool,
    reviewer_ref: str,
    reason: str,
) -> Dict[str, Any]:
    actor = actor_uuid(actor_user_id)
    candidate_uuid = uuid.UUID(str(candidate_id))
    expected = _required_text(
        expected_proposal_hash, "expected_proposal_hash", max_length=128
    )
    reviewer = _required_text(reviewer_ref, "reviewer_ref", max_length=200)
    reason = _required_text(reason, "reason", max_length=1000)

    async with conn.transaction():
        await _set_actor(conn, actor)
        row = await conn.fetchrow(
            """
            SELECT candidate.candidate_id,
                   candidate.status::text,
                   candidate.proposal_hash,
                   candidate.comparison,
                   evidence.status::text AS evidence_status
            FROM memory.candidate AS candidate
            JOIN memory.evidence AS evidence
              ON evidence.owner_user_id=candidate.owner_user_id
             AND evidence.evidence_id=candidate.evidence_id
            WHERE candidate.owner_user_id=$1 AND candidate.candidate_id=$2
            FOR UPDATE OF candidate
            """,
            actor,
            candidate_uuid,
        )
        if not row:
            raise CandidateNotFound("candidate is not visible to the actor")
        if row["proposal_hash"] != expected:
            raise CandidateConflict("candidate proposal hash changed")
        if approve and row["evidence_status"] != "active":
            raise CandidateConflict("candidate evidence is not active")
        if row["status"] not in {"proposed", "review_required", "approved", "rejected"}:
            raise CandidateConflict(f"candidate cannot be reviewed from status {row['status']}")

        target = "approved" if approve else "rejected"
        comparison = _json_object(row["comparison"], "candidate comparison")
        comparison["review"] = {
            "reviewer_ref": reviewer,
            "reason": reason,
            "decision": target,
        }
        updated = await conn.fetchrow(
            """
            UPDATE memory.candidate
            SET status=$3::memory.candidate_status,
                comparison=$4::jsonb,
                review_reason=$5,
                updated_at=now()
            WHERE owner_user_id=$1 AND candidate_id=$2
            RETURNING candidate_id, status::text, proposal_hash
            """,
            actor,
            candidate_uuid,
            target,
            _stable_json(comparison),
            reason,
        )
        return dict(updated)


async def _get_or_create_entity(
    conn: asyncpg.Connection,
    actor: uuid.UUID,
    entity: Dict[str, str],
) -> uuid.UUID:
    row = await conn.fetchrow(
        """
        SELECT entity_id, entity_type
        FROM memory.entity
        WHERE owner_user_id=$1 AND entity_key=$2
        FOR UPDATE
        """,
        actor,
        entity["entity_key"],
    )
    if row:
        if row["entity_type"] != entity["entity_type"]:
            raise ProposalValidationError("entity_key exists with a different entity_type")
        return uuid.UUID(str(row["entity_id"]))

    entity_id = await conn.fetchval(
        """
        INSERT INTO memory.entity(
          owner_user_id, entity_key, entity_type, canonical_name, normalized_name
        ) VALUES($1, $2, $3, $4, $5)
        RETURNING entity_id
        """,
        actor,
        entity["entity_key"],
        entity["entity_type"],
        entity["canonical_name"],
        entity["normalized_name"],
    )
    return uuid.UUID(str(entity_id))


async def _next_revision_number(
    conn: asyncpg.Connection, actor: uuid.UUID, claim_id: uuid.UUID
) -> int:
    return int(
        await conn.fetchval(
            """
            SELECT COALESCE(max(revision_number), 0) + 1
            FROM memory.claim_revision
            WHERE owner_user_id=$1 AND claim_id=$2
            """,
            actor,
            claim_id,
        )
    )


async def _latest_assessment_id(
    conn: asyncpg.Connection, actor: uuid.UUID, claim_id: uuid.UUID
) -> Optional[uuid.UUID]:
    value = await conn.fetchval(
        """
        SELECT assessment_id
        FROM memory.claim_assessment
        WHERE owner_user_id=$1 AND claim_id=$2
        ORDER BY assessed_at DESC, assessment_id DESC
        LIMIT 1
        """,
        actor,
        claim_id,
    )
    return uuid.UUID(str(value)) if value else None


async def _write_revision(
    conn: asyncpg.Connection,
    actor: uuid.UUID,
    claim_id: uuid.UUID,
    *,
    reason: str,
    actor_type: str,
    actor_ref: Optional[str],
) -> int:
    row = await conn.fetchrow(
        """
        SELECT claim_id, subject_entity_id, predicate, object_entity_id, object_literal,
               qualifiers, canonical_text, canonical_key, status::text, confidence, importance,
               salience, sensitivity::text, valid_from, valid_to, last_confirmed_at,
               retrieval_policy, metadata, created_at, updated_at
        FROM memory.claim
        WHERE owner_user_id=$1 AND claim_id=$2
        """,
        actor,
        claim_id,
    )
    if not row:
        raise CandidateConflict("claim disappeared during revision write")
    revision_number = await _next_revision_number(conn, actor, claim_id)
    snapshot = {key: (str(value) if isinstance(value, uuid.UUID) else value) for key, value in dict(row).items()}
    for key, value in list(snapshot.items()):
        if isinstance(value, datetime):
            snapshot[key] = value.isoformat()
        elif hasattr(value, "as_tuple"):
            snapshot[key] = float(value)
    await conn.execute(
        """
        INSERT INTO memory.claim_revision(
          owner_user_id, claim_id, revision_number, snapshot,
          reason, actor_type, actor_ref
        ) VALUES($1, $2, $3, $4::jsonb, $5, $6, $7)
        """,
        actor,
        claim_id,
        revision_number,
        _stable_json(snapshot),
        reason,
        actor_type,
        actor_ref,
    )
    return revision_number


async def _queue_claim_projection(
    conn: asyncpg.Connection,
    actor: uuid.UUID,
    claim_id: uuid.UUID,
    revision_number: int,
) -> None:
    payload = {"claim_id": str(claim_id), "revision_number": revision_number}
    await conn.execute(
        """
        INSERT INTO memory.projection_outbox(
          owner_user_id, aggregate_type, aggregate_id, operation, payload
        ) VALUES($1, 'claim', $2, 'upsert', $3::jsonb)
        ON CONFLICT (owner_user_id, aggregate_type, aggregate_id, operation)
        DO UPDATE SET payload=EXCLUDED.payload,
                      status='pending'::memory.outbox_status,
                      attempts=0,
                      available_at=now(),
                      last_error=NULL,
                      updated_at=now()
        """,
        actor,
        claim_id,
        _stable_json(payload),
    )


async def apply_candidate(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    *,
    candidate_id: str | uuid.UUID,
    expected_proposal_hash: str,
    actor_type: str = "admin",
    actor_ref: Optional[str] = None,
) -> Dict[str, Any]:
    actor = actor_uuid(actor_user_id)
    candidate_uuid = uuid.UUID(str(candidate_id))
    expected = _required_text(
        expected_proposal_hash, "expected_proposal_hash", max_length=128
    )
    if actor_type not in {"user", "system", "job", "admin"}:
        raise ProposalValidationError("invalid actor_type")

    async with conn.transaction():
        await _set_actor(conn, actor)
        candidate = await conn.fetchrow(
            """
            SELECT candidate_id, evidence_id, status::text, proposal,
                   proposal_hash, extractor, extractor_version
            FROM memory.candidate
            WHERE owner_user_id=$1 AND candidate_id=$2
            FOR UPDATE
            """,
            actor,
            candidate_uuid,
        )
        if not candidate:
            raise CandidateNotFound("candidate is not visible to the actor")
        if candidate["proposal_hash"] != expected:
            raise CandidateConflict("candidate proposal hash changed")
        if candidate["status"] != "approved":
            raise CandidateNotApproved(
                f"candidate status must be approved, got {candidate['status']}"
            )

        evidence_status = await conn.fetchval(
            """
            SELECT status::text
            FROM memory.evidence
            WHERE owner_user_id=$1 AND evidence_id=$2
            """,
            actor,
            candidate["evidence_id"],
        )
        if evidence_status != "active":
            raise CandidateConflict("candidate evidence is not active")

        proposal = normalize_proposal(
            _json_object(candidate["proposal"], "candidate proposal")
        )
        valid_from_parameter = _timestamp_parameter(
            proposal["valid_from"],
            "valid_from",
        )
        valid_to_parameter = _timestamp_parameter(
            proposal["valid_to"],
            "valid_to",
        )
        key = canonical_claim_key(proposal)
        subject_id = await _get_or_create_entity(conn, actor, proposal["subject"])
        object_entity_id = None
        if proposal["object_entity"]:
            object_entity_id = await _get_or_create_entity(
                conn, actor, proposal["object_entity"]
            )

        claim = await conn.fetchrow(
            """
            SELECT claim_id, status::text
            FROM memory.claim
            WHERE owner_user_id=$1 AND canonical_key=$2
            FOR UPDATE
            """,
            actor,
            key,
        )
        created = claim is None
        if claim:
            claim_id = uuid.UUID(str(claim["claim_id"]))
            if claim["status"] in {"superseded", "retracted", "quarantined"}:
                raise CandidateConflict(
                    f"existing claim cannot be reused from status {claim['status']}"
                )
            await conn.execute(
                """
                UPDATE memory.claim
                SET status=$3::memory.claim_status,
                    confidence=$4,
                    salience=GREATEST(salience, $5),
                    importance=GREATEST(importance, $6),
                    last_confirmed_at=CASE WHEN $7='supports' THEN now() ELSE last_confirmed_at END,
                    updated_at=now()
                WHERE owner_user_id=$1 AND claim_id=$2
                """,
                actor,
                claim_id,
                proposal["status"],
                proposal["confidence"],
                proposal["salience"],
                proposal["importance"],
                proposal["evidence_stance"],
            )
        else:
            claim_id = uuid.UUID(
                str(
                    await conn.fetchval(
                        """
                        INSERT INTO memory.claim(
                          owner_user_id, subject_entity_id, predicate,
                          object_entity_id, object_literal, qualifiers,
                          canonical_text, canonical_key, status, confidence, importance, salience,
                          sensitivity, valid_from, valid_to, last_confirmed_at,
                          retrieval_policy, metadata
                        ) VALUES(
                          $1, $2, $3, $4, $5::jsonb, $6::jsonb, $7,
                          $8, $9::memory.claim_status, $10, $11, $12,
                          $13::memory.sensitivity_level, $14::timestamptz, $15::timestamptz,
                          CASE WHEN $16='supports' THEN now() ELSE NULL END,
                          $17::jsonb, $18::jsonb
                        )
                        RETURNING claim_id
                        """,
                        actor,
                        subject_id,
                        proposal["predicate"],
                        object_entity_id,
                        _stable_json(proposal["object_literal"])
                        if proposal["object_literal"] is not None
                        else None,
                        _stable_json(proposal["qualifiers"]),
                        proposal["canonical_text"],
                        key,
                        proposal["status"],
                        proposal["confidence"],
                        proposal["importance"],
                        proposal["salience"],
                        proposal["sensitivity"],
                        valid_from_parameter,
                        valid_to_parameter,
                        proposal["evidence_stance"],
                        _stable_json(proposal["retrieval_policy"]),
                        _stable_json(proposal["metadata"]),
                    )
                )
            )

        await conn.execute(
            """
            INSERT INTO memory.claim_evidence(
              owner_user_id, claim_id, evidence_id, stance, relevance, rationale
            ) VALUES($1, $2, $3, $4::memory.evidence_stance, $5, $6)
            ON CONFLICT (owner_user_id, claim_id, evidence_id, stance)
            DO UPDATE SET relevance=GREATEST(memory.claim_evidence.relevance, EXCLUDED.relevance),
                          rationale=COALESCE(EXCLUDED.rationale, memory.claim_evidence.rationale)
            """,
            actor,
            claim_id,
            candidate["evidence_id"],
            proposal["evidence_stance"],
            proposal["evidence_relevance"],
            f"candidate:{candidate_uuid}",
        )

        previous_assessment = await _latest_assessment_id(conn, actor, claim_id)
        await conn.execute(
            """
            INSERT INTO memory.claim_assessment(
              owner_user_id, claim_id, status, support_score, opposition_score,
              confidence, method, method_version, rationale, inputs,
              supersedes_assessment_id, assessed_at
            ) VALUES(
              $1, $2, $3::memory.claim_status, $4, $5,
              $6, $7, $8, $9, $10::jsonb, $11, clock_timestamp()
            )
            """,
            actor,
            claim_id,
            proposal["status"],
            proposal["support_score"],
            proposal["opposition_score"],
            proposal["confidence"],
            proposal["assessment_method"],
            proposal["assessment_method_version"],
            f"applied candidate {candidate_uuid}",
            _stable_json(
                {
                    "candidate_id": str(candidate_uuid),
                    "evidence_id": str(candidate["evidence_id"]),
                    "extractor": candidate["extractor"],
                    "extractor_version": candidate["extractor_version"],
                }
            ),
            previous_assessment,
        )

        superseded_claim_id = None
        if proposal["supersedes_claim_id"]:
            superseded_claim_id = uuid.UUID(proposal["supersedes_claim_id"])
            if superseded_claim_id == claim_id:
                raise CandidateConflict("a claim cannot supersede itself")
            old = await conn.fetchrow(
                """
                SELECT claim_id, status::text, confidence
                FROM memory.claim
                WHERE owner_user_id=$1 AND claim_id=$2
                FOR UPDATE
                """,
                actor,
                superseded_claim_id,
            )
            if not old:
                raise CandidateConflict("superseded claim is not visible to the actor")
            await conn.execute(
                """
                UPDATE memory.claim
                SET status='superseded'::memory.claim_status,
                    updated_at=now()
                WHERE owner_user_id=$1 AND claim_id=$2
                """,
                actor,
                superseded_claim_id,
            )
            old_previous_assessment = await _latest_assessment_id(
                conn, actor, superseded_claim_id
            )
            await conn.execute(
                """
                INSERT INTO memory.claim_assessment(
                  owner_user_id, claim_id, status, confidence,
                  method, method_version, rationale, inputs,
                  supersedes_assessment_id, assessed_at
                ) VALUES(
                  $1, $2, 'superseded'::memory.claim_status, $3,
                  'supersession', 'v1', $4, $5::jsonb, $6, clock_timestamp()
                )
                """,
                actor,
                superseded_claim_id,
                float(old["confidence"]),
                f"superseded by claim {claim_id}",
                _stable_json(
                    {
                        "candidate_id": str(candidate_uuid),
                        "superseding_claim_id": str(claim_id),
                    }
                ),
                old_previous_assessment,
            )
            await conn.execute(
                """
                INSERT INTO memory.claim_relation(
                  owner_user_id, from_claim_id, to_claim_id,
                  relation_type, rationale
                ) VALUES($1, $2, $3, 'supersedes', $4)
                ON CONFLICT DO NOTHING
                """,
                actor,
                claim_id,
                superseded_claim_id,
                f"candidate:{candidate_uuid}",
            )
            old_revision = await _write_revision(
                conn,
                actor,
                superseded_claim_id,
                reason=f"superseded_by:{claim_id}",
                actor_type=actor_type,
                actor_ref=actor_ref,
            )
            await _queue_claim_projection(
                conn, actor, superseded_claim_id, old_revision
            )

        revision_number = await _write_revision(
            conn,
            actor,
            claim_id,
            reason="candidate_applied" if created else "evidence_linked",
            actor_type=actor_type,
            actor_ref=actor_ref,
        )
        await _queue_claim_projection(conn, actor, claim_id, revision_number)

        await conn.execute(
            """
            UPDATE memory.candidate
            SET status='applied'::memory.candidate_status,
                applied_claim_id=$3,
                updated_at=now()
            WHERE owner_user_id=$1 AND candidate_id=$2
            """,
            actor,
            candidate_uuid,
            claim_id,
        )

        return {
            "candidate_id": str(candidate_uuid),
            "claim_id": str(claim_id),
            "created": created,
            "revision_number": revision_number,
            "superseded_claim_id": str(superseded_claim_id)
            if superseded_claim_id
            else None,
            "canonical_key": key,
        }
