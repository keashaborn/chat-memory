from __future__ import annotations

"""Canonical PostgreSQL effect owner for SeeBx telemetry."""

import json
from typing import Any, Dict, List, Sequence, Tuple

from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.capabilities.observability.telemetry_contracts import (
    TelemetryEventRecordV1,
    TelemetryTimeseriesSnapshotV1,
)


SET_ACTOR_SQL = "SELECT set_config('app.user_id',$1,true)"
TELEMETRY_EVENT_INSERT_SQL = """
INSERT INTO telemetry_event (
event_id, event_type,
subject_type, subject_id,
target_model_id, target_model_version,
judge_model_id, judge_model_version,
condition_id,
thread_id, turn_id,
actor_user_id,
payload, occurred_at
)
VALUES (
$1,$2,
$3,$4,
$5,$6,
$7,$8,
$9,
$10,$11,
$12,
$13,$14
)
ON CONFLICT (event_id) DO NOTHING
"""
CONDITION_BASE_SQL = """
SELECT condition_id, occurred_at, payload
FROM telemetry_event
WHERE subject_type=$1 AND subject_id=$2 AND actor_user_id=$3
AND event_type='condition.set' AND occurred_at < $4
ORDER BY occurred_at DESC
LIMIT 1
"""
CONDITION_WITHIN_SQL = """
SELECT condition_id, occurred_at, payload
FROM telemetry_event
WHERE subject_type=$1 AND subject_id=$2 AND actor_user_id=$3
AND event_type='condition.set'
AND occurred_at >= $4 AND occurred_at < $5
ORDER BY occurred_at ASC
"""
VOICE_SLO_SQL = """
WITH voice AS (
  SELECT
    occurred_at,
    payload->>'status' AS status,
    payload->>'failure_stage' AS failure_stage,
    payload->>'failure_code' AS failure_code,
    CASE
      WHEN payload->>'transcription_ms' ~ '^[0-9]+$'
      THEN (payload->>'transcription_ms')::double precision
    END AS transcription_ms,
    CASE
      WHEN payload->>'response_ms' ~ '^[0-9]+$'
      THEN (payload->>'response_ms')::double precision
    END AS response_ms,
    CASE
      WHEN payload->>'tts_first_audio_ms' ~ '^[0-9]+$'
      THEN (payload->>'tts_first_audio_ms')::double precision
    END AS tts_first_audio_ms,
    CASE
      WHEN payload->>'speech_to_first_audio_basis'
           = 'detected_speech_end_v1'
       AND payload->>'speech_to_first_audio_ms' ~ '^[0-9]+$'
      THEN
        (payload->>'speech_to_first_audio_ms')::double precision
      WHEN coalesce(
             payload->>'speech_to_first_audio_basis',
             ''
           ) IN ('', 'synthetic_turn_start_v1')
       AND payload->>'speech_to_first_audio_ms' ~ '^[0-9]+$'
       AND payload->>'speech_ms' ~ '^[0-9]+$'
      THEN greatest(
        (payload->>'speech_to_first_audio_ms')::double precision
        - (payload->>'speech_ms')::double precision,
        0
      )
    END AS end_of_speech_to_first_audio_ms
  FROM public.telemetry_event
  WHERE event_type='voice.turn.trace'
    AND actor_user_id=$1
    AND occurred_at >=
      clock_timestamp()-make_interval(days => $2)
)
SELECT
  max(occurred_at) AS latest_sample_at,
  (
    SELECT status
    FROM voice
    ORDER BY occurred_at DESC
    LIMIT 1
  ) AS latest_status,
  (
    SELECT count(*)::bigint
    FROM voice AS successful
    WHERE successful.status='completed'
      AND successful.occurred_at > coalesce(
        (
          SELECT max(interrupted.occurred_at)
          FROM voice AS interrupted
          WHERE interrupted.status <> 'completed'
        ),
        '-infinity'::timestamptz
      )
  ) AS consecutive_successes,
  (
    SELECT occurred_at
    FROM voice
    WHERE status='failed'
    ORDER BY occurred_at DESC
    LIMIT 1
  ) AS latest_failure_at,
  (
    SELECT failure_stage
    FROM voice
    WHERE status='failed'
    ORDER BY occurred_at DESC
    LIMIT 1
  ) AS latest_failure_stage,
  (
    SELECT failure_code
    FROM voice
    WHERE status='failed'
    ORDER BY occurred_at DESC
    LIMIT 1
  ) AS latest_failure_code,
  count(*)::bigint AS total,
  count(*) FILTER (
    WHERE status='completed'
  )::bigint AS completed,
  count(*) FILTER (
    WHERE status='failed'
  )::bigint AS failed,
  count(*) FILTER (
    WHERE status='cancelled'
  )::bigint AS cancelled,
  count(*) FILTER (
    WHERE failure_stage='transcription'
  )::bigint AS transcription_failures,
  count(*) FILTER (
    WHERE failure_stage='response'
  )::bigint AS response_failures,
  count(*) FILTER (
    WHERE failure_stage='tts'
  )::bigint AS tts_failures,
  percentile_cont(0.50) WITHIN GROUP (
    ORDER BY transcription_ms
  ) FILTER (
    WHERE status='completed'
      AND transcription_ms IS NOT NULL
  ) AS transcription_ms_p50,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY transcription_ms
  ) FILTER (
    WHERE status='completed'
      AND transcription_ms IS NOT NULL
  ) AS transcription_ms_p95,
  percentile_cont(0.99) WITHIN GROUP (
    ORDER BY transcription_ms
  ) FILTER (
    WHERE status='completed'
      AND transcription_ms IS NOT NULL
  ) AS transcription_ms_p99,
  percentile_cont(0.50) WITHIN GROUP (
    ORDER BY response_ms
  ) FILTER (
    WHERE status='completed'
      AND response_ms IS NOT NULL
  ) AS response_ms_p50,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY response_ms
  ) FILTER (
    WHERE status='completed'
      AND response_ms IS NOT NULL
  ) AS response_ms_p95,
  percentile_cont(0.99) WITHIN GROUP (
    ORDER BY response_ms
  ) FILTER (
    WHERE status='completed'
      AND response_ms IS NOT NULL
  ) AS response_ms_p99,
  percentile_cont(0.50) WITHIN GROUP (
    ORDER BY tts_first_audio_ms
  ) FILTER (
    WHERE status='completed'
      AND tts_first_audio_ms IS NOT NULL
  ) AS tts_first_audio_ms_p50,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY tts_first_audio_ms
  ) FILTER (
    WHERE status='completed'
      AND tts_first_audio_ms IS NOT NULL
  ) AS tts_first_audio_ms_p95,
  percentile_cont(0.99) WITHIN GROUP (
    ORDER BY tts_first_audio_ms
  ) FILTER (
    WHERE status='completed'
      AND tts_first_audio_ms IS NOT NULL
  ) AS tts_first_audio_ms_p99,
  percentile_cont(0.50) WITHIN GROUP (
    ORDER BY end_of_speech_to_first_audio_ms
  ) FILTER (
    WHERE status='completed'
      AND end_of_speech_to_first_audio_ms IS NOT NULL
  ) AS end_of_speech_to_first_audio_ms_p50,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY end_of_speech_to_first_audio_ms
  ) FILTER (
    WHERE status='completed'
      AND end_of_speech_to_first_audio_ms IS NOT NULL
  ) AS end_of_speech_to_first_audio_ms_p95,
  percentile_cont(0.99) WITHIN GROUP (
    ORDER BY end_of_speech_to_first_audio_ms
  ) FILTER (
    WHERE status='completed'
      AND end_of_speech_to_first_audio_ms IS NOT NULL
  ) AS end_of_speech_to_first_audio_ms_p99
FROM voice
"""


