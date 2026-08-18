from __future__ import annotations

"""Deterministic, server-owned routing and URL policy for trusted web search."""

import ipaddress
import re
from enum import Enum
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from rag_engine.trusted_source_registry_v1 import (
    ALL_REGISTERED_DOMAINS,
    APNEWS_DOMAIN,
    ARSTECHNICA_DOMAIN,
    BACB_DOMAIN,
    DGA_DOMAIN,
    FDA_DOMAIN,
    HUGGINGFACE_DOMAIN,
    MEDLINEPLUS_DOMAIN,
    NHK_DOMAIN,
    ODS_DOMAIN,
    ODPHP_DOMAIN,
    OPENAI_DOMAIN,
    PMC_DOMAIN,
    PUBMED_DOMAIN,
    REALFOOD_DOMAIN,
    REUTERS_DOMAIN,
    THEVERGE_DOMAIN,
    TrustedSourcePackIdV1,
    WIRED_DOMAIN,
    trusted_source_pack_v1,
)

POLICY_VERSION = "trusted_web_policy_v1_4"

CORE_ALLOWED_DOMAINS = ALL_REGISTERED_DOMAINS

CURRENT_NEWS_ALLOWED_DOMAINS = trusted_source_pack_v1(
    TrustedSourcePackIdV1.AI_TECH_CURRENT_NEWS
).allowed_domains
GENERAL_CURRENT_NEWS_ALLOWED_DOMAINS = trusted_source_pack_v1(
    TrustedSourcePackIdV1.GENERAL_CURRENT_NEWS
).allowed_domains
MEDICAL_HEALTH_ALLOWED_DOMAINS = trusted_source_pack_v1(
    TrustedSourcePackIdV1.MEDICAL_HEALTH
).allowed_domains
NUTRITION_FOOD_ALLOWED_DOMAINS = trusted_source_pack_v1(
    TrustedSourcePackIdV1.NUTRITION_FOOD
).allowed_domains
EXERCISE_TRAINING_ALLOWED_DOMAINS = trusted_source_pack_v1(
    TrustedSourcePackIdV1.EXERCISE_TRAINING
).allowed_domains
SOFTWARE_SECURITY_ALLOWED_DOMAINS = trusted_source_pack_v1(
    TrustedSourcePackIdV1.SOFTWARE_SECURITY_REFERENCE
).allowed_domains


class TrustedWebTopicV1(str, Enum):
    SUPPLEMENTS = "supplements"
    MEDICAL_ADJACENT = "medical_adjacent"
    NUTRITION_EVIDENCE = "nutrition_evidence"
    TRAINING_EVIDENCE = "training_evidence"
    BEHAVIOR_CHANGE = "behavior_change"
    USDA_FOOD_COMPOSITION = "usda_food_composition"
    INTERNAL_EXERCISE_LIBRARY = "internal_exercise_library"
    SAFETY_STOP = "safety_stop"
    CURRENT_NEWS = "current_news"
    MEDICAL_CURRENT_NEWS = "medical_current_news"
    NUTRITION_REFERENCE = "nutrition_reference"
    EXERCISE_REFERENCE = "exercise_reference"
    SOFTWARE_SECURITY_REFERENCE = "software_security_reference"
    UNSUPPORTED = "unsupported"


class TrustedWebDispositionV1(str, Enum):
    SEARCH = "search"
    DECLINE = "decline"
    ROUTE_INTERNAL = "route_internal"
    SAFETY_STOP = "safety_stop"


class TrustedWebPolicyDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_version: str = POLICY_VERSION
    topic: TrustedWebTopicV1
    disposition: TrustedWebDispositionV1
    reason: str
    allowed_domains: tuple[str, ...] = ()


_URL_RE = re.compile(r"https?://[^\s<>{}\[\]\"']+", re.IGNORECASE)

_NO_SEARCH_RE = re.compile(
    r"(?:"
    r"\bdo not (?:search|browse|look online|use the web)\b"
    r"|\bdon't (?:search|browse|look online|use the web)\b"
    r"|\bnever (?:search|browse|look online|use the web)\b"
    r"|\bno (?:web|internet|online) (?:access|search(?:ing)?|browsing)\b"
    r"|\boffline only\b"
    r"|\bdo not (?:access|consult|use) (?:any )?external sources?\b"
    r")",
    re.IGNORECASE,
)

