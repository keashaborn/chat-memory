from __future__ import annotations

"""Server-authoritative search planning shared by text and voice."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict


SEARCH_PLAN_CONTRACT = "search_plan_v1"
SEARCH_DECISION_POLICY_VERSION = "search_decision_v1_4"

SearchDecision = Literal["no_search", "indexed", "live", "research"]
SearchPolicyPack = Literal[
    "none",
    "general",
    "current_news",
    "health",
    "nutrition",
    "exercise",
    "software_security",
    "legal_financial",
]
SearchRoute = Literal["normal_chat", "trusted_health", "current_news"]


class SearchBudgetV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_searches: int
    max_sources: int


class SearchPlanV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal[SEARCH_PLAN_CONTRACT] = SEARCH_PLAN_CONTRACT
    policy_version: Literal[SEARCH_DECISION_POLICY_VERSION] = (
        SEARCH_DECISION_POLICY_VERSION
    )
    decision: SearchDecision
    reason_codes: tuple[str, ...]
    policy_pack: SearchPolicyPack
    query_context: Literal["current_message_only"] = "current_message_only"
    external_web_access: bool
    confidence: Literal["high", "medium"]
    budget: SearchBudgetV1
    selected_route: SearchRoute


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value) for value in values)


NO_SEARCH_PATTERNS = _patterns(
    r"\bdo not (?:search|browse|look online|use the web)\b",
    r"\bdon't (?:search|browse|look online|use the web)\b",
    r"\bnever (?:search|browse|look online|use the web)\b",
    r"\bwithout (?:searching|browsing|web search|internet access)\b",
    r"\bno (?:web|internet|online) (?:access|search(?:ing)?|browsing)\b",
    r"\bno browsing\b",
    r"\boffline only\b",
    r"\bdo not (?:access|consult|use) (?:any )?external sources?\b",
    r"\b(?:answer|respond|work) (?:only )?from (?:your )?(?:existing|internal|offline) knowledge\b",
    r"\b(?:use|rely on) (?:only )?(?:your )?(?:existing|internal|offline) knowledge\b",
    r"\bweb off\b",
)
SPECIFIC_SOURCE_PATTERNS = _patterns(
    r"https?://\S+",
    r"\b(?:open|read|check|review|summarize)\s+(?:this|the)\s+(?:page|url|link|website)\b",
)
RESEARCH_PATTERNS = _patterns(
    r"\bdeep research\b",
    r"\bcomprehensive literature review\b",
    r"\bsystematic review\b",
    r"\binvestigate thoroughly\b",
    r"\bexhaustive research\b",
)
EXPLICIT_WEB_PATTERNS = _patterns(
    r"\bsearch the web\b",
    r"\bsearch online\b",
    r"\blook (?:it )?up online\b",
    r"\bbrowse the web\b",
    r"\bfind (?:online |web )?sources\b",
    r"\bcheck (?:online|the web)\b",
)
STRONG_FRESHNESS_PATTERNS = _patterns(
    r"\bwhat just happened\b",
    r"\bjust happened\b",
    r"\bbreaking (?:news|story|update)\b",
    r"\bcurrent news\b",
    r"\bnews (?:about|on|in|from)\b",
    r"\b(?:biggest|top|major)\s+(?:news|headline|story|stories)\b",
    r"\b(?:top|major)\s+headlines?\b",
    r"\bany (?:new|recent|current) (?:news|updates?)\b",
    r"\bwhat updates? (?:are there )?(?:about|on)\b",
    r"\bupdates? (?:about|on)\b",
    r"\bis (?:this|that) still (?:true|accurate|current)\b",
    r"\bup[- ]to[- ]date\b",
)
GENERAL_CURRENT_NEWS_PATTERNS = _patterns(
    r"\bnews (?:about|on|in|from)\b",
    r"\b(?:biggest|top|major)\s+(?:news|headline|story|stories)\b",
    r"\b(?:top|major)\s+headlines?\b",
)
WEAK_FRESHNESS_PATTERNS = _patterns(
    r"\blatest\b",
    r"\brecent\b",
    r"\bcurrent\b",
    r"\btoday\b",
    r"\byesterday\b",
    r"\bthis week\b",
    r"\bright now\b",
)
VOLATILE_FACT_PATTERNS = _patterns(
    r"\bnews\b",
    r"\bupdates?\b",
    r"\breleases?\b",
    r"\bversions?\b",
    r"\bprices?\b",
    r"\bcosts?\b",
    r"\binterest rates?\b",
    r"\bweather\b",
    r"\bforecasts?\b",
    r"\bschedules?\b",
    r"\bscores?\b",
    r"\bstandings?\b",
    r"\blaws?\b",
    r"\bregulations?\b",
    r"\bguidelines?\b",
    r"\bguidance\b",
    r"\brecommendations?\b",
    r"\bpolic(?:y|ies)\b",
    r"\bceo\b",
    r"\bpresident\b",
    r"\bsecurity incidents?\b",
    r"\bbreaches?\b",
    r"\bcve-\d{4}-\d+\b",
    r"\boutages?\b",
    r"\bservice status\b",
    r"\bavailability\b",
)
EVIDENCE_PATTERNS = _patterns(
    r"\bcite (?:a |your )?sources?\b",
    r"\bcite (?:studies|research|evidence|papers?)\b",
    r"\bwith citations?\b",
    r"\bprovide sources?\b",
    r"\bwhat (?:does|do) the evidence\b",
    r"\bevidence[- ]based\b",
    r"\bverify (?:this|that|the claim|whether)\b",
    r"\bfact[- ]check\b",
    r"\bis (?:this|that) true\b",
    r"\bpeer[- ]reviewed\b",
    r"\bwhat (?:does|do) the research\b",
    r"\bfind (?:a |the )?(?:study|studies|paper|papers)\b",
)
REFERENCE_LOOKUP_PATTERNS = _patterns(
    r"\bofficial (?:source|guidance|documentation|docs|recommendations?)\b",
    r"\bofficial\b[a-z0-9 ._-]{0,80}\b(?:guidance|documentation|docs|recommendations?)\b",
    r"\b(?:documentation|docs|specification|standard|advisory)\b",
    r"\b(?:guideline|guidelines)\b",
)
TRANSFORM_PATTERNS = _patterns(
    r"\b(?:summarize|rewrite|edit|translate|proofread|reformat)\b[\s\S]*\b(?:the following|this text|below|above|provided|attached)\b",
    r"\b(?:the following|this text|text below|text above)\b[\s\S]*\b(?:summarize|rewrite|edit|translate|proofread|reformat)\b",
    r"\b(?:summarize|rewrite|edit|translate|proofread|reformat)\s+(?:this|the)\s+(?:text|passage|paragraph|email|message|draft|content)\b",
)
INTERNAL_CONTEXT_PATTERNS = _patterns(
    r"\bwhat did i (?:say|tell you|ask)\b",
    r"\b(?:use|based on|according to|recall) (?:only )?what i (?:said|told|shared|wrote)\b",
    r"\bearlier in (?:this|our) (?:chat|conversation|thread)\b",
    r"\b(?:my|our) (?:previous )?(?:message|messages|conversation|thread|notes|memory|memories)\b",
    r"\b(?:my|our) (?:nutrition|workout|training|health|meal|exercise|project) (?:plan|plans|history|record|records|goals?|data)\b",
    r"\bfrom (?:my|our) (?:records|memory|conversation|thread|notes)\b",
)
HEALTH_TOPIC_PATTERNS = _patterns(
    r"\bhealth\b",
    r"\bmedical\b",
    r"\bmedicine\b",
    r"\bpublic health\b",
    r"\bdiseases?\b",
    r"\binfections?\b",
    r"\bvaccines?\b",
    r"\bmedications?\b",
    r"\bdrugs?\b",
    r"\bdos(?:e|age|ing)\b",
    r"\bside effects?\b",
    r"\binteractions?\b",
    r"\bcontraindications?\b",
    r"\bsymptoms?\b",
    r"\bdiagnos(?:is|e|tic)\b",
    r"\btreatments?\b",
    r"\bclinical\b",
    r"\bcreatine\b",
    r"\bsupplements?\b",
    r"\bvitamins?\b",
    r"\bminerals?\b",
    r"\bnutrition\b",
    r"\bpregnan(?:t|cy)\b",
    r"\bkidney\b",
    r"\bliver\b",
    r"\bheart\b",
    r"\bblood pressure\b",
)
NUTRITION_TOPIC_PATTERNS = _patterns(
    r"\bnutrition\b",
    r"\bdiet(?:ary)?\b",
    r"\bfood composition\b",
    r"\bprotein intake\b",
    r"\benergy balance\b",
    r"\bcalorie deficit\b",
    r"\bmeal timing\b",
    r"\bnutrient timing\b",
)
EXERCISE_TOPIC_PATTERNS = _patterns(
    r"\bexercise\b",
    r"\btraining\b",
    r"\bweightlifting\b",
    r"\bweight lifting\b",
    r"\bresistance training\b",
    r"\bstrength training\b",
    r"\bhypertrophy\b",
    r"\btraining volume\b",
    r"\btraining frequency\b",
)
HEALTH_RISK_PATTERNS = _patterns(
    r"\bis (?:it|this|that) safe\b",
    r"\bis [a-z0-9 ,'-]{1,80} safe\b",
    r"\bshould i (?:take|stop|start|use)\b",
    r"\bhow much should i (?:take|use)\b",
    r"\bwhat (?:dose|dosage)\b",
    r"\bside effects?\b",
    r"\binteractions?\b",
    r"\bcontraindications?\b",
    r"\bdiagnos(?:is|e)\b",
    r"\btreatments?\b",
)
SOFTWARE_SECURITY_PATTERNS = _patterns(
    r"\bopenai\b",
    r"\bsupabase\b",
    r"\bnext\.?js\b",
    r"\breact\b",
    r"\bpostgres(?:ql)?\b",
    r"\bqdrant\b",
    r"\bapi\b",
    r"\bsoftware\b",
    r"\bsecurity\b",
    r"\bvulnerabilit(?:y|ies)\b",
    r"\bbreaches?\b",
    r"\bcve-\d{4}-\d+\b",
)
LEGAL_FINANCIAL_PATTERNS = _patterns(
    r"\blaws?\b",
    r"\blegal\b",
    r"\bregulations?\b",
    r"\btax(?:es)?\b",
    r"\bcompliance\b",
    r"\bstocks?\b",
    r"\bsecurities\b",
    r"\binterest rates?\b",
    r"\bexchange rates?\b",
    r"\bfinancial\b",
)
TRUSTED_CURRENT_NEWS_ENTITY_PATTERNS = _patterns(
    r"\bopenai\b",
    r"\bopen eye\b",
    r"\bchatgpt\b",
    r"\bhugging\s*face\b",
    r"\bhuggingface\b",
)
SECURITY_RISK_PATTERNS = _patterns(
    r"\bsecurity\b",
    r"\bvulnerabilit(?:y|ies)\b",
    r"\bbreaches?\b",
    r"\bcve-\d{4}-\d+\b",
)


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower().replace("’", "'").replace("‘", "'")).strip()


def _matches(value: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(value) is not None for pattern in patterns)


def _budget(decision: SearchDecision) -> SearchBudgetV1:
    if decision == "research":
        return SearchBudgetV1(max_searches=12, max_sources=30)
    if decision == "live":
        return SearchBudgetV1(max_searches=4, max_sources=10)
    if decision == "indexed":
        return SearchBudgetV1(max_searches=2, max_sources=5)
    return SearchBudgetV1(max_searches=0, max_sources=0)


def _policy_pack(value: str) -> SearchPolicyPack:
    if _matches(value, TRUSTED_CURRENT_NEWS_ENTITY_PATTERNS):
        return "software_security"
    if _matches(value, NUTRITION_TOPIC_PATTERNS):
        return "nutrition"
    if _matches(value, EXERCISE_TOPIC_PATTERNS):
        return "exercise"
    if _matches(value, HEALTH_TOPIC_PATTERNS):
        return "health"
    if _matches(value, SOFTWARE_SECURITY_PATTERNS):
        return "software_security"
    if _matches(value, GENERAL_CURRENT_NEWS_PATTERNS):
        return "current_news"
    if _matches(value, LEGAL_FINANCIAL_PATTERNS):
        return "legal_financial"
    return "general"


def _route(
    decision: SearchDecision,
    reasons: tuple[str, ...],
    policy_pack: SearchPolicyPack,
) -> SearchRoute:
    if decision == "no_search":
        return "normal_chat"
    if decision == "research" or "specific_source_requested" in reasons:
        return "normal_chat"
    if policy_pack in {"health", "nutrition", "exercise"}:
        return "trusted_health"
    if (
        "trusted_current_news_scope" in reasons
        and (
            "freshness_required" in reasons
            or "explicit_web_request" in reasons
        )
    ):
        return "current_news"
    if (
        policy_pack in {"current_news", "software_security"}
        and "freshness_required" in reasons
    ):
        return "current_news"
    if policy_pack == "software_security" and any(
        reason in reasons
        for reason in (
            "explicit_web_request",
            "evidence_requested",
            "high_stakes_verification",
        )
    ):
        return "trusted_health"
    return "normal_chat"


def _plan(
    decision: SearchDecision,
    reasons: tuple[str, ...],
    policy_pack: SearchPolicyPack,
    confidence: Literal["high", "medium"],
) -> SearchPlanV1:
    unique_reasons = tuple(dict.fromkeys(reasons))
    effective_pack: SearchPolicyPack = (
        "none" if decision == "no_search" else policy_pack
    )
    return SearchPlanV1(
        decision=decision,
        reason_codes=unique_reasons,
        policy_pack=effective_pack,
        external_web_access=decision in {"live", "research"},
        confidence=confidence,
        budget=_budget(decision),
        selected_route=_route(decision, unique_reasons, effective_pack),
    )


def create_search_plan_v1(query: str) -> SearchPlanV1:
    value = _normalized(query)
    if not value:
        return _plan("no_search", ("stable_knowledge_default",), "none", "high")
    if _matches(value, NO_SEARCH_PATTERNS):
        return _plan(
            "no_search", ("search_prohibited_by_user",), "none", "high"
        )

    pack = _policy_pack(value)
    trusted_current_news = _matches(
        value, TRUSTED_CURRENT_NEWS_ENTITY_PATTERNS
    )
    general_current_news = _matches(
        value, GENERAL_CURRENT_NEWS_PATTERNS
    )
    if _matches(value, SPECIFIC_SOURCE_PATTERNS):
        return _plan(
            "live", ("specific_source_requested",), pack, "high"
        )
    if _matches(value, RESEARCH_PATTERNS):
        return _plan("research", ("explicit_research",), pack, "high")
    if _matches(value, TRANSFORM_PATTERNS):
        return _plan("no_search", ("user_content_transform",), "none", "high")
    if _matches(value, INTERNAL_CONTEXT_PATTERNS):
        return _plan(
            "no_search", ("internal_context_sufficient",), "none", "high"
        )

    explicit_web = _matches(value, EXPLICIT_WEB_PATTERNS)
    strong_freshness = _matches(value, STRONG_FRESHNESS_PATTERNS)
    weak_freshness = _matches(value, WEAK_FRESHNESS_PATTERNS) and (
        _matches(value, VOLATILE_FACT_PATTERNS) or trusted_current_news
    )
    if strong_freshness or weak_freshness:
        reasons = ["freshness_required"]
        if explicit_web:
            reasons.append("explicit_web_request")
        if trusted_current_news:
            reasons.append("trusted_current_news_scope")
        elif general_current_news and pack == "current_news":
            reasons.append("general_current_news_scope")
        return _plan(
            "live",
            tuple(reasons),
            pack,
            "high" if strong_freshness else "medium",
        )
    if explicit_web:
        return _plan(
            (
                "live"
                if pack
                in {
                    "current_news",
                    "health",
                    "nutrition",
                    "exercise",
                    "software_security",
                }
                else "indexed"
            ),
            ("explicit_web_request",),
            pack,
            "high",
        )

    evidence = _matches(value, EVIDENCE_PATTERNS)
    reference_lookup = _matches(value, REFERENCE_LOOKUP_PATTERNS)
    if reference_lookup and pack in {
        "health",
        "nutrition",
        "exercise",
        "software_security",
    }:
        return _plan(
            "live",
            ("evidence_requested",),
            pack,
            "high",
        )
    high_stakes_health = _matches(
        value, HEALTH_TOPIC_PATTERNS
    ) and _matches(value, HEALTH_RISK_PATTERNS)
    high_stakes_other = _matches(
        value, LEGAL_FINANCIAL_PATTERNS
    ) or (
        _matches(value, SOFTWARE_SECURITY_PATTERNS)
        and _matches(value, SECURITY_RISK_PATTERNS)
    )
    if evidence or high_stakes_health or high_stakes_other:
        reasons = []
        if evidence:
            reasons.append("evidence_requested")
        if high_stakes_health or high_stakes_other:
            reasons.append("high_stakes_verification")
        return _plan(
            "indexed",
            tuple(reasons),
            pack,
            "high" if evidence else "medium",
        )
    return _plan(
        "no_search", ("stable_knowledge_default",), "none", "medium"
    )


__all__ = [
    "SEARCH_DECISION_POLICY_VERSION",
    "SEARCH_PLAN_CONTRACT",
    "SearchPlanV1",
    "create_search_plan_v1",
]
