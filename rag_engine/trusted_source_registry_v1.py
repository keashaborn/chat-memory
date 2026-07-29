from __future__ import annotations

"""Versioned, server-owned domain packs for bounded trusted-web retrieval."""

from enum import Enum

from pydantic import BaseModel, ConfigDict


SOURCE_REGISTRY_VERSION = "trusted_source_registry_v1"

ODS_DOMAIN = "ods.od.nih.gov"
MEDLINEPLUS_DOMAIN = "medlineplus.gov"
DGA_DOMAIN = "dietaryguidelines.gov"
REALFOOD_DOMAIN = "realfood.gov"
ODPHP_DOMAIN = "odphp.health.gov"
FDA_DOMAIN = "fda.gov"
FDC_DOMAIN = "fdc.nal.usda.gov"
USDA_DOMAIN = "usda.gov"
PUBMED_DOMAIN = "pubmed.ncbi.nlm.nih.gov"
PMC_DOMAIN = "pmc.ncbi.nlm.nih.gov"
CLINICAL_TRIALS_DOMAIN = "clinicaltrials.gov"
CDC_DOMAIN = "cdc.gov"
NIH_DOMAIN = "nih.gov"
WHO_DOMAIN = "who.int"
COCHRANE_DOMAIN = "cochranelibrary.com"
ACSM_DOMAIN = "acsm.org"
NSCA_DOMAIN = "nsca.com"
BACB_DOMAIN = "bacb.com"

OPENAI_DOMAIN = "openai.com"
OPENAI_DEVELOPERS_DOMAIN = "developers.openai.com"
HUGGINGFACE_DOMAIN = "huggingface.co"
SUPABASE_DOMAIN = "supabase.com"
NEXTJS_DOMAIN = "nextjs.org"
REACT_DOMAIN = "react.dev"
POSTGRESQL_DOMAIN = "postgresql.org"
QDRANT_DOMAIN = "qdrant.tech"
GITHUB_DOCS_DOMAIN = "docs.github.com"
MDN_DOMAIN = "developer.mozilla.org"
OWASP_DOMAIN = "owasp.org"
NIST_DOMAIN = "nist.gov"
CISA_DOMAIN = "cisa.gov"
IETF_DOMAIN = "ietf.org"
W3C_DOMAIN = "w3.org"

APNEWS_DOMAIN = "apnews.com"
REUTERS_DOMAIN = "reuters.com"
NHK_DOMAIN = "nhk.or.jp"
ARSTECHNICA_DOMAIN = "arstechnica.com"
WIRED_DOMAIN = "wired.com"
THEVERGE_DOMAIN = "theverge.com"


class TrustedSourcePackIdV1(str, Enum):
    GENERAL_CURRENT_NEWS = "general_current_news"
    AI_TECH_CURRENT_NEWS = "ai_tech_current_news"
    MEDICAL_HEALTH = "medical_health"
    NUTRITION_FOOD = "nutrition_food"
    EXERCISE_TRAINING = "exercise_training"
    BEHAVIOR_CHANGE = "behavior_change"
    SOFTWARE_SECURITY_REFERENCE = "software_security_reference"


class TrustedSourcePackV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    registry_version: str = SOURCE_REGISTRY_VERSION
    pack_id: TrustedSourcePackIdV1
    allowed_domains: tuple[str, ...]


_SOFTWARE_SECURITY_DOMAINS = (
    OPENAI_DEVELOPERS_DOMAIN,
    SUPABASE_DOMAIN,
    NEXTJS_DOMAIN,
    REACT_DOMAIN,
    POSTGRESQL_DOMAIN,
    QDRANT_DOMAIN,
    GITHUB_DOCS_DOMAIN,
    MDN_DOMAIN,
    OWASP_DOMAIN,
    NIST_DOMAIN,
    CISA_DOMAIN,
    IETF_DOMAIN,
    W3C_DOMAIN,
)