_SAFETY_TERMS = (
    "suicidal",
    "suicide",
    "self harm",
    "self-harm",
    "eating disorder",
    "anorexia",
    "bulimia",
    "purging",
    "vomiting after eating",
    "compulsive exercise",
    "can't stop exercise",
    "cannot stop exercise",
    "can't stop exercising",
    "cannot stop exercising",
    "exercise addiction",
    "chest pain",
    "cannot breathe",
    "can't breathe",
    "severe shortness of breath",
    "fainted",
    "fainting",
    "severe bleeding",
)

_SUPPLEMENT_TERMS = (
    "supplement",
    "creatine",
    "caffeine",
    "beta alanine",
    "beta-alanine",
    "citrulline",
    "fish oil",
    "omega-3",
    "multivitamin",
    "vitamin",
    "mineral",
    "magnesium",
    "zinc",
    "pre workout",
    "pre-workout",
    "ergogenic",
    "worth it",
    "worthwhile",
    "gummy",
    "gummies",
    "creatine monohydrate",
    "monohydrate",
)

_BEHAVIOR_TERMS = (
    "baseline phase",
    "change phase",
    "self experiment",
    "self-experiment",
    "single case",
    "single-case",
    "self monitoring",
    "self-monitoring",
    "adherence",
    "habit tracking",
    "one change at a time",
    "planned sets completed",
)

_MEDICAL_ADJACENT_TERMS = (
    "contraindication",
    "interaction",
    "side effect",
    "medication",
    "prescription",
    "medical condition",
    "pregnant",
    "pregnancy",
    "breastfeeding",
    "allergy",
    "symptom",
    "blood pressure",
    "kidney disease",
    "liver disease",
)

_USDA_TERMS = (
    "fooddata central",
    "fooddata",
    "barcode",
    "nutrition facts",
    "food composition",
    "calories in",
    "macros in",
    "micronutrients in",
    "nutrients in",
)

_TECHNIQUE_TERMS = (
    "exercise technique",
    "lifting technique",
    "form check",
    "how to squat",
    "how to bench",
    "how to deadlift",
    "how to perform",
    "exercise cues",
)

_NUTRITION_EVIDENCE_TERMS = (
    "nutrition",
    "diet",
    "protein target",
    "protein intake",
    "energy balance",
    "calorie deficit",
    "fat loss",
    "weight loss",
    "dietary pattern",
    "dietary guidelines",
    "meal timing",
    "nutrient timing",
)

_CURRENT_NEWS_ENTITY_TERMS = (
    "openai",
    "hugging face",
    "huggingface",
    "anthropic",
    "google deepmind",
    "deepmind",
    "nvidia",
    "meta ai",
    "mistral",
    "usda",
    "fda",
    "ftc",
    "nist",
    "supabase",
    "next.js",
    "nextjs",
    "react",
    "postgres",
    "postgresql",
    "qdrant",
    "github",
    "mozilla",
    "owasp",
    "cisa",
)

_SOFTWARE_CURRENT_NEWS_ENTITY_TERMS = (
    "openai",
    "hugging face",
    "huggingface",
    "anthropic",
    "google deepmind",
    "deepmind",
    "nvidia",
    "meta ai",
    "mistral",
    "supabase",
    "next.js",
    "nextjs",
    "react",
    "postgres",
    "postgresql",
    "qdrant",
    "github",
    "mozilla",
    "owasp",
    "nist",
    "cisa",
)

_CURRENT_NEWS_INTENT_TERMS = (
    "latest",
    "recent",
    "current",
    "news",
    "just happened",
    "what happened",
    "what's going on with",
    "whats going on with",
    "what is going on with",
    "what's happening with",
    "whats happening with",
    "what is happening with",
    "what's the situation with",
    "whats the situation with",
    "what is the situation with",
    "what's up with",
    "whats up with",
    "what is up with",
    "what's new with",
    "whats new with",
    "what is new with",
    "any updates on",
    "updates on",
    "is this still true",
    "is that still true",
    "still accurate",
    "still current",
    "announced",
    "announcement",
    "this week",
    "today",
    "yesterday",
    "breaking",
)

_GENERAL_CURRENT_NEWS_RE = re.compile(
    r"(?:"
    r"\bnews\s+(?:about|on|in|from)\b"
    r"|\b(?:biggest|top|major)\s+(?:news|headline|story|stories)\b"
    r"|\b(?:top|major)\s+headlines?\b"
    r")",
    re.IGNORECASE,
)

