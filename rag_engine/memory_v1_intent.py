from __future__ import annotations

from typing import Any, Dict


VERSION = "memory_intent_adapter_v1"
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
FAMILY_TERMS = ("mother", "mom", "mum", "father", "dad", "parent", "deedee")
PET_TERMS = ("pet", "dog", "cat", "neko", "nemo", "dahlia", "helsing")
CAREGIVING_TERMS = (
    "caregiving",
    "caregiver",
    "caretaking",
    "caretaker",
    "psychotic break",
    "care burden",
    "caring for my wife",
    "caring for my spouse",
    "monika",
)
MUSIC_TERMS = ("music", "song", "songs", "artist", "artists", "lyrics", "playlist")
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
    "i prefer",
    "do i prefer",
    "what kind of music",
    "what music do i",
    "remember what music",
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


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split()).strip()


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _candidate_entities(text: str) -> list[str]:
    entities: list[str] = []
    if "jerry" in text:
        entities.append("jerry")
    if "deedee" in text or "dee dee" in text:
        entities.append("deedee")
    return entities


def _project_domain(text: str) -> str:
    if "ab design" in text or "a/b design" in text or "intervention" in text:
        return "behavior_change"
    if "website" in text or "dataset" in text or "history" in text:
        return "project_history"
    if "memory" in text or "retrieval" in text or "qdrant" in text:
        return "memory_architecture"
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


def _claim_context(text: str, request_classification: str) -> Dict[str, Any]:
    if request_classification in {"TECH", "MEMORY_ARCHITECTURE", "FM_CONCEPTUAL"}:
        return {
            "eligible": False,
            "reason": f"turn_intent:{request_classification.lower()}",
        }

    domain = None
    if _contains(text, PET_TERMS) and (
        _contains(text, LOSS_TERMS) or _contains(text, EVENT_RECALL_TERMS)
    ):
        domain = "pet_loss"
    elif _contains(text, NAME_TERMS):
        domain = "name_correction"
    elif _contains(text, FAMILY_TERMS) and (
        _contains(text, LOSS_TERMS) or _contains(text, EVENT_RECALL_TERMS)
    ):
        domain = "family_death"
    elif _contains(text, CAREGIVING_TERMS):
        domain = "life_context"

    if domain is None:
        return {"eligible": False, "reason": "unclassified_domain"}

    entity_hints: list[str] = []
    if domain == "pet_loss":
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
        "intent": "relevant_support" if domain == "life_context" else "personal_recall",
        "explicit_recall": request_classification == "SPECIFIC_RECALL",
        "entity_hints": entity_hints,
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

    if _contains(text, MUSIC_TERMS) and _contains(text, RECOMMENDATION_TERMS):
        memory_intent = "recommendation"
        domains = ["music"]
        reasons.append("explicit_music_recommendation")
    elif _contains(text, MUSIC_TERMS) and _contains(text, PREFERENCE_RECALL_TERMS):
        memory_intent = "preference_recall"
        domains = ["music"]
        reasons.append("explicit_music_preference_recall")
    else:
        has_project_signal = _contains(text, PROJECT_SIGNALS)
        project_classification = classification == "MEMORY_ARCHITECTURE"
        if has_project_signal or project_classification:
            memory_intent = _project_intent(text)
            domains = [_project_domain(text)]
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
