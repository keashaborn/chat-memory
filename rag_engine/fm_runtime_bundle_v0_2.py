from __future__ import annotations

"""Pinned, immutable runtime projection of canonical Fractal Monism v0.2.

The runtime loader intentionally has no YAML, retrieval, database, network, or
provider dependency. The YAML compiler is an offline build tool; production
loads only the reviewed JSON artifact beside this module.
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
import re
from typing import Annotated, Any, Literal, Mapping, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


BUNDLE_CONTRACT_VERSION = "fm_runtime_bundle_v0_2"
RUNTIME_REFERENCE = "fm_v0_2"
SEMANTIC_VERSION = "0.2"
CANONICAL_MANIFEST_ID = "fm-canonical-v0.2-manifest"
CANONICAL_MANIFEST_SHA256 = (
    "1d2912854b368f2a802752ad0c2d1a37a09f700c925e7beb842724e47acf627d"
)
COMPILED_BUNDLE_SHA256 = (
    "a50a256e5d8b84d7521b816054019ab09a4f5a0e4e31b12bf4ecd036f4e002fe"
)
DEFAULT_BUNDLE_PATH = Path(__file__).resolve().parent / "data" / "fm_v0_2_runtime_bundle.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROVENANCE_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

EXPECTED_SOURCE_ARTIFACT_SHA256: dict[str, str] = {
    "semantic_model/FM_AUTHOR_QUESTIONS_v0_2.md": (
        "bd286edb3e042be18d574bed82d64120893c75b08edfcd96850b98d43d33cf2f"
    ),
    "semantic_model/FM_CANONICAL_V0_2_HANDOFF.md": (
        "6daa0e73e38b596c580f8be2480ad3e061a898ce60fbf8c48db972035f3743dc"
    ),
    "semantic_model/FM_CONCEPT_SCHEMA_v0_2.yaml": (
        "5d8674127a3902d91fd2bb5d3ef43e2b475d476fe1a1bd6e97c3c84a304808b0"
    ),
    "semantic_model/FM_CORE_MODEL_v0_2.yaml": (
        "4efa55403f474a837edc0d9d477c825cdf176293fe198f376326166416c75f0f"
    ),
    "semantic_model/FM_INFERENCE_RULES_v0_2.yaml": (
        "9b02dd60f366230c72f27948c5ca633c5294d00e10f235fe586646a6a71e4de7"
    ),
    "semantic_model/FM_KERNEL_v0_2.md": (
        "c0db8af61d7ed531a35bdc09ebaa4704c7a161ce6fbed2686f60c1387ce383e2"
    ),
    "semantic_model/FM_REASONING_EVALS_v0_2.jsonl": (
        "1db912c8cd42d3fe37e0971ce302577a8c2369103a264f9ca07614ba53237979"
    ),
    "semantic_model/FM_TENSIONS_v0_2.md": (
        "b7037d0992b07b5d5646cca9aa47fb0f2ac64396c4b6d4b38d1b2dfe8de0e5f7"
    ),
    "semantic_model/FM_VALIDATION_v0_2.json": (
        "83d73d7702f8bf8c59d7368b2306fd85bc57560f6b834f85caa6c9003baf9f50"
    ),
}


class FMRuntimeBundleError(RuntimeError):
    """The pinned runtime bundle is missing, malformed, or not canonical."""


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


AuthorshipStatus: TypeAlias = Literal[
    "directly_authored", "model_synthesis", "externally_attributed"
]
Representation: TypeAlias = Literal[
    "direct_quote", "faithful_paraphrase", "derived_conclusion"
]
ClaimMode: TypeAlias = Literal[
    "metaphysical_claim",
    "conceptual_definition",
    "metaphor",
    "practical_principle",
    "speculative_extension",
    "empirical_claim",
    "safety_boundary",
    "application_boundary",
]
EpistemicStatus: TypeAlias = Literal[
    "asserted",
    "corroborated_within_corpus",
    "provisional",
    "disputed",
    "contradictory",
    "deprecated",
    "excluded",
    "unresolved",
    "bounded",
]
FrameworkStatus: TypeAlias = Literal[
    "central_commitment",
    "supporting_commitment",
    "practical_commitment",
    "speculative_extension",
    "application_constraint",
    "historical_only",
    "disputed",
    "deprecated",
    "excluded",
]
ExternalStatus: TypeAlias = Literal[
    "philosophical_proposition",
    "externally_unverified",
    "empirical_hypothesis",
    "empirically_supported",
    "empirically_disputed",
    "domain_dependent",
    "not_applicable",
]
SupportRole: TypeAlias = Literal[
    "supports",
    "defines",
    "illustrates",
    "constrains",
    "contradicts",
    "disputes",
    "deprecates",
    "supersedes",
]


class RuntimeProvenanceV02(StrictFrozenModel):
    source_kind: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=500)
    support_role: SupportRole
    heading: str | None = Field(default=None, min_length=1, max_length=500)
    lines: str | None = Field(default=None, min_length=1, max_length=40)
    record_id: str | None = Field(default=None, min_length=1, max_length=160)
    line: int | None = Field(default=None, ge=1)
    field: str | None = Field(default=None, min_length=1, max_length=80)
    citation: str | None = Field(default=None, min_length=1, max_length=2000)


class RuntimeSemanticAssertionV02(StrictFrozenModel):
    assertion_id: str = Field(pattern=r"^FM-(?:CA|RA|HFA)-\d{3}$")
    statement: str = Field(min_length=1, max_length=4000)
    authorship_status: AuthorshipStatus
    representation: Representation
    claim_mode: ClaimMode
    epistemic_status: EpistemicStatus
    framework_status: FrameworkStatus
    external_status: ExternalStatus
    confidence: float = Field(ge=0.0, le=1.0)
    author_review_required: Literal[False]
    competing_interpretation: str = Field(min_length=1, max_length=4000)
    provenance_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("provenance_refs")
    @classmethod
    def _valid_provenance_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values:
            raise ValueError("provenance refs must be unique and sorted")
        if any(not PROVENANCE_REF_RE.fullmatch(value) for value in values):
            raise ValueError("invalid provenance ref")
        return values


class RuntimeRecordBaseV02(StrictFrozenModel):
    id: str = Field(min_length=8, max_length=100)
    selection_text: str = Field(min_length=1, max_length=20000)
    render_text: str = Field(min_length=1, max_length=20000)


class RuntimeConceptV02(RuntimeRecordBaseV02):
    kind: Literal["concept"]
    inference_use: Literal["premise_with_named_rule"]
    label: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=1, max_length=4000)
    kernel_role: str = Field(min_length=1, max_length=200)
    assertion: RuntimeSemanticAssertionV02


class RuntimeRelationshipV02(RuntimeRecordBaseV02):
    kind: Literal["relationship"]
    inference_use: Literal["premise_with_named_rule"]
    subject_id: str = Field(min_length=8, max_length=100)
    predicate: str = Field(min_length=1, max_length=240)
    object_id: str = Field(min_length=8, max_length=100)
    assertion: RuntimeSemanticAssertionV02


class RuntimeHistoricalFormulationV02(RuntimeRecordBaseV02):
    kind: Literal["historical_formulation"]
    inference_use: Literal["status_only"]
    formulation: str = Field(min_length=1, max_length=4000)
    status: Literal["deprecated", "disputed", "excluded", "historical_only"]
    reason: str = Field(min_length=1, max_length=4000)
    superseded_by: tuple[str, ...] = Field(min_length=1)
    assertion: RuntimeSemanticAssertionV02


class RuntimeInferenceRuleV02(RuntimeRecordBaseV02):
    kind: Literal["inference_rule"]
    inference_use: Literal["named_rule"]
    name: str = Field(min_length=1, max_length=300)
    source_premises: tuple[str, ...] = Field(min_length=1)
    inference_rule: str = Field(min_length=1, max_length=4000)
    derived_conclusion: str = Field(min_length=1, max_length=4000)
    epistemic_status: EpistemicStatus
    confidence: float = Field(ge=0.0, le=1.0)
    possible_competing_interpretation: str = Field(min_length=1, max_length=4000)
    author_review_required: Literal[False]
    provenance_refs: tuple[str, ...] = Field(min_length=1)


class RuntimeInferencePremiseV02(StrictFrozenModel):
    record_id: str = Field(min_length=8, max_length=100)
    premise: str = Field(min_length=1, max_length=2000)
    premise_use: Literal["current_premise", "noncurrent_status_only"]


class RuntimeInferenceApplicationV02(RuntimeRecordBaseV02):
    kind: Literal["inference_application"]
    inference_use: Literal["reviewed_application"]
    source_premises: tuple[RuntimeInferencePremiseV02, ...] = Field(min_length=1)
    inference_rule_id: str = Field(pattern=r"^FM-IR-\d{3}-[a-z0-9-]+$")
    derived_conclusion: str = Field(min_length=1, max_length=4000)
    epistemic_status: EpistemicStatus
    confidence: float = Field(ge=0.0, le=1.0)
    possible_competing_interpretation: str = Field(min_length=1, max_length=4000)
    author_review_required: Literal[False]
    provenance_refs: tuple[str, ...] = Field(min_length=1)


class RuntimeTensionV02(RuntimeRecordBaseV02):
    kind: Literal["tension"]
    inference_use: Literal["constraint_only"]
    title: str = Field(min_length=1, max_length=500)
    claim_a: str = Field(min_length=1, max_length=4000)
    claim_b: str = Field(min_length=1, max_length=4000)
    status: str = Field(min_length=1, max_length=500)
    boundary: str = Field(min_length=1, max_length=4000)
    provenance_citation: str = Field(min_length=1, max_length=2000)
    provenance_refs: tuple[str, ...] = Field(min_length=1)


RuntimeRecordV02: TypeAlias = Annotated[
    Union[
        RuntimeConceptV02,
        RuntimeRelationshipV02,
        RuntimeHistoricalFormulationV02,
        RuntimeInferenceRuleV02,
        RuntimeInferenceApplicationV02,
        RuntimeTensionV02,
    ],
    Field(discriminator="kind"),
]


class RuntimeApplicationGateV02(StrictFrozenModel):
    id: Literal["FM-AG-001"]
    trigger_context: str = Field(min_length=1, max_length=4000)
    fm_influence: Literal["off_or_deferred"]
    fallback: str = Field(min_length=1, max_length=4000)
    later_reentry: str = Field(min_length=1, max_length=4000)
    layer_boundary: str = Field(min_length=1, max_length=4000)
    provenance_refs: tuple[str, ...] = Field(min_length=1)


class RuntimeBundleCountsV02(StrictFrozenModel):
    primary_core_records: Literal[200]
    concepts: Literal[46]
    relationships: Literal[48]
    noncurrent_formulations: Literal[22]
    inference_rules: Literal[27]
    inference_applications: Literal[27]
    tensions: Literal[28]
    reasoning_evaluations: Literal[48]
    runtime_records: Literal[198]


def canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class FMRuntimeBundleV02(StrictFrozenModel):
    bundle_contract_version: Literal["fm_runtime_bundle_v0_2"]
    runtime_reference: Literal["fm_v0_2"]
    semantic_version: Literal["0.2"]
    canonical_manifest_id: Literal["fm-canonical-v0.2-manifest"]
    canonical_manifest_sha256: Literal[
        "1d2912854b368f2a802752ad0c2d1a37a09f700c925e7beb842724e47acf627d"
    ]
    canonical_status: Literal["author_approved_canonical"]
    canonical_created: Literal["2026-07-20"]
    validation_status: Literal["PASS_CANONICAL_AUTHOR_APPROVED"]
    deterministic_checks_passed: Literal[5859]
    source_artifact_sha256: dict[str, str]
    counts: RuntimeBundleCountsV02
    application_gate: RuntimeApplicationGateV02
    execution_order: tuple[str, ...] = Field(min_length=27, max_length=27)
    provenance: dict[str, RuntimeProvenanceV02]
    records: tuple[RuntimeRecordV02, ...] = Field(min_length=198, max_length=198)
    bundle_sha256: str

    @field_validator("bundle_sha256")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("bundle_sha256 is not a SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _validate_canonical_bundle(self) -> "FMRuntimeBundleV02":
        if self.source_artifact_sha256 != EXPECTED_SOURCE_ARTIFACT_SHA256:
            raise ValueError("source artifact pin set differs from canonical v0.2")
        if self.bundle_sha256 != COMPILED_BUNDLE_SHA256:
            raise ValueError("compiled bundle digest is not the reviewed v0.2 digest")
        payload = self.model_dump(
            mode="json", exclude={"bundle_sha256"}, exclude_none=True
        )
        if canonical_sha256(payload) != self.bundle_sha256:
            raise ValueError("bundle payload digest mismatch")

        records_by_id = {record.id: record for record in self.records}
        if len(records_by_id) != len(self.records):
            raise ValueError("runtime record IDs are not unique")
        expected_kind_counts = {
            "concept": 46,
            "relationship": 48,
            "historical_formulation": 22,
            "inference_rule": 27,
            "inference_application": 27,
            "tension": 28,
        }
        actual_kind_counts = {
            kind: sum(record.kind == kind for record in self.records)
            for kind in expected_kind_counts
        }
        if actual_kind_counts != expected_kind_counts:
            raise ValueError("runtime record kind counts changed")

        concept_ids = {
            record.id for record in self.records if isinstance(record, RuntimeConceptV02)
        }
        relationship_ids = {
            record.id
            for record in self.records
            if isinstance(record, RuntimeRelationshipV02)
        }
        historical_ids = {
            record.id
            for record in self.records
            if isinstance(record, RuntimeHistoricalFormulationV02)
        }
        rule_ids = {
            record.id
            for record in self.records
            if isinstance(record, RuntimeInferenceRuleV02)
        }
        if set(self.execution_order) != rule_ids or len(set(self.execution_order)) != 27:
            raise ValueError("inference execution order is not exact")

        for record in self.records:
            provenance_refs = record_provenance_refs(record)
            if any(reference not in self.provenance for reference in provenance_refs):
                raise ValueError(f"{record.id} has unresolved provenance")
            if isinstance(record, RuntimeRelationshipV02):
                if record.subject_id not in concept_ids | historical_ids:
                    raise ValueError(f"{record.id} has unresolved subject")
                if record.object_id not in concept_ids | historical_ids:
                    raise ValueError(f"{record.id} has unresolved object")
            elif isinstance(record, RuntimeHistoricalFormulationV02):
                if not set(record.superseded_by) <= concept_ids | relationship_ids:
                    raise ValueError(f"{record.id} has unresolved replacement")
            elif isinstance(record, RuntimeInferenceApplicationV02):
                if record.inference_rule_id not in rule_ids:
                    raise ValueError(f"{record.id} has unresolved inference rule")
                for premise in record.source_premises:
                    if premise.record_id not in concept_ids | historical_ids:
                        raise ValueError(f"{record.id} has unresolved premise")
                    if premise.record_id in historical_ids and premise.premise_use != "noncurrent_status_only":
                        raise ValueError(f"{record.id} treats history as a current premise")
                    if premise.record_id in concept_ids and premise.premise_use != "current_premise":
                        raise ValueError(f"{record.id} suppresses a current premise")

        for reference, provenance in self.provenance.items():
            if not PROVENANCE_REF_RE.fullmatch(reference):
                raise ValueError("invalid provenance registry key")
            if reference != "sha256:" + canonical_sha256(
                provenance.model_dump(mode="json", exclude_none=True)
            ):
                raise ValueError("provenance registry digest mismatch")
        return self

    def record_index(self) -> dict[str, RuntimeRecordV02]:
        return {record.id: record for record in self.records}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self)


def record_provenance_refs(record: RuntimeRecordV02) -> tuple[str, ...]:
    if isinstance(
        record,
        (RuntimeConceptV02, RuntimeRelationshipV02, RuntimeHistoricalFormulationV02),
    ):
        return record.assertion.provenance_refs
    return record.provenance_refs


def record_epistemic_status(record: RuntimeRecordV02) -> str | None:
    if isinstance(
        record,
        (RuntimeConceptV02, RuntimeRelationshipV02, RuntimeHistoricalFormulationV02),
    ):
        return record.assertion.epistemic_status
    if isinstance(record, (RuntimeInferenceRuleV02, RuntimeInferenceApplicationV02)):
        return record.epistemic_status
    return None


def record_competing_interpretations(record: RuntimeRecordV02) -> tuple[str, ...]:
    if isinstance(
        record,
        (RuntimeConceptV02, RuntimeRelationshipV02, RuntimeHistoricalFormulationV02),
    ):
        return (record.assertion.competing_interpretation,)
    if isinstance(record, (RuntimeInferenceRuleV02, RuntimeInferenceApplicationV02)):
        return (record.possible_competing_interpretation,)
    return (record.claim_a, record.claim_b)


def record_claim_mode(record: RuntimeRecordV02) -> str | None:
    if isinstance(
        record,
        (RuntimeConceptV02, RuntimeRelationshipV02, RuntimeHistoricalFormulationV02),
    ):
        return record.assertion.claim_mode
    return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FMRuntimeBundleError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def parse_runtime_bundle_v0_2(value: str | bytes) -> FMRuntimeBundleV02:
    try:
        document = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                FMRuntimeBundleError(f"invalid JSON constant: {constant}")
            ),
        )
        # Validate through JSON semantics so strict tuple fields accept JSON
        # arrays while scalar coercion remains disabled.
        return FMRuntimeBundleV02.model_validate_json(canonical_json_bytes(document))
    except FMRuntimeBundleError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
        raise FMRuntimeBundleError(f"invalid canonical FM runtime bundle: {exc}") from exc


@lru_cache(maxsize=4)
def _load_runtime_bundle_cached(path: str) -> FMRuntimeBundleV02:
    bundle_path = Path(path)
    try:
        value = bundle_path.read_bytes()
    except OSError as exc:
        raise FMRuntimeBundleError(f"cannot read FM runtime bundle: {bundle_path}") from exc
    return parse_runtime_bundle_v0_2(value)


def load_runtime_bundle_v0_2(path: Path | None = None) -> FMRuntimeBundleV02:
    selected_path = (path or DEFAULT_BUNDLE_PATH).resolve()
    return _load_runtime_bundle_cached(str(selected_path))


def clear_runtime_bundle_cache_v0_2() -> None:
    _load_runtime_bundle_cached.cache_clear()


__all__ = [
    "BUNDLE_CONTRACT_VERSION",
    "CANONICAL_MANIFEST_ID",
    "CANONICAL_MANIFEST_SHA256",
    "COMPILED_BUNDLE_SHA256",
    "DEFAULT_BUNDLE_PATH",
    "FMRuntimeBundleError",
    "FMRuntimeBundleV02",
    "RuntimeConceptV02",
    "RuntimeHistoricalFormulationV02",
    "RuntimeInferenceApplicationV02",
    "RuntimeInferenceRuleV02",
    "RuntimeRecordV02",
    "RuntimeRelationshipV02",
    "RuntimeTensionV02",
    "canonical_json_bytes",
    "canonical_sha256",
    "clear_runtime_bundle_cache_v0_2",
    "load_runtime_bundle_v0_2",
    "parse_runtime_bundle_v0_2",
    "record_claim_mode",
    "record_competing_interpretations",
    "record_epistemic_status",
    "record_provenance_refs",
]
