#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import ValidationError

from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    EXTRACTION_INSTRUCTIONS,
    MODEL_RE,
    _registry_contract,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    CONTRACT_VERSION,
    ProviderPacket,
    TrustedExtractionSource,
    canonical_json,
    canonical_sha256,
)


LOCAL_PROVIDER_ID = "local_llama_cpp"
LOCAL_PROVIDER_VERSION = "v1"
LOCAL_CALL_ENABLE_TOKEN = "memory_v1_local_v5_inference_v1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
LLAMA_CPP_MAX_GRAMMAR_STRING_REPETITION = 1024
LOCAL_GRAMMAR_MAX_ITEMS = {
    "comparison_hints": 4,
    "deferrals": 4,
    "entity_mentions": 8,
    "observations": 8,
    "packet_findings": 0,
    "reason_codes": 4,
    "source_spans": 4,
}
LOCAL_IDENTITY_EXAMPLE_SOURCE = "My name is Rowan."
LOCAL_IDENTITY_EXAMPLE_PACKET = canonical_json(
    {
        "comparison_hints": [],
        "deferrals": [],
        "entity_mentions": [
            {
                "entity_ref": "e00",
                "entity_type": "self",
                "extraction_confidence": 0.99,
                "mention_kind": "self_reference",
                "name_text": None,
                "reason_codes": ["explicit_self_reference"],
                "relationship_role": "user:self",
                "source_spans": [
                    {
                        "start": 0,
                        "end": len(LOCAL_IDENTITY_EXAMPLE_SOURCE),
                        "quote": LOCAL_IDENTITY_EXAMPLE_SOURCE,
                    }
                ],
            }
        ],
        "observations": [
            {
                "extraction_confidence": 0.99,
                "modality": "asserted",
                "object": {
                    "approximate": False,
                    "datatype": "text",
                    "kind": "literal",
                    "unit": None,
                    "value": "Rowan",
                },
                "observation_ref": "o00",
                "polarity": "affirmed",
                "predicate": "identity.name",
                "projection_class": "direct_claim",
                "reason_codes": ["explicit_name_statement"],
                "sensitivity": "medium",
                "source_spans": [
                    {
                        "start": 0,
                        "end": len(LOCAL_IDENTITY_EXAMPLE_SOURCE),
                        "quote": LOCAL_IDENTITY_EXAMPLE_SOURCE,
                    }
                ],
                "subject_entity_ref": "e00",
                "surface_policy": "direct_or_relevant",
                "temporal": {
                    "anchored_to_source_time": False,
                    "basis": "none",
                    "calendar_range": None,
                    "certainty": "unknown",
                    "instant": None,
                    "instant_range": None,
                    "precision": "unknown",
                    "reason_codes": ["implicit_source_time"],
                    "recurrence": None,
                    "relative_offset": None,
                    "semantic": "observation_time",
                    "shape": "none",
                    "source_form": "implicit_source_time",
                },
            }
        ],
        "packet_findings": [],
    }
)


def _example_span(source: str) -> dict[str, Any]:
    return {"start": 0, "end": len(source), "quote": source}


def _example_temporal(semantic: str = "observation_time") -> dict[str, Any]:
    return {
        "anchored_to_source_time": False,
        "basis": "none",
        "calendar_range": None,
        "certainty": "unknown",
        "instant": None,
        "instant_range": None,
        "precision": "unknown",
        "reason_codes": ["implicit_source_time"],
        "recurrence": None,
        "relative_offset": None,
        "semantic": semantic,
        "shape": "none",
        "source_form": "implicit_source_time",
    }


def _example_entity(
    source: str,
    *,
    entity_ref: str,
    entity_type: str,
    mention_kind: str,
    name_text: str | None,
    relationship_role: str | None,
    reason_code: str,
) -> dict[str, Any]:
    return {
        "entity_ref": entity_ref,
        "entity_type": entity_type,
        "extraction_confidence": 0.99,
        "mention_kind": mention_kind,
        "name_text": name_text,
        "reason_codes": [reason_code],
        "relationship_role": relationship_role,
        "source_spans": [_example_span(source)],
    }


