from __future__ import annotations

"""Trusted OpenAI moderation adapter for the RESSE response-policy boundary.

The adapter has no credential, environment, logging, database, or routing
responsibilities.  A live server adapter must inject an authenticated OpenAI
client.  Provider failures and malformed provider responses produce an
auditable, fail-closed ``UNCERTAIN`` assessment rather than an exception that
could accidentally bypass the high-stakes gate.
"""

from collections.abc import Mapping
from typing import Any

from seebx.capabilities.conversation.policy import (
    GateState,
    ResponsePolicyInputV0_2,
    SafetyAssessmentV0_2,
)


MODERATION_ADAPTER_VERSION = "openai_moderation_adapter_v0_2"
DEFAULT_MODERATION_MODEL = "omni-moderation-latest"
DEFAULT_MODERATION_TIMEOUT_SECONDS = 10.0
MAX_MODERATION_TIMEOUT_SECONDS = 30.0
MAX_MODERATION_INPUT_BYTES = 32_768
MAX_MODERATION_TOTAL_INPUT_BYTES = 80_768

_API_ERROR_REASON = "openai_moderation_api_error"
_INPUT_LIMIT_REASON = "openai_moderation_input_limit"
_MALFORMED_RESPONSE_REASON = "openai_moderation_malformed_response"

_CATEGORY_ALIASES: dict[str, str] = {
    "harassment": "harassment",
    "harassment/threatening": "harassment_threatening",
    "harassment_threatening": "harassment_threatening",
    "hate": "hate",
    "hate/threatening": "hate_threatening",
    "hate_threatening": "hate_threatening",
    "illicit": "illicit",
    "illicit/violent": "illicit_violent",
    "illicit_violent": "illicit_violent",
    "self-harm": "self_harm",
    "self-harm/intent": "self_harm_intent",
    "self-harm/instructions": "self_harm_instructions",
    "self_harm": "self_harm",
    "self_harm_intent": "self_harm_intent",
    "self_harm_instructions": "self_harm_instructions",
    "sexual": "sexual",
    "sexual/minors": "sexual_minors",
    "sexual_minors": "sexual_minors",
    "violence": "violence",
    "violence/graphic": "violence_graphic",
    "violence_graphic": "violence_graphic",
}

_ACUTE_SELF_HARM_CATEGORIES = frozenset(
    {"self_harm_intent", "self_harm_instructions"}
)
_REQUIRED_CATEGORIES = frozenset(_CATEGORY_ALIASES.values())


class OpenAIModerationAdapterError(RuntimeError):
    """The trusted local moderation contract is invalid."""


def _strict_request(request: ResponsePolicyInputV0_2) -> ResponsePolicyInputV0_2:
    if not isinstance(request, ResponsePolicyInputV0_2):
        raise OpenAIModerationAdapterError(
            "moderation requires ResponsePolicyInputV0_2"
        )
    try:
        return ResponsePolicyInputV0_2.model_validate_json(
            request.model_dump_json()
        )
    except Exception:
        raise OpenAIModerationAdapterError(
            "invalid response-policy input at moderation boundary"
        ) from None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump()
        except Exception:
            return None
        if isinstance(result, Mapping):
            return result
    return None


def _field(value: Any, name: str) -> Any:
    mapped = _mapping(value)
    if mapped is not None and name in mapped:
        return mapped[name]
    return getattr(value, name, None)


def _category_values(value: Any) -> dict[str, bool] | None:
    mapped = _mapping(value)
    if mapped is None:
        mapped = {
            alias: getattr(value, alias)
            for alias in _CATEGORY_ALIASES
            if hasattr(value, alias)
        }
    if not mapped:
        return None

    normalized: dict[str, bool] = {}
    unknown_true = False
    for raw_name, raw_value in mapped.items():
        if type(raw_value) is not bool:
            # Category payloads are a closed boolean decision surface.  Scores
            # belong to ``category_scores`` and are deliberately not consumed.
            return None
        canonical = _CATEGORY_ALIASES.get(str(raw_name))
        if canonical is None:
            unknown_true = unknown_true or raw_value
            continue
        previous = normalized.get(canonical)
        if previous is not None and previous is not raw_value:
            return None
        normalized[canonical] = raw_value
    if not normalized:
        return None
    if not _REQUIRED_CATEGORIES.issubset(normalized):
        # A partial category surface could silently omit the exact category
        # that should trigger the high-stakes gate.  Treat it as malformed.
        return None
    if unknown_true:
        normalized["other"] = True
    return normalized


def _parse_moderation_result(
    response: Any,
    *,
    expected_result_count: int,
) -> tuple[str, ...]:
    results = _field(response, "results")
    if (
        not isinstance(results, (list, tuple))
        or len(results) != expected_result_count
    ):
        raise ValueError("moderation response count differs from assessed messages")
    triggered: set[str] = set()
    for result in results:
        flagged = _field(result, "flagged")
        if type(flagged) is not bool:
            raise ValueError("moderation flagged value must be boolean")
        categories = _category_values(_field(result, "categories"))
        if categories is None:
            raise ValueError("moderation categories are invalid")
        item_triggered = {
            name for name, value in categories.items() if value
        }
        if flagged is not bool(item_triggered):
            raise ValueError("moderation flag and categories disagree")
        triggered.update(item_triggered)
    return tuple(sorted(triggered))


