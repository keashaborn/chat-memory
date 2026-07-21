from __future__ import annotations

"""Pure composition of RESSE runtime policy and user-preference selection.

This module is an isolated decision boundary. It does not assemble prompts,
retrieve memory, query a corpus, call a model, or mutate application state.
"""

from dataclasses import dataclass
from typing import Any, Mapping

from rag_engine.resse_runtime_policy import (
    ASSISTANT_PROFILE_ID,
    MEMORY_INTENT_OWNER,
    PolicyDecision,
    PolicyInput,
    PolicySignals,
    decide_policy,
)
from rag_engine.resse_user_preferences import (
    PreferenceSelectionContext,
    ResolvedPreferenceEnvelope,
    build_preference_envelope,
)


COMPOSITE_POLICY_VERSION = "resse_composite_decision_v0_1"


@dataclass(frozen=True)
class ProfileRelevanceSignals:
    """Trusted relevance signals supplied by a future server-side adapter."""

    nickname_relevant: bool = False
    occupation_relevant: bool = False
    more_about_you_relevant: bool = False
    custom_instructions_relevant: bool = False


@dataclass(frozen=True)
class ResseDecisionBundle:
    runtime: PolicyDecision
    preferences: ResolvedPreferenceEnvelope
    policy_version: str = COMPOSITE_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "assistant_profile_id": ASSISTANT_PROFILE_ID,
            "runtime": self.runtime.to_dict(),
            "preferences": self.preferences.to_dict(),
            "governed_context": {
                "memory_intent_owner": MEMORY_INTENT_OWNER,
                "governed_memory_allowed": self.runtime.governed_memory_allowed,
                "structured_data_allowed": self.runtime.structured_data_allowed,
                "memory_selected_elsewhere": True,
            },
            "invariants": {
                "response_mode_source": "runtime_policy",
                "preference_mode_matches_runtime": (
                    self.preferences.response_mode is self.runtime.mode
                ),
                "assistant_identity_fixed": True,
                "memory_intent_independent": True,
                "performs_prompt_assembly": False,
                "performs_retrieval": False,
                "performs_side_effects": False,
            },
        }


def decide_resse_bundle(
    request: PolicyInput,
    *,
    preference_payload: Mapping[str, Any] | None = None,
    policy_signals: PolicySignals | None = None,
    relevance: ProfileRelevanceSignals | None = None,
) -> ResseDecisionBundle:
    """Resolve one internally consistent decision from trusted typed inputs.

    The preference selector's response mode is always constructed from the
    runtime decision. Callers cannot independently supply or override it.
    """

    runtime = decide_policy(request, signals=policy_signals)
    selected = relevance or ProfileRelevanceSignals()
    preference_context = PreferenceSelectionContext(
        response_mode=runtime.mode,
        nickname_relevant=selected.nickname_relevant,
        occupation_relevant=selected.occupation_relevant,
        more_about_you_relevant=selected.more_about_you_relevant,
        custom_instructions_relevant=selected.custom_instructions_relevant,
    )
    preferences = build_preference_envelope(
        preference_payload or {},
        preference_context,
    )
    return ResseDecisionBundle(runtime=runtime, preferences=preferences)
