from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.memory_v1_relationship_policy_v5_1 import (
    DEFAULT_REGISTRY,
    RelationshipDecision,
    assess_relationship_proposal,
    load_registry,
    map_named_party_role,
    normalize_role,
    role_index,
)


POLICY_VERSION = "memory_v1_relationship_observation_v5_1"
SENSITIVITY_ORDER = {"low": 0, "medium": 1, "high": 2, "restricted": 3}
SURFACE_POLICY_ADAPTER = {
    "direct_or_relevant": "direct_or_relevant",
    "explicit_person_or_relationship_context_only": "explicit_recall_only",
    "mention_when_directly_relevant": "mention_when_directly_relevant",
    "restricted_explicit_recall_only": "explicit_recall_only",
}
ROLE_SPLIT_RE = re.compile(r"[:/|]+")
NESTED_POSSESSIVE_RELATION_RE = re.compile(
    r"\bmy\s+[^.!?]{1,80}(?:'s|’s)\s+"
    r"(?:aunt|brother|child|coach|cousin|doctor|father|friend|manager|mentor|"
    r"mother|neighbor|neighbour|parent|partner|provider|relative|roommate|"
    r"sibling|sister|spouse|teacher|uncle)\b",
    re.IGNORECASE,
)


def _pattern(value: str) -> str:
    words = [re.escape(part) for part in value.split("_") if part]
    return r"\b" + r"[\s_-]+".join(words) + r"s?\b"


PREDICATE_EVIDENCE_PATTERNS = {
    "relationship.caregiver_for": r"\b(?:caregiver|carer|care\s+for|caring\s+for)\b",
    "relationship.collaborator_with": r"\bcollaborat(?:e|es|ed|ing|or)s?\b",
    "relationship.has_pet": r"\b(?:pet|pets|dog|dogs|cat|cats|animal|animals)\b",
    "relationship.healthcare_provider_for": r"\b(?:doctor|physician|cardiologist|psychiatrist|psychologist|therapist)\b",
    "relationship.in_law_of": r"\b(?:wife|husband|spouse)['’]s\s+(?:brother|sister|father|mother)\b|\b(?:brother|sister|father|mother)[\s-]+in[\s-]+law\b",
    "relationship.lives_with": r"\b(?:live|lives|lived|living)\s+(?:together\s+)?with\b|\bshare\s+(?:an?|the)\s+(?:apartment|home|house)\b",
    "relationship.manager_of": r"\b(?:boss|manager|supervisor|reports?\s+(?:directly\s+)?to|manage[sd]?)\b",
    "relationship.mentor_of": r"\bmentor(?:s|ed|ing)?\b",
    "relationship.parent_of": r"\b(?:parent|parents|father(?![\s-]+in[\s-]+law)|mother(?![\s-]+in[\s-]+law)|dad|mom|child|children|son|sons|daughter|daughters|stepchild|stepchildren|stepson|stepsons|stepdaughter|stepdaughters)\b",
    "relationship.plan_helper_for": r"\b(?:helps?\s+me\s+manage|permission\s+to\s+edit)\b[^.!?]{0,100}\b(?:LifeSwitch\s+)?plan\b|\b(?:LifeSwitch\s+)?plan\b[^.!?]{0,100}\bhelps?\s+me\b",
    "relationship.romantic_partner_of": r"\b(?:boyfriend|girlfriend|fianc(?:e|ee|é|ée)|life[\s-]+partner|romantic[\s-]+partner)\b",
    "relationship.roommate_of": r"\b(?:roommate|roommates|housemate|housemates|flatmate|flatmates)\b",
    "relationship.spouse_of": r"\b(?:wife|wives|husband|husbands|spouse|spouses|married\s+to)\b",
    "relationship.teacher_of": r"\b(?:teacher|professor|instructor|teaches|taught)\b",
    "relationship.teammate_of": r"\b(?:teammate|teammates|same\s+[^.!?]{0,40}\bteam|play[^.!?]{0,40}\bteam)\b",
    "social.avoids": r"\bavoid(?:s|ed|ing)?\b",
    "social.competes_with": r"\b(?:compet(?:e|es|ed|ing|itor)|rival(?:s|ry)?)\b",
    "social.depends_on": r"\b(?:depend(?:s|ed|ing)?\s+on|rel(?:y|ies|ied|ying)\s+on)\b",
    "social.distrusts": r"\b(?:distrust(?:s|ed|ing)?|do\s+not\s+trust|don['’]?t\s+trust|no\s+longer\s+trust)\b",
    "social.estranged_from": r"\bestrang(?:ed|ement)\b",
    "social.experiences_tension_with": r"\b(?:ongoing\s+)?tension\b",
    "social.feels_close_to": r"\bfeel(?:s|ing)?\s+(?:very\s+|emotionally\s+)?close\s+to\b",
    "social.feels_unsafe_with": r"\bfeel(?:s|ing)?\s+unsafe\b",
    "social.in_conflict_with": r"\b(?:ongoing\s+)?conflict\b",
    "social.no_contact_with": r"\bno\s+contact\b",
    "social.perceives_as_adversary": r"\b(?:consider|considers|considered|regard|regards|regarded|see|sees|saw)\b[^.!?]{0,100}\b(?:enemy|adversary)\b",
    "social.supports": r"\b(?:support|supports|supported|supporting|provides?\s+[^.!?]{0,50}\s+support)\b",
    "social.trusts": r"\btrust(?:s|ed|ing)?\b",
}

