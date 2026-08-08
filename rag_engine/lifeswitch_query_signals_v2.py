from __future__ import annotations

"""Deterministic, runtime-neutral LifeSwitch query signals for self-owned reads."""

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.lifeswitch_temporal_semantics_v1 import (
    LifeSwitchTemporalContextV1,
    LifeSwitchTemporalResolutionV1,
    LifeSwitchTemporalWindowV1,
    parse_lifeswitch_temporal_windows_v1,
)


QUERY_SIGNALS_CONTRACT = "lifeswitch_query_signals_v2"
QUERY_SIGNALS_POLICY_VERSION = "lifeswitch_query_signals_policy_v2_0"
CONTINUATION_HINT_CONTRACT = "lifeswitch_continuation_hint_v1"

LifeSwitchDomainV2 = Literal[
    "plan", "nutrition", "training", "conditioning", "measurements"
]
OutputGrainV2 = Literal[
    "current", "day", "daily_rows", "summary", "ranking", "trend", "comparison"
]
PlanRelationshipV2 = Literal[
    "targets", "adherence", "historical_comparison", "none"
]
ContinuationKindV2 = Literal[
    "none", "prior_domain", "prior_window", "prior_subject"
]

_DOMAIN_ORDER: tuple[LifeSwitchDomainV2, ...] = (
    "plan",
    "nutrition",
    "training",
    "conditioning",
    "measurements",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _sha256(value: Any) -> str:
    raw = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LifeSwitchContinuationHintV1(_StrictFrozenModel):
    contract_version: Literal[CONTINUATION_HINT_CONTRACT] = CONTINUATION_HINT_CONTRACT
    prior_domains: tuple[LifeSwitchDomainV2, ...]
    prior_projection_family: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    prior_window: LifeSwitchTemporalWindowV1 | None = None
    prior_output_grain: OutputGrainV2
    owner_binding_sha256: str
    thread_binding_sha256: str
    pre_request_snapshot_sha256: str
    selection_manifest_sha256: str
    strict_pre_request_cutoff: Literal[True] = True

    @field_validator(
        "owner_binding_sha256",
        "thread_binding_sha256",
        "pre_request_snapshot_sha256",
        "selection_manifest_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def ordered_domains(self) -> "LifeSwitchContinuationHintV1":
        expected = tuple(domain for domain in _DOMAIN_ORDER if domain in self.prior_domains)
        if expected != self.prior_domains:
            raise ValueError("prior domains must be unique and ordered")
        return self


class LifeSwitchQuerySignalsV2(_StrictFrozenModel):
    contract_version: Literal[QUERY_SIGNALS_CONTRACT] = QUERY_SIGNALS_CONTRACT
    policy_version: Literal[QUERY_SIGNALS_POLICY_VERSION] = QUERY_SIGNALS_POLICY_VERSION
    status: Literal["ACTIVE", "OFF", "UNAVAILABLE"]
    subject_scope: Literal["SELF"] = "SELF"
    requested_domains: tuple[LifeSwitchDomainV2, ...]
    output_grain: OutputGrainV2
    temporal_request: LifeSwitchTemporalResolutionV1
    named_subject: str | None = Field(default=None, min_length=1, max_length=80)
    plan_relationship: PlanRelationshipV2
    continuation_kind: ContinuationKindV2
    reason_codes: tuple[str, ...]
    query_sha256: str
    signal_manifest_sha256: str

    @field_validator("query_sha256", "signal_manifest_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def coherent_and_hashed(self) -> "LifeSwitchQuerySignalsV2":
        expected = tuple(domain for domain in _DOMAIN_ORDER if domain in self.requested_domains)
        if expected != self.requested_domains:
            raise ValueError("requested domains must be unique and ordered")
        if self.reason_codes != tuple(dict.fromkeys(self.reason_codes)):
            raise ValueError("reason codes must be unique and ordered")
        if self.status == "ACTIVE" and not self.requested_domains:
            raise ValueError("active signals require a domain")
        if self.status == "OFF" and self.requested_domains:
            raise ValueError("OFF signals cannot request domains")
        if self.named_subject is not None and "training" not in self.requested_domains:
            raise ValueError("named subject requires training domain")
        if self.status == "ACTIVE" and self.temporal_request.status == "UNAVAILABLE":
            raise ValueError("active signals cannot carry unavailable temporal semantics")
        payload = self.model_dump(mode="json", exclude={"signal_manifest_sha256"})
        if self.signal_manifest_sha256 != _sha256(payload):
            raise ValueError("query signal hash mismatch")
        return self


_SELF = re.compile(r"\b(?:i|me|my|mine)\b", re.IGNORECASE)
_PLAN = re.compile(r"\b(?:plan|target|targets|goal|goals|supposed to)\b", re.IGNORECASE)
_NUTRITION = re.compile(
    r"\b(?:eat|ate|eating|food|meal|nutrition|macro|macros|calorie|calories|kcal|protein|carb|carbs|carbohydrate|carbohydrates|fat|intake)\b",
    re.IGNORECASE,
)
_TRAINING = re.compile(
    r"\b(?:train|trained|training|workout|workouts|lift|lifts|lifting|weightlifting|strength|exercise|exercises|movement|movements|sets|reps|volume)\b",
    re.IGNORECASE,
)
_CONDITIONING = re.compile(r"\b(?:conditioning|cardio|aerobic)\b", re.IGNORECASE)
_MEASUREMENTS = re.compile(
    r"\b(?:weigh|weighed|weight|waist|body[ -]?fat|measurement|measurements|physique)\b",
    re.IGNORECASE,
)
_CONTINUATION = re.compile(
    r"^\s*(?:what about\b|and\b|compare that\b|how about\b)", re.IGNORECASE
)
_COMPARISON = re.compile(r"\b(?:compare|comparison|versus|vs\.?|difference|week before)\b", re.IGNORECASE)
_DAILY = re.compile(r"\b(?:for each day|each day|day[- ]by[- ]day|daily|which days|list (?:the )?dates)\b", re.IGNORECASE)
_RANKING = re.compile(r"\b(?:most|rank|ranking|top)\b", re.IGNORECASE)
_TREND = re.compile(r"\b(?:progress|progressed|progressing|improve|improved|improving|trend|change|changed|stronger)\b", re.IGNORECASE)
_SUMMARY = re.compile(r"\b(?:how am i doing|average|summary|overall|adherence|consistent|meeting|hitting|following|lately|recently)\b", re.IGNORECASE)
_DAY = re.compile(r"\b(?:today|yesterday|this|last)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE)
_TARGETS = re.compile(r"\b(?:what are my|current)\s+(?:macro|macros|targets|goals)|\b(?:target|targets|supposed to)\b", re.IGNORECASE)
_ADHERENCE = re.compile(r"\b(?:adherence|hitting|meeting|following|compared? with my plan|versus my plan|vs\.? my plan|how am i doing)\b", re.IGNORECASE)
_HISTORICAL_PLAN = re.compile(r"\b(?:old|older|previous|historical|former)\s+plan\b|\bplan\s+(?:then|at that time)\b", re.IGNORECASE)
_NAMED_SUBJECT = (
    re.compile(r"\b(?:progress|progression)\s+(?:on|for|with)\s+(?:my\s+)?([a-z][a-z0-9 ()&+'’\-/]{1,70}?)(?:[?.!,]|$)", re.IGNORECASE),
    re.compile(r"\bhow (?:is|are) my\s+([a-z][a-z0-9 ()&+'’\-/]{1,70}?)\s+(?:progressing|improving)\b", re.IGNORECASE),
)


def _ordered_domains(candidates: set[str]) -> tuple[LifeSwitchDomainV2, ...]:
    return tuple(domain for domain in _DOMAIN_ORDER if domain in candidates)


def _named_subject(value: str) -> str | None:
    for pattern in _NAMED_SUBJECT:
        match = pattern.search(value)
        if match:
            cleaned = re.sub(r"\s+", " ", match.group(1)).strip(" .?,'\"")
            cleaned = re.sub(
                r"\s+(?:over|during|in)\s+(?:the\s+)?(?:last|past|this)\s+"
                r"(?:\d+\s+days?|\d+\s+weeks?|month|week)$",
                "",
                cleaned,
                flags=re.IGNORECASE,
            ).strip()
            return cleaned[:80] or None
    return None


def _make_signals(
    *,
    query: str,
    status: Literal["ACTIVE", "OFF", "UNAVAILABLE"],
    domains: tuple[LifeSwitchDomainV2, ...],
    grain: OutputGrainV2,
    temporal: LifeSwitchTemporalResolutionV1,
    subject: str | None,
    plan_relationship: PlanRelationshipV2,
    continuation_kind: ContinuationKindV2,
    reasons: tuple[str, ...],
) -> LifeSwitchQuerySignalsV2:
    payload = {
        "contract_version": QUERY_SIGNALS_CONTRACT,
        "policy_version": QUERY_SIGNALS_POLICY_VERSION,
        "status": status,
        "subject_scope": "SELF",
        "requested_domains": domains,
        "output_grain": grain,
        "temporal_request": temporal,
        "named_subject": subject,
        "plan_relationship": plan_relationship,
        "continuation_kind": continuation_kind,
        "reason_codes": tuple(dict.fromkeys(reasons)),
        "query_sha256": _text_sha256(query),
    }
    return LifeSwitchQuerySignalsV2(
        **payload,
        signal_manifest_sha256=_sha256(payload),
    )


def create_lifeswitch_query_signals_v2(
    query: str,
    *,
    temporal_context: LifeSwitchTemporalContextV1,
    projection_default_days: int | None = None,
    continuation_hint: LifeSwitchContinuationHintV1 | None = None,
    expected_owner_binding_sha256: str | None = None,
    expected_thread_binding_sha256: str | None = None,
    expected_pre_request_snapshot_sha256: str | None = None,
) -> LifeSwitchQuerySignalsV2:
    """Parse self-owned semantic signals without selecting identity or storage."""

    value = re.sub(r"\s+", " ", str(query or "")).strip()
    base_temporal = parse_lifeswitch_temporal_windows_v1(
        value,
        context=temporal_context,
        projection_default_days=projection_default_days,
        prior_window=continuation_hint.prior_window if continuation_hint else None,
    )
    if not value:
        return _make_signals(query=value, status="OFF", domains=(), grain="current", temporal=base_temporal, subject=None, plan_relationship="none", continuation_kind="none", reasons=("LIFESWITCH_NOT_REQUESTED",))

    continuation_marker = bool(_CONTINUATION.search(value))
    if continuation_marker:
        expected = (
            expected_owner_binding_sha256,
            expected_thread_binding_sha256,
            expected_pre_request_snapshot_sha256,
        )
        if continuation_hint is None or any(item is None for item in expected):
            return _make_signals(query=value, status="UNAVAILABLE", domains=(), grain="current", temporal=base_temporal if base_temporal.status != "UNAVAILABLE" else parse_lifeswitch_temporal_windows_v1("", context=temporal_context), subject=None, plan_relationship="none", continuation_kind="none", reasons=("CONTINUATION_CONTEXT_UNAVAILABLE",))
        if (
            continuation_hint.owner_binding_sha256 != expected_owner_binding_sha256
            or continuation_hint.thread_binding_sha256 != expected_thread_binding_sha256
            or continuation_hint.pre_request_snapshot_sha256 != expected_pre_request_snapshot_sha256
        ):
            return _make_signals(query=value, status="UNAVAILABLE", domains=(), grain="current", temporal=parse_lifeswitch_temporal_windows_v1("", context=temporal_context), subject=None, plan_relationship="none", continuation_kind="none", reasons=("CONTINUATION_CONTEXT_UNAVAILABLE",))

    domains: set[str] = set()
    if _PLAN.search(value):
        domains.add("plan")
    if _NUTRITION.search(value):
        domains.add("nutrition")
    if _TRAINING.search(value):
        domains.add("training")
    if _CONDITIONING.search(value):
        domains.add("conditioning")
    if _MEASUREMENTS.search(value):
        domains.add("measurements")

    subject = _named_subject(value)
    if subject:
        domains.add("training")

    implicit_personal_training = bool(
        re.match(r"^\s*(?:which|what)\s+(?:lifts?|exercises?|movements?)\b", value, re.IGNORECASE)
        and (_TREND.search(value) or _RANKING.search(value))
    )
    personal = bool(_SELF.search(value) or implicit_personal_training)

    continuation_kind: ContinuationKindV2 = "none"
    if continuation_marker and continuation_hint is not None:
        personal = True
        explicit_domains = bool(domains)
        if not domains:
            domains.update(continuation_hint.prior_domains)
            continuation_kind = "prior_domain"
        elif base_temporal.status == "NONE" and continuation_hint.prior_window is not None:
            continuation_kind = "prior_window"
        else:
            continuation_kind = "prior_domain" if not explicit_domains else "prior_window"
        if base_temporal.status == "NONE" and continuation_hint.prior_window is not None:
            prior = continuation_hint.prior_window
            base_temporal = parse_lifeswitch_temporal_windows_v1(
                f"{prior.start_date.isoformat()} through {prior.end_date.isoformat()}",
                context=temporal_context,
            )
        if subject is None and "training" in continuation_hint.prior_domains:
            match = re.match(r"^\s*(?:what|how)\s+about\s+(.+?)\s*[?.!]*$", value, re.IGNORECASE)
            if match and not any(pattern.search(match.group(1)) for pattern in (_NUTRITION, _TRAINING, _CONDITIONING, _MEASUREMENTS, _PLAN)) and parse_lifeswitch_temporal_windows_v1(match.group(1), context=temporal_context).status == "NONE":
                subject = re.sub(r"\s+", " ", match.group(1)).strip(" .?,'\"")[:80] or None
                if subject:
                    domains.add("training")
                    continuation_kind = "prior_subject"

    if base_temporal.status == "UNAVAILABLE":
        return _make_signals(query=value, status="UNAVAILABLE", domains=_ordered_domains(domains) if personal else (), grain="comparison" if _COMPARISON.search(value) else "summary", temporal=base_temporal, subject=subject if personal else None, plan_relationship="none", continuation_kind=continuation_kind, reasons=base_temporal.reason_codes)

    if _COMPARISON.search(value):
        grain: OutputGrainV2 = "comparison"
    elif _DAILY.search(value):
        grain = "daily_rows"
    elif _RANKING.search(value):
        grain = "ranking"
    elif _TREND.search(value):
        grain = "trend"
    elif _DAY.search(value) or (base_temporal.status == "RESOLVED" and len(base_temporal.windows) == 1 and base_temporal.windows[0].start_date == base_temporal.windows[0].end_date):
        grain = "day"
    elif _SUMMARY.search(value) or (
        base_temporal.status == "RESOLVED"
        and len(base_temporal.windows) == 1
        and base_temporal.windows[0].start_date != base_temporal.windows[0].end_date
    ):
        grain = "summary"
    else:
        grain = "current"

    if _HISTORICAL_PLAN.search(value):
        relationship: PlanRelationshipV2 = "historical_comparison"
    elif _ADHERENCE.search(value):
        relationship = "adherence"
        domains.add("plan")
    elif _TARGETS.search(value):
        relationship = "targets"
        domains.add("plan")
    else:
        relationship = "none"

    if not personal or not domains:
        return _make_signals(query=value, status="OFF", domains=(), grain=grain, temporal=base_temporal, subject=None, plan_relationship="none", continuation_kind="none", reasons=("LIFESWITCH_NOT_REQUESTED",))

    reasons = ["self_owned_lifeswitch_request"]
    if continuation_kind != "none":
        reasons.append(f"validated_{continuation_kind}")
    if base_temporal.status == "RESOLVED":
        reasons.append("normalized_temporal_request")
    return _make_signals(query=value, status="ACTIVE", domains=_ordered_domains(domains), grain=grain, temporal=base_temporal, subject=subject, plan_relationship=relationship, continuation_kind=continuation_kind, reasons=tuple(reasons))


__all__ = [
    "CONTINUATION_HINT_CONTRACT",
    "QUERY_SIGNALS_CONTRACT",
    "QUERY_SIGNALS_POLICY_VERSION",
    "LifeSwitchContinuationHintV1",
    "LifeSwitchQuerySignalsV2",
    "create_lifeswitch_query_signals_v2",
]