def _uncertain_assessment(
    request: ResponsePolicyInputV0_2,
    reason: str,
) -> SafetyAssessmentV0_2:
    return SafetyAssessmentV0_2.create(
        request,
        high_stakes_gate=GateState.UNCERTAIN,
        safety_action_required=False,
        reason_codes=(reason,),
        assessor_components=(MODERATION_ADAPTER_VERSION,),
    )


class OpenAIModerationAdapterV0_2:
    """Assess every provider-visible user turn with an injected OpenAI client.

    A one-turn request is sent as one string; a multi-turn request is sent as a
    list with one result required per user message.  The returned
    ``SafetyAssessmentV0_2`` is bound to the exact full response-policy request
    and the ordered hashes of all assessed user messages.
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str = DEFAULT_MODERATION_MODEL,
        timeout_seconds: float = DEFAULT_MODERATION_TIMEOUT_SECONDS,
        max_input_bytes: int = MAX_MODERATION_INPUT_BYTES,
        max_total_input_bytes: int = MAX_MODERATION_TOTAL_INPUT_BYTES,
    ) -> None:
        if client is None:
            raise OpenAIModerationAdapterError("an OpenAI client is required")
        if not callable(getattr(client, "with_options", None)):
            raise OpenAIModerationAdapterError(
                "OpenAI client must support zero-retry request options"
            )
        if model != DEFAULT_MODERATION_MODEL:
            raise OpenAIModerationAdapterError(
                "moderation model must be omni-moderation-latest"
            )
        if (
            type(timeout_seconds) not in (int, float)
            or isinstance(timeout_seconds, bool)
            or not 1.0 <= float(timeout_seconds) <= MAX_MODERATION_TIMEOUT_SECONDS
        ):
            raise OpenAIModerationAdapterError("moderation timeout is out of bounds")
        if (
            type(max_input_bytes) is not int
            or isinstance(max_input_bytes, bool)
            or not 1 <= max_input_bytes <= MAX_MODERATION_INPUT_BYTES
        ):
            raise OpenAIModerationAdapterError(
                "moderation input limit is out of bounds"
            )
        if (
            type(max_total_input_bytes) is not int
            or isinstance(max_total_input_bytes, bool)
            or not 1
            <= max_total_input_bytes
            <= MAX_MODERATION_TOTAL_INPUT_BYTES
        ):
            raise OpenAIModerationAdapterError(
                "moderation total input limit is out of bounds"
            )
        self._client = client
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._max_input_bytes = max_input_bytes
        self._max_total_input_bytes = max_total_input_bytes

    def assess(self, request: ResponsePolicyInputV0_2) -> SafetyAssessmentV0_2:
        verified = _strict_request(request)
        moderation_inputs = tuple(
            item.content
            for item in verified.conversation
            if item.role.value == "user"
        )
        input_sizes = tuple(
            len(item.encode("utf-8")) for item in moderation_inputs
        )
        if (
            not moderation_inputs
            or any(size > self._max_input_bytes for size in input_sizes)
            or sum(input_sizes) > self._max_total_input_bytes
        ):
            return _uncertain_assessment(verified, _INPUT_LIMIT_REASON)
        moderation_input: str | list[str] = (
            moderation_inputs[0]
            if len(moderation_inputs) == 1
            else list(moderation_inputs)
        )
        try:
            call_client = self._client.with_options(
                max_retries=0,
                timeout=self._timeout_seconds,
            )
            response = call_client.moderations.create(
                model=self._model,
                input=moderation_input,
                timeout=self._timeout_seconds,
            )
        except Exception:
            return _uncertain_assessment(verified, _API_ERROR_REASON)
        try:
            triggered_categories = _parse_moderation_result(
                response,
                expected_result_count=len(moderation_inputs),
            )
        except Exception:
            return _uncertain_assessment(verified, _MALFORMED_RESPONSE_REASON)

        if not triggered_categories:
            return SafetyAssessmentV0_2.create(
                verified,
                assessor_components=(MODERATION_ADAPTER_VERSION,),
            )
        reasons = tuple(
            f"openai_moderation_{category}" for category in triggered_categories
        )
        return SafetyAssessmentV0_2.create(
            verified,
            high_stakes_gate=GateState.TRIGGERED,
            safety_action_required=bool(
                _ACUTE_SELF_HARM_CATEGORIES.intersection(triggered_categories)
            ),
            reason_codes=reasons,
            assessor_components=(MODERATION_ADAPTER_VERSION,),
        )


def assess_openai_moderation_v0_2(
    request: ResponsePolicyInputV0_2,
    *,
    client: Any,
    timeout_seconds: float = DEFAULT_MODERATION_TIMEOUT_SECONDS,
) -> SafetyAssessmentV0_2:
    """Convenience boundary for callers that do not retain an adapter."""

    return OpenAIModerationAdapterV0_2(
        client,
        timeout_seconds=timeout_seconds,
    ).assess(request)


__all__ = [
    "DEFAULT_MODERATION_MODEL",
    "DEFAULT_MODERATION_TIMEOUT_SECONDS",
    "MAX_MODERATION_INPUT_BYTES",
    "MAX_MODERATION_TOTAL_INPUT_BYTES",
    "MODERATION_ADAPTER_VERSION",
    "OpenAIModerationAdapterError",
    "OpenAIModerationAdapterV0_2",
    "assess_openai_moderation_v0_2",
]
