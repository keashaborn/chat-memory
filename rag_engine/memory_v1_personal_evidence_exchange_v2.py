from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
from typing import Any

from rag_engine.memory_v1_evidence_context_v2 import (
    MemoryEvidenceContextEnvelopeV2,
    PriorTurnContextV2,
)
from rag_engine.memory_v1_personal_evidence_prefilter_v1 import (
    POLICY_SHA256 as MESSAGE_POLICY_SHA256,
    PersonalEvidencePrefilterResultV1,
    PersonalEvidenceSpanV1,
    classify_personal_evidence_v1,
)


CONTRACT_VERSION = "memory_v1_personal_evidence_exchange_v2"
POLICY_VERSION = "memory_v1_personal_evidence_exchange_20260808_v3"
DISPOSITION_RECEIPT_VERSION = (
    "memory_v1_personal_evidence_disposition_receipt_v1"
)
MAX_CONTEXT_TURNS = 8
MAX_CONTEXT_CHARS = 8000
MAX_CONTEXTUAL_ANSWER_CHARS = 512

_EXTERNAL_REASONS = frozenset(
    {
        "contextual_answer_selected",
        "contextual_correction_selected",
        "embedded_personal_proposition_selected",
        "high_recall_owner_authored_candidate",
        "indirect_personal_proposition_selected",
    }
)
_REASON_CODES = tuple(
    sorted(
        {
            "ambiguous_context_fragment",
            "context_binding_required",
            "contextual_answer_selected",
            "contextual_correction_selected",
            "embedded_personal_proposition_selected",
            "gate_error_review_required",
            "high_recall_owner_authored_candidate",
            "indirect_personal_proposition_selected",
            "personal_question_needs_context",
            "unadopted_quote",
            "workflow_noise_zero_call",
        }
    )
)
POLICY_SHA256 = hashlib.sha256(
    "\n".join(
        (POLICY_VERSION, MESSAGE_POLICY_SHA256, *_REASON_CODES)
    ).encode("utf-8")
).hexdigest()

_EMBEDDED_THAT_RE = re.compile(
    r"\bthat\s+(?P<clause>(?:i|we|my|our)\b.+?)\s*\?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_INDIRECT_RESPONSE_PREFERENCE_RE = re.compile(
    r"\b(?:short|concise|brief|detailed|long|structured)\s+"
    r"(?:answers?|responses?|replies)\s+(?:work|works|are|feel|feels)\s+"
    r"(?:best|better|easier|right)\s+for\s+me\b",
    re.IGNORECASE,
)
_INDIRECT_BELIEF_RE = re.compile(
    r"\b.+\s+(?:makes?|seems?|feels?)\s+"
    r"(?:more\s+)?(?:sense|right|true)\s+to\s+me\b",
    re.IGNORECASE | re.DOTALL,
)
_INDIRECT_PLAN_RE = re.compile(
    r"(?:\b(?:next|this)\s+(?:week|month|year)\b.+\b(?:goal|plan)\b|"
    r"\b.+\bis\s+(?:the|my)\s+(?:goal|plan)\b)",
    re.IGNORECASE | re.DOTALL,
)
_CORRECTION_FRAGMENT_RE = re.compile(
    r"^\s*(?:actually\b|correction\b|not\b|no\b.+\binstead\b)",
    re.IGNORECASE | re.DOTALL,
)
_NONANSWER_RE = re.compile(
    r"^\s*(?:i\s+(?:do\s+not|don't)\s+know|not\s+sure|maybe|"
    r"it\s+depends|skip|pass)\b",
    re.IGNORECASE,
)
# Keep this conservative and Unicode-safe without requiring a regex extension.
_SIMPLE_FRAGMENT_RE = re.compile(
    r"^\s*(?:[A-Za-z0-9]|[^\x00-\x7f]).{0,511}$",
    re.DOTALL,
)

