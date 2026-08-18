#!/usr/bin/env python3
from __future__ import annotations

"""Refresh bounded NIH ODS guidance cache for trusted-web requests."""

import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from datetime import datetime, timedelta, timezone

import asyncpg
import subprocess

from seebx.capabilities.search.ods import (
    NIHODSClientV1,
    ODS_EXERCISE_PERFORMANCE_CONSUMER_URL,
    ODSGuidanceRecordV1,
    _compact_creatine_guidance,
    _html_to_text,
)
from seebx.capabilities.search.policy import ODS_DOMAIN, validate_allowed_source_url


def _extract_section(text: str, start_heading: str, end_heading: str) -> str:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    start_match = re.search(rf"(?m)^{re.escape(start_heading)}$", normalized)
    if not start_match:
        raise RuntimeError("ods_creatine_section_missing")
    end_match = re.search(rf"(?m)^{re.escape(end_heading)}$", normalized[start_match.end():])
    end = start_match.end() + end_match.start() if end_match else len(normalized)
    return normalized[start_match.start():end].strip()


def fetch_creatine_record() -> ODSGuidanceRecordV1:
    url = validate_allowed_source_url(ODS_EXERCISE_PERFORMANCE_CONSUMER_URL, (ODS_DOMAIN,))
    completed = subprocess.run(
        [
            "curl",
            "-4",
            "-sS",
            "--fail",
            "--location",
            "--connect-timeout",
            "5",
            "--max-time",
            "20",
            "--user-agent",
            "VerbalSageTrustedWebODSCache/1.0",
            url,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=25,
    )
    raw = completed.stdout
    if len(raw) > 1_000_000:
        raise RuntimeError("ods_response_too_large")
    text = _html_to_text(raw.decode("utf-8", errors="replace"))
    guidance = _compact_creatine_guidance(_extract_section(text, "Creatine", "Deer antler velvet"))
    if len(guidance) < 200:
        raise RuntimeError("ods_guidance_too_short")
    return ODSGuidanceRecordV1(
        url=url,
        title="Dietary Supplements for Exercise and Athletic Performance - Consumer",
        section_title="Creatine",
        source_id="ExerciseAndAthleticPerformance:Creatine:Consumer",
        guidance_text=guidance[:1800],
    )


async def upsert_record(record: ODSGuidanceRecordV1) -> None:
    dsn = (os.getenv("POSTGRES_DSN") or "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN missing")
    content_hash = hashlib.sha256(record.guidance_text.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=30)
    conn = await asyncpg.connect(dsn, command_timeout=20)
    try:
        await conn.execute(
            """
            INSERT INTO trusted_web.source_cache (
                source_id, authority_type, evidence_type, url, title, section_title,
                guidance_text, content_sha256, fetched_at, expires_at, status,
                error_code, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'active', NULL, now())
            ON CONFLICT (source_id)
            DO UPDATE SET
                authority_type = EXCLUDED.authority_type,
                evidence_type = EXCLUDED.evidence_type,
                url = EXCLUDED.url,
                title = EXCLUDED.title,
                section_title = EXCLUDED.section_title,
                guidance_text = EXCLUDED.guidance_text,
                content_sha256 = EXCLUDED.content_sha256,
                fetched_at = EXCLUDED.fetched_at,
                expires_at = EXCLUDED.expires_at,
                status = 'active',
                error_code = NULL,
                updated_at = now()
            """,
            record.source_id,
            "official_public_guidance",
            record.evidence_type,
            record.url,
            record.title,
            record.section_title,
            record.guidance_text,
            content_hash,
            now,
            expires,
        )
    finally:
        await conn.close()


async def main() -> None:
    record = fetch_creatine_record()
    await upsert_record(record)
    print(f"ODS_CACHE_REFRESHED source_id={record.source_id} bytes={len(record.guidance_text)}")


if __name__ == "__main__":
    asyncio.run(main())
