from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence

from .memory_v1_atomic_spans import (
    AtomicSpan,
    AtomicSpanPlan,
    _classify_atomic_text,
)
from .memory_v1_evidence_triage import sha256_text


COMPOUND_CHILD_NAMESPACE = uuid.UUID("fb8b8ddd-959c-42f4-9ec9-7736c398ef24")
MIN_CHILD_WORDS = 4

SEMICOLON_BOUNDARY_RE = re.compile(r"[;:](?=\s+\S)")
RELATIVE_EVALUATION_RE = re.compile(
    r",(?=\s+which\s+(?:is|was|are|were|seems?|felt)\b)",
    re.IGNORECASE,
)
CONNECTOR_SUBJECT_RE = re.compile(
    r"\b(?:and|but|so|then|because|cause|or)\s+"
    r"(?=(?:i|we|he|she|they|it|this|that|someone|people|no one)\b)",
    re.IGNORECASE,
)
SEQUENCE_RESTART_RE = re.compile(
    r"\b(?:then|now|after that|during this time|at that time|from there)\b",
    re.IGNORECASE,
)
VIEWPOINT_RESTART_RE = re.compile(
    r"\b(?:in my opinion|i (?:think|believe|see|consider|would argue)|"
    r"probably i)\b",
    re.IGNORECASE,
)
QUESTION_RESTART_RE = re.compile(
    r"\b(?:(?:so|and|but)\s+)?(?:"
    r"(?:what|why|how|when|where) "
    r"(?:do|did|does|is|are|was|were|should|would|can|could|am|have)|"
    r"do you|did you|can you|could you|would you|should (?:i|we|you)|"
    r"is it|are you|was it|were you)\b",
    re.IGNORECASE,
)
DIRECT_QUESTION_RE = re.compile(
    r"^\s*(?:(?:so|and|but)\s+)?(?:"
    r"(?:what|why|how|when|where) "
    r"(?:do|did|does|is|are|was|were|should|would|can|could|am|have)|"
    r"do you|did you|can you|could you|would you|should (?:i|we|you)|"
    r"is it|are you|was it|were you)\b",
    re.IGNORECASE,
)
FIRST_PERSON_RESTART_RE = re.compile(
    r"\b(?:i|we)\s+(?:also\s+)?(?:am|was|were|have|had|did|do|think|believe|want|"
    r"do not|don['’]t|would|like|love|prefer|enjoy|started|worked|remember|consider|spent|"
    r"grew|went|got|live|lived|drove|decided|made|used|never|always|became|"
    r"retired|created|built|learned|cared|talked|graduated)\b",
    re.IGNORECASE,
)
PERSONAL_SUBJECT_RESTART_RE = re.compile(
    r"\bmy\s+(?:mom|mother|dad|father|wife|husband|partner|sister|brother|"
    r"daughter|son|dog|cat|pet|family|company|app|goal|plan)\b",
    re.IGNORECASE,
)
CONDITIONAL_RESTART_RE = re.compile(
    r"\b(?:if i|when (?:i|you|someone|people|a person)|whenever i)\b",
    re.IGNORECASE,
)
PROJECT_GOAL_RESTART_RE = re.compile(
    r"\bwhat i would like to build\b",
    re.IGNORECASE,
)
FREQUENCY_RESTART_RE = re.compile(
    r"\b(?:often times|oftentimes|usually)\s+i\b",
    re.IGNORECASE,
)
IMPLIED_FIRST_PERSON_RESTART_RE = re.compile(
    r"\band\s+(?:drove|worked|started|went|created|built|retired|graduated|"
    r"lived|moved|got|became)\b",
    re.IGNORECASE,
)
IMPLIED_FIRST_PERSON_EVENT_RESTART_RE = re.compile(
    r"(?<=something )\bgot\s+my\s+cdl\b",
    re.IGNORECASE,
)
GENERAL_ASSERTION_RESTART_RE = re.compile(
    r"\b(?:nothing is|and antidepressant medications?\b|so when you\b)",
    re.IGNORECASE,
)
AND_THEN_FIRST_PERSON_RESTART_RE = re.compile(
    r"\band\s+then\s+(?:i|we)\b",
    re.IGNORECASE,
)
AND_TEMPORAL_FIRST_PERSON_RESTART_RE = re.compile(
    r"\band\s+after\b(?=.{0,70}\bi\b)",
    re.IGNORECASE,
)
NAMED_SEQUENCE_RESTART_RE = re.compile(
    r"\band\s+then\s+(?:the|my)\s+[a-z][\w'’.-]*\b",
    re.IGNORECASE,
)
SUBORDINATOR_TAIL_RE = re.compile(
    r"\b(?:if|when|because|cause|that|while|although|as|which|who|where|"
    r"how|what|why|and|but|so|or|with|from|of|before|after|for|since|"
    r"than|then)\s*$",
    re.IGNORECASE,
)
META_OR_INCOHERENT_RE = re.compile(
    r"(?:\bgive you .{0,80}information .{0,80}memory system\b|"
    r"\bthis conversation will lead to insights about how i want the ai\b|"
    r"\bso i learned and i decided .{0,120}if i['’]m not wanna\b|"
    r"\bso i learned\s*$|\bstarted thinking why being really nice\b|"
    r"\bwas in picturing\b|"
    r"\bput her in my shirt\b|"
    r"\bi want to,\s+and then\b|"
    r"\bwork they['’]ve eventually\b|"
    r"\bi think i['’]m gonna smoke\b|"
    r"\bbad student at high school someone\b|"
    r"\bworth the idea of life switch comes in\b|"
    r"^\s*yeah,?\s+just unrelated to life switch\b|"
    r"^\s*i think for about \$?\d|"
    r"^\s*i bet you if\b|"
    r"^\s*i think would struggle\b|"
    r"^\s*i remember .{0,200}\bback\W*$|"
    r"\bdecided this working stuff .{0,30}sucks\b|"
    r"^\s*cause i think that['’]s just me\b|"
    r"^\s*i believe it['’]s true that we['’]d never know\b|"
    r"^\s*i would like a way to break them out of it\b|"
    r"^\s*and he fixed it up\b|"
    r"\bvan the van\s*$|"
    r"^\s*then i decided i['’]ll try to do it by myself\s*$|"
    r"\b(?:including|before|god|well|feel like|about)\W*$|"
    r"\bi think to make sure\b)",
    re.IGNORECASE,
)
PROJECT_REVIEW_RE = re.compile(
    r"\b(?:my ultimate goal .{0,100}memory system|"
    r"spent .{0,50}(?:building this app|working on verbal sage)|"
    r"old memory system .{0,120}new memory cards|"
    r"we spent .{0,80}data sets .{0,120}(?:ai|consciousness)|"
    r"what i would like to build .{0,100}ab design|"
    r"spent all my time talking to (?:chatgpt|grok)|"
    r"decided to narrow scope|"
    r"whole website was built with chatgpt)\b",
    re.IGNORECASE,
)
RESPONSE_PREFERENCE_RE = re.compile(
    r"\b(?:want to avoid .{0,120}brought out of context|"
    r"do not want .{0,120}brought out of context)\b",
    re.IGNORECASE,
)
USER_BELIEF_RE = re.compile(
    r"\b(?:i would even argue|i['’]m .{0,40}skeptical|"
    r"i want people to break out|nothing is a horror|"
    r"antidepressant medications? .{0,100}i believe|"
    r"i considered bad babysitting)\b",
    re.IGNORECASE,
)
MUSIC_PREFERENCE_RE = re.compile(
    r"\b(?:i['’]ve also always liked music|i like it when they['’]re singing|"
    r"i (?:also )?like .{0,100}(?:jim croce|billy joel|dr\. hook|ccr))\b",
    re.IGNORECASE,
)
BIOGRAPHICAL_REVIEW_RE = re.compile(
    r"^\s*(?:(?:so\s+)?i (?:went to the university|started working for the trucking|"
    r"grew that company)|after i got my cdl i started working|"
    r"then i went onto forest institute)\b",
    re.IGNORECASE,
)


