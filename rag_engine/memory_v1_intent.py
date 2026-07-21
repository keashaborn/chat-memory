from __future__ import annotations

import re
from typing import Any, Dict


VERSION = "memory_intent_adapter_v9"
PROJECT_KEY = "verbal-sage"
PROJECT_INTENTS = {
    "project_recall",
    "project_status",
    "project_planning",
    "project_decision",
}

LOSS_TERMS = ("died", "dying", "death", "dead", "passed away", "loss", "lost")
EVENT_RECALL_TERMS = (
    "what happened",
    "remember what happened",
    "what was it that happened",
    "remind me what happened",
)
NAME_TERMS = (
    "neko",
    "nemo",
    "correct name",
    "correct spelling",
    "spell the name",
    "name correction",
    "name should be",
)
FAMILY_SIGNAL_RE = re.compile(
    r"\b(?:family|family members?|relatives?|mother|mom|mum|father|dad|parents?|"
    r"wife|husband|spouse|partner|sons?|daughters?|children|brothers?|sisters?|"
    r"siblings?|cousins?|aunts?|uncles?|grandparents?|deedee)\b"
)
FAMILY_PROFILE_PREDICATES = (
    "identity.name",
    "life_event.died",
    "relationship.aunt_or_uncle_of",
    "relationship.cousin_of",
    "relationship.grandparent_of",
    "relationship.in_law_of",
    "relationship.parent_of",
    "relationship.relative_of",
    "relationship.romantic_partner_of",
    "relationship.sibling_of",
    "relationship.spouse_of",
)
FAMILY_DEATH_PREDICATES = (
    "identity.name",
    "life_event.died",
    "relationship.parent_of",
    "relationship.sibling_of",
    "relationship.spouse_of",
)
NAME_PREDICATES = (
    "identity.name",
    "identity.name_canonical",
)
LIFE_CONTEXT_PREDICATES = (
    "occupation.works_as",
    "preference.life",
    "relationship.caregiver_for",
)
HEALTH_BEHAVIOR_PREDICATES = (
    "health.user_reported_observation",
)
PET_SIGNAL_RE = re.compile(
    r"\b(?:pets?|dogs?|cats?|neko|nemo|dahlia|helsing)\b"
)
CAREGIVING_TERMS = (
    "caregiving",
    "caregiver",
    "caretaking",
    "caretaker",
    "psychotic break",
    "care burden",
    "caring for my wife",
    "caring for my spouse",
)
ALCOHOL_TERMS = (
    "alcohol",
    "drinking",
    "quit drinking",
    "stopped drinking",
    "sobriety",
    "sober",
)
RURAL_LIFE_TERMS = (
    "tractor",
    "tractors",
    "combine",
    "combines",
    "farm",
    "farming",
    "cattle",
    "beekeeping",
    "beekeeper",
    "bees",
    "llama",
    "llamas",
)
SUPPORT_NEED_TERMS = (
    "struggling",
    "difficult",
    "hard lately",
    "overwhelmed",
    "burned out",
    "burnt out",
    "need support",
    "need help",
    "help me",
    "how do i cope",
    "care burden",
)
NORMALIZATION_TERMS = (
    "correct",
    "correction",
    "should be",
    "not nemo",
    "spellcheck",
    "spell check",
    "voice-to-text",
    "voice to text",
    "transcription error",
)
MUSIC_TERMS = ("music", "song", "songs", "artist", "artists", "lyrics", "playlist")
OUTDOORS_TERMS = (
    "outdoors",
    "woods",
    "forest",
    "nature",
    "wildlife",
    "creatures",
)
RECOMMENDATION_TERMS = (
    "recommend",
    "recommendation",
    "suggest",
    "what should i listen",
    "what could i listen",
    "find me",
    "playlist for me",
)
PREFERENCE_RECALL_TERMS = (
    "do i like",
    "i like",
    "my taste",
    "my preference",
    "my preferences",
    "music preference",
    "music preferences",
    "i prefer",
    "do i prefer",
    "what kind of music",
    "what music do i",
    "remember what music",
    "my favorite artist",
    "my favorite singer",
    "favorite artist would be",
    "favorite singer would be",
    "what do i enjoy",
    "do i enjoy",
    "what do i find peaceful",
    "do i find peaceful",
    "my outdoor preference",
    "my outdoor preferences",
)
PROJECT_SIGNALS = (
    "verbal sage",
    "memory system",
    "memory architecture",
    "memory retrieval",
    "memory card",
    "preference card",
    "profile card",
    "feedback loop",
    "ab design",
    "a/b design",
    "background intervention",
    "website",
    "dataset",
    "backend",
    "frontend",
    "server",
    "service",
    "deployment",
    "branch",
    "worktree",
    "qdrant",
    "postgres",
    "supabase",
    "roadmap",
    "project",
)
PROJECT_APP_RE = re.compile(
    r"\b(?:(?:my|the|our|this|that)\s+app|app\s+or\s+website|website\s+or\s+app)\b"
)
PROJECT_DECISION_TERMS = (
    "decision",
    "decide",
    "decided",
    "approved",
    "authorize",
    "authorized",
    "ratified",
)
PROJECT_PLANNING_TERMS = (
    "next step",
    "what next",
    "plan",
    "planning",
    "roadmap",
    "continue",
    "implement",
    "design",
    "build",
    "should we",
    "need to do",
    "integrate",
    "integration",
    "add to the app",
    "include in the app",
    "thinking about",
    "considering",
    "fit into",
    "would fit",
    "could we",
    "how could",
    "add that",
)
PROJECT_STATUS_TERMS = (
    "status",
    "state",
    "where are we",
    "current",
    "progress",
    "direction",
    "summary",
    "summarize",
    "overview",
    "working",
    "done",
    "finished",
    "remaining",
    "branch",
    "worktree",
    "deployed",
    "running",
)
PROJECT_RECALL_TERMS = (
    "history",
    "historical",
    "how was",
    "how did",
    "used to",
    "before",
    "legacy",
    "old system",
)
SONG_IDENTIFICATION_TERMS = (
    "what song",
    "which song",
    "who sings",
    "who sang",
    "name that song",
    "name of the song",
    "song is this",
    "song stuck in my head",
)
PERSONAL_SONG_RECALL_TERMS = (
    "what song did i",
    "which song did i",
    "song i said",
    "song i told you",
    "my favorite song",
    "what songs do i",
    "remember what song",
    "remind me what song",
)
LEGACY_PERSONAL_MEMORY_MODES = {"on", "specific_recall_only", "off"}
PERSONAL_RECALL_CUE_RE = re.compile(
    r"\b(?:do you remember|what do you (?:know|remember)|do you know (?:anything )?about|"
    r"what (?:happened|was|is|did)|when did|which (?:pet|dog|cat|name)|was it|"
    r"remind me|tell me what|more particularly do you know)\b"
)
PERSONAL_ANCHOR_RE = re.compile(r"\b(?:my|mine|me|i)\b")
BROAD_PET_RECALL_RE = re.compile(
    r"\b(?:what do you (?:know|remember) about my (?:current )?pets|"
    r"do you know (?:anything )?about my (?:current )?pets|"
    r"tell me (?:what you know )?about my (?:current )?pets)\b"
)
BROAD_PARENT_RECALL_RE = re.compile(
    r"\b(?:what do you (?:know|remember) about my (?:mother|mom|mum|father|dad)|"
    r"do you know (?:anything )?about my (?:mother|mom|mum|father|dad)|"
    r"more particularly do you know (?:anything )?about my (?:mother|mom|mum|father|dad))\b"
)
FAMILY_PROFILE_RECALL_RE = re.compile(
    r"\b(?:(?:can you )?tell me (?:anything |what you (?:know|remember) )?about my "
    r"(?:family|family members?|relatives?)|"
    r"what do you (?:know|remember) about my (?:family|family members?|relatives?)|"
    r"do you (?:know|remember) (?:anything )?about my (?:family|family members?|relatives?)|"
    r"who (?:is|are) (?:in )?my family|"
    r"which (?:family members?|relatives?) do you (?:know|remember))\b"
)
FAMILY_MEMBER_RECALL_RE = re.compile(
    r"\b(?:(?:can you )?tell me (?:anything )?about my|"
    r"what do you (?:know|remember) about my|"
    r"do you (?:know|remember) (?:anything )?about my)\s+"
    r"(?:mother|mom|mum|father|dad|parent|wife|husband|spouse|partner|son|"
    r"daughter|child|brother|sister|sibling|cousin|aunt|uncle|grandparent)\b"
)
DIRECT_FAMILY_RELATION_RE = re.compile(
    r"\b(?:who (?:is|was|are|were) my "
    r"(?:mother|mom|mum|father|dad|parent|wife|husband|spouse|partner|son|"
    r"daughter|child|brother|sister|sibling|cousin|aunt|uncle|grandparent)|"
    r"do you (?:know|remember) who my "
    r"(?:mother|mom|mum|father|dad|parent|wife|husband|spouse|partner|son|"
    r"daughter|child|brother|sister|sibling|cousin|aunt|uncle|grandparent) "
    r"(?:is|was)|"
    r"how (?:is|was|are|were) [a-z][a-z .'-]{0,80} related to me|"
    r"what (?:is|was) (?:my relationship (?:to|with) [a-z][a-z .'-]{0,80}|"
    r"[a-z][a-z .'-]{0,80}(?:'s|’s) relationship to me)|"
    r"(?:is|was) [a-z][a-z .'-]{0,80} my "
    r"(?:mother|mom|mum|father|dad|parent|wife|husband|spouse|partner|son|"
    r"daughter|child|brother|sister|sibling|cousin|aunt|uncle|grandparent))\b"
)
NAME_RECALL_RE = re.compile(
    r"\b(?:was it nemo or neko|nemo or neko|"
    r"what was (?:the )?(?:correct )?(?:spelling|name)|"
    r"what (?:is|was) (?:the )?name of my (?:pet|dog|cat)|"
    r"what (?:is|was) (?:my|the) (?:pet|dog|cat)(?:'s|’s)? name|"
    r"do you remember (?:my|the) (?:pet|dog|cat)(?:'s|’s)? name|"
    r"how (?:is|was) (?:my |the )?(?:pet(?:'s|’s)? )?name spell)\b"
)
PET_PROFILE_QUERY_RE = re.compile(
    r"(?:\b(?:what|which) (?:breed|sex|gender|name|type|kind)\b|"
    r"^what (?:is|was) (?:my |the )?"
    r"(?:pet|dog|cat|neko|nemo|dahlia|helsing)(?:'s|’s)? "
    r"(?:breed|sex|gender|name|type|kind)\b|"
    r"^(?:is|was) (?:my |the )?"
    r"(?:pet|dog|cat|neko|nemo|dahlia|helsing)(?:'s|’s)? "
    r"(?:(?:a|an)\b|male\b|female\b|"
    r"(?:breed|sex|gender|name|type|kind)\b)|"
    r"^(?:do|did) i (?:have|own) (?:a |an |the )?(?:pet|dog|cat)\b)"
)
PET_BREED_ASSERTION_RE = re.compile(
    r"^(?:is|was) (?:my |the )?"
    r"(?:pet|dog|cat|neko|nemo|dahlia|helsing)(?:'s|’s)? "
    r"(?:a|an)\b"
)
NAMED_PERSONAL_TERMS = (
    "neko",
    "nemo",
    "dahlia",
    "helsing",
    "deedee",
    "dee dee",
    "jerry",
)


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split()).strip()


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _looks_like_song_identification(text: str) -> bool:
    return _contains(text, SONG_IDENTIFICATION_TERMS) and not _contains(
        text,
        PERSONAL_SONG_RECALL_TERMS,
    )