_GENERAL_CURRENT_NEWS_EXCLUDED_TERMS = (
    "medication",
    "drug",
    "dose",
    "dosage",
    "side effect",
    "interaction",
    "contraindication",
    "symptom",
    "diagnosis",
    "treatment",
    "clinical",
    "creatine",
    "supplement",
    "vitamin",
    "mineral",
    "nutrition",
    "pregnant",
    "pregnancy",
    "kidney",
    "liver",
    "heart",
    "blood pressure",
)

_BLOCKED_NEWS_SOURCE_TERMS = (
    "reddit",
    "twitter",
    "x.com",
    "social media",
    "forum",
    "gossip",
    "rumor",
    "rumour",
)

_TRAINING_EVIDENCE_TERMS = (
    "hypertrophy",
    "muscle growth",
    "strength training",
    "resistance training",
    "weightlifting",
    "weight lifting",
    "bodybuilding",
    "physique",
    "training volume",
    "training frequency",
    "repetitions in reserve",
    "rir",
    "estimated 1rm",
    "one rep max",
    "progressive overload",
)

_MEDICAL_EVIDENCE_TERMS = (
    "health",
    "medical",
    "medicine",
    "public health",
    "health research",
    "clinical trial",
    "clinical trials",
    "disease",
    "infection",
    "vaccine",
    "vaccines",
)

_SOFTWARE_SECURITY_REFERENCE_TERMS = (
    "openai",
    "supabase",
    "next.js",
    "nextjs",
    "react",
    "postgres",
    "postgresql",
    "qdrant",
    "github",
    "mozilla",
    "mdn",
    "owasp",
    "nist",
    "cisa",
    "ietf",
    "w3c",
    "api",
    "software",
    "cybersecurity",
    "security",
    "vulnerability",
    "vulnerabilities",
    "cve-",
)

_REFERENCE_INTENT_TERMS = (
    "documentation",
    "docs",
    "official source",
    "official guidance",
    "guideline",
    "guidelines",
    "recommendation",
    "recommendations",
    "specification",
    "standard",
    "advisory",
    "cite",
    "citation",
    "citations",
    "evidence",
    "research",
    "verify",
    "fact check",
    "fact-check",
    "search",
    "look up",
    "check the web",
)

_OFFICIAL_REFERENCE_INTENT_TERMS = (
    "documentation",
    "docs",
    "official source",
    "official guidance",
    "guideline",
    "guidelines",
    "recommendation",
    "recommendations",
    "specification",
    "standard",
    "advisory",
    "search",
    "look up",
    "check the web",
)

_REFERENCE_OVER_NEWS_TERMS = (
    "documentation",
    "docs",
    "official source",
    "official guidance",
    "guideline",
    "guidelines",
    "recommendation",
    "recommendations",
    "specification",
    "standard",
    "advisory",
)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _canonical_host(host: str) -> str:
    value = str(host or "").strip().lower().rstrip(".")
    if not value or len(value) > 253:
        raise ValueError("invalid_source_host")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("non_ascii_source_host") from None
    if value == "localhost" or value.endswith(".localhost"):
        raise ValueError("local_source_host")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("ip_literal_source_host")


def host_is_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    canonical = _canonical_host(host)
    for allowed in allowed_domains:
        domain = _canonical_host(allowed)
        if canonical == domain or canonical.endswith(f".{domain}"):
            return True
    return False


def validate_allowed_source_url(url: str, allowed_domains: tuple[str, ...]) -> str:
    """Return a canonical HTTPS URL or fail closed."""

    raw = str(url or "").strip()
    if not raw or len(raw) > 4096:
        raise ValueError("invalid_source_url")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https":
        raise ValueError("source_scheme_not_https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source_userinfo_forbidden")
    try:
        if parsed.port is not None:
            raise ValueError("source_port_forbidden")
    except ValueError as exc:
        if str(exc) == "source_port_forbidden":
            raise
        raise ValueError("invalid_source_port") from None
    host = _canonical_host(parsed.hostname or "")
    if not host_is_allowed(host, allowed_domains):
        raise ValueError("source_domain_not_allowed")
    if not parsed.path.startswith("/"):
        raise ValueError("invalid_source_path")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _query_contains_forbidden_url(query: str) -> bool:
    for match in _URL_RE.findall(query):
        candidate = match.rstrip(".,;:!?)")
        try:
            parsed = urlsplit(candidate)
            host = _canonical_host(parsed.hostname or "")
            if parsed.scheme.lower() != "https":
                return True
            if not host_is_allowed(host, tuple(sorted(CORE_ALLOWED_DOMAINS | {BACB_DOMAIN}))):
                return True
        except ValueError:
            return True
    return False