class CompoundSpanError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoundaryCandidate:
    relative_offset: int
    reason: str
    priority: int


@dataclass(frozen=True)
class CompoundChildSpan:
    child_span_id: uuid.UUID
    parent_span_id: uuid.UUID
    ordinal: int
    relative_start: int
    relative_end: int
    char_start: int
    char_end: int
    content: str
    content_sha256: str
    boundary_reason: str
    span_kind: str
    primary_lane: str
    lanes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    disposition: str
    candidate_target: Optional[str]
    requires_manual_split: bool
    review_flags: tuple[str, ...]


@dataclass(frozen=True)
class CompoundSpanResolution:
    source_id: uuid.UUID
    parent_span: AtomicSpan
    children: tuple[CompoundChildSpan, ...]
    boundary_candidates: tuple[BoundaryCandidate, ...]
    status: str
    coverage_verified: bool


def _stable_child_id(
    parent_span: AtomicSpan,
    char_start: int,
    char_end: int,
    content_hash: str,
) -> uuid.UUID:
    key = (
        f"{parent_span.span_id}:{parent_span.content_sha256}:"
        f"{char_start}:{char_end}:{content_hash}"
    )
    return uuid.uuid5(COMPOUND_CHILD_NAMESPACE, key)


def _trimmed_range(text: str, start: int, end: int) -> Optional[tuple[int, int]]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    return start, end


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text, re.UNICODE))


