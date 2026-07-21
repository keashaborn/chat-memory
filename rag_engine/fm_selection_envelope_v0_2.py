from __future__ import annotations

"""Deterministic, fail-closed Fractal Monism v0.2 lexical selection.

This module accepts a trusted response-policy result. It cannot elevate FM from
OFF, override the canonical application gate, access retrieval systems, or
retain raw query text in its output.
"""

import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rag_engine.fm_runtime_bundle_v0_2 import (
    CANONICAL_MANIFEST_SHA256,
    COMPILED_BUNDLE_SHA256,
    FMRuntimeBundleV02,
    RuntimeConceptV02,
    RuntimeHistoricalFormulationV02,
    RuntimeInferenceApplicationV02,
    RuntimeInferenceRuleV02,
    RuntimeRecordV02,
    RuntimeRelationshipV02,
    RuntimeTensionV02,
    canonical_sha256,
    load_runtime_bundle_v0_2,
    record_claim_mode,
    record_competing_interpretations,
    record_epistemic_status,
    record_provenance_refs,
)
from rag_engine.response_policy_v0_2 import (
    FMLevel as PolicyFMLevel,
    GateState,
    ResponseMode as PolicyResponseMode,
    ResponsePolicyDecisionV0_2,
)


REQUEST_CONTRACT_VERSION = "fm_selection_request_v0_2"
ENVELOPE_CONTRACT_VERSION = "fm_selection_envelope_v0_2"
SELECTOR_VERSION = "fm_deterministic_lexical_selector_v0_2"
TOKEN_ESTIMATOR_VERSION = "fm_utf8_bytes_div4_v0_2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SELECTION_ID_RE = re.compile(r"^fm-sel-[0-9a-f]{24}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")

ResponseModeValue: TypeAlias = Literal[
    "HIGH_STAKES", "TECHNICAL", "FM_EXPLICIT", "COACHING", "ORDINARY"
]
FMLevelValue: TypeAlias = Literal["OFF", "LIGHT", "EXPLICIT"]
SelectionStatus: TypeAlias = Literal["OFF", "EMPTY", "SELECTED"]
SelectionReason: TypeAlias = Literal[
    "application_gate_triggered",
    "application_gate_uncertain",
    "user_opt_out",
    "high_stakes_off",
    "technical_off",
    "fm_level_off",
    "no_match",
    "token_budget_exhausted",
    "selected",
]

MODE_MAX_RECORDS: dict[str, int] = {
    "HIGH_STAKES": 0,
    "TECHNICAL": 0,
    "FM_EXPLICIT": 8,
    "COACHING": 3,
    "ORDINARY": 1,
}
MODE_MAX_TOKENS: dict[str, int] = {
    "HIGH_STAKES": 0,
    "TECHNICAL": 0,
    "FM_EXPLICIT": 1600,
    "COACHING": 600,
    "ORDINARY": 220,
}

# These are response-policy routing allowlists, not additions to FM semantics.
# Every selected statement still comes verbatim from the pinned canonical bundle.
COACHING_PRIMARY_IDS = frozenset(
    {
        "FM-C-022",  # optional perspective flexibility with return to action
        "FM-IR-018-practical-agency-under-constraint",
        "FM-IR-019-pattern-not-identity",
        "FM-IR-020-consented-functional-experiment",
        "FM-IR-021-behavior-consequence-practical-unit",
    }
)
ORDINARY_LIGHT_PRIMARY_IDS = frozenset(
    {
        "FM-C-022",
        "FM-IR-013-actuality-not-endorsement",
        "FM-IR-018-practical-agency-under-constraint",
        "FM-IR-019-pattern-not-identity",
    }
)
FM_EXPLICIT_OVERVIEW_IDS = (
    "FM-C-001",
    "FM-C-002",
    "FM-C-003",
    "FM-C-005",
    "FM-C-007",
    "FM-C-008",
    "FM-C-009",
    "FM-C-034",
)

SELECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "FM-C-001": ("relational whole", "one whole"),
    "FM-C-002": ("undifferentiated field", "latent potential", "before distinction"),
    "FM-C-003": ("relational distinction", "difference", "contrast"),
    "FM-C-005": ("recursive rule", "fractal pattern", "self similarity"),
    "FM-C-007": ("one perceiver", "one consciousness", "shared identity"),
    "FM-C-008": ("localized vantage", "individual person", "local perspective"),
    "FM-C-009": ("consciousness emergence", "active consciousness", "consciousness threshold"),
    "FM-C-013": ("nonfundamental time", "emergent time", "time is not fundamental"),
    "FM-C-022": ("perspective flexibility", "another perspective", "reframe", "vantage mobility"),
    "FM-C-034": ("local reality", "local suffering", "kindness boundary", "grief is real"),
    "FM-C-038": ("personal survival", "personal immortality", "survive death", "afterlife"),
    "FM-C-040": ("external framework", "scientific meaning", "translation boundary"),
    "FM-C-043": ("necessary suffering", "required suffering", "abuse for growth"),
    "FM-HF-011": ("quantum evidence", "quantum proof", "quantum confirms"),
    "FM-HF-016": ("personal immortality", "survival guarantee"),
    "FM-HF-017": ("karmic punishment", "karmic guarantee", "cosmic punishment"),
    "FM-HF-018": ("nonlocal cognition", "shared memory", "mind reading", "premonition"),
    "FM-HF-022": ("required suffering", "harm required for growth"),
    "FM-IR-005-empirical-evidence-restraint": (
        "empirical evidence",
        "scientific proof",
        "scientifically prove",
        "quantum behavior",
        "quantum proof",
    ),
    "FM-IR-009-local-access-boundary": (
        "mind reading",
        "shared memory",
        "nonlocal cognition",
        "privacy",
    ),
    "FM-IR-010-consciousness-mechanism-restraint": (
        "consciousness threshold",
        "distinction count",
        "consciousness mechanism",
    ),
    "FM-IR-011-nonfundamental-time-restraint": (
        "all events simultaneous",
        "future already exists",
        "many worlds",
    ),
    "FM-IR-013-actuality-not-endorsement": (
        "regret",
        "mistake",
        "needed to happen",
        "make it useful",
        "destined",
    ),
    "FM-IR-014-local-reality-preservation": (
        "grief",
        "local loss",
        "suffering",
        "death is unreal",
    ),
    "FM-IR-016-intent-effect-accountability": (
        "good intent",
        "actual harm",
        "accountability",
        "repair",
    ),
    "FM-IR-017-no-karmic-guarantee": (
        "karmic punishment",
        "cosmic punishment",
        "guaranteed guilt",
    ),
    "FM-IR-018-practical-agency-under-constraint": (
        "agency",
        "choice under constraint",
        "what can i control",
        "helpless",
        "respond differently",
    ),
    "FM-IR-019-pattern-not-identity": (
        "i am lazy",
        "i am broken",
        "always fail",
        "pattern not identity",
        "habit",
        "keep missing",
    ),
    "FM-IR-020-consented-functional-experiment": (
        "test a change",
        "behavioral experiment",
        "track a habit",
        "baseline",
        "stop rule",
        "reversible experiment",
    ),
    "FM-IR-021-behavior-consequence-practical-unit": (
        "consequence awareness",
        "behavior and consequence",
        "impact",
        "feedback",
        "outcome",
    ),
    "FM-IR-023-cross-domain-bridge-requirement": (
        "universal mechanism",
        "cross domain",
        "same mechanism",
        "fractal proves",
    ),
    "FM-IR-026-death-and-personal-persistence-agnosticism": (
        "personal survival",
        "personal immortality",
        "survive death",
        "afterlife",
    ),
    "FM-T-003": ("one perceiver multiple people", "shared identity local people"),
    "FM-T-008": ("atemporal identity personal continuation", "personal survival"),
    "FM-T-009": ("unity local death", "unity suffering", "local grief"),
    "FM-T-012": ("contrast necessary suffering", "required suffering"),
    "FM-T-018": ("cross domain recursion", "universal mechanism"),
    "FM-T-019": ("quantum behavior", "quantum evidence", "quantum compatibility"),
    "FM-T-020": ("one perceiver nonlocal cognition", "shared mind"),
}

STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "give",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "say",
        "tell",
        "that",
        "the",
        "this",
        "to",
        "use",
        "view",
        "what",
        "when",
        "with",
        "would",
        "you",
    }
)
GENERIC_FM_TOKENS = frozenset(
    {"explain", "fm", "fractal", "lens", "monism", "overview", "philosophy"}
)
HISTORICAL_QUERY_PHRASES = (
    "deprecated",
    "disputed",
    "earlier fm",
    "earlier version",
    "excluded",
    "former claim",
    "historical",
    "history of",
    "old fm",
    "older version",
    "original claim",
    "previous claim",
    "superseded",
    "used to claim",
)
KIND_RANK = {
    "concept": 0,
    "inference_rule": 1,
    "inference_application": 2,
    "tension": 3,
    "relationship": 4,
    "historical_formulation": 5,
}


