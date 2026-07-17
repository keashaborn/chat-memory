from __future__ import annotations

import hashlib
import json
import math
import struct
import uuid
from typing import Any, Mapping, Sequence


VERSION = "memory_v1_v5_shadow_candidate_v1"


class V5ShadowCandidateError(RuntimeError):
    pass


def _stable_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _vector(values: Sequence[float]) -> list[float]:
    try:
        parsed = [float(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise V5ShadowCandidateError("query vector must contain numbers") from exc
    if not parsed or len(parsed) > 16384:
        raise V5ShadowCandidateError("query vector dimension is invalid")
    if any(not math.isfinite(value) for value in parsed):
        raise V5ShadowCandidateError("query vector contains a non-finite value")
    return parsed


def discover_v5_shadow_candidates(
    index: Any,
    actor_user_id: str | uuid.UUID,
    query_vector: Sequence[float],
    *,
    limit: int = 24,
) -> dict[str, Any]:
    try:
        actor = uuid.UUID(str(actor_user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise V5ShadowCandidateError("actor_user_id must be a UUID") from exc
    if not 1 <= int(limit) <= 100:
        raise V5ShadowCandidateError("limit must be between 1 and 100")
    vector = _vector(query_vector)
    collection = str(getattr(index, "collection_name", "") or "").strip()
    if not collection or len(collection) > 255:
        raise V5ShadowCandidateError("candidate index collection name is invalid")

    raw_hits = index.search_claims(actor, vector, limit=int(limit))
    if not isinstance(raw_hits, list) or len(raw_hits) > int(limit):
        raise V5ShadowCandidateError("candidate index returned an invalid hit set")
    hits: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    prior_score: float | None = None
    for position, hit in enumerate(raw_hits, start=1):
        if not isinstance(hit, Mapping):
            raise V5ShadowCandidateError("candidate hit must be an object")
        try:
            claim_id = uuid.UUID(str(hit.get("claim_id")))
            score = float(hit.get("semantic_score"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise V5ShadowCandidateError("candidate hit contains invalid fields") from exc
        if claim_id in seen:
            raise V5ShadowCandidateError("candidate index returned a duplicate claim")
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise V5ShadowCandidateError("candidate semantic score is invalid")
        if prior_score is not None and score > prior_score:
            raise V5ShadowCandidateError("candidate hits are not rank ordered")
        seen.add(claim_id)
        prior_score = score
        hits.append(
            {
                "rank": position,
                "claim_id": str(claim_id),
                "semantic_score": round(score, 8),
            }
        )

    vector_bytes = struct.pack(f"!{len(vector)}d", *vector)
    vector_sha256 = hashlib.sha256(vector_bytes).hexdigest()
    hash_material = {
        "version": VERSION,
        "owner_user_id": str(actor),
        "collection": collection,
        "query_vector_dimension": len(vector),
        "query_vector_sha256": vector_sha256,
        "candidate_limit": int(limit),
        "candidate_hits": hits,
    }
    return {
        **hash_material,
        "candidate_count": len(hits),
        "candidate_set_sha256": hashlib.sha256(_stable_json(hash_material)).hexdigest(),
        "qdrant_writes": 0,
        "database_writes": 0,
        "prompt_injection": False,
        "answer_model_exposure": False,
        "retrieval_activation": False,
    }
