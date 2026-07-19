from __future__ import annotations

"""Pure, side-effect-free RESSE response policy decisions.

This module intentionally has no runtime integration. It does not import the
prompt builder, retrieval, Memory V1, databases, Qdrant, HTTP clients, model
providers, or environment configuration. Future adapters may translate trusted
application signals into ``PolicySignals`` after joint integration review.
"""

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping, Sequence


POLICY_VERSION = "resse_runtime_policy_v0_1"
ASSISTANT_PROFILE_ID = "RESSE"
MEMORY_INTENT_OWNER = "independent_router"


class ResponseMode(str, Enum):
    HIGH_STAKES = "HIGH_STAKES"
    TECHNICAL = "TECHNICAL"
    FM_EXPLICIT = "FM_EXPLICIT"
    COACHING = "COACHING"
    ORDINARY = "ORDINARY"


MODE_PRECEDENCE: tuple[ResponseMode, ...] = (
    ResponseMode.HIGH_STAKES,
    ResponseMode.TECHNICAL,
    ResponseMode.FM_EXPLICIT,
    ResponseMode.COACHING,
    ResponseMode.ORDINARY,
)


class Closure(str, Enum):
    COMPLETE = "complete"
    TECHNICAL_PROCEDURE = "technical_procedure"
    MATERIAL_CLARIFICATION = "material_clarification"
    CONSENTED_COACHING = "consented_coaching"
    SAFETY_ACTION = "safety_action"


class FmLens(str, Enum):
    OFF = "off"
    OPTIONAL = "optional"
    ON = "on"


LEGACY_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "limits",
        "mix",
        "routing",
        "pragmatics",
        "roleplay",
        "definition_overlay",
        "personalization",
        "vantage_id",
    }
)


@dataclass(frozen=True)
class PolicySignals:
    """Trusted application signals; never construct directly from request JSON.

    A negative high-stakes signal cannot suppress an explicit local safety rule.
    This makes the isolated fallback fail-safe when an upstream classifier misses
    an obvious critical phrase.
    """

    high_stakes: bool | None = None
    technical: bool | None = None
    fm_explicit: bool | None = None
    coaching: bool | None = None
    safety_action_required: bool | None = None
    coaching_consent: bool | None = None
    material_clarification_required: bool | None = None


@dataclass(frozen=True)
class PolicyInput:
    message: str
    conversation_text: str = ""
    assistant_profile_id: str | None = None
    available_sources: tuple[str, ...] = ()
    request_fields: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PolicyInput":
        messages = raw.get("messages")
        conversation_parts: list[str] = []
        latest_user = ""
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            for item in messages:
                if not isinstance(item, Mapping):
                    continue
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                role = str(item.get("role") or "").strip().lower()
                conversation_parts.append(f"{role}: {content}" if role else content)
                if role == "user":
                    latest_user = content

        message = str(raw.get("message") or latest_user or "").strip()
        available = raw.get("available_sources")
        sources = (
            tuple(str(value) for value in available if str(value).strip())
            if isinstance(available, Sequence) and not isinstance(available, (str, bytes))
            else ()
        )
        return cls(
            message=message,
            conversation_text="\n".join(conversation_parts),
            assistant_profile_id=(
                str(raw.get("assistant_profile_id")).strip()
                if raw.get("assistant_profile_id") is not None
                else None
            ),
            available_sources=sources,
            request_fields=tuple(sorted(str(key) for key in raw.keys())),
        )