def _example_observation(
    source: str,
    *,
    observation_ref: str,
    subject_entity_ref: str,
    predicate: str,
    object_value: dict[str, Any],
    projection_class: str,
    surface_policy: str,
    sensitivity: str,
    reason_code: str,
    modality: str = "asserted",
    polarity: str = "affirmed",
    temporal_semantic: str = "observation_time",
) -> dict[str, Any]:
    return {
        "extraction_confidence": 0.99,
        "modality": modality,
        "object": object_value,
        "observation_ref": observation_ref,
        "polarity": polarity,
        "predicate": predicate,
        "projection_class": projection_class,
        "reason_codes": [reason_code],
        "sensitivity": sensitivity,
        "source_spans": [_example_span(source)],
        "subject_entity_ref": subject_entity_ref,
        "surface_policy": surface_policy,
        "temporal": _example_temporal(temporal_semantic),
    }


def _literal(
    datatype: str,
    value: Any,
    *,
    unit: str | None = None,
    approximate: bool = False,
) -> dict[str, Any]:
    return {
        "approximate": approximate,
        "datatype": datatype,
        "kind": "literal",
        "unit": unit,
        "value": value,
    }


def _packet(
    *,
    entities: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    comparisons: list[dict[str, Any]] | None = None,
    deferrals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "comparison_hints": comparisons or [],
        "deferrals": deferrals or [],
        "entity_mentions": entities or [],
        "observations": observations or [],
        "packet_findings": [],
    }


def _deferral_example(
    source: str,
    reason_code: str,
    *,
    sensitivity: str = "low",
) -> tuple[str, dict[str, Any]]:
    return (
        source,
        _packet(
            deferrals=[
                {
                    "memory_shape": "none",
                    "reason_code": reason_code,
                    "sensitivity": sensitivity,
                    "source_spans": [_example_span(source)],
                }
            ]
        ),
    )


