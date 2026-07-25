from __future__ import annotations

"""Server-owned admission of provider-observed web evidence."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from rag_engine.trusted_web_provider_v1 import (
    TrustedWebProviderSecurityError,
    TrustedWebSourceV1,
)


WEB_EVIDENCE_ADMISSION_CONTRACT = "web_evidence_admission_v1"
CURRENT_NEWS_MAX_ADMITTED_SOURCES = 10
TRUSTED_HEALTH_MAX_ADMITTED_SOURCES = 5

_CURRENT_NEWS_GENERIC_PATHS = {
    ("openai.com", "/"),
    ("openai.com", "/news"),
    ("openai.com", "/news/company-announcements"),
    ("openai.com", "/news/product-releases"),
    ("arstechnica.com", "/ai"),
    ("arstechnica.com", "/tag/openai"),
}


@dataclass(frozen=True)
class TrustedWebEvidenceAdmissionV1:
    admitted_sources: tuple[TrustedWebSourceV1, ...]
    rejected_source_reasons: tuple[tuple[str, str], ...]
    max_sources: int

    @property
    def rejected_source_count(self) -> int:
        return len(self.rejected_source_reasons)


def _host_and_path(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "/").rstrip("/") or "/"
    return host, path


def _current_news_rejection_reason(source: TrustedWebSourceV1) -> str | None:
    host, path = _host_and_path(source.url)
    if path.lower().endswith(".pdf"):
        return "uncited_document"
    if host == "status.openai.com" and path.startswith("/incidents/"):
        return "uncited_incident"
    if (host, path) in _CURRENT_NEWS_GENERIC_PATHS:
        return "generic_index"
    return None


def admit_trusted_web_sources_v1(
    *,
    cited_sources: tuple[TrustedWebSourceV1, ...],
    consulted_sources: tuple[TrustedWebSourceV1, ...],
    max_sources: int,
    policy_pack: str,
) -> TrustedWebEvidenceAdmissionV1:
    if max_sources < 1:
        raise TrustedWebProviderSecurityError(
            "trusted_web_admission_budget_invalid"
        )

    consulted_by_url = {source.url: source for source in consulted_sources}
    cited_urls = {source.url for source in cited_sources}
    if len(cited_urls) != len(cited_sources):
        raise TrustedWebProviderSecurityError(
            "trusted_web_duplicate_cited_source"
        )
    if not cited_urls.issubset(consulted_by_url):
        raise TrustedWebProviderSecurityError(
            "trusted_web_cited_source_not_consulted"
        )
    if len(cited_sources) > max_sources:
        raise TrustedWebProviderSecurityError(
            "trusted_web_cited_sources_exceed_budget"
        )

    admitted: list[TrustedWebSourceV1] = list(cited_sources)
    admitted_urls = set(cited_urls)
    rejected: list[tuple[str, str]] = []

    for source in consulted_sources:
        if source.url in admitted_urls:
            continue
        rejection_reason = None
        if policy_pack == "current_news":
            rejection_reason = _current_news_rejection_reason(source)
        if rejection_reason is not None:
            rejected.append((source.url, rejection_reason))
            continue
        if len(admitted) >= max_sources:
            rejected.append((source.url, "budget_exceeded"))
            continue
        admitted.append(source)
        admitted_urls.add(source.url)

    return TrustedWebEvidenceAdmissionV1(
        admitted_sources=tuple(admitted),
        rejected_source_reasons=tuple(rejected),
        max_sources=max_sources,
    )


__all__ = [
    "CURRENT_NEWS_MAX_ADMITTED_SOURCES",
    "TRUSTED_HEALTH_MAX_ADMITTED_SOURCES",
    "TrustedWebEvidenceAdmissionV1",
    "WEB_EVIDENCE_ADMISSION_CONTRACT",
    "admit_trusted_web_sources_v1",
]
