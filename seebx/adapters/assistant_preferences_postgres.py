from __future__ import annotations

"""PostgreSQL authority for explicit owner-scoped assistant preferences."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.capabilities.preferences.assistant_contracts import (
    AssistantPreferencesPublic,
    AssistantPreferencesRecord,
    AssistantPreferencesUpdate,
    EffectiveAssistantPreferencePlanV1,
    CompiledPreferenceRule,
    ConversationStyle,
    PreferenceCompilationCandidate,
    PreferenceCompilationPublic,
    PreferenceCompilationStatus,
    PreferenceSource,
    ResponseFormat,
    ResponseLength,
    TechnicalDepth,
    compiled_rule_marker,
    default_preferences,
    effective_preference_plan,
)


class PreferencesConflict(RuntimeError):
    pass


class PreferenceCandidateUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreferencesBundle:
    preferences: AssistantPreferencesRecord
    public_state: PreferenceCompilationPublic
    preference_narrative: str | None

    def public_value(self) -> AssistantPreferencesPublic:
        value = self.preferences
        return AssistantPreferencesPublic(
            revision=value.revision,
            updated_at=value.updated_at,
            assistant_name=value.assistant_name,
            nickname=value.nickname,
            occupation=value.occupation,
            more_about_you=value.more_about_you,
            custom_instructions=self.preference_narrative,
            response_length=value.response_length,
            technical_depth=value.technical_depth,
            response_format=value.response_format,
            conversation_style=value.conversation_style,
            compilation=self.public_state,
        )


def _record(row: Any, owner: UUID) -> AssistantPreferencesRecord:
    if row is None:
        return default_preferences(owner)
    return AssistantPreferencesRecord(
        owner_user_id=UUID(str(row["owner_user_id"])),
        revision=int(row["revision"]),
        source=PreferenceSource.POSTGRES,
        updated_at=row["updated_at"],
        assistant_name=row["assistant_name"],
        nickname=row["nickname"],
        occupation=row["occupation"],
        more_about_you=row["more_about_you"],
        compiled_rule_marker=row["custom_instructions"],
        source_compilation_plan_sha256=row["active_compilation_plan_sha256"],
        response_length=ResponseLength(str(row["response_length"])),
        technical_depth=TechnicalDepth(str(row["technical_depth"])),
        response_format=ResponseFormat(str(row["response_format"])),
        conversation_style=ConversationStyle(str(row["conversation_style"])),
    )


async def _load_record(conn: Any, owner: UUID, *, lock: bool = False) -> Any:
    suffix = " FOR UPDATE" if lock else ""
    return await conn.fetchrow(
        """
        SELECT owner_user_id, revision, assistant_name, nickname, occupation,
               more_about_you, custom_instructions, response_length,
               technical_depth, response_format, conversation_style, updated_at,
               active_compilation_plan_sha256
          FROM user_settings.assistant_response_preference_v1
         WHERE owner_user_id=$1
        """ + suffix,
        owner,
    )


async def _load_bundle(conn: Any, owner: UUID) -> PreferencesBundle:
    record_row = await _load_record(conn, owner)
    state_row = await conn.fetchrow(
        """
        SELECT preference_narrative, active_compilation_candidate_id,
               active_compilation_summary, active_compilation_rejections,
               active_compiled_at
          FROM user_settings.assistant_response_preference_v1
         WHERE owner_user_id=$1
        """,
        owner,
    )
    if state_row is None:
        state = PreferenceCompilationPublic(status="none")
        narrative = None
    else:
        active = state_row["active_compilation_candidate_id"] is not None
        state = PreferenceCompilationPublic(
            status="active" if active else "none",
            summary=tuple(str(item) for item in (state_row["active_compilation_summary"] or ())),
            not_applied=tuple(str(item) for item in (state_row["active_compilation_rejections"] or ())),
            compiled_at=state_row["active_compiled_at"],
        )
        narrative = state_row["preference_narrative"]
    return PreferencesBundle(_record(record_row, owner), state, narrative)


class PostgresAssistantPreferencesRepository:
    def __init__(self, postgres: PostgresConnectionProvider) -> None:
        self._postgres = postgres

    async def get(self, owner: UUID) -> PreferencesBundle:
        async with self._postgres.owner_connection(owner) as conn:
            async with conn.transaction(readonly=True):
                return await _load_bundle(conn, owner)

    async def get_effective_plan(
        self, owner: UUID
    ) -> EffectiveAssistantPreferencePlanV1 | None:
        bundle = await self.get(owner)
        return effective_preference_plan(bundle.preferences)

    async def put(self, owner: UUID, value: AssistantPreferencesUpdate) -> PreferencesBundle:
        async with self._postgres.owner_connection(owner) as conn:
            async with conn.transaction():
                if value.expected_revision == 0:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO user_settings.assistant_response_preference_v1 (
                          owner_user_id, revision, assistant_name, nickname,
                          occupation, more_about_you, preference_narrative,
                          response_length, technical_depth, response_format,
                          conversation_style
                        ) VALUES ($1,1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        ON CONFLICT (owner_user_id) DO NOTHING
                        RETURNING owner_user_id
                        """,
                        owner, value.assistant_name, value.nickname,
                        value.occupation, value.more_about_you,
                        value.custom_instructions, value.response_length.value,
                        value.technical_depth.value, value.response_format.value,
                        value.conversation_style.value,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        UPDATE user_settings.assistant_response_preference_v1
                           SET revision=revision+1,
                               assistant_name=$3, nickname=$4, occupation=$5,
                               more_about_you=$6,
                               custom_instructions=CASE WHEN preference_narrative IS NOT DISTINCT FROM $7 THEN custom_instructions ELSE NULL END,
                               active_compilation_candidate_id=CASE WHEN preference_narrative IS NOT DISTINCT FROM $7 THEN active_compilation_candidate_id ELSE NULL END,
                               active_compilation_summary=CASE WHEN preference_narrative IS NOT DISTINCT FROM $7 THEN active_compilation_summary ELSE ARRAY[]::text[] END,
                               active_compilation_rejections=CASE WHEN preference_narrative IS NOT DISTINCT FROM $7 THEN active_compilation_rejections ELSE ARRAY[]::text[] END,
                               active_compilation_plan_sha256=CASE WHEN preference_narrative IS NOT DISTINCT FROM $7 THEN active_compilation_plan_sha256 ELSE NULL END,
                               active_compiler_version=CASE WHEN preference_narrative IS NOT DISTINCT FROM $7 THEN active_compiler_version ELSE NULL END,
                               active_compiled_at=CASE WHEN preference_narrative IS NOT DISTINCT FROM $7 THEN active_compiled_at ELSE NULL END,
                               preference_narrative=$7,
                               response_length=$8, technical_depth=$9,
                               response_format=$10, conversation_style=$11,
                               updated_at=clock_timestamp()
                         WHERE owner_user_id=$1 AND revision=$2
                        RETURNING owner_user_id
                        """,
                        owner, value.expected_revision, value.assistant_name,
                        value.nickname, value.occupation, value.more_about_you,
                        value.custom_instructions, value.response_length.value,
                        value.technical_depth.value, value.response_format.value,
                        value.conversation_style.value,
                    )
                if row is None:
                    raise PreferencesConflict("preferences changed since they were loaded")
                return await _load_bundle(conn, owner)

    async def revision(self, owner: UUID) -> int:
        async with self._postgres.owner_connection(owner) as conn:
            async with conn.transaction(readonly=True):
                value = await conn.fetchval(
                    "SELECT revision FROM user_settings.assistant_response_preference_v1 WHERE owner_user_id=$1",
                    owner,
                )
                return int(value) if value is not None else 0

    async def store_candidate(self, candidate: PreferenceCompilationCandidate) -> None:
        owner = candidate.owner_user_id
        async with self._postgres.owner_connection(owner) as conn:
            async with conn.transaction():
                current = await conn.fetchval(
                    "SELECT revision FROM user_settings.assistant_response_preference_v1 WHERE owner_user_id=$1",
                    owner,
                )
                if (int(current) if current is not None else 0) != candidate.source_revision:
                    raise PreferencesConflict("preferences changed during compilation")
                await conn.execute(
                    """
                    UPDATE user_settings.assistant_response_preference_compilation_candidate_v1
                       SET status='superseded'
                     WHERE owner_user_id=$1 AND status='candidate'
                    """,
                    owner,
                )
                await conn.execute(
                    """
                    INSERT INTO user_settings.assistant_response_preference_compilation_candidate_v1 (
                      candidate_id, owner_user_id, source_revision,
                      source_narrative, source_narrative_sha256,
                      proposed_response_length, proposed_technical_depth,
                      proposed_response_format, proposed_conversation_style,
                      rule_ids, rejected_reason_codes, compilation_status,
                      summary, not_applied, compiler_version, plan_sha256,
                      provider_model, provider_response_id, created_at,
                      expires_at, status
                    ) VALUES (
                      $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                      $16,$17,$18,$19,$20,'candidate'
                    )
                    """,
                    candidate.candidate_id, owner, candidate.source_revision,
                    candidate.source_narrative, candidate.source_narrative_sha256,
                    candidate.response_length.value if candidate.response_length else None,
                    candidate.technical_depth.value if candidate.technical_depth else None,
                    candidate.response_format.value if candidate.response_format else None,
                    candidate.conversation_style.value if candidate.conversation_style else None,
                    [item.value for item in candidate.rule_ids],
                    [item.value for item in candidate.rejected_reason_codes],
                    candidate.compilation_status.value, list(candidate.summary),
                    list(candidate.not_applied), candidate.compiler_version,
                    candidate.plan_sha256, candidate.provider_model,
                    candidate.provider_response_id, candidate.created_at,
                    candidate.expires_at,
                )

    async def approve(self, owner: UUID, candidate_id: UUID, expected_revision: int) -> PreferencesBundle:
        async with self._postgres.owner_connection(owner) as conn:
            async with conn.transaction():
                current = await _load_record(conn, owner, lock=True)
                actual_revision = int(current["revision"]) if current is not None else 0
                if actual_revision != expected_revision:
                    raise PreferencesConflict("preferences changed before approval")
                candidate = await conn.fetchrow(
                    """
                    SELECT * FROM user_settings.assistant_response_preference_compilation_candidate_v1
                     WHERE owner_user_id=$1 AND candidate_id=$2 FOR UPDATE
                    """,
                    owner,
                    candidate_id,
                )
                if (
                    candidate is None
                    or str(candidate["status"]) != "candidate"
                    or int(candidate["source_revision"]) != expected_revision
                    or candidate["expires_at"] <= datetime.now(timezone.utc)
                    or str(candidate["compilation_status"]) == PreferenceCompilationStatus.REJECTED.value
                ):
                    raise PreferenceCandidateUnavailable("candidate is unavailable")
                rules = tuple(CompiledPreferenceRule(str(item)) for item in candidate["rule_ids"])
                marker = compiled_rule_marker(rules)
                is_clear = str(candidate["compilation_status"]) == PreferenceCompilationStatus.CLEAR.value
                current_length = ResponseLength(str(current["response_length"])) if current else ResponseLength.BALANCED
                current_depth = TechnicalDepth(str(current["technical_depth"])) if current else TechnicalDepth.BALANCED
                current_format = ResponseFormat(str(current["response_format"])) if current else ResponseFormat.AUTO
                current_style = ConversationStyle(str(current["conversation_style"])) if current else ConversationStyle.NATURAL
                length = ResponseLength(str(candidate["proposed_response_length"])) if candidate["proposed_response_length"] else current_length
                depth = TechnicalDepth(str(candidate["proposed_technical_depth"])) if candidate["proposed_technical_depth"] else current_depth
                response_format = ResponseFormat(str(candidate["proposed_response_format"])) if candidate["proposed_response_format"] else current_format
                style = ConversationStyle(str(candidate["proposed_conversation_style"])) if candidate["proposed_conversation_style"] else current_style
                active_id = None if is_clear else candidate_id
                summary = [] if is_clear else list(candidate["summary"])
                rejected = [] if is_clear else list(candidate["not_applied"])
                plan_sha = None if is_clear else str(candidate["plan_sha256"])
                compiler_version = None if is_clear else str(candidate["compiler_version"])
                narrative = str(candidate["source_narrative"]) or None
                if current is None:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO user_settings.assistant_response_preference_v1 (
                          owner_user_id, revision, preference_narrative,
                          custom_instructions, response_length, technical_depth,
                          response_format, conversation_style,
                          active_compilation_candidate_id,
                          active_compilation_summary,
                          active_compilation_rejections,
                          active_compilation_plan_sha256,
                          active_compiler_version, active_compiled_at
                        ) VALUES ($1,1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                          CASE WHEN $8::uuid IS NULL THEN NULL ELSE clock_timestamp() END)
                        RETURNING owner_user_id
                        """,
                        owner, narrative, marker, length.value, depth.value,
                        response_format.value, style.value, active_id, summary,
                        rejected, plan_sha, compiler_version,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        UPDATE user_settings.assistant_response_preference_v1
                           SET revision=revision+1, preference_narrative=$3,
                               custom_instructions=$4, response_length=$5,
                               technical_depth=$6, response_format=$7,
                               conversation_style=$8,
                               active_compilation_candidate_id=$9,
                               active_compilation_summary=$10,
                               active_compilation_rejections=$11,
                               active_compilation_plan_sha256=$12,
                               active_compiler_version=$13,
                               active_compiled_at=CASE WHEN $9::uuid IS NULL THEN NULL ELSE clock_timestamp() END,
                               updated_at=clock_timestamp()
                         WHERE owner_user_id=$1 AND revision=$2
                        RETURNING owner_user_id
                        """,
                        owner, expected_revision, narrative, marker, length.value,
                        depth.value, response_format.value, style.value, active_id,
                        summary, rejected, plan_sha, compiler_version,
                    )
                if row is None:
                    raise PreferencesConflict("preferences changed during approval")
                await conn.execute(
                    """
                    UPDATE user_settings.assistant_response_preference_compilation_candidate_v1
                       SET status='approved', approved_at=clock_timestamp()
                     WHERE owner_user_id=$1 AND candidate_id=$2 AND status='candidate'
                    """,
                    owner,
                    candidate_id,
                )
                return await _load_bundle(conn, owner)
