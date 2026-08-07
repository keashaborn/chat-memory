from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
from typing import Any, Literal

from rag_engine.memory_v1_contextual_span_splitter_v2 import contextual_spans_v2


CONTRACT_VERSION = "memory_v1_personal_evidence_prefilter_v1"
POLICY_VERSION = "memory_v1_personal_evidence_prefilter_20260805_v1"
TRUSTED_SOURCE_ROLE = "frontend/chat:user"
MAX_INPUT_CHARS = 200_000
MAX_SELECTED_SPANS = 64

Decision = Literal[
    "send_external",
    "skip_zero_call",
    "route_internal",
    "block_local",
    "review_context",
]
AssertionMode = Literal[
    "asserted",
    "negated",
    "corrective",
    "uncertain",
    "planned",
    "endorsed",
    "hypothetical",
    "quoted",
]
SubjectHint = Literal[
    "owner",
    "owner_relationship",
    "attributed_third_party",
    "unknown",
]
Sensitivity = Literal[
    "ordinary",
    "sensitive_self",
    "sensitive_third_party",
    "restricted_identifier",
]

_CATEGORIES = (
    "self_identity_profile",
    "family_relationship",
    "social_relationship",
    "pet",
    "residence_life_history",
    "employment_education",
    "life_event_experience",
    "health_self_report",
    "life_preference",
    "response_preference",
    "belief_stance_idea",
    "goal_plan_intention",
    "project_knowledge_decision",
    "correction_retraction_negation",
)
_REASON_CODES = (
    "pure_general_question",
    "pure_advice_question",
    "task_request_only",
    "social_only",
    "non_user_role",
    "role_spoof",
    "unadopted_quote",
    "hypothetical_nonassertion",
    "no_personal_proposition",
    "empty_or_noise",
    "structured_domain_internal",
    "transient_state_local",
    "context_binding_required",
    "secret_material",
    "direct_identifier_local_only",
    "sensitive_self_local_only",
    "sensitive_third_party_local_only",
    "personal_evidence_selected",
    "gate_error_review_required",
)
POLICY_SHA256 = hashlib.sha256(
    "\n".join((POLICY_VERSION, *_CATEGORIES, *_REASON_CODES)).encode("utf-8")
).hexdigest()