def _local_examples() -> tuple[tuple[str, dict[str, Any]], ...]:
    life = "I enjoy jazz music."
    response = "Please keep every response brief."
    project = (
        "In Project Beacon, account queries must enforce owner isolation."
    )
    occupation = "I work as a carpenter."
    pet = "My dog Nova is a female Labrador."
    correction = "My cat's correct name is Kira, not Kyra."
    self_life = _example_entity(
        life,
        entity_ref="e00",
        entity_type="self",
        mention_kind="self_reference",
        name_text=None,
        relationship_role="user:self",
        reason_code="explicit_self_reference",
    )
    self_response = _example_entity(
        response,
        entity_ref="e00",
        entity_type="self",
        mention_kind="self_reference",
        name_text=None,
        relationship_role="user:self",
        reason_code="explicit_self_reference",
    )
    project_entity = _example_entity(
        project,
        entity_ref="e00",
        entity_type="project",
        mention_kind="named",
        name_text="Project Beacon",
        relationship_role="project:named",
        reason_code="explicit_project_name",
    )
    occupation_self = _example_entity(
        occupation,
        entity_ref="e00",
        entity_type="self",
        mention_kind="self_reference",
        name_text=None,
        relationship_role="user:self",
        reason_code="explicit_self_reference",
    )
    occupation_concept = _example_entity(
        occupation,
        entity_ref="e01",
        entity_type="concept",
        mention_kind="named",
        name_text="carpenter",
        relationship_role="occupation:reported",
        reason_code="explicit_occupation_concept",
    )
    pet_self = _example_entity(
        pet,
        entity_ref="e00",
        entity_type="self",
        mention_kind="self_reference",
        name_text=None,
        relationship_role="user:self",
        reason_code="explicit_self_reference",
    )
    pet_animal = _example_entity(
        pet,
        entity_ref="e01",
        entity_type="animal",
        mention_kind="named",
        name_text="Nova",
        relationship_role="pet:current:1",
        reason_code="explicit_named_pet",
    )
    correction_animal = _example_entity(
        correction,
        entity_ref="e00",
        entity_type="animal",
        mention_kind="named",
        name_text="Kira",
        relationship_role="pet:corrected_name_subject",
        reason_code="explicit_corrected_pet_name",
    )
    return (
        _deferral_example("Do you remember my favorite color?", "question_only"),
        _deferral_example(
            "I feel sleepy this afternoon.",
            "transient_state",
        ),
        _deferral_example(
            "I logged four sets of deadlifts.",
            "structured_domain",
            sensitivity="medium",
        ),
        (
            life,
            _packet(
                entities=[self_life],
                observations=[
                    _example_observation(
                        life,
                        observation_ref="o00",
                        subject_entity_ref="e00",
                        predicate="preference.life",
                        object_value=_literal(
                            "json",
                            {
                                "context": None,
                                "domain": "music",
                                "polarity": "likes",
                                "target": "jazz music",
                            },
                        ),
                        projection_class="life_preference",
                        surface_policy=(
                            "relevant_recommendation_or_explicit_recall"
                        ),
                        sensitivity="medium",
                        reason_code="explicit_stable_life_preference",
                        modality="endorsed",
                        temporal_semantic="state_validity",
                    )
                ],
            ),
        ),
        (
            response,
            _packet(
                entities=[self_response],
                observations=[
                    _example_observation(
                        response,
                        observation_ref="o00",
                        subject_entity_ref="e00",
                        predicate="preference.response",
                        object_value=_literal(
                            "json",
                            {
                                "dimension": "response_length",
                                "value": "brief",
                            },
                        ),
                        projection_class="response_preference",
                        surface_policy="zero_token_control_only",
                        sensitivity="low",
                        reason_code="explicit_stable_response_instruction",
                        modality="endorsed",
                        temporal_semantic="state_validity",
                    )
                ],
            ),
        ),
        (
            project,
            _packet(
                entities=[project_entity],
                observations=[
                    _example_observation(
                        project,
                        observation_ref="o00",
                        subject_entity_ref="e00",
                        predicate="project.requirement",
                        object_value=_literal(
                            "text",
                            "account queries must enforce owner isolation",
                        ),
                        projection_class="project_knowledge",
                        surface_policy="exact_project_scope_only",
                        sensitivity="medium",
                        reason_code="explicit_project_requirement",
                        temporal_semantic="state_validity",
                    )
                ],
                deferrals=[
                    {
                        "memory_shape": "project_knowledge",
                        "reason_code": "project_scope_unresolved",
                        "sensitivity": "medium",
                        "source_spans": [_example_span(project)],
                    }
                ],
            ),
        ),
        (
            occupation,
            _packet(
                entities=[occupation_self, occupation_concept],
                observations=[
                    _example_observation(
                        occupation,
                        observation_ref="o00",
                        subject_entity_ref="e00",
                        predicate="occupation.works_as",
                        object_value={"entity_ref": "e01", "kind": "entity"},
                        projection_class="direct_claim",
                        surface_policy="direct_or_relevant",
                        sensitivity="medium",
                        reason_code="explicit_occupation_statement",
                        temporal_semantic="state_validity",
                    )
                ],
            ),
        ),
        (
            pet,
            _packet(
                entities=[pet_self, pet_animal],
                observations=[
                    _example_observation(
                        pet,
                        observation_ref="o00",
                        subject_entity_ref="e00",
                        predicate="relationship.has_pet",
                        object_value={"entity_ref": "e01", "kind": "entity"},
                        projection_class="direct_claim",
                        surface_policy="direct_or_relevant",
                        sensitivity="low",
                        reason_code="explicit_pet_relationship",
                        temporal_semantic="state_validity",
                    ),
                    _example_observation(
                        pet,
                        observation_ref="o01",
                        subject_entity_ref="e01",
                        predicate="identity.name",
                        object_value=_literal("text", "Nova"),
                        projection_class="direct_claim",
                        surface_policy="direct_or_relevant",
                        sensitivity="medium",
                        reason_code="explicit_pet_name",
                    ),
                    _example_observation(
                        pet,
                        observation_ref="o02",
                        subject_entity_ref="e01",
                        predicate="pet.species",
                        object_value=_literal("text", "dog"),
                        projection_class="direct_claim",
                        surface_policy="direct_or_relevant",
                        sensitivity="low",
                        reason_code="explicit_pet_species",
                    ),
                    _example_observation(
                        pet,
                        observation_ref="o03",
                        subject_entity_ref="e01",
                        predicate="pet.sex",
                        object_value=_literal("enum", "female"),
                        projection_class="direct_claim",
                        surface_policy="direct_or_relevant",
                        sensitivity="low",
                        reason_code="explicit_pet_sex",
                    ),
                    _example_observation(
                        pet,
                        observation_ref="o04",
                        subject_entity_ref="e01",
                        predicate="pet.breed",
                        object_value=_literal("text", "Labrador"),
                        projection_class="direct_claim",
                        surface_policy="direct_or_relevant",
                        sensitivity="low",
                        reason_code="explicit_pet_breed",
                    ),
                ],
            ),
        ),
        (
            correction,
            _packet(
                entities=[correction_animal],
                observations=[
                    _example_observation(
                        correction,
                        observation_ref="o00",
                        subject_entity_ref="e00",
                        predicate="identity.name_canonical",
                        object_value=_literal("text", "Kira"),
                        projection_class="correction",
                        surface_policy="normalization_only",
                        sensitivity="medium",
                        reason_code="explicit_name_correction",
                        modality="corrective",
                        temporal_semantic="state_validity",
                    )
                ],
                comparisons=[
                    {
                        "observation_ref": "o00",
                        "reason_codes": ["explicit_prior_name_rejected"],
                        "relation_type": "corrects",
                        "target_lookup_key": "identity.name:kyra",
                    },
                    {
                        "observation_ref": "o00",
                        "reason_codes": ["canonical_name_supersedes_prior"],
                        "relation_type": "supersedes",
                        "target_lookup_key": "identity.name:kyra",
                    },
                ],
            ),
        ),
    )


