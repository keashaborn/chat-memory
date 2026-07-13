from __future__ import annotations

import hashlib
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import asyncpg

from .memory_v1_artifacts import ArtifactManifestEntry
from .memory_v1_store import actor_uuid


SOURCE_SYSTEM = "public.chat_log"
SOURCE_ROLE = "frontend/chat:user"
MAX_CUES_PER_FAMILY = 6

QUESTION_START_RE = re.compile(
    r"^\s*(?:who|what|when|where|why|how|do|does|did|can|could|would|should|"
    r"is|are|was|were|have|has|will|which|any|if you|tell me|describe|explain)\b",
    re.IGNORECASE,
)
QUESTION_RECALL_RE = re.compile(
    r"\b(?:do you remember|what do you remember|did i tell you|have i told you|"
    r"what happened with|what happened to|what type of .* do you think i|"
    r"can you tell me about (?:my|what i)|any details (?:about|on) my|"
    r"what was the correct (?:spelling|name|word|value|number))\b",
    re.IGNORECASE,
)
FIRST_PERSON_RE = re.compile(r"\b(?:i|i['’]m|i['’]ve|i['’]d|my|mine|we|our)\b", re.IGNORECASE)
REQUEST_START_RE = re.compile(
    r"^\s*(?:please|could you|can you|would you|will you|"
    r"i would like to (?:ask|have you|know)|let me know|tell me|write|make|give me|help me)\b",
    re.IGNORECASE,
)

CORRECTION_PATTERNS = (
    re.compile(r"\bthe correct (?:spelling|name|word|value|number)\b", re.IGNORECASE),
    re.compile(r"\b(?:it|that|this|the name) should be\b.{0,100}\bnot\b", re.IGNORECASE),
    re.compile(r"\bi (?:meant|misspoke|misstated)\b", re.IGNORECASE),
    re.compile(r"\b(?:spell ?check|transcription) (?:changed|error|mistake)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+[A-Z][\w'-]{1,40}\s*[,;—-]+\s*(?:it(?:'s| is)\s+)?[A-Z][\w'-]{1,40}\b"),
)

RESPONSE_PREFERENCE_PATTERNS = (
    re.compile(r"\bhow i want (?:you|the ai|verbal sage) to respond\b", re.IGNORECASE),
    re.compile(r"\bi (?:want|would like|prefer) (?:you|the ai|verbal sage) to\b", re.IGNORECASE),
    re.compile(r"\b(?:your|the ai['’]s|verbal sage['’]s) (?:response|responses|tone|style) should\b", re.IGNORECASE),
    re.compile(r"\bdo not (?:simulate|inject|surface|lecture|moralize|over[- ]?explain)\b", re.IGNORECASE),
    re.compile(r"\bplease (?:do not|don['’]t) (?:make|give|offer|include)\b", re.IGNORECASE),
    re.compile(r"\b(?:do not|don['’]t) make suggestions\b", re.IGNORECASE),
    re.compile(r"\bon this app i (?:mainly )?(?:want|prefer)\b", re.IGNORECASE),
    re.compile(r"\b(?:concise|direct|technical|minimal filler|no bullet points|prose over bullets)\b", re.IGNORECASE),
)

PREFERENCE_GOAL_PATTERNS = (
    re.compile(r"\bi (?:prefer|like|don['’]t like|do not like|value|care about)\b", re.IGNORECASE),
    re.compile(r"\bmy (?:goal|priority|preference|plan|intention) is\b", re.IGNORECASE),
    re.compile(r"\b(?:my ultimate goal|eventually i (?:want|would like)|i want to build|i['’]m building|i am building)\b", re.IGNORECASE),
)

PROJECT_PATTERNS = (
    re.compile(r"\b(?:verbal sage|life ?switch|memory system|memory architecture|memory v1)\b", re.IGNORECASE),
    re.compile(r"\b(?:backend|front ?end|qdrant|postgres|supabase|rag|prompt injection|vector database)\b", re.IGNORECASE),
    re.compile(r"\b(?:the app|this app|our app|project roadmap|project plan|admin ui)\b", re.IGNORECASE),
)