_OWNER_CUE_RE = re.compile(
    r"\b(?:i|i'm|i’ve|i've|i’d|i'd|me|my|mine|we|we're|we’ve|we've|our|ours)\b",
    re.IGNORECASE,
)
_FAMILY_RE = re.compile(
    r"\b(?:mother|mom|father|dad|parent|parents|sister|sisters|brother|brothers|"
    r"wife|husband|spouse|partner|daughter|son|child|children|aunt|uncle|"
    r"grandmother|grandfather|grandparent|cousin|family)\b",
    re.IGNORECASE,
)
_SOCIAL_RE = re.compile(
    r"\b(?:friend|friends|coworker|colleague|neighbor|roommate|boss|client|"
    r"girlfriend|boyfriend|fianc(?:e|é|ée))\b",
    re.IGNORECASE,
)
_PET_RE = re.compile(
    r"\b(?:my|our)\s+(?:dog|cat|pet|puppy|kitten|horse|bird|rabbit)s?\b|"
    r"\b(?:dog|cat|pet|puppy|kitten|horse|bird|rabbit)s?\s+(?:is|was|died|lives?)\b",
    re.IGNORECASE,
)
_BELIEF_RE = re.compile(
    r"\b(?:i\s+(?:think|believe|feel|consider|suspect|figure|view)|"
    r"in\s+my\s+(?:opinion|view)|my\s+belief|my\s+theory|my\s+idea)\b",
    re.IGNORECASE,
)
_IDEA_QUESTION_RE = re.compile(
    r"^\s*what\s+if\s+.+(?:\?|$)",
    re.IGNORECASE | re.DOTALL,
)
_DISCLOSURE_QUESTION_RE = re.compile(
    r"^\s*(?:did\s+i\s+tell\s+you|have\s+i\s+told\s+you|"
    r"did\s+you\s+know\s+that)\b",
    re.IGNORECASE,
)
_PREFERENCE_RE = re.compile(
    r"\b(?:i|we)\s+(?:really\s+)?(?:prefer|like|love|enjoy|hate|dislike|avoid|"
    r"want\s+(?:answers?|responses?)|don't\s+like|do\s+not\s+like)\b",
    re.IGNORECASE,
)
_POSSESSIVE_PREFERENCE_RE = re.compile(
    r"\bmy\s+(?:favorite|favourite|preferred)\b|"
    r"\bmy\s+(?:favorite|favourite)\s+\w+\s+(?:is|was)\b",
    re.IGNORECASE,
)
_RESPONSE_PREFERENCE_RE = re.compile(
    r"(?:\b(?:i|we)\s+(?:prefer|want|like|love|hate|dislike)\b.{0,80}\b"
    r"(?:answers?|responses?|replies|tables?|bullets?|paragraphs?|markdown)\b|"
    r"^\s*(?:please\s+)?(?:answer|respond|reply|call\s+me|address\s+me|"
    r"use\s+(?:tables?|bullets?|paragraphs?)|keep\s+(?:answers?|responses?)|"
    r"(?:do\s+not|don't|never|always)\s+use\s+"
    r"(?:tables?|bullets?|markdown))\b)",
    re.IGNORECASE,
)
_PLAN_RE = re.compile(
    r"\b(?:i|we)\s+(?:plan|intend|hope|aim|expect|want|need|am\s+going|"
    r"are\s+going|would\s+like)\s+to\b|\bmy\s+(?:goal|plan|intention)\b",
    re.IGNORECASE,
)
_PROJECT_RE = re.compile(
    r"\b(?:i|we)\s+(?:am|are|'m|'re)\s+(?:building|creating|designing|working\s+on)|"
    r"\b(?:my|our)\s+(?:project|app|system|business|research|design)\b|"
    r"\b(?:i|we)\s+(?:decided|implemented|changed|built|created)\b",
    re.IGNORECASE,
)
_HEALTH_RE = re.compile(
    r"\b(?:diagnos(?:is|ed)|dementia|diabetes|diabetic|cancer|arthritis|"
    r"depression|anxiety|autism|adhd|injur(?:y|ed)|swollen|swelling|pain|"
    r"surgery|hospital|assisted\s+living|medication|symptom|illness|disease|"
    r"blood\s+pressure|heart\s+attack|stroke|pregnan(?:t|cy))\b",
    re.IGNORECASE,
)
_THIRD_PARTY_RELATION_RE = re.compile(
    r"\b(?:my|our)\s+(?:mother|mom|father|dad|parent|parents|sister|"
    r"brother|wife|husband|spouse|partner|daughter|son|child|children|"
    r"friend|family|relative|relatives)\b",
    re.IGNORECASE,
)
_SENSITIVE_TOPIC_RE = re.compile(
    r"\b(?:dementia|diabetes|diabetic|cancer|arthritis|depression|anxiety|"
    r"autism|adhd|injur(?:y|ed)|surgery|hospital|assisted\s+living|illness|"
    r"disease|heart\s+attack|stroke|died|dead|passed\s+away|pregnan(?:t|cy)|"
    r"assault(?:ed)?|abus(?:e|ed))\b",
    re.IGNORECASE,
)
_EMPLOYMENT_RE = re.compile(
    r"\b(?:i|we)\s+(?:work|worked|am\s+employed|was\s+employed|teach|taught|"
    r"study|studied|attend|attended|graduated)|\bmy\s+(?:job|career|occupation|"
    r"employer|school|college|degree|credential)\b",
    re.IGNORECASE,
)
_RESIDENCE_RE = re.compile(
    r"\b(?:i|we)\s+(?:live|lived|reside|resided|grew\s+up|moved|was\s+born)|"
    r"\bmy\s+(?:home|hometown|address|residence)\b",
    re.IGNORECASE,
)
_IDENTITY_RE = re.compile(
    r"\b(?:my\s+name\s+is|call\s+me|my\s+age\s+is|my\s+birthday\s+is|"
    r"i\s+am\s+\d{1,3}(?:\s+years?\s+old)?|"
    r"i'm\s+\d{1,3}(?:\s+years?\s+old)?)\b",
    re.IGNORECASE,
)
_LIFE_EVENT_RE = re.compile(
    r"\b(?:i|we)\s+(?:went|visited|traveled|travelled|lost|won|met|married|"
    r"divorced|retired|started|stopped|learned|experienced|remember|remembered|"
    r"survived|served|adopted|bought|sold|joined|left|was\s+assaulted|"
    r"was\s+abused)\b",
    re.IGNORECASE,
)
_CORRECTION_RE = re.compile(
    r"\b(?:actually|correction|correct\s+that|i\s+meant|not\s+.+\s+but|"
    r"no\s+longer|used\s+to|formerly|that\s+was\s+wrong)\b",
    re.IGNORECASE | re.DOTALL,
)
_NEGATION_RE = re.compile(
    r"\b(?:not|never|no\s+longer|don't|doesn't|didn't|isn't|wasn't|aren't|weren't)\b",
    re.IGNORECASE,
)
_UNCERTAINTY_RE = re.compile(
    r"\b(?:maybe|might|may|possibly|probably|i\s+guess|i'm\s+not\s+sure|"
    r"i\s+wonder|did\s+i\s+tell\s+you)\b",
    re.IGNORECASE,
)
_HYPOTHETICAL_RE = re.compile(
    r"\bif\s+i\s+(?:said|were|was|had|claimed)\b.{0,160}\b(?:would|could|"
    r"should|remember)\b",
    re.IGNORECASE | re.DOTALL,
)
_PURE_QUESTION_RE = re.compile(
    r"^\s*(?:who|what|when|where|why|how|which|is|are|am|was|were|do|does|"
    r"did|can|could|would|should|will|have|has|had)\b.*\?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_ADVICE_QUESTION_RE = re.compile(
    r"\b(?:best\s+(?:treatment|way)|what\s+should|how\s+(?:do|can|should)|"
    r"medical\s+advice|treat|cure|fix)\b.*\?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_TASK_REQUEST_RE = re.compile(
    r"^\s*(?:please\s+)?(?:write|draft|calculate|search|find|look\s+up|show|"
    r"tell|explain|summarize|translate|generate|make|create|open|close|send)\b",
    re.IGNORECASE,
)
_FIRST_PERSON_TASK_RE = re.compile(
    r"^\s*i\s+(?:need|want|would\s+like)\s+you\s+to\s+"
    r"(?:write|draft|calculate|search|find|look\s+up|show|tell|explain|"
    r"summarize|translate|generate|make|create|open|close|send)\b",
    re.IGNORECASE,
)
_ROLE_SPOOF_RE = re.compile(
    r"^\s*(?:system|assistant|developer|tool)\s*:",
    re.IGNORECASE,
)
_SOCIAL_ONLY_RE = re.compile(
    r"^\s*(?:hi|hello|hey|thanks|thank\s+you|good\s+(?:morning|afternoon|"
    r"evening|night)|how\s+are\s+you)[!?.\s]*$",
    re.IGNORECASE,
)
_STRUCTURED_DOMAIN_RE = re.compile(
    r"^\s*(?:log|record|save|add|delete|update)\b.{0,160}\b(?:calories?|meal|"
    r"workout|exercise|weight|waist|body\s+fat|medication|measurement)\b",
    re.IGNORECASE | re.DOTALL,
)
_TRANSIENT_RE = re.compile(
    r"^\s*(?:i\s+am|i'm)\s+(?:tired|hungry|thirsty|bored|busy|sleepy|cold|"
    r"hot|fine|okay|ok)\s+(?:right\s+now|today|at\s+the\s+moment)?[.!?\s]*$",
    re.IGNORECASE,
)
_CONTEXT_ONLY_CORRECTION_RE = re.compile(
    r"^\s*(?:actually|correction)[:,]?\s*[\w'’\- ]{1,80},?\s+not\s+"
    r"[\w'’\- ]{1,80}[.!?]?\s*$",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"\b(?:password|passcode|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"private[ _-]?key|secret[ _-]?key|recovery[ _-]?code|seed[ _-]?phrase)\b"
    r"\s*(?:is|:|=)\s*\S+",
    re.IGNORECASE,
)
_DIRECT_IDENTIFIER_RE = re.compile(
    r"(?:\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\b\d{3}-\d{2}-\d{4}\b|"
    r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b|"
    r"\b(?:\d[ -]*?){13,19}\b|"
    r"\b\d{1,6}\s+[A-Z0-9.'’\- ]{2,60}\s+"
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|"
    r"court|ct|way)\b)",
    re.IGNORECASE,
)
_UNADOPTED_QUOTE_RE = re.compile(
    r"^\s*(?:the\s+(?:assistant|article|website|model)\s+(?:said|says|wrote)|"
    r"(?:he|she|they)\s+(?:said|wrote))\b",
    re.IGNORECASE,
)
_EXPLICIT_ENDORSEMENT_RE = re.compile(
    r"\b(?:i\s+agree|i\s+endorse|that\s+is\s+also\s+my\s+view|"
    r"this\s+is\s+also\s+my\s+view)\b",
    re.IGNORECASE,
)
_MIXED_QUESTION_TAIL_RE = re.compile(
    r"(?:[—–;,]|\b(?:and|but|so)\b)\s*(?=(?:what|who|when|where|why|how|"
    r"should|can|could|would|do|does|did|is|are)\b.*\?\s*$)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class PersonalEvidenceSpanV1:
    char_start: int
    char_end: int
    content_sha256: str
    category: str
    assertion_mode: AssertionMode
    subject_hint: SubjectHint
    sensitivity: Sensitivity

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PersonalEvidencePrefilterResultV1:
    contract_version: str
    policy_version: str
    policy_sha256: str
    decision: Decision
    reason_codes: tuple[str, ...]
    selected_spans: tuple[PersonalEvidenceSpanV1, ...]

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        value["selected_spans"] = [
            span.public_dict() for span in self.selected_spans
        ]
        return value


def _result(
    decision: Decision,
    *reason_codes: str,
    selected_spans: tuple[PersonalEvidenceSpanV1, ...] = (),
) -> PersonalEvidencePrefilterResultV1:
    if not reason_codes or any(code not in _REASON_CODES for code in reason_codes):
        raise ValueError("personal evidence reason code is invalid")
    return PersonalEvidencePrefilterResultV1(
        contract_version=CONTRACT_VERSION,
        policy_version=POLICY_VERSION,
        policy_sha256=POLICY_SHA256,
        decision=decision,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        selected_spans=selected_spans,
    )


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _candidate_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for span in contextual_spans_v2(text):
        start, end = span.char_start, span.char_end
        content = text[start:end]
        tail = _MIXED_QUESTION_TAIL_RE.search(content)
        if tail and _OWNER_CUE_RE.search(content[: tail.start()]):
            prefix = _trim(text, start, start + tail.start())
            suffix = _trim(text, start + tail.end(), end)
            if prefix:
                ranges.append(prefix)
            if suffix:
                ranges.append(suffix)
        else:
            ranges.append((start, end))
    if len(ranges) > MAX_SELECTED_SPANS:
        raise ValueError("personal evidence source exceeds span limit")
    return ranges


def _category(content: str) -> str | None:
    if _RESPONSE_PREFERENCE_RE.search(content):
        return "response_preference"
    if _CORRECTION_RE.search(content):
        return "correction_retraction_negation"
    if _BELIEF_RE.search(content) or _IDEA_QUESTION_RE.search(content):
        return "belief_stance_idea"
    if _sensitive_third_party(content):
        return "family_relationship"
    if _FAMILY_RE.search(content) and _OWNER_CUE_RE.search(content):
        return "family_relationship"
    if _SOCIAL_RE.search(content) and _OWNER_CUE_RE.search(content):
        return "social_relationship"
    if _PET_RE.search(content):
        return "pet"
    if _HEALTH_RE.search(content) and _OWNER_CUE_RE.search(content):
        return "health_self_report"
    if _EMPLOYMENT_RE.search(content):
        return "employment_education"
    if _RESIDENCE_RE.search(content):
        return "residence_life_history"
    if _PREFERENCE_RE.search(content) or _POSSESSIVE_PREFERENCE_RE.search(content):
        return "life_preference"
    if _PLAN_RE.search(content):
        return "goal_plan_intention"
    if _PROJECT_RE.search(content):
        return "project_knowledge_decision"
    if _LIFE_EVENT_RE.search(content):
        return "life_event_experience"
    if _IDENTITY_RE.search(content):
        return "self_identity_profile"
    return None


def _assertion_mode(content: str, category: str) -> AssertionMode:
    if _CONTEXT_ONLY_CORRECTION_RE.fullmatch(content) or _CORRECTION_RE.search(content):
        return "corrective"
    if _HYPOTHETICAL_RE.search(content):
        return "hypothetical"
    if _UNCERTAINTY_RE.search(content) or _IDEA_QUESTION_RE.search(content):
        return "uncertain"
    if category == "goal_plan_intention":
        return "planned"
    if category in {"belief_stance_idea", "life_preference", "response_preference"}:
        return "endorsed"
    if _NEGATION_RE.search(content):
        return "negated"
    return "asserted"


def _subject_hint(content: str, category: str) -> SubjectHint:
    if category in {"family_relationship", "social_relationship"}:
        if re.search(r"\b(?:says|said|thinks|believes|feels|reports)\b", content, re.IGNORECASE):
            return "attributed_third_party"
        return "owner_relationship"
    if _OWNER_CUE_RE.search(content) or category == "response_preference":
        return "owner"
    return "unknown"


def _selected_span(
    text: str,
    start: int,
    end: int,
    category: str,
    *,
    sensitivity: Sensitivity = "ordinary",
) -> PersonalEvidenceSpanV1:
    content = text[start:end]
    return PersonalEvidenceSpanV1(
        char_start=start,
        char_end=end,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        category=category,
        assertion_mode=_assertion_mode(content, category),
        subject_hint=_subject_hint(content, category),
        sensitivity=sensitivity,
    )


def _sensitive_third_party(content: str) -> bool:
    return bool(
        _THIRD_PARTY_RELATION_RE.search(content)
        and _SENSITIVE_TOPIC_RE.search(content)
    )


def classify_personal_evidence_v1(
    text: str,
    *,
    source_role: str,
) -> PersonalEvidencePrefilterResultV1:
    if source_role != TRUSTED_SOURCE_ROLE:
        return _result("review_context", "non_user_role")
    if not isinstance(text, str) or not text.strip():
        return _result("skip_zero_call", "empty_or_noise")
    if len(text) > MAX_INPUT_CHARS:
        return _result("review_context", "gate_error_review_required")
    if _ROLE_SPOOF_RE.search(text):
        return _result("review_context", "role_spoof")
    if _SECRET_RE.search(text):
        return _result("block_local", "secret_material")
    if _DIRECT_IDENTIFIER_RE.search(text):
        return _result("block_local", "direct_identifier_local_only")
    if _SOCIAL_ONLY_RE.fullmatch(text):
        return _result("skip_zero_call", "social_only")
    if _HYPOTHETICAL_RE.search(text):
        return _result("skip_zero_call", "hypothetical_nonassertion")
    if _STRUCTURED_DOMAIN_RE.search(text):
        return _result("route_internal", "structured_domain_internal")
    if _TRANSIENT_RE.fullmatch(text):
        return _result("route_internal", "transient_state_local")
    if _CONTEXT_ONLY_CORRECTION_RE.fullmatch(text):
        category = "correction_retraction_negation"
        span = _selected_span(text, 0, len(text), category)
        return _result(
            "review_context",
            "context_binding_required",
            selected_spans=(span,),
        )
    if _UNADOPTED_QUOTE_RE.search(text) and not _EXPLICIT_ENDORSEMENT_RE.search(text):
        return _result("review_context", "unadopted_quote")
    if (
        _PURE_QUESTION_RE.fullmatch(text)
        and not _DISCLOSURE_QUESTION_RE.search(text)
        and not _IDEA_QUESTION_RE.search(text)
    ):
        if _ADVICE_QUESTION_RE.search(text):
            return _result("skip_zero_call", "pure_advice_question")
        return _result("skip_zero_call", "pure_general_question")

    try:
        ranges = _candidate_ranges(text)
    except (TypeError, ValueError):
        return _result("review_context", "gate_error_review_required")

    selected: list[PersonalEvidenceSpanV1] = []
    internal_reason: str | None = None
    for start, end in ranges:
        content = text[start:end]
        if _TASK_REQUEST_RE.search(content) or _FIRST_PERSON_TASK_RE.search(content):
            continue
        category = _category(content)
        if category is None:
            continue
        if _sensitive_third_party(content):
            selected.append(
                _selected_span(
                    text,
                    start,
                    end,
                    category,
                    sensitivity="sensitive_third_party",
                )
            )
            internal_reason = "sensitive_third_party_local_only"
            continue
        if category == "health_self_report" or (
            _OWNER_CUE_RE.search(content) and _SENSITIVE_TOPIC_RE.search(content)
        ):
            selected.append(
                _selected_span(
                    text,
                    start,
                    end,
                    category,
                    sensitivity="sensitive_self",
                )
            )
            internal_reason = "sensitive_self_local_only"
            continue
        selected.append(_selected_span(text, start, end, category))

    selected_tuple = tuple(selected)
    if selected_tuple:
        if internal_reason is not None:
            return _result(
                "route_internal",
                internal_reason,
                selected_spans=selected_tuple,
            )
        return _result(
            "send_external",
            "personal_evidence_selected",
            selected_spans=selected_tuple,
        )

    if _ADVICE_QUESTION_RE.search(text) and _PURE_QUESTION_RE.fullmatch(text):
        return _result("skip_zero_call", "pure_advice_question")
    if _PURE_QUESTION_RE.fullmatch(text):
        return _result("skip_zero_call", "pure_general_question")
    if _TASK_REQUEST_RE.search(text) or _FIRST_PERSON_TASK_RE.search(text):
        return _result("skip_zero_call", "task_request_only")
    return _result("skip_zero_call", "no_personal_proposition")


__all__ = [
    "CONTRACT_VERSION",
    "MAX_INPUT_CHARS",
    "MAX_SELECTED_SPANS",
    "POLICY_SHA256",
    "POLICY_VERSION",
    "PersonalEvidencePrefilterResultV1",
    "PersonalEvidenceSpanV1",
    "TRUSTED_SOURCE_ROLE",
    "classify_personal_evidence_v1",
]
