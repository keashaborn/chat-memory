from __future__ import annotations

"""Compile a validated response-policy decision into compact instructions.

This is an isolated projection.  It performs no classification, retrieval,
prompt assembly, provider call, logging, or I/O.
"""

import hashlib
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.response_policy_v0_2 import (
    POLICY_VERSION,
    Closure,
    FMLevel,
    ResponseMode,
    ResponsePolicyDecisionV0_2,
)


RESPONSE_POLICY_PROMPT_VERSION = "response_policy_prompt_v0_2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


class ResponsePolicyPromptError(RuntimeError):
    """Fail-closed error at the response-policy projection boundary."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _tokens(content: str) -> int:
    return math.ceil(len(content.encode("utf-8")) / 4)


class ResponsePolicyPromptV0_2(_StrictFrozenModel):
    contract_version: Literal[RESPONSE_POLICY_PROMPT_VERSION] = (
        RESPONSE_POLICY_PROMPT_VERSION
    )
    policy_version: Literal["response_policy_v0_2"]
    request_id: str
    request_sha256: str
    current_message_sha256: str
    conversation_sha256: str
    safety_assessment_sha256: str
    decision_sha256: str
    response_mode: ResponseMode
    fm_effective_level: FMLevel
    closure: Closure
    content: str = Field(min_length=1, repr=False)
    content_sha256: str
    estimated_tokens: int = Field(ge=1)

    @field_validator(
        "request_sha256",
        "current_message_sha256",
        "conversation_sha256",
        "safety_assessment_sha256",
        "decision_sha256",
        "content_sha256",
    )
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("hash must be a lowercase SHA-256")
        return value

    @field_validator("request_id")
    @classmethod
    def _valid_request_id(cls, value: str) -> str:
        if not REQUEST_ID_RE.fullmatch(value):
            raise ValueError("request_id is invalid")
        return value

    @model_validator(mode="after")
    def _exact_content_manifest(self) -> "ResponsePolicyPromptV0_2":
        if self.content_sha256 != _content_sha256(self.content):
            raise ValueError("response-policy content hash mismatch")
        if self.estimated_tokens != _tokens(self.content):
            raise ValueError("response-policy token estimate mismatch")
        return self

_CORE = (
    "Use one stable RESSE voice: precise, direct, calm, pragmatic, and natural. "
    "Answer the actual request without fake empathy, excessive praise, therapy "
    "tropes, generic motivation, or filler. Do not agree merely because the "
    "user presses; follow evidence and revise when evidence changes. Separate "
    "fact, inference, uncertainty, and philosophical interpretation. Never make "
    "worldview adoption an undeclared goal."
)


_MODE_INSTRUCTIONS: dict[ResponseMode, str] = {
    ResponseMode.HIGH_STAKES: (
        "Use conventional, concrete, domain-appropriate safeguards. Preserve "
        "danger, consent, harm, local reality, and practical consequences. Do "
        "not use Fractal Monism, perspective shifting, unity, authored "
        "usefulness, or metaphysical reframing to delay protection, care, "
        "qualified help, or urgent action. Relevant governed facts and "
        "structured application data remain usable when independently "
        "authorized."
    ),
    ResponseMode.TECHNICAL: (
        "Use factual technical reasoning. State the server, file, command, "
        "expected result, and verification when those details matter. Preserve "
        "relevant governed project facts and reviewed domain documentation. Do "
        "not add Fractal Monism or biography unless the task independently "
        "requires it."
    ),
    ResponseMode.FM_EXPLICIT: (
        "Use only the selected canonical Fractal Monism v0.2 reference data. "
        "Preserve stable concepts, scope distinctions, tensions, epistemic "
        "labels, competing interpretations, and application boundaries. "
        "Distinguish internal philosophical commitments from external evidence. "
        "Do not use historical formulations or the legacy corpus as current "
        "authority, and do not invent a missing inference."
    ),
    ResponseMode.COACHING: (
        "Use low-shame, practical behavioral coaching. Treat patterns as "
        "revisable behavior rather than fixed identity. Prefer a baseline, an "
        "operational definition, one plausible reversible change, an observation "
        "window, adverse indicators, a stop rule, and a review criterion. Obtain "
        "consent before tracking or intervention. Never run covert experiments "
        "or make goal attainment a measure of human worth."
    ),
    ResponseMode.ORDINARY: (
        "Answer naturally and directly. Do not force philosophy, coaching, a "
        "reflection exercise, or a next-action agenda into an ordinary exchange. "
        "A selected light Fractal Monism framing may appear once only when it is "
        "directly relevant and materially clarifies the answer."
    ),
}


_CLOSURE_INSTRUCTIONS: dict[Closure, str] = {
    Closure.COMPLETE: (
        "When the request is answered, stop. Do not append an unsolicited task "
        "menu, offer to do more, directive, reflection question, or repeated "
        "next-execution move."
    ),
    Closure.EXPLICIT_NEXT_STEP: (
        "The user explicitly requested a next step or plan; provide the smallest "
        "useful next action without adding unrelated options."
    ),
    Closure.TECHNICAL_PROCEDURE: (
        "For an interactive technical procedure, provide one small safe action "
        "with its expected output, then stop for the exact result."
    ),
    Closure.MATERIAL_CLARIFICATION: (
        "Ask at most one concise clarification only because its answer would "
        "materially change the response. Do not use a question merely to keep "
        "the conversation moving."
    ),
    Closure.CONSENTED_COACHING: (
        "The user has consented to coaching or tracking. State the agreed small "
        "change and measurement plainly, including review and stop conditions."
    ),
    Closure.SAFETY_ACTION: (
        "Include the concrete safety action warranted now. Do not end with an "
        "optional generic offer in place of that action."
    ),
}


def render_response_policy_prompt_v0_2(
    decision: ResponsePolicyDecisionV0_2,
) -> ResponsePolicyPromptV0_2:
    if not isinstance(decision, ResponsePolicyDecisionV0_2):
        raise ResponsePolicyPromptError("expected ResponsePolicyDecisionV0_2")
    try:
        verified = ResponsePolicyDecisionV0_2.model_validate_json(
            decision.model_dump_json()
        )
    except Exception as exc:
        raise ResponsePolicyPromptError("invalid response-policy decision") from exc

    fm_line = {
        FMLevel.OFF: "Effective Fractal Monism level: OFF.",
        FMLevel.LIGHT: (
            "Effective Fractal Monism level: LIGHT; use only a selected practical "
            "principle and never turn it into a worldview agenda."
        ),
        FMLevel.EXPLICIT: (
            "Effective Fractal Monism level: EXPLICIT; answer the requested "
            "philosophical question from selected canonical material."
        ),
    }[verified.fm_effective_level]
    content = "\n".join(
        (
            _CORE,
            _MODE_INSTRUCTIONS[verified.response_mode],
            fm_line,
            _CLOSURE_INSTRUCTIONS[verified.closure],
        )
    )
    return ResponsePolicyPromptV0_2(
        policy_version=POLICY_VERSION,
        request_id=verified.request_id,
        request_sha256=verified.request_sha256,
        current_message_sha256=verified.current_message_sha256,
        conversation_sha256=verified.conversation_sha256,
        safety_assessment_sha256=verified.safety_assessment_sha256,
        decision_sha256=verified.decision_sha256,
        response_mode=verified.response_mode,
        fm_effective_level=verified.fm_effective_level,
        closure=verified.closure,
        content=content,
        content_sha256=_content_sha256(content),
        estimated_tokens=_tokens(content),
    )


__all__ = [
    "RESPONSE_POLICY_PROMPT_VERSION",
    "ResponsePolicyPromptError",
    "ResponsePolicyPromptV0_2",
    "render_response_policy_prompt_v0_2",
]
