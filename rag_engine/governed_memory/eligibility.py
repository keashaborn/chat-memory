from __future__ import annotations

"""Deterministic, zero-side-effect eligibility at the ingestion boundary."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Any, Mapping
from uuid import UUID

from .chat_commands import (
    explicit_preference_correction_command_v1,
)

from .contracts import (
    ContractViolation,
    EligibilityDecision,
    IngestEnvelope,
    SelectedEvidence,
    Sensitivity,
    bridge_source_binding_sha256,
    canonical_sha256,
    require_exact_int,
    require_sha256,
    require_utc,
    require_uuid,
    selection_binding_sha256,
    sha256_text,
)


class EligibilityReason(str, Enum):
    PERSONAL_FACT_SELECTED = "personal_fact_selected"
    EXPLICIT_CORRECTION_COMMAND = "explicit_correction_command"
    EXPLICIT_REMEMBER_SELECTED = "explicit_remember_selected"
    BEFORE_CUTOVER = "before_cutover"
    EMPTY_OR_NOISE = "empty_or_noise"
    SOCIAL_ONLY = "social_only"
    PURE_QUESTION = "pure_question"
    TASK_REQUEST = "task_request"
    WORKFLOW_CONTROL = "workflow_control"
    NO_DURABLE_FACT = "no_durable_fact"
    STRUCTURED_PRODUCT_EVENT = "structured_product_event"
    ASSISTANT_PREFERENCE = "assistant_preference"
    PROJECT_SCOPE_EXCLUDED = "project_scope_excluded"
    SENSITIVE_LOCAL_ONLY = "sensitive_local_only"
    SECRET_MATERIAL = "secret_material"
    ACCOUNT_OR_CARD_MATERIAL = "account_or_card_material"
    DIRECT_IDENTIFIER = "direct_identifier"
    HIGH_ENTROPY_MATERIAL = "high_entropy_material"
    ATTACHMENT_NOT_AUTHORIZED = "attachment_not_authorized"
    NON_USER_SOURCE = "non_user_source"
    ROLE_SPOOF = "role_spoof"
    UNADOPTED_QUOTE = "unadopted_quote"
    CONTEXT_REQUIRED = "context_required"
    INPUT_LIMIT_REVIEW = "input_limit_review"


class EvidenceCategory(str, Enum):
    PERSONAL_FACT = "personal_fact"
    RELATIONSHIP = "relationship"
    PET = "pet"
    RESIDENCE = "residence"
    EMPLOYMENT = "employment"
    LIFE_EVENT = "life_event"
    LIFE_PREFERENCE = "life_preference"
    BELIEF = "belief"
    CORRECTION = "correction"


class AssertionMode(str, Enum):
    ASSERTED = "asserted"
    NEGATED = "negated"
    CORRECTIVE = "corrective"
    UNCERTAIN = "uncertain"
    ENDORSED = "endorsed"


class SubjectHint(str, Enum):
    OWNER = "owner"
    OWNER_RELATIONSHIP = "owner_relationship"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class EligibilityPolicy:
    ingest_after: datetime | None = None
    max_input_characters: int = 16_000
    max_selected_evidence: int = 1
    policy_revision: int = 1

    def __post_init__(self) -> None:
        if self.ingest_after is not None:
            require_utc(self.ingest_after, "invalid_eligibility_cutover")
        require_exact_int(
            self.max_input_characters,
            code="invalid_eligibility_input_limit",
            minimum=1,
            maximum=200_000,
        )
        require_exact_int(
            self.max_selected_evidence,
            code="invalid_eligibility_evidence_limit",
            minimum=1,
            maximum=8,
        )
        require_exact_int(
            self.policy_revision,
            code="invalid_eligibility_policy_revision",
            minimum=1,
        )

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(
            "governed_memory.eligibility_policy",
            {
                "ingest_after": self.ingest_after,
                "max_input_characters": self.max_input_characters,
                "max_selected_evidence": self.max_selected_evidence,
                "policy_revision": self.policy_revision,
                "reason_codes": tuple(reason.value for reason in EligibilityReason),
            },
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EligibilityResult:
    decision: EligibilityDecision
    reason_codes: tuple[EligibilityReason, ...]
    selected_evidence: SelectedEvidence | None
    source_binding_sha256: str
    policy_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, EligibilityDecision):
            raise ContractViolation("invalid_eligibility_decision")
        if not self.reason_codes or any(
            not isinstance(reason, EligibilityReason) for reason in self.reason_codes
        ):
            raise ContractViolation("invalid_eligibility_reasons")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ContractViolation("duplicate_eligibility_reasons")
        if (
            self.decision is EligibilityDecision.SEND_EXTERNAL
        ) != (self.selected_evidence is not None):
            raise ContractViolation("eligibility_selection_mismatch")
        require_sha256(self.source_binding_sha256)
        require_sha256(self.policy_sha256)

    @property
    def provider_allowed(self) -> bool:
        return self.decision is EligibilityDecision.SEND_EXTERNAL

    @property
    def result_sha256(self) -> str:
        return canonical_sha256(
            "governed_memory.eligibility_result",
            {
                "decision": self.decision,
                "reason_codes": self.reason_codes,
                "selected_evidence_sha256": (
                    self.selected_evidence.binding_sha256
                    if self.selected_evidence is not None
                    else None
                ),
                "source_binding_sha256": self.source_binding_sha256,
                "policy_sha256": self.policy_sha256,
            },
        )


_ROLE_SPOOF_RE = re.compile(r"^\s*(?:system|assistant|developer|tool)\s*:", re.I)
_SECRET_KEYWORD_RE = re.compile(
    r"\b(?:passwords?|passcodes?|credentials?|api[ _-]?keys?|access[ _-]?tokens?|"
    r"refresh[ _-]?tokens?|auth(?:entication)?[ _-]?tokens?|private[ _-]?keys?|"
    r"client[ _-]?secrets?|secrets?|recovery[ _-]?(?:codes?|keys?|phrases?)|"
    r"seed[ _-]?phrases?|mnemonics?|one[ _-]?time[ _-]?(?:passwords?|codes?)|"
    r"security[ _-]?answers?|backup[ _-]?codes?|cvv|cvc|pin(?:s|[ _-]?codes?)?)\b",
    re.I,
)
_ACCOUNT_OR_CARD_RE = re.compile(
    r"\b(?:account|routing|iban|swift|bic|wallet|card|credit[ _-]?card|"
    r"debit[ _-]?card)[ _-]?(?:number|identifier|id|address)?s?\b",
    re.I,
)
_DIRECT_IDENTIFIER_RE = re.compile(
    r"(?:\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\b\d{3}-\d{2}-\d{4}\b|"
    r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b|"
    r"\b(?:\d[ -]*?){13,19}\b|"
    r"\b\d{1,6}\s+[A-Z0-9.'’\- ]{2,60}\s+"
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct)\b|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b)",
    re.I,
)
_HIGH_ENTROPY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9_-]{32,}|"
    r"[A-Za-z0-9+/]{28,}={0,2})(?![A-Za-z0-9])"
)
_SOCIAL_RE = re.compile(
    r"^\s*(?:hi|hello|hey|thanks|thank you|good (?:morning|afternoon|evening)|ok(?:ay)?)"
    r"[.!?\s]*$",
    re.I,
)
_WORKFLOW_RE = re.compile(
    r"^\s*(?:continue|proceed|go ahead|run the tests?|commit|deploy|restart|"
    r"authorized as written|try again|that sounds good)[.!\s]*$",
    re.I,
)
_STRUCTURED_RE = re.compile(
    r"^\s*(?:log|record|add|update|delete)\b.{0,180}\b(?:workout|exercise|"
    r"repetitions?|calories?|meal|weight|measurement|medication)\b",
    re.I,
)
_RESPONSE_PREFERENCE_RE = re.compile(
    r"\b(?:answers?|responses?|replies|markdown|tables?|bullets?|paragraphs?)\b",
    re.I,
)
_PROJECT_RE = re.compile(
    r"\b(?:my|our|the)\s+(?:project|app|system)\b|\b(?:building|implementing|deploying)\b",
    re.I,
)
_SENSITIVE_RE = re.compile(
    r"\b(?:diagnos(?:is|ed)|dementia|diabetes|cancer|depression|anxiety|"
    r"pregnan(?:t|cy)|assault(?:ed)?|abus(?:e|ed)|surgery|hospital|died|dead)\b",
    re.I,
)
_UNADOPTED_QUOTE_RE = re.compile(
    r"^\s*(?:[\"“‘']|(?:he|she|they|the assistant|the article)\s+(?:said|wrote))",
    re.I,
)
_HYPOTHETICAL_RE = re.compile(r"\bif\s+i\s+(?:said|were|was|had|claimed)\b", re.I)
_QUESTION_RE = re.compile(
    r"^\s*(?:who|what|when|where|why|how|which|is|are|am|was|were|do|does|"
    r"did|can|could|would|should|will|have|has|had)\b.*\?\s*$",
    re.I | re.S,
)
_TASK_RE = re.compile(
    r"^\s*(?:please\s+)?(?:write|draft|calculate|search|find|show|tell|explain|"
    r"summarize|translate|generate|make|create|open|close|send)\b",
    re.I,
)
_CONTEXT_FRAGMENT_RE = re.compile(
    r"^\s*(?:yes|no|maybe|actually|correction)\b.{0,180}\b(?:that|it|this|still|instead)\b",
    re.I | re.S,
)
_REMEMBER_RE = re.compile(r"^\s*(?:please\s+)?remember(?:\s+that|\s*:)?\s+", re.I)
_ATTACHMENT_REQUEST_RE = re.compile(
    r"^\s*(?:please\s+)?remember\b.{0,80}\bfrom\b.{0,40}\battachment\b",
    re.I,
)
_ALLOWLISTED_FACT_RE = re.compile(
    r"\s*(?:(?:i|we)\s+(?:prefer|like|love|dislike|avoid|live|lived|work|worked|"
    r"own|owned|adopted|moved|believe)\b[^?\n]{1,1000}|"
    r"(?:my|our)\s+(?:favorite|preferred|family|mother|father|sister|brother|spouse|"
    r"partner|dog|cat|pet|job|career|home|hometown|belief|preference)\b"
    r"[^?\n]{1,1000}|synthetic\s+(?:account|owner)\s+prefers\b[^?\n]{1,1000})"
    r"\s*[.!]?\s*",
    re.I | re.S,
)
_PROHIBITED_INGEST_KEYS = frozenset(
    {
        "attachment_content",
        "attachment_text",
        "attachment_raw",
        "raw_attachment",
        "attachment_payload",
        "raw",
        "text",
    }
)
_RELATIONSHIP_RE = re.compile(
    r"\b(?:mother|father|sister|brother|spouse|partner|daughter|son|family)\b",
    re.I,
)
_PET_RE = re.compile(r"\b(?:dog|cat|pet|puppy|kitten|horse|bird)\b", re.I)
_RESIDENCE_RE = re.compile(r"\b(?:live|lived|home|hometown|moved|grew up)\b", re.I)
_EMPLOYMENT_RE = re.compile(r"\b(?:work|worked|job|career|occupation|degree)\b", re.I)
_BELIEF_RE = re.compile(r"\b(?:believe|think|opinion|view|theory)\b", re.I)
_PREFERENCE_RE = re.compile(r"\b(?:prefer|preferred|favorite|like|love|dislike|avoid)\b", re.I)


def external_send_privacy_denial(text: object) -> EligibilityReason | None:
    """Return the closed local-only reason for text that may not leave the backend."""

    if not isinstance(text, str):
        raise ContractViolation("invalid_external_send_text")
    for pattern, reason in (
        (_SECRET_KEYWORD_RE, EligibilityReason.SECRET_MATERIAL),
        (_ACCOUNT_OR_CARD_RE, EligibilityReason.ACCOUNT_OR_CARD_MATERIAL),
        (_DIRECT_IDENTIFIER_RE, EligibilityReason.DIRECT_IDENTIFIER),
        (_HIGH_ENTROPY_RE, EligibilityReason.HIGH_ENTROPY_MATERIAL),
        (_ATTACHMENT_REQUEST_RE, EligibilityReason.ATTACHMENT_NOT_AUTHORIZED),
        (_SENSITIVE_RE, EligibilityReason.SENSITIVE_LOCAL_ONLY),
    ):
        if pattern.search(text):
            return reason
    return None


def context_external_route_denial(text: object) -> EligibilityReason | None:
    """Return non-Memory lanes that bounded context may not bypass."""

    if not isinstance(text, str):
        raise ContractViolation("invalid_context_route_text")
    for pattern, reason in (
        (_STRUCTURED_RE, EligibilityReason.STRUCTURED_PRODUCT_EVENT),
        (_RESPONSE_PREFERENCE_RE, EligibilityReason.ASSISTANT_PREFERENCE),
        (_PROJECT_RE, EligibilityReason.PROJECT_SCOPE_EXCLUDED),
    ):
        if pattern.search(text):
            return reason
    return None


def _parse_uuid(value: object, code: str) -> UUID:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc
    return require_uuid(parsed, code)


def _parse_timestamp(value: object, code: str) -> datetime:
    if isinstance(value, datetime):
        return require_utc(value, code)
    if not isinstance(value, str):
        raise ContractViolation(code)
    try:
        return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), code)
    except ValueError as exc:
        raise ContractViolation(code) from exc


def _coerce_ingest(payload: IngestEnvelope | Mapping[str, Any]) -> IngestEnvelope:
    if isinstance(payload, IngestEnvelope):
        return payload
    if not isinstance(payload, Mapping):
        raise ContractViolation("invalid_ingest_payload")
    if any(key in payload for key in _PROHIBITED_INGEST_KEYS):
        raise ContractViolation("attachment_or_raw_payload_prohibited")
    if "requested_owner_user_id" in payload:
        raise ContractViolation("owner_override_prohibited")
    required = {
        "owner_user_id",
        "thread_id",
        "message_id",
        "role",
        "created_at",
        "content_sha256",
        "content",
        "exchange_id",
        "window_id",
        "window_ordinal",
        "window_sha256",
    }
    allowed = required | {"attachment_ids", "fixture_provenance"}
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise ContractViolation("ingest_payload_missing_field")
    owner = _parse_uuid(payload["owner_user_id"], "invalid_ingest_owner")
    message = _parse_uuid(payload["message_id"], "invalid_ingest_message")
    thread = _parse_uuid(payload["thread_id"], "invalid_ingest_thread")
    exchange = _parse_uuid(payload["exchange_id"], "invalid_ingest_exchange")
    window = _parse_uuid(payload["window_id"], "invalid_ingest_window")
    window_ordinal = require_exact_int(
        payload["window_ordinal"],
        code="invalid_ingest_window_ordinal",
        maximum=10_000_000,
    )
    content = payload["content"]
    if not isinstance(content, str):
        raise ContractViolation("invalid_ingest_content")
    attachment_ids = payload.get("attachment_ids", ())
    if not isinstance(attachment_ids, (list, tuple)):
        raise ContractViolation("invalid_attachment_identifier_list")
    for attachment_id in attachment_ids:
        _parse_uuid(attachment_id, "invalid_attachment_identifier")
    expected_window_sha256 = canonical_sha256(
        "governed_memory.ingest_window",
        {
            "owner_user_id": owner,
            "thread_id": thread,
            "exchange_id": exchange,
            "window_id": window,
            "message_id": message,
            "content_sha256": payload["content_sha256"],
        },
    )
    window_sha256 = require_sha256(
        payload["window_sha256"], "invalid_ingest_window_sha256"
    )
    if window_sha256 != expected_window_sha256:
        raise ContractViolation("ingest_window_sha256_mismatch")
    return IngestEnvelope(
        owner_user_id=owner,
        message_id=message,
        thread_id=thread,
        created_at=_parse_timestamp(payload["created_at"], "invalid_ingest_timestamp"),
        source_role=str(payload["role"]),
        content=content,
        content_sha256=str(payload["content_sha256"]),
        exchange_id=exchange,
        window_id=window,
        window_ordinal=window_ordinal,
        window_sha256=window_sha256,
    )


def _category(text: str) -> EvidenceCategory:
    if _RELATIONSHIP_RE.search(text):
        return EvidenceCategory.RELATIONSHIP
    if _PET_RE.search(text):
        return EvidenceCategory.PET
    if _RESIDENCE_RE.search(text):
        return EvidenceCategory.RESIDENCE
    if _EMPLOYMENT_RE.search(text):
        return EvidenceCategory.EMPLOYMENT
    if _BELIEF_RE.search(text):
        return EvidenceCategory.BELIEF
    if _PREFERENCE_RE.search(text):
        return EvidenceCategory.LIFE_PREFERENCE
    return EvidenceCategory.PERSONAL_FACT


def _message_evidence(
    source: IngestEnvelope,
    *,
    start_character: int,
    end_character: int,
) -> SelectedEvidence:
    prefix = source.content[:start_character].encode("utf-8")
    selected = source.content[start_character:end_character]
    start_utf8 = len(prefix)
    end_utf8 = start_utf8 + len(selected.encode("utf-8"))
    if _RELATIONSHIP_RE.search(selected):
        sensitivity = Sensitivity.SENSITIVE_THIRD_PARTY
    elif _RESIDENCE_RE.search(selected) or _EMPLOYMENT_RE.search(selected):
        sensitivity = Sensitivity.SENSITIVE_SELF
    else:
        sensitivity = Sensitivity.ORDINARY
    return SelectedEvidence(
        source_kind="conversation_message",
        source_message_id=source.message_id,
        source_thread_id=source.thread_id,
        source_window_id=source.window_id,
        char_start=start_utf8,
        char_end=end_utf8,
        content_sha256=sha256_text(selected),
        category=_category(selected).value,
        assertion_mode=(
            AssertionMode.UNCERTAIN.value
            if re.search(r"\b(?:maybe|might|possibly|not sure)\b", selected, re.I)
            else AssertionMode.ENDORSED.value
            if _PREFERENCE_RE.search(selected) or _BELIEF_RE.search(selected)
            else AssertionMode.ASSERTED.value
        ),
        subject_hint=(
            SubjectHint.OWNER_RELATIONSHIP.value
            if _RELATIONSHIP_RE.search(selected) or _PET_RE.search(selected)
            else SubjectHint.OWNER.value
        ),
        sensitivity=sensitivity.value,
    )


def _result(
    source: IngestEnvelope,
    policy: EligibilityPolicy,
    decision: EligibilityDecision,
    reason: EligibilityReason,
    selected: SelectedEvidence | None = None,
) -> EligibilityResult:
    policy_sha256 = policy.policy_sha256
    return EligibilityResult(
        decision=decision,
        reason_codes=(reason,),
        selected_evidence=selected,
        source_binding_sha256=bridge_source_binding_sha256(
            owner_user_id=source.owner_user_id,
            message_id=source.message_id,
            thread_id=source.thread_id,
            exchange_id=source.exchange_id,
            window_id=source.window_id,
            window_ordinal=source.window_ordinal,
            window_sha256=source.window_sha256,
            content_sha256=source.content_sha256,
            policy_sha256=policy_sha256,
            source_created_at=source.created_at,
        ),
        policy_sha256=policy_sha256,
    )


def classify_eligibility(
    payload: IngestEnvelope | Mapping[str, Any],
    *,
    cutover: datetime | str | None = None,
    policy: EligibilityPolicy | None = None,
) -> tuple[IngestEnvelope, EligibilityResult]:
    source = _coerce_ingest(payload)
    resolved_cutover = (
        _parse_timestamp(cutover, "invalid_eligibility_cutover")
        if cutover is not None
        else None
    )
    selected_policy = policy or EligibilityPolicy(ingest_after=resolved_cutover)
    if policy is not None and resolved_cutover is not None:
        if policy.ingest_after != resolved_cutover:
            raise ContractViolation("eligibility_cutover_mismatch")

    if selected_policy.ingest_after is not None and (
        source.created_at < selected_policy.ingest_after
    ):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.SKIP_ZERO_CALL,
            EligibilityReason.BEFORE_CUTOVER,
        )
    if source.source_role != "user":
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.BLOCK_LOCAL,
            EligibilityReason.NON_USER_SOURCE,
        )
    if isinstance(payload, Mapping) and bool(payload.get("attachment_ids", ())):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.BLOCK_LOCAL,
            EligibilityReason.ATTACHMENT_NOT_AUTHORIZED,
        )
    text = source.content
    if not text.strip():
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.SKIP_ZERO_CALL,
            EligibilityReason.EMPTY_OR_NOISE,
        )
    if len(text) > selected_policy.max_input_characters:
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.REVIEW_CONTEXT,
            EligibilityReason.INPUT_LIMIT_REVIEW,
        )
    privacy_denial = external_send_privacy_denial(text)
    if privacy_denial is not None:
        return source, _result(
            source,
            selected_policy,
            (
                EligibilityDecision.ROUTE_INTERNAL
                if privacy_denial is EligibilityReason.SENSITIVE_LOCAL_ONLY
                else EligibilityDecision.BLOCK_LOCAL
            ),
            privacy_denial,
        )
    if _ROLE_SPOOF_RE.search(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.REVIEW_CONTEXT,
            EligibilityReason.ROLE_SPOOF,
        )
    if _SOCIAL_RE.fullmatch(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.SKIP_ZERO_CALL,
            EligibilityReason.SOCIAL_ONLY,
        )
    if _WORKFLOW_RE.fullmatch(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.SKIP_ZERO_CALL,
            EligibilityReason.WORKFLOW_CONTROL,
        )
    if _STRUCTURED_RE.search(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.ROUTE_INTERNAL,
            EligibilityReason.STRUCTURED_PRODUCT_EVENT,
        )
    if _RESPONSE_PREFERENCE_RE.search(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.ROUTE_INTERNAL,
            EligibilityReason.ASSISTANT_PREFERENCE,
        )
    if _PROJECT_RE.search(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.ROUTE_INTERNAL,
            EligibilityReason.PROJECT_SCOPE_EXCLUDED,
        )
    if _SENSITIVE_RE.search(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.ROUTE_INTERNAL,
            EligibilityReason.SENSITIVE_LOCAL_ONLY,
        )
    if _UNADOPTED_QUOTE_RE.search(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.REVIEW_CONTEXT,
            EligibilityReason.UNADOPTED_QUOTE,
        )
    if _CONTEXT_FRAGMENT_RE.search(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.REVIEW_CONTEXT,
            EligibilityReason.CONTEXT_REQUIRED,
        )
    if _HYPOTHETICAL_RE.search(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.SKIP_ZERO_CALL,
            EligibilityReason.NO_DURABLE_FACT,
        )
    if _QUESTION_RE.fullmatch(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.SKIP_ZERO_CALL,
            EligibilityReason.PURE_QUESTION,
        )
    if _TASK_RE.search(text):
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.SKIP_ZERO_CALL,
            EligibilityReason.TASK_REQUEST,
        )

    if explicit_preference_correction_command_v1(text) is not None:
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.SKIP_ZERO_CALL,
            EligibilityReason.EXPLICIT_CORRECTION_COMMAND,
        )

    remember = _REMEMBER_RE.match(text)
    if remember is not None:
        start = remember.end()
        while start < len(text) and text[start].isspace():
            start += 1
        end = len(text.rstrip())
        selected_body = text[start:end]
        if start < end and _ALLOWLISTED_FACT_RE.fullmatch(selected_body):
            selected = _message_evidence(
                source, start_character=start, end_character=end
            )
            return source, _result(
                source,
                selected_policy,
                EligibilityDecision.SEND_EXTERNAL,
                EligibilityReason.EXPLICIT_REMEMBER_SELECTED,
                selected,
            )
    if _ALLOWLISTED_FACT_RE.fullmatch(text):
        start = len(text) - len(text.lstrip())
        end = len(text.rstrip())
        selected = _message_evidence(source, start_character=start, end_character=end)
        return source, _result(
            source,
            selected_policy,
            EligibilityDecision.SEND_EXTERNAL,
            EligibilityReason.PERSONAL_FACT_SELECTED,
            selected,
        )
    return source, _result(
        source,
        selected_policy,
        EligibilityDecision.SKIP_ZERO_CALL,
        EligibilityReason.NO_DURABLE_FACT,
    )


def selected_evidence_mapping(
    source: IngestEnvelope,
    selected: SelectedEvidence | None,
    *,
    context_message_id: UUID | None = None,
    context_sha256: str | None = None,
) -> dict[str, object] | None:
    if selected is None:
        return None
    if selected.source_kind != "conversation_message":
        raise ContractViolation("unsupported_evidence_source_kind")
    if (
        selected.source_message_id != source.message_id
        or selected.source_thread_id != source.thread_id
        or selected.source_window_id != source.window_id
    ):
        raise ContractViolation("selected_evidence_source_binding_mismatch")
    encoded = source.content.encode("utf-8")
    selected_bytes = encoded[selected.char_start : selected.char_end]
    try:
        selected_text = selected_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractViolation("selected_evidence_utf8_boundary") from exc
    if sha256_text(selected_text) != selected.content_sha256:
        raise ContractViolation("selected_evidence_sha256_mismatch")
    binding_sha256 = selection_binding_sha256(
        owner_user_id=source.owner_user_id,
        source_kind=selected.source_kind,
        source_message_id=selected.source_message_id,
        source_thread_id=selected.source_thread_id,
        source_window_id=selected.source_window_id,
        window_sha256=source.window_sha256,
        source_sha256=source.content_sha256,
        selected_sha256=selected.content_sha256,
        start_utf8=selected.char_start,
        end_utf8=selected.char_end,
        context_message_id=context_message_id,
        context_sha256=context_sha256,
    )
    return {
        "owner_user_id": str(source.owner_user_id),
        "source_kind": selected.source_kind,
        "source_message_id": str(selected.source_message_id),
        "source_thread_id": str(selected.source_thread_id),
        "source_window_id": str(selected.source_window_id),
        "start_utf8": selected.char_start,
        "end_utf8": selected.char_end,
        "selected_text": selected_text,
        "selected_sha256": selected.content_sha256,
        "source_sha256": source.content_sha256,
        "window_sha256": source.window_sha256,
        "context_message_id": (
            str(context_message_id) if context_message_id is not None else None
        ),
        "context_sha256": context_sha256,
        "selection_binding_sha256": binding_sha256,
        "category": selected.category,
        "assertion_mode": selected.assertion_mode,
        "subject_hint": selected.subject_hint,
        "sensitivity": selected.sensitivity,
    }


def evaluate_eligibility(
    payload: IngestEnvelope | Mapping[str, Any],
    *,
    cutover: datetime | str | None = None,
    policy: EligibilityPolicy | None = None,
) -> dict[str, object]:
    source, result = classify_eligibility(
        payload,
        cutover=cutover,
        policy=policy,
    )
    selected = selected_evidence_mapping(source, result.selected_evidence)
    lineage_material = {
        "owner_user_id": source.owner_user_id,
        "message_id": source.message_id,
        "thread_id": source.thread_id,
        "created_at": source.created_at,
        "exchange_id": source.exchange_id,
        "window_id": source.window_id,
        "window_ordinal": source.window_ordinal,
    }
    receipt: dict[str, object] = {
        "owner_user_id_sha256": canonical_sha256(
            "governed_memory.owner", str(source.owner_user_id)
        ),
        "message_id": str(source.message_id),
        "thread_id": str(source.thread_id),
        "exchange_id": str(source.exchange_id),
        "window_id": str(source.window_id),
        "window_ordinal": source.window_ordinal,
        "lineage_sha256": canonical_sha256(
            "governed_memory.eligibility_lineage", lineage_material
        ),
        "policy_sha256": result.policy_sha256,
        "decision": result.decision.value,
        "reason_codes": [reason.value for reason in result.reason_codes],
        "external_model_calls": 0,
    }
    if result.provider_allowed:
        receipt.update(
            {
                "window_sha256": source.window_sha256,
                "source_sha256": source.content_sha256,
                "source_binding_sha256": result.source_binding_sha256,
                "selected_evidence_sha256": (
                    result.selected_evidence.binding_sha256
                    if result.selected_evidence is not None
                    else None
                ),
                "selection_binding_sha256": (
                    selected["selection_binding_sha256"]
                    if selected is not None
                    else None
                ),
                "result_sha256": result.result_sha256,
            }
        )
    return {
        "decision": result.decision.value,
        "reason_codes": [reason.value for reason in result.reason_codes],
        "selected_evidence": selected,
        "provider_allowed": result.provider_allowed,
        "source_binding_sha256": result.source_binding_sha256,
        "policy_sha256": result.policy_sha256,
        "receipt": receipt,
    }


__all__ = [
    "AssertionMode",
    "EligibilityPolicy",
    "EligibilityReason",
    "EligibilityResult",
    "EvidenceCategory",
    "Sensitivity",
    "SubjectHint",
    "classify_eligibility",
    "context_external_route_denial",
    "evaluate_eligibility",
    "external_send_privacy_denial",
    "selected_evidence_mapping",
]