STRUCTURED_PATTERNS = (
    re.compile(r"\b(?:macro|macros|protein|carbs?|carbohydrates?|fat intake|calories?|nutrition)\b", re.IGNORECASE),
    re.compile(r"\b(?:meal plan|meal log|food log|serving|nutrient)\b", re.IGNORECASE),
    re.compile(r"\b(?:workout|training session|exercise log|sets? and reps?|conditioning|body ?weight)\b", re.IGNORECASE),
)

STRUCTURED_OPERATION_PATTERNS = (
    re.compile(
        r"\b(?:my|our) (?:macros?|calories?|nutrition|meals?|meal plan|"
        r"workouts?|training(?: sessions?)?|exercise logs?|body ?weight)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi(?:['’]ve| have|['’]m| am) (?:been )?"
        r"(?:sticking|tracking|logging|eating|training|working out)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:see|access|show|check|analy[sz]e|review|compare|track|log)"
        r".{0,80}\b(?:my|our|recent|daily|weekly).{0,50}"
        r"\b(?:macros?|calories?|nutrition|meals?|workouts?|training|weight)\b",
        re.IGNORECASE,
    ),
)

PERSONAL_HISTORY_PATTERNS = (
    re.compile(r"\bi (?:was born|grew up|graduated|founded|retired|married)\b", re.IGNORECASE),
    re.compile(r"\bi worked (?:at|for|as|in)\b", re.IGNORECASE),
    re.compile(r"\bi moved (?:to|from)\b", re.IGNORECASE),
    re.compile(r"\bi went to (?:school|college|university|.+ institute)\b", re.IGNORECASE),
    re.compile(r"\b(?:when i was|after (?:high school|college)|as a child|growing up)\b", re.IGNORECASE),
    re.compile(r"\bi (?:have )?(?:spent|used to|remember|consider myself|never left behind|had realized)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:[A-Z][\w'-]{1,40} is my|my (?:wife|husband|mother|mom|father|dad|"
        r"sister|brother|daughter|son)['’]s name is) "
        r"(?:wife|husband|mother|mom|father|dad|sister|brother|daughter|son|[A-Z][\w'-]{1,40})\b"
    ),
    re.compile(r"\b(?:my|our) .{0,60}\b(?:died|passed away|was euthanized|put .* to sleep|was lost)\b", re.IGNORECASE),
    re.compile(
        r"\bmy (?:wife|husband|mother|mom|father|dad|sister|brother|daughter|son)"
        r" .{0,50}\b(?:had a psychotic break|has dementia|had dementia|lives in|"
        r"is in assisted living)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bi(?: am|['’]m) (?:a |an )?(?:clinical psychologist|psychologist|bcba|retired|founder|developer|blue[- ]collar)\b", re.IGNORECASE),
    re.compile(r"\bi (?:have|had) (?:two|three|four|a |an )?(?:dogs?|cats?|sisters?|brothers?|children|degrees?)\b", re.IGNORECASE),
)

TEMPORARY_PATTERNS = (
    re.compile(r"\b(?:today|tonight|this morning|this afternoon|yesterday|right now|currently|lately|recently)\b", re.IGNORECASE),
    re.compile(r"\b(?:the last few (?:days|weeks)|this week|for now|at the moment)\b", re.IGNORECASE),
    re.compile(r"\bi(?:['’]m| am) (?:tired|heading off|going to bed|taking off|stuck|working on .* right now)\b", re.IGNORECASE),
    re.compile(r"\bi(?:['’]m| am) struggling\b", re.IGNORECASE),
    re.compile(r"\b(?:has|have|it['’]s|it is) been (?:difficult|hard|rough)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:i(?:['’]m| am)|we(?:['’]re| are)|is|are) "
        r"(?:laying|lying|sitting|watching|sprawled|sleeping)\b",
        re.IGNORECASE,
    ),
)