class FMSelectionContractError(RuntimeError):
    """Fail-closed error at the typed FM selection boundary."""


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


class FMSelectionRequestV02(StrictFrozenModel):
    contract_version: Literal["fm_selection_request_v0_2"] = REQUEST_CONTRACT_VERSION
    policy_decision: ResponsePolicyDecisionV0_2
    query_text: str = Field(max_length=20000, repr=False)
    token_budget: int | None = Field(default=None, ge=0, le=1600)

    @model_validator(mode="after")
    def _query_binds_to_policy_message(self) -> "FMSelectionRequestV02":
        query_sha256 = hashlib.sha256(self.query_text.encode("utf-8")).hexdigest()
        if query_sha256 != self.policy_decision.current_message_sha256:
            raise ValueError("FM query does not bind to the policy current message")
        return self


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


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FMSelectionContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


class FMSelectionEnvelopeV02(StrictFrozenModel):
    contract_version: Literal["fm_selection_envelope_v0_2"]
    selector_version: Literal["fm_deterministic_lexical_selector_v0_2"]
    token_estimator_version: Literal["fm_utf8_bytes_div4_v0_2"]
    semantic_version: Literal["0.2"]
    canonical_manifest_sha256: Literal[
        "1d2912854b368f2a802752ad0c2d1a37a09f700c925e7beb842724e47acf627d"
    ]
    bundle_sha256: str
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
    reason_codes: tuple[SelectionReason, ...] = Field(min_length=1)
    selected_record_ids: tuple[str, ...]
    concept_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    inference_rule_ids: tuple[str, ...]
    inference_application_ids: tuple[str, ...]
    tension_ids: tuple[str, ...]
    historical_formulation_ids: tuple[str, ...]
    epistemic_status_by_id: dict[str, str]
    tension_status_by_id: dict[str, str]
    competing_interpretations_by_id: dict[str, tuple[str, ...]]
    application_boundary_ids: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    max_records: int = Field(ge=0, le=8)
    token_budget: int = Field(ge=0, le=1600)
    used_tokens: int = Field(ge=0, le=1600)

    @field_validator(
        "bundle_sha256",
        "request_sha256",
        "current_message_sha256",
        "conversation_sha256",
        "safety_assessment_sha256",
        "policy_decision_sha256",
        "selection_sha256",
        "query_sha256",
    )
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256 digest")
        return value

    @field_validator("request_id")
    @classmethod
    def _valid_request_id(cls, value: str) -> str:
        if not REQUEST_ID_RE.fullmatch(value):
            raise ValueError("invalid request id")
        return value

    @field_validator("selection_id")
    @classmethod
    def _valid_selection_id(cls, value: str) -> str:
        if not SELECTION_ID_RE.fullmatch(value):
            raise ValueError("invalid selection id")
        return value

    @model_validator(mode="after")
    def _validate_selection(self) -> "FMSelectionEnvelopeV02":
        if self.bundle_sha256 != COMPILED_BUNDLE_SHA256:
            raise ValueError("selection envelope is not pinned to the compiled FM bundle")
        if self.query_sha256 != self.current_message_sha256:
            raise ValueError("selection query does not match the policy current message")
        if self.used_tokens > self.token_budget:
            raise ValueError("used token estimate exceeds budget")
        if len(set(self.selected_record_ids)) != len(self.selected_record_ids):
            raise ValueError("selected record IDs are not unique")
        if len(self.selected_record_ids) > self.max_records:
            raise ValueError("selected record count exceeds budget")
        if self.max_records != MODE_MAX_RECORDS[self.response_mode]:
            raise ValueError("mode record budget mismatch")
        expected_token_max = MODE_MAX_TOKENS[self.response_mode]
        if self.token_budget > expected_token_max:
            raise ValueError("mode token budget exceeds hard maximum")
        if self.response_mode in {"HIGH_STAKES", "TECHNICAL"} and self.status != "OFF":
            raise ValueError("high-stakes and technical envelopes must be OFF")
        if self.response_mode == "HIGH_STAKES" and self.reason_codes != (
            "high_stakes_off",
        ):
            raise ValueError("high-stakes envelope has an invalid reason")
        if self.response_mode == "TECHNICAL" and self.reason_codes != (
            "technical_off",
        ):
            raise ValueError("technical envelope has an invalid reason")
        if self.status == "OFF" and self.response_mode in {
            "FM_EXPLICIT",
            "COACHING",
        } and self.reason_codes not in {
            ("application_gate_triggered",),
            ("application_gate_uncertain",),
            ("user_opt_out",),
        }:
            raise ValueError("active-mode OFF envelope has an invalid veto reason")
        if self.status == "OFF" and self.response_mode == "ORDINARY" and (
            self.reason_codes
            not in {
                ("application_gate_triggered",),
                ("application_gate_uncertain",),
                ("user_opt_out",),
                ("fm_level_off",),
            }
        ):
            raise ValueError("ordinary OFF envelope has an invalid reason")
        if self.status != "OFF":
            expected_level = {
                "FM_EXPLICIT": "EXPLICIT",
                "COACHING": "LIGHT",
                "ORDINARY": "LIGHT",
            }.get(self.response_mode)
            if expected_level is None or self.fm_level != expected_level:
                raise ValueError("active selection level does not match response mode")
        if self.response_mode == "COACHING" and len(self.selected_record_ids) > 3:
            raise ValueError("coaching selection exceeds three records")
        if self.response_mode == "COACHING" and not set(
            self.selected_record_ids
        ) <= COACHING_PRIMARY_IDS:
            raise ValueError("coaching selection contains a non-allowlisted record")
        if self.response_mode == "ORDINARY" and len(self.selected_record_ids) > 1:
            raise ValueError("ordinary selection exceeds one record")
        if self.response_mode == "ORDINARY" and not set(
            self.selected_record_ids
        ) <= ORDINARY_LIGHT_PRIMARY_IDS:
            raise ValueError("ordinary selection contains a non-allowlisted record")
        if self.historical_formulation_ids and self.response_mode != "FM_EXPLICIT":
            raise ValueError("historical formulations require explicit FM mode")

        typed_ids = (
            self.concept_ids
            + self.relationship_ids
            + self.inference_rule_ids
            + self.inference_application_ids
            + self.tension_ids
            + self.historical_formulation_ids
        )
        if set(typed_ids) != set(self.selected_record_ids) or len(typed_ids) != len(
            self.selected_record_ids
        ):
            raise ValueError("typed ID arrays do not partition selected records")
        if set(self.competing_interpretations_by_id) != set(self.selected_record_ids):
            raise ValueError("competing interpretations do not cover selected records")
        if not set(self.epistemic_status_by_id) <= set(self.selected_record_ids):
            raise ValueError("epistemic map contains an unselected record")
        if set(self.tension_status_by_id) != set(self.tension_ids):
            raise ValueError("tension status map does not match tension IDs")
        if tuple(sorted(set(self.provenance_refs))) != self.provenance_refs:
            raise ValueError("provenance refs must be unique and sorted")

        empty_fields = (
            self.selected_record_ids,
            self.concept_ids,
            self.relationship_ids,
            self.inference_rule_ids,
            self.inference_application_ids,
            self.tension_ids,
            self.historical_formulation_ids,
            tuple(self.epistemic_status_by_id),
            tuple(self.tension_status_by_id),
            tuple(self.competing_interpretations_by_id),
            self.application_boundary_ids,
            self.provenance_refs,
        )
        if self.status in {"OFF", "EMPTY"}:
            if any(empty_fields) or self.used_tokens != 0:
                raise ValueError("OFF/EMPTY envelope must contain no FM records")
            if self.status == "OFF" and self.fm_level != "OFF":
                raise ValueError("OFF envelope must have FM level OFF")
            if self.status == "EMPTY" and self.fm_level == "OFF":
                raise ValueError("EMPTY envelope must retain an active FM level")
            if self.status == "OFF" and self.reason_codes == ("selected",):
                raise ValueError("OFF envelope cannot have selected reason")
            if self.status == "EMPTY" and self.reason_codes not in {
                ("no_match",),
                ("token_budget_exhausted",),
            }:
                raise ValueError("EMPTY envelope has an invalid reason")
        else:
            if not self.selected_record_ids or self.used_tokens == 0:
                raise ValueError("SELECTED envelope must contain FM records")
            if self.fm_level == "OFF" or self.reason_codes != ("selected",):
                raise ValueError("SELECTED envelope has inconsistent level/reason")
            if "FM-AG-001" not in self.application_boundary_ids:
                raise ValueError("active FM selection must record FM-AG-001")
            if self.application_boundary_ids[0] != "FM-AG-001":
                raise ValueError("FM-AG-001 must be the first application boundary")
            if len(set(self.application_boundary_ids)) != len(
                self.application_boundary_ids
            ):
                raise ValueError("application boundary IDs are not unique")
            if not set(self.application_boundary_ids[1:]) <= set(
                self.selected_record_ids
            ):
                raise ValueError("application boundary contains an unselected record")

        payload = self.model_dump(
            mode="json", exclude={"selection_id", "selection_sha256"}
        )
        expected_hash = canonical_sha256(payload)
        if self.selection_sha256 != expected_hash:
            raise ValueError("selection payload digest mismatch")
        if self.selection_id != f"fm-sel-{expected_hash[:24]}":
            raise ValueError("selection id does not bind the selection digest")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)

    def compact_content(
        self, bundle: FMRuntimeBundleV02 | None = None
    ) -> str:
        if self.status != "SELECTED":
            return ""
        bundle = bundle or load_runtime_bundle_v0_2()
        try:
            bundle = FMRuntimeBundleV02.model_validate(bundle.model_dump(mode="python"))
        except (AttributeError, ValidationError) as exc:
            raise FMSelectionContractError(f"invalid FM runtime bundle: {exc}") from exc
        if bundle.bundle_sha256 != self.bundle_sha256:
            raise FMSelectionContractError("selection and runtime bundle digests differ")
        index = bundle.record_index()
        try:
            selected = tuple(index[record_id] for record_id in self.selected_record_ids)
        except KeyError as exc:
            raise FMSelectionContractError("selection references an unknown FM record") from exc
        content = render_compact_content_v0_2(self.fm_level, selected)
        expected_typed_ids = (
            tuple(record.id for record in selected if isinstance(record, RuntimeConceptV02)),
            tuple(
                record.id
                for record in selected
                if isinstance(record, RuntimeRelationshipV02)
            ),
            tuple(
                record.id
                for record in selected
                if isinstance(record, RuntimeInferenceRuleV02)
            ),
            tuple(
                record.id
                for record in selected
                if isinstance(record, RuntimeInferenceApplicationV02)
            ),
            tuple(record.id for record in selected if isinstance(record, RuntimeTensionV02)),
            tuple(
                record.id
                for record in selected
                if isinstance(record, RuntimeHistoricalFormulationV02)
            ),
        )
        if expected_typed_ids != (
            self.concept_ids,
            self.relationship_ids,
            self.inference_rule_ids,
            self.inference_application_ids,
            self.tension_ids,
            self.historical_formulation_ids,
        ):
            raise FMSelectionContractError("selection type projections differ from bundle")
        expected_epistemic = {
            record.id: value
            for record in selected
            if (value := record_epistemic_status(record)) is not None
        }
        expected_tensions = {
            record.id: record.status
            for record in selected
            if isinstance(record, RuntimeTensionV02)
        }
        expected_competing = {
            record.id: record_competing_interpretations(record) for record in selected
        }
        expected_provenance = tuple(
            sorted(
                {
                    reference
                    for record in selected
                    for reference in record_provenance_refs(record)
                }
            )
        )
        expected_boundaries = tuple(
            dict.fromkeys(
                (
                    "FM-AG-001",
                    *(
                        record.id
                        for record in selected
                        if record_claim_mode(record)
                        in {"application_boundary", "safety_boundary"}
                    ),
                )
            )
        )
        if (
            self.epistemic_status_by_id != expected_epistemic
            or self.tension_status_by_id != expected_tensions
            or self.competing_interpretations_by_id != expected_competing
            or self.provenance_refs != expected_provenance
            or self.application_boundary_ids != expected_boundaries
        ):
            raise FMSelectionContractError("selection metadata differs from bundle")
        if _estimate_text_tokens(content) != self.used_tokens:
            raise FMSelectionContractError("selection token use differs from compact content")
        return content

    @classmethod
    def from_wire_json(cls, value: str | bytes) -> "FMSelectionEnvelopeV02":
        try:
            document = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
            return cls.model_validate_json(_canonical_json_bytes(document))
        except FMSelectionContractError:
            raise
        except (TypeError, ValueError, ValidationError) as exc:
            raise FMSelectionContractError(f"invalid FM selection envelope: {exc}") from exc


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold().replace("_", " ")
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in _normalize(value).split()
        if token not in STOPWORDS and len(token) > 1
    )


