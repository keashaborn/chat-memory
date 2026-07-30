from __future__ import annotations

"""Owner-scoped candidate and approval storage for compiled preferences."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

from rag_engine.assistant_response_preference_compiler_v1 import (
    ASSISTANT_PREFERENCE_COMPILER_VERSION,
    AssistantPreferenceCompilationCandidateV1,
    PreferenceCompilationStatus,
    PreferenceRejectionReasonCode,
)
from rag_engine.assistant_response_preferences_store_v1 import (
    AssistantResponsePreferenceConflictV1,
    _record_from_row,
)
from rag_engine.assistant_response_preferences_v1 import (
    CompiledPreferenceRuleId,
    ConversationStyle,
    ResponseFormat,
    ResponseLength,
    TechnicalDepth,
    compiled_preference_marker_v1,
)


class PreferenceCompilationCandidateUnavailableV1(RuntimeError):
    """The candidate is absent, stale, expired, or already consumed."""


async def load_preference_public_state_v1(
    conn: asyncpg.Connection,
    owner_user_id: UUID,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT preference_narrative,
               active_compilation_candidate_id,
               active_compilation_summary,
               active_compilation_rejections,
               active_compilation_plan_sha256,
               active_compiler_version,
               active_compiled_at
          FROM user_settings.assistant_response_preference_v1
         WHERE owner_user_id=$1
        """,
        owner_user_id,
    )
    if row is None:
        return {
            "custom_instructions": None,
            "compilation": {
                "status": "none",
                "summary": [],
                "not_applied": [],
                "compiled_at": None,
            },
        }
    active = row["active_compilation_candidate_id"] is not None
    return {
        "custom_instructions": row["preference_narrative"],
        "compilation": {
            "status": "active" if active else "none",
            "summary": list(row["active_compilation_summary"] or ()),
            "not_applied": list(row["active_compilation_rejections"] or ()),
            "compiled_at": (
                row["active_compiled_at"].isoformat()
                if row["active_compiled_at"] is not None
                else None
            ),
        },
    }


async def store_compilation_candidate_v1(
    conn: asyncpg.Connection,
    candidate: AssistantPreferenceCompilationCandidateV1,
) -> None:
    current_revision = await conn.fetchval(
        """
        SELECT revision
          FROM user_settings.assistant_response_preference_v1
         WHERE owner_user_id=$1
        """,
        candidate.owner_user_id,
    )
    actual_revision = int(current_revision) if current_revision is not None else 0
    if actual_revision != candidate.source_revision:
        raise AssistantResponsePreferenceConflictV1(
            "assistant response preferences changed during compilation"
        )
    await conn.execute(
        """
        UPDATE user_settings.assistant_response_preference_compilation_candidate_v1
           SET status='superseded'
         WHERE owner_user_id=$1
           AND status='candidate'
        """,
        candidate.owner_user_id,
    )
    await conn.execute(
        """
        INSERT INTO user_settings.assistant_response_preference_compilation_candidate_v1 (
          candidate_id,
          owner_user_id,
          source_revision,
          source_narrative,
          source_narrative_sha256,
          proposed_response_length,
          proposed_technical_depth,
          proposed_response_format,
          proposed_conversation_style,
          rule_ids,
          rejected_reason_codes,
          compilation_status,
          summary,
          not_applied,
          compiler_version,
          plan_sha256,
          provider_model,
          provider_response_id,
          created_at,
          expires_at,
          status
        )
        VALUES (
          $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
          'candidate'
        )
        """,
        candidate.candidate_id,
        candidate.owner_user_id,
        candidate.source_revision,
        candidate.source_narrative,
        candidate.source_narrative_sha256,
        candidate.response_length.value if candidate.response_length else None,
        candidate.technical_depth.value if candidate.technical_depth else None,
        candidate.response_format.value if candidate.response_format else None,
        (
            candidate.conversation_style.value
            if candidate.conversation_style
            else None
        ),
        [item.value for item in candidate.rule_ids],
        [item.value for item in candidate.rejected_reason_codes],
        candidate.status.value,
        list(candidate.summary),
        list(candidate.not_applied),
        candidate.compiler_version,
        candidate.plan_sha256,
        candidate.provider_model,
        candidate.provider_response_id,
        candidate.created_at,
        candidate.expires_at,
    )


