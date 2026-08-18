from __future__ import annotations

"""Hash-pinned Relational Monism v0.4 response-prompt selection.

The response-policy contract still exposes ``fm_*`` field names for wire
compatibility.  This boundary is the only active philosophy selector and never
loads the historical FM v0.2 bundle.
"""

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from seebx.capabilities.conversation.policy import FMLevel, GateState, ResponseMode, ResponsePolicyDecisionV0_2


REQUEST_CONTRACT_VERSION = "rm_selection_request_v0_4"
ENVELOPE_CONTRACT_VERSION = "rm_selection_envelope_v0_4"
SELECTOR_VERSION = "rm_routed_full_prompt_selector_v0_4"
TOKEN_ESTIMATOR_VERSION = "rm_utf8_bytes_div4_v0_4"
SEMANTIC_VERSION = "0.4"
ACTIVE_PHILOSOPHY_ID = "relational_monism_v0_4"
CANONICAL_MANIFEST_SHA256 = (
    "29412aeeed3b98ffdeac6436d7ca1d1b2b27aa573bf23a85c99546f6a5c5f28a"
)
RUNTIME_PROMPT_SHA256 = (
    "d41d8a7428406f3a4570d293d1436cf31c99df1c4bc8f4e7a01c1c0f05885ecf"
)
DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent / "data" / "rm_v0_4_runtime_prompt.md"
)
MAX_PROMPT_TOKENS = 5200

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
SELECTION_ID_RE = re.compile(r"^rm-sel-[0-9a-f]{24}$")

ResponseModeValue: TypeAlias = Literal[
    "HIGH_STAKES", "TECHNICAL", "FM_EXPLICIT", "COACHING", "ORDINARY"
]
FMLevelValue: TypeAlias = Literal["OFF", "LIGHT", "EXPLICIT"]
SelectionStatus: TypeAlias = Literal["OFF", "EMPTY", "SELECTED"]
SelectionReason: TypeAlias = Literal[
    "high_stakes_off",
    "technical_off",
    "application_gate_triggered",
    "application_gate_uncertain",
    "user_opt_out",
    "philosophy_level_off",
    "token_budget_exhausted",
    "selected",
]


