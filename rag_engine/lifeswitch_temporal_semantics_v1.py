from __future__ import annotations

"""Pure, deterministic account-local temporal semantics for LifeSwitch V2."""

import calendar
import datetime as dt
import hashlib
import json
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TEMPORAL_CONTEXT_CONTRACT = "lifeswitch_temporal_context_v1"
TEMPORAL_WINDOW_CONTRACT = "lifeswitch_temporal_window_v1"
TEMPORAL_RESOLUTION_CONTRACT = "lifeswitch_temporal_resolution_v1"
TEMPORAL_PARSER_VERSION = "lifeswitch_temporal_parser_v1_0"

TemporalSemanticKind = Literal[
    "EXPLICIT_DAY",
    "EXPLICIT_RANGE",
    "TRAILING_DAYS",
    "CURRENT_CALENDAR_WEEK",
    "PREVIOUS_CALENDAR_WEEK",
    "CURRENT_CALENDAR_MONTH",
    "PREVIOUS_CALENDAR_MONTH",
    "PROJECTION_DEFAULT",
    "INHERITED_PRIOR_WINDOW",
]
TemporalPhraseKind = Literal[
    "TODAY",
    "YESTERDAY",
    "LAST_N_DAYS",
    "THIS_WEEK",
    "LAST_WEEK",
    "THIS_MONTH",
    "LAST_MONTH",
    "OVER_LAST_MONTH",
    "IN_MONTH",
    "THIS_WEEKDAY",
    "LAST_WEEKDAY",
    "EXPLICIT_DAY",
    "EXPLICIT_RANGE",
    "RECENTLY",
    "WEEK_BEFORE_THAT",
    "IMPLIED_PREVIOUS_PERIOD",
]


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
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
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


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LifeSwitchTemporalContextV1(_StrictFrozenModel):
    contract_version: Literal[TEMPORAL_CONTEXT_CONTRACT] = TEMPORAL_CONTEXT_CONTRACT
    timezone_name: str = Field(min_length=1, max_length=80)
    timezone_source: Literal["account_setting", "active_plan_fallback"]
    as_of_utc: dt.datetime
    as_of_local_date: dt.date
    context_sha256: str

    @field_validator("timezone_name")
    @classmethod
    def recognized_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone is not recognized") from error
        return value

    @field_validator("context_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def coherent_and_hashed(self) -> "LifeSwitchTemporalContextV1":
        if self.as_of_utc.tzinfo is None or self.as_of_utc.utcoffset() is None:
            raise ValueError("as_of_utc must be timezone-aware")
        normalized = self.as_of_utc.astimezone(dt.timezone.utc)
        expected_date = normalized.astimezone(ZoneInfo(self.timezone_name)).date()
        if expected_date != self.as_of_local_date:
            raise ValueError("as_of_local_date does not match account timezone")
        payload = self.model_dump(mode="json", exclude={"context_sha256"})
        if self.context_sha256 != _sha256(payload):
            raise ValueError("temporal context hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        timezone_name: str,
        timezone_source: Literal["account_setting", "active_plan_fallback"],
        as_of_utc: dt.datetime,
    ) -> "LifeSwitchTemporalContextV1":
        if as_of_utc.tzinfo is None or as_of_utc.utcoffset() is None:
            raise ValueError("as_of_utc must be timezone-aware")
        normalized = as_of_utc.astimezone(dt.timezone.utc)
        try:
            local_date = normalized.astimezone(ZoneInfo(timezone_name)).date()
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone is not recognized") from error
        payload = {
            "contract_version": TEMPORAL_CONTEXT_CONTRACT,
            "timezone_name": timezone_name,
            "timezone_source": timezone_source,
            "as_of_utc": normalized,
            "as_of_local_date": local_date,
        }
        return cls(**payload, context_sha256=_sha256(payload))


class LifeSwitchTemporalWindowV1(_StrictFrozenModel):
    contract_version: Literal[TEMPORAL_WINDOW_CONTRACT] = TEMPORAL_WINDOW_CONTRACT
    semantic_kind: TemporalSemanticKind
    timezone_name: str = Field(min_length=1, max_length=80)
    timezone_source: Literal["account_setting", "active_plan_fallback"]
    as_of_utc: dt.datetime
    as_of_local_date: dt.date
    start_date: dt.date
    end_date: dt.date
    is_complete_period: bool
    source_phrase_kind: TemporalPhraseKind
    parser_version: Literal[TEMPORAL_PARSER_VERSION] = TEMPORAL_PARSER_VERSION
    window_manifest_sha256: str

    @field_validator("timezone_name")
    @classmethod
    def recognized_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone is not recognized") from error
        return value

    @field_validator("window_manifest_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def coherent_and_hashed(self) -> "LifeSwitchTemporalWindowV1":
        if self.as_of_utc.tzinfo is None or self.as_of_utc.utcoffset() is None:
            raise ValueError("as_of_utc must be timezone-aware")
        if self.end_date < self.start_date:
            raise ValueError("temporal window is reversed")
        if (self.end_date - self.start_date).days > 1094:
            raise ValueError("temporal window exceeds 1095 dates")
        local_date = self.as_of_utc.astimezone(ZoneInfo(self.timezone_name)).date()
        if local_date != self.as_of_local_date:
            raise ValueError("window authority date mismatch")
        payload = self.model_dump(mode="json", exclude={"window_manifest_sha256"})
        if self.window_manifest_sha256 != _sha256(payload):
            raise ValueError("temporal window hash mismatch")
        return self


class LifeSwitchTemporalResolutionV1(_StrictFrozenModel):
    contract_version: Literal[TEMPORAL_RESOLUTION_CONTRACT] = (
        TEMPORAL_RESOLUTION_CONTRACT
    )
    status: Literal["NONE", "RESOLVED", "UNAVAILABLE"]
    windows: tuple[LifeSwitchTemporalWindowV1, ...] = Field(max_length=2)
    reason_codes: tuple[str, ...]
    parser_version: Literal[TEMPORAL_PARSER_VERSION] = TEMPORAL_PARSER_VERSION
    resolution_sha256: str

    @field_validator("resolution_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def coherent_and_hashed(self) -> "LifeSwitchTemporalResolutionV1":
        if self.reason_codes != tuple(dict.fromkeys(self.reason_codes)):
            raise ValueError("reason codes must be unique and ordered")
        if self.status == "RESOLVED" and not self.windows:
            raise ValueError("resolved temporal request requires a window")
        if self.status != "RESOLVED" and self.windows:
            raise ValueError("non-resolved temporal request cannot carry windows")
        if len(self.windows) == 2:
            first, second = self.windows
            if not (first.end_date < second.start_date or second.end_date < first.start_date):
                raise ValueError("comparison windows must not overlap")
        payload = self.model_dump(mode="json", exclude={"resolution_sha256"})
        if self.resolution_sha256 != _sha256(payload):
            raise ValueError("temporal resolution hash mismatch")
        return self


def _resolution(
    status: Literal["NONE", "RESOLVED", "UNAVAILABLE"],
    windows: tuple[LifeSwitchTemporalWindowV1, ...],
    reasons: tuple[str, ...],
) -> LifeSwitchTemporalResolutionV1:
    payload = {
        "contract_version": TEMPORAL_RESOLUTION_CONTRACT,
        "status": status,
        "windows": windows,
        "reason_codes": tuple(dict.fromkeys(reasons)),
        "parser_version": TEMPORAL_PARSER_VERSION,
    }
    return LifeSwitchTemporalResolutionV1(
        **payload,
        resolution_sha256=_sha256(payload),
    )


def _window(
    context: LifeSwitchTemporalContextV1,
    *,
    semantic_kind: TemporalSemanticKind,
    start: dt.date,
    end: dt.date,
    complete: bool,
    phrase: TemporalPhraseKind,
) -> LifeSwitchTemporalWindowV1:
    payload = {
        "contract_version": TEMPORAL_WINDOW_CONTRACT,
        "semantic_kind": semantic_kind,
        "timezone_name": context.timezone_name,
        "timezone_source": context.timezone_source,
        "as_of_utc": context.as_of_utc,
        "as_of_local_date": context.as_of_local_date,
        "start_date": start,
        "end_date": end,
        "is_complete_period": complete,
        "source_phrase_kind": phrase,
        "parser_version": TEMPORAL_PARSER_VERSION,
    }
    return LifeSwitchTemporalWindowV1(
        **payload,
        window_manifest_sha256=_sha256(payload),
    )


_MONTHS = {
    name.lower(): number
    for number in range(1, 13)
    for name in (calendar.month_name[number], calendar.month_abbr[number])
}
_MONTH_PATTERN = "|".join(sorted((re.escape(x) for x in _MONTHS), key=len, reverse=True))
_EXPLICIT_RANGE = re.compile(
    rf"\b(?:from\s+)?(?P<sm>{_MONTH_PATTERN})\.?\s+(?P<sd>\d{{1,2}})"
    rf"(?:st|nd|rd|th)?(?:,?\s+(?P<sy>\d{{4}}))?\s*"
    rf"(?:through|thru|to|until|[-–—])\s*"
    rf"(?:(?P<em>{_MONTH_PATTERN})\.?\s+)?(?P<ed>\d{{1,2}})"
    rf"(?:st|nd|rd|th)?(?:,?\s+(?P<ey>\d{{4}}))?\b",
    re.IGNORECASE,
)
_ISO_RANGE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\s*(?:through|to|[-–—])\s*(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
_EXPLICIT_DAY = re.compile(
    rf"\b(?P<m>{_MONTH_PATTERN})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y>\d{{4}}))?\b",
    re.IGNORECASE,
)
_ISO_DAY = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_IN_MONTH = re.compile(rf"\bin\s+(?P<m>{_MONTH_PATTERN})(?:\s+(?P<y>\d{{4}}))?\b", re.IGNORECASE)
_LAST_N = re.compile(r"\b(?:last|past)\s+(\d{1,3})\s+days?\b", re.IGNORECASE)
_WEEKDAY = {name.lower(): index for index, name in enumerate(calendar.day_name)}


def _most_recent_year(month: int, day: int, as_of: dt.date) -> int:
    return as_of.year - 1 if (month, day) > (as_of.month, as_of.day) else as_of.year


def _explicit(value: str, context: LifeSwitchTemporalContextV1) -> LifeSwitchTemporalResolutionV1 | None:
    match = _ISO_RANGE.search(value)
    if match:
        try:
            start, end = (dt.date.fromisoformat(match.group(i)) for i in (1, 2))
        except ValueError:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_UNSUPPORTED",))
        if end < start:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_CONFLICT",))
        return _resolution("RESOLVED", (_window(context, semantic_kind="EXPLICIT_RANGE", start=start, end=end, complete=end < context.as_of_local_date, phrase="EXPLICIT_RANGE"),), ("explicit_range",))

    match = _EXPLICIT_RANGE.search(value)
    if match:
        sm = _MONTHS[match.group("sm").lower()]
        em = _MONTHS[(match.group("em") or match.group("sm")).lower()]
        sy = int(match.group("sy")) if match.group("sy") else _most_recent_year(sm, int(match.group("sd")), context.as_of_local_date)
        ey = int(match.group("ey")) if match.group("ey") else sy
        if not match.group("em") and em < sm:
            ey += 1
        try:
            start = dt.date(sy, sm, int(match.group("sd")))
            end = dt.date(ey, em, int(match.group("ed")))
        except ValueError:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_UNSUPPORTED",))
        if end < start:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_CONFLICT",))
        return _resolution("RESOLVED", (_window(context, semantic_kind="EXPLICIT_RANGE", start=start, end=end, complete=end < context.as_of_local_date, phrase="EXPLICIT_RANGE"),), ("explicit_range",))

    match = _ISO_DAY.search(value)
    if match:
        try:
            day = dt.date.fromisoformat(match.group(1))
        except ValueError:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_UNSUPPORTED",))
        return _resolution("RESOLVED", (_window(context, semantic_kind="EXPLICIT_DAY", start=day, end=day, complete=day < context.as_of_local_date, phrase="EXPLICIT_DAY"),), ("explicit_day",))

    match = _EXPLICIT_DAY.search(value)
    if match:
        month = _MONTHS[match.group("m").lower()]
        day_number = int(match.group("d"))
        year = int(match.group("y")) if match.group("y") else _most_recent_year(month, day_number, context.as_of_local_date)
        try:
            day = dt.date(year, month, day_number)
        except ValueError:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_UNSUPPORTED",))
        return _resolution("RESOLVED", (_window(context, semantic_kind="EXPLICIT_DAY", start=day, end=day, complete=day < context.as_of_local_date, phrase="EXPLICIT_DAY"),), ("explicit_day",))
    return None


def _one(
    value: str,
    context: LifeSwitchTemporalContextV1,
    *,
    projection_default_days: int | None,
    prior_window: LifeSwitchTemporalWindowV1 | None,
) -> LifeSwitchTemporalResolutionV1:
    explicit = _explicit(value, context)
    if explicit is not None:
        return explicit
    today = context.as_of_local_date
    lowered = value.lower()
    if re.fullmatch(r"(?:that|the prior period|the previous period)", lowered.strip(" .?!")):
        if prior_window is None or prior_window.timezone_name != context.timezone_name:
            return _resolution("UNAVAILABLE", (), ("CONTINUATION_CONTEXT_UNAVAILABLE",))
        return _resolution(
            "RESOLVED",
            (
                _window(
                    context,
                    semantic_kind="INHERITED_PRIOR_WINDOW",
                    start=prior_window.start_date,
                    end=prior_window.end_date,
                    complete=prior_window.is_complete_period,
                    phrase="IMPLIED_PREVIOUS_PERIOD",
                ),
            ),
            ("inherited_prior_window",),
        )
    if re.search(r"\btoday\b", lowered):
        return _resolution("RESOLVED", (_window(context, semantic_kind="EXPLICIT_DAY", start=today, end=today, complete=False, phrase="TODAY"),), ("today",))
    if re.search(r"\byesterday\b", lowered):
        day = today - dt.timedelta(days=1)
        return _resolution("RESOLVED", (_window(context, semantic_kind="EXPLICIT_DAY", start=day, end=day, complete=True, phrase="YESTERDAY"),), ("yesterday",))
    match = _LAST_N.search(lowered)
    if match:
        days = int(match.group(1))
        if not 1 <= days <= 367:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_UNSUPPORTED",))
        return _resolution("RESOLVED", (_window(context, semantic_kind="TRAILING_DAYS", start=today - dt.timedelta(days=days - 1), end=today, complete=False, phrase="LAST_N_DAYS"),), ("trailing_days",))
    if re.search(r"\bover\s+the\s+last\s+month\b", lowered):
        return _resolution("RESOLVED", (_window(context, semantic_kind="TRAILING_DAYS", start=today - dt.timedelta(days=29), end=today, complete=False, phrase="OVER_LAST_MONTH"),), ("trailing_30_dates",))
    if re.search(r"\bthis\s+week\b", lowered):
        start = today - dt.timedelta(days=today.weekday())
        return _resolution("RESOLVED", (_window(context, semantic_kind="CURRENT_CALENDAR_WEEK", start=start, end=today, complete=today.weekday() == 6, phrase="THIS_WEEK"),), ("current_calendar_week",))
    if re.search(r"\b(?:last|previous)\s+week\b", lowered):
        this_monday = today - dt.timedelta(days=today.weekday())
        start = this_monday - dt.timedelta(days=7)
        return _resolution("RESOLVED", (_window(context, semantic_kind="PREVIOUS_CALENDAR_WEEK", start=start, end=start + dt.timedelta(days=6), complete=True, phrase="LAST_WEEK"),), ("previous_calendar_week",))
    if re.search(r"\bweek\s+before\s+that\b", lowered):
        if prior_window is None:
            return _resolution("UNAVAILABLE", (), ("CONTINUATION_CONTEXT_UNAVAILABLE",))
        if prior_window.timezone_name != context.timezone_name:
            return _resolution("UNAVAILABLE", (), ("CONTINUATION_CONTEXT_UNAVAILABLE",))
        length = (prior_window.end_date - prior_window.start_date).days + 1
        end = prior_window.start_date - dt.timedelta(days=1)
        start = end - dt.timedelta(days=length - 1)
        return _resolution("RESOLVED", (_window(context, semantic_kind="INHERITED_PRIOR_WINDOW", start=start, end=end, complete=True, phrase="WEEK_BEFORE_THAT"),), ("inherited_prior_window",))
    if re.search(r"\bthis\s+month\b", lowered):
        start = today.replace(day=1)
        complete = today.day == calendar.monthrange(today.year, today.month)[1]
        return _resolution("RESOLVED", (_window(context, semantic_kind="CURRENT_CALENDAR_MONTH", start=start, end=today, complete=complete, phrase="THIS_MONTH"),), ("current_calendar_month",))
    if re.search(r"\blast\s+month\b", lowered):
        end = today.replace(day=1) - dt.timedelta(days=1)
        start = end.replace(day=1)
        return _resolution("RESOLVED", (_window(context, semantic_kind="PREVIOUS_CALENDAR_MONTH", start=start, end=end, complete=True, phrase="LAST_MONTH"),), ("previous_calendar_month",))
    match = _IN_MONTH.search(lowered)
    if match:
        month = _MONTHS[match.group("m").lower()]
        year = int(match.group("y")) if match.group("y") else (today.year if month <= today.month else today.year - 1)
        start = dt.date(year, month, 1)
        final = dt.date(year, month, calendar.monthrange(year, month)[1])
        end = min(final, today) if (year, month) == (today.year, today.month) else final
        if start > today:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_UNSUPPORTED",))
        return _resolution("RESOLVED", (_window(context, semantic_kind="EXPLICIT_RANGE", start=start, end=end, complete=end == final and end < today, phrase="IN_MONTH"),), ("named_calendar_month",))
    match = re.search(r"\b(this|last)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
    if match:
        target = _WEEKDAY[match.group(2)]
        this_monday = today - dt.timedelta(days=today.weekday())
        day = this_monday + dt.timedelta(days=target)
        if match.group(1) == "last":
            day -= dt.timedelta(days=7)
        phrase: TemporalPhraseKind = "THIS_WEEKDAY" if match.group(1) == "this" else "LAST_WEEKDAY"
        return _resolution("RESOLVED", (_window(context, semantic_kind="EXPLICIT_DAY", start=day, end=day, complete=day < today, phrase=phrase),), ("account_local_weekday",))
    if re.search(r"\b(?:recently|lately)\b", lowered):
        if projection_default_days is None or not 1 <= projection_default_days <= 367:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_UNSUPPORTED",))
        return _resolution("RESOLVED", (_window(context, semantic_kind="PROJECTION_DEFAULT", start=today - dt.timedelta(days=projection_default_days - 1), end=today, complete=False, phrase="RECENTLY"),), ("projection_declared_default",))
    return _resolution("NONE", (), ("no_temporal_expression",))


def _comparison_operands(value: str) -> tuple[str, str] | None:
    """Return recognized comparison operands without consuming range separators."""

    infix = re.split(
        r"\s+(?:versus|vs\.?|compared\s+(?:with|to))\s+",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    if len(infix) == 2:
        return infix[0].strip(), infix[1].strip()

    incomplete_infix = re.match(
        r"^(.*?)\s+(?:versus|vs\.?|compared\s+(?:with|to))\s*$",
        value,
        re.IGNORECASE,
    )
    if incomplete_infix:
        return incomplete_infix.group(1).strip(), ""

    prefix = re.match(r"^\s*compare\b\s*(.*)$", value, re.IGNORECASE)
    if prefix is None:
        return None

    body = prefix.group(1).strip()
    with_split = re.split(r"\s+with(?:\s+|$)", body, maxsplit=1, flags=re.IGNORECASE)
    if len(with_split) == 2:
        return with_split[0].strip(), with_split[1].strip()

    # A single `to` can be the comparison operator. Multiple `to` tokens are
    # ambiguous with explicit range syntax, so fail closed instead of guessing.
    to_matches = tuple(re.finditer(r"\s+to(?:\s+|$)", body, re.IGNORECASE))
    if len(to_matches) == 1:
        marker = to_matches[0]
        return body[: marker.start()].strip(), body[marker.end() :].strip()
    if len(to_matches) > 1 or re.search(r"\s+(?:with|to)\s*$", body, re.IGNORECASE):
        return body, ""
    return body, ""


def parse_lifeswitch_temporal_windows_v1(
    query: str,
    *,
    context: LifeSwitchTemporalContextV1,
    projection_default_days: int | None = None,
    prior_window: LifeSwitchTemporalWindowV1 | None = None,
) -> LifeSwitchTemporalResolutionV1:
    """Resolve zero, one, or two inclusive account-local windows."""

    value = re.sub(r"\s+", " ", str(query or "")).strip()
    if not value:
        return _resolution("NONE", (), ("no_temporal_expression",))

    comparison = _comparison_operands(value)
    if comparison is not None:
        left_operand, right_operand = comparison
        if not left_operand or not right_operand:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_UNSUPPORTED",))
        left = _one(left_operand, context, projection_default_days=projection_default_days, prior_window=prior_window)
        right = _one(right_operand, context, projection_default_days=projection_default_days, prior_window=prior_window)
        if left.status == "NONE" and right.status == "NONE":
            return _resolution("NONE", (), ("no_temporal_expression",))
        if left.status != "RESOLVED" or right.status != "RESOLVED" or len(left.windows) != 1 or len(right.windows) != 1:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_UNSUPPORTED",))
        try:
            return _resolution("RESOLVED", (left.windows[0], right.windows[0]), ("explicit_comparison",))
        except ValueError:
            return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_CONFLICT",))

    explicit = _explicit(value, context)
    if explicit is not None:
        return explicit

    result = _one(value, context, projection_default_days=projection_default_days, prior_window=prior_window)
    broad_matches = sum(
        bool(re.search(pattern, value, re.IGNORECASE))
        for pattern in (
            r"\bthis\s+week\b",
            r"\b(?:last|previous)\s+week\b",
            r"\bthis\s+month\b",
            r"(?<!over the )\blast\s+month\b",
            r"\bover\s+the\s+last\s+month\b",
        )
    )
    if broad_matches > 1:
        return _resolution("UNAVAILABLE", (), ("TEMPORAL_EXPRESSION_CONFLICT",))
    return result


__all__ = [
    "TEMPORAL_CONTEXT_CONTRACT",
    "TEMPORAL_PARSER_VERSION",
    "TEMPORAL_RESOLUTION_CONTRACT",
    "TEMPORAL_WINDOW_CONTRACT",
    "LifeSwitchTemporalContextV1",
    "LifeSwitchTemporalResolutionV1",
    "LifeSwitchTemporalWindowV1",
    "parse_lifeswitch_temporal_windows_v1",
]
