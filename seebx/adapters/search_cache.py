from __future__ import annotations

"""PostgreSQL-backed trusted-search cache reads."""

from typing import Any

from seebx.capabilities.search.ods import ODSClientError, ODSGuidanceRecordV1


async def load_cached_ods_creatine_guidance(
    conn: Any,
) -> ODSGuidanceRecordV1 | None:
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


__all__ = ["load_cached_ods_creatine_guidance"]
