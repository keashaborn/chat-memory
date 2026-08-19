from __future__ import annotations

"""Server-owned planning for bounded LifeSwitch structured-data access."""

import datetime as dt
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


LIFESWITCH_DATA_PLAN_CONTRACT = "lifeswitch_data_plan_v1"
LIFESWITCH_DATA_POLICY_VERSION = "lifeswitch_data_policy_v1_3"

LifeSwitchDataIntent = Literal[
    "OFF",
    "OVERALL_STATUS",
    "PLAN",
    "NUTRITION_DAY",
    "NUTRITION_RANGE",
    "TRAINING_SUMMARY",
    "TRAINING_SESSION",
    "TRAINING_RANGE",
    "DAILY_STATUS_RANGE",
    "EXERCISE_PROGRESSION",
    "EXERCISE_FREQUENCY",
    "LIFTING_PROGRESSION_SUMMARY",
    "MEASUREMENTS_SUMMARY",
]
LifeSwitchDomain = Literal[
    "plan",
    "nutrition",
    "training",
    "conditioning",
    "measurements",
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    value = _jsonable(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


class LifeSwitchDataWindowV1(_StrictFrozenModel):
    start_date: dt.date
    end_date: dt.date

    @model_validator(mode="after")
    def ordered_and_bounded(self) -> "LifeSwitchDataWindowV1":
        if self.end_date < self.start_date:
            raise ValueError("LifeSwitch data window is reversed")
        if (self.end_date - self.start_date).days > 366:
            raise ValueError("LifeSwitch data window exceeds 367 days")
        return self


class LifeSwitchDataBudgetV1(_StrictFrozenModel):
    max_rows: int = Field(ge=0, le=500)
    max_prompt_tokens: int = Field(ge=0, le=1000)


class LifeSwitchDataPlanV1(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_DATA_PLAN_CONTRACT] = (
        LIFESWITCH_DATA_PLAN_CONTRACT
    )
    policy_version: Literal[LIFESWITCH_DATA_POLICY_VERSION] = (
        LIFESWITCH_DATA_POLICY_VERSION
    )
    intent: LifeSwitchDataIntent
    domains: tuple[LifeSwitchDomain, ...]
    reason_codes: tuple[str, ...]
    confidence: Literal["high", "medium"]
    query_context: Literal["current_message_only"] = "current_message_only"
    data_access: bool
    window: LifeSwitchDataWindowV1 | None = None
    subject: str | None = Field(default=None, min_length=1, max_length=80)
    budget: LifeSwitchDataBudgetV1
    plan_sha256: str

    @model_validator(mode="after")
    def coherent_and_bound(self) -> "LifeSwitchDataPlanV1":
        expected_domains = tuple(dict.fromkeys(self.domains))
        if expected_domains != self.domains:
            raise ValueError("LifeSwitch domains must be unique and ordered")
        if self.intent == "OFF":
            if self.data_access or self.domains or self.window or self.subject:
                raise ValueError("OFF plan cannot carry data access")
            if self.budget.max_rows or self.budget.max_prompt_tokens:
                raise ValueError("OFF plan requires a zero budget")
        else:
            if not self.data_access or not self.domains:
                raise ValueError("active LifeSwitch plan requires domains")
            if self.budget.max_prompt_tokens <= 0:
                raise ValueError("active LifeSwitch plan requires prompt budget")
        if self.intent == "EXERCISE_PROGRESSION" and not self.subject:
            raise ValueError("exercise progression requires a bounded subject")
        if self.intent != "EXERCISE_PROGRESSION" and self.subject is not None:
            raise ValueError("only exercise progression accepts a subject")
        payload = self.model_dump(mode="json", exclude={"plan_sha256"})
        if self.plan_sha256 != _sha256(payload):
            raise ValueError("LifeSwitch data plan hash mismatch")
        return self


_SELF = re.compile(r"\b(i|me|my|mine)\b", re.IGNORECASE)
_OVERALL = (
    re.compile(r"^\s*how am i doing\??\s*$", re.IGNORECASE),
    re.compile(
        r"\b(?:am i|how am i)\b.{0,100}\b(?:meeting|doing|tracking|following)\b"
        r".{0,100}\b(?:plan|goals?|macros?|nutrition|training|exercise|workouts?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:look at|review|check|compare)\b.{0,60}\bmy plan\b.{0,160}"
        r"\b(?:macros?|nutrition|training|exercise|workouts?|goals?)\b",
        re.IGNORECASE,
    ),
)
_PLAN = re.compile(
    r"\b(?:my|our)\s+(?:current\s+)?(?:plan|targets?|goals?)\b|"
    r"\bwhat (?:am i|are we) supposed to (?:do|eat)\b",
    re.IGNORECASE,
)
_NUTRITION = re.compile(
    r"\b(?:macro|macros|protein|calories?|kcal|carbs?|carbohydrates?|fat|"
    r"nutrition|food log|meal log|intake)\b",
    re.IGNORECASE,
)
_TRAINING = re.compile(
    r"\b(?:training|workouts?|lifting|weightlifting|strength|sets?|volume|"
    r"conditioning|cardio|exercises?|exercise sessions?|movements?|lifts?)\b",
    re.IGNORECASE,
)
_MEASUREMENTS = re.compile(
    r"\b(?:my\s+)?(?:weight|waist|body[ -]?fat|measurements?|physique)\b",
    re.IGNORECASE,
)
_PROGRESSION = re.compile(
    r"\b(?:progress(?:ed|ing|ion)?|improv(?:e|ed|ing|ement)|"
    r"getting stronger|going up)\b",
    re.IGNORECASE,
)
_LIFTING_LOAD = re.compile(
    r"\bweights\b|\b(?:lifting|training)\s+(?:loads?|poundages?)\b",
    re.IGNORECASE,
)
_SESSION = re.compile(
    r"\b(?:session|workout|trained|lifted|training)\b",
    re.IGNORECASE,
)
_RANGE = re.compile(
    r"\b(?:this week|last week|past week|last 7 days|last 14 days|"
    r"last 21 days|last (?:couple|two) (?:of )?weeks|past (?:couple|two) "
    r"(?:of )?weeks|recently|over time|lately)\b",
    re.IGNORECASE,
)
_WEEKDAY = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_TODAY = re.compile(r"\btoday\b", re.IGNORECASE)
_YESTERDAY = re.compile(r"\byesterday\b", re.IGNORECASE)
_DAILY_SERIES = re.compile(
    r"\b(?:for\s+each\s+day|each\s+day|day[- ]by[- ]day|daily|"
    r"which\s+days?|list\s+(?:the\s+)?dates?)\b",
    re.IGNORECASE,
)
_MONTH_NUMBER = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True))
_ABSOLUTE_DATE_RANGE = re.compile(
    rf"\b(?:from\s+)?"
    rf"(?P<start_month>{_MONTH_PATTERN})\.?\s+"
    rf"(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s+(?P<start_year>\d{{4}}))?\s*"
    rf"(?:through|thru|to|until|[-–—])\s*"
    rf"(?:(?P<end_month>{_MONTH_PATTERN})\.?\s+)?"
    rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s+(?P<end_year>\d{{4}}))?\b",
    re.IGNORECASE,
)
_PROGRESSION_SUBJECT = (
    re.compile(
        r"\b(?:show|tell)\s+(?:me\s+)?my\s+(?:recent\s+|latest\s+|current\s+)?"
        r"progress(?:ion)?\s+(?:on|for|with)\s+(?:my\s+)?"
        r"([a-z][a-z0-9 ()&+'’\-/]{1,60}?)(?:[?.!,]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:how (?:is|are)|are)\s+my\s+"
        r"([a-z][a-z0-9 ()&+'’\-/]{1,60}?)\s+"
        r"(?:progressing|improving|going up)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bmy\s+([a-z][a-z0-9 ()&+'’\-/]{1,60}?)\s+progress(?:ion)?\b",
        re.IGNORECASE,
    ),
)
_IMPLICIT_PERSONAL_LIFTING_PROGRESS = (
    re.compile(
        r"^\s*(?:which|what)\s+(?:lifts?|exercises?|movements?)\s+"
        r"(?:have|are)\s+(?:progressed|progressing|improved|improving)\b"
        r".{0,80}\b(?:last|past|recent|this)\b",
        re.IGNORECASE,
    ),
)
_HISTORY_RANGE_DAYS = (
    (
        re.compile(
            r"\b(?:(?:this|last|past|previous)\s+week|"
            r"(?:last|past|previous)\s+7\s+days?)\b",
            re.IGNORECASE,
        ),
        7,
    ),
    (
        re.compile(
            r"\b(?:last|past|previous|recent)\s+"
            r"(?:(?:couple|two|2)\s+weeks?|14\s+days?)\b",
            re.IGNORECASE,
        ),
        14,
    ),
    (
        re.compile(
            r"\b(?:last|past|previous|recent)\s+"
            r"(?:(?:three|3)\s+weeks?|21\s+days?)\b",
            re.IGNORECASE,
        ),
        21,
    ),
    (
        re.compile(
            r"\b(?:last|past|previous|recent)\s+"
            r"(?:(?:four|4)\s+weeks?|28\s+days?)\b",
            re.IGNORECASE,
        ),
        28,
    ),
    (
        re.compile(
            r"\b(?:last|past|previous|recent)\s+(?:month|30\s+days?)\b",
            re.IGNORECASE,
        ),
        30,
    ),
)
_EXERCISE_FREQUENCY = (
    re.compile(
        r"\bwhat\s+(?:exercises?|movements?|lifts?)\s+(?:do|have)\s+i\s+"
        r"(?:do|done|perform(?:ed)?|train(?:ed)?)\s+(?:the\s+)?most\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:which|what|show)\s+(?:of\s+)?my\s+(?:exercises?|movements?|lifts?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat\s+(?:exercises?|movements?|lifts?)\s+(?:have|did)\s+i\s+"
        r"(?:do|done|perform(?:ed)?|train(?:ed)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:my\s+)?most\s+(?:common|frequent(?:ly performed)?)\s+"
        r"(?:exercises?|movements?|lifts?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:do you|can you)\s+have\s+access\s+to\s+(?:any\s+)?"
        r"(?:of\s+)?my\s+exercise\s+(?:data|history)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdo you\s+have\s+access\s+to\s+(?:any\s+)?exercise\s+data\b"
        r".{0,80}\b(?:i(?:'ve| have)?\s+done|my)\b",
        re.IGNORECASE,
    ),
)