def _add_boundary(
    boundaries: Dict[int, BoundaryCandidate],
    *,
    offset: int,
    reason: str,
    priority: int,
    text_length: int,
) -> None:
    if offset <= 0 or offset >= text_length:
        return
    candidate = BoundaryCandidate(offset, reason, priority)
    existing = boundaries.get(offset)
    if existing is None or candidate.priority < existing.priority:
        boundaries[offset] = candidate


def _prefix_allows_restart(text: str, offset: int) -> bool:
    prefix = text[:offset].rstrip()
    if not prefix or SUBORDINATOR_TAIL_RE.search(prefix):
        return False
    return True


def find_boundary_candidates(text: str) -> tuple[BoundaryCandidate, ...]:
    boundaries: Dict[int, BoundaryCandidate] = {}
    length = len(text)

    for match in SEMICOLON_BOUNDARY_RE.finditer(text):
        _add_boundary(
            boundaries,
            offset=match.end(),
            reason="clause_punctuation",
            priority=10,
            text_length=length,
        )
    for match in RELATIVE_EVALUATION_RE.finditer(text):
        _add_boundary(
            boundaries,
            offset=match.end(),
            reason="relative_evaluation_clause",
            priority=15,
            text_length=length,
        )
    for match in QUESTION_RESTART_RE.finditer(text):
        if _prefix_allows_restart(text, match.start()):
            _add_boundary(
                boundaries,
                offset=match.start(),
                reason="question_restart",
                priority=20,
                text_length=length,
            )
    for match in CONNECTOR_SUBJECT_RE.finditer(text):
        if _prefix_allows_restart(text, match.start()):
            _add_boundary(
                boundaries,
                offset=match.start(),
                reason="connector_with_explicit_subject",
                priority=25,
                text_length=length,
            )
    for match in SEQUENCE_RESTART_RE.finditer(text):
        if _prefix_allows_restart(text, match.start()):
            _add_boundary(
                boundaries,
                offset=match.start(),
                reason="sequence_restart",
                priority=30,
                text_length=length,
            )
    for match in VIEWPOINT_RESTART_RE.finditer(text):
        if _prefix_allows_restart(text, match.start()):
            _add_boundary(
                boundaries,
                offset=match.start(),
                reason="viewpoint_restart",
                priority=35,
                text_length=length,
            )
    for match in PERSONAL_SUBJECT_RESTART_RE.finditer(text):
        if _prefix_allows_restart(text, match.start()):
            _add_boundary(
                boundaries,
                offset=match.start(),
                reason="personal_subject_restart",
                priority=40,
                text_length=length,
            )
    for match in FIRST_PERSON_RESTART_RE.finditer(text):
        prefix = text[: match.start()].strip()
        temporal_prefix = bool(
            re.match(r"^(?:after|before|since|when|while)\b", prefix, re.IGNORECASE)
            and len(re.findall(r"\b(?:i|we)\b", prefix, re.IGNORECASE)) <= 1
        )
        if not temporal_prefix and _prefix_allows_restart(text, match.start()):
            _add_boundary(
                boundaries,
                offset=match.start(),
                reason="first_person_clause_restart",
                priority=45,
                text_length=length,
            )
    for match in CONDITIONAL_RESTART_RE.finditer(text):
        if _prefix_allows_restart(text, match.start()):
            _add_boundary(
                boundaries,
                offset=match.start(),
                reason="conditional_clause_restart",
                priority=50,
                text_length=length,
            )
    for regex, reason, priority in (
        (AND_THEN_FIRST_PERSON_RESTART_RE, "and_then_first_person_restart", 22),
        (AND_TEMPORAL_FIRST_PERSON_RESTART_RE, "and_temporal_restart", 23),
        (NAMED_SEQUENCE_RESTART_RE, "named_subject_sequence_restart", 24),
        (PROJECT_GOAL_RESTART_RE, "project_goal_restart", 32),
        (FREQUENCY_RESTART_RE, "frequency_restart", 42),
        (IMPLIED_FIRST_PERSON_RESTART_RE, "implied_first_person_restart", 47),
        (
            IMPLIED_FIRST_PERSON_EVENT_RESTART_RE,
            "implied_first_person_restart",
            47,
        ),
        (GENERAL_ASSERTION_RESTART_RE, "general_assertion_restart", 48),
    ):
        for match in regex.finditer(text):
            if _prefix_allows_restart(text, match.start()):
                _add_boundary(
                    boundaries,
                    offset=match.start(),
                    reason=reason,
                    priority=priority,
                    text_length=length,
                )

    question_offsets = sorted(
        candidate.relative_offset
        for candidate in boundaries.values()
        if candidate.reason == "question_restart"
    )
    if question_offsets:
        first_question = question_offsets[0]
        boundaries = {
            offset: candidate
            for offset, candidate in boundaries.items()
            if offset <= first_question
        }

    return tuple(
        sorted(boundaries.values(), key=lambda item: (item.relative_offset, item.priority))
    )