SUPPORT_NAMED_TO_SELF_RE = re.compile(
    r"(?:\b[A-Z][A-Za-z'’\-]{1,79}\b[^.!?]{0,80}\b"
    r"(?:support(?:s|ed|ing)?\s+me|provides?[^.!?]{0,50}\bsupport\s+(?:for|to)\s+me)\b|"
    r"\bI\s+(?:get|receive|received)\s+[^.!?]{0,40}\bsupport\s+from\b)",
    re.IGNORECASE,
)
SUPPORT_SELF_TO_NAMED_RE = re.compile(
    r"(?:\bI\s+(?:support|supported|am\s+supporting)\b|"
    r"\bI\s+provide[sd]?\s+[^.!?]{0,50}\bsupport\s+(?:for|to)\b|"
    r"\b[A-Z][A-Za-z'’\-]{1,79}\b[^.!?]{0,60}\b"
    r"(?:gets|receives?)\s+[^.!?]{0,40}\bsupport\s+from\s+me\b)",
    re.IGNORECASE,
)
HISTORICAL_RELATIONSHIP_END_RE = re.compile(
    r"\b(?:used\s+to\s+be|former(?:ly)?|ex[\s-]+(?:wife|husband|spouse|"
    r"boyfriend|girlfriend|partner|friend)|no\s+longer\s+(?:my|a)|"
    r"lost\s+touch|relationship\s+ended|separated|divorced|"
    r"until\s+(?:he|she|they|the\s+person)?\s*(?:died|passed\s+away))\b",
    re.IGNORECASE,
)
DYNAMIC_CURRENT_STATE_RE = re.compile(
    r"\b(?:since|still|currently|ongoing|these\s+days|lately|right\s+now|"
    r"have\s+had|has\s+had|no\s+contact)\b",
    re.IGNORECASE,
)
PERSON_NAME_PATTERN = r"[A-Z][A-Za-z'’\-]{1,79}"


@dataclass(frozen=True)
class SourceRelationshipAssertion:
    predicate: str
    named_party_role: str
    name_text: str
    historical_end: bool = False


