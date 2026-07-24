from __future__ import annotations

"""Deterministic PubMed discovery through NCBI E-utilities."""

import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from rag_engine.trusted_web_policy_v1 import PUBMED_DOMAIN
_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_RESEARCH_TOPIC_VALUES = frozenset(
    {"supplements", "nutrition_evidence", "training_evidence", "behavior_change"}
)


class NCBIClientError(RuntimeError):
    pass


class NCBIResearchRecordV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pmid: str = Field(pattern=r"^[0-9]{1,16}$")
    title: str = Field(min_length=1, max_length=600)
    journal: str = Field(default="", max_length=300)
    publication_date: str = Field(default="", max_length=80)
    publication_types: tuple[str, ...] = Field(default=())
    abstract: str = Field(default="", max_length=6000)

    @property
    def url(self) -> str:
        return f"https://{PUBMED_DOMAIN}/{self.pmid}/"

    @property
    def citation_marker(self) -> str:
        return f"[PMID:{self.pmid}]"

    @property
    def source_id(self) -> str:
        return f"PMID:{self.pmid}"


def trusted_web_topic_uses_ncbi(topic: object) -> bool:
    return getattr(topic, "value", str(topic)) in _RESEARCH_TOPIC_VALUES


def classify_publication_types(publication_types: Iterable[str]) -> str:
    joined = " ".join(t.lower() for t in publication_types)
    if "meta-analysis" in joined:
        return "meta_analysis"
    if "systematic review" in joined:
        return "systematic_review"
    if "randomized controlled trial" in joined:
        return "randomized_controlled_trial"
    if "clinical trial" in joined:
        return "clinical_trial"
    if "review" in joined:
        return "review"
    return "pubmed_record"


_PUBMED_STOPWORDS = frozenset(
    {
        "about",
        "before",
        "adult",
        "adults",
        "after",
        "also",
        "and",
        "any",
        "are",
        "can",
        "champs",
        "calories",
        "cal",
        "cause",
        "cite",
        "does",
        "effect",
        "evidence",
        "for",
        "from",
        "have",
        "how",
        "improve",
        "improves",
        "into",
        "lift",
        "lifting",
        "people",
        "nutra",
        "please",
        "product",
        "reasonably",
        "show",
        "say",
        "taking",
        "that",
        "the",
        "there",
        "their",
        "this",
        "weight",
        "worth",
        "weights",
        "what",
        "when",
        "worthwhile",
        "whether",
        "with",
        "who",
    }
)
_PUBMED_SYNONYMS = {
    "caffeine": ("caffeine", "exercise performance"),
    "beta-alanine": ("beta alanine", "exercise performance", "paresthesia"),
    "alanine": ("beta alanine", "exercise performance", "paresthesia"),
    "tingling": ("paresthesia", "adverse effects"),
    "volume": ("training volume", "resistance training", "muscle hypertrophy"),
    "contraindication": ("safety", "kidney disease", "renal function", "adverse effects"),
    "contraindications": ("safety", "kidney disease", "renal function", "adverse effects"),
    "caution": ("safety", "kidney disease", "renal function", "adverse effects"),
    "cautious": ("safety", "kidney disease", "renal function", "adverse effects"),
    "kidney": ("kidney disease", "renal function"),
    "kidneys": ("kidney disease", "renal function"),
    "renal": ("renal function",),
    "safety": ("safety", "adverse effects"),
    "safe": ("safety", "adverse effects"),
    "side": ("adverse effects",),
    "effects": ("adverse effects",),
    "hypertrophy": ("muscle hypertrophy", "resistance training"),
    "muscle": ("muscle strength",),
    "strength": ("muscle strength", "resistance training"),
    "creatine": ("creatine", "creatine supplementation"),
    "protein": ("dietary protein",),
}


def _normalize_pubmed_query(query: str) -> str:
    raw_text = " ".join(str(query or "").lower().split())
    if "monohydrate" in raw_text and "creatine" not in raw_text:
        raw_text = "creatine " + raw_text
    if "creatine" in raw_text and any(term in raw_text for term in ("gummy", "gummies", "worth", "worthwhile", "product", "dose", "dosage", "monohydrate")):
        return "creatine monohydrate supplementation safety resistance training"
    if "beta-alanine" in raw_text or "beta alanine" in raw_text:
        if any(term in raw_text for term in ("tingling", "paresthesia", "safe", "safety", "side effect", "adverse")):
            return "beta alanine paresthesia"
        return "beta alanine supplementation exercise performance"
    tokens = [
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", raw_text)
        if token not in _PUBMED_STOPWORDS
    ]
    terms: list[str] = []
    seen: set[str] = set()
    for token in tokens[:12]:
        mapped = _PUBMED_SYNONYMS.get(token, (token,))
        for term in mapped:
            if term not in seen:
                seen.add(term)
                terms.append(term)
    return " ".join(terms or tokens or ["exercise nutrition"])[:240]


