from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Sequence


CONTRACT_VERSION = "memory_evidence_context_envelope_v1"
SOURCE_SYSTEM = "public.chat_log"
CONTEXT_POLICY = "target_assertions_sibling_disambiguation_only_v1"
SHA256_LENGTH = 64


class EvidenceContextContractError(ValueError):
    """Fail-closed error at the evidence-to-context boundary."""


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


def _sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != SHA256_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise EvidenceContextContractError(
            f"{field} must be a lowercase SHA-256"
        )
    return value


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "text" if allow_empty else "non-empty text"
        raise EvidenceContextContractError(f"{field} must be {qualifier}")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise EvidenceContextContractError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceContextContractError(
            f"{field} must be an integer"
        ) from exc
    if str(parsed) != str(value):
        raise EvidenceContextContractError(
            f"{field} must be a canonical integer"
        )
    return parsed


def _metadata(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise EvidenceContextContractError(
                "evidence metadata must be valid JSON"
            ) from exc
    if not isinstance(value, Mapping):
        raise EvidenceContextContractError(
            "evidence metadata must be an object"
        )
    return value


def _timestamp(value: Any, field: str) -> str:
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
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class EvidenceContextSourceV1:
    source_system: Literal["public.chat_log"]
    source_id: str
    owner_user_id: str
    thread_id: str
    request_id: str
    source_content_sha256: str
    source_recorded_at: str
    source_char_count: int
    raw_source_text_retained: Literal[False] = False
    assertion_origin_allowed: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "source_id": self.source_id,
            "owner_user_id": self.owner_user_id,
            "thread_id": self.thread_id,
            "request_id": self.request_id,
            "source_content_sha256": self.source_content_sha256,
            "source_recorded_at": self.source_recorded_at,
            "source_char_count": self.source_char_count,
            "raw_source_text_retained": self.raw_source_text_retained,
            "assertion_origin_allowed": self.assertion_origin_allowed,
        }


@dataclass(frozen=True)
class EvidenceContextSpanV1:
    evidence_id: str
    content_sha256: str
    char_start: int
    char_end: int
    content: str
    context_role: Literal["before", "target", "after"]
    evidence_use: Literal[
        "target_assertion_source",
        "disambiguating_context_only",
    ]
    assertion_origin_allowed: bool
    primary_lane: str
    epistemic_role: str
    span_origin: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "content_sha256": self.content_sha256,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "content": self.content,
            "context_role": self.context_role,
            "evidence_use": self.evidence_use,
            "assertion_origin_allowed": self.assertion_origin_allowed,
            "primary_lane": self.primary_lane,
            "epistemic_role": self.epistemic_role,
            "span_origin": self.span_origin,
        }


@dataclass(frozen=True)
class MemoryEvidenceContextEnvelopeV1:
    contract_version: Literal["memory_evidence_context_envelope_v1"]
    context_policy: Literal["target_assertions_sibling_disambiguation_only_v1"]
    owner_user_id: str
    target_evidence_id: str
    target_content_sha256: str
    source: EvidenceContextSourceV1
    spans: tuple[EvidenceContextSpanV1, ...]
    allowed_assertion_evidence_ids: tuple[str, ...]
    context_only_evidence_ids: tuple[str, ...]
    envelope_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "context_policy": self.context_policy,
            "owner_user_id": self.owner_user_id,
            "target_evidence_id": self.target_evidence_id,
            "target_content_sha256": self.target_content_sha256,
            "source": self.source.to_dict(),
            "spans": [span.to_dict() for span in self.spans],
            "allowed_assertion_evidence_ids": list(
                self.allowed_assertion_evidence_ids
            ),
            "context_only_evidence_ids": list(
                self.context_only_evidence_ids
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "envelope_sha256": self.envelope_sha256}

    def validate_hash(self) -> None:
        if _canonical_json_sha256(self.payload()) != self.envelope_sha256:
            raise EvidenceContextContractError(
                "evidence context envelope hash mismatch"
            )