def _phrase_present(query: str, phrase: str) -> bool:
    return f" {_normalize(phrase)} " in f" {query} "


def _record_title(record: RuntimeRecordV02) -> str:
    if isinstance(record, RuntimeConceptV02):
        return record.label
    if isinstance(record, RuntimeRelationshipV02):
        return record.predicate
    if isinstance(record, RuntimeHistoricalFormulationV02):
        return record.formulation
    if isinstance(record, RuntimeInferenceRuleV02):
        return record.name
    if isinstance(record, RuntimeInferenceApplicationV02):
        return record.derived_conclusion
    return record.title


def _score_record(record: RuntimeRecordV02, raw_query: str, query: str) -> int:
    score = 0
    direct_id = record.id.casefold() in raw_query.casefold()
    if direct_id:
        score += 100_000
    aliases = SELECTION_ALIASES.get(record.id, ())
    alias_match = False
    for alias in aliases:
        if _phrase_present(query, alias):
            alias_match = True
            score += 5_000 + 50 * len(_tokens(alias))
    title = _normalize(_record_title(record))
    exact_title = bool(title and _phrase_present(query, title))
    if exact_title:
        score += 2_000

    query_tokens = _tokens(query) - GENERIC_FM_TOKENS
    title_overlap = query_tokens & _tokens(title)
    body_overlap = query_tokens & _tokens(record.selection_text)
    if not (direct_id or alias_match or exact_title) and max(
        len(title_overlap), len(body_overlap)
    ) < 2:
        return 0
    score += 80 * len(title_overlap)
    if len(body_overlap) >= 2:
        score += 8 * len(body_overlap)
    return score