def _csv_values(raw: Any) -> set[str]:
    return {
        item.strip().casefold()
        for item in str(raw or "").split(",")
        if item.strip()
    }


def resolve_legacy_personal_memory_mode(
    actor_user_id: str,
    *,
    default_mode: str,
    safe_user_ids: str = "",
    off_user_ids: str = "",
) -> str:
    """Resolve the reversible legacy-memory migration mode for one owner."""
    actor = str(actor_user_id or "").strip().casefold()
    if actor and actor in _csv_values(off_user_ids):
        return "off"
    if actor and actor in _csv_values(safe_user_ids):
        return "specific_recall_only"

    raw_mode = str(default_mode or "on").strip().casefold().replace("-", "_")
    aliases = {
        "enabled": "on",
        "safe": "specific_recall_only",
        "specific_recall": "specific_recall_only",
        "disabled": "off",
    }
    mode = aliases.get(raw_mode, raw_mode)
    return mode if mode in LEGACY_PERSONAL_MEMORY_MODES else "off"


def _candidate_entities(text: str) -> list[str]:
    entities: list[str] = []
    if "jerry" in text:
        entities.append("jerry")
    if "deedee" in text or "dee dee" in text:
        entities.append("deedee")
    return entities


def _project_domain(text: str, project_intent: str) -> str:
    if "ab design" in text or "a/b design" in text or "intervention" in text:
        return "behavior_change"
    if "memory" in text or "retrieval" in text or "qdrant" in text:
        return "memory_architecture"
    if project_intent == "project_recall" and (
        "website" in text or "dataset" in text or "history" in text
    ):
        return "project_history"
    return "project"