def route_trusted_web_query(
    query: str,
    *,
    allow_bacb: bool = False,
) -> TrustedWebPolicyDecisionV1:
    """Conservatively route only the product's approved research topics."""

    normalized = " ".join(str(query or "").lower().split())
    if _query_contains_forbidden_url(normalized):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.UNSUPPORTED,
            disposition=TrustedWebDispositionV1.DECLINE,
            reason="unapproved_url_target",
        )
    if _contains_any(normalized, _BLOCKED_NEWS_SOURCE_TERMS):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.UNSUPPORTED,
            disposition=TrustedWebDispositionV1.DECLINE,
            reason="unapproved_news_source",
        )
    if _NO_SEARCH_RE.search(normalized):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.UNSUPPORTED,
            disposition=TrustedWebDispositionV1.DECLINE,
            reason="search_prohibited_by_user",
        )
    if _contains_any(normalized, _SAFETY_TERMS):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.SAFETY_STOP,
            disposition=TrustedWebDispositionV1.SAFETY_STOP,
            reason="safety_signal",
        )
    if (
        _contains_any(normalized, _NUTRITION_EVIDENCE_TERMS)
        and _contains_any(
            normalized,
            _REFERENCE_OVER_NEWS_TERMS,
        )
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.NUTRITION_REFERENCE,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_nutrition_reference",
            allowed_domains=NUTRITION_FOOD_ALLOWED_DOMAINS,
        )
    if (
        _contains_any(normalized, _TRAINING_EVIDENCE_TERMS)
        and _contains_any(
            normalized,
            _REFERENCE_OVER_NEWS_TERMS,
        )
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.EXERCISE_REFERENCE,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_exercise_reference",
            allowed_domains=EXERCISE_TRAINING_ALLOWED_DOMAINS,
        )
    if (
        _contains_any(normalized, _MEDICAL_EVIDENCE_TERMS)
        and _contains_any(normalized, _REFERENCE_OVER_NEWS_TERMS)
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.MEDICAL_ADJACENT,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_medical_evidence",
            allowed_domains=MEDICAL_HEALTH_ALLOWED_DOMAINS,
        )
    if (
        _contains_any(normalized, _SOFTWARE_SECURITY_REFERENCE_TERMS)
        and _contains_any(normalized, _REFERENCE_OVER_NEWS_TERMS)
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.SOFTWARE_SECURITY_REFERENCE,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_software_security_reference",
            allowed_domains=SOFTWARE_SECURITY_ALLOWED_DOMAINS,
        )
    if (
        _contains_any(normalized, _CURRENT_NEWS_INTENT_TERMS)
        and _contains_any(
            normalized,
            _SOFTWARE_CURRENT_NEWS_ENTITY_TERMS,
        )
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.CURRENT_NEWS,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_current_news_lookup",
            allowed_domains=CURRENT_NEWS_ALLOWED_DOMAINS,
        )
    if (
        _contains_any(normalized, _CURRENT_NEWS_INTENT_TERMS)
        and _contains_any(normalized, _MEDICAL_EVIDENCE_TERMS)
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.MEDICAL_CURRENT_NEWS,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_medical_current_news_lookup",
            allowed_domains=MEDICAL_HEALTH_ALLOWED_DOMAINS,
        )
    if _contains_any(normalized, _CURRENT_NEWS_INTENT_TERMS) and _contains_any(
        normalized,
        _CURRENT_NEWS_ENTITY_TERMS,
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.CURRENT_NEWS,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_current_news_lookup",
            allowed_domains=CURRENT_NEWS_ALLOWED_DOMAINS,
        )
    if _contains_any(normalized, _SUPPLEMENT_TERMS):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.SUPPLEMENTS,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_supplement_evidence",
            allowed_domains=MEDICAL_HEALTH_ALLOWED_DOMAINS,
        )
    if _contains_any(normalized, _BEHAVIOR_TERMS):
        domains = trusted_source_pack_v1(
            TrustedSourcePackIdV1.BEHAVIOR_CHANGE,
            allow_bacb=allow_bacb,
        ).allowed_domains
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.BEHAVIOR_CHANGE,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_self_experimentation_evidence",
            allowed_domains=domains,
        )
    if _contains_any(normalized, _MEDICAL_ADJACENT_TERMS):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.MEDICAL_ADJACENT,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_plain_language_safety",
            allowed_domains=MEDICAL_HEALTH_ALLOWED_DOMAINS,
        )
    if _contains_any(normalized, _USDA_TERMS):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.USDA_FOOD_COMPOSITION,
            disposition=TrustedWebDispositionV1.ROUTE_INTERNAL,
            reason="use_existing_usda_integration",
        )
    if _contains_any(normalized, _TECHNIQUE_TERMS):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.INTERNAL_EXERCISE_LIBRARY,
            disposition=TrustedWebDispositionV1.ROUTE_INTERNAL,
            reason="use_internal_exercise_library",
        )
    if (
        _contains_any(normalized, _NUTRITION_EVIDENCE_TERMS)
        and _contains_any(
            normalized,
            _OFFICIAL_REFERENCE_INTENT_TERMS,
        )
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.NUTRITION_REFERENCE,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_nutrition_reference",
            allowed_domains=NUTRITION_FOOD_ALLOWED_DOMAINS,
        )
    if (
        _contains_any(normalized, _TRAINING_EVIDENCE_TERMS)
        and _contains_any(
            normalized,
            _OFFICIAL_REFERENCE_INTENT_TERMS,
        )
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.EXERCISE_REFERENCE,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_exercise_reference",
            allowed_domains=EXERCISE_TRAINING_ALLOWED_DOMAINS,
        )
    if (
        _contains_any(normalized, _MEDICAL_EVIDENCE_TERMS)
        and _contains_any(normalized, _REFERENCE_INTENT_TERMS)
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.MEDICAL_ADJACENT,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_medical_evidence",
            allowed_domains=MEDICAL_HEALTH_ALLOWED_DOMAINS,
        )
    if (
        _contains_any(normalized, _SOFTWARE_SECURITY_REFERENCE_TERMS)
        and _contains_any(normalized, _REFERENCE_INTENT_TERMS)
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.SOFTWARE_SECURITY_REFERENCE,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_software_security_reference",
            allowed_domains=SOFTWARE_SECURITY_ALLOWED_DOMAINS,
        )
    if _contains_any(normalized, _NUTRITION_EVIDENCE_TERMS):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.NUTRITION_EVIDENCE,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_nutrition_evidence",
            allowed_domains=NUTRITION_FOOD_ALLOWED_DOMAINS,
        )
    if _contains_any(normalized, _TRAINING_EVIDENCE_TERMS):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.TRAINING_EVIDENCE,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_training_evidence",
            allowed_domains=EXERCISE_TRAINING_ALLOWED_DOMAINS,
        )
    if (
        _GENERAL_CURRENT_NEWS_RE.search(normalized)
        and not _contains_any(
            normalized,
            _GENERAL_CURRENT_NEWS_EXCLUDED_TERMS,
        )
    ):
        return TrustedWebPolicyDecisionV1(
            topic=TrustedWebTopicV1.CURRENT_NEWS,
            disposition=TrustedWebDispositionV1.SEARCH,
            reason="approved_general_current_news_lookup",
            allowed_domains=GENERAL_CURRENT_NEWS_ALLOWED_DOMAINS,
        )
    return TrustedWebPolicyDecisionV1(
        topic=TrustedWebTopicV1.UNSUPPORTED,
        disposition=TrustedWebDispositionV1.DECLINE,
        reason="topic_outside_trusted_web_scope",
    )


__all__ = [
    "BACB_DOMAIN",
    "FDA_DOMAIN",
    "ODPHP_DOMAIN",
    "REALFOOD_DOMAIN",
    "APNEWS_DOMAIN",
    "ARSTECHNICA_DOMAIN",
    "CORE_ALLOWED_DOMAINS",
    "CURRENT_NEWS_ALLOWED_DOMAINS",
    "GENERAL_CURRENT_NEWS_ALLOWED_DOMAINS",
    "EXERCISE_TRAINING_ALLOWED_DOMAINS",
    "HUGGINGFACE_DOMAIN",
    "MEDICAL_HEALTH_ALLOWED_DOMAINS",
    "NHK_DOMAIN",
    "NUTRITION_FOOD_ALLOWED_DOMAINS",
    "OPENAI_DOMAIN",
    "REUTERS_DOMAIN",
    "SOFTWARE_SECURITY_ALLOWED_DOMAINS",
    "THEVERGE_DOMAIN",
    "WIRED_DOMAIN",
    "POLICY_VERSION",
    "TrustedWebDispositionV1",
    "TrustedWebPolicyDecisionV1",
    "TrustedWebTopicV1",
    "host_is_allowed",
    "route_trusted_web_query",
    "validate_allowed_source_url",
]