def _candidate_values(row: Any) -> dict[str, Any]:
    return {
        "source_narrative": str(row["source_narrative"]),
        "response_length": (
            ResponseLength(str(row["proposed_response_length"]))
            if row["proposed_response_length"] is not None
            else None
        ),
        "technical_depth": (
            TechnicalDepth(str(row["proposed_technical_depth"]))
            if row["proposed_technical_depth"] is not None
            else None
        ),
        "response_format": (
            ResponseFormat(str(row["proposed_response_format"]))
            if row["proposed_response_format"] is not None
            else None
        ),
        "conversation_style": (
            ConversationStyle(str(row["proposed_conversation_style"]))
            if row["proposed_conversation_style"] is not None
            else None
        ),
        "rule_ids": tuple(
            CompiledPreferenceRuleId(str(item)) for item in row["rule_ids"]
        ),
        "rejected_reason_codes": tuple(
            PreferenceRejectionReasonCode(str(item))
            for item in row["rejected_reason_codes"]
        ),
        "summary": tuple(str(item) for item in row["summary"]),
        "not_applied": tuple(str(item) for item in row["not_applied"]),
        "compilation_status": PreferenceCompilationStatus(
            str(row["compilation_status"])
        ),
        "plan_sha256": str(row["plan_sha256"]),
        "compiler_version": str(row["compiler_version"]),
    }


