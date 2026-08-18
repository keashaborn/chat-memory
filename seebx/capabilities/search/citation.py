from __future__ import annotations

"""Deterministic qualification of citation evidence before delivery."""

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from seebx.capabilities.search.provider import (
    TrustedWebProviderSecurityError,
    TrustedWebSourceV1,
)


CITATION_EVIDENCE_CONTRACT = "citation_evidence_v1"

_GENERIC_SOURCE_PATHS = frozenset(
    {
        ("apnews.com", "/"),
        ("arstechnica.com", "/ai"),
        ("arstechnica.com", "/tag/openai"),
        ("fda.gov", "/news-events/fda-newsroom"),
        ("fda.gov", "/news-events/press-announcements"),
        ("huggingface.co", "/blog"),
        ("nhk.or.jp", "/"),
        ("openai.com", "/"),
        ("openai.com", "/news"),
        ("openai.com", "/news/company-announcements"),
        ("openai.com", "/news/product-releases"),
        ("reuters.com", "/"),
        ("status.openai.com", "/history"),
        ("theverge.com", "/"),
        ("who.int", "/news-room/headlines"),
        ("who.int", "/news-room/releases"),
        ("who.int", "/news-room/releases/0"),
        ("wired.com", "/"),
    }
)
_MONTHLY_INDEX_RE = re.compile(r"^/20[0-9]{2}/(?:0[1-9]|1[0-2])$")
_ARCHIVED_PATH_RE = re.compile(
    r"(?:^|[-_/])(?:archive|archived)(?:[-_/]|$)|2020-2025",
    re.IGNORECASE,
)
_PUBLICATION_DATE_RE = re.compile(r"\b(?:19|20)[0-9]{2}\b")
_CURRENT_REFERENCE_PATHS = frozenset(
    {
        (
            "odphp.health.gov",
            "/our-work/nutrition-physical-activity/dietary-guidelines/"
            "current-dietary-guidelines",
        ),
    }
)


@dataclass(frozen=True)
class CitationEvidenceAssessmentV1:
    cited_sources: tuple[TrustedWebSourceV1, ...]
    exact_page_source_count: int
    freshness_verified_source_count: int
    archived_source_count: int
    freshness_status: str


def _host_and_path(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "/").rstrip("/") or "/"
    return host, path


def source_rejection_reason_v1(
    source: TrustedWebSourceV1,
    *,
    policy_pack: str,
    cited: bool,
) -> str | None:
    host, path = _host_and_path(source.url)
    normalized = (host, path.lower())
    if normalized in _GENERIC_SOURCE_PATHS:
        return "generic_index"
    if host == "newsinhealth.nih.gov" and _MONTHLY_INDEX_RE.fullmatch(path):
        return "generic_index"
    if host == "arstechnica.com" and path.startswith("/tag/"):
        return "generic_index"
    if host == "wired.com" and (
        path.startswith("/tag/") or path.startswith("/category/")
    ):
        return "generic_index"
    if policy_pack == "current_news":
        if path.lower().endswith(".pdf"):
            return "document_not_news_page" if cited else "uncited_document"
        if (
            host == "status.openai.com"
            and path.startswith("/incidents/")
            and not cited
        ):
            return "uncited_incident"
    return None


def source_freshness_status_v1(source: TrustedWebSourceV1) -> str:
    host, path = _host_and_path(source.url)
    if source.published_at and _PUBLICATION_DATE_RE.search(source.published_at):
        return "verified_date"
    if (host, path) in _CURRENT_REFERENCE_PATHS:
        return "current_reference"
    if _ARCHIVED_PATH_RE.search(path):
        return "historical"
    return "unverified"


def qualify_source_freshness_v1(
    source: TrustedWebSourceV1,
) -> TrustedWebSourceV1:
    status = source_freshness_status_v1(source)
    if source.freshness_status == status:
        return source
    if hasattr(source, "model_copy"):
        return source.model_copy(update={"freshness_status": status})
    return source.copy(update={"freshness_status": status})


def citation_source_rank_v1(source: TrustedWebSourceV1) -> int:
    status = source_freshness_status_v1(source)
    rank = {
        "current_reference": 0,
        "verified_date": 1,
        "unverified": 2,
        "historical": 3,
    }[status]
    return rank


def assess_citation_evidence_v1(
    *,
    cited_sources: tuple[TrustedWebSourceV1, ...],
    policy_pack: str,
) -> CitationEvidenceAssessmentV1:
    if not cited_sources:
        raise TrustedWebProviderSecurityError(
            "citation_evidence_cited_sources_missing"
        )
    for source in cited_sources:
        reason = source_rejection_reason_v1(
            source,
            policy_pack=policy_pack,
            cited=True,
        )
        if reason is not None:
            raise TrustedWebProviderSecurityError(
                f"citation_evidence_cited_{reason}"
            )

    qualified = tuple(
        sorted(
            (qualify_source_freshness_v1(source) for source in cited_sources),
            key=citation_source_rank_v1,
        )
    )
    statuses = tuple(source.freshness_status for source in qualified)
    verified_count = sum(
        status in {"verified_date", "current_reference"}
        for status in statuses
    )
    archived_count = sum(status == "historical" for status in statuses)
    if archived_count == len(statuses):
        aggregate = "historical"
    elif archived_count or (
        verified_count and verified_count != len(statuses)
    ):
        aggregate = "mixed"
    elif verified_count == len(statuses):
        aggregate = "verified"
    else:
        aggregate = "unverified"
    return CitationEvidenceAssessmentV1(
        cited_sources=qualified,
        exact_page_source_count=len(qualified),
        freshness_verified_source_count=verified_count,
        archived_source_count=archived_count,
        freshness_status=aggregate,
    )


__all__ = [
    "CITATION_EVIDENCE_CONTRACT",
    "CitationEvidenceAssessmentV1",
    "assess_citation_evidence_v1",
    "citation_source_rank_v1",
    "qualify_source_freshness_v1",
    "source_freshness_status_v1",
    "source_rejection_reason_v1",
]
