from __future__ import annotations

"""Owner-scoped PostgreSQL storage for explicit AI response preferences."""

from typing import Any
from uuid import UUID

import asyncpg

from rag_engine.assistant_response_preferences_v1 import (
    AssistantResponsePreferencesInputV1,
    AssistantResponsePreferencesV1,
    ConversationStyle,
    PreferenceSource,
    ResponseFormat,
    ResponseLength,
    TechnicalDepth,
    default_assistant_response_preferences_v1,
)


class AssistantResponsePreferenceConflictV1(RuntimeError):
    """The caller attempted to overwrite a newer preference revision."""


async def set_preference_actor_v1(
    conn: asyncpg.Connection,
    owner_user_id: UUID,
) -> None:
    await conn.execute(
        "SELECT set_config('app.user_id',$1,true)",
        str(owner_user_id),
    )


def _record_from_row(row: Any) -> AssistantResponsePreferencesV1:
    return AssistantResponsePreferencesV1(
        owner_user_id=row["owner_user_id"],
        revision=int(row["revision"]),
        source=PreferenceSource.POSTGRES,
        updated_at=row["updated_at"],
        assistant_name=row["assistant_name"],
        nickname=row["nickname"],
        occupation=row["occupation"],
        more_about_you=row["more_about_you"],
        custom_instructions=row["custom_instructions"],
        response_length=ResponseLength(str(row["response_length"])),
        technical_depth=TechnicalDepth(str(row["technical_depth"])),
        response_format=ResponseFormat(str(row["response_format"])),
        conversation_style=ConversationStyle(str(row["conversation_style"])),
    )


async def load_assistant_response_preferences_v1(
    conn: asyncpg.Connection,
    owner_user_id: UUID,
) -> AssistantResponsePreferencesV1:
    row = await conn.fetchrow(
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
        """,
        owner_user_id,
    )
    if row is None:
        return default_assistant_response_preferences_v1(owner_user_id)
    return _record_from_row(row)


async def save_assistant_response_preferences_v1(
    conn: asyncpg.Connection,
    owner_user_id: UUID,
    value: AssistantResponsePreferencesInputV1,
) -> AssistantResponsePreferencesV1:
    fields = (
        owner_user_id,
        value.assistant_name,
        value.nickname,
        value.occupation,
        value.more_about_you,
        value.custom_instructions,
        value.response_length.value,
        value.technical_depth.value,
        value.response_format.value,
        value.conversation_style.value,
    )
    if value.expected_revision == 0:
        row = await conn.fetchrow(
            """
            INSERT INTO user_settings.assistant_response_preference_v1 (
              owner_user_id,
              revision,
              assistant_name,
              nickname,
              occupation,
              more_about_you,
              custom_instructions,
              response_length,
              technical_depth,
              response_format,
              conversation_style
            )
            VALUES ($1,1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (owner_user_id) DO NOTHING
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
            *fields,
        )
    else:
        row = await conn.fetchrow(
            """
            UPDATE user_settings.assistant_response_preference_v1
               SET revision=revision+1,
                   assistant_name=$3,
                   nickname=$4,
                   occupation=$5,
                   more_about_you=$6,
                   custom_instructions=$7,
                   response_length=$8,
                   technical_depth=$9,
                   response_format=$10,
                   conversation_style=$11,
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
            value.expected_revision,
            *fields[1:],
        )
    if row is None:
        raise AssistantResponsePreferenceConflictV1(
            "assistant response preferences changed since they were loaded"
        )
    return _record_from_row(row)


__all__ = [
    "AssistantResponsePreferenceConflictV1",
    "load_assistant_response_preferences_v1",
    "save_assistant_response_preferences_v1",
    "set_preference_actor_v1",
]