class EvidenceTriageError(RuntimeError):
    pass


class EvidenceSourceConflict(EvidenceTriageError):
    pass


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TriageCursor:
    created_at: datetime
    source_id: uuid.UUID


@dataclass(frozen=True)
class TriageSourceRow:
    source_id: uuid.UUID
    owner_user_id: uuid.UUID
    source: str
    text: str
    created_at: datetime
    thread_id: Optional[uuid.UUID]
    vantage_id: Optional[str]
    request_id: Optional[str]


@dataclass(frozen=True)
class ExistingEvidence:
    evidence_id: uuid.UUID
    content_sha256: Optional[str]
    status: str


@dataclass(frozen=True)
class CueSpan:
    family: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class TriageDecision:
    source: TriageSourceRow
    content_sha256: str
    independence_key: str
    duplicate_of_source_id: Optional[uuid.UUID]
    content_authorship: str
    primary_lane: str
    lanes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    cue_spans: tuple[CueSpan, ...]
    question_only: bool
    requires_atomic_split: bool
    evidence_action: str
    candidate_action: str
    candidate_target: Optional[str]
    existing_evidence_id: Optional[uuid.UUID]
    artifact_title: Optional[str]
    request_like: bool


def _find_cues(
    text: str,
    family: str,
    patterns: Sequence[re.Pattern[str]],
) -> list[CueSpan]:
    found: list[CueSpan] = []
    seen: set[tuple[int, int]] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            key = (match.start(), match.end())
            if key in seen:
                continue
            seen.add(key)
            found.append(
                CueSpan(
                    family=family,
                    start=match.start(),
                    end=match.end(),
                    text=match.group(0)[:160],
                )
            )
            if len(found) >= MAX_CUES_PER_FAMILY:
                return sorted(found, key=lambda item: (item.start, item.end))
    return sorted(found, key=lambda item: (item.start, item.end))