def _build_term(query: str) -> str:
    text = _normalize_pubmed_query(query)
    quality = "(systematic review[Publication Type] OR meta-analysis[Publication Type] OR randomized controlled trial[Publication Type] OR clinical trial[Publication Type] OR review[Publication Type])"
    humans = "humans[MeSH Terms]"
    return f"({text}) AND ({quality}) AND ({humans})"


@dataclass(frozen=True)
class NCBIPubMedClientV1:
    timeout_seconds: float = 15.0
    max_records: int = 3
    api_key: str = ""
    tool_email: str = ""
    tool_name: str = "verbalsage-trusted-web"

    @classmethod
    def from_env(cls) -> "NCBIPubMedClientV1":
        return cls(
            api_key=(os.getenv("NCBI_API_KEY") or "").strip(),
            tool_email=(os.getenv("NCBI_TOOL_EMAIL") or "").strip(),
        )

    def search(self, query: str) -> tuple[NCBIResearchRecordV1, ...]:
        ids = self._esearch(query)
        if not ids:
            raise NCBIClientError("ncbi_no_pubmed_results")
        records = self._efetch(ids)
        if not records:
            raise NCBIClientError("ncbi_no_fetchable_records")
        return records[: self.max_records]

    def _request_xml(self, endpoint: str, params: dict[str, str]) -> ET.Element:
        fixed = {"tool": self.tool_name, "retmode": "xml"}
        if self.tool_email:
            fixed["email"] = self.tool_email
        if self.api_key:
            fixed["api_key"] = self.api_key
        url = f"{_EUTILS_BASE}/{endpoint}?{urllib.parse.urlencode({**fixed, **params})}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "VerbalSageTrustedWeb/1.1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                ctype = str(resp.headers.get("content-type") or "").lower()
                if "xml" not in ctype and "text" not in ctype:
                    raise NCBIClientError("ncbi_unexpected_content_type")
                data = resp.read(1_000_000)
        except Exception as exc:
            raise NCBIClientError("ncbi_request_failed") from exc
        try:
            return ET.fromstring(data)
        except ET.ParseError as exc:
            raise NCBIClientError("ncbi_xml_parse_failed") from exc

    def _esearch(self, query: str) -> tuple[str, ...]:
        root = self._request_xml(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": _build_term(query),
                "retmax": str(max(1, min(10, self.max_records * 2))),
                "sort": "relevance",
            },
        )
        ids: list[str] = []
        for elem in root.findall(".//IdList/Id"):
            value = "".join(elem.itertext()).strip()
            if value.isdigit() and value not in ids:
                ids.append(value)
        return tuple(ids)

    def _efetch(self, ids: tuple[str, ...]) -> tuple[NCBIResearchRecordV1, ...]:
        root = self._request_xml(
            "efetch.fcgi",
            {"db": "pubmed", "id": ",".join(ids)},
        )
        records: list[NCBIResearchRecordV1] = []
        for article in root.findall(".//PubmedArticle"):
            pmid = "".join(article.findtext(".//PMID") or "").strip()
            title = _text(article.find(".//ArticleTitle"))
            abstract = " ".join(
                _text(node) for node in article.findall(".//Abstract/AbstractText")
            ).strip()
            journal = _text(article.find(".//Journal/Title"))
            pub_date = _publication_date(article)
            pub_types = tuple(
                _text(node)[:120]
                for node in article.findall(".//PublicationTypeList/PublicationType")
                if _text(node)
            )
            if pmid.isdigit() and title:
                records.append(
                    NCBIResearchRecordV1(
                        pmid=pmid,
                        title=title[:600],
                        journal=journal[:300],
                        publication_date=pub_date[:80],
                        publication_types=pub_types[:20],
                        abstract=abstract[:6000],
                    )
                )
        return tuple(records)


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _publication_date(article: ET.Element) -> str:
    pub_date = article.find(".//JournalIssue/PubDate")
    if pub_date is None:
        return ""
    year = _text(pub_date.find("Year"))
    month = _text(pub_date.find("Month"))
    day = _text(pub_date.find("Day"))
    medline = _text(pub_date.find("MedlineDate"))
    return " ".join(part for part in (year, month, day) if part) or medline


def format_ncbi_records_for_model(
    query: str,
    records: tuple[NCBIResearchRecordV1, ...],
) -> str:
    blocks = [
        "User question:",
        query.strip(),
        "",
        "Use only the PubMed records below. Cite material claims inline with [PMID:number]. If the records are insufficient, say what is missing.",
    ]
    for record in records:
        blocks.extend(
            [
                "",
                f"Source {record.citation_marker}",
                f"Title: {record.title}",
                f"Journal: {record.journal}",
                f"Date: {record.publication_date}",
                f"Publication types: {', '.join(record.publication_types) or 'not listed'}",
                f"Abstract: {record.abstract or 'Abstract not available from PubMed.'}",
            ]
        )
    return "\n".join(blocks)


__all__ = [
    "NCBIClientError",
    "NCBIPubMedClientV1",
    "NCBIResearchRecordV1",
    "classify_publication_types",
    "format_ncbi_records_for_model",
    "trusted_web_topic_uses_ncbi",
]