def estimate_record_tokens_v0_2(record: RuntimeRecordV02) -> int:
    return max(1, math.ceil(len(record.render_text.encode("utf-8")) / 4))


def _estimate_text_tokens(value: str) -> int:
    return math.ceil(len(value.encode("utf-8")) / 4)


def render_compact_content_v0_2(
    fm_level: FMLevelValue, records: tuple[RuntimeRecordV02, ...]
) -> str:
    if not records:
        return ""
    if fm_level == "EXPLICIT":
        header = (
            "Fractal Monism v0.2 selected canonical reference. Internal framework "
            "status is separate from external evidence. Do not extend beyond named "
            "rules or erase local reality."
        )
    elif fm_level == "LIGHT":
        header = (
            "Optional low-stakes practical framing. Keep it ordinary, directly "
            "relevant, noncoercive, and tied to concrete local action."
        )
    else:
        raise FMSelectionContractError("OFF selections cannot render FM content")
    return "\n".join((header, *(record.render_text for record in records)))


def _historical_query(raw_query: str, normalized_query: str) -> bool:
    if re.search(r"\bfm-hf-\d{3}\b", raw_query.casefold()):
        return True
    return any(_phrase_present(normalized_query, phrase) for phrase in HISTORICAL_QUERY_PHRASES)