_PACKS = {
    TrustedSourcePackIdV1.GENERAL_CURRENT_NEWS: (
        APNEWS_DOMAIN,
        REUTERS_DOMAIN,
        NHK_DOMAIN,
    ),
    TrustedSourcePackIdV1.AI_TECH_CURRENT_NEWS: (
        OPENAI_DOMAIN,
        HUGGINGFACE_DOMAIN,
        APNEWS_DOMAIN,
        REUTERS_DOMAIN,
        ARSTECHNICA_DOMAIN,
        WIRED_DOMAIN,
        THEVERGE_DOMAIN,
        *_SOFTWARE_SECURITY_DOMAINS,
    ),
    TrustedSourcePackIdV1.MEDICAL_HEALTH: (
        PUBMED_DOMAIN,
        PMC_DOMAIN,
        CLINICAL_TRIALS_DOMAIN,
        FDA_DOMAIN,
        CDC_DOMAIN,
        NIH_DOMAIN,
        WHO_DOMAIN,
        COCHRANE_DOMAIN,
        MEDLINEPLUS_DOMAIN,
        ODS_DOMAIN,
    ),
    TrustedSourcePackIdV1.NUTRITION_FOOD: (
        FDC_DOMAIN,
        USDA_DOMAIN,
        DGA_DOMAIN,
        ODS_DOMAIN,
        PUBMED_DOMAIN,
        PMC_DOMAIN,
        ODPHP_DOMAIN,
        FDA_DOMAIN,
        REALFOOD_DOMAIN,
    ),
    TrustedSourcePackIdV1.EXERCISE_TRAINING: (
        PUBMED_DOMAIN,
        PMC_DOMAIN,
        ACSM_DOMAIN,
        NSCA_DOMAIN,
        ODPHP_DOMAIN,
    ),
    TrustedSourcePackIdV1.BEHAVIOR_CHANGE: (
        PUBMED_DOMAIN,
        PMC_DOMAIN,
    ),
    TrustedSourcePackIdV1.SOFTWARE_SECURITY_REFERENCE: (
        *_SOFTWARE_SECURITY_DOMAINS,
    ),
}


def trusted_source_pack_v1(
    pack_id: TrustedSourcePackIdV1,
    *,
    allow_bacb: bool = False,
) -> TrustedSourcePackV1:
    domains = list(_PACKS[pack_id])
    if pack_id == TrustedSourcePackIdV1.BEHAVIOR_CHANGE and allow_bacb:
        domains.append(BACB_DOMAIN)
    return TrustedSourcePackV1(
        pack_id=pack_id,
        allowed_domains=tuple(dict.fromkeys(domains)),
    )


ALL_REGISTERED_DOMAINS = frozenset(
    domain
    for domains in _PACKS.values()
    for domain in domains
) | {BACB_DOMAIN}


__all__ = [
    "ACSM_DOMAIN",
    "ALL_REGISTERED_DOMAINS",
    "APNEWS_DOMAIN",
    "ARSTECHNICA_DOMAIN",
    "BACB_DOMAIN",
    "CDC_DOMAIN",
    "CISA_DOMAIN",
    "CLINICAL_TRIALS_DOMAIN",
    "COCHRANE_DOMAIN",
    "DGA_DOMAIN",
    "FDA_DOMAIN",
    "FDC_DOMAIN",
    "GITHUB_DOCS_DOMAIN",
    "HUGGINGFACE_DOMAIN",
    "IETF_DOMAIN",
    "MDN_DOMAIN",
    "MEDLINEPLUS_DOMAIN",
    "NEXTJS_DOMAIN",
    "NHK_DOMAIN",
    "NIH_DOMAIN",
    "NIST_DOMAIN",
    "NSCA_DOMAIN",
    "ODS_DOMAIN",
    "ODPHP_DOMAIN",
    "OPENAI_DEVELOPERS_DOMAIN",
    "OPENAI_DOMAIN",
    "OWASP_DOMAIN",
    "PMC_DOMAIN",
    "POSTGRESQL_DOMAIN",
    "PUBMED_DOMAIN",
    "QDRANT_DOMAIN",
    "REACT_DOMAIN",
    "REALFOOD_DOMAIN",
    "REUTERS_DOMAIN",
    "SOURCE_REGISTRY_VERSION",
    "SUPABASE_DOMAIN",
    "THEVERGE_DOMAIN",
    "TrustedSourcePackIdV1",
    "TrustedSourcePackV1",
    "USDA_DOMAIN",
    "W3C_DOMAIN",
    "WHO_DOMAIN",
    "WIRED_DOMAIN",
    "trusted_source_pack_v1",
]
