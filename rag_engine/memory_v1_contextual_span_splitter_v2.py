from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
from typing import Any


CONTRACT_VERSION = "memory_v1_contextual_span_splitter_v2"
SPLITTER_VERSION = "memory_v1_contextual_span_splitter_20260728_v3"
DEFAULT_MAX_SPAN_CHARS = 520
MAX_SPANS = 64

_ABBREVIATIONS = {
    "dr",
    "e.g",
    "etc",
    "i.e",
    "jr",
    "mr",
    "mrs",
    "ms",
    "prof",
    "sr",
    "st",
    "vs",
}
_SENTENCE_BOUNDARY_RE = re.compile(
    r"(?P<punct>[.!?](?:[\"'’”)]*))(?P<space>[ \t]+)"
    r"|(?P<newline>\r?\n+)"
    r"|(?P<tight>[.!?])(?=[A-Z])"
)
_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?P<punct>[;:](?:[\"'’”)]?))(?P<space>[ \t]+)"
    r"|(?P<comma>,)(?=[ \t]+(?:and|but|then|so|yet)\s+"
    r"(?:I|we|he|she|they|it|this|that|these|those|"
    r"[A-Z][^\W\d_][\w'’\-]*))"
    r"|(?P<conjunction>[ \t]+)(?=(?:and|but|then|so|yet)\s+"
    r"(?:I|we|he|she|they|it|this|that|these|those)\b)"
)
_LEADING_CONTEXT_RE = re.compile(
    r"^\s*(?:and|but|then|so|yet)?\s*"
    r"(?:he|she|they|it|this|that|these|those|her|him|them|"
    r"his|hers|their|its)\b",
    re.IGNORECASE,
)
_TEMPORAL_FRAGMENT_RE = re.compile(
    r"^\s*(?:that(?:'s| is| was)?|this(?: is| was)?|it(?:'s| is| was)?)\s+"
    r"(?:when|then|during|before|after|in|at)\b",
    re.IGNORECASE,
)
_BARE_NAME_LIST_RE = re.compile(
    r"^\s*[A-Z][\w'’\-]{0,79}"
    r"(?:\s+(?:and|&)\s+[A-Z][\w'’\-]{0,79})+"
    r"\s*[.!?]?\s*$"
)
_NAME_LIST_ANTECEDENT_RE = re.compile(
    r"\b(?:names?|called|sisters?|brothers?|dogs?|cats?|pets?|animals?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContextualSpanV2:
    ordinal: int
    char_start: int
    char_end: int
    content: str
    content_sha256: str
    boundary_reason: str
    context_needed: bool
    primary_lane: str
    epistemic_role: str
    span_origin: str = "contextual_split_v3"

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trimmed_range(
    text: str,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _is_abbreviation_boundary(text: str, punctuation_end: int) -> bool:
    if punctuation_end <= 0 or text[punctuation_end - 1] != ".":
        return False
    match = re.search(
        r"([A-Za-z](?:[A-Za-z.]*)?)$",
        text[: punctuation_end - 1],
    )
    return bool(match and match.group(1).lower() in _ABBREVIATIONS)


def _sentence_ranges(text: str) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        if match.group("newline") is not None:
            end, next_cursor = match.start(), match.end()
        elif match.group("punct") is not None:
            end = match.end("punct")
            if _is_abbreviation_boundary(text, end):
                continue
            next_cursor = match.end()
        else:
            end = match.end("tight")
            next_cursor = end
        value = _trimmed_range(text, cursor, end)
        if value:
            ranges.append((*value, "sentence_boundary"))
        cursor = next_cursor
    value = _trimmed_range(text, cursor, len(text))
    if value:
        ranges.append((*value, "terminal_span"))
    return ranges


def _candidate_clause_ends(
    text: str,
    start: int,
    hard_end: int,
) -> list[int]:
    candidates: list[int] = []
    for match in _CLAUSE_BOUNDARY_RE.finditer(text, start, hard_end):
        if match.group("punct") is not None:
            candidates.append(match.end("punct"))
        elif match.group("comma") is not None:
            candidates.append(match.end("comma"))
        else:
            candidates.append(match.start("conjunction"))
    return candidates


def _split_long_range(
    text: str,
    start: int,
    end: int,
    *,
    max_span_chars: int,
) -> list[tuple[int, int, str]]:
    result: list[tuple[int, int, str]] = []
    cursor = start
    minimum_clause_chars = min(100, max_span_chars // 3)
    while end - cursor > max_span_chars:
        ceiling = cursor + max_span_chars
        candidates = [
            candidate
            for candidate in _candidate_clause_ends(text, cursor, ceiling + 1)
            if candidate - cursor >= minimum_clause_chars
        ]
        if candidates:
            split_at = candidates[-1]
            reason = "clause_boundary"
        else:
            whitespace = [
                match.start()
                for match in re.finditer(r"\s+", text, cursor, ceiling + 1)
                if match.start() - cursor >= minimum_clause_chars
            ]
            if not whitespace:
                raise ValueError(
                    "contextual span contains an unsplittable token longer "
                    "than the configured maximum"
                )
            split_at = whitespace[-1]
            reason = "length_boundary"
        value = _trimmed_range(text, cursor, split_at)
        if value is None:
            raise ValueError("contextual span split produced an empty range")
        result.append((*value, reason))
        cursor = split_at
        while cursor < end and text[cursor].isspace():
            cursor += 1
    value = _trimmed_range(text, cursor, end)
    if value:
        result.append((*value, "terminal_span"))
    return result


def _context_needed(content: str) -> bool:
    return bool(
        _LEADING_CONTEXT_RE.search(content)
        or _TEMPORAL_FRAGMENT_RE.search(content)
        or _BARE_NAME_LIST_RE.fullmatch(content)
        or (
            len(content) < 80
            and re.search(
                r"\b(?:earlier|later|before|after|then|there|that|this|it)\b",
                content,
                re.IGNORECASE,
            )
        )
    )


def _coalesce_bare_name_lists(
    text: str,
    ranges: list[tuple[int, int, str]],
    *,
    max_span_chars: int,
) -> list[tuple[int, int, str]]:
    """Keep a bare appositive name list with its explicit antecedent.

    A span such as ``Bella and Beauty.`` has no independent relationship or
    species assertion. Splitting it away from ``two ... sisters`` destroys the
    source-grounded naming relation and causes downstream role-only entities.
    """

    coalesced: list[tuple[int, int, str]] = []
    for start, end, reason in ranges:
        content = text[start:end]
        if (
            coalesced
            and _BARE_NAME_LIST_RE.fullmatch(content)
            and _NAME_LIST_ANTECEDENT_RE.search(
                text[coalesced[-1][0]:coalesced[-1][1]]
            )
        ):
            prior_start, _prior_end, _prior_reason = coalesced[-1]
            if end - prior_start <= max_span_chars:
                coalesced[-1] = (
                    prior_start,
                    end,
                    "appositive_name_list_coalesced",
                )
                continue
        coalesced.append((start, end, reason))
    return coalesced


def contextual_spans_v2(
    text: str,
    *,
    max_span_chars: int = DEFAULT_MAX_SPAN_CHARS,
) -> list[ContextualSpanV2]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("contextual source text must be non-empty")
    if not 160 <= max_span_chars <= 600:
        raise ValueError("max_span_chars must be between 160 and 600")

    raw_ranges: list[tuple[int, int, str]] = []
    for start, end, reason in _sentence_ranges(text):
        if end - start <= max_span_chars:
            raw_ranges.append((start, end, reason))
        else:
            raw_ranges.extend(
                _split_long_range(
                    text,
                    start,
                    end,
                    max_span_chars=max_span_chars,
                )
            )
    raw_ranges = _coalesce_bare_name_lists(
        text,
        raw_ranges,
        max_span_chars=max_span_chars,
    )
    if not 1 <= len(raw_ranges) <= MAX_SPANS:
        raise ValueError("contextual source must produce one to 64 spans")

    spans: list[ContextualSpanV2] = []
    previous_end = 0
    for ordinal, (start, end, reason) in enumerate(raw_ranges):
        if start < previous_end or text[previous_end:start].strip():
            raise ValueError("contextual split coverage is invalid")
        content = text[start:end]
        if not content.strip() or len(content) > max_span_chars:
            raise ValueError("contextual split span length is invalid")
        spans.append(
            ContextualSpanV2(
                ordinal=ordinal,
                char_start=start,
                char_end=end,
                content=content,
                content_sha256=sha256_text(content),
                boundary_reason=(
                    "terminal_span"
                    if ordinal == len(raw_ranges) - 1
                    else reason
                ),
                context_needed=_context_needed(content),
                primary_lane="unclassified_user_statement",
                epistemic_role="user_report_unclassified",
            )
        )
        previous_end = end
    if text[previous_end:].strip():
        raise ValueError("contextual split leaves trailing source content")
    return spans


def contextual_span_plan_v2(
    text: str,
    *,
    max_span_chars: int = DEFAULT_MAX_SPAN_CHARS,
) -> list[dict[str, Any]]:
    return [
        span.public_dict()
        for span in contextual_spans_v2(
            text,
            max_span_chars=max_span_chars,
        )
    ]
