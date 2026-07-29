from __future__ import annotations

"""Server-owned admission of provider-observed web evidence."""

from dataclasses import dataclass

from rag_engine.citation_evidence_v1 import (
    CITATION_EVIDENCE_CONTRACT,
    assess_citation_evidence_v1,
    citation_source_rank_v1,
    qualify_source_freshness_v1,
    source_rejection_reason_v1,
)
from rag_engine.trusted_web_provider_v1 import (
    TrustedWebProviderSecurityError,
    TrustedWebSourceV1,
)


WEB_EVIDENCE_ADMISSION_CONTRACT = "web_evidence_admission_v1"
CURRENT_NEWS_MAX_ADMITTED_SOURCES = 10
TRUSTED_HEALTH_MAX_ADMITTED_SOURCES = 5

@dataclass(frozen=True)
class TrustedWebEvidenceAdmissionV1:
    validated_cited_sources: tuple[TrustedWebSourceV1, ...]
    admitted_sources: tuple[TrustedWebSourceV1, ...]
    rejected_source_reasons: tuple[tuple[str, str], ...]
    max_sources: int
    citation_evidence_contract: str
    exact_page_source_count: int
    freshness_verified_source_count: int
    archived_source_count: int
    freshness_status: str

    @property
    def rejected_source_count(self) -> int:
        return len(self.rejected_source_reasons)


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
    citation = assess_citation_evidence_v1(
        cited_sources=cited_sources,
        policy_pack=policy_pack,
    )

    admitted: list[TrustedWebSourceV1] = list(citation.cited_sources)
    admitted_urls = set(cited_urls)
    rejected: list[tuple[str, str]] = []

    supporting_sources = sorted(
        consulted_sources,
        key=citation_source_rank_v1,
    )
    for source in supporting_sources:
        if source.url in admitted_urls:
            continue
        rejection_reason = source_rejection_reason_v1(
            source,
            policy_pack=policy_pack,
            cited=False,
        )
        if rejection_reason is not None:
            rejected.append((source.url, rejection_reason))
            continue
        if len(admitted) >= max_sources:
            rejected.append((source.url, "budget_exceeded"))
            continue
        admitted.append(qualify_source_freshness_v1(source))
        admitted_urls.add(source.url)

    return TrustedWebEvidenceAdmissionV1(
        validated_cited_sources=citation.cited_sources,
        admitted_sources=tuple(admitted),
        rejected_source_reasons=tuple(rejected),
        max_sources=max_sources,
        citation_evidence_contract=CITATION_EVIDENCE_CONTRACT,
        exact_page_source_count=citation.exact_page_source_count,
        freshness_verified_source_count=(
            citation.freshness_verified_source_count
        ),
        archived_source_count=citation.archived_source_count,
        freshness_status=citation.freshness_status,
    )


__all__ = [
    "CURRENT_NEWS_MAX_ADMITTED_SOURCES",
    "TRUSTED_HEALTH_MAX_ADMITTED_SOURCES",
    "TrustedWebEvidenceAdmissionV1",
    "WEB_EVIDENCE_ADMISSION_CONTRACT",
    "admit_trusted_web_sources_v1",
]