async def approve_compilation_candidate_v1(
    conn: asyncpg.Connection,
    *,
    owner_user_id: UUID,
    candidate_id: UUID,
    expected_revision: int,
) -> Any:
    current = await conn.fetchrow(
        """
        SELECT owner_user_id,
               revision,
               assistant_name,
               nickname,
               occupation,
               more_about_you,
               custom_instructions,
               response_length,
               technical_depth,
               response_format,
               conversation_style,
               updated_at
          FROM user_settings.assistant_response_preference_v1
         WHERE owner_user_id=$1
         FOR UPDATE
        """,
        owner_user_id,
    )
    actual_revision = int(current["revision"]) if current is not None else 0
    if actual_revision != expected_revision:
        raise AssistantResponsePreferenceConflictV1(
            "assistant response preferences changed before approval"
        )

    candidate = await conn.fetchrow(
        """
        SELECT *
          FROM user_settings.assistant_response_preference_compilation_candidate_v1
         WHERE owner_user_id=$1
           AND candidate_id=$2
         FOR UPDATE
        """,
        owner_user_id,
        candidate_id,
    )
    if (
        candidate is None
        or str(candidate["status"]) != "candidate"
        or int(candidate["source_revision"]) != expected_revision
        or candidate["expires_at"] <= datetime.now(timezone.utc)
    ):
        raise PreferenceCompilationCandidateUnavailableV1(
            "preference compilation candidate is unavailable"
        )
    values = _candidate_values(candidate)
    if (
        values["compilation_status"]
        is PreferenceCompilationStatus.REJECTED
    ):
        raise PreferenceCompilationCandidateUnavailableV1(
            "rejected preference compilation candidates cannot be approved"
        )

    current_length = (
        ResponseLength(str(current["response_length"]))
        if current is not None
        else ResponseLength.BALANCED
    )
    current_depth = (
        TechnicalDepth(str(current["technical_depth"]))
        if current is not None
        else TechnicalDepth.BALANCED
    )
    current_format = (
        ResponseFormat(str(current["response_format"]))
        if current is not None
        else ResponseFormat.AUTO
    )
    current_style = (
        ConversationStyle(str(current["conversation_style"]))
        if current is not None
        else ConversationStyle.NATURAL
    )
    response_length = values["response_length"] or current_length
    technical_depth = values["technical_depth"] or current_depth
    response_format = values["response_format"] or current_format
    conversation_style = values["conversation_style"] or current_style
    marker = compiled_preference_marker_v1(values["rule_ids"])
    is_clear = values["compilation_status"] is PreferenceCompilationStatus.CLEAR
    active_candidate_id = None if is_clear else candidate_id
    active_summary = [] if is_clear else list(values["summary"])
    active_rejections = [] if is_clear else list(values["not_applied"])
    active_plan_sha256 = None if is_clear else values["plan_sha256"]
    active_compiler_version = None if is_clear else values["compiler_version"]

    if current is None:
        row = await conn.fetchrow(
            """
            INSERT INTO user_settings.assistant_response_preference_v1 (
              owner_user_id,
              revision,
              preference_narrative,
              custom_instructions,
              response_length,
              technical_depth,
              response_format,
              conversation_style,
              active_compilation_candidate_id,
              active_compilation_summary,
              active_compilation_rejections,
              active_compilation_plan_sha256,
              active_compiler_version,
              active_compiled_at
            )
            VALUES (
              $1,1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
              CASE WHEN $8::uuid IS NULL THEN NULL ELSE clock_timestamp() END
            )
            RETURNING owner_user_id,
                      revision,
                      assistant_name,
                      nickname,
                      occupation,
                      more_about_you,
                      custom_instructions,
                      response_length,
                      technical_depth,
                      response_format,
                      conversation_style,
                      updated_at
            """,
            owner_user_id,
            values["source_narrative"] or None,
            marker,
            response_length.value,
            technical_depth.value,
            response_format.value,
            conversation_style.value,
            active_candidate_id,
            active_summary,
            active_rejections,
            active_plan_sha256,
            active_compiler_version,
        )
    else:
        row = await conn.fetchrow(
            """
            UPDATE user_settings.assistant_response_preference_v1
               SET revision=revision+1,
                   preference_narrative=$3,
                   custom_instructions=$4,
                   response_length=$5,
                   technical_depth=$6,
                   response_format=$7,
                   conversation_style=$8,
                   active_compilation_candidate_id=$9,
                   active_compilation_summary=$10,
                   active_compilation_rejections=$11,
                   active_compilation_plan_sha256=$12,
                   active_compiler_version=$13,
                   active_compiled_at=CASE
                     WHEN $9::uuid IS NULL THEN NULL
                     ELSE clock_timestamp()
                   END,
                   updated_at=clock_timestamp()
             WHERE owner_user_id=$1
               AND revision=$2
            RETURNING owner_user_id,
                      revision,
                      assistant_name,
                      nickname,
                      occupation,
                      more_about_you,
                      custom_instructions,
                      response_length,
                      technical_depth,
                      response_format,
                      conversation_style,
                      updated_at
            """,
            owner_user_id,
            expected_revision,
            values["source_narrative"] or None,
            marker,
            response_length.value,
            technical_depth.value,
            response_format.value,
            conversation_style.value,
            active_candidate_id,
            active_summary,
            active_rejections,
            active_plan_sha256,
            active_compiler_version,
        )
    if row is None:
        raise AssistantResponsePreferenceConflictV1(
            "assistant response preferences changed during approval"
        )
    await conn.execute(
        """
        UPDATE user_settings.assistant_response_preference_compilation_candidate_v1
           SET status='approved',
               approved_at=clock_timestamp()
         WHERE owner_user_id=$1
           AND candidate_id=$2
           AND status='candidate'
        """,
        owner_user_id,
        candidate_id,
    )
    return _record_from_row(row)


__all__ = [
    "PreferenceCompilationCandidateUnavailableV1",
    "approve_compilation_candidate_v1",
    "load_preference_public_state_v1",
    "store_compilation_candidate_v1",
]