def explicit_relationship_assertions(
    text: str,
) -> tuple[SourceRelationshipAssertion, ...]:
    """Return only explicit owner-to-person relationships entailed by source."""
    assertions: list[SourceRelationshipAssertion] = []
    seen: set[tuple[str, str]] = set()

    def add(
        predicate: str,
        role: str,
        name: str,
        *,
        historical_end: bool = False,
    ) -> None:
        normalized_name = name.strip(" \t\r\n.,;:")
        key = (predicate, normalized_name.casefold())
        if normalized_name and key not in seen:
            assertions.append(
                SourceRelationshipAssertion(
                    predicate=predicate,
                    named_party_role=role,
                    name_text=normalized_name,
                    historical_end=historical_end,
                )
            )
            seen.add(key)

    caregiver_patterns = (
        (
            rf"\bI\s+am\s+(?:the\s+)?(?:primary\s+)?caregiver\s+for\s+"
            rf"(?:my\s+(?:father|mother|parent|wife|husband|spouse)\s+)?"
            rf"(?P<name>{PERSON_NAME_PATTERN})\b"
        ),
        (
            rf"\bI\s+(?:care|cared|am\s+caring|have\s+been\s+caring)\s+"
            rf"for\s+(?:my\s+(?:father|mother|parent|wife|husband|spouse)\s+)?"
            rf"(?P<name>{PERSON_NAME_PATTERN})\b"
        ),
        (
            rf"\bI\s+(?:take|took|have\s+taken)\s+care\s+of\s+"
            rf"(?:my\s+(?:father|mother|parent|wife|husband|spouse)\s+)?"
            rf"(?P<name>{PERSON_NAME_PATTERN})\b"
        ),
        (
            rf"\bI\s+have\s+spent[^.!?]{{0,160}}\bcaring\s+for\s+"
            rf"(?:others[^.!?]{{0,80}}\bincluding\s+)?"
            rf"(?:my\s+(?:father|mother|parent|wife|husband|spouse)\s+)?"
            rf"(?P<name>{PERSON_NAME_PATTERN})\b"
        ),
    )
    for caregiver_pattern in caregiver_patterns:
        caregiver = re.search(
            caregiver_pattern,
            text,
            re.IGNORECASE,
        )
        if caregiver:
            add(
                "relationship.caregiver_for",
                "care_recipient",
                caregiver.group("name"),
            )
            break

    lives_with = re.search(
        rf"\bI\s+(?:currently\s+)?live\s+with\s+"
        rf"(?:my\s+(?:wife|husband|spouse)\s+)?"
        rf"(?P<name>{PERSON_NAME_PATTERN})\b",
        text,
        re.IGNORECASE,
    )
    if lives_with:
        add("relationship.lives_with", "cohabitant", lives_with.group("name"))

    shared_home = re.search(
        rf"\b(?P<name>{PERSON_NAME_PATTERN})\s+and\s+I\s+"
        rf"(?:share\s+(?:an?|the)\s+(?:apartment|home|house)|live\s+together)"
        rf"[^.!?]{{0,80}}\broommates?\b",
        text,
        re.IGNORECASE,
    )
    if shared_home:
        add("relationship.lives_with", "cohabitant", shared_home.group("name"))
        add("relationship.roommate_of", "roommate", shared_home.group("name"))

    spouse = re.search(
        rf"\b(?P<name>{PERSON_NAME_PATTERN})\s+(?:is|was|used\s+to\s+be)\s+"
        rf"my\s+(?P<role>wife|husband|spouse)\b",
        text,
        re.IGNORECASE,
    )
    if spouse and not re.match(r"['’]s\b", text[spouse.end() :], re.IGNORECASE):
        add(
            "relationship.spouse_of",
            spouse.group("role").casefold(),
            spouse.group("name"),
            historical_end=relationship_has_explicit_historical_end(text)
            or bool(re.search(r"\buntil\b", text, re.IGNORECASE)),
        )
    possessed_spouse = re.search(
        rf"\bmy\s+(?P<role>wife|husband|spouse)\s+"
        rf"(?P<name>{PERSON_NAME_PATTERN})\b",
        text,
        re.IGNORECASE,
    )
    if possessed_spouse:
        add(
            "relationship.spouse_of",
            possessed_spouse.group("role").casefold(),
            possessed_spouse.group("name"),
        )

    if not re.search(
        r"\blike\s+(?:a\s+)?(?:brother|sister|sibling)\b|"
        r"\bnot\s+related\b",
        text,
        re.IGNORECASE,
    ):
        sibling = re.search(
            rf"\b(?P<name>{PERSON_NAME_PATTERN})\s+is\s+my\s+"
            rf"(?P<role>brother|sister|sibling)\b",
            text,
            re.IGNORECASE,
        )
        if sibling and not re.match(
            r"(?:['’]s\b|[\s-]+in[\s-]+law\b)",
            text[sibling.end() :],
            re.IGNORECASE,
        ):
            add(
                "relationship.sibling_of",
                sibling.group("role").casefold(),
                sibling.group("name"),
            )

    in_law = re.search(
        rf"\b(?P<name>{PERSON_NAME_PATTERN})\s+is\s+my\s+"
        rf"(?:wife|husband|spouse)['’]s\s+"
        rf"(?P<role>brother|sister|father|mother)\b",
        text,
        re.IGNORECASE,
    )
    if in_law:
        add(
            "relationship.in_law_of",
            f"{in_law.group('role').casefold()}_in_law",
            in_law.group("name"),
        )
    direct_in_law = re.search(
        rf"\b(?P<name>{PERSON_NAME_PATTERN})\s+is\s+my\s+"
        rf"(?P<role>brother|sister|father|mother)[\s-]+in[\s-]+law\b",
        text,
        re.IGNORECASE,
    )
    if direct_in_law:
        add(
            "relationship.in_law_of",
            f"{direct_in_law.group('role').casefold()}_in_law",
            direct_in_law.group("name"),
        )

    teammate = re.search(
        rf"\b(?P<name>{PERSON_NAME_PATTERN})\s+and\s+I\s+"
        rf"(?:play|are|compete)[^.!?]{{0,80}}\b(?:same\s+)?[^.!?]*team\b",
        text,
        re.IGNORECASE,
    )
    if teammate:
        add("relationship.teammate_of", "teammate", teammate.group("name"))

    manager = re.search(
        rf"\bI\s+report\s+(?:directly\s+)?to\s+"
        rf"(?P<name>{PERSON_NAME_PATTERN})\b",
        text,
        re.IGNORECASE,
    )
    if manager:
        add("relationship.manager_of", "manager", manager.group("name"))
    direct_report = re.search(
        rf"\b(?P<name>{PERSON_NAME_PATTERN})\s+reports?\s+"
        rf"(?:directly\s+)?to\s+me\b",
        text,
        re.IGNORECASE,
    )
    if direct_report:
        add(
            "relationship.manager_of",
            "direct_report",
            direct_report.group("name"),
        )

    clinician = re.search(
        rf"\b(?:Dr\.\s*)?(?P<name>{PERSON_NAME_PATTERN})\s+is\s+my\s+"
        rf"(?:friend\s+and\s+(?:also\s+)?(?:my\s+)?)?"
        rf"(?:primary\s+care\s+)?"
        rf"(?:doctor|physician|cardiologist|psychiatrist|psychologist|therapist)\b",
        text,
        re.IGNORECASE,
    )
    if clinician:
        add(
            "relationship.healthcare_provider_for",
            "doctor",
            clinician.group("name"),
        )

    plan_helper = re.search(
        rf"\b(?P<name>{PERSON_NAME_PATTERN})\s+has\s+permission\s+to\s+"
        rf"edit\s+my\s+LifeSwitch\s+plan[^.!?]{{0,100}}\bhelps?\s+me\b",
        text,
        re.IGNORECASE,
    )
    if plan_helper:
        add(
            "relationship.plan_helper_for",
            "plan_helper",
            plan_helper.group("name"),
        )

    teacher = re.search(
        rf"\b(?:Professor\s+)?(?P<name>{PERSON_NAME_PATTERN})\s+"
        rf"teaches\s+(?:me|my\s+[^.!?]{{1,80}}(?:course|class))\b",
        text,
        re.IGNORECASE,
    )
    if teacher:
        add("relationship.teacher_of", "teacher", teacher.group("name"))

    avoided = re.search(
        rf"\bI\s+(?P<historical>used\s+to\s+)?avoid\s+"
        rf"(?P<name>{PERSON_NAME_PATTERN})\b",
        text,
        re.IGNORECASE,
    )
    if avoided and (
        avoided.group("historical")
        or not re.search(
            r"\b(?:do\s+not|don['’]?t|no\s+longer)\s+avoid\b",
            text,
            re.IGNORECASE,
        )
    ):
        add(
            "social.avoids",
            "person_avoided",
            avoided.group("name"),
            historical_end=bool(avoided.group("historical"))
            or bool(re.search(r"\bnot\s+avoid\b[^.!?]*\banymore\b", text, re.IGNORECASE)),
        )

    tension = re.search(
        rf"\b(?:ongoing\s+)?tension\s+between\s+"
        rf"(?:(?P<name_first>{PERSON_NAME_PATTERN})\s+and\s+me|"
        rf"me\s+and\s+(?P<name_second>{PERSON_NAME_PATTERN}))\b",
        text,
        re.IGNORECASE,
    )
    if tension:
        add(
            "social.experiences_tension_with",
            "person_in_tension",
            tension.group("name_first") or tension.group("name_second"),
        )

    named_relation = re.search(
        rf"\b(?P<name>{PERSON_NAME_PATTERN})\s+is\s+my\s+"
        rf"(?P<roles>[^.!?]{{1,120}})",
        text,
        re.IGNORECASE,
    )
    if named_relation:
        name = named_relation.group("name")
        roles = named_relation.group("roles")
        if re.search(r"['’]s\b", roles):
            return tuple(assertions)
        if re.search(r"\bcoworker\b", roles, re.IGNORECASE):
            add("relationship.coworker_of", "coworker", name)
        if re.search(
            r"\b(?:workout|training|lifting)\s+partner\b",
            roles,
            re.IGNORECASE,
        ) and not re.search(
            r"\bnot\s+(?:my\s+)?(?:workout|training|lifting)\s+partner\b",
            roles,
            re.IGNORECASE,
        ):
            add("relationship.training_partner_of", "workout_partner", name)
        if re.search(r"\bfriend\b", roles, re.IGNORECASE) and not re.search(
            r"\bnot\s+(?:a\s+)?(?:personal\s+)?friend\b",
            text,
            re.IGNORECASE,
        ):
            add("relationship.friend_of", "friend", name)

    return tuple(assertions)


