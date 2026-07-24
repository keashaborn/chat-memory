from __future__ import annotations

"""Deterministic NIH ODS guidance retrieval for supplement safety."""

import html
import re

from pydantic import BaseModel, ConfigDict, Field

from rag_engine.trusted_web_policy_v1 import ODS_DOMAIN, validate_allowed_source_url


ODS_EXERCISE_PERFORMANCE_CONSUMER_URL = (
    "https://ods.od.nih.gov/factsheets/ExerciseAndAthleticPerformance-Consumer/"
)


class ODSClientError(RuntimeError):
    pass


class ODSGuidanceRecordV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    url: str = Field(min_length=1, max_length=4096)
    title: str = Field(min_length=1, max_length=500)
    section_title: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=120)
    evidence_type: str = Field(default="official_public_guidance", max_length=80)
    guidance_text: str = Field(min_length=1, max_length=1800)

    @property
    def citation_marker(self) -> str:
        return "[ODS]"


def trusted_web_query_uses_ods(query: str) -> bool:
    text = " ".join(str(query or "").lower().split())
    if "creatine" not in text and "monohydrate" not in text:
        return False
    safety_terms = (
        "contraindication",
        "contraindications",
        "safe",
        "safety",
        "side effect",
        "side effects",
        "kidney",
        "renal",
        "dose",
        "dosage",
        "take",
        "taking",
    )
    return any(term in text for term in safety_terms)


class NIHODSClientV1:
    """Return pinned NIH ODS guidance records.

    Runtime network fetching is intentionally avoided in the canary path. The
    source URL is retained, and the short guidance extract should be refreshed
    by a controlled maintenance job rather than during a user request.
    """

    def creatine_exercise_performance(self) -> ODSGuidanceRecordV1:
        url = validate_allowed_source_url(
            ODS_EXERCISE_PERFORMANCE_CONSUMER_URL,
            (ODS_DOMAIN,),
        )
        return ODSGuidanceRecordV1(
            url=url,
            title="Dietary Supplements for Exercise and Athletic Performance - Consumer",
            section_title="Creatine",
            source_id="ExerciseAndAthleticPerformance:Creatine:Consumer",
            guidance_text=(
                "NIH ODS states that creatine is stored in muscles and supplies energy. "
                "Creatine supplements can increase strength, power, and maximal-effort muscle contraction, "
                "with individual variation. ODS describes creatine as useful mainly for repeated short bursts "
                "of intense intermittent activity, such as sprinting and weightlifting, and as having little value "
                "for endurance activities. ODS says creatine is safe for healthy adults for weeks or months and "
                "appears safe for long-term use over several years. It can cause weight gain from water retention; "
                "rare individual reactions include muscle stiffness, cramps, and gastrointestinal distress. ODS lists "
                "typical study dosing as about 20 g/day in four equal portions for 5 to 7 days, followed by 3 to 5 g/day, "
                "and identifies creatine monohydrate as the most widely used and studied form."
            ),
        )


def _compact_creatine_guidance(section: str) -> str:
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    keep: list[str] = []
    capture = False
    for line in lines:
        lower = line.lower()
        if line == "Creatine" or lower.startswith("does it work?") or lower.startswith("is it safe?") or lower.startswith("bottom line"):
            capture = True
        if capture:
            keep.append(line)
        if len(" ".join(keep)) > 1400:
            break
    value = "\n".join(keep or lines[:18]).strip()
    return value


def _html_to_text(raw_html: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", raw_html)
    value = re.sub(r"(?i)<br\\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|table)>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


async def load_cached_ods_creatine_guidance(conn) -> ODSGuidanceRecordV1 | None:
    row = await conn.fetchrow(
        """
        SELECT source_id, url, title, section_title, evidence_type, guidance_text
        FROM trusted_web.source_cache
        WHERE source_id = $1
          AND status = 'active'
          AND expires_at > now()
        """,
        "ExerciseAndAthleticPerformance:Creatine:Consumer",
    )
    if not row:
        return None
    try:
        return ODSGuidanceRecordV1(
            url=str(row["url"]),
            title=str(row["title"]),
            section_title=str(row["section_title"]),
            source_id=str(row["source_id"]),
            evidence_type=str(row["evidence_type"]),
            guidance_text=str(row["guidance_text"]),
        )
    except Exception as exc:
        raise ODSClientError("ods_cache_record_invalid") from exc


def format_ods_guidance_for_model(records: tuple[ODSGuidanceRecordV1, ...]) -> str:
    if not records:
        return ""
    blocks = [
        "Official public guidance records:",
        "Use these as the first-line safety/plain-language reference. Cite material claims inline with [ODS].",
    ]
    for record in records:
        blocks.extend(
            [
                "",
                f"Source {record.citation_marker}",
                f"Title: {record.title}",
                f"URL: {record.url}",
                f"Section: {record.section_title}",
                f"Text: {record.guidance_text}",
            ]
        )
    return "\n".join(blocks)


__all__ = [
    "NIHODSClientV1",
    "ODSClientError",
    "ODSGuidanceRecordV1",
    "format_ods_guidance_for_model",
    "load_cached_ods_creatine_guidance",
    "trusted_web_query_uses_ods",
]
