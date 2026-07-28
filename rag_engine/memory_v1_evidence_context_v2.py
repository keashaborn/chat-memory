from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Sequence

from rag_engine.memory_v1_evidence_context_v1 import (
    EvidenceContextContractError,
    MemoryEvidenceContextEnvelopeV1,
)


CONTRACT_VERSION = "memory_evidence_context_envelope_v2"
CONTEXT_POLICY = (
    "target_assertions_sibling_and_prior_turn_disambiguation_only_v2"
)
MAX_PRIOR_TURNS = 8
MAX_CONTEXT_CHARS = 8000
MAX_TURN_CHARS = 3000


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _uuid_text(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceContextContractError(f"{field} must be a UUID") from exc


def _timestamp(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceContextContractError(
                f"{field} must be an ISO timestamp"
            ) from exc
    else:
        raise EvidenceContextContractError(
            f"{field} must be an ISO timestamp"
        )
    if parsed.tzinfo is None:
        raise EvidenceContextContractError(
            f"{field} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _speaker_role(source: Any) -> Literal["user", "assistant"]:
    if not isinstance(source, str):
        raise EvidenceContextContractError(
            "context source must identify its speaker"
        )
    normalized = source.casefold()
    if normalized.endswith(":user"):
        return "user"
    if normalized.endswith(":assistant"):
        return "assistant"
    raise EvidenceContextContractError(
        "context source speaker is unsupported"
    )


@dataclass(frozen=True)
class PriorTurnContextV2:
    source_id: str
    source_content_sha256: str
    source_recorded_at: str
    request_id_sha256: str
    speaker_role: Literal["user", "assistant"]
    context_distance: int
    content: str
    content_sha256: str
    source_char_start: int
    source_char_end: int
    source_char_count: int
    evidence_use: Literal["disambiguating_context_only"]
    assertion_origin_allowed: Literal[False] = False
    instruction_capability: Literal[False] = False

    @property
    def char_start(self) -> int:
        return self.source_char_start

    @property
    def char_end(self) -> int:
        return self.source_char_end

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_content_sha256": self.source_content_sha256,
            "source_recorded_at": self.source_recorded_at,
            "request_id_sha256": self.request_id_sha256,
            "speaker_role": self.speaker_role,
            "context_distance": self.context_distance,
            "content": self.content,
            "content_sha256": self.content_sha256,
            "source_char_start": self.source_char_start,
            "source_char_end": self.source_char_end,
            "source_char_count": self.source_char_count,
            "evidence_use": self.evidence_use,
            "assertion_origin_allowed": self.assertion_origin_allowed,
            "instruction_capability": self.instruction_capability,
        }


@dataclass(frozen=True)
class MemoryEvidenceContextEnvelopeV2:
    contract_version: Literal["memory_evidence_context_envelope_v2"]
    context_policy: Literal[
        "target_assertions_sibling_and_prior_turn_disambiguation_only_v2"
    ]
    sibling_context: MemoryEvidenceContextEnvelopeV1
    prior_turns: tuple[PriorTurnContextV2, ...]
    envelope_sha256: str

    @property
    def owner_user_id(self) -> str:
        return self.sibling_context.owner_user_id

    @property
    def target_evidence_id(self) -> str:
        return self.sibling_context.target_evidence_id

    @property
    def target_content_sha256(self) -> str:
        return self.sibling_context.target_content_sha256

    @property
    def source(self) -> Any:
        return self.sibling_context.source

    @property
    def spans(self) -> Any:
        return self.sibling_context.spans

    @property
    def allowed_assertion_evidence_ids(self) -> tuple[str, ...]:
        return self.sibling_context.allowed_assertion_evidence_ids

    @property
    def context_only_evidence_ids(self) -> tuple[str, ...]:
        return self.sibling_context.context_only_evidence_ids

    def payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "context_policy": self.context_policy,
            "sibling_context": self.sibling_context.to_dict(),
            "prior_turns": [turn.to_dict() for turn in self.prior_turns],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "envelope_sha256": self.envelope_sha256}

    def validate_hash(self) -> None:
        self.sibling_context.validate_hash()
        if _canonical_json_sha256(self.payload()) != self.envelope_sha256:
            raise EvidenceContextContractError(
                "evidence context v2 envelope hash mismatch"
            )
        if self.allowed_assertion_evidence_ids != (
            self.target_evidence_id,
        ):
            raise EvidenceContextContractError(
                "only target evidence may originate assertions"
            )
        if any(
            turn.assertion_origin_allowed or turn.instruction_capability
            for turn in self.prior_turns
        ):
            raise EvidenceContextContractError(
                "prior turns may not originate assertions or instructions"
            )


def build_memory_evidence_context_envelope_v2(
    *,
    sibling_context: MemoryEvidenceContextEnvelopeV1,
    prior_turn_rows: Sequence[Mapping[str, Any]],
    max_prior_turns: int = 6,
    max_context_chars: int = 6000,
    max_turn_chars: int = 2500,
) -> MemoryEvidenceContextEnvelopeV2:
    sibling_context.validate_hash()
    if not 0 <= max_prior_turns <= MAX_PRIOR_TURNS:
        raise EvidenceContextContractError(
            f"max_prior_turns must be between 0 and {MAX_PRIOR_TURNS}"
        )
    if not 0 <= max_context_chars <= MAX_CONTEXT_CHARS:
        raise EvidenceContextContractError(
            f"max_context_chars must be between 0 and {MAX_CONTEXT_CHARS}"
        )
    if not 1 <= max_turn_chars <= MAX_TURN_CHARS:
        raise EvidenceContextContractError(
            f"max_turn_chars must be between 1 and {MAX_TURN_CHARS}"
        )
    if not isinstance(prior_turn_rows, Sequence) or isinstance(
        prior_turn_rows,
        (str, bytes),
    ):
        raise EvidenceContextContractError(
            "prior_turn_rows must be a sequence"
        )

    owner = sibling_context.owner_user_id
    thread_id = sibling_context.source.thread_id
    target_order = (
        _timestamp(
            sibling_context.source.source_recorded_at,
            "target source recorded_at",
        ),
        sibling_context.source.source_id,
    )
    canonical: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(prior_turn_rows):
        if not isinstance(row, Mapping):
            raise EvidenceContextContractError(
                f"prior_turn_rows[{index}] must be an object"
            )
        source_id = _uuid_text(
            row.get("id"),
            f"prior_turn_rows[{index}].id",
        )
        if source_id in seen_ids:
            raise EvidenceContextContractError(
                "duplicate prior context source"
            )
        seen_ids.add(source_id)
        if (
            _uuid_text(
                row.get("owner_user_id"),
                f"prior_turn_rows[{index}].owner_user_id",
            )
            != owner
        ):
            raise EvidenceContextContractError(
                "prior context owner mismatch"
            )
        if (
            _uuid_text(
                row.get("thread_id"),
                f"prior_turn_rows[{index}].thread_id",
            )
            != thread_id
        ):
            raise EvidenceContextContractError(
                "prior context thread mismatch"
            )
        created_at = _timestamp(
            row.get("created_at"),
            f"prior_turn_rows[{index}].created_at",
        )
        if (created_at, source_id) >= target_order:
            raise EvidenceContextContractError(
                "context row is not prior to target source"
            )
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise EvidenceContextContractError(
                "prior context text must be non-empty"
            )
        canonical.append(
            {
                "source_id": source_id,
                "created_at": created_at,
                "speaker_role": _speaker_role(row.get("source")),
                "request_id": str(row.get("request_id") or ""),
                "text": text,
            }
        )

    canonical.sort(key=lambda item: (item["created_at"], item["source_id"]))
    selected: list[PriorTurnContextV2] = []
    remaining_chars = max_context_chars
    for distance, item in enumerate(reversed(canonical), start=1):
        if len(selected) >= max_prior_turns or remaining_chars <= 0:
            break
        text = item["text"]
        allowed = min(len(text), max_turn_chars, remaining_chars)
        if allowed <= 0:
            break
        char_start = len(text) - allowed
        excerpt = text[char_start:]
        selected.append(
            PriorTurnContextV2(
                source_id=item["source_id"],
                source_content_sha256=_sha256_text(text),
                source_recorded_at=_timestamp_text(item["created_at"]),
                request_id_sha256=_sha256_text(item["request_id"]),
                speaker_role=item["speaker_role"],
                context_distance=distance,
                content=excerpt,
                content_sha256=_sha256_text(excerpt),
                source_char_start=char_start,
                source_char_end=len(text),
                source_char_count=len(text),
                evidence_use="disambiguating_context_only",
            )
        )
        remaining_chars -= allowed
    selected.reverse()

    payload = {
        "contract_version": CONTRACT_VERSION,
        "context_policy": CONTEXT_POLICY,
        "sibling_context": sibling_context.to_dict(),
        "prior_turns": [turn.to_dict() for turn in selected],
    }
    envelope = MemoryEvidenceContextEnvelopeV2(
        contract_version=CONTRACT_VERSION,
        context_policy=CONTEXT_POLICY,
        sibling_context=sibling_context,
        prior_turns=tuple(selected),
        envelope_sha256=_canonical_json_sha256(payload),
    )
    envelope.validate_hash()
    return envelope


def sanitized_evidence_context_report_v2(
    envelope: MemoryEvidenceContextEnvelopeV2,
) -> dict[str, Any]:
    envelope.validate_hash()
    speaker_counts = {"assistant": 0, "user": 0}
    for turn in envelope.prior_turns:
        speaker_counts[turn.speaker_role] += 1
    return {
        "contract_version": envelope.contract_version,
        "context_policy": envelope.context_policy,
        "envelope_sha256": envelope.envelope_sha256,
        "sibling_envelope_sha256": (
            envelope.sibling_context.envelope_sha256
        ),
        "owner_user_id_sha256": _sha256_text(envelope.owner_user_id),
        "target_evidence_id_sha256": _sha256_text(
            envelope.target_evidence_id
        ),
        "target_content_sha256": envelope.target_content_sha256,
        "sibling_span_count": len(envelope.spans),
        "prior_turn_count": len(envelope.prior_turns),
        "prior_turn_char_count": sum(
            len(turn.content) for turn in envelope.prior_turns
        ),
        "prior_turn_speaker_counts": speaker_counts,
        "assertion_origin_count": len(
            envelope.allowed_assertion_evidence_ids
        ),
        "prior_turn_assertion_origin_count": 0,
        "raw_context_text_persisted": False,
        "retrieval_activation": False,
        "prompt_influence": False,
    }