def _generic_fm_overview_query(query: str) -> bool:
    query_tokens = _tokens(query)
    return bool(query_tokens & {"fm", "fractal", "monism"}) and not (
        query_tokens - GENERIC_FM_TOKENS
    )


def _active_policy_reason(request: FMSelectionRequestV02) -> SelectionReason | None:
    decision = request.policy_decision
    if decision.response_mode is PolicyResponseMode.HIGH_STAKES:
        return "high_stakes_off"
    if decision.response_mode is PolicyResponseMode.TECHNICAL:
        return "technical_off"
    if decision.fm_application_gate is GateState.TRIGGERED:
        return "application_gate_triggered"
    if decision.fm_application_gate is GateState.UNCERTAIN:
        return "application_gate_uncertain"
    if decision.user_opt_out_applied:
        return "user_opt_out"
    if decision.fm_effective_level is PolicyFMLevel.OFF:
        return "fm_level_off"
    return None


def _candidate_records(
    request: FMSelectionRequestV02, bundle: FMRuntimeBundleV02
) -> list[RuntimeRecordV02]:
    mode = request.policy_decision.response_mode
    if mode is PolicyResponseMode.COACHING:
        return [record for record in bundle.records if record.id in COACHING_PRIMARY_IDS]
    if mode is PolicyResponseMode.ORDINARY:
        return [
            record for record in bundle.records if record.id in ORDINARY_LIGHT_PRIMARY_IDS
        ]
    include_history = _historical_query(request.query_text, _normalize(request.query_text))
    return [
        record
        for record in bundle.records
        if include_history or not isinstance(record, RuntimeHistoricalFormulationV02)
    ]


