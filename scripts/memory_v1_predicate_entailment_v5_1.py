#!/usr/bin/env python3
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence


POLICY_VERSION = "memory_v1_predicate_entailment_v5_1"

DecisionStatus = Literal["accept", "defer"]

_EMPLOYMENT_NEGATION_RE = re.compile(
    r"(?:"
    r"\b(?:do(?:n['’]?t| not)|does(?:n['’]?t| not)|"
    r"did(?:n['’]?t| not))\s+"
    r"(?:(?:currently|actually|really|normally|now)\s+){0,4}"
    r"(?:work(?:ing)?(?:\s+as)?|practice(?:\s+as)?|"
    r"do\s+(?:it|this|that)\s+for\s+a\s+living)\b"
    r"|"
    r"\b(?:am|is|are|was|were)\s+not\s+"
    r"(?:(?:currently|actually|really|now)\s+){0,4}"
    r"(?:working|employed|practicing)(?:\s+as)?\b"
    r"|"
    r"\bnot\s+(?:my|his|her|their|the)\s+"
    r"(?:job|occupation|profession|career)\b"
    r"|"
    r"\b(?:no\s+longer|never)\s+"
    r"(?:work(?:ing|ed|s)?(?:\s+as)?|practice(?:d|s|ing)?(?:\s+as)?|"
    r"employed(?:\s+as)?)\b"
    r")",
    re.IGNORECASE,
)

_EMPLOYMENT_ASSERTION_RE = re.compile(
    r"(?:"
    r"\b(?:work|works|worked|working|employed|practice|practices|"
    r"practiced|practicing)\s+as\b"
    r"|"
    r"\b(?:job|occupation|profession|career)\s+(?:is|was|as)\b"
    r"|"
    r"\b(?:do|does|did|doing)\s+(?:it|this|that)\s+for\s+a\s+living\b"
    r"|"
    r"\b(?:make|makes|made|making)\s+(?:a|my|their|his|her)\s+living\s+as\b"
    r"|"
    r"\b(?:i\s+am|i['’]m|he\s+is|she\s+is|they\s+are|"
    r"he['’]s|she['’]s|they['’]re)\s+(?:an?\s+)?"
    r")",
    re.IGNORECASE,
)

_QUALIFICATION_ONLY_RE = re.compile(
    r"\b(?:certif(?:ied|ication)|credential(?:ed|s)?|licen[cs](?:e|ed|ing)|"
    r"qualif(?:ied|ication)|train(?:ed|ing)|education|coursework|"
    r"degree|diploma)\b",
    re.IGNORECASE,
)

_BECAME_ROLE_RE = re.compile(
    r"\b(?:became|become|becoming)\s+(?:an?\s+)?", re.IGNORECASE
)

_CREDENTIAL_ASSERTION_RE = re.compile(
    r"\b(?:certif(?:ied|ication)|credential(?:ed|s)?|licen[cs](?:e|ed)|"
    r"qualified|completed\s+(?:a\s+)?(?:training|course|program)|"
    r"trained\s+as|earned\s+(?:a\s+)?(?:credential|certificate|license|degree))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EntailmentDecision:
    status: DecisionStatus
    policy_version: str
    reason_code: str | None
    source_span: dict[str, Any] | None


def _span_bounds(
    source_spans: Sequence[Mapping[str, Any]], text: str
) -> tuple[int, int]:
    starts = [
        int(item["start"])
        for item in source_spans
        if isinstance(item, Mapping) and "start" in item and "end" in item
    ]
    ends = [
        int(item["end"])
        for item in source_spans
        if isinstance(item, Mapping) and "start" in item and "end" in item
    ]
    if not starts or not ends:
        return 0, len(text)
    start = max(0, min(starts))
    end = min(len(text), max(ends))
    return start, max(start, end)


def _sentence_span(
    source_spans: Sequence[Mapping[str, Any]], text: str
) -> dict[str, Any]:
    start, end = _span_bounds(source_spans, text)
    left = max(
        text.rfind(".", 0, start),
        text.rfind("!", 0, start),
        text.rfind("?", 0, start),
    )
    left = 0 if left < 0 else left + 1
    right_candidates = [
        value
        for value in (
            text.find(".", end),
            text.find("!", end),
            text.find("?", end),
        )
        if value >= 0
    ]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    if right - left > 5000:
        left = max(left, start - 2000)
        right = min(right, max(end + 2000, left + 1))
        right = min(right, left + 5000)
    while left < right and text[left].isspace():
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    return {"start": left, "end": right, "quote": text[left:right]}


def _defer(reason_code: str, span: dict[str, Any]) -> EntailmentDecision:
    return EntailmentDecision(
        status="defer",
        policy_version=POLICY_VERSION,
        reason_code=reason_code,
        source_span=span,
    )


def _accept() -> EntailmentDecision:
    return EntailmentDecision(
        status="accept",
        policy_version=POLICY_VERSION,
        reason_code=None,
        source_span=None,
    )


def assess_observation_entailment(
    observation: Mapping[str, Any], text: str
) -> EntailmentDecision:
    """Fail closed for predicates with a registered V5.1 semantic guard.

    Structural registry validation still applies to every predicate. This layer
    adds source-level entailment checks where a structurally valid predicate can
    materially overstate the source. It never rewrites or substitutes a
    predicate.
    """

    predicate = str(observation.get("predicate", ""))
    polarity = str(observation.get("polarity", ""))
    source_spans = observation.get("source_spans")
    if not isinstance(source_spans, Sequence) or isinstance(
        source_spans, (str, bytes)
    ):
        source_spans = []
    span = _sentence_span(source_spans, text)
    context = str(span["quote"])

    if predicate == "occupation.works_as":
        negated_employment = _EMPLOYMENT_NEGATION_RE.search(context) is not None
        asserted_employment = _EMPLOYMENT_ASSERTION_RE.search(context) is not None
        qualification_only = _QUALIFICATION_ONLY_RE.search(context) is not None
        became_role = _BECAME_ROLE_RE.search(context) is not None

        if polarity == "affirmed" and negated_employment:
            return _defer("source_contradicts_predicate", span)
        if polarity == "negated":
            if negated_employment:
                return _accept()
            return _defer("predicate_semantics_unresolved", span)
        if polarity != "affirmed":
            return _defer("predicate_semantics_unresolved", span)
        if asserted_employment:
            return _accept()
        if qualification_only or became_role:
            return _defer("predicate_semantics_unresolved", span)
        return _defer("predicate_semantics_unresolved", span)

    if predicate == "credential.reported":
        if polarity != "affirmed":
            return _defer("predicate_semantics_unresolved", span)
        if _CREDENTIAL_ASSERTION_RE.search(context):
            return _accept()
        return _defer("predicate_semantics_unresolved", span)

    return _accept()