@dataclass(frozen=True)
class ObservationDecision:
    status: str
    reason_code: str
    policy_version: str
    normalized_observation: dict[str, Any] | None
    repairs: tuple[str, ...]
    manual_review_required: bool


def relationship_role_candidates(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    candidates: list[str] = []
    for raw in (value, *ROLE_SPLIT_RE.split(value)):
        try:
            candidate = normalize_role(raw)
        except ValueError:
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    for raw in tuple(candidates):
        for token in raw.split("_"):
            if len(token) > 1 and token not in candidates:
                candidates.append(token)
    return tuple(candidates)


def map_entity_relationship_role(
    relationship_role: Any,
    *,
    proposed_predicate: str,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> RelationshipDecision:
    accepted: list[RelationshipDecision] = []
    for candidate in relationship_role_candidates(relationship_role):
        decision = map_named_party_role(
            candidate,
            proposed_predicate=proposed_predicate,
            registry_path=registry_path,
        )
        if decision.status == "accept":
            accepted.append(decision)
    mappings = {
        (value.mapping.predicate, value.mapping.edge_direction): value
        for value in accepted
        if value.mapping is not None
    }
    if len(mappings) == 1:
        return next(iter(mappings.values()))
    if len(mappings) > 1:
        return RelationshipDecision(
            status="defer",
            reason_code="ambiguous_relationship_role",
            policy_version=POLICY_VERSION,
            mapping=None,
            manual_review_required=True,
        )
    return RelationshipDecision(
        status="defer",
        reason_code="unregistered_or_mismatched_relationship_role",
        policy_version=POLICY_VERSION,
        mapping=None,
        manual_review_required=True,
    )


def _sentence_context(
    source_spans: Any,
    text: str,
) -> str:
    if not isinstance(source_spans, Sequence) or isinstance(
        source_spans, (str, bytes)
    ):
        return text[:5000]
    starts: list[int] = []
    ends: list[int] = []
    for span in source_spans:
        if not isinstance(span, Mapping):
            continue
        start = span.get("start")
        end = span.get("end")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(text)
        ):
            starts.append(start)
            ends.append(end)
    if not starts:
        return text[:5000]
    start = min(starts)
    end = max(ends)
    left = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start))
    left = 0 if left < 0 else left + 1
    right_values = [
        value
        for value in (text.find(".", end), text.find("!", end), text.find("?", end))
        if value >= 0
    ]
    right = min(right_values) + 1 if right_values else len(text)
    return text[left:right].strip()[:5000]