_DOMAIN_ORDER: tuple[LifeSwitchDomain, ...] = (
    "plan",
    "nutrition",
    "training",
    "conditioning",
    "measurements",
)


def _matches(value: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def _window(end: dt.date, days: int) -> LifeSwitchDataWindowV1:
    return LifeSwitchDataWindowV1(
        start_date=end - dt.timedelta(days=days - 1),
        end_date=end,
    )


def _absolute_date_window(
    value: str,
    today: dt.date,
) -> LifeSwitchDataWindowV1 | None:
    match = _ABSOLUTE_DATE_RANGE.search(value)
    if match is None:
        return None
    try:
        start_month = _MONTH_NUMBER[match.group("start_month").lower()]
        end_month_name = match.group("end_month")
        end_month = (
            _MONTH_NUMBER[end_month_name.lower()]
            if end_month_name
            else start_month
        )
        start_day = int(match.group("start_day"))
        end_day = int(match.group("end_day"))
        start_year_text = match.group("start_year")
        end_year_text = match.group("end_year")

        if end_year_text:
            end_year = int(end_year_text)
        elif start_year_text:
            end_year = int(start_year_text)
        else:
            end_year = today.year
            if dt.date(end_year, end_month, end_day) > today:
                end_year -= 1

        start_year = int(start_year_text) if start_year_text else end_year
        start_date = dt.date(start_year, start_month, start_day)
        end_date = dt.date(end_year, end_month, end_day)
        if not start_year_text and start_date > end_date:
            start_date = dt.date(end_year - 1, start_month, start_day)
        return LifeSwitchDataWindowV1(
            start_date=start_date,
            end_date=end_date,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _relative_history_window(
    value: str,
    end: dt.date,
) -> LifeSwitchDataWindowV1 | None:
    for pattern, days in _HISTORY_RANGE_DAYS:
        if pattern.search(value):
            return _window(end, days)
    return None


def _history_window(value: str, end: dt.date) -> LifeSwitchDataWindowV1:
    absolute = _absolute_date_window(value, end)
    if absolute is not None:
        return absolute
    relative = _relative_history_window(value, end)
    if relative is not None:
        return relative
    return _window(end, 84)


def _day_from_query(value: str, today: dt.date) -> dt.date | None:
    if _TODAY.search(value):
        return today
    if _YESTERDAY.search(value):
        return today - dt.timedelta(days=1)
    match = _WEEKDAY.search(value)
    if match is None:
        return None
    target = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ).index(match.group(1).lower())
    return today - dt.timedelta(days=(today.weekday() - target) % 7)


def _subject(value: str) -> str | None:
    for pattern in _PROGRESSION_SUBJECT:
        match = pattern.search(value)
        if match:
            cleaned = re.sub(r"\s+", " ", match.group(1)).strip(" .?,'\"")
            if cleaned.lower() in {"current", "latest", "overall", "recent"}:
                continue
            return cleaned[:80] or None
    return None


def _make_plan(
    *,
    intent: LifeSwitchDataIntent,
    domains: tuple[LifeSwitchDomain, ...],
    reasons: tuple[str, ...],
    confidence: Literal["high", "medium"],
    window: LifeSwitchDataWindowV1 | None,
    subject: str | None,
    max_rows: int,
    max_prompt_tokens: int,
) -> LifeSwitchDataPlanV1:
    payload = {
        "contract_version": LIFESWITCH_DATA_PLAN_CONTRACT,
        "policy_version": LIFESWITCH_DATA_POLICY_VERSION,
        "intent": intent,
        "domains": domains,
        "reason_codes": tuple(dict.fromkeys(reasons)),
        "confidence": confidence,
        "query_context": "current_message_only",
        "data_access": intent != "OFF",
        "window": window,
        "subject": subject,
        "budget": LifeSwitchDataBudgetV1(
            max_rows=max_rows,
            max_prompt_tokens=max_prompt_tokens,
        ),
    }
    return LifeSwitchDataPlanV1(
        **payload,
        plan_sha256=_sha256(payload),
    )


def create_lifeswitch_data_plan_v1(
    query: str,
    *,
    today: dt.date | None = None,
) -> LifeSwitchDataPlanV1:
    """Create a fail-closed deterministic LifeSwitch data plan.

    This planner intentionally covers only clear, common request shapes. A later
    provider classifier may propose additional typed signals, but it may not
    choose an owner, table, or unbounded query.
    """

    value = re.sub(r"\s+", " ", str(query or "")).strip()
    local_today = today or dt.date.today()
    if not value:
        return _make_plan(
            intent="OFF",
            domains=(),
            reasons=("no_lifeswitch_signal",),
            confidence="high",
            window=None,
            subject=None,
            max_rows=0,
            max_prompt_tokens=0,
        )

    personal = bool(_SELF.search(value))
    nutrition = bool(_NUTRITION.search(value))
    lifting_progress = bool(
        _LIFTING_LOAD.search(value)
        and (_PROGRESSION.search(value) or _RANGE.search(value))
    )
    broad_lifting_progress = bool(
        lifting_progress
        or (
            re.search(r"\b(?:weightlifting|lifting|lifts?)\b", value, re.IGNORECASE)
            and (_PROGRESSION.search(value) or _RANGE.search(value))
        )
    )
    implicit_personal_lifting_progress = _matches(
        value,
        _IMPLICIT_PERSONAL_LIFTING_PROGRESS,
    )
    training = bool(_TRAINING.search(value) or lifting_progress)
    training_reason = (
        "explicit_personal_lifting_progress_request"
        if lifting_progress
        else "explicit_personal_training_request"
    )
    measurements = bool(_MEASUREMENTS.search(value))
    plan = bool(_PLAN.search(value))
    absolute_window = _absolute_date_window(value, local_today)
    relative_window = _relative_history_window(value, local_today)
    requested_range = absolute_window or relative_window
    daily_series = bool(_DAILY_SERIES.search(value))

    if (
        personal
        and plan
        and (nutrition or training)
        and requested_range is None
    ):
        return _make_plan(
            intent="OVERALL_STATUS",
            domains=_DOMAIN_ORDER,
            reasons=("explicit_plan_adherence_request",),
            confidence="high",
            window=_window(local_today, 90),
            subject=None,
            max_rows=400,
            max_prompt_tokens=800,
        )

    if _matches(value, _OVERALL):
        return _make_plan(
            intent="OVERALL_STATUS",
            domains=_DOMAIN_ORDER,
            reasons=("explicit_personal_status_request",),
            confidence="high",
            window=_window(local_today, 90),
            subject=None,
            max_rows=400,
            max_prompt_tokens=800,
        )

    if personal and measurements:
        return _make_plan(
            intent="MEASUREMENTS_SUMMARY",
            domains=("measurements",),
            reasons=("explicit_personal_measurements_request",),
            confidence="high",
            window=_window(local_today, 90),
            subject=None,
            max_rows=100,
            max_prompt_tokens=450,
        )

    if personal and nutrition and training and requested_range:
        return _make_plan(
            intent="DAILY_STATUS_RANGE",
            domains=("nutrition", "training", "conditioning", "plan"),
            reasons=("explicit_personal_daily_status_range",),
            confidence="high",
            window=requested_range,
            subject=None,
            max_rows=500,
            max_prompt_tokens=800,
        )

    progression_subject = _subject(value)
    if personal and progression_subject and _PROGRESSION.search(value):
        return _make_plan(
            intent="EXERCISE_PROGRESSION",
            domains=("training",),
            reasons=("explicit_personal_exercise_progression",),
            confidence="high",
            window=_history_window(value, local_today),
            subject=progression_subject,
            max_rows=200,
            max_prompt_tokens=600,
        )

    if personal and _matches(value, _EXERCISE_FREQUENCY):
        return _make_plan(
            intent="EXERCISE_FREQUENCY",
            domains=("training",),
            reasons=("explicit_personal_exercise_history_request",),
            confidence="high",
            window=_history_window(value, local_today),
            subject=None,
            max_rows=12,
            max_prompt_tokens=550,
        )

    if (personal or implicit_personal_lifting_progress) and broad_lifting_progress:
        return _make_plan(
            intent="LIFTING_PROGRESSION_SUMMARY",
            domains=("training", "plan"),
            reasons=(
                "explicit_personal_lifting_progress_request"
                if personal
                else "implicit_personal_lifting_progress_request",
            ),
            confidence="high",
            window=_history_window(value, local_today),
            subject=None,
            max_rows=12,
            max_prompt_tokens=750,
        )

    requested_day = _day_from_query(value, local_today)
    if personal and nutrition:
        if requested_day is not None:
            return _make_plan(
                intent="NUTRITION_DAY",
                domains=("nutrition", "plan"),
                reasons=("explicit_personal_nutrition_day",),
                confidence="high",
                window=_window(requested_day, 1),
                subject=None,
                max_rows=50,
                max_prompt_tokens=250,
            )
        selected_window = requested_range or _window(local_today, 21)
        return _make_plan(
            intent="NUTRITION_RANGE",
            domains=("nutrition", "plan"),
            reasons=(
                "explicit_personal_nutrition_range"
                if (_RANGE.search(value) or requested_range is not None)
                else "explicit_personal_nutrition_request"
            ,),
            confidence=(
                "high"
                if (_RANGE.search(value) or requested_range is not None)
                else "medium"
            ),
            window=selected_window,
            subject=None,
            max_rows=100,
            max_prompt_tokens=450,
        )

    if personal and training:
        if requested_day is not None and _SESSION.search(value):
            return _make_plan(
                intent="TRAINING_SESSION",
                domains=("training", "conditioning"),
                reasons=("explicit_personal_training_day",),
                confidence="high",
                window=_window(requested_day, 1),
                subject=None,
                max_rows=100,
                max_prompt_tokens=450,
            )
        if absolute_window is not None or (relative_window is not None and daily_series):
            return _make_plan(
                intent="TRAINING_RANGE",
                domains=("training", "conditioning"),
                reasons=("explicit_personal_training_range",),
                confidence="high",
                window=absolute_window or relative_window,
                subject=None,
                max_rows=500,
                max_prompt_tokens=650,
            )
        return _make_plan(
            intent="TRAINING_SUMMARY",
            domains=("training", "conditioning", "plan"),
            reasons=(training_reason,),
            confidence="high",
            window=_window(local_today, 28),
            subject=None,
            max_rows=250,
            max_prompt_tokens=500,
        )

    if personal and plan:
        return _make_plan(
            intent="PLAN",
            domains=("plan",),
            reasons=("explicit_personal_plan_request",),
            confidence="high",
            window=None,
            subject=None,
            max_rows=2,
            max_prompt_tokens=500,
        )

    return _make_plan(
        intent="OFF",
        domains=(),
        reasons=("no_lifeswitch_signal",),
        confidence="high",
        window=None,
        subject=None,
        max_rows=0,
        max_prompt_tokens=0,
    )


__all__ = [
    "LIFESWITCH_DATA_PLAN_CONTRACT",
    "LIFESWITCH_DATA_POLICY_VERSION",
    "LifeSwitchDataPlanV1",
    "LifeSwitchDataWindowV1",
    "create_lifeswitch_data_plan_v1",
]