def _project_intent(text: str) -> str:
    if _contains(text, PROJECT_DECISION_TERMS):
        return "project_decision"
    if _contains(text, PROJECT_PLANNING_TERMS):
        return "project_planning"
    if _contains(text, PROJECT_STATUS_TERMS):
        return "project_status"
    if _contains(text, PROJECT_RECALL_TERMS):
        return "project_recall"
    return "project_recall"


def _looks_like_personal_recall(text: str) -> bool:
    has_anchor = bool(PERSONAL_ANCHOR_RE.search(text)) or _contains(
        text, NAMED_PERSONAL_TERMS
    )
    return bool(has_anchor and PERSONAL_RECALL_CUE_RE.search(text))


def _claim_context(text: str, request_classification: str) -> Dict[str, Any]:
    if request_classification in {"TECH", "MEMORY_ARCHITECTURE", "FM_CONCEPTUAL"}:
        return {
            "eligible": False,
            "reason": f"turn_intent:{request_classification.lower()}",
        }

    recall_requested = _looks_like_personal_recall(text)
    normalization_requested = _contains(text, NAME_TERMS) and _contains(
        text, NORMALIZATION_TERMS
    )
    name_recall_requested = recall_requested and bool(NAME_RECALL_RE.search(text))
    broad_pet_recall = bool(BROAD_PET_RECALL_RE.search(text))
    broad_parent_recall = bool(BROAD_PARENT_RECALL_RE.search(text))
    broad_family_profile_recall = bool(FAMILY_PROFILE_RECALL_RE.search(text))
    family_member_recall = bool(FAMILY_MEMBER_RECALL_RE.search(text))
    direct_family_relation_recall = bool(DIRECT_FAMILY_RELATION_RE.search(text))
    broad_family_recall = (
        broad_parent_recall
        or broad_family_profile_recall
        or family_member_recall
        or direct_family_relation_recall
    )
    has_pet_signal = bool(PET_SIGNAL_RE.search(text))
    pet_event = has_pet_signal and (
        _contains(text, LOSS_TERMS) or _contains(text, EVENT_RECALL_TERMS)
    )
    named_pet_recall = recall_requested and has_pet_signal
    pet_profile_recall = (
        not pet_event
        and has_pet_signal
        and (named_pet_recall or bool(PET_PROFILE_QUERY_RE.search(text)))
    )
    family_event = bool(FAMILY_SIGNAL_RE.search(text)) and (
        _contains(text, LOSS_TERMS) or _contains(text, EVENT_RECALL_TERMS)
    )
    caregiving_context = _contains(text, CAREGIVING_TERMS)
    alcohol_context = _contains(text, ALCOHOL_TERMS)
    rural_life_context = _contains(text, RURAL_LIFE_TERMS)
    support_requested = caregiving_context and (
        recall_requested or _contains(text, SUPPORT_NEED_TERMS)
    )

    domain = None
    if normalization_requested or name_recall_requested:
        domain = "name_correction"
    elif (pet_event and recall_requested) or broad_pet_recall:
        domain = "pet_loss"
    elif pet_profile_recall:
        domain = "pet_profile"
    elif family_event and recall_requested:
        domain = "family_death"
    elif broad_family_recall:
        domain = "family_profile"
    elif support_requested:
        domain = "life_context"
    elif alcohol_context and recall_requested:
        domain = "health_behavior"
    elif rural_life_context and recall_requested:
        domain = "life_context"

    if domain is None:
        if (
            pet_event
            or family_event
            or normalization_requested
            or caregiving_context
            or alcohol_context
            or rural_life_context
            or has_pet_signal
        ):
            return {"eligible": False, "reason": "information_providing_turn"}
        return {"eligible": False, "reason": "unclassified_domain"}

    allowed_predicates: list[str] = []
    if domain == "pet_loss":
        allowed_predicates = ["life_event.died"]
    elif domain == "family_death":
        allowed_predicates = list(FAMILY_DEATH_PREDICATES)
    elif domain == "family_profile":
        allowed_predicates = list(FAMILY_PROFILE_PREDICATES)
    elif domain == "name_correction":
        allowed_predicates = list(NAME_PREDICATES)
    elif domain == "life_context":
        allowed_predicates = list(LIFE_CONTEXT_PREDICATES)
    elif domain == "health_behavior":
        allowed_predicates = list(HEALTH_BEHAVIOR_PREDICATES)
    elif domain == "pet_profile":
        if re.match(r"^(?:do|did) i (?:have|own)\b", text):
            allowed_predicates.append("relationship.has_pet")
        if re.search(r"\b(?:sex|gender|male|female)\b", text):
            allowed_predicates.append("pet.sex")
        if re.search(r"\bname\b", text):
            allowed_predicates.append("identity.name")
        if (
            re.search(r"\b(?:breed|type|kind)\b", text)
            or PET_BREED_ASSERTION_RE.search(text)
        ):
            allowed_predicates.append("pet.breed")
        if not allowed_predicates:
            allowed_predicates = [
                "identity.name",
                "pet.breed",
                "pet.sex",
                "relationship.has_pet",
            ]

    if not allowed_predicates:
        raise RuntimeError(f"eligible claim domain lacks predicate policy: {domain}")

    entity_hints: list[str] = []
    if domain in {"pet_loss", "pet_profile"}:
        if "neko" in text or "nemo" in text:
            entity_hints = ["neko"]
        elif "dahlia" in text:
            entity_hints = ["dahlia"]
        elif "helsing" in text:
            entity_hints = ["helsing"]

    return {
        "eligible": True,
        "reason": "classified",
        "domain": domain,
        "intent": (
            "relevant_support"
            if domain == "life_context" and support_requested
            else "personal_recall"
        ),
        "explicit_recall": bool(
            request_classification == "SPECIFIC_RECALL"
            or recall_requested
            or normalization_requested
            or broad_pet_recall
            or broad_family_recall
            or pet_profile_recall
        ),
        "entity_hints": entity_hints,
        "allowed_predicates": allowed_predicates,
    }