def _ranked_records(
    request: FMSelectionRequestV02, bundle: FMRuntimeBundleV02
) -> list[RuntimeRecordV02]:
    query = _normalize(request.query_text)
    index = bundle.record_index()
    if (
        request.policy_decision.response_mode is PolicyResponseMode.FM_EXPLICIT
        and _generic_fm_overview_query(query)
    ):
        return [index[record_id] for record_id in FM_EXPLICIT_OVERVIEW_IDS]

    execution_rank = {
        record_id: index for index, record_id in enumerate(bundle.execution_order)
    }
    scored: list[tuple[int, int, int, str, RuntimeRecordV02]] = []
    for record in _candidate_records(request, bundle):
        score = _score_record(record, request.query_text, query)
        if score <= 0:
            continue
        scored.append(
            (
                -score,
                KIND_RANK[record.kind],
                execution_rank.get(record.id, 10_000),
                record.id,
                record,
            )
        )
    scored.sort(key=lambda item: item[:4])
    return [item[4] for item in scored]


def _selected_records(
    ranked: list[RuntimeRecordV02],
    fm_level: FMLevelValue,
    max_records: int,
    token_budget: int,
) -> tuple[list[RuntimeRecordV02], int]:
    selected: list[RuntimeRecordV02] = []
    for record in ranked:
        if len(selected) >= max_records:
            break
        proposed = tuple((*selected, record))
        proposed_tokens = _estimate_text_tokens(
            render_compact_content_v0_2(fm_level, proposed)
        )
        if proposed_tokens > token_budget:
            continue
        selected.append(record)
    used_tokens = (
        _estimate_text_tokens(render_compact_content_v0_2(fm_level, tuple(selected)))
        if selected
        else 0
    )
    return selected, used_tokens


def _empty_or_off_envelope(
    *,
    request: FMSelectionRequestV02,
    bundle: FMRuntimeBundleV02,
    status: Literal["OFF", "EMPTY"],
    reason: SelectionReason,
    fm_level: FMLevelValue,
    token_budget: int,
) -> FMSelectionEnvelopeV02:
    return _build_envelope(
        request=request,
        bundle=bundle,
        status=status,
        reason=reason,
        fm_level=fm_level,
        selected=(),
        token_budget=token_budget,
        used_tokens=0,
    )


def _build_envelope(
    *,
    request: FMSelectionRequestV02,
    bundle: FMRuntimeBundleV02,
    status: SelectionStatus,
    reason: SelectionReason,
    fm_level: FMLevelValue,
    selected: tuple[RuntimeRecordV02, ...],
    token_budget: int,
    used_tokens: int,
) -> FMSelectionEnvelopeV02:
    decision = request.policy_decision
    response_mode = decision.response_mode.value
    concept_ids = tuple(
        record.id for record in selected if isinstance(record, RuntimeConceptV02)
    )
    relationship_ids = tuple(
        record.id for record in selected if isinstance(record, RuntimeRelationshipV02)
    )
    rule_ids = tuple(
        record.id for record in selected if isinstance(record, RuntimeInferenceRuleV02)
    )
    application_ids = tuple(
        record.id
        for record in selected
        if isinstance(record, RuntimeInferenceApplicationV02)
    )
    tension_ids = tuple(
        record.id for record in selected if isinstance(record, RuntimeTensionV02)
    )
    historical_ids = tuple(
        record.id
        for record in selected
        if isinstance(record, RuntimeHistoricalFormulationV02)
    )
    epistemic_status = {
        record.id: value
        for record in selected
        if (value := record_epistemic_status(record)) is not None
    }
    tension_status = {
        record.id: record.status
        for record in selected
        if isinstance(record, RuntimeTensionV02)
    }
    competing = {
        record.id: record_competing_interpretations(record) for record in selected
    }
    provenance_refs = tuple(
        sorted(
            {
                reference
                for record in selected
                for reference in record_provenance_refs(record)
            }
        )
    )
    boundary_ids: tuple[str, ...] = ()
    if selected:
        boundary_ids = tuple(
            dict.fromkeys(
                (
                    "FM-AG-001",
                    *(
                        record.id
                        for record in selected
                        if record_claim_mode(record)
                        in {"application_boundary", "safety_boundary"}
                    ),
                )
            )
        )

    payload: dict[str, Any] = {
        "contract_version": ENVELOPE_CONTRACT_VERSION,
        "selector_version": SELECTOR_VERSION,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
        "semantic_version": bundle.semantic_version,
        "canonical_manifest_sha256": bundle.canonical_manifest_sha256,
        "bundle_sha256": bundle.bundle_sha256,
        "policy_version": decision.policy_version,
        "request_id": decision.request_id,
        "request_sha256": decision.request_sha256,
        "current_message_sha256": decision.current_message_sha256,
        "conversation_sha256": decision.conversation_sha256,
        "safety_assessment_sha256": decision.safety_assessment_sha256,
        "policy_decision_sha256": decision.decision_sha256,
        "query_sha256": _sha256_text(request.query_text),
        "response_mode": response_mode,
        "fm_level": fm_level,
        "status": status,
        "reason_codes": (reason,),
        "selected_record_ids": tuple(record.id for record in selected),
        "concept_ids": concept_ids,
        "relationship_ids": relationship_ids,
        "inference_rule_ids": rule_ids,
        "inference_application_ids": application_ids,
        "tension_ids": tension_ids,
        "historical_formulation_ids": historical_ids,
        "epistemic_status_by_id": epistemic_status,
        "tension_status_by_id": tension_status,
        "competing_interpretations_by_id": competing,
        "application_boundary_ids": boundary_ids,
        "provenance_refs": provenance_refs,
        "max_records": MODE_MAX_RECORDS[response_mode],
        "token_budget": token_budget,
        "used_tokens": used_tokens,
    }
    digest = canonical_sha256(payload)
    payload["selection_id"] = f"fm-sel-{digest[:24]}"
    payload["selection_sha256"] = digest
    try:
        return FMSelectionEnvelopeV02.model_validate(payload)
    except ValidationError as exc:  # pragma: no cover - internal invariant
        raise FMSelectionContractError(f"invalid generated FM selection: {exc}") from exc