_QUESTION_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "response_preference",
        re.compile(
            r"\b(?:answers?|responses?|replies|tables?|bullets?|markdown|"
            r"paragraphs?|call\s+you|address\s+you)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "family_relationship",
        re.compile(
            r"\b(?:mother|mom|father|dad|parents?|sisters?|brothers?|"
            r"wife|husband|spouse|partner|daughters?|sons?|children|family)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "social_relationship",
        re.compile(
            r"\b(?:friends?|coworkers?|colleagues?|neighbors?|roommates?|"
            r"boss|client)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pet",
        re.compile(r"\b(?:pets?|dogs?|cats?|pupp(?:y|ies)|kittens?)\b", re.IGNORECASE),
    ),
    (
        "health_self_report",
        re.compile(
            r"\b(?:health|diagnos(?:is|ed)|dementia|diabetes|cancer|"
            r"depression|anxiety|injur(?:y|ed)|pain|surgery|medication|"
            r"symptoms?|illness|disease|pregnan(?:t|cy))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "life_preference",
        re.compile(r"\b(?:prefer|favorite|favourite|like|love|enjoy|hate|dislike)\b", re.IGNORECASE),
    ),
    (
        "belief_stance_idea",
        re.compile(r"\b(?:believe|belief|view|opinion|position|theory|think)\b", re.IGNORECASE),
    ),
    (
        "goal_plan_intention",
        re.compile(r"\b(?:goal|plan|intend|hope|aim|going\s+to)\b", re.IGNORECASE),
    ),
    (
        "project_knowledge_decision",
        re.compile(r"\b(?:project|app|system|build|building|create|design|decision)\b", re.IGNORECASE),
    ),
    (
        "residence_life_history",
        re.compile(r"\b(?:live|lived|reside|grew\s+up|born|moved|hometown|home)\b", re.IGNORECASE),
    ),
    (
        "employment_education",
        re.compile(r"\b(?:job|work|career|occupation|school|college|degree|study)\b", re.IGNORECASE),
    ),
    (
        "self_identity_profile",
        re.compile(r"\b(?:name|age|birthday|old\s+are\s+you)\b", re.IGNORECASE),
    ),
)
_SECOND_PERSON_RE = re.compile(r"\b(?:you|your|yours)\b", re.IGNORECASE)
_OWNER_QUESTION_RE = re.compile(r"\b(?:i|we|my|our|me|mine|ours)\b", re.IGNORECASE)
_SENSITIVE_RE = re.compile(
    r"\b(?:dementia|diabetes|cancer|depression|anxiety|injur(?:y|ed)|"
    r"surgery|hospital|illness|disease|died|dead|passed\s+away|"
    r"pregnan(?:t|cy)|assault(?:ed)?|abus(?:e|ed))\b",
    re.IGNORECASE,
)
_WORKFLOW_NOISE_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|thanks|thank\s+you|got\s+it|(?:that\s+)?sounds\s+good|"
    r"go\s+ahead|try\s+again|do\s+it)|"
    r"(?:authorized|approved|accepted)(?:\s+as\s+written|\s+to\s+continue)?|"
    r"(?:authorization|approval)\s+(?:accepted|approved)(?:\s+as\s+written)?|"
    r"(?:please\s+)?(?:continue|proceed|resume)(?:\s+(?:with|under)\s+"
    r"(?:the\s+)?authorization(?:\s+as\s+written)?)?|"
    r"(?:please\s+)?(?:run|rerun)\s+(?:the\s+)?(?:tests?|validation|build)|"
    r"(?:please\s+)?(?:commit|deploy|restart|push)(?:\s+(?:it|now))?)"
    r"[.!\s]*$",
    re.IGNORECASE,
)
_ATTRIBUTED_MATERIAL_RE = re.compile(
    r"^\s*(?:(?:he|she|they|the\s+(?:assistant|article|website|model))|"
    r"[A-Z][\w'’\-]{1,63})\s+(?:said|says|wrote|writes|told\s+me)\b|"
    r"^\s*[\"“‘']",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True)
class PersonalEvidenceExchangeResultV2:
    contract_version: str
    policy_version: str
    policy_sha256: str
    decision: str
    reason_codes: tuple[str, ...]
    selected_spans: tuple[PersonalEvidenceSpanV1, ...]
    source_gate_sha256: str
    context_envelope_sha256: str | None
    context_turn_count: int

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        value["selected_spans"] = [
            span.public_dict() for span in self.selected_spans
        ]
        return value

    def disposition_receipt(self) -> dict[str, Any]:
        return content_free_disposition_receipt_v2(self)


def _canonical_sha256(value: Any) -> str:
    import json

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def content_free_disposition_receipt_v2(
    result: PersonalEvidencePrefilterResultV1 | PersonalEvidenceExchangeResultV2,
) -> dict[str, Any]:
    if not isinstance(
        result,
        (PersonalEvidencePrefilterResultV1, PersonalEvidenceExchangeResultV2),
    ):
        raise TypeError("personal evidence disposition type is invalid")
    public = result.public_dict()
    selected_spans = public["selected_spans"]
    receipt = {
        "contract_version": DISPOSITION_RECEIPT_VERSION,
        "context_envelope_sha256": getattr(
            result, "context_envelope_sha256", None
        ),
        "context_turn_count": int(getattr(result, "context_turn_count", 0)),
        "decision": result.decision,
        "gate_contract_version": result.contract_version,
        "gate_result_sha256": _canonical_sha256(public),
        "policy_sha256": result.policy_sha256,
        "policy_version": result.policy_version,
        "reason_codes": list(result.reason_codes),
        "selected_span_count": len(result.selected_spans),
        "selected_spans_sha256": _canonical_sha256(selected_spans),
        "source_gate_sha256": getattr(result, "source_gate_sha256", None),
    }
    return receipt


def _result(
    base: PersonalEvidencePrefilterResultV1,
    *,
    decision: str | None = None,
    reason: str | None = None,
    selected_spans: tuple[PersonalEvidenceSpanV1, ...] | None = None,
    context: MemoryEvidenceContextEnvelopeV2 | None = None,
) -> PersonalEvidenceExchangeResultV2:
    if reason is not None and reason not in _REASON_CODES and reason not in base.reason_codes:
        raise ValueError("exchange eligibility reason code is invalid")
    return PersonalEvidenceExchangeResultV2(
        contract_version=CONTRACT_VERSION,
        policy_version=POLICY_VERSION,
        policy_sha256=POLICY_SHA256,
        decision=decision or base.decision,
        reason_codes=(reason,) if reason else base.reason_codes,
        selected_spans=(
            base.selected_spans if selected_spans is None else selected_spans
        ),
        source_gate_sha256=_canonical_sha256(base.public_dict()),
        context_envelope_sha256=(
            context.envelope_sha256 if context is not None else None
        ),
        context_turn_count=(len(context.prior_turns) if context is not None else 0),
    )


def _translated_spans(
    text: str,
    clause_start: int,
    result: PersonalEvidencePrefilterResultV1,
) -> tuple[PersonalEvidenceSpanV1, ...]:
    translated: list[PersonalEvidenceSpanV1] = []
    for span in result.selected_spans:
        start = clause_start + span.char_start
        end = clause_start + span.char_end
        content = text[start:end]
        translated.append(
            PersonalEvidenceSpanV1(
                char_start=start,
                char_end=end,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                category=span.category,
                assertion_mode=span.assertion_mode,
                subject_hint=span.subject_hint,
                sensitivity=span.sensitivity,
            )
        )
    return tuple(translated)


def _whole_span(
    text: str,
    *,
    category: str,
    assertion_mode: str,
    subject_hint: str,
    sensitivity: str = "ordinary",
) -> PersonalEvidenceSpanV1:
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    content = text[start:end]
    return PersonalEvidenceSpanV1(
        char_start=start,
        char_end=end,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        category=category,
        assertion_mode=assertion_mode,
        subject_hint=subject_hint,
        sensitivity=sensitivity,
    )


def _indirect_span(text: str) -> PersonalEvidenceSpanV1 | None:
    if _INDIRECT_RESPONSE_PREFERENCE_RE.search(text):
        category = "response_preference"
        mode = "endorsed"
    elif _INDIRECT_BELIEF_RE.search(text):
        category = "belief_stance_idea"
        mode = "endorsed"
    elif _INDIRECT_PLAN_RE.search(text):
        category = "goal_plan_intention"
        mode = "planned"
    else:
        return None
    return _whole_span(
        text,
        category=category,
        assertion_mode=mode,
        subject_hint="owner",
    )


def _high_recall_span(text: str) -> PersonalEvidenceSpanV1 | None:
    stripped = text.strip()
    if (
        not stripped
        or _WORKFLOW_NOISE_RE.fullmatch(stripped)
        or _NONANSWER_RE.search(stripped)
    ):
        return None
    words = _WORD_RE.findall(stripped)
    if (
        len(words) < 4
        and len(stripped) < 24
        and not _OWNER_QUESTION_RE.search(stripped)
    ):
        return None
    return _whole_span(
        text,
        category="open_personal_evidence_candidate",
        assertion_mode="asserted",
        subject_hint="unknown",
    )


def _immediate_assistant_question(
    context: MemoryEvidenceContextEnvelopeV2,
) -> PriorTurnContextV2 | None:
    if not context.prior_turns:
        return None
    nearest = min(context.prior_turns, key=lambda turn: turn.context_distance)
    if nearest.context_distance != 1 or nearest.speaker_role != "assistant":
        return None
    if "?" not in nearest.content:
        return None
    return nearest


def _question_category(question: str) -> str | None:
    if not _SECOND_PERSON_RE.search(question):
        return None
    for category, pattern in _QUESTION_CATEGORY_PATTERNS:
        if pattern.search(question):
            return category
    return None


def _contextual_answer_span(
    text: str,
    question: str,
) -> PersonalEvidenceSpanV1 | None:
    if (
        not text.strip()
        or len(text) > MAX_CONTEXTUAL_ANSWER_CHARS
        or "?" in text
        or _NONANSWER_RE.search(text)
        or not _SIMPLE_FRAGMENT_RE.fullmatch(text)
    ):
        return None
    category = _question_category(question)
    if category is None:
        return None
    corrective = bool(_CORRECTION_FRAGMENT_RE.search(text))
    normalized = text.strip().casefold().rstrip(".! ")
    if corrective:
        mode = "corrective"
    elif normalized in {"no", "nope", "never"}:
        mode = "negated"
    elif category in {"life_preference", "response_preference", "belief_stance_idea"}:
        mode = "endorsed"
    elif category == "goal_plan_intention":
        mode = "planned"
    else:
        mode = "asserted"
    relationship = category in {"family_relationship", "social_relationship"}
    sensitivity = "ordinary"
    if _SENSITIVE_RE.search(question):
        sensitivity = "sensitive_third_party" if relationship else "sensitive_self"
    return _whole_span(
        text,
        category=category,
        assertion_mode=mode,
        subject_hint="owner_relationship" if relationship else "owner",
        sensitivity=sensitivity,
    )


def classify_personal_evidence_exchange_v2(
    text: str,
    *,
    source_role: str,
    evidence_context: MemoryEvidenceContextEnvelopeV2 | None = None,
) -> PersonalEvidenceExchangeResultV2:
    base = classify_personal_evidence_v1(text, source_role=source_role)
    if (
        base.decision not in {"block_local", "route_internal"}
        and base.reason_codes != ("non_user_role",)
        and _ATTRIBUTED_MATERIAL_RE.search(text)
    ):
        return _result(
            base,
            decision="review_context",
            reason="unadopted_quote",
            selected_spans=(),
        )
    if base.decision != "skip_zero_call":
        return _result(base)

    if (
        base.reason_codes
        in {("no_personal_proposition",), ("task_request_only",)}
        and _WORKFLOW_NOISE_RE.fullmatch(text)
    ):
        return _result(
            base,
            decision="skip_zero_call",
            reason="workflow_noise_zero_call",
            selected_spans=(),
        )

    embedded = _EMBEDDED_THAT_RE.search(text)
    if embedded is not None:
        clause = embedded.group("clause").rstrip()
        clause_result = classify_personal_evidence_v1(
            clause,
            source_role=source_role,
        )
        if clause_result.selected_spans:
            translated = _translated_spans(text, embedded.start("clause"), clause_result)
            reason = "embedded_personal_proposition_selected"
            return _result(
                base,
                decision=clause_result.decision,
                reason=reason,
                selected_spans=translated,
            )

    indirect = _indirect_span(text)
    if indirect is not None:
        return _result(
            base,
            decision="send_external",
            reason="indirect_personal_proposition_selected",
            selected_spans=(indirect,),
        )

    if evidence_context is not None:
        evidence_context.validate_hash()
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != (
            evidence_context.target_content_sha256
        ):
            raise ValueError("exchange context target hash mismatch")
        if len(evidence_context.prior_turns) > MAX_CONTEXT_TURNS:
            raise ValueError("exchange context turn count exceeds policy")
        if sum(len(turn.content) for turn in evidence_context.prior_turns) > (
            MAX_CONTEXT_CHARS
        ):
            raise ValueError("exchange context content exceeds policy")
        question = _immediate_assistant_question(evidence_context)
        if question is not None:
            span = _contextual_answer_span(text, question.content)
            if span is not None:
                decision = (
                    "route_internal"
                    if span.sensitivity != "ordinary"
                    else "send_external"
                )
                reason = (
                    "contextual_correction_selected"
                    if span.assertion_mode == "corrective"
                    else "contextual_answer_selected"
                )
                return _result(
                    base,
                    decision=decision,
                    reason=reason,
                    selected_spans=(span,),
                    context=evidence_context,
                )
            if _question_category(question.content) is not None:
                return _result(
                    base,
                    decision="review_context",
                    reason="ambiguous_context_fragment",
                    selected_spans=(),
                    context=evidence_context,
                )

    if base.reason_codes == ("no_personal_proposition",):
        high_recall = _high_recall_span(text)
        if high_recall is not None:
            return _result(
                base,
                decision="send_external",
                reason="high_recall_owner_authored_candidate",
                selected_spans=(high_recall,),
            )

    if base.reason_codes == ("no_personal_proposition",) and (
        _CORRECTION_FRAGMENT_RE.search(text)
        or (
            len(text) <= MAX_CONTEXTUAL_ANSWER_CHARS
            and _SIMPLE_FRAGMENT_RE.fullmatch(text)
            and not _NONANSWER_RE.search(text)
        )
    ):
        return _result(base, decision="review_context", reason="context_binding_required")

    if (
        base.reason_codes in {("pure_general_question",), ("pure_advice_question",)}
        and _OWNER_QUESTION_RE.search(text)
        and any(pattern.search(text) for _, pattern in _QUESTION_CATEGORY_PATTERNS)
    ):
        return _result(base, decision="review_context", reason="personal_question_needs_context")

    return _result(base)


def is_external_exchange_reason(reason: str) -> bool:
    return reason in _EXTERNAL_REASONS


__all__ = [
    "CONTRACT_VERSION",
    "DISPOSITION_RECEIPT_VERSION",
    "MAX_CONTEXT_CHARS",
    "MAX_CONTEXT_TURNS",
    "MAX_CONTEXTUAL_ANSWER_CHARS",
    "POLICY_SHA256",
    "POLICY_VERSION",
    "PersonalEvidenceExchangeResultV2",
    "classify_personal_evidence_exchange_v2",
    "content_free_disposition_receipt_v2",
    "is_external_exchange_reason",
]