def classify_memory_intent(
    message: str,
    *,
    request_classification: str,
) -> Dict[str, Any]:
    """Map request semantics to governed-memory needs.

    The input classification describes the request only. Assistant response mode,
    persona, and vantage identifiers are deliberately absent from this contract.
    """
    text = _normalized(message)
    classification = (
        str(request_classification or "GENERAL").strip().upper() or "GENERAL"
    )
    if not text:
        return {
            "version": VERSION,
            "status": "no_memory",
            "request_classification": classification,
            "memory_intent": "none",
            "domains": [],
            "project_key": None,
            "candidate_entities": [],
            "direct_relevance": False,
            "routes": {"governed_claims": False, "specialized": False},
            "claim_context": {"eligible": False, "reason": "empty_query"},
            "reason_codes": ["empty_query"],
        }

    claim = _claim_context(text, classification)
    entities = _candidate_entities(text)
    memory_intent = "none"
    domains: list[str] = []
    project_key = None
    reasons: list[str] = []

    preference_domain = None
    if _contains(text, MUSIC_TERMS):
        preference_domain = "music"
    elif _contains(text, OUTDOORS_TERMS):
        preference_domain = "outdoors"

    if preference_domain and _contains(text, RECOMMENDATION_TERMS):
        memory_intent = "recommendation"
        domains = [preference_domain]
        reasons.append(f"explicit_{preference_domain}_recommendation")
    elif preference_domain and _contains(text, PREFERENCE_RECALL_TERMS):
        memory_intent = "preference_recall"
        domains = [preference_domain]
        reasons.append(f"explicit_{preference_domain}_preference_recall")
    else:
        has_project_signal = _contains(text, PROJECT_SIGNALS) or bool(
            PROJECT_APP_RE.search(text)
        )
        project_classification = classification == "MEMORY_ARCHITECTURE"
        if has_project_signal or project_classification:
            memory_intent = _project_intent(text)
            domains = [_project_domain(text, memory_intent)]
            project_key = PROJECT_KEY
            reasons.append("explicit_project_context")
        elif claim.get("eligible"):
            memory_intent = str(claim["intent"])
            domains = [str(claim["domain"])]
            reasons.append("governed_claim_context")

    specialized = memory_intent in {
        "preference_recall",
        "recommendation",
        *PROJECT_INTENTS,
    } or bool(entities)
    if memory_intent == "none" and entities:
        memory_intent = "personal_recall"
        reasons.append("named_response_control_subject")

    return {
        "version": VERSION,
        "status": "classified" if memory_intent != "none" else "no_memory",
        "request_classification": classification,
        "memory_intent": memory_intent,
        "domains": domains,
        "project_key": project_key,
        "candidate_entities": entities,
        "direct_relevance": bool(entities) or memory_intent != "none",
        "routes": {
            "governed_claims": bool(claim.get("eligible")),
            "specialized": bool(specialized),
        },
        "claim_context": claim,
        "reason_codes": reasons or ["no_governed_memory_need"],
    }