def _role_supported(role: str, text: str) -> bool:
    return re.search(_pattern(role), text, re.IGNORECASE) is not None


def directed_role_supported_by_source(predicate: str, text: str) -> str | None:
    """Return a registry role only when source syntax fixes edge direction."""
    if predicate == "social.supports":
        named_to_self = SUPPORT_NAMED_TO_SELF_RE.search(text) is not None
        self_to_named = SUPPORT_SELF_TO_NAMED_RE.search(text) is not None
        if named_to_self == self_to_named:
            return None
        return "supporter" if named_to_self else "supported_person"
    source_rules = {
        "relationship.caregiver_for": (
            r"\bI\s+(?:"
            r"am\s+(?:the\s+)?(?:primary\s+)?caregiver\s+for|"
            r"(?:care|cared|am\s+caring|have\s+been\s+caring)\s+for|"
            r"(?:take|took|have\s+taken)\s+care\s+of|"
            r"have\s+spent[^.!?]{0,160}\bcaring\s+for"
            r")\b",
            "care_recipient",
        ),
        "relationship.manager_of": (
            r"\bI\s+report\s+(?:directly\s+)?to\b",
            "manager",
        ),
        "relationship.healthcare_provider_for": (
            r"\b(?:doctor|physician|cardiologist|psychiatrist|psychologist|therapist)\b",
            "doctor",
        ),
        "relationship.plan_helper_for": (
            r"\bhelps?\s+me\s+manage\s+(?:my\s+)?(?:LifeSwitch\s+)?plan\b",
            "plan_helper",
        ),
        "relationship.teacher_of": (
            r"\bteaches\s+(?:me|my\s+[^.!?]{1,80}(?:course|class))\b",
            "teacher",
        ),
        "social.avoids": (r"\bI\s+(?:used\s+to\s+)?avoid\b", "person_avoided"),
        "social.experiences_tension_with": (
            r"\b(?:ongoing\s+)?tension\s+between\b",
            "person_in_tension",
        ),
    }
    if predicate == "relationship.manager_of" and re.search(
        r"\b[A-Z][A-Za-z'’\-]{1,79}\s+reports?\s+"
        r"(?:directly\s+)?to\s+me\b",
        text,
        re.IGNORECASE,
    ):
        return "direct_report"
    rule = source_rules.get(predicate)
    if rule is None or re.search(rule[0], text, re.IGNORECASE) is None:
        return None
    return rule[1]