LOCAL_FEW_SHOT_EXAMPLES = "\n\n".join(
    "STRUCTURE_EXAMPLE_SOURCE_CONTENT="
    f"{source}\nSTRUCTURE_EXAMPLE_PROVIDER_PACKET={canonical_json(packet)}"
    for source, packet in _local_examples()
)
LOCAL_EXTRACTION_INSTRUCTIONS = (
    f"{EXTRACTION_INSTRUCTIONS}\n"
    "Within each reason_codes array, values must be unique. "
    "packet_findings values must also be unique. For simple source records, "
    "emit only the minimum supported entities and observations; leave all "
    "other arrays empty unless the source requires them. A self mention uses "
    "mention_kind=self_reference and relationship_role=user:self. Always emit "
    "anchored_to_source_time=false; the server alone performs trusted temporal "
    "anchoring. For an undated present identity statement, use semantic="
    "observation_time, shape=none, basis=none, source_form=implicit_source_time, "
    "certainty=unknown, precision=unknown, and null for instant, all ranges, "
    "relative_offset, and recurrence. Unused nullable values are null, never an "
    "empty string. A self mention has name_text=null; the stated name belongs in "
    "the identity.name observation object. If a source needs more than eight "
    "entities or observations, defer it as compound_requires_split. Always emit "
    "packet_findings as an empty array. Every observation subject and every "
    "entity-valued object must reference an entity_mentions entry. Never use "
    "self as an animal, project, place, or concept; create separate entities. "
    "Use only a governed predicate exactly as supplied. Questions, transient "
    "states, and structured application data require their demonstrated "
    "deferral and no observation. An explicit 'X, not Y' name correction uses "
    "identity.name_canonical with correction projection and unresolved corrects "
    "and supersedes comparison hints.\n\n"
    f"STRUCTURE_EXAMPLE_SOURCE_CONTENT={LOCAL_IDENTITY_EXAMPLE_SOURCE}\n"
    f"STRUCTURE_EXAMPLE_PROVIDER_PACKET={LOCAL_IDENTITY_EXAMPLE_PACKET}\n"
    f"{LOCAL_FEW_SHOT_EXAMPLES}\n"
    "These examples demonstrate structure only. Extract values and exact Python "
    "Unicode offsets from the actual SOURCE_CONTENT, never from an example."
)


class LocalProviderAdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        http_status: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


@dataclass(frozen=True)
class LocalStructuredRequest:
    model: str
    instructions: str
    input_text: str
    output_schema: dict[str, Any]
    max_output_tokens: int
    timeout_seconds: float
    seed: int = 1
    temperature: float = 0.2
    top_k: int = 20
    top_p: float = 0.8
    min_p: float = 0.0

    def body(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.instructions},
                {"role": "user", "content": self.input_text},
            ],
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "min_p": self.min_p,
            "seed": self.seed,
            "max_tokens": self.max_output_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "memory_v1_provider_packet_v5",
                    "strict": True,
                    "schema": self.output_schema,
                },
            },
        }

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self.body())


