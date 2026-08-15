from __future__ import annotations

"""Closed normal-chat adapter for explicit owner preference lifecycle commands."""

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import os
import re
from typing import Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from rag_engine.governed_memory.chat_commands import (
    ExplicitPreferenceCorrectionCommandV1,
    explicit_preference_correction_command_v1,
    explicit_preference_retraction_target_v1,
    normalized_chat_text_v1,
    normalized_preference_value_v1,
)
from rag_engine.governed_memory.preference_correction_interpreter import (
    PreferenceCorrectionClaimV1,
    PreferenceCorrectionInterpretationUnavailableV1,
    PreferenceCorrectionInterpreterV1,
    openai_preference_correction_interpreter_from_environment_v1,
    potential_preference_correction_v1,
)
from rag_engine.governed_memory_erasure_proxy_v1 import (
    ErasureProxyTransport,
    ProxyResult,
    UnixSocketErasureTransport,
    governed_memory_proxy_service_token_is_valid,
)


logger = logging.getLogger("uvicorn.error")
SERVICE_TOKEN_ENV = "GOVERNED_MEMORY_SERVICE_TOKEN"
MAX_PREFERENCE_CLAIMS = 32
MAX_RESPONSE_BODY_BYTES = 131_072
REQUEST_DEADLINE_SECONDS = 16.0
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_BEARER_RE = re.compile(
    r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z",
    re.ASCII | re.IGNORECASE,
)
_LIST_KEYS = frozenset(
    {
        "claim_id",
        "lifecycle_state",
        "revision_id",
        "revision_number",
        "revision_sha256",
        "current_state_sha256",
        "revision_fact_policy_sha256",
        "predicate_catalog_sha256",
        "selected_sha256",
        "selection_binding_sha256",
        "object_kind",
        "predicate",
        "epistemic_state",
        "sensitivity",
        "updated_at",
    }
)
_DETAIL_KEYS = _LIST_KEYS | {
    "subject_entity_type",
    "subject_entity_key",
    "subject_display_name",
    "object_entity_type",
    "object_entity_key",
    "object_display_name",
    "object_literal",
}
_PROPOSAL_KEYS = frozenset(
    {
        "proposal_id",
        "operation_id",
        "proposal_sha256",
        "source_sha256",
        "selected_sha256",
        "selection_binding_sha256",
        "predicate_catalog_sha256",
        "source_excerpt",
        "subject_entity_type",
        "subject_entity_key",
        "subject_display_name",
        "predicate",
        "object_kind",
        "object_entity_type",
        "object_entity_key",
        "object_display_name",
        "object_literal",
        "epistemic_state",
        "sensitivity",
        "projectable",
        "domains",
        "intents",
        "surface",
        "requires_explicit",
        "valid_from",
        "valid_to",
        "correction_of_claim_id",
        "expires_at",
        "created_at",
    }
)