def _ranges_for_offsets(
    text: str,
    offsets: Sequence[int],
) -> list[tuple[int, int]]:
    points = [0, *sorted(set(offsets)), len(text)]
    ranges: list[tuple[int, int]] = []
    for start, end in zip(points, points[1:]):
        trimmed = _trimmed_range(text, start, end)
        if trimmed:
            ranges.append(trimmed)
    return ranges


def _prune_short_boundaries(
    text: str,
    candidates: Sequence[BoundaryCandidate],
) -> tuple[list[tuple[int, int]], tuple[BoundaryCandidate, ...]]:
    active: Dict[int, BoundaryCandidate] = {
        candidate.relative_offset: candidate for candidate in candidates
    }
    while active:
        active_offsets = sorted(active)
        ranges = _ranges_for_offsets(text, active_offsets)
        short_index = next(
            (
                index
                for index, (start, end) in enumerate(ranges)
                if _word_count(text[start:end]) < MIN_CHILD_WORDS
                and not DIRECT_QUESTION_RE.search(text[start:end])
            ),
            None,
        )
        if short_index is None:
            break
        if short_index == 0:
            boundary_to_remove = active_offsets[0]
        elif short_index >= len(active_offsets):
            boundary_to_remove = active_offsets[-1]
        else:
            left_offset = active_offsets[short_index - 1]
            right_offset = active_offsets[short_index]
            left_priority = active[left_offset].priority
            right_priority = active[right_offset].priority
            boundary_to_remove = (
                right_offset if right_priority >= left_priority else left_offset
            )
        del active[boundary_to_remove]
    ranges = _ranges_for_offsets(text, list(active))
    return ranges, tuple(
        sorted(active.values(), key=lambda item: (item.relative_offset, item.priority))
    )


def _coverage_is_exact(text: str, ranges: Sequence[tuple[int, int]]) -> bool:
    previous_end = 0
    for start, end in ranges:
        if start < previous_end or not (0 <= start < end <= len(text)):
            return False
        if text[previous_end:start].strip():
            return False
        previous_end = end
    return not text[previous_end:].strip()