def _metric_expr(metric_key: str) -> Tuple[str, str]:
    defs: Dict[str, Tuple[str, str]] = {
        "probe_overall": (
            "NULLIF(payload->'scores'->>'overall','')::double precision",
            "event_type IN ('probe.response','chat.response')",
        ),
        "hallucination_rate": (
            "CASE WHEN (payload->'flags'->>'hallucination')='true' THEN 1.0 "
            "WHEN (payload->'flags'->>'hallucination')='false' THEN 0.0 "
            "ELSE NULL END",
            "event_type IN ('probe.response','chat.response')",
        ),
        "concession_rate": (
            "CASE WHEN (payload->'flags'->>'concession')='true' THEN 1.0 "
            "WHEN (payload->'flags'->>'concession')='false' THEN 0.0 "
            "ELSE NULL END",
            "event_type IN ('probe.response','chat.response')",
        ),
        "clarification_rate": (
            "CASE WHEN (payload->'flags'->>'clarification')='true' THEN 1.0 "
            "WHEN (payload->'flags'->>'clarification')='false' THEN 0.0 "
            "ELSE NULL END",
            "event_type IN ('probe.response','chat.response')",
        ),
        "style_drift": (
            "NULLIF(payload->'scores'->>'style_drift','')::double precision",
            "event_type IN ('probe.response','chat.response')",
        ),
        "refusal_rate": (
            "CASE WHEN (payload->'flags'->>'refusal')='true' THEN 1.0 "
            "WHEN (payload->'flags'->>'refusal')='false' THEN 0.0 "
            "ELSE NULL END",
            "event_type IN ('probe.response','chat.response')",
        ),
    }
    if metric_key not in defs:
        raise KeyError(metric_key)
    return defs[metric_key]