def classify_legacy_personal_memory_access(
    message: str,
    *,
    request_classification: str,
    mode: str,
) -> Dict[str, Any]:
    """Gate the legacy raw-vector archive during the Memory V1 migration."""
    text = _normalized(message)
    classification = (
        str(request_classification or "GENERAL").strip().upper() or "GENERAL"
    )
    normalized_mode = resolve_legacy_personal_memory_mode(
        "",
        default_mode=mode,
    )

    if _looks_like_song_identification(text):
        return {
            "version": VERSION,
            "allowed": False,
            "mode": normalized_mode,
            "reason": "song_identification_uses_live_context_only",
            "suppress_corpus": True,
        }
    if normalized_mode == "off":
        return {
            "version": VERSION,
            "allowed": False,
            "mode": normalized_mode,
            "reason": "legacy_personal_memory_disabled",
            "suppress_corpus": False,
        }
    if normalized_mode == "on":
        return {
            "version": VERSION,
            "allowed": True,
            "mode": normalized_mode,
            "reason": "legacy_personal_memory_enabled",
            "suppress_corpus": False,
        }

    intent_plan = classify_memory_intent(
        message,
        request_classification=classification,
    )
    if intent_plan["memory_intent"] in {
        "preference_recall",
        "recommendation",
        *PROJECT_INTENTS,
    }:
        return {
            "version": VERSION,
            "allowed": False,
            "mode": normalized_mode,
            "reason": "curated_memory_route_required",
            "suppress_corpus": False,
        }
    if (
        intent_plan["memory_intent"] == "personal_recall"
        or intent_plan["routes"]["governed_claims"]
    ):
        return {
            "version": VERSION,
            "allowed": True,
            "mode": normalized_mode,
            "reason": "narrow_personal_recall_not_yet_replaced",
            "suppress_corpus": False,
        }
    if classification != "SPECIFIC_RECALL":
        return {
            "version": VERSION,
            "allowed": False,
            "mode": normalized_mode,
            "reason": "not_narrow_personal_recall",
            "suppress_corpus": False,
        }
    return {
        "version": VERSION,
        "allowed": True,
        "mode": normalized_mode,
        "reason": "narrow_personal_recall_not_yet_replaced",
        "suppress_corpus": False,
    }


def apply_legacy_personal_memory_gate(
    retrieval_plan: Dict[str, Any],
    access: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Apply a legacy access decision to an already-computed retrieval plan."""
    updated = dict(retrieval_plan or {})
    requested = int(updated.get("k_personal") or 0)
    requested_corpus = int(updated.get("k_corpus") or 0)
    if not bool(access.get("allowed")):
        updated["k_personal"] = 0
        updated["personal_archive_enabled"] = False
    if bool(access.get("suppress_corpus")):
        updated["k_corpus"] = 0
        updated["corpus_enabled"] = False
    audit = {
        **dict(access or {}),
        "requested_k_personal": requested,
        "effective_k_personal": int(updated.get("k_personal") or 0),
        "requested_k_corpus": requested_corpus,
        "effective_k_corpus": int(updated.get("k_corpus") or 0),
    }
    return updated, audit