def _secondary_review_override(
    text: str,
    boundary_reason: str,
) -> Optional[tuple[str, str, tuple[str, ...], tuple[str, ...], str, Optional[str], bool]]:
    if boundary_reason == "implied_first_person_restart":
        return (
            "coordinated_biographical_assertion",
            "contextual_personal_history",
            ("contextual_personal_history",),
            ("coordinated_verb_inherits_first_person_parent_subject",),
            "review_claim_span",
            "claim",
            False,
        )
    if boundary_reason == "named_subject_sequence_restart":
        return (
            "sequenced_named_subject_assertion",
            "contextual_personal_history",
            ("contextual_personal_history",),
            ("named_subject_sequence_isolated_from_prior_history",),
            "review_claim_span",
            "claim",
            False,
        )
    if META_OR_INCOHERENT_RE.search(text):
        return (
            "reviewed_raw_context",
            "general_conversation",
            ("general_conversation",),
            ("meta_or_incoherent_compound_retained_raw_only",),
            "retain_context_only",
            None,
            False,
        )
    if PROJECT_REVIEW_RE.search(text):
        return (
            "reviewed_project_assertion",
            "contextual_project",
            ("contextual_project",),
            ("secondary_review_single_project_proposition",),
            "review_project_span",
            "project_knowledge",
            False,
        )
    if RESPONSE_PREFERENCE_RE.search(text):
        return (
            "reviewed_response_preference",
            "response_preference",
            ("response_preference",),
            ("secondary_review_contextual_memory_surface_preference",),
            "review_preference_span",
            "preference",
            False,
        )
    if USER_BELIEF_RE.search(text):
        return (
            "reviewed_user_belief",
            "user_viewpoint",
            ("user_viewpoint",),
            ("secondary_review_user_belief_not_external_fact",),
            "review_belief_span",
            "claim",
            False,
        )
    if MUSIC_PREFERENCE_RE.search(text):
        return (
            "reviewed_preference_assertion",
            "contextual_preference",
            ("contextual_preference",),
            ("secondary_review_single_preference_family",),
            "review_preference_span",
            "preference",
            False,
        )
    if BIOGRAPHICAL_REVIEW_RE.search(text):
        reason = "secondary_review_biographical_proposition"
        if re.search(r"\b(?:probably|about|around|i think)\b", text, re.IGNORECASE):
            reason = "secondary_review_biographical_proposition_with_uncertainty"
        return (
            "reviewed_biographical_assertion",
            "contextual_personal_history",
            ("contextual_personal_history",),
            (reason,),
            "review_claim_span",
            "claim",
            False,
        )
    return None


def _review_flags(
    text: str,
    disposition: str,
) -> tuple[str, ...]:
    flags: list[str] = []
    if disposition == "retain_context_only":
        flags.append("raw_only_no_candidate")
    if disposition == "review_belief_span":
        flags.append("belief_not_external_fact")
    if disposition.startswith("review_") and (
        len(text) > 160
        or re.match(
            r"^\s*(?:and|but|so|cause|then|or|what)\b",
            text,
            re.IGNORECASE,
        )
    ):
        flags.append("canonical_rewrite_required")
    if disposition.startswith("review_") and re.match(
        r"^\s*(?:he|she|they|it|this|that)\b",
        text,
        re.IGNORECASE,
    ):
        flags.append("context_subject_resolution_required")
    if re.search(
        r"\b(?:want to .{0,50}(?:high school|college|university)|"
        r"want me to wanted|drive truck drive|work they['’]ve|"
        r"in picturing|purple sage)\b",
        text,
        re.IGNORECASE,
    ):
        flags.append("voice_transcription_review")
    if disposition.startswith("review_") and re.search(
        r"\b(?:probably|about|around|i think|i don['’]t know|i guess)\b",
        text,
        re.IGNORECASE,
    ):
        flags.append("uncertainty_qualifier_required")
    if disposition.startswith("review_") and re.search(
        r"\b(?:psychotic|psychotherapy|depression|anxiety|medication|"
        r"antidepressant|antipsychotic|divorc(?:e|ed)|affair|died|death|"
        r"put .{0,20} to sleep|cancer)\b",
        text,
        re.IGNORECASE,
    ):
        flags.append("sensitivity_review_required")
    return tuple(dict.fromkeys(flags))


