from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import asyncpg

from .memory_v1_store import actor_uuid, record_evidence


ARTIFACT_NAMESPACE = uuid.UUID("1b2f02dc-05c8-4af7-8e57-262c3c43c698")
ALLOWED_AUTHORSHIP = {"user", "assistant", "external", "mixed", "unknown"}
ALLOWED_ENDORSEMENTS = {
    "submitted",
    "reference",
    "partial",
    "ratified",
    "rejected",
    "revoked",
}
ALLOWED_EXTRACTION_POLICIES = {"blocked", "review_only", "eligible"}
ALLOWED_SENSITIVITIES = {"low", "medium", "high", "restricted"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MARKDOWN_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+([^\n\r]+)")
NUMBERED_ITEM_RE = re.compile(r"(?m)^\s*(\d+)[.)][ \t]+([^\n\r]+)")
TOP_LEVEL_BULLET_RE = re.compile(r"(?m)^[*-][ \t]+([^\n\r]+)")


class ArtifactError(RuntimeError):
    pass


class ArtifactManifestError(ArtifactError):
    pass


class ArtifactSourceConflict(ArtifactError):
    pass


class ArtifactPersistenceConflict(ArtifactError):
    pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_text(value: Any, field: str, *, max_length: int = 1000) -> str:
    text = str(value or "").strip()
    if not text:
        raise ArtifactManifestError(f"{field} is required")
    if len(text) > max_length:
        raise ArtifactManifestError(f"{field} exceeds {max_length} characters")
    return text


def _uuid5(*parts: Any) -> uuid.UUID:
    return uuid.uuid5(ARTIFACT_NAMESPACE, ":".join(str(part) for part in parts))


@dataclass(frozen=True)
class ArtifactManifestEntry:
    source_id: uuid.UUID
    owner_user_id: uuid.UUID
    expected_sha256: str
    title: str
    artifact_kind: str
    authorship: str
    body_marker: Optional[str]
    body_authorship: Optional[str]
    endorsement_level: str
    endorsement_explicit: bool
    endorsement_rationale: str
    document_state: str
    extraction_policy: str
    sensitivity: str
    manifest_version: str
    source_system: str

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        owner_user_id: uuid.UUID,
        manifest_version: str,
        source_system: str,
    ) -> "ArtifactManifestEntry":
        try:
            source_id = uuid.UUID(str(raw.get("source_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ArtifactManifestError("source_id must be a UUID") from exc

        expected_sha256 = str(raw.get("expected_sha256") or "").strip().lower()
        if not SHA256_RE.fullmatch(expected_sha256):
            raise ArtifactManifestError(f"invalid expected_sha256 for {source_id}")

        authorship = str(raw.get("authorship") or "unknown").strip().lower()
        if authorship not in ALLOWED_AUTHORSHIP:
            raise ArtifactManifestError(f"invalid authorship for {source_id}: {authorship}")

        body_marker = raw.get("body_marker")
        if body_marker is not None:
            body_marker = _required_text(body_marker, "body_marker", max_length=500)

        body_authorship = raw.get("body_authorship")
        if body_authorship is not None:
            body_authorship = str(body_authorship).strip().lower()
            if body_authorship not in ALLOWED_AUTHORSHIP:
                raise ArtifactManifestError(
                    f"invalid body_authorship for {source_id}: {body_authorship}"
                )
        if body_marker and not body_authorship:
            raise ArtifactManifestError(
                f"body_authorship is required when body_marker is set for {source_id}"
            )
        if body_authorship and not body_marker:
            raise ArtifactManifestError(
                f"body_marker is required when body_authorship is set for {source_id}"
            )

        endorsement_level = str(raw.get("endorsement_level") or "submitted").strip().lower()
        if endorsement_level not in ALLOWED_ENDORSEMENTS:
            raise ArtifactManifestError(
                f"invalid endorsement_level for {source_id}: {endorsement_level}"
            )
        endorsement_explicit = bool(raw.get("endorsement_explicit", False))
        if endorsement_level == "ratified" and not endorsement_explicit:
            raise ArtifactManifestError("ratified endorsement must be explicit")

        extraction_policy = str(raw.get("extraction_policy") or "review_only").strip().lower()
        if extraction_policy not in ALLOWED_EXTRACTION_POLICIES:
            raise ArtifactManifestError(
                f"invalid extraction_policy for {source_id}: {extraction_policy}"
            )

        sensitivity = str(raw.get("sensitivity") or "medium").strip().lower()
        if sensitivity not in ALLOWED_SENSITIVITIES:
            raise ArtifactManifestError(f"invalid sensitivity for {source_id}: {sensitivity}")

        return cls(
            source_id=source_id,
            owner_user_id=owner_user_id,
            expected_sha256=expected_sha256,
            title=_required_text(raw.get("title"), "title", max_length=500),
            artifact_kind=_required_text(
                raw.get("artifact_kind"), "artifact_kind", max_length=120
            ),
            authorship=authorship,
            body_marker=body_marker,
            body_authorship=body_authorship,
            endorsement_level=endorsement_level,
            endorsement_explicit=endorsement_explicit,
            endorsement_rationale=_required_text(
                raw.get("endorsement_rationale"),
                "endorsement_rationale",
                max_length=2000,
            ),
            document_state=_required_text(
                raw.get("document_state"), "document_state", max_length=120
            ),
            extraction_policy=extraction_policy,
            sensitivity=sensitivity,
            manifest_version=manifest_version,
            source_system=source_system,
        )


@dataclass(frozen=True)
class ArtifactSourceRow:
    source_id: uuid.UUID
    owner_user_id: uuid.UUID
    source: str
    text: str
    created_at: datetime
    thread_id: Optional[uuid.UUID]
    vantage_id: Optional[str]
    request_id: Optional[str]


@dataclass(frozen=True)
class ArtifactSectionPlan:
    section_id: uuid.UUID
    ordinal: int
    parent_section_id: Optional[uuid.UUID]
    level: int
    heading: Optional[str]
    content: str
    content_sha256: str
    char_start: int
    char_end: int
    authorship: str
    source_role: str


@dataclass(frozen=True)
class ArtifactPlan:
    entry: ArtifactManifestEntry
    source: ArtifactSourceRow
    artifact_id: uuid.UUID
    occurrence_id: uuid.UUID
    endorsement_id: uuid.UUID
    occurrence_key: str
    endorsement_key: str
    sections: Sequence[ArtifactSectionPlan]


@dataclass(frozen=True)
class _SectionDraft:
    char_start: int
    char_end: int
    level: int
    heading: Optional[str]
    authorship: str
    source_role: str
    parent_ordinal: Optional[int]


def load_artifact_manifest(path: str | Path) -> List[ArtifactManifestEntry]:
    manifest_path = Path(path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactManifestError(f"cannot read manifest: {manifest_path}") from exc
    if not isinstance(raw, dict):
        raise ArtifactManifestError("manifest root must be an object")

    try:
        owner = actor_uuid(raw.get("owner_user_id"))
    except Exception as exc:
        raise ArtifactManifestError("manifest owner_user_id must be a non-nil UUID") from exc
    manifest_version = _required_text(
        raw.get("manifest_version"), "manifest_version", max_length=200
    )
    source_system = _required_text(
        raw.get("source_system"), "source_system", max_length=200
    )
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise ArtifactManifestError("manifest entries must be a non-empty array")

    entries = [
        ArtifactManifestEntry.from_mapping(
            item,
            owner_user_id=owner,
            manifest_version=manifest_version,
            source_system=source_system,
        )
        for item in entries_raw
    ]
    source_ids = [entry.source_id for entry in entries]
    if len(source_ids) != len(set(source_ids)):
        raise ArtifactManifestError("manifest contains duplicate source_id values")
    hashes = [entry.expected_sha256 for entry in entries]
    if len(hashes) != len(set(hashes)):
        raise ArtifactManifestError(
            "manifest contains duplicate content hashes; use occurrences for duplicates"
        )
    return entries


async def fetch_manifest_sources_readonly(
    conn: asyncpg.Connection,
    entries: Sequence[ArtifactManifestEntry],
) -> Dict[uuid.UUID, ArtifactSourceRow]:
    if not entries:
        return {}
    owners = {entry.owner_user_id for entry in entries}
    source_systems = {entry.source_system for entry in entries}
    if len(owners) != 1 or source_systems != {"public.chat_log"}:
        raise ArtifactManifestError(
            "the current dry-run reader supports one owner and public.chat_log only"
        )
    owner = next(iter(owners))
    ids = [entry.source_id for entry in entries]

    async with conn.transaction(readonly=True):
        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)",
            str(owner),
        )
        rows = await conn.fetch(
            """
            SELECT id, owner_user_id, source, text, created_at,
                   thread_id, vantage_id, request_id
            FROM public.chat_log
            WHERE owner_user_id=$1
              AND id = ANY($2::uuid[])
            ORDER BY created_at, id
            """,
            owner,
            ids,
        )

    result: Dict[uuid.UUID, ArtifactSourceRow] = {}
    for row in rows:
        source_id = uuid.UUID(str(row["id"]))
        try:
            owner_user_id = actor_uuid(row["owner_user_id"])
        except Exception as exc:
            raise ArtifactSourceConflict(
                f"source row {source_id} has an invalid owner UUID"
            ) from exc
        result[source_id] = ArtifactSourceRow(
            source_id=source_id,
            owner_user_id=owner_user_id,
            source=str(row["source"] or ""),
            text=str(row["text"] or ""),
            created_at=row["created_at"],
            thread_id=uuid.UUID(str(row["thread_id"])) if row["thread_id"] else None,
            vantage_id=str(row["vantage_id"]) if row["vantage_id"] else None,
            request_id=str(row["request_id"]) if row["request_id"] else None,
        )

    missing = [str(entry.source_id) for entry in entries if entry.source_id not in result]
    if missing:
        raise ArtifactSourceConflict("manifest source rows not found: " + ", ".join(missing))
    return result


def _plain_chunks(text: str, *, base_offset: int, max_chars: int = 2400) -> List[tuple[int, int]]:
    if not text.strip():
        return []
    chunks: List[tuple[int, int]] = []
    start = 0
    total = len(text)
    while start < total:
        remaining = total - start
        if remaining <= max_chars:
            end = total
        else:
            target = start + max_chars
            minimum = start + max(400, max_chars // 3)
            boundary = text.rfind("\n\n", minimum, target)
            if boundary >= minimum:
                end = boundary + 2
            else:
                boundary = text.rfind("\n", minimum, target)
                if boundary < minimum:
                    boundary = text.rfind(" ", minimum, target)
                end = boundary + 1 if boundary >= minimum else target
        if text[start:end].strip():
            chunks.append((base_offset + start, base_offset + end))
        start = end
    return chunks


def _structural_matches(text: str) -> tuple[str, list[re.Match[str]]]:
    headings = list(MARKDOWN_HEADING_RE.finditer(text))
    if headings:
        return "markdown_heading", headings
    numbered = list(NUMBERED_ITEM_RE.finditer(text))
    if len(numbered) >= 2:
        return "numbered_item", numbered
    bullets = list(TOP_LEVEL_BULLET_RE.finditer(text))
    if len(bullets) >= 3:
        return "bullet_item", bullets
    return "plain", []


def _segment_region(
    full_text: str,
    *,
    start: int,
    end: int,
    authorship: str,
    source_role: str,
    ordinal_start: int,
) -> List[_SectionDraft]:
    region = full_text[start:end]
    structure, matches = _structural_matches(region)
    drafts: List[_SectionDraft] = []

    if not matches:
        for chunk_start, chunk_end in _plain_chunks(region, base_offset=start):
            drafts.append(
                _SectionDraft(
                    char_start=chunk_start,
                    char_end=chunk_end,
                    level=1,
                    heading=None,
                    authorship=authorship,
                    source_role=source_role,
                    parent_ordinal=None,
                )
            )
        return drafts

    if region[: matches[0].start()].strip():
        for chunk_start, chunk_end in _plain_chunks(
            region[: matches[0].start()], base_offset=start
        ):
            drafts.append(
                _SectionDraft(
                    char_start=chunk_start,
                    char_end=chunk_end,
                    level=1,
                    heading="Preamble",
                    authorship=authorship,
                    source_role=source_role,
                    parent_ordinal=None,
                )
            )

    heading_stack: List[tuple[int, int]] = []
    for index, match in enumerate(matches):
        local_start = match.start()
        local_end = matches[index + 1].start() if index + 1 < len(matches) else len(region)
        char_start = start + local_start
        char_end = start + local_end
        if not full_text[char_start:char_end].strip():
            continue

        if structure == "markdown_heading":
            level = len(match.group(1))
            heading = match.group(2).strip()
        elif structure == "numbered_item":
            level = 1
            heading = f"{match.group(1)}. {match.group(2).strip()}"
        else:
            level = 1
            heading = match.group(1).strip()

        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        parent_ordinal = heading_stack[-1][1] if heading_stack else None
        ordinal = ordinal_start + len(drafts)
        drafts.append(
            _SectionDraft(
                char_start=char_start,
                char_end=char_end,
                level=level,
                heading=heading[:500],
                authorship=authorship,
                source_role=source_role,
                parent_ordinal=parent_ordinal,
            )
        )
        heading_stack.append((level, ordinal))
    return drafts


def build_artifact_plan(
    entry: ArtifactManifestEntry,
    source: ArtifactSourceRow,
) -> ArtifactPlan:
    if source.source_id != entry.source_id:
        raise ArtifactSourceConflict("source ID does not match manifest entry")
    if source.owner_user_id != entry.owner_user_id:
        raise ArtifactSourceConflict("source owner does not match manifest owner")
    if source.source != "frontend/chat:user":
        raise ArtifactSourceConflict(
            f"source {source.source_id} is not a frontend user turn: {source.source}"
        )
    if not source.text.strip():
        raise ArtifactSourceConflict(f"source {source.source_id} has empty text")
    actual_hash = _sha256_text(source.text)
    if actual_hash != entry.expected_sha256:
        raise ArtifactSourceConflict(
            f"source hash mismatch for {source.source_id}: "
            f"expected {entry.expected_sha256}, got {actual_hash}"
        )

    artifact_id = _uuid5("artifact", entry.owner_user_id, actual_hash)
    drafts: List[_SectionDraft] = []
    if entry.body_marker:
        marker_count = source.text.count(entry.body_marker)
        if marker_count != 1:
            raise ArtifactSourceConflict(
                f"body marker for {source.source_id} occurs {marker_count} times"
            )
        body_start = source.text.index(entry.body_marker)
        if source.text[:body_start].strip():
            for chunk_start, chunk_end in _plain_chunks(
                source.text[:body_start], base_offset=0
            ):
                drafts.append(
                    _SectionDraft(
                        char_start=chunk_start,
                        char_end=chunk_end,
                        level=1,
                        heading="Submission context",
                        authorship="user",
                        source_role="submission_context",
                        parent_ordinal=None,
                    )
                )
        drafts.extend(
            _segment_region(
                source.text,
                start=body_start,
                end=len(source.text),
                authorship=entry.body_authorship or "unknown",
                source_role="document_body",
                ordinal_start=len(drafts),
            )
        )
    else:
        drafts.extend(
            _segment_region(
                source.text,
                start=0,
                end=len(source.text),
                authorship=entry.authorship,
                source_role="document_body",
                ordinal_start=0,
            )
        )

    if not drafts:
        raise ArtifactSourceConflict(f"source {source.source_id} produced no sections")

    section_ids: List[uuid.UUID] = []
    for ordinal, draft in enumerate(drafts):
        content = source.text[draft.char_start : draft.char_end]
        section_ids.append(
            _uuid5(
                "section",
                artifact_id,
                ordinal,
                draft.char_start,
                draft.char_end,
                _sha256_text(content),
            )
        )

    sections: List[ArtifactSectionPlan] = []
    for ordinal, draft in enumerate(drafts):
        content = source.text[draft.char_start : draft.char_end]
        if not content.strip():
            raise ArtifactSourceConflict("empty section escaped planning validation")
        parent_id = (
            section_ids[draft.parent_ordinal]
            if draft.parent_ordinal is not None
            else None
        )
        sections.append(
            ArtifactSectionPlan(
                section_id=section_ids[ordinal],
                ordinal=ordinal,
                parent_section_id=parent_id,
                level=draft.level,
                heading=draft.heading,
                content=content,
                content_sha256=_sha256_text(content),
                char_start=draft.char_start,
                char_end=draft.char_end,
                authorship=draft.authorship,
                source_role=draft.source_role,
            )
        )

    previous_end = 0
    known_section_ids: set[uuid.UUID] = set()
    for section in sections:
        if section.char_start < previous_end:
            raise ArtifactSourceConflict(
                f"overlapping section spans at ordinal {section.ordinal}"
            )
        if source.text[previous_end : section.char_start].strip():
            raise ArtifactSourceConflict(
                f"uncovered non-whitespace source text before ordinal {section.ordinal}"
            )
        if section.parent_section_id and section.parent_section_id not in known_section_ids:
            raise ArtifactSourceConflict(
                f"section parent must precede child at ordinal {section.ordinal}"
            )
        if source.text[section.char_start : section.char_end] != section.content:
            raise ArtifactSourceConflict(
                f"section/source span mismatch at ordinal {section.ordinal}"
            )
        known_section_ids.add(section.section_id)
        previous_end = section.char_end
    if source.text[previous_end:].strip():
        raise ArtifactSourceConflict("uncovered non-whitespace source text after final section")

    occurrence_key = _sha256_text(
        f"{entry.source_system}:{source.source_id}:{artifact_id}:submitted"
    )
    endorsement_key = _sha256_text(
        f"{entry.manifest_version}:{source.source_id}:{artifact_id}:"
        f"{entry.endorsement_level}:{int(entry.endorsement_explicit)}"
    )
    return ArtifactPlan(
        entry=entry,
        source=source,
        artifact_id=artifact_id,
        occurrence_id=_uuid5("occurrence", occurrence_key),
        endorsement_id=_uuid5("endorsement", endorsement_key),
        occurrence_key=occurrence_key,
        endorsement_key=endorsement_key,
        sections=tuple(sections),
    )


def build_manifest_plans(
    entries: Sequence[ArtifactManifestEntry],
    sources: Mapping[uuid.UUID, ArtifactSourceRow],
) -> List[ArtifactPlan]:
    return [build_artifact_plan(entry, sources[entry.source_id]) for entry in entries]


async def persist_artifact_plan(
    conn: asyncpg.Connection,
    actor_user_id: str | uuid.UUID,
    plan: ArtifactPlan,
) -> Dict[str, Any]:
    actor = actor_uuid(actor_user_id)
    if actor != plan.entry.owner_user_id or actor != plan.source.owner_user_id:
        raise ArtifactSourceConflict("actor does not own the artifact plan")

    async with conn.transaction():
        evidence_id = await record_evidence(
            conn,
            actor,
            kind="document",
            source_system=plan.entry.source_system,
            external_id=str(plan.source.source_id),
            content=plan.source.text,
            observed_at=plan.source.created_at,
            sensitivity=plan.entry.sensitivity,
            metadata={
                "thread_id": str(plan.source.thread_id) if plan.source.thread_id else None,
                "request_id": plan.source.request_id,
                "vantage_id_at_capture": plan.source.vantage_id,
                "source_role": plan.source.source,
                "manifest_version": plan.entry.manifest_version,
            },
        )

        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)", str(actor)
        )
        inserted_artifact = await conn.fetchrow(
            """
            INSERT INTO memory.artifact(
              artifact_id, owner_user_id, artifact_kind, title, media_type,
              authorship, content, content_sha256, extraction_policy,
              sensitivity, metadata
            ) VALUES(
              $1, $2, $3, $4, 'text/markdown',
              $5, $6, $7, $8, $9::memory.sensitivity_level, $10::jsonb
            )
            ON CONFLICT DO NOTHING
            RETURNING artifact_id
            """,
            plan.artifact_id,
            actor,
            plan.entry.artifact_kind,
            plan.entry.title,
            plan.entry.authorship,
            plan.source.text,
            plan.entry.expected_sha256,
            plan.entry.extraction_policy,
            plan.entry.sensitivity,
            _stable_json(
                {
                    "document_state": plan.entry.document_state,
                    "manifest_version": plan.entry.manifest_version,
                    "source_system": plan.entry.source_system,
                }
            ),
        )
        existing_artifact = await conn.fetchrow(
            """
            SELECT artifact_id, content_sha256, content
            FROM memory.artifact
            WHERE owner_user_id=$1 AND content_sha256=$2
            """,
            actor,
            plan.entry.expected_sha256,
        )
        if not existing_artifact:
            raise ArtifactPersistenceConflict("artifact insert could not be resolved")
        if uuid.UUID(str(existing_artifact["artifact_id"])) != plan.artifact_id:
            raise ArtifactPersistenceConflict(
                "artifact hash exists under a different deterministic artifact ID"
            )
        if existing_artifact["content"] != plan.source.text:
            raise ArtifactPersistenceConflict("artifact hash/content conflict")

        inserted_occurrence = await conn.fetchrow(
            """
            INSERT INTO memory.artifact_occurrence(
              occurrence_id, owner_user_id, artifact_id, evidence_id,
              occurrence_key, relation, observed_authorship,
              source_char_start, source_char_end, metadata
            ) VALUES(
              $1, $2, $3, $4, $5, 'submitted', $6, 0, $7, $8::jsonb
            )
            ON CONFLICT DO NOTHING
            RETURNING occurrence_id
            """,
            plan.occurrence_id,
            actor,
            plan.artifact_id,
            evidence_id,
            plan.occurrence_key,
            plan.entry.authorship,
            len(plan.source.text),
            _stable_json({"manifest_version": plan.entry.manifest_version}),
        )
        occurrence_id = await conn.fetchval(
            """
            SELECT occurrence_id
            FROM memory.artifact_occurrence
            WHERE owner_user_id=$1 AND occurrence_key=$2
            """,
            actor,
            plan.occurrence_key,
        )
        if not occurrence_id or uuid.UUID(str(occurrence_id)) != plan.occurrence_id:
            raise ArtifactPersistenceConflict("artifact occurrence conflict")

        inserted_sections = 0
        for section in plan.sections:
            row = await conn.fetchrow(
                """
                INSERT INTO memory.artifact_section(
                  section_id, owner_user_id, artifact_id, parent_section_id,
                  ordinal, level, heading, content, content_sha256,
                  char_start, char_end, authorship,
                  retrieval_eligible, promotion_eligible, metadata
                ) VALUES(
                  $1, $2, $3, $4, $5, $6, $7, $8, $9,
                  $10, $11, $12, false, false, $13::jsonb
                )
                ON CONFLICT DO NOTHING
                RETURNING section_id
                """,
                section.section_id,
                actor,
                plan.artifact_id,
                section.parent_section_id,
                section.ordinal,
                section.level,
                section.heading,
                section.content,
                section.content_sha256,
                section.char_start,
                section.char_end,
                section.authorship,
                _stable_json({"source_role": section.source_role}),
            )
            inserted_sections += int(row is not None)
            existing = await conn.fetchrow(
                """
                SELECT section_id, content_sha256, char_start, char_end, authorship,
                       retrieval_eligible, promotion_eligible
                FROM memory.artifact_section
                WHERE owner_user_id=$1 AND artifact_id=$2 AND ordinal=$3
                """,
                actor,
                plan.artifact_id,
                section.ordinal,
            )
            if not existing:
                raise ArtifactPersistenceConflict("artifact section insert disappeared")
            expected = (
                section.section_id,
                section.content_sha256,
                section.char_start,
                section.char_end,
                section.authorship,
                False,
                False,
            )
            actual = (
                uuid.UUID(str(existing["section_id"])),
                existing["content_sha256"],
                existing["char_start"],
                existing["char_end"],
                existing["authorship"],
                existing["retrieval_eligible"],
                existing["promotion_eligible"],
            )
            if actual != expected:
                raise ArtifactPersistenceConflict(
                    f"artifact section conflict at ordinal {section.ordinal}"
                )

        inserted_endorsement = await conn.fetchrow(
            """
            INSERT INTO memory.artifact_endorsement(
              endorsement_id, owner_user_id, artifact_id, evidence_id,
              endorsement_key, endorsement_level, explicit, rationale, metadata
            ) VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING endorsement_id
            """,
            plan.endorsement_id,
            actor,
            plan.artifact_id,
            evidence_id,
            plan.endorsement_key,
            plan.entry.endorsement_level,
            plan.entry.endorsement_explicit,
            plan.entry.endorsement_rationale,
            _stable_json({"manifest_version": plan.entry.manifest_version}),
        )
        endorsement_id = await conn.fetchval(
            """
            SELECT endorsement_id
            FROM memory.artifact_endorsement
            WHERE owner_user_id=$1 AND endorsement_key=$2
            """,
            actor,
            plan.endorsement_key,
        )
        if not endorsement_id or uuid.UUID(str(endorsement_id)) != plan.endorsement_id:
            raise ArtifactPersistenceConflict("artifact endorsement conflict")

        return {
            "artifact_id": str(plan.artifact_id),
            "evidence_id": str(evidence_id),
            "section_count": len(plan.sections),
            "inserted": {
                "artifact": int(inserted_artifact is not None),
                "occurrence": int(inserted_occurrence is not None),
                "sections": inserted_sections,
                "endorsement": int(inserted_endorsement is not None),
            },
        }


def artifact_dry_run_report(
    plans: Iterable[ArtifactPlan],
    *,
    include_sections: bool = True,
) -> Dict[str, Any]:
    plan_list = list(plans)
    artifacts: List[Dict[str, Any]] = []
    total_chars = 0
    total_sections = 0
    for plan in plan_list:
        total_chars += len(plan.source.text)
        total_sections += len(plan.sections)
        item: Dict[str, Any] = {
            "source_id": str(plan.source.source_id),
            "source_created_at": plan.source.created_at.isoformat(),
            "thread_id": str(plan.source.thread_id) if plan.source.thread_id else None,
            "artifact_id": str(plan.artifact_id),
            "title": plan.entry.title,
            "artifact_kind": plan.entry.artifact_kind,
            "document_state": plan.entry.document_state,
            "authorship": plan.entry.authorship,
            "content_sha256": plan.entry.expected_sha256,
            "character_count": len(plan.source.text),
            "section_count": len(plan.sections),
            "endorsement": {
                "level": plan.entry.endorsement_level,
                "explicit": plan.entry.endorsement_explicit,
                "rationale": plan.entry.endorsement_rationale,
            },
            "controls": {
                "extraction_policy": plan.entry.extraction_policy,
                "retrieval_eligible_sections": 0,
                "promotion_eligible_sections": 0,
                "qdrant_projection": False,
            },
        }
        if include_sections:
            item["sections"] = [
                {
                    "section_id": str(section.section_id),
                    "ordinal": section.ordinal,
                    "parent_section_id": (
                        str(section.parent_section_id)
                        if section.parent_section_id
                        else None
                    ),
                    "level": section.level,
                    "heading": section.heading,
                    "authorship": section.authorship,
                    "source_role": section.source_role,
                    "char_start": section.char_start,
                    "char_end": section.char_end,
                    "character_count": section.char_end - section.char_start,
                    "content_sha256": section.content_sha256,
                    "preview": " ".join(section.content.split())[:180],
                }
                for section in plan.sections
            ]
        artifacts.append(item)

    return {
        "mode": "dry_run_no_writes",
        "artifact_count": len(plan_list),
        "total_characters": total_chars,
        "total_sections": total_sections,
        "all_sections_retrieval_eligible": False,
        "all_sections_promotion_eligible": False,
        "qdrant_projection_requested": False,
        "artifacts": artifacts,
    }