def build_memory_evidence_context_envelope_v1(
    *,
    expected_owner_user_id: Any,
    target_evidence_id: Any,
    expected_target_content_sha256: Any,
    source_row: Mapping[str, Any],
    evidence_rows: Sequence[Mapping[str, Any]],
    max_spans: int = 12,
) -> MemoryEvidenceContextEnvelopeV1:
    owner = _uuid_text(expected_owner_user_id, "expected_owner_user_id")
    target_id = _uuid_text(target_evidence_id, "target_evidence_id")
    target_hash = _sha256(
        expected_target_content_sha256,
        "expected_target_content_sha256",
    )
    if not 1 <= max_spans <= 32:
        raise EvidenceContextContractError(
            "max_spans must be between 1 and 32"
        )
    if not isinstance(source_row, Mapping):
        raise EvidenceContextContractError("source_row must be an object")
    if not isinstance(evidence_rows, Sequence) or isinstance(
        evidence_rows,
        (str, bytes),
    ):
        raise EvidenceContextContractError(
            "evidence_rows must be a sequence"
        )
    if not evidence_rows:
        raise EvidenceContextContractError("source has no evidence spans")
    if len(evidence_rows) > max_spans:
        raise EvidenceContextContractError(
            "source exceeds the bounded evidence-context window"
        )

    source_owner = _uuid_text(
        source_row.get("owner_user_id"),
        "source.owner_user_id",
    )
    if source_owner != owner:
        raise EvidenceContextContractError("source owner mismatch")
    source_id = _uuid_text(source_row.get("id"), "source.id")
    source_text = _text(
        source_row.get("text"),
        "source.text",
        allow_empty=True,
    )
    source_hash = _sha256_text(source_text)
    source = EvidenceContextSourceV1(
        source_system=SOURCE_SYSTEM,
        source_id=source_id,
        owner_user_id=owner,
        thread_id=_uuid_text(source_row.get("thread_id"), "source.thread_id"),
        request_id=_uuid_text(
            source_row.get("request_id"),
            "source.request_id",
        ),
        source_content_sha256=source_hash,
        source_recorded_at=_timestamp(
            source_row.get("created_at"),
            "source.created_at",
        ),
        source_char_count=len(source_text),
    )

    canonical_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(evidence_rows):
        if not isinstance(row, Mapping):
            raise EvidenceContextContractError(
                f"evidence_rows[{index}] must be an object"
            )
        evidence_owner = _uuid_text(
            row.get("owner_user_id"),
            f"evidence_rows[{index}].owner_user_id",
        )
        if evidence_owner != owner:
            raise EvidenceContextContractError("evidence owner mismatch")
        if row.get("source_system") != SOURCE_SYSTEM:
            raise EvidenceContextContractError(
                "evidence source system mismatch"
            )
        evidence_id = _uuid_text(
            row.get("evidence_id"),
            f"evidence_rows[{index}].evidence_id",
        )
        if evidence_id in seen_ids:
            raise EvidenceContextContractError("duplicate evidence span")
        seen_ids.add(evidence_id)
        metadata = _metadata(row.get("metadata"))
        metadata_source_id = _uuid_text(
            metadata.get("source_id"),
            f"evidence_rows[{index}].metadata.source_id",
        )
        if metadata_source_id != source_id:
            raise EvidenceContextContractError(
                "evidence source record mismatch"
            )
        metadata_source_hash = _sha256(
            metadata.get("source_content_sha256"),
            (
                f"evidence_rows[{index}].metadata."
                "source_content_sha256"
            ),
        )
        if metadata_source_hash != source_hash:
            raise EvidenceContextContractError(
                "evidence full-source hash mismatch"
            )
        if (
            _uuid_text(
                metadata.get("thread_id"),
                f"evidence_rows[{index}].metadata.thread_id",
            )
            != source.thread_id
            or _uuid_text(
                metadata.get("request_id"),
                f"evidence_rows[{index}].metadata.request_id",
            )
            != source.request_id
        ):
            raise EvidenceContextContractError(
                "evidence conversation binding mismatch"
            )
        char_start = _integer(
            metadata.get("source_char_start"),
            f"evidence_rows[{index}].metadata.source_char_start",
        )
        char_end = _integer(
            metadata.get("source_char_end"),
            f"evidence_rows[{index}].metadata.source_char_end",
        )
        if char_start < 0 or char_end <= char_start or char_end > len(source_text):
            raise EvidenceContextContractError(
                "evidence source offsets are invalid"
            )
        content = _text(
            row.get("content"),
            f"evidence_rows[{index}].content",
        )
        content_hash = _sha256(
            row.get("content_sha256"),
            f"evidence_rows[{index}].content_sha256",
        )
        if _sha256_text(content) != content_hash:
            raise EvidenceContextContractError(
                "evidence content hash mismatch"
            )
        if source_text[char_start:char_end] != content:
            raise EvidenceContextContractError(
                "evidence content does not match its source offsets"
            )
        canonical_rows.append(
            {
                "evidence_id": evidence_id,
                "content_sha256": content_hash,
                "char_start": char_start,
                "char_end": char_end,
                "content": content,
                "primary_lane": _text(
                    metadata.get("primary_lane"),
                    f"evidence_rows[{index}].metadata.primary_lane",
                ),
                "epistemic_role": _text(
                    metadata.get("epistemic_role"),
                    f"evidence_rows[{index}].metadata.epistemic_role",
                ),
                "span_origin": _text(
                    metadata.get("span_origin"),
                    f"evidence_rows[{index}].metadata.span_origin",
                ),
            }
        )

    canonical_rows.sort(
        key=lambda row: (
            row["char_start"],
            row["char_end"],
            row["evidence_id"],
        )
    )
    target_rows = [
        row for row in canonical_rows if row["evidence_id"] == target_id
    ]
    if len(target_rows) != 1:
        raise EvidenceContextContractError(
            "target evidence is absent or ambiguous"
        )
    if target_rows[0]["content_sha256"] != target_hash:
        raise EvidenceContextContractError(
            "target evidence content hash mismatch"
        )

    previous_end = -1
    for row in canonical_rows:
        if row["char_start"] < previous_end:
            raise EvidenceContextContractError(
                "evidence source spans overlap"
            )
        previous_end = row["char_end"]

    target_start = target_rows[0]["char_start"]
    spans: list[EvidenceContextSpanV1] = []
    for row in canonical_rows:
        is_target = row["evidence_id"] == target_id
        role: Literal["before", "target", "after"]
        if is_target:
            role = "target"
        elif row["char_start"] < target_start:
            role = "before"
        else:
            role = "after"
        spans.append(
            EvidenceContextSpanV1(
                evidence_id=row["evidence_id"],
                content_sha256=row["content_sha256"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                content=row["content"],
                context_role=role,
                evidence_use=(
                    "target_assertion_source"
                    if is_target
                    else "disambiguating_context_only"
                ),
                assertion_origin_allowed=is_target,
                primary_lane=row["primary_lane"],
                epistemic_role=row["epistemic_role"],
                span_origin=row["span_origin"],
            )
        )

    allowed = tuple(
        span.evidence_id for span in spans if span.assertion_origin_allowed
    )
    context_only = tuple(
        span.evidence_id for span in spans if not span.assertion_origin_allowed
    )
    if allowed != (target_id,):
        raise EvidenceContextContractError(
            "only the target evidence may originate assertions"
        )
    payload = {
        "contract_version": CONTRACT_VERSION,
        "context_policy": CONTEXT_POLICY,
        "owner_user_id": owner,
        "target_evidence_id": target_id,
        "target_content_sha256": target_hash,
        "source": source.to_dict(),
        "spans": [span.to_dict() for span in spans],
        "allowed_assertion_evidence_ids": list(allowed),
        "context_only_evidence_ids": list(context_only),
    }
    envelope = MemoryEvidenceContextEnvelopeV1(
        contract_version=CONTRACT_VERSION,
        context_policy=CONTEXT_POLICY,
        owner_user_id=owner,
        target_evidence_id=target_id,
        target_content_sha256=target_hash,
        source=source,
        spans=tuple(spans),
        allowed_assertion_evidence_ids=allowed,
        context_only_evidence_ids=context_only,
        envelope_sha256=_canonical_json_sha256(payload),
    )
    envelope.validate_hash()
    return envelope


def sanitized_evidence_context_report_v1(
    envelope: MemoryEvidenceContextEnvelopeV1,
) -> dict[str, Any]:
    envelope.validate_hash()
    lane_counts: dict[str, int] = {}
    for span in envelope.spans:
        lane_counts[span.primary_lane] = lane_counts.get(span.primary_lane, 0) + 1
    target = next(
        span for span in envelope.spans if span.context_role == "target"
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "context_policy": CONTEXT_POLICY,
        "envelope_sha256": envelope.envelope_sha256,
        "owner_user_id_sha256": _sha256_text(envelope.owner_user_id),
        "source_id_sha256": _sha256_text(envelope.source.source_id),
        "target_evidence_id_sha256": _sha256_text(
            envelope.target_evidence_id
        ),
        "target_content_sha256": envelope.target_content_sha256,
        "source_content_sha256": envelope.source.source_content_sha256,
        "source_char_count": envelope.source.source_char_count,
        "span_count": len(envelope.spans),
        "context_only_span_count": len(envelope.context_only_evidence_ids),
        "assertion_origin_count": len(
            envelope.allowed_assertion_evidence_ids
        ),
        "target_offsets": {
            "char_start": target.char_start,
            "char_end": target.char_end,
        },
        "ordered_context_roles": [
            span.context_role for span in envelope.spans
        ],
        "lane_counts": dict(sorted(lane_counts.items())),
        "raw_source_text_retained": False,
        "retrieval_activation": False,
        "prompt_influence": False,
    }