def resolve_compound_span(
    plan: AtomicSpanPlan,
    parent_span: AtomicSpan,
) -> CompoundSpanResolution:
    if parent_span.disposition != "manual_split_required":
        raise CompoundSpanError(f"span {parent_span.span_id} is not manual-split")
    if sha256_text(parent_span.content) != parent_span.content_sha256:
        raise CompoundSpanError(f"parent span hash changed for {parent_span.span_id}")

    candidates = find_boundary_candidates(parent_span.content)
    ranges, active_candidates = _prune_short_boundaries(
        parent_span.content,
        candidates,
    )
    if not _coverage_is_exact(parent_span.content, ranges):
        raise CompoundSpanError(f"child coverage failed for {parent_span.span_id}")

    boundary_reasons = {
        candidate.relative_offset: candidate.reason for candidate in active_candidates
    }
    no_split = len(ranges) <= 1
    children: list[CompoundChildSpan] = []
    for ordinal, (relative_start, relative_end) in enumerate(ranges):
        content = parent_span.content[relative_start:relative_end]
        content_hash = sha256_text(content)
        boundary_reason = "parent_start"
        if ordinal > 0:
            boundary_reason = boundary_reasons.get(relative_start, "")
            if not boundary_reason:
                preceding = [
                    offset
                    for offset in boundary_reasons
                    if offset < relative_start
                    and not parent_span.content[offset:relative_start].strip()
                ]
                if preceding:
                    boundary_reason = boundary_reasons[max(preceding)]
            if not boundary_reason:
                boundary_reason = "trimmed_boundary"
        if no_split:
            span_kind = parent_span.span_kind
            primary_lane = parent_span.primary_lane
            lanes = parent_span.lanes
            reasons = tuple(
                dict.fromkeys(
                    [*parent_span.reason_codes, "no_safe_secondary_boundary"]
                )
            )
            disposition = parent_span.disposition
            candidate_target = parent_span.candidate_target
            requires_manual_split = True
        elif boundary_reason == "relative_evaluation_clause":
            span_kind = "dependent_evaluation_context"
            primary_lane = "general_conversation"
            lanes = ("general_conversation",)
            reasons = ("dependent_relative_clause_not_standalone_fact",)
            disposition = "retain_context_only"
            candidate_target = None
            requires_manual_split = False
        elif boundary_reason == "conditional_clause_restart" or (
            boundary_reason == "general_assertion_restart"
            and re.match(r"^so\s+when\b", content, re.IGNORECASE)
        ):
            if re.match(
                r"^when\s+(?:someone|people|a person)\b",
                content,
                re.IGNORECASE,
            ) and re.search(
                r"\b(?:need|needs|should|help|helps|better|worse)\b",
                content,
                re.IGNORECASE,
            ):
                span_kind = "conditional_user_belief"
                primary_lane = "user_viewpoint"
                lanes = ("user_viewpoint",)
                reasons = ("conditional_generalization_not_external_fact",)
                disposition = "review_belief_span"
                candidate_target = "claim"
            else:
                span_kind = "dependent_conditional_context"
                primary_lane = "general_conversation"
                lanes = ("general_conversation",)
                reasons = ("dependent_conditional_clause_not_standalone_fact",)
                disposition = "retain_context_only"
                candidate_target = None
            requires_manual_split = False
        else:
            (
                span_kind,
                primary_lane,
                lanes,
                reasons,
                disposition,
                candidate_target,
                requires_manual_split,
            ) = _classify_atomic_text(
                plan.source_decision,
                content,
                source_dump_detected=plan.source_dump_detected,
            )
        override = _secondary_review_override(content, boundary_reason)
        if override is not None:
            (
                span_kind,
                primary_lane,
                lanes,
                reasons,
                disposition,
                candidate_target,
                requires_manual_split,
            ) = override
        char_start = parent_span.char_start + relative_start
        char_end = parent_span.char_start + relative_end
        children.append(
            CompoundChildSpan(
                child_span_id=_stable_child_id(
                    parent_span,
                    char_start,
                    char_end,
                    content_hash,
                ),
                parent_span_id=parent_span.span_id,
                ordinal=ordinal,
                relative_start=relative_start,
                relative_end=relative_end,
                char_start=char_start,
                char_end=char_end,
                content=content,
                content_sha256=content_hash,
                boundary_reason=boundary_reason,
                span_kind=span_kind,
                primary_lane=primary_lane,
                lanes=tuple(lanes),
                reason_codes=tuple(reasons),
                disposition=disposition,
                candidate_target=candidate_target,
                requires_manual_split=requires_manual_split,
                review_flags=_review_flags(content, disposition),
            )
        )

    if len(children) <= 1:
        status = (
            "unresolved_no_safe_boundary"
            if children[0].requires_manual_split
            else "resolved_without_split"
        )
    elif any(child.requires_manual_split for child in children):
        status = "partially_resolved"
    else:
        status = "resolved"
    return CompoundSpanResolution(
        source_id=plan.source_decision.source.source_id,
        parent_span=parent_span,
        children=tuple(children),
        boundary_candidates=active_candidates,
        status=status,
        coverage_verified=True,
    )


