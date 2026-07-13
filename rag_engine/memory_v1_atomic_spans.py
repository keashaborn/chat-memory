from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence

from .memory_v1_evidence_triage import (
    QUESTION_START_RE,
    REQUEST_START_RE,
    TriageDecision,
    TriageSourceRow,
    classify_source,
    sha256_text,
)


ATOMIC_SPAN_NAMESPACE = uuid.UUID("87c95fa5-6fd7-4569-b43c-fcf94313277d")
MAX_ATOMIC_CHARS = 600

BOUNDARY_RE = re.compile(
    r"(?P<punct>[.!?](?:[\"'’”)]*))(?P<space>[ \t]+)"
    r"|(?P<newline>\r?\n+)"
    r"|(?P<tight>[.!?])(?=[A-Za-z])"
)
ABBREVIATIONS = {
    "dr",
    "e.g",
    "etc",
    "i.e",
    "jr",
    "mr",
    "mrs",
    "ms",
    "prof",
    "sr",
    "st",
    "vs",
}
STRUCTURAL_OR_PASTED_RE = re.compile(
    r"^\s*(?:#{1,6}\s|```|(?:[-*+] |\d+[.)] )|"
    r"(?:assistant|chatgpt|system|developer|user)\s*:)",
    re.IGNORECASE,
)
PASTED_DATA_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9_]+\.[a-z][a-z0-9_.]+\b")
USER_WRAPPER_RE = re.compile(
    r"^\s*(?:i (?:am|was) pasting|i['’]m pasting|i agree|i endorse|"
    r"i want this stored|i wanted to provide|here (?:is|are)|here['’]s)\b",
    re.IGNORECASE,
)
FIRST_PERSON_ASSERTION_RE = re.compile(
    r"\b(?:i|i['’]m|i am|i['’]ve|i have|my|we|our)\b",
    re.IGNORECASE,
)
DURABLE_HISTORY_RE = re.compile(
    r"\b(?:i|we) (?:have had|grew|moved|worked|founded|retired|graduated|"
    r"went|drove|spent|lost|got|lived|live|married|started|created|built|"
    r"owned|learned|became|considered|cared|talked|decided|relied)\b|"
    r"\bi['’]ve had\b|"
    r"\bi was (?:born|married|retired|fired|a|an|the|in|at|from|always|"
    r"working|taking|focused)\b|"
    r"\bi had (?:to|a|an|the|two|three|four|five)\b",
    re.IGNORECASE,
)
HISTORICAL_PREFIX_RE = re.compile(
    r"^\s*(?:about .{0,20} ago|when i was|after (?:high school|college)|"
    r"as a child|growing up|back when)\b",
    re.IGNORECASE,
)
PREFERENCE_ASSERTION_RE = re.compile(
    r"\bi (?:also )?(?:like|love|prefer|enjoy|value|care about|don['’]t like|"
    r"do not like|want|would like)\b",
    re.IGNORECASE,
)
BELIEF_ASSERTION_RE = re.compile(
    r"\b(?:in my opinion|probably i)\b|"
    r"\bi (?:do not believe|don['’]t believe|believe|think|suspect|consider|"
    r"see|view|regard|would (?:even )?argue)\b",
    re.IGNORECASE,
)
NAMED_ASSERTION_RE = re.compile(
    r"^\s*(?:(?i:my|our)\s+(?:[\w'’.-]+\s+){0,5}|"
    r"(?i:they|he|she)\s+|[A-ZÀ-ÖØ-ÞА-ЯЁ][\w'’.-]{1,50}\s+)"
    r"(?i:is|are|was|were|had|has|died|lived|worked|looked|became|loved|"
    r"treated|moved|graduated|put|slept|went|got)\b"
)
ASSERTIVE_VERB_RE = re.compile(
    r"\b(?:am|is|are|was|were|have|has|had|did|made|built|created|lost|"
    r"worked|lived|moved|believe|think|want|prefer|like|love|value|consider|"
    r"bought|fixed|started|grew|learned|decided|sold|cared|retired|graduated|"
    r"went|got|put|slept|relied|became)\b",
    re.IGNORECASE,
)
TEMPORARY_ATOMIC_RE = re.compile(
    r"\b(?:today|tonight|this morning|this afternoon|yesterday|right now|"
    r"currently|lately|recently|this week|at the moment|last few days|"
    r"last few weeks)\b",
    re.IGNORECASE,
)
PROFILE_ASSERTION_RE = re.compile(
    r"\bi (?:have always been|was always|am always|consider myself|"
    r"have \d+|have (?:two|three|four|five))\b",
    re.IGNORECASE,
)
TEMPORAL_DURATION_RE = re.compile(
    r"\b(?:for|about) \d+ (?:days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
PROJECT_GOAL_SIGNAL_RE = re.compile(
    r"\b(?:my (?:idea|goal|plan)|memory system|"
    r"verbal sage|life ?switch|sage helper|tracking tools?|want to "
    r"(?:build|create|make|add|integrate)|want (?:this|the|our) app to|"
    r"(?:this|the|our) app should)\b",
    re.IGNORECASE,
)
PROJECT_NEGATION_RE = re.compile(
    r"\b(?:unrelated to|not about|separate from) (?:the )?(?:app|life ?switch|"
    r"verbal sage|memory system|project)\b",
    re.IGNORECASE,
)
SPEECH_TO_TEXT_WENT_TO_RE = re.compile(
    r"\bi want to (?:[A-ZÀ-ÖØ-ÞА-ЯЁ][\w'’.-]+\s+){1,5}"
    r"(?:high school|college|university|institute)\b",
    re.IGNORECASE,
)
COMPOUND_CONNECTOR_RE = re.compile(
    r"\b(?:and|but|so|because|while|although|which|then|whereas)\b",
    re.IGNORECASE,
)
META_DISCOURSE_RE = re.compile(
    r"\b(?:got (?:a little )?off track|i think i said enough|"
    r"i tried to write down|that answers your question|as i said above)\b",
    re.IGNORECASE,
)
INCOMPLETE_END_RE = re.compile(
    r"\b(?:and|but|because|that|to|into|of|the|a|an|if|when)\W*$",
    re.IGNORECASE,
)
FRAGMENT_START_RE = re.compile(
    r"^\s*and\s+\w+ing\b",
    re.IGNORECASE,
)
CONDITIONAL_START_RE = re.compile(
    r"^\s*(?:in my opinion,?\s+)?if\b",
    re.IGNORECASE,
)
EVALUATIVE_ASSERTION_RE = re.compile(
    r"\b(?:nice|lucky|fun|good|bad|better|worse|pretty good|important|"
    r"strange|odd|cool|foreign|successful)\b",
    re.IGNORECASE,
)
UNPUNCTUATED_QUESTION_RE = re.compile(
    r"^\s*(?:(?:and|but|so|well)\s+)?(?:who|what (?:did|do|does|is|are|was|were)|"
    r"when|where|why|how|do you|does|did|can you|could you|would you|"
    r"should|is it|are you|was it|were you|have you|has|will you)\b",
    re.IGNORECASE,
)
COMMON_NON_NAME_SUBJECTS = {
    "and",
    "at",
    "but",
    "during",
    "in",
    "it",
    "only",
    "pain",
    "so",
    "that",
    "the",
    "there",
    "this",
    "vocals",
    "well",
    "yes",
}


class AtomicSpanError(RuntimeError):
    pass


@dataclass(frozen=True)
class AtomicSpan:
    span_id: uuid.UUID
    ordinal: int
    char_start: int
    char_end: int
    content: str
    content_sha256: str
    span_kind: str
    authorship: str
    primary_lane: str
    lanes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    disposition: str
    candidate_target: Optional[str]
    requires_manual_split: bool


@dataclass(frozen=True)
class AtomicSpanPlan:
    source_decision: TriageDecision
    spans: tuple[AtomicSpan, ...]
    source_dump_detected: bool


def _stable_span_id(
    source_id: uuid.UUID,
    source_hash: str,
    char_start: int,
    char_end: int,
    content_hash: str,
) -> uuid.UUID:
    key = f"{source_id}:{source_hash}:{char_start}:{char_end}:{content_hash}"
    return uuid.uuid5(ATOMIC_SPAN_NAMESPACE, key)


def _trimmed_range(text: str, start: int, end: int) -> Optional[tuple[int, int]]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    return start, end


def _is_abbreviation_boundary(text: str, punctuation_end: int) -> bool:
    if punctuation_end <= 0 or text[punctuation_end - 1] != ".":
        return False
    prefix = text[: punctuation_end - 1]
    token_match = re.search(r"([A-Za-z](?:[A-Za-z.]*)?)$", prefix)
    if not token_match:
        return False
    token = token_match.group(1).lower()
    return token in ABBREVIATIONS


def sentence_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for match in BOUNDARY_RE.finditer(text):
        if match.group("newline") is not None:
            end = match.start()
            next_cursor = match.end()
        elif match.group("punct") is not None:
            end = match.end("punct")
            if _is_abbreviation_boundary(text, end):
                continue
            next_cursor = match.end()
        else:
            end = match.end("tight")
            next_cursor = end
        trimmed = _trimmed_range(text, cursor, end)
        if trimmed:
            ranges.append(trimmed)
        cursor = next_cursor
    trimmed = _trimmed_range(text, cursor, len(text))
    if trimmed:
        ranges.append(trimmed)
    return ranges


def _source_gaps_are_whitespace(text: str, ranges: Sequence[tuple[int, int]]) -> bool:
    previous_end = 0
    for start, end in ranges:
        if start < previous_end or not (0 <= start < end <= len(text)):
            return False
        if text[previous_end:start].strip():
            return False
        previous_end = end
    return not text[previous_end:].strip()


def _looks_like_pasted_data(text: str) -> bool:
    if STRUCTURAL_OR_PASTED_RE.search(text):
        return True
    if len(PASTED_DATA_TOKEN_RE.findall(text)) >= 2:
        return True
    if text.count("_") >= 5 and len(text.split()) >= 5:
        return True
    if " · " in text and re.search(r"\b(?:allowed|denied|roles?|permissions?)\b", text, re.IGNORECASE):
        return True
    return False


def _looks_like_source_dump(text: str, ranges: Sequence[tuple[int, int]]) -> bool:
    if len(ranges) < 20:
        return False
    average_span_chars = len(text) / len(ranges)
    machine_like = 0
    for start, end in ranges:
        span = text[start:end]
        if (
            PASTED_DATA_TOKEN_RE.search(span)
            or span.count("_") >= 2
            or re.search(r"\b(?:allowed|denied|roles?|permissions?)\b", span, re.IGNORECASE)
        ):
            machine_like += 1
    return average_span_chars < 45 or machine_like / len(ranges) >= 0.45


def _authorship_for_span(
    parent: TriageDecision,
    text: str,
    *,
    source_dump_detected: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if source_dump_detected:
        return "unknown_or_quoted", ["source_level_structured_dump_signal"]
    if parent.content_authorship == "mixed_or_quoted":
        if USER_WRAPPER_RE.search(text):
            return "submission_wrapper", ["explicit_user_submission_wrapper"]
        return "unknown_or_quoted", ["parent_turn_marked_mixed_or_quoted"]
    if _looks_like_pasted_data(text):
        return "unknown_or_quoted", ["structural_or_data_dump_signal"]
    return "user", reasons


def _question_kind(text: str) -> Optional[str]:
    stripped = text.strip()
    if not stripped:
        return None
    normalized = re.sub(r"^\s*(?:and|but|so|well)\s+", "", stripped, flags=re.IGNORECASE)
    starts_as_question = bool(
        QUESTION_START_RE.search(normalized)
        or UNPUNCTUATED_QUESTION_RE.search(stripped)
    )
    if "?" not in stripped:
        return "question" if starts_as_question else None
    if starts_as_question or stripped.endswith("?"):
        if FIRST_PERSON_ASSERTION_RE.search(stripped) and not starts_as_question:
            return "mixed_assertion_question"
        return "question"
    return "mixed_assertion_question"


def _isolated_decision(parent: TriageDecision, text: str) -> TriageDecision:
    source = parent.source
    isolated = TriageSourceRow(
        source_id=source.source_id,
        owner_user_id=source.owner_user_id,
        source=source.source,
        text=text,
        created_at=source.created_at,
        thread_id=source.thread_id,
        vantage_id=source.vantage_id,
        request_id=source.request_id,
    )
    return classify_source(isolated, artifact_entries={}, existing_evidence={})


def _has_incompatible_atomic_lanes(lanes: Sequence[str]) -> bool:
    substantive = {
        lane
        for lane in lanes
        if lane not in {"temporary_context", "recall_query", "general_conversation"}
    }
    if len(substantive) <= 1:
        return False
    if substantive <= {"response_preference", "preference_or_goal"}:
        return False
    if substantive <= {"technical_project", "preference_or_goal"}:
        return False
    if substantive <= {
        "response_preference",
        "preference_or_goal",
        "technical_project",
    }:
        return False
    if "structured_data_reference" in substantive:
        return False
    return True


def _looks_compound_assertion(text: str) -> bool:
    connectors = len(COMPOUND_CONNECTOR_RE.findall(text))
    assertive_verbs = len(ASSERTIVE_VERB_RE.findall(text))
    preference_cues = len(PREFERENCE_ASSERTION_RE.findall(text))
    first_person_cues = len(
        re.findall(r"\b(?:i|my|we|our)\b", text, re.IGNORECASE)
    )
    repeated_subjects = (
        len(re.findall(r"\bmy\b", text, re.IGNORECASE)) >= 2
        and len(re.findall(r"\b(?:is|was|has|had)\b", text, re.IGNORECASE)) >= 2
    )
    mixed_evaluation = bool(EVALUATIVE_ASSERTION_RE.search(text)) and bool(
        re.search(
            r"\b(?:and|but|so|because|before|after|while|then|which)\b",
            text,
            re.IGNORECASE,
        )
    )
    return (
        repeated_subjects
        or preference_cues >= 2
        or (
            SPEECH_TO_TEXT_WENT_TO_RE.search(text) is not None
            and connectors >= 1
            and assertive_verbs >= 2
        )
        or (mixed_evaluation and assertive_verbs >= 2)
        or (
            preference_cues >= 1
            and connectors >= 1
            and assertive_verbs >= 2
            and len(text) > 80
        )
        or (len(text) > 110 and connectors >= 2)
        or (len(text) > 170 and connectors >= 1 and assertive_verbs >= 2)
        or (assertive_verbs >= 3 and connectors >= 1)
        or (first_person_cues >= 3 and assertive_verbs >= 2)
    )


def _looks_like_conditional_fragment(text: str) -> bool:
    if not CONDITIONAL_START_RE.search(text):
        return False
    normalized = re.sub(
        r"^\s*in my opinion,?\s+",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )
    if "," in normalized or re.search(r"\bthen\b", normalized, re.IGNORECASE):
        return False
    if not re.match(r"^if\s+i\b", normalized, re.IGNORECASE):
        return False
    return len(re.findall(r"\bi\b", normalized, re.IGNORECASE)) <= 1


def _is_named_assertion(text: str) -> bool:
    if not NAMED_ASSERTION_RE.search(text):
        return False
    first = re.search(r"[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё]+", text)
    if not first:
        return False
    token = first.group(0).lower()
    if token in {"my", "our", "they", "he", "she"}:
        return True
    return token not in COMMON_NON_NAME_SUBJECTS


def _classify_atomic_text(
    parent: TriageDecision,
    text: str,
    *,
    source_dump_detected: bool,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...], str, Optional[str], bool]:
    authorship, authorship_reasons = _authorship_for_span(
        parent,
        text,
        source_dump_detected=source_dump_detected,
    )
    if authorship == "unknown_or_quoted":
        return (
            "quoted_or_pasted",
            "quoted_or_pasted",
            ("quoted_or_pasted",),
            tuple(authorship_reasons),
            "exclude_quoted_or_pasted",
            None,
            False,
        )
    if authorship == "submission_wrapper":
        return (
            "submission_wrapper",
            "submission_wrapper",
            ("submission_wrapper",),
            tuple(authorship_reasons),
            "retain_context_only",
            None,
            False,
        )

    question_kind = _question_kind(text)
    if question_kind == "question":
        return (
            "question",
            "recall_query" if re.search(r"\b(?:remember|recall|what did i tell you)\b", text, re.IGNORECASE) else "question",
            ("question",),
            ("question_is_not_a_new_assertion",),
            "exclude_question",
            None,
            False,
        )
    if question_kind == "mixed_assertion_question":
        return (
            "mixed_assertion_question",
            "mixed",
            ("mixed_assertion_question",),
            ("assertion_and_question_share_one_span",),
            "manual_split_required",
            None,
            True,
        )

    if META_DISCOURSE_RE.search(text):
        return (
            "meta_conversation",
            "general_conversation",
            ("general_conversation",),
            ("meta_discourse_not_durable_memory",),
            "retain_context_only",
            None,
            False,
        )
    if (
        (len(text) > 25 and INCOMPLETE_END_RE.search(text))
        or FRAGMENT_START_RE.search(text)
        or _looks_like_conditional_fragment(text)
    ):
        return (
            "fragment",
            "fragment",
            ("fragment",),
            ("incomplete_or_conditional_fragment",),
            "retain_context_only",
            None,
            False,
        )

    isolated = _isolated_decision(parent, text)
    reasons = list(isolated.reason_codes)
    lanes = list(isolated.lanes)

    potential_assertion = (
        isolated.primary_lane
        in {
            "explicit_correction",
            "response_preference",
            "personal_history",
            "technical_project",
            "preference_or_goal",
        }
        or bool(
            BELIEF_ASSERTION_RE.search(text)
            or PREFERENCE_ASSERTION_RE.search(text)
            or DURABLE_HISTORY_RE.search(text)
            or HISTORICAL_PREFIX_RE.search(text)
            or PROFILE_ASSERTION_RE.search(text)
            or _is_named_assertion(text)
        )
    )
    too_long = len(text) > MAX_ATOMIC_CHARS
    incompatible_lanes = _has_incompatible_atomic_lanes(isolated.lanes)
    compound_shape = potential_assertion and _looks_compound_assertion(text)
    if too_long or incompatible_lanes or compound_shape:
        if too_long:
            reasons.append("span_exceeds_atomic_character_limit")
        if incompatible_lanes:
            reasons.append("span_contains_incompatible_candidate_lanes")
        if compound_shape:
            reasons.append("span_contains_multiple_clause_assertions")
        return (
            "compound_assertion",
            isolated.primary_lane,
            tuple(lanes),
            tuple(dict.fromkeys(reasons)),
            "manual_split_required",
            None,
            True,
        )

    if isolated.primary_lane == "response_preference":
        return (
            "response_policy",
            isolated.primary_lane,
            tuple(lanes),
            tuple(reasons),
            "review_preference_span",
            "preference",
            False,
        )
    if REQUEST_START_RE.search(text):
        return (
            "request",
            "request",
            ("request",),
            tuple(dict.fromkeys(reasons + ["request_is_not_a_new_assertion"])),
            "exclude_request",
            None,
            False,
        )
    if isolated.primary_lane == "structured_data_reference":
        return (
            "structured_state_or_request",
            isolated.primary_lane,
            tuple(lanes),
            tuple(reasons),
            "route_structured_adapter",
            None,
            False,
        )
    if isolated.primary_lane == "temporary_context" or TEMPORARY_ATOMIC_RE.search(text):
        return (
            "temporary_state",
            "temporary_context",
            tuple(dict.fromkeys(lanes + ["temporary_context"])),
            tuple(dict.fromkeys(reasons + ["time_bounded_span"])),
            "retain_temporary_evidence",
            None,
            False,
        )
    if isolated.primary_lane == "explicit_correction":
        return (
            "correction_assertion",
            isolated.primary_lane,
            tuple(lanes),
            tuple(reasons),
            "review_claim_span",
            "claim",
            False,
        )
    if PROFILE_ASSERTION_RE.search(text):
        return (
            "biographical_assertion",
            "contextual_personal_history",
            ("contextual_personal_history",),
            ("stable_self_description_shape",),
            "review_claim_span",
            "claim",
            False,
        )
    if BELIEF_ASSERTION_RE.search(text):
        if parent.primary_lane == "response_preference":
            return (
                "response_policy_context",
                "response_preference_context",
                ("response_preference_context",),
                ("non_policy_belief_inside_response_policy_turn",),
                "retain_context_only",
                None,
                False,
            )
        if PROJECT_GOAL_SIGNAL_RE.search(text) or (
            isolated.primary_lane == "technical_project"
            and not PROJECT_NEGATION_RE.search(text)
        ):
            return (
                "project_viewpoint_assertion",
                "contextual_project",
                ("contextual_project",),
                ("first_person_viewpoint_in_project_statement",),
                "review_project_span",
                "project_knowledge",
                False,
            )
        if parent.primary_lane == "personal_history" and DURABLE_HISTORY_RE.search(text):
            return (
                "uncertain_biographical_assertion",
                "contextual_personal_history",
                ("contextual_personal_history",),
                ("hedged_personal_history_requires_uncertainty_qualifier",),
                "review_claim_span",
                "claim",
                False,
            )
        return (
            "user_belief_or_opinion",
            "user_viewpoint",
            ("user_viewpoint",),
            ("store_as_user_belief_not_external_fact",),
            "review_belief_span",
            "claim",
            False,
        )
    if SPEECH_TO_TEXT_WENT_TO_RE.search(text) and parent.primary_lane == "personal_history":
        return (
            "biographical_assertion",
            "contextual_personal_history",
            ("contextual_personal_history",),
            ("possible_voice_transcription_of_went_to_school",),
            "review_claim_span",
            "claim",
            False,
        )
    if PREFERENCE_ASSERTION_RE.search(text) and isolated.primary_lane != "response_preference":
        project_goal = bool(PROJECT_GOAL_SIGNAL_RE.search(text))
        return (
            "project_goal_assertion" if project_goal else "preference_assertion",
            "contextual_project" if project_goal else "contextual_preference",
            ("contextual_project",) if project_goal else ("contextual_preference",),
            ("deterministic_project_goal_shape",)
            if project_goal
            else ("deterministic_first_person_preference_shape",),
            "review_project_span" if project_goal else "review_preference_span",
            "project_knowledge" if project_goal else "preference",
            False,
        )
    if isolated.primary_lane == "personal_history":
        if EVALUATIVE_ASSERTION_RE.search(text):
            return (
                "user_belief_or_opinion",
                "user_viewpoint",
                ("user_viewpoint",),
                ("subjective_personal_assessment_not_external_fact",),
                "review_belief_span",
                "claim",
                False,
            )
        return (
            "biographical_assertion",
            isolated.primary_lane,
            tuple(lanes),
            tuple(reasons),
            "review_claim_span",
            "claim",
            False,
        )
    if isolated.primary_lane == "technical_project":
        return (
            "project_assertion",
            isolated.primary_lane,
            tuple(lanes),
            tuple(reasons),
            "review_project_span",
            "project_knowledge",
            False,
        )
    if isolated.primary_lane == "preference_or_goal":
        target = (
            "project_knowledge"
            if PROJECT_GOAL_SIGNAL_RE.search(text)
            else "preference"
        )
        disposition = "review_project_span" if target == "project_knowledge" else "review_preference_span"
        kind = "project_goal_assertion" if target == "project_knowledge" else "preference_assertion"
        return (
            kind,
            isolated.primary_lane,
            tuple(lanes),
            tuple(reasons),
            disposition,
            target,
            False,
        )

    word_count = len(re.findall(r"\b\w+\b", text, re.UNICODE))
    if word_count < 4:
        return (
            "fragment",
            "fragment",
            ("fragment",),
            ("insufficient_atomic_assertion_structure",),
            "retain_context_only",
            None,
            False,
        )
    if (
        DURABLE_HISTORY_RE.search(text)
        or HISTORICAL_PREFIX_RE.search(text)
        or PROFILE_ASSERTION_RE.search(text)
        or (
            FIRST_PERSON_ASSERTION_RE.search(text)
            and TEMPORAL_DURATION_RE.search(text)
        )
    ):
        return (
            "biographical_assertion",
            "contextual_personal_history",
            ("contextual_personal_history",),
            ("deterministic_first_person_history_shape",),
            "review_claim_span",
            "claim",
            False,
        )
    if parent.primary_lane == "personal_history" and (
        _is_named_assertion(text)
    ):
        if EVALUATIVE_ASSERTION_RE.search(text):
            return (
                "contextual_user_evaluation",
                "user_viewpoint",
                ("user_viewpoint",),
                ("evaluative_language_not_external_fact",),
                "review_belief_span",
                "claim",
                False,
            )
        return (
            "contextual_personal_assertion",
            "contextual_personal_history",
            ("contextual_personal_history",),
            ("inherited_review_context_from_personal_history_turn",),
            "review_claim_span",
            "claim",
            False,
        )
    return (
        "context",
        "general_conversation",
        ("general_conversation",),
        ("no_atomic_candidate_signal",),
        "retain_context_only",
        None,
        False,
    )


def build_atomic_span_plan(decision: TriageDecision) -> AtomicSpanPlan:
    if decision.candidate_action != "split_before_candidate":
        raise AtomicSpanError(
            f"source {decision.source.source_id} is not split-required: {decision.candidate_action}"
        )
    source = decision.source
    if sha256_text(source.text) != decision.content_sha256:
        raise AtomicSpanError(f"source hash changed for {source.source_id}")
    ranges = sentence_ranges(source.text)
    if not ranges or not _source_gaps_are_whitespace(source.text, ranges):
        raise AtomicSpanError(f"invalid source coverage for {source.source_id}")
    source_dump_detected = _looks_like_source_dump(source.text, ranges)

    spans: list[AtomicSpan] = []
    for ordinal, (start, end) in enumerate(ranges):
        content = source.text[start:end]
        content_hash = sha256_text(content)
        (
            span_kind,
            primary_lane,
            lanes,
            reasons,
            disposition,
            candidate_target,
            requires_manual_split,
        ) = _classify_atomic_text(
            decision,
            content,
            source_dump_detected=source_dump_detected,
        )
        authorship, _ = _authorship_for_span(
            decision,
            content,
            source_dump_detected=source_dump_detected,
        )
        spans.append(
            AtomicSpan(
                span_id=_stable_span_id(
                    source.source_id,
                    decision.content_sha256,
                    start,
                    end,
                    content_hash,
                ),
                ordinal=ordinal,
                char_start=start,
                char_end=end,
                content=content,
                content_sha256=content_hash,
                span_kind=span_kind,
                authorship=authorship,
                primary_lane=primary_lane,
                lanes=lanes,
                reason_codes=reasons,
                disposition=disposition,
                candidate_target=candidate_target,
                requires_manual_split=requires_manual_split,
            )
        )

    previous_end = 0
    for span in spans:
        if span.char_start < previous_end:
            raise AtomicSpanError(f"overlapping spans for {source.source_id}")
        if source.text[previous_end : span.char_start].strip():
            raise AtomicSpanError(f"uncovered source text for {source.source_id}")
        if source.text[span.char_start : span.char_end] != span.content:
            raise AtomicSpanError(f"span/source mismatch for {span.span_id}")
        if sha256_text(span.content) != span.content_sha256:
            raise AtomicSpanError(f"span hash mismatch for {span.span_id}")
        previous_end = span.char_end
    if source.text[previous_end:].strip():
        raise AtomicSpanError(f"uncovered trailing source text for {source.source_id}")
    return AtomicSpanPlan(
        source_decision=decision,
        spans=tuple(spans),
        source_dump_detected=source_dump_detected,
    )


def build_atomic_span_plans(
    decisions: Sequence[TriageDecision],
) -> list[AtomicSpanPlan]:
    return [
        build_atomic_span_plan(decision)
        for decision in decisions
        if decision.candidate_action == "split_before_candidate"
    ]


def _counter_dict(values: Iterable[str]) -> Dict[str, int]:
    return dict(sorted(Counter(values).items()))


def atomic_span_dry_run_report(
    plans: Sequence[AtomicSpanPlan],
    *,
    owner_user_id: str | uuid.UUID,
    batch_source_count: int,
) -> Dict[str, Any]:
    owner = uuid.UUID(str(owner_user_id))
    spans = [span for plan in plans for span in plan.spans]
    review_dispositions = {
        "review_claim_span",
        "review_preference_span",
        "review_project_span",
        "review_belief_span",
    }
    return {
        "mode": "atomic_span_dry_run_no_writes",
        "owner_user_id": str(owner),
        "source_system": "public.chat_log",
        "source_role": "frontend/chat:user",
        "batch_source_count": batch_source_count,
        "split_source_count": len(plans),
        "span_count": len(spans),
        "summary": {
            "span_kinds": _counter_dict(span.span_kind for span in spans),
            "authorship": _counter_dict(span.authorship for span in spans),
            "dispositions": _counter_dict(span.disposition for span in spans),
            "candidate_targets": _counter_dict(
                span.candidate_target for span in spans if span.candidate_target
            ),
            "review_span_count": sum(
                span.disposition in review_dispositions for span in spans
            ),
            "manual_split_count": sum(span.requires_manual_split for span in spans),
            "structured_route_count": sum(
                span.disposition == "route_structured_adapter" for span in spans
            ),
            "excluded_non_assertion_count": sum(
                span.disposition
                in {
                    "exclude_question",
                    "exclude_request",
                    "exclude_quoted_or_pasted",
                }
                for span in spans
            ),
        },
        "controls": {
            "database_writes": 0,
            "evidence_writes": 0,
            "candidate_writes": 0,
            "claim_writes": 0,
            "qdrant_writes": 0,
            "prompt_injection": False,
            "full_source_text_in_report": False,
            "exact_source_offsets": True,
            "source_hash_verification": True,
            "review_required": True,
        },
        "sources": [
            {
                "source_id": str(plan.source_decision.source.source_id),
                "created_at": plan.source_decision.source.created_at.isoformat(),
                "thread_id": (
                    str(plan.source_decision.source.thread_id)
                    if plan.source_decision.source.thread_id
                    else None
                ),
                "source_content_sha256": plan.source_decision.content_sha256,
                "source_character_count": len(plan.source_decision.source.text),
                "parent_primary_lane": plan.source_decision.primary_lane,
                "parent_lanes": list(plan.source_decision.lanes),
                "parent_content_authorship": plan.source_decision.content_authorship,
                "source_dump_detected": plan.source_dump_detected,
                "span_count": len(plan.spans),
                "coverage_verified": True,
                "spans": [
                    {
                        "span_id": str(span.span_id),
                        "ordinal": span.ordinal,
                        "char_start": span.char_start,
                        "char_end": span.char_end,
                        "character_count": span.char_end - span.char_start,
                        "content_sha256": span.content_sha256,
                        "span_kind": span.span_kind,
                        "authorship": span.authorship,
                        "primary_lane": span.primary_lane,
                        "lanes": list(span.lanes),
                        "reason_codes": list(span.reason_codes),
                        "disposition": span.disposition,
                        "candidate_target": span.candidate_target,
                        "requires_manual_split": span.requires_manual_split,
                        "preview": " ".join(span.content.split())[:320],
                    }
                    for span in plan.spans
                ],
            }
            for plan in plans
        ],
    }