class ChatMemoryLifecycleError(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ChatMemoryLifecycleReceiptV1:
    action: str
    claim_id: UUID
    outcome: str


def _operation_id(*, purpose: str, claim_id: UUID, message: str) -> UUID:
    normalized_sha256 = sha256(
        normalized_chat_text_v1(message).encode("utf-8")
    ).hexdigest()
    return uuid5(
        NAMESPACE_URL,
        f"governed-memory:chat-{purpose}-v1:{claim_id}:{normalized_sha256}",
    )


def retraction_operation_id_v1(*, claim_id: UUID, message: str) -> UUID:
    return _operation_id(purpose="retraction", claim_id=claim_id, message=message)


def correction_operation_id_v1(*, claim_id: UUID, message: str) -> UUID:
    return _operation_id(purpose="correction", claim_id=claim_id, message=message)


def _reject_non_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _closed_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _unavailable() -> ChatMemoryLifecycleError:
    return ChatMemoryLifecycleError("memory_lifecycle_unavailable", 503)


def _decode_json(result: ProxyResult) -> object:
    content_type = (
        result.headers.get("content-type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if (
        result.status_code != 200
        or content_type != "application/json"
        or len(result.body) > MAX_RESPONSE_BODY_BYTES
    ):
        raise _unavailable()
    try:
        return json.loads(
            result.body,
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise _unavailable() from None


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise _unavailable()
    try:
        parsed = UUID(value)
    except ValueError:
        raise _unavailable() from None
    if str(parsed) != value:
        raise _unavailable()
    return parsed


def _sha256(value: object) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise _unavailable()
    return value


def _validate_list_item(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != _LIST_KEYS:
        raise _unavailable()
    _uuid(value["claim_id"])
    _uuid(value["revision_id"])
    for name in (
        "revision_sha256",
        "current_state_sha256",
        "revision_fact_policy_sha256",
        "predicate_catalog_sha256",
        "selected_sha256",
        "selection_binding_sha256",
    ):
        _sha256(value[name])
    if (
        type(value["revision_number"]) is not int
        or value["revision_number"] < 1
        or not isinstance(value["lifecycle_state"], str)
        or not isinstance(value["predicate"], str)
        or not isinstance(value["object_kind"], str)
        or not isinstance(value["epistemic_state"], str)
        or not isinstance(value["sensitivity"], str)
    ):
        raise _unavailable()
    return value


def _validate_detail(
    value: object,
    *,
    summary: Mapping[str, object],
) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != _DETAIL_KEYS:
        raise _unavailable()
    for name in _LIST_KEYS:
        if value[name] != summary[name]:
            raise _unavailable()
    kind = value["object_kind"]
    literal = value["object_literal"]
    display_name = value["object_display_name"]
    if kind == "literal":
        if (
            not isinstance(literal, str)
            or not literal
            or len(literal.encode("utf-8")) > 4_096
            or "\x00" in literal
            or value["object_entity_type"] is not None
            or value["object_entity_key"] is not None
            or display_name is not None
        ):
            raise _unavailable()
    elif kind == "entity":
        if (
            literal is not None
            or not isinstance(value["object_entity_type"], str)
            or not value["object_entity_type"]
            or not isinstance(value["object_entity_key"], str)
            or not value["object_entity_key"]
            or not isinstance(display_name, str)
            or not display_name
            or len(display_name.encode("utf-8")) > 4_096
            or "\x00" in display_name
        ):
            raise _unavailable()
    else:
        raise _unavailable()
    return value


def _validate_proposal(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != _PROPOSAL_KEYS:
        raise _unavailable()
    _uuid(value["proposal_id"])
    _uuid(value["operation_id"])
    for name in (
        "proposal_sha256",
        "source_sha256",
        "selected_sha256",
        "selection_binding_sha256",
        "predicate_catalog_sha256",
    ):
        _sha256(value[name])
    if (
        not isinstance(value["predicate"], str)
        or not value["predicate"]
        or not isinstance(value["epistemic_state"], str)
        or not isinstance(value["sensitivity"], str)
    ):
        raise _unavailable()
    if value["object_kind"] == "literal":
        if (
            not isinstance(value["object_literal"], str)
            or not value["object_literal"]
            or value["object_entity_type"] is not None
            or value["object_entity_key"] is not None
            or value["object_display_name"] is not None
        ):
            raise _unavailable()
    elif value["object_kind"] == "entity":
        if (
            value["object_literal"] is not None
            or not isinstance(value["object_entity_type"], str)
            or not value["object_entity_type"]
            or not isinstance(value["object_entity_key"], str)
            or not value["object_entity_key"]
            or not isinstance(value["object_display_name"], str)
            or not value["object_display_name"]
        ):
            raise _unavailable()
    else:
        raise _unavailable()
    if value["correction_of_claim_id"] is not None:
        _uuid(value["correction_of_claim_id"])
    return value


def _claim_target_text(detail: Mapping[str, object]) -> str:
    if detail["object_kind"] == "literal":
        return str(detail["object_literal"])
    return str(detail["object_display_name"])


def _target_contains_claim_value(*, target: str, claim_value: str) -> bool:
    normalized_value = normalized_chat_text_v1(claim_value)
    if not normalized_value:
        return False
    return re.search(
        rf"(?<!\w){re.escape(normalized_value)}(?!\w)",
        target,
        re.UNICODE,
    ) is not None


def _same_preference_value(left: str, right: str) -> bool:
    return normalized_preference_value_v1(left) == normalized_preference_value_v1(
        right
    )


class ChatMemoryLifecycleRuntimeV1:
    def __init__(
        self,
        *,
        service_token: str | None,
        transport: ErasureProxyTransport | None = None,
        correction_interpreter: PreferenceCorrectionInterpreterV1 | None = None,
    ) -> None:
        self._service_token = (
            service_token
            if governed_memory_proxy_service_token_is_valid(service_token)
            else ""
        )
        self._transport = transport or UnixSocketErasureTransport()
        self._correction_interpreter = correction_interpreter

    @classmethod
    def from_environment(cls) -> "ChatMemoryLifecycleRuntimeV1":
        return cls(
            service_token=os.getenv(SERVICE_TOKEN_ENV),
            correction_interpreter=(
                openai_preference_correction_interpreter_from_environment_v1()
            ),
        )

    async def _request(
        self,
        *,
        method: str,
        path: str,
        authorization: str,
        body: bytes = b"",
    ) -> object:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._transport.request,
                    method=method,
                    path=path,
                    authorization=authorization,
                    service_token=self._service_token,
                    body=body,
                ),
                timeout=REQUEST_DEADLINE_SECONDS,
            )
        except ChatMemoryLifecycleError:
            raise
        except (asyncio.TimeoutError, OSError, ValueError):
            raise _unavailable() from None
        return _decode_json(result)

    async def _preference_claims(
        self,
        *,
        authorization: str,
        ambiguous_code: str,
    ) -> list[Mapping[str, object]]:
        raw_claims = await self._request(
            method="GET",
            path="/memory/claims",
            authorization=authorization,
        )
        if not isinstance(raw_claims, list):
            raise _unavailable()
        summaries: list[Mapping[str, object]] = []
        seen_ids: set[UUID] = set()
        for raw in raw_claims:
            summary = _validate_list_item(raw)
            claim_id = _uuid(summary["claim_id"])
            if claim_id in seen_ids:
                raise _unavailable()
            seen_ids.add(claim_id)
            if (
                summary["predicate"] == "preference.personal"
                and summary["object_kind"] in {"literal", "entity"}
                and summary["lifecycle_state"]
                in {"active", "correction_pending", "retracted"}
            ):
                summaries.append(summary)
        if len(summaries) > MAX_PREFERENCE_CLAIMS:
            raise ChatMemoryLifecycleError(ambiguous_code, 409)
        details: list[Mapping[str, object]] = []
        for summary in summaries:
            claim_id = _uuid(summary["claim_id"])
            details.append(
                _validate_detail(
                    await self._request(
                        method="GET",
                        path=f"/memory/claims/{claim_id}",
                        authorization=authorization,
                    ),
                    summary=summary,
                )
            )
        return details

    async def _apply_retraction(
        self,
        *,
        message: str,
        target: str,
        authorization: str,
    ) -> ChatMemoryLifecycleReceiptV1:
        claims = await self._preference_claims(
            authorization=authorization,
            ambiguous_code="memory_retraction_target_ambiguous",
        )
        matching = [
            claim
            for claim in claims
            if claim["lifecycle_state"] in {"active", "retracted"}
            and _target_contains_claim_value(
                target=target,
                claim_value=_claim_target_text(claim),
            )
        ]
        active = [claim for claim in matching if claim["lifecycle_state"] == "active"]
        retracted = [
            claim for claim in matching if claim["lifecycle_state"] == "retracted"
        ]
        if len(active) > 1 or (not active and len(retracted) > 1):
            raise ChatMemoryLifecycleError(
                "memory_retraction_target_ambiguous", 409
            )
        if not active:
            if len(retracted) == 1:
                return ChatMemoryLifecycleReceiptV1(
                    action="retraction",
                    claim_id=_uuid(retracted[0]["claim_id"]),
                    outcome="replayed",
                )
            raise ChatMemoryLifecycleError("memory_retraction_target_not_found", 409)

        claim = active[0]
        claim_id = _uuid(claim["claim_id"])
        body = json.dumps(
            {
                "expected_revision_sha256": _sha256(claim["revision_sha256"]),
                "expected_state_sha256": _sha256(claim["current_state_sha256"]),
                "operation_id": str(
                    retraction_operation_id_v1(
                        claim_id=claim_id,
                        message=message,
                    )
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        receipt = await self._request(
            method="POST",
            path=f"/memory/claims/{claim_id}/retract",
            authorization=authorization,
            body=body,
        )
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"outcome", "outbox_id"}
            or receipt["outcome"] not in {"retracted", "replayed"}
        ):
            raise _unavailable()
        _uuid(receipt["outbox_id"])
        return ChatMemoryLifecycleReceiptV1(
            action="retraction",
            claim_id=claim_id,
            outcome=str(receipt["outcome"]),
        )

    async def _correction_proposals(
        self,
        *,
        authorization: str,
        claim_id: UUID,
        replacement_literal: str,
    ) -> list[Mapping[str, object]]:
        raw = await self._request(
            method="GET",
            path="/memory/proposals",
            authorization=authorization,
        )
        if not isinstance(raw, list) or len(raw) > 100:
            raise _unavailable()
        proposals = [_validate_proposal(item) for item in raw]
        return [
            proposal
            for proposal in proposals
            if proposal["correction_of_claim_id"] == str(claim_id)
            and proposal["predicate"] == "preference.personal"
            and proposal["object_kind"] == "literal"
            and _same_preference_value(
                str(proposal["object_literal"]), replacement_literal
            )
        ]

    async def _review_correction(
        self,
        *,
        authorization: str,
        claim_id: UUID,
        proposal: Mapping[str, object],
        review_operation_id: UUID,
    ) -> ChatMemoryLifecycleReceiptV1:
        if _uuid(proposal["operation_id"]) != review_operation_id:
            raise _unavailable()
        body = json.dumps(
            {
                "decision": "admit",
                "expected_predicate_catalog_sha256": _sha256(
                    proposal["predicate_catalog_sha256"]
                ),
                "expected_proposal_sha256": _sha256(proposal["proposal_sha256"]),
                "expected_selected_sha256": _sha256(proposal["selected_sha256"]),
                "expected_selection_binding_sha256": _sha256(
                    proposal["selection_binding_sha256"]
                ),
                "expected_source_sha256": _sha256(proposal["source_sha256"]),
                "operation_id": str(review_operation_id),
                "reason_codes": ["explicit_owner_review"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        proposal_id = _uuid(proposal["proposal_id"])
        receipt = await self._request(
            method="POST",
            path=f"/memory/proposals/{proposal_id}/review",
            authorization=authorization,
            body=body,
        )
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"outcome", "claim_id", "revision_id", "outbox_id"}
            or receipt["outcome"] not in {"admitted", "replayed"}
            or _uuid(receipt["claim_id"]) != claim_id
        ):
            raise _unavailable()
        _uuid(receipt["revision_id"])
        _uuid(receipt["outbox_id"])
        return ChatMemoryLifecycleReceiptV1(
            action="correction",
            claim_id=claim_id,
            outcome=str(receipt["outcome"]),
        )

    async def _apply_correction(
        self,
        *,
        message: str,
        command: ExplicitPreferenceCorrectionCommandV1,
        authorization: str,
        claims: list[Mapping[str, object]] | None = None,
    ) -> ChatMemoryLifecycleReceiptV1:
        if claims is None:
            claims = await self._preference_claims(
                authorization=authorization,
                ambiguous_code="memory_correction_target_ambiguous",
            )
        old = [
            claim
            for claim in claims
            if claim["lifecycle_state"] in {"active", "correction_pending"}
            and _same_preference_value(
                _claim_target_text(claim), command.previous_literal
            )
        ]
        replacement = [
            claim
            for claim in claims
            if claim["lifecycle_state"] == "active"
            and _same_preference_value(
                _claim_target_text(claim), command.replacement_literal
            )
        ]
        if len(old) > 1 or len(replacement) > 1:
            raise ChatMemoryLifecycleError("memory_correction_target_ambiguous", 409)
        if old and replacement and old[0]["claim_id"] != replacement[0]["claim_id"]:
            raise ChatMemoryLifecycleError("memory_correction_target_ambiguous", 409)
        if not old:
            if len(replacement) == 1:
                return ChatMemoryLifecycleReceiptV1(
                    action="correction",
                    claim_id=_uuid(replacement[0]["claim_id"]),
                    outcome="replayed",
                )
            raise ChatMemoryLifecycleError("memory_correction_target_not_found", 409)

        claim = old[0]
        claim_id = _uuid(claim["claim_id"])
        if claim["lifecycle_state"] == "correction_pending":
            proposals = await self._correction_proposals(
                authorization=authorization,
                claim_id=claim_id,
                replacement_literal=command.replacement_literal,
            )
            if len(proposals) != 1:
                raise _unavailable()
            proposal = proposals[0]
            return await self._review_correction(
                authorization=authorization,
                claim_id=claim_id,
                proposal=proposal,
                review_operation_id=_uuid(proposal["operation_id"]),
            )

        operation_id = correction_operation_id_v1(
            claim_id=claim_id,
            message=message,
        )
        body = json.dumps(
            {
                "expected_predicate_catalog_sha256": _sha256(
                    claim["predicate_catalog_sha256"]
                ),
                "expected_revision_sha256": _sha256(claim["revision_sha256"]),
                "expected_state_sha256": _sha256(claim["current_state_sha256"]),
                "operation_id": str(operation_id),
                "replacement": {
                    "epistemic_state": str(claim["epistemic_state"]),
                    "object_display_name": None,
                    "object_entity_type": None,
                    "object_kind": "literal",
                    "object_literal": command.replacement_literal,
                    "sensitivity": str(claim["sensitivity"]),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        receipt = await self._request(
            method="POST",
            path=f"/memory/claims/{claim_id}/correct",
            authorization=authorization,
            body=body,
        )
        if (
            not isinstance(receipt, dict)
            or set(receipt)
            != {"outcome", "proposal_id", "proposal_sha256", "review_operation_id"}
            or receipt["outcome"] not in {"correction_pending", "replayed"}
        ):
            raise _unavailable()
        proposal_id = _uuid(receipt["proposal_id"])
        proposal_sha256 = _sha256(receipt["proposal_sha256"])
        review_operation_id = _uuid(receipt["review_operation_id"])
        proposals = await self._correction_proposals(
            authorization=authorization,
            claim_id=claim_id,
            replacement_literal=command.replacement_literal,
        )
        exact = [
            proposal
            for proposal in proposals
            if _uuid(proposal["proposal_id"]) == proposal_id
            and _sha256(proposal["proposal_sha256"]) == proposal_sha256
        ]
        if len(exact) != 1:
            raise _unavailable()
        return await self._review_correction(
            authorization=authorization,
            claim_id=claim_id,
            proposal=exact[0],
            review_operation_id=review_operation_id,
        )

    async def apply_if_requested(
        self,
        *,
        message: str,
        authorization: str,
        owner_user_id: UUID | None = None,
    ) -> ChatMemoryLifecycleReceiptV1 | None:
        correction = explicit_preference_correction_command_v1(message)
        retraction = explicit_preference_retraction_target_v1(message)
        flexible_candidate = (
            correction is None
            and retraction is None
            and self._correction_interpreter is not None
            and potential_preference_correction_v1(message)
        )
        if correction is None and retraction is None and not flexible_candidate:
            return None
        if not self._service_token:
            raise ChatMemoryLifecycleError("memory_lifecycle_unconfigured", 503)
        if _BEARER_RE.fullmatch(authorization) is None:
            raise ChatMemoryLifecycleError("memory_authentication_required", 401)
        if correction is not None:
            return await self._apply_correction(
                message=message,
                command=correction,
                authorization=authorization,
            )
        if retraction is not None:
            return await self._apply_retraction(
                message=message,
                target=retraction,
                authorization=authorization,
            )
        if owner_user_id is None or self._correction_interpreter is None:
            raise ChatMemoryLifecycleError("memory_lifecycle_unconfigured", 503)
        claims = await self._preference_claims(
            authorization=authorization,
            ambiguous_code="memory_correction_target_ambiguous",
        )
        if not claims:
            return None
        interpreter_claims = tuple(
            PreferenceCorrectionClaimV1(
                claim_id=_uuid(claim["claim_id"]),
                lifecycle_state=str(claim["lifecycle_state"]),
                current_value=_claim_target_text(claim),
            )
            for claim in claims
            if claim["lifecycle_state"] in {"active", "correction_pending"}
        )
        if not interpreter_claims:
            return None
        try:
            interpreted = await self._correction_interpreter.interpret(
                owner_user_id=owner_user_id,
                message=message,
                claims=interpreter_claims,
            )
        except PreferenceCorrectionInterpretationUnavailableV1:
            logger.error("[memory_correction] interpreter unavailable")
            raise ChatMemoryLifecycleError(
                "memory_correction_interpreter_unavailable", 503
            ) from None
        if interpreted is None:
            return None
        return await self._apply_correction(
            message=message,
            command=interpreted,
            authorization=authorization,
            claims=claims,
        )

    def requires_ingest_coordination(self, message: str) -> bool:
        """Whether this runtime may synchronously consume the chat message."""

        if explicit_preference_correction_command_v1(message) is not None:
            return True
        if explicit_preference_retraction_target_v1(message) is not None:
            return True
        return (
            self._correction_interpreter is not None
            and potential_preference_correction_v1(message)
        )


__all__ = [
    "ChatMemoryLifecycleError",
    "ChatMemoryLifecycleReceiptV1",
    "ChatMemoryLifecycleRuntimeV1",
    "correction_operation_id_v1",
    "retraction_operation_id_v1",
]