def relationship_has_explicit_historical_end(text: str) -> bool:
    return HISTORICAL_RELATIONSHIP_END_RE.search(text) is not None


def predicate_supported_by_source(
    predicate: str,
    named_party_role: str,
    text: str,
    *,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> bool:
    if predicate == "relationship.sibling_of" and re.search(
        r"\blike\s+(?:a\s+)?(?:brother|sister|sibling)\b|"
        r"\bnot\s+related\b|"
        r"\bmy\s+(?:wife|husband|spouse)['’]s\s+"
        r"(?:brother|sister)\b|"
        r"\b(?:brother|sister)[\s-]+in[\s-]+law\b",
        text,
        re.IGNORECASE,
    ):
        return False
    if predicate == "relationship.training_partner_of" and re.search(
        r"\bnot\s+(?:my\s+)?(?:workout|training|lifting)\s+partner\b",
        text,
        re.IGNORECASE,
    ):
        return False
    if predicate == "relationship.manager_of" and re.search(
        r"\bnot\s+my\s+(?:boss|manager|supervisor)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    if predicate == "social.trusts" and re.search(
        r"\b(?:do\s+not|don['’]?t|no\s+longer)\s+trust\b",
        text,
        re.IGNORECASE,
    ):
        return False
    special = PREDICATE_EVIDENCE_PATTERNS.get(predicate)
    if special is not None and re.search(special, text, re.IGNORECASE):
        return True
    decision = map_named_party_role(
        named_party_role,
        proposed_predicate=predicate,
        registry_path=registry_path,
    )
    return decision.status == "accept" and _role_supported(named_party_role, text)


def relationship_predicate_candidates_from_source(
    text: str,
    relationship_role: Any,
    *,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> tuple[str, ...]:
    explicit = sorted(
        predicate
        for predicate, pattern in PREDICATE_EVIDENCE_PATTERNS.items()
        if re.search(pattern, text, re.IGNORECASE)
        and not (
            predicate == "social.trusts"
            and re.search(
                r"\b(?:do\s+not|don['’]?t|no\s+longer)\s+trust\b",
                text,
                re.IGNORECASE,
            )
        )
    )
    if explicit:
        return tuple(explicit)
    for role in relationship_role_candidates(relationship_role):
        predicates = {
            mapping.predicate
            for mapping in role_index(registry_path).get(role, ())
            if predicate_supported_by_source(
                mapping.predicate,
                role,
                text,
                registry_path=registry_path,
            )
        }
        if predicates:
            return tuple(sorted(predicates))
    return ()


def _defer(reason_code: str) -> ObservationDecision:
    return ObservationDecision(
        status="defer",
        reason_code=reason_code,
        policy_version=POLICY_VERSION,
        normalized_observation=None,
        repairs=(),
        manual_review_required=True,
    )


def normalize_relationship_observation(
    observation: Mapping[str, Any],
    entity_mentions: Sequence[Mapping[str, Any]],
    text: str,
    *,
    source_class: str,
    explicit_current_state: bool = True,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> ObservationDecision:
    predicate = str(observation.get("predicate", ""))
    registry = load_registry(registry_path)
    contracts = {row["predicate"]: row for row in registry["predicates"]}
    if predicate not in contracts or predicate in registry["forbidden_predicates"]:
        return _defer("unregistered_relationship_predicate")
    obj = observation.get("object")
    if not isinstance(obj, Mapping) or obj.get("kind") != "entity":
        return _defer("relationship_object_must_be_entity")
    subject_ref = observation.get("subject_entity_ref")
    object_ref = obj.get("entity_ref")
    entities = {
        item.get("entity_ref"): item
        for item in entity_mentions
        if isinstance(item, Mapping) and isinstance(item.get("entity_ref"), str)
    }
    if subject_ref not in entities or object_ref not in entities:
        return _defer("relationship_entity_reference_missing")
    if subject_ref == object_ref:
        return _defer("relationship_self_loop_forbidden")
    subject = entities[subject_ref]
    object_entity = entities[object_ref]
    endpoint_values = [subject, object_entity]
    self_entities = [value for value in endpoint_values if value.get("entity_type") == "self"]
    if len(self_entities) != 1:
        return _defer(
            "third_party_relationship_requires_manual_resolution"
            if len(self_entities) == 0
            else "invalid_self_endpoint_count"
        )
    self_entity = self_entities[0]
    named_entity = object_entity if subject is self_entity else subject
    context = _sentence_context(observation.get("source_spans"), text)
    source_directed_role = directed_role_supported_by_source(predicate, context)
    role_decision = (
        map_named_party_role(
            source_directed_role,
            proposed_predicate=predicate,
            registry_path=registry_path,
        )
        if source_directed_role is not None
        else map_entity_relationship_role(
            named_entity.get("relationship_role"),
            proposed_predicate=predicate,
            registry_path=registry_path,
        )
    )
    if role_decision.status != "accept" or role_decision.mapping is None:
        return _defer(role_decision.reason_code)
    role = source_directed_role or next(
        (
            candidate
            for candidate in relationship_role_candidates(
                named_entity.get("relationship_role")
            )
            if candidate in role_index(registry_path)
            and any(
                mapping.predicate == predicate
                and mapping.edge_direction == role_decision.mapping.edge_direction
                for mapping in role_index(registry_path)[candidate]
            )
        ),
        "",
    )
    nested_possessive = NESTED_POSSESSIVE_RELATION_RE.search(context)
    explicitly_governed_nested_relation = any(
        assertion.predicate == predicate
        for assertion in explicit_relationship_assertions(context)
    )
    if nested_possessive and not explicitly_governed_nested_relation:
        return _defer("relationship_belongs_to_third_party")
    if not role or not predicate_supported_by_source(
        predicate, role, context, registry_path=registry_path
    ):
        return _defer("relationship_predicate_not_entailed_by_source")

    direction = role_decision.mapping.edge_direction
    if direction == "named_to_self":
        canonical_subject, canonical_object = named_entity, self_entity
    elif direction in {"self_to_named", "unordered_self_named"}:
        canonical_subject, canonical_object = self_entity, named_entity
    else:
        return _defer("relationship_direction_unresolved")

    proposal = assess_relationship_proposal(
        predicate=predicate,
        named_party_role=role,
        source_class=source_class,
        subject_entity_type=str(canonical_subject.get("entity_type", "")),
        object_entity_type=str(canonical_object.get("entity_type", "")),
        self_endpoint_count=1,
        explicit_current_state=explicit_current_state,
        third_party_edge=False,
        registry_path=registry_path,
    )
    if proposal.status == "defer":
        return _defer(proposal.reason_code)

    normalized = copy.deepcopy(dict(observation))
    repairs: list[str] = []
    if normalized.get("subject_entity_ref") != canonical_subject.get("entity_ref"):
        normalized["subject_entity_ref"] = canonical_subject.get("entity_ref")
        repairs.append("canonical_relationship_direction")
    normalized_object = normalized.get("object")
    if not isinstance(normalized_object, dict):
        return _defer("relationship_object_must_be_entity")
    if normalized_object.get("entity_ref") != canonical_object.get("entity_ref"):
        normalized_object["entity_ref"] = canonical_object.get("entity_ref")
        repairs.append("canonical_relationship_direction")

    contract = contracts[predicate]
    minimum = contract["sensitivity_floor"]
    current = str(normalized.get("sensitivity", ""))
    if current not in SENSITIVITY_ORDER:
        return _defer("relationship_sensitivity_invalid")
    if SENSITIVITY_ORDER[current] < SENSITIVITY_ORDER[minimum]:
        normalized["sensitivity"] = minimum
        repairs.append("relationship_sensitivity_floor")
    required_surface = SURFACE_POLICY_ADAPTER[contract["surface_policy"]]
    if normalized.get("surface_policy") != required_surface:
        normalized["surface_policy"] = required_surface
        repairs.append("relationship_surface_policy")

    if predicate == "social.no_contact_with" and re.search(
        PREDICATE_EVIDENCE_PATTERNS[predicate], context, re.IGNORECASE
    ):
        if normalized.get("polarity") != "affirmed":
            normalized["polarity"] = "affirmed"
            repairs.append("lexical_state_polarity")
        if normalized.get("modality") not in {
            "reported_observation",
            "uncertain",
            "corrective",
        }:
            normalized["modality"] = "reported_observation"
            repairs.append("lexical_state_modality")

    if contract["family"] == "relational_state":
        modality = normalized.get("modality")
        if modality == "asserted":
            normalized["modality"] = "reported_observation"
            repairs.append("owner_perspective_modality")
        elif modality not in {"reported_observation", "uncertain", "corrective"}:
            return _defer("relational_state_modality_invalid")
    elif normalized.get("modality") not in {
        "asserted",
        "reported_observation",
        "uncertain",
        "corrective",
    }:
        return _defer("relationship_modality_invalid")
    elif normalized.get("modality") == "reported_observation":
        normalized["modality"] = "asserted"
        repairs.append("direct_relationship_assertion_modality")

    if normalized.get("polarity") != "affirmed":
        return ObservationDecision(
            status="manual_review",
            reason_code="negated_relationship_requires_reconciliation",
            policy_version=POLICY_VERSION,
            normalized_observation=normalized,
            repairs=tuple(sorted(set(repairs))),
            manual_review_required=True,
        )
    temporal = normalized.get("temporal")
    if not isinstance(temporal, dict):
        return _defer("relationship_temporal_contract_missing")
    if temporal.get("semantic") != "state_validity":
        temporal["semantic"] = "state_validity"
        repairs.append("relationship_temporal_semantic")
    if (
        relationship_has_explicit_historical_end(context)
        and not (
            temporal.get("shape") == "bounded_interval"
            or (
                temporal.get("shape") == "open_interval"
                and (
                    (
                        isinstance(temporal.get("instant_range"), dict)
                        and temporal["instant_range"].get("lower") is None
                        and temporal["instant_range"].get("upper") is not None
                    )
                    or (
                        isinstance(temporal.get("calendar_range"), dict)
                        and temporal["calendar_range"].get("lower") is None
                        and temporal["calendar_range"].get("upper") is not None
                    )
                )
            )
        )
    ):
        temporal.update(
            {
                "semantic": "state_validity",
                "shape": "open_interval",
                "basis": "instant",
                "source_form": "implicit_source_time",
                "certainty": "bounded",
                "precision": "exact",
                "instant": None,
                "calendar_range": None,
                "instant_range": {
                    "lower": None,
                    "upper": None,
                    "bounds": "[)",
                },
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": False,
            }
        )
        temporal_reasons = temporal.get("reason_codes")
        if (
            isinstance(temporal_reasons, list)
            and "historical_relationship_ended_before_source" not in temporal_reasons
            and len(temporal_reasons) < 10
        ):
            temporal_reasons.append("historical_relationship_ended_before_source")
        repairs.append("historical_relationship_upper_bounded_by_source")
    relationship_policy = contract.get("relationship_policy")
    temporal_profile = contract.get("temporal_profile")
    if temporal_profile is None and isinstance(relationship_policy, Mapping):
        temporal_profile = relationship_policy.get("temporal_profile")
    dynamic_current = (
        temporal_profile == "dynamic_state"
        and explicit_current_state
        and DYNAMIC_CURRENT_STATE_RE.search(context) is not None
    )
    if dynamic_current and temporal.get("shape") != "open_interval":
        if (
            temporal.get("basis") == "calendar"
            and isinstance(temporal.get("calendar_range"), dict)
        ):
            temporal["shape"] = "open_interval"
            temporal["calendar_range"]["upper"] = None
        elif (
            temporal.get("basis") == "instant"
            and isinstance(temporal.get("instant_range"), dict)
        ):
            temporal["shape"] = "open_interval"
            temporal["instant_range"]["upper"] = None
        else:
            temporal.update(
                {
                    "shape": "none",
                    "basis": "none",
                    "source_form": "implicit_source_time",
                    "certainty": "unknown",
                    "precision": "unknown",
                    "instant": None,
                    "calendar_range": None,
                    "instant_range": None,
                    "relative_offset": None,
                    "recurrence": None,
                    "anchored_to_source_time": False,
                }
            )
        repairs.append("dynamic_relationship_state_opened")
    reasons = normalized.get("reason_codes")
    if isinstance(reasons, list) and "relationship_v5_1_governed" not in reasons:
        reasons.append("relationship_v5_1_governed")

    historical_review = (
        re.search(
            r"\buntil\s+(?:he|she|they|the\s+person)?\s*"
            r"(?:died|passed\s+away)\b",
            context,
            re.IGNORECASE,
        ) is not None
        and "relationship_status_temporal_language"
        in set(contract.get("manual_review_rules", []))
    )
    manual_review_required = (
        proposal.manual_review_required or historical_review
    )
    return ObservationDecision(
        status="manual_review" if manual_review_required else proposal.status,
        reason_code=(
            "relationship_manual_review_required"
            if manual_review_required
            else proposal.reason_code
        ),
        policy_version=POLICY_VERSION,
        normalized_observation=normalized,
        repairs=tuple(sorted(set(repairs))),
        manual_review_required=manual_review_required,
    )