def select_fm_v0_2(
    request: FMSelectionRequestV02,
    bundle: FMRuntimeBundleV02 | None = None,
) -> FMSelectionEnvelopeV02:
    try:
        if not isinstance(request, FMSelectionRequestV02):
            raise TypeError("expected FMSelectionRequestV02")
        request = FMSelectionRequestV02.model_validate_json(
            _canonical_json_bytes(request)
        )
    except (AttributeError, TypeError, ValueError, ValidationError) as exc:
        raise FMSelectionContractError(f"invalid FM selection request: {exc}") from exc
    if bundle is None:
        bundle = load_runtime_bundle_v0_2()
    else:
        try:
            bundle = FMRuntimeBundleV02.model_validate(bundle.model_dump(mode="python"))
        except (AttributeError, ValidationError) as exc:
            raise FMSelectionContractError(f"invalid FM runtime bundle: {exc}") from exc
    if bundle.canonical_manifest_sha256 != CANONICAL_MANIFEST_SHA256:
        raise FMSelectionContractError("FM manifest pin mismatch")
    if bundle.bundle_sha256 != COMPILED_BUNDLE_SHA256:
        raise FMSelectionContractError("FM compiled bundle pin mismatch")

    off_reason = _active_policy_reason(request)
    if off_reason is not None:
        return _empty_or_off_envelope(
            request=request,
            bundle=bundle,
            status="OFF",
            reason=off_reason,
            fm_level="OFF",
            token_budget=0,
        )

    response_mode = request.policy_decision.response_mode.value
    fm_level = request.policy_decision.fm_effective_level.value
    max_tokens = MODE_MAX_TOKENS[response_mode]
    requested_tokens = max_tokens if request.token_budget is None else request.token_budget
    token_budget = min(requested_tokens, max_tokens)
    ranked = _ranked_records(request, bundle)
    if not ranked:
        return _empty_or_off_envelope(
            request=request,
            bundle=bundle,
            status="EMPTY",
            reason="no_match",
            fm_level=fm_level,
            token_budget=token_budget,
        )
    selected, used_tokens = _selected_records(
        ranked,
        fm_level,
        MODE_MAX_RECORDS[response_mode],
        token_budget,
    )
    if not selected:
        return _empty_or_off_envelope(
            request=request,
            bundle=bundle,
            status="EMPTY",
            reason="token_budget_exhausted",
            fm_level=fm_level,
            token_budget=token_budget,
        )
    return _build_envelope(
        request=request,
        bundle=bundle,
        status="SELECTED",
        reason="selected",
        fm_level=fm_level,
        selected=tuple(selected),
        token_budget=token_budget,
        used_tokens=used_tokens,
    )


__all__ = [
    "COACHING_PRIMARY_IDS",
    "ENVELOPE_CONTRACT_VERSION",
    "FMSelectionContractError",
    "FMSelectionEnvelopeV02",
    "FMSelectionRequestV02",
    "FM_EXPLICIT_OVERVIEW_IDS",
    "MODE_MAX_RECORDS",
    "MODE_MAX_TOKENS",
    "ORDINARY_LIGHT_PRIMARY_IDS",
    "REQUEST_CONTRACT_VERSION",
    "SELECTOR_VERSION",
    "estimate_record_tokens_v0_2",
    "render_compact_content_v0_2",
    "select_fm_v0_2",
]