def _looks_question_only(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    starts_as_question = bool(QUESTION_START_RE.search(stripped))
    question_marks = stripped.count("?")
    paragraphs = len([part for part in re.split(r"\n\s*\n", stripped) if part.strip()])
    return starts_as_question and question_marks > 0 and paragraphs <= 2 and len(stripped) <= 700


def _cue_is_in_question(text: str, cue: CueSpan) -> bool:
    sentence_start = max(
        text.rfind(".", 0, cue.start),
        text.rfind("!", 0, cue.start),
        text.rfind("?", 0, cue.start),
        text.rfind("\n", 0, cue.start),
    ) + 1
    endings = [
        position
        for position in (
            text.find(".", cue.end),
            text.find("!", cue.end),
            text.find("?", cue.end),
            text.find("\n", cue.end),
        )
        if position >= 0
    ]
    sentence_end = min(endings) + 1 if endings else len(text)
    sentence = text[sentence_start:sentence_end].strip()
    return "?" in sentence or (
        bool(QUESTION_START_RE.search(sentence)) and sentence.endswith("?")
    )


def _declarative_cues(text: str, cues: Sequence[CueSpan]) -> list[CueSpan]:
    return [cue for cue in cues if not _cue_is_in_question(text, cue)]


def _looks_mixed_or_quoted(text: str) -> bool:
    if len(text) < 800:
        return False
    indicators = (
        text.count("\n#") > 0,
        text.count("\n*") >= 3,
        text.count("\n1.") > 0,
        bool(re.search(r"\b(?:this was the response|here['’]s what we did|checkpoint)\b", text, re.IGNORECASE)),
    )
    return sum(bool(value) for value in indicators) >= 1


def classify_source(
    source: TriageSourceRow,
    *,
    artifact_entries: Mapping[uuid.UUID, ArtifactManifestEntry],
    existing_evidence: Mapping[uuid.UUID, ExistingEvidence],
    duplicate_of_source_id: Optional[uuid.UUID] = None,
) -> TriageDecision:
    if source.source != SOURCE_ROLE:
        raise EvidenceSourceConflict(
            f"unsupported source role for {source.source_id}: {source.source}"
        )
    if not source.text.strip():
        raise EvidenceSourceConflict(f"empty source text: {source.source_id}")

    content_hash = sha256_text(source.text)
    existing = existing_evidence.get(source.source_id)
    if existing and existing.content_sha256 != content_hash:
        raise EvidenceSourceConflict(
            f"stored evidence hash mismatch for source {source.source_id}"
        )

    artifact_entry = artifact_entries.get(source.source_id)
    if artifact_entry:
        if artifact_entry.expected_sha256 != content_hash:
            raise EvidenceSourceConflict(
                f"artifact manifest hash mismatch for source {source.source_id}"
            )
        return TriageDecision(
            source=source,
            content_sha256=content_hash,
            independence_key=f"chat_content_sha256:{content_hash}",
            duplicate_of_source_id=duplicate_of_source_id,
            content_authorship=artifact_entry.authorship,
            primary_lane="artifact_archive",
            lanes=("artifact_archive",),
            reason_codes=(
                "hash_locked_artifact_manifest",
                f"artifact_kind:{artifact_entry.artifact_kind}",
                f"document_state:{artifact_entry.document_state}",
            ),
            cue_spans=(),
            question_only=False,
            requires_atomic_split=False,
            evidence_action="reuse_existing" if existing else "record_immutable",
            candidate_action="artifact_archive_only",
            candidate_target=None,
            existing_evidence_id=existing.evidence_id if existing else None,
            artifact_title=artifact_entry.title,
            request_like=False,
        )

    cue_groups = {
        "correction": _find_cues(source.text, "correction", CORRECTION_PATTERNS),
        "response_preference": _find_cues(
            source.text, "response_preference", RESPONSE_PREFERENCE_PATTERNS
        ),
        "preference_goal": _find_cues(
            source.text, "preference_goal", PREFERENCE_GOAL_PATTERNS
        ),
        "project": _find_cues(source.text, "project", PROJECT_PATTERNS),
        "structured": _find_cues(source.text, "structured", STRUCTURED_PATTERNS),
        "personal_history": _find_cues(
            source.text, "personal_history", PERSONAL_HISTORY_PATTERNS
        ),
        "temporary": _find_cues(source.text, "temporary", TEMPORARY_PATTERNS),
    }
    question_only = _looks_question_only(source.text)
    recall_query = bool(QUESTION_RECALL_RE.search(source.text)) or (
        question_only and bool(re.search(r"\b(?:remember|recall|my (?:mom|mother|dad|father|pet|history))\b", source.text, re.IGNORECASE))
    )
    first_person = bool(FIRST_PERSON_RE.search(source.text))
    request_like = bool(REQUEST_START_RE.search(source.text))
    declarative_cues = {
        family: _declarative_cues(source.text, cue_groups[family])
        for family in (
            "correction",
            "response_preference",
            "preference_goal",
            "personal_history",
        )
    }
    structured_operation = bool(
        cue_groups["structured"]
        and any(pattern.search(source.text) for pattern in STRUCTURED_OPERATION_PATTERNS)
    )

    lanes: list[str] = []
    reasons: list[str] = []
    if declarative_cues["correction"]:
        lanes.append("explicit_correction")
        reasons.append("declarative_correction_cue")
    if declarative_cues["response_preference"] and first_person:
        lanes.append("response_preference")
        reasons.append("first_person_response_policy_cue")
    if structured_operation:
        lanes.append("structured_data_reference")
        reasons.append("nutrition_or_training_domain_cue")
    if cue_groups["project"]:
        lanes.append("technical_project")
        reasons.append("project_or_system_domain_cue")
    if declarative_cues["personal_history"] and first_person:
        lanes.append("personal_history")
        reasons.append("first_person_stable_history_cue")
    if declarative_cues["preference_goal"] and first_person:
        lanes.append("preference_or_goal")
        reasons.append("first_person_preference_or_goal_cue")
    if cue_groups["temporary"]:
        lanes.append("temporary_context")
        reasons.append("time_bounded_context_cue")
    if recall_query:
        lanes.append("recall_query")
        reasons.append("question_or_presupposition_not_new_assertion")
    if not lanes:
        lanes.append("general_conversation")
        reasons.append("no_durable_or_routed_signal")

    precedence = (
        "explicit_correction",
        "response_preference",
        "structured_data_reference",
        "technical_project",
        "personal_history",
        "preference_or_goal",
        "temporary_context",
        "recall_query",
        "general_conversation",
    )
    primary_lane = next(lane for lane in precedence if lane in lanes)

    if primary_lane == "explicit_correction":
        candidate_action = "propose_atomic_candidate"
        candidate_target = "claim"
    elif primary_lane == "response_preference":
        candidate_action = "propose_preference_candidate"
        candidate_target = "preference"
    elif primary_lane == "structured_data_reference":
        candidate_action = "route_structured_adapter"
        candidate_target = None
    elif primary_lane == "technical_project":
        if question_only or recall_query or request_like:
            candidate_action = "retain_raw_only"
            candidate_target = None
        else:
            candidate_action = "project_evidence_review"
            candidate_target = "project_knowledge"
    elif primary_lane == "personal_history":
        candidate_action = "propose_atomic_candidate"
        candidate_target = "claim"
    elif primary_lane == "preference_or_goal":
        candidate_action = "propose_preference_candidate"
        candidate_target = "preference"
    elif primary_lane == "temporary_context":
        candidate_action = "retain_evidence_only"
        candidate_target = None
    else:
        candidate_action = "retain_raw_only"
        candidate_target = None

    substantive_lanes = [
        lane
        for lane in lanes
        if lane not in {"temporary_context", "recall_query", "general_conversation"}
    ]
    paragraph_count = len(
        [part for part in re.split(r"\n\s*\n", source.text.strip()) if part.strip()]
    )
    sentence_count = len(
        [
            part
            for part in re.split(r"(?:[.!?]+(?:\s+|$)|\n+)", source.text.strip())
            if part.strip()
        ]
    )
    requires_split = (
        candidate_target is not None
        and (
            len(source.text) > 600
            or sentence_count > 2
            or paragraph_count > 3
            or len(substantive_lanes) > 1
        )
    )
    if requires_split:
        candidate_action = "split_before_candidate"
        reasons.append("mixed_or_long_turn_requires_atomic_spans")
    if question_only:
        reasons.append("question_only")
    if request_like:
        if primary_lane == "response_preference":
            reasons.append("request_form_contains_response_policy")
        elif candidate_target is None:
            reasons.append("instruction_or_request_not_new_assertion")
        else:
            reasons.append("request_form_detected")
    if duplicate_of_source_id:
        reasons.append("duplicate_content_same_independence_key")

    cues = sorted(
        (cue for group in cue_groups.values() for cue in group),
        key=lambda item: (item.start, item.end, item.family),
    )
    authorship = "mixed_or_quoted" if _looks_mixed_or_quoted(source.text) else "user"
    return TriageDecision(
        source=source,
        content_sha256=content_hash,
        independence_key=f"chat_content_sha256:{content_hash}",
        duplicate_of_source_id=duplicate_of_source_id,
        content_authorship=authorship,
        primary_lane=primary_lane,
        lanes=tuple(lanes),
        reason_codes=tuple(dict.fromkeys(reasons)),
        cue_spans=tuple(cues),
        question_only=question_only,
        requires_atomic_split=requires_split,
        evidence_action="reuse_existing" if existing else "record_immutable",
        candidate_action=candidate_action,
        candidate_target=candidate_target,
        existing_evidence_id=existing.evidence_id if existing else None,
        artifact_title=None,
        request_like=request_like,
    )


async def fetch_triage_batch_readonly(
    conn: asyncpg.Connection,
    owner_user_id: str | uuid.UUID,
    *,
    after: Optional[TriageCursor] = None,
    limit: int = 1000,
) -> tuple[list[TriageSourceRow], Dict[uuid.UUID, ExistingEvidence]]:
    owner = actor_uuid(owner_user_id)
    if not 1 <= limit <= 5000:
        raise EvidenceTriageError("limit must be between 1 and 5000")

    async with conn.transaction(readonly=True):
        await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))
        if after:
            rows = await conn.fetch(
                """
                SELECT id, user_id, source, text, created_at, thread_id, vantage_id, request_id
                FROM public.chat_log
                WHERE user_id=$1
                  AND source=$2
                  AND (created_at, id) > ($3::timestamptz, $4::uuid)
                ORDER BY created_at, id
                LIMIT $5
                """,
                str(owner),
                SOURCE_ROLE,
                after.created_at,
                after.source_id,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, user_id, source, text, created_at, thread_id, vantage_id, request_id
                FROM public.chat_log
                WHERE user_id=$1
                  AND source=$2
                ORDER BY created_at, id
                LIMIT $3
                """,
                str(owner),
                SOURCE_ROLE,
                limit,
            )

        source_ids = [uuid.UUID(str(row["id"])) for row in rows]
        evidence_rows = []
        if source_ids:
            evidence_rows = await conn.fetch(
                """
                SELECT evidence_id, external_id, content_sha256, status::text
                FROM memory.evidence
                WHERE owner_user_id=$1
                  AND source_system=$2
                  AND external_id = ANY($3::text[])
                """,
                owner,
                SOURCE_SYSTEM,
                [str(source_id) for source_id in source_ids],
            )

    sources: list[TriageSourceRow] = []
    for row in rows:
        row_owner = actor_uuid(row["user_id"])
        if row_owner != owner:
            raise EvidenceSourceConflict("query returned a cross-owner source row")
        sources.append(
            TriageSourceRow(
                source_id=uuid.UUID(str(row["id"])),
                owner_user_id=row_owner,
                source=str(row["source"]),
                text=str(row["text"] or ""),
                created_at=row["created_at"],
                thread_id=uuid.UUID(str(row["thread_id"])) if row["thread_id"] else None,
                vantage_id=str(row["vantage_id"]) if row["vantage_id"] else None,
                request_id=str(row["request_id"]) if row["request_id"] else None,
            )
        )

    existing: Dict[uuid.UUID, ExistingEvidence] = {}
    for row in evidence_rows:
        try:
            source_id = uuid.UUID(str(row["external_id"]))
        except ValueError as exc:
            raise EvidenceSourceConflict("evidence external_id is not a UUID") from exc
        existing[source_id] = ExistingEvidence(
            evidence_id=uuid.UUID(str(row["evidence_id"])),
            content_sha256=str(row["content_sha256"]) if row["content_sha256"] else None,
            status=str(row["status"]),
        )
    return sources, existing


def triage_sources(
    sources: Sequence[TriageSourceRow],
    *,
    artifact_entries: Mapping[uuid.UUID, ArtifactManifestEntry],
    existing_evidence: Mapping[uuid.UUID, ExistingEvidence],
) -> list[TriageDecision]:
    first_by_hash: Dict[str, uuid.UUID] = {}
    decisions: list[TriageDecision] = []
    for source in sources:
        content_hash = sha256_text(source.text)
        duplicate_of = first_by_hash.get(content_hash)
        if duplicate_of is None:
            first_by_hash[content_hash] = source.source_id
        decisions.append(
            classify_source(
                source,
                artifact_entries=artifact_entries,
                existing_evidence=existing_evidence,
                duplicate_of_source_id=duplicate_of,
            )
        )
    return decisions


def _counter_dict(values: Iterable[str]) -> Dict[str, int]:
    return dict(sorted(Counter(values).items()))


def triage_dry_run_report(
    decisions: Sequence[TriageDecision],
    *,
    owner_user_id: str | uuid.UUID,
    input_cursor: Optional[TriageCursor],
) -> Dict[str, Any]:
    owner = actor_uuid(owner_user_id)
    next_cursor = None
    if decisions:
        last = decisions[-1].source
        next_cursor = {
            "created_at": last.created_at.isoformat(),
            "source_id": str(last.source_id),
        }

    return {
        "mode": "dry_run_no_writes",
        "owner_user_id": str(owner),
        "source_system": SOURCE_SYSTEM,
        "source_role": SOURCE_ROLE,
        "source_count": len(decisions),
        "input_cursor": (
            {
                "created_at": input_cursor.created_at.isoformat(),
                "source_id": str(input_cursor.source_id),
            }
            if input_cursor
            else None
        ),
        "next_cursor": next_cursor,
        "summary": {
            "primary_lanes": _counter_dict(
                decision.primary_lane for decision in decisions
            ),
            "all_lanes": _counter_dict(
                lane for decision in decisions for lane in decision.lanes
            ),
            "candidate_actions": _counter_dict(
                decision.candidate_action for decision in decisions
            ),
            "evidence_actions": _counter_dict(
                decision.evidence_action for decision in decisions
            ),
            "existing_evidence": sum(
                decision.existing_evidence_id is not None for decision in decisions
            ),
            "duplicates": sum(
                decision.duplicate_of_source_id is not None for decision in decisions
            ),
            "requires_atomic_split": sum(
                decision.requires_atomic_split for decision in decisions
            ),
        },
        "controls": {
            "database_writes": 0,
            "candidate_writes": 0,
            "claim_writes": 0,
            "qdrant_writes": 0,
            "prompt_injection": False,
            "cursor_strategy": "keyset(created_at,id)",
            "recommended_index": (
                "CREATE INDEX CONCURRENTLY chat_log_memory_ingest_cursor_idx "
                "ON public.chat_log(user_id, created_at, id) "
                "WHERE source='frontend/chat:user'"
            ),
        },
        "sources": [
            {
                "source_id": str(decision.source.source_id),
                "created_at": decision.source.created_at.isoformat(),
                "thread_id": (
                    str(decision.source.thread_id)
                    if decision.source.thread_id
                    else None
                ),
                "content_sha256": decision.content_sha256,
                "independence_key": decision.independence_key,
                "duplicate_of_source_id": (
                    str(decision.duplicate_of_source_id)
                    if decision.duplicate_of_source_id
                    else None
                ),
                "character_count": len(decision.source.text),
                "content_authorship": decision.content_authorship,
                "primary_lane": decision.primary_lane,
                "lanes": list(decision.lanes),
                "reason_codes": list(decision.reason_codes),
                "question_only": decision.question_only,
                "request_like": decision.request_like,
                "requires_atomic_split": decision.requires_atomic_split,
                "evidence_action": decision.evidence_action,
                "existing_evidence_id": (
                    str(decision.existing_evidence_id)
                    if decision.existing_evidence_id
                    else None
                ),
                "candidate_action": decision.candidate_action,
                "candidate_target": decision.candidate_target,
                "artifact_title": decision.artifact_title,
                "cue_spans": [
                    {
                        "family": cue.family,
                        "start": cue.start,
                        "end": cue.end,
                        "text": cue.text,
                    }
                    for cue in decision.cue_spans
                ],
                "preview": " ".join(decision.source.text.split())[:220],
            }
            for decision in decisions
        ],
    }