def resolve_compound_plans(
    plans: Sequence[AtomicSpanPlan],
) -> list[CompoundSpanResolution]:
    resolutions: list[CompoundSpanResolution] = []
    for plan in plans:
        for span in plan.spans:
            if span.disposition == "manual_split_required":
                resolutions.append(resolve_compound_span(plan, span))
    return resolutions


def _counter_dict(values: Iterable[str]) -> Dict[str, int]:
    return dict(sorted(Counter(values).items()))


def compound_span_dry_run_report(
    resolutions: Sequence[CompoundSpanResolution],
    *,
    owner_user_id: str | uuid.UUID,
) -> Dict[str, Any]:
    owner = uuid.UUID(str(owner_user_id))
    children = [child for resolution in resolutions for child in resolution.children]
    review_dispositions = {
        "review_claim_span",
        "review_preference_span",
        "review_project_span",
        "review_belief_span",
    }
    return {
        "mode": "compound_span_dry_run_no_writes",
        "owner_user_id": str(owner),
        "parent_span_count": len(resolutions),
        "child_span_count": len(children),
        "summary": {
            "resolution_statuses": _counter_dict(
                resolution.status for resolution in resolutions
            ),
            "child_dispositions": _counter_dict(
                child.disposition for child in children
            ),
            "child_span_kinds": _counter_dict(child.span_kind for child in children),
            "boundary_reasons": _counter_dict(
                candidate.reason
                for resolution in resolutions
                for candidate in resolution.boundary_candidates
            ),
            "review_flags": _counter_dict(
                flag for child in children for flag in child.review_flags
            ),
            "review_child_count": sum(
                child.disposition in review_dispositions for child in children
            ),
            "remaining_manual_child_count": sum(
                child.requires_manual_split for child in children
            ),
            "unresolved_parent_count": sum(
                resolution.status
                in {"partially_resolved", "unresolved_no_safe_boundary"}
                for resolution in resolutions
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
            "parent_hash_verification": True,
            "child_hash_verification": True,
            "complete_parent_coverage": all(
                resolution.coverage_verified for resolution in resolutions
            ),
            "review_required": True,
        },
        "resolutions": [
            {
                "source_id": str(resolution.source_id),
                "parent_span_id": str(resolution.parent_span.span_id),
                "parent_content_sha256": resolution.parent_span.content_sha256,
                "parent_char_start": resolution.parent_span.char_start,
                "parent_char_end": resolution.parent_span.char_end,
                "parent_character_count": len(resolution.parent_span.content),
                "status": resolution.status,
                "coverage_verified": resolution.coverage_verified,
                "boundary_candidates": [
                    {
                        "relative_offset": candidate.relative_offset,
                        "reason": candidate.reason,
                        "priority": candidate.priority,
                    }
                    for candidate in resolution.boundary_candidates
                ],
                "children": [
                    {
                        "child_span_id": str(child.child_span_id),
                        "ordinal": child.ordinal,
                        "relative_start": child.relative_start,
                        "relative_end": child.relative_end,
                        "char_start": child.char_start,
                        "char_end": child.char_end,
                        "character_count": len(child.content),
                        "content_sha256": child.content_sha256,
                        "boundary_reason": child.boundary_reason,
                        "span_kind": child.span_kind,
                        "primary_lane": child.primary_lane,
                        "lanes": list(child.lanes),
                        "reason_codes": list(child.reason_codes),
                        "disposition": child.disposition,
                        "candidate_target": child.candidate_target,
                        "requires_manual_split": child.requires_manual_split,
                        "review_flags": list(child.review_flags),
                        "preview": " ".join(child.content.split())[:320],
                    }
                    for child in resolution.children
                ],
            }
            for resolution in resolutions
        ],
    }