class RMSelectionContractError(RuntimeError):
    """Fail-closed error at the active RM selection boundary."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _estimated_tokens(value: str) -> int:
    return math.ceil(len(value.encode("utf-8")) / 4)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RMSelectionContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_runtime_prompt_v0_4(path: Path | None = None) -> str:
    selected_path = (path or DEFAULT_PROMPT_PATH).resolve()
    try:
        raw = selected_path.read_bytes()
    except OSError as exc:
        raise RMSelectionContractError("RM v0.4 runtime prompt is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != RUNTIME_PROMPT_SHA256:
        raise RMSelectionContractError("RM v0.4 runtime prompt hash mismatch")
    try:
        prompt = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RMSelectionContractError("RM v0.4 runtime prompt is not UTF-8") from exc
    if not prompt.strip() or _estimated_tokens(prompt) > MAX_PROMPT_TOKENS:
        raise RMSelectionContractError("RM v0.4 runtime prompt exceeds its hard budget")
    return prompt


class RMSelectionRequestV04(_StrictFrozenModel):
    contract_version: Literal["rm_selection_request_v0_4"] = REQUEST_CONTRACT_VERSION
    policy_decision: ResponsePolicyDecisionV0_2
    query_text: str = Field(max_length=20_000, repr=False)
    token_budget: int | None = Field(default=None, ge=0, le=MAX_PROMPT_TOKENS)

    @model_validator(mode="after")
    def query_binds_to_policy_message(self) -> "RMSelectionRequestV04":
        if _text_sha256(self.query_text) != self.policy_decision.current_message_sha256:
            raise ValueError("RM query does not bind to the policy current message")
        return self


class RMSelectionEnvelopeV04(_StrictFrozenModel):
    contract_version: Literal["rm_selection_envelope_v0_4"]
    selector_version: Literal["rm_routed_full_prompt_selector_v0_4"]
    token_estimator_version: Literal["rm_utf8_bytes_div4_v0_4"]
    semantic_version: Literal["0.4"]
    active_philosophy_id: Literal["relational_monism_v0_4"]
    canonical_manifest_sha256: Literal[
        "29412aeeed3b98ffdeac6436d7ca1d1b2b27aa573bf23a85c99546f6a5c5f28a"
    ]
    runtime_prompt_sha256: Literal[
        "d41d8a7428406f3a4570d293d1436cf31c99df1c4bc8f4e7a01c1c0f05885ecf"
    ]
    # Compatibility for the existing response-plan manifest field. This is the
    # RM prompt digest, never an FM bundle digest.
    bundle_sha256: Literal[
        "d41d8a7428406f3a4570d293d1436cf31c99df1c4bc8f4e7a01c1c0f05885ecf"
    ]
    policy_version: Literal["response_policy_v0_2"]
    request_id: str
    request_sha256: str
    current_message_sha256: str
    conversation_sha256: str
    safety_assessment_sha256: str
    policy_decision_sha256: str
    selection_id: str
    selection_sha256: str
    query_sha256: str
    response_mode: ResponseModeValue
    fm_level: FMLevelValue
    status: SelectionStatus
    reason_codes: tuple[SelectionReason, ...] = Field(min_length=1, max_length=1)
    selected_record_ids: tuple[str, ...] = Field(max_length=1)
    token_budget: int = Field(ge=0, le=MAX_PROMPT_TOKENS)
    used_tokens: int = Field(ge=0, le=MAX_PROMPT_TOKENS)

    @model_validator(mode="after")
    def exact_selection(self) -> "RMSelectionEnvelopeV04":
        for digest in (
            self.request_sha256,
            self.current_message_sha256,
            self.conversation_sha256,
            self.safety_assessment_sha256,
            self.policy_decision_sha256,
            self.selection_sha256,
            self.query_sha256,
        ):
            if not SHA256_RE.fullmatch(digest):
                raise ValueError("invalid SHA-256 digest")
        if not REQUEST_ID_RE.fullmatch(self.request_id):
            raise ValueError("invalid request id")
        if not SELECTION_ID_RE.fullmatch(self.selection_id):
            raise ValueError("invalid selection id")
        if self.query_sha256 != self.current_message_sha256:
            raise ValueError("selection query differs from policy message")
        if self.used_tokens > self.token_budget:
            raise ValueError("used token estimate exceeds budget")

        if self.status == "SELECTED":
            if self.response_mode in {"HIGH_STAKES", "TECHNICAL"}:
                raise ValueError("controlling modes cannot select RM")
            if self.fm_level == "OFF":
                raise ValueError("selected RM requires an active philosophy level")
            if self.reason_codes != ("selected",):
                raise ValueError("selected RM has an invalid reason")
            if self.selected_record_ids != (ACTIVE_PHILOSOPHY_ID,):
                raise ValueError("selected RM identity is invalid")
            if self.used_tokens != _estimated_tokens(load_runtime_prompt_v0_4()):
                raise ValueError("selected RM token estimate differs from prompt")
        else:
            if self.selected_record_ids or self.used_tokens != 0:
                raise ValueError("inactive RM selection contains prompt data")
            if self.status == "OFF" and self.fm_level != "OFF":
                raise ValueError("OFF RM selection requires OFF policy level")
            if self.status == "EMPTY" and self.fm_level == "OFF":
                raise ValueError("EMPTY RM selection requires active policy level")
            if self.status == "EMPTY" and self.reason_codes != (
                "token_budget_exhausted",
            ):
                raise ValueError("EMPTY RM selection has an invalid reason")

        expected_reason: SelectionReason | None = None
        if self.response_mode == "HIGH_STAKES":
            expected_reason = "high_stakes_off"
        elif self.response_mode == "TECHNICAL":
            expected_reason = "technical_off"
        if expected_reason is not None and self.reason_codes != (expected_reason,):
            raise ValueError("controlling mode RM reason is invalid")

        payload = self.model_dump(mode="json", exclude={"selection_id", "selection_sha256"})
        expected_hash = _canonical_sha256(payload)
        if self.selection_sha256 != expected_hash:
            raise ValueError("RM selection payload digest mismatch")
        if self.selection_id != f"rm-sel-{expected_hash[:24]}":
            raise ValueError("RM selection id does not bind the digest")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)

    def compact_content(self) -> str:
        if self.status != "SELECTED":
            return ""
        prompt = load_runtime_prompt_v0_4()
        if _text_sha256(prompt) != self.runtime_prompt_sha256:
            raise RMSelectionContractError("RM selection and prompt digests differ")
        return prompt

    @property
    def max_records(self) -> int:
        """Compatibility with the existing content-free response trace."""

        return 1

    @classmethod
    def from_wire_json(cls, value: str | bytes) -> "RMSelectionEnvelopeV04":
        try:
            document = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
            return cls.model_validate_json(_canonical_json_bytes(document))
        except RMSelectionContractError:
            raise
        except (TypeError, ValueError, ValidationError) as exc:
            raise RMSelectionContractError("invalid RM selection envelope") from exc


def _off_reason(decision: ResponsePolicyDecisionV0_2) -> SelectionReason:
    if decision.response_mode is ResponseMode.HIGH_STAKES:
        return "high_stakes_off"
    if decision.response_mode is ResponseMode.TECHNICAL:
        return "technical_off"
    if decision.user_opt_out_applied:
        return "user_opt_out"
    if decision.fm_application_gate is GateState.TRIGGERED:
        return "application_gate_triggered"
    if decision.fm_application_gate is GateState.UNCERTAIN:
        return "application_gate_uncertain"
    return "philosophy_level_off"


def _build_envelope(
    request: RMSelectionRequestV04,
    *,
    status: SelectionStatus,
    reason: SelectionReason,
    token_budget: int,
    used_tokens: int,
) -> RMSelectionEnvelopeV04:
    decision = request.policy_decision
    values: dict[str, Any] = {
        "contract_version": ENVELOPE_CONTRACT_VERSION,
        "selector_version": SELECTOR_VERSION,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
        "semantic_version": SEMANTIC_VERSION,
        "active_philosophy_id": ACTIVE_PHILOSOPHY_ID,
        "canonical_manifest_sha256": CANONICAL_MANIFEST_SHA256,
        "runtime_prompt_sha256": RUNTIME_PROMPT_SHA256,
        "bundle_sha256": RUNTIME_PROMPT_SHA256,
        "policy_version": decision.policy_version,
        "request_id": decision.request_id,
        "request_sha256": decision.request_sha256,
        "current_message_sha256": decision.current_message_sha256,
        "conversation_sha256": decision.conversation_sha256,
        "safety_assessment_sha256": decision.safety_assessment_sha256,
        "policy_decision_sha256": decision.decision_sha256,
        "query_sha256": _text_sha256(request.query_text),
        "response_mode": decision.response_mode.value,
        "fm_level": decision.fm_effective_level.value,
        "status": status,
        "reason_codes": (reason,),
        "selected_record_ids": (
            (ACTIVE_PHILOSOPHY_ID,) if status == "SELECTED" else ()
        ),
        "token_budget": token_budget,
        "used_tokens": used_tokens,
    }
    digest = _canonical_sha256(values)
    values["selection_id"] = f"rm-sel-{digest[:24]}"
    values["selection_sha256"] = digest
    return RMSelectionEnvelopeV04.model_validate(values)


def select_rm_v0_4(request: RMSelectionRequestV04) -> RMSelectionEnvelopeV04:
    if not isinstance(request, RMSelectionRequestV04):
        raise TypeError("RM selection request type mismatch")
    request = RMSelectionRequestV04.model_validate_json(_canonical_json_bytes(request))
    decision = request.policy_decision
    budget = MAX_PROMPT_TOKENS if request.token_budget is None else request.token_budget

    if decision.fm_effective_level is FMLevel.OFF:
        return _build_envelope(
            request,
            status="OFF",
            reason=_off_reason(decision),
            token_budget=budget,
            used_tokens=0,
        )

    prompt_tokens = _estimated_tokens(load_runtime_prompt_v0_4())
    if budget < prompt_tokens:
        return _build_envelope(
            request,
            status="EMPTY",
            reason="token_budget_exhausted",
            token_budget=budget,
            used_tokens=0,
        )
    return _build_envelope(
        request,
        status="SELECTED",
        reason="selected",
        token_budget=budget,
        used_tokens=prompt_tokens,
    )


__all__ = [
    "ACTIVE_PHILOSOPHY_ID",
    "CANONICAL_MANIFEST_SHA256",
    "DEFAULT_PROMPT_PATH",
    "ENVELOPE_CONTRACT_VERSION",
    "MAX_PROMPT_TOKENS",
    "REQUEST_CONTRACT_VERSION",
    "RMSelectionContractError",
    "RMSelectionEnvelopeV04",
    "RMSelectionRequestV04",
    "RUNTIME_PROMPT_SHA256",
    "load_runtime_prompt_v0_4",
    "select_rm_v0_4",
]