@dataclass(frozen=True)
class LocalStructuredResult:
    response_id: str | None
    model: str
    finish_reason: str
    parsed: Any
    response_sha256: str
    prompt_tokens: int | None
    completion_tokens: int | None


class LocalStructuredTransport(Protocol):
    external_call_capability: bool
    external_model_calls: int
    local_model_calls: int

    def complete(self, request: LocalStructuredRequest) -> LocalStructuredResult:
        ...


class StaticLocalStructuredTransport:
    external_call_capability = False
    external_model_calls = 0

    def __init__(
        self,
        result: LocalStructuredResult | None = None,
        error: LocalProviderAdapterError | None = None,
    ) -> None:
        if (result is None) == (error is None):
            raise ValueError("static transport requires exactly one result or error")
        self._result = result
        self._error = error
        self.local_model_calls = 0
        self.requests: list[LocalStructuredRequest] = []

    def complete(self, request: LocalStructuredRequest) -> LocalStructuredResult:
        self.requests.append(request)
        self.local_model_calls += 1
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise AssertionError("static local transport result disappeared")
        return self._result


class LlamaCppSecureTransport:
    external_call_capability = False
    external_model_calls = 0

    def __init__(
        self,
        *,
        endpoint: str,
        enable_token: str | None,
        api_key: str | None = None,
        ca_file: Path | None = None,
        client_cert_file: Path | None = None,
        client_key_file: Path | None = None,
        allow_loopback_http: bool = False,
        allow_unauthenticated_loopback: bool = False,
    ) -> None:
        self._enabled = enable_token == LOCAL_CALL_ENABLE_TOKEN
        self._endpoint = _validated_endpoint(
            endpoint,
            allow_loopback_http=allow_loopback_http,
        )
        self._api_key = _validated_api_key(api_key)
        if self._api_key is None and not (
            allow_unauthenticated_loopback
            and urlsplit(self._endpoint).scheme == "http"
            and _is_loopback_host(urlsplit(self._endpoint).hostname or "")
        ):
            raise ValueError("local inference requires an API key")
        self._ssl_context = _ssl_context(
            self._endpoint,
            ca_file=ca_file,
            client_cert_file=client_cert_file,
            client_key_file=client_key_file,
        )
        self.local_model_calls = 0

    def complete(self, request: LocalStructuredRequest) -> LocalStructuredResult:
        if not self._enabled:
            raise LocalProviderAdapterError(
                "local_provider_disabled",
                retryable=False,
            )
        body = canonical_json(request.body()).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        http_request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        self.local_model_calls += 1
        try:
            with urllib.request.urlopen(
                http_request,
                timeout=request.timeout_seconds,
                context=self._ssl_context,
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise LocalProviderAdapterError(
                _http_error_code(exc.code),
                retryable=exc.code in {408, 425, 429, 500, 502, 503, 504},
                http_status=exc.code,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LocalProviderAdapterError(
                "local_transport_timeout",
                retryable=True,
            ) from exc
        except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
            raise LocalProviderAdapterError(
                "local_transport_unavailable",
                retryable=True,
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LocalProviderAdapterError(
                "local_response_too_large",
                retryable=False,
            )
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalProviderAdapterError(
                "local_response_invalid_json",
                retryable=False,
            ) from exc
        return _structured_result(value)


class LocalLlamaCppProvider:
    provider_id = LOCAL_PROVIDER_ID
    provider_version = LOCAL_PROVIDER_VERSION
    external_call_capability = False

    def __init__(
        self,
        *,
        model: str,
        model_file_sha256: str,
        runtime_revision: str,
        registry: dict[str, Any],
        transport: LocalStructuredTransport,
        max_output_tokens: int = 16000,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            raise ValueError("local provider model identifier is invalid")
        if not _is_sha256(model_file_sha256):
            raise ValueError("local model file SHA-256 is invalid")
        if not isinstance(runtime_revision, str) or not MODEL_RE.fullmatch(
            runtime_revision
        ):
            raise ValueError("local runtime revision is invalid")
        if not 1000 <= int(max_output_tokens) <= 20000:
            raise ValueError("max_output_tokens must be between 1000 and 20000")
        if not 1.0 <= float(timeout_seconds) <= 600.0:
            raise ValueError("timeout_seconds must be between 1 and 600")
        self._model = model
        self._model_file_sha256 = model_file_sha256
        self._runtime_revision = runtime_revision
        self._registry_contract = _registry_contract(registry)
        self._allowed_predicates = tuple(
            sorted(item["predicate"] for item in registry["predicates"])
        )
        self._transport = transport
        self._max_output_tokens = int(max_output_tokens)
        self._timeout_seconds = float(timeout_seconds)
        self.last_audit: dict[str, Any] | None = None

    @property
    def external_model_calls(self) -> int:
        return int(self._transport.external_model_calls)

    @property
    def local_model_calls(self) -> int:
        return int(self._transport.local_model_calls)

    def request(self, source: TrustedExtractionSource) -> LocalStructuredRequest:
        input_text = (
            "TRUSTED_SOURCE_TIME="
            f"{source.source_recorded_at}\n"
            "Offsets are Python Unicode offsets into SOURCE_CONTENT only.\n"
            "SOURCE_CONTENT_START\n"
            f"{source.content}\n"
            "SOURCE_CONTENT_END"
        )
        return LocalStructuredRequest(
            model=self._model,
            instructions=(
                f"{LOCAL_EXTRACTION_INSTRUCTIONS}\n\n"
                "GOVERNED_PREDICATE_REGISTRY\n"
                f"{self._registry_contract}"
            ),
            input_text=input_text,
            output_schema=_llama_cpp_output_schema(
                ProviderPacket.model_json_schema(),
                allowed_predicates=self._allowed_predicates,
            ),
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
        )

    def extract(self, source: TrustedExtractionSource) -> ProviderPacket:
        request = self.request(source)
        self.last_audit = {
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "model_sha256": canonical_sha256(self._model),
            "model_file_sha256": self._model_file_sha256,
            "runtime_revision_sha256": canonical_sha256(
                self._runtime_revision
            ),
            "request_sha256": request.request_sha256,
            "output_schema_sha256": canonical_sha256(
                request.output_schema
            ),
            "response_status": "request_pending",
            "response_sha256": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "error_code": None,
        }
        try:
            result = self._transport.complete(request)
        except LocalProviderAdapterError as exc:
            self.last_audit.update(
                {
                    "response_status": "request_error",
                    "error_code": exc.code,
                }
            )
            raise
        self.last_audit.update(
            {
                "response_status": "completed",
                "response_sha256": result.response_sha256,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            }
        )
        if result.model != self._model:
            self.last_audit["error_code"] = "local_model_alias_mismatch"
            raise LocalProviderAdapterError(
                "local_model_alias_mismatch",
                retryable=False,
            )
        if result.finish_reason != "stop":
            self.last_audit["error_code"] = "local_incomplete_response"
            raise LocalProviderAdapterError(
                "local_incomplete_response",
                retryable=True,
            )
        try:
            return ProviderPacket.model_validate(result.parsed)
        except (ValidationError, TypeError, ValueError) as exc:
            self.last_audit["error_code"] = "invalid_structured_output"
            raise LocalProviderAdapterError(
                "invalid_structured_output",
                retryable=False,
            ) from exc


def _validated_endpoint(endpoint: str, *, allow_loopback_http: bool) -> str:
    parsed = urlsplit(endpoint)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("local endpoint may not contain credentials or query data")
    if parsed.path.rstrip("/") != "/v1/chat/completions":
        raise ValueError("local endpoint path must be /v1/chat/completions")
    if parsed.hostname is None or parsed.port is None:
        raise ValueError("local endpoint requires an explicit host and port")
    host = parsed.hostname
    if parsed.scheme == "http":
        if not allow_loopback_http or not _is_loopback_host(host):
            raise ValueError("plaintext local inference is loopback-test-only")
    elif parsed.scheme == "https":
        if not _all_resolved_addresses_private(host, parsed.port):
            raise ValueError("local inference endpoint must resolve only privately")
    else:
        raise ValueError("local endpoint must use HTTPS")
    return endpoint


def _llama_cpp_output_schema(
    value: dict[str, Any],
    *,
    allowed_predicates: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Keep the canonical validator strict while avoiding unsafe GBNF repeats."""
    schema = deepcopy(value)

    def normalize(item: Any, property_name: str | None = None) -> None:
        if isinstance(item, dict):
            maximum = item.get("maxLength")
            if (
                isinstance(maximum, int)
                and maximum > LLAMA_CPP_MAX_GRAMMAR_STRING_REPETITION
            ):
                item.pop("maxLength")
            if property_name in LOCAL_GRAMMAR_MAX_ITEMS and (
                item.get("type") == "array"
            ):
                item["maxItems"] = LOCAL_GRAMMAR_MAX_ITEMS[property_name]
            properties = item.get("properties")
            if isinstance(properties, dict):
                for name, child in properties.items():
                    normalize(child, name)
            for key, child in item.items():
                if key != "properties":
                    normalize(child, property_name)
        elif isinstance(item, list):
            for child in item:
                normalize(child, property_name)

    normalize(schema)
    observation = schema.get("$defs", {}).get("ProviderObservation", {})
    predicate = observation.get("properties", {}).get("predicate")
    if allowed_predicates and isinstance(predicate, dict):
        predicate.pop("pattern", None)
        predicate["enum"] = list(allowed_predicates)
    return schema


def _ssl_context(
    endpoint: str,
    *,
    ca_file: Path | None,
    client_cert_file: Path | None,
    client_key_file: Path | None,
) -> ssl.SSLContext | None:
    if urlsplit(endpoint).scheme == "http":
        return None
    if ca_file is None or client_cert_file is None or client_key_file is None:
        raise ValueError("HTTPS local inference requires CA and client identity")
    context = ssl.create_default_context(cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(
        certfile=str(client_cert_file),
        keyfile=str(client_key_file),
    )
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _validated_api_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 32 <= len(value) <= 500:
        raise ValueError("local inference API key must contain 32 to 500 characters")
    if any(character.isspace() for character in value):
        raise ValueError("local inference API key may not contain whitespace")
    return value


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _all_resolved_addresses_private(host: str, port: int) -> bool:
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError("local inference endpoint did not resolve") from exc
    if not addresses:
        return False
    return all(_is_approved_private_address(address) for address in addresses)


def _is_approved_private_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("::1/128"),
    )
    return any(address in network for network in networks)


def _structured_result(value: Any) -> LocalStructuredResult:
    if not isinstance(value, dict):
        raise LocalProviderAdapterError(
            "local_response_shape_invalid",
            retryable=False,
        )
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise LocalProviderAdapterError(
            "local_response_choice_count_invalid",
            retryable=False,
        )
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        raise LocalProviderAdapterError(
            "local_response_message_invalid",
            retryable=False,
        )
    if message.get("reasoning_content") not in {None, ""}:
        raise LocalProviderAdapterError(
            "local_reasoning_content_forbidden",
            retryable=False,
        )
    content = message.get("content")
    if not isinstance(content, str):
        raise LocalProviderAdapterError(
            "local_response_content_invalid",
            retryable=False,
        )
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LocalProviderAdapterError(
            "local_structured_content_invalid",
            retryable=False,
        ) from exc
    usage = value.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    response_id = value.get("id")
    model = value.get("model")
    finish_reason = choice.get("finish_reason")
    if not isinstance(model, str) or not isinstance(finish_reason, str):
        raise LocalProviderAdapterError(
            "local_response_metadata_invalid",
            retryable=False,
        )
    return LocalStructuredResult(
        response_id=str(response_id) if response_id else None,
        model=model,
        finish_reason=finish_reason,
        parsed=parsed,
        response_sha256=canonical_sha256(value),
        prompt_tokens=_optional_nonnegative_int(usage.get("prompt_tokens")),
        completion_tokens=_optional_nonnegative_int(
            usage.get("completion_tokens")
        ),
    )


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LocalProviderAdapterError(
            "local_response_usage_invalid",
            retryable=False,
        )
    return value


def _http_error_code(status: int) -> str:
    if status in {401, 403}:
        return "local_transport_auth_rejected"
    if status == 429:
        return "local_transport_rate_limited"
    if status >= 500:
        return "local_transport_server_error"
    return "local_transport_http_rejected"


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)