class PostgresTelemetryRepository:
    """Own telemetry connection, transaction, codec, RLS, and SQL effects."""

    def __init__(self, provider: PostgresConnectionProvider) -> None:
        self._provider = provider

    @staticmethod
    async def _configure_connection(connection: Any) -> None:
        await connection.set_type_codec(
            "json",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        await connection.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    async def write_events(
        self,
        *,
        actor_user_id: str,
        events: Sequence[TelemetryEventRecordV1],
    ) -> None:
        async with self._provider.connection() as connection:
            await self._configure_connection(connection)
            async with connection.transaction():
                await connection.execute(SET_ACTOR_SQL, actor_user_id)
                for event in events:
                    await connection.execute(
                        TELEMETRY_EVENT_INSERT_SQL,
                        *event.postgres_arguments(),
                    )

    async def read_timeseries(
        self,
        *,
        actor_user_id: str,
        metric_key: str,
        subject_type: str,
        subject_id: str,
        start: Any,
        end: Any,
        bucket: str,
        target_model_id: str | None,
    ) -> TelemetryTimeseriesSnapshotV1:
        expr, default_where = _metric_expr(metric_key)
        wh = [
            "subject_type=$1",
            "subject_id=$2",
            "occurred_at >= $3",
            "occurred_at < $4",
            default_where,
            "actor_user_id=$5",
        ]
        params: List[Any] = [
            subject_type,
            subject_id,
            start,
            end,
            actor_user_id,
        ]
        if target_model_id:
            wh.append("target_model_id=$6")
            params.append(target_model_id)
        where_sql = " AND ".join(f"({clause})" for clause in wh)
        dt_unit = "day" if bucket == "day" else "hour"
        query = f"""
            SELECT
              date_trunc('{dt_unit}', occurred_at) AS t,
              AVG({expr}) AS v,
              COUNT({expr}) AS n
            FROM telemetry_event
            WHERE {where_sql}
            GROUP BY 1
            ORDER BY 1
        """

        async with self._provider.connection() as connection:
            await self._configure_connection(connection)
            async with connection.transaction():
                await connection.execute(SET_ACTOR_SQL, actor_user_id)
                point_rows = await connection.fetch(query, *params)
                base = await connection.fetchrow(
                    CONDITION_BASE_SQL,
                    subject_type,
                    subject_id,
                    actor_user_id,
                    start,
                )
                within = await connection.fetch(
                    CONDITION_WITHIN_SQL,
                    subject_type,
                    subject_id,
                    actor_user_id,
                    start,
                    end,
                )
        conditions = ([base] if base else []) + list(within)
        return TelemetryTimeseriesSnapshotV1(
            point_rows=tuple(point_rows),
            condition_rows=tuple(conditions),
        )

    async def read_voice_slo(
        self,
        *,
        actor_user_id: str,
        window_days: int,
    ) -> Any:
        async with self._provider.connection() as connection:
            await self._configure_connection(connection)
            async with connection.transaction():
                await connection.execute(SET_ACTOR_SQL, actor_user_id)
                return await connection.fetchrow(
                    VOICE_SLO_SQL,
                    actor_user_id,
                    window_days,
                )


__all__ = [
    "CONDITION_BASE_SQL",
    "CONDITION_WITHIN_SQL",
    "PostgresTelemetryRepository",
    "SET_ACTOR_SQL",
    "TELEMETRY_EVENT_INSERT_SQL",
    "VOICE_SLO_SQL",
]