@dataclass(frozen=True)
class PolicyDecision:
    mode: ResponseMode
    closure: Closure
    fm_lens: FmLens
    fm_tiers: tuple[str, ...]
    fm_max_hits: int
    assistant_profile_id: str
    memory_intent_owner: str
    governed_memory_allowed: bool
    structured_data_allowed: bool
    ignored_request_fields: tuple[str, ...]
    reasons: tuple[str, ...]
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "assistant_profile_id": self.assistant_profile_id,
            "mode": self.mode.value,
            "closure": self.closure.value,
            "routing": {
                "fm_lens": self.fm_lens.value,
                "fm_tiers": list(self.fm_tiers),
                "fm_max_hits": self.fm_max_hits,
                "memory_intent_owner": self.memory_intent_owner,
                "governed_memory_allowed": self.governed_memory_allowed,
                "structured_data_allowed": self.structured_data_allowed,
            },
            "ignored_request_fields": list(self.ignored_request_fields),
            "reasons": list(self.reasons),
        }


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _contains_any(text: str, phrases: Sequence[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _detect_high_stakes(text: str) -> bool:
    return _contains_any(
        text,
        (
            "kill myself",
            "end my life",
            "suicide",
            "means are beside me",
            "partner is hitting me",
            "partner is outside threatening",
            "threatening to break in",
            "immediate danger",
            "still in the house",
            "crushing chest pain",
            "shortness of breath",
            "shaking and seeing things",
            "500 calories",
            "i am fainting",
            "fainting during",
            "fainted twice",
            "cannot bear weight",
            "leg looks deformed",
            "taper it starting tonight",
            "friend was assaulted",
            "custody hearing tomorrow",
            "guarantee i win",
        ),
    )


def _detect_technical(text: str) -> bool:
    return _contains_any(
        text,
        (
            " command",
            "backend",
            "qdrant",
            "collection",
            "trace the request",
            "implement",
            " python",
            "python ",
            " sql",
            "sql ",
            "unique key",
            "system script",
            " cookie",
            "response router",
            "response_mode",
            "worktree",
            " branch",
            "service one command",
            "exact patch",
            "api route",
        ),
    )


def _detect_fm_explicit(text: str) -> bool:
    return _contains_any(
        text,
        (
            "fractal monism",
            "fm view",
            "fm idea",
            "recursive level",
            "recursive vantage",
            "within this philosophy",
            "choosing who i want to be in this moment",
            "every vantage belongs to one whole",
            "core_axioms",
            "authored core",
            "current authored core",
        ),
    )


def _detect_coaching(text: str, conversation: str) -> bool:
    combined = f"{conversation} {text}".strip()
    direct = _contains_any(
        text,
        (
            "help me stop",
            "i keep missing",
            "i want to change",
            "i want to test",
            "let us track",
            "let's track",
            "run little experiments",
            "experiments on users",
            "my weight rose",
            "followed the plan",
            "changed my route home",
            "stopped buying shakes",
            "if i do not lose the weight",
            "if i don't lose the weight",
            "i buy fast food every day",
        ),
    )
    continued = bool(conversation) and _contains_any(
        combined,
        (
            "track missed planned sessions",
            "define what counts as a miss",
            "missed planned sessions",
            "stop missing my macros",
        ),
    )
    return direct or continued


def _ordinary_lens_relevant(text: str) -> bool:
    return _contains_any(
        text,
        (
            "dead end",
            "i am lazy",
            "another way to see",
            "this setback",
            "sincerely believe they are doing good",
            "still hurt everyone around them",
        ),
    )


def _requires_safety_action(text: str) -> bool:
    return _contains_any(
        text,
        (
            "kill myself",
            "means are beside me",
            "partner is hitting me",
            "partner is outside threatening",
            "threatening to break in",
            "immediate danger",
            "still in the house",
            "crushing chest pain",
            "shortness of breath",
            "shaking and seeing things",
            "500 calories",
            "fainting",
            "fainted twice",
            "cannot bear weight",
            "leg looks deformed",
        ),
    )


def _technical_procedure_requested(text: str) -> bool:
    return _contains_any(
        text,
        (
            "safest command",
            "restart the backend",
            "trace the request",
            "implement",
            "exact python patch",
            "walk me through",
            "one command at a time",
        ),
    )


def _coaching_consent(text: str, conversation: str) -> bool:
    combined = f"{conversation} {text}".strip()
    return _contains_any(
        combined,
        (
            "i want to test a change",
            "yes. a miss means",
            "yes, let us track",
            "yes, let's track",
            "let us track missed",
            "let's track missed",
        ),
    )


def _coaching_needs_clarification(text: str) -> bool:
    return _contains_any(
        text,
        (
            "can you help me",
            "i have not agreed to tracking",
            "i haven't agreed to tracking",
        ),
    )


def _select_mode(request: PolicyInput, signals: PolicySignals) -> tuple[ResponseMode, str]:
    text = _normalized(request.message)
    conversation = _normalized(request.conversation_text)

    if signals.high_stakes is True:
        return ResponseMode.HIGH_STAKES, "trusted_high_stakes_signal"
    if _detect_high_stakes(text):
        return ResponseMode.HIGH_STAKES, "deterministic_high_stakes_rule"

    checks = (
        (ResponseMode.TECHNICAL, signals.technical, _detect_technical(text)),
        (ResponseMode.FM_EXPLICIT, signals.fm_explicit, _detect_fm_explicit(text)),
        (ResponseMode.COACHING, signals.coaching, _detect_coaching(text, conversation)),
    )
    for mode, trusted, detected in checks:
        if trusted is True:
            return mode, f"trusted_{mode.value.lower()}_signal"
        if trusted is not False and detected:
            return mode, f"deterministic_{mode.value.lower()}_rule"
    return ResponseMode.ORDINARY, "ordinary_default"


def _select_closure(
    mode: ResponseMode,
    request: PolicyInput,
    signals: PolicySignals,
) -> Closure:
    text = _normalized(request.message)
    conversation = _normalized(request.conversation_text)
    if mode is ResponseMode.HIGH_STAKES:
        required = signals.safety_action_required is True or _requires_safety_action(text)
        return Closure.SAFETY_ACTION if required else Closure.COMPLETE
    if mode is ResponseMode.TECHNICAL:
        return (
            Closure.TECHNICAL_PROCEDURE
            if _technical_procedure_requested(text)
            else Closure.COMPLETE
        )
    if mode is ResponseMode.COACHING:
        if signals.coaching_consent is True or (
            signals.coaching_consent is None and _coaching_consent(text, conversation)
        ):
            return Closure.CONSENTED_COACHING
        needs_clarification = (
            signals.material_clarification_required
            if signals.material_clarification_required is not None
            else _coaching_needs_clarification(text)
        )
        if needs_clarification:
            return Closure.MATERIAL_CLARIFICATION
    return Closure.COMPLETE


def _fm_routing(mode: ResponseMode, request: PolicyInput) -> tuple[FmLens, tuple[str, ...], int]:
    text = _normalized(request.message)
    if mode in {ResponseMode.HIGH_STAKES, ResponseMode.TECHNICAL}:
        return FmLens.OFF, (), 0
    if mode is ResponseMode.FM_EXPLICIT:
        if _contains_any(text, ("compare", "comparison", "buddhist", "external context")):
            return FmLens.ON, ("A", "B", "C"), 4
        if _contains_any(text, ("old core_axioms", "old axiom", "historical draft")):
            return FmLens.ON, ("A", "D"), 4
        return FmLens.ON, ("A", "B"), 4
    if mode is ResponseMode.COACHING:
        if _contains_any(text, ("without telling", "covert")):
            return FmLens.OFF, (), 0
        return FmLens.OPTIONAL, ("A", "B"), 3
    if _ordinary_lens_relevant(text):
        if any(source == "tier_A_fm" for source in request.available_sources):
            return FmLens.OPTIONAL, ("A",), 2
        return FmLens.OPTIONAL, (), 0
    return FmLens.OFF, (), 0


def decide_policy(
    request: PolicyInput,
    *,
    signals: PolicySignals | None = None,
) -> PolicyDecision:
    """Return a deterministic policy decision without performing any side effect."""

    trusted = signals or PolicySignals()
    mode, mode_reason = _select_mode(request, trusted)
    closure = _select_closure(mode, request, trusted)
    fm_lens, fm_tiers, fm_max_hits = _fm_routing(mode, request)
    ignored = tuple(sorted(LEGACY_REQUEST_FIELDS.intersection(request.request_fields)))
    reasons = [mode_reason, f"closure_{closure.value}"]
    if request.assistant_profile_id and request.assistant_profile_id.upper() != ASSISTANT_PROFILE_ID:
        reasons.append("requested_profile_rejected")
    if ignored:
        reasons.append("legacy_request_fields_ignored")
    if mode in {ResponseMode.HIGH_STAKES, ResponseMode.TECHNICAL}:
        reasons.append("fm_suppressed_by_mode")

    return PolicyDecision(
        mode=mode,
        closure=closure,
        fm_lens=fm_lens,
        fm_tiers=fm_tiers,
        fm_max_hits=fm_max_hits,
        assistant_profile_id=ASSISTANT_PROFILE_ID,
        memory_intent_owner=MEMORY_INTENT_OWNER,
        governed_memory_allowed=True,
        structured_data_allowed=True,
        ignored_request_fields=ignored,
        reasons=tuple(reasons),
    )
