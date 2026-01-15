-- Minimal schema for CI canary

CREATE TABLE IF NOT EXISTS public.vantage_answer_trace (
  answer_id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  user_id text NOT NULL,
  thread_id uuid NULL,
  vantage_id text NOT NULL DEFAULT 'default',
  model_id text NULL,
  answer_text text NOT NULL,
  answer_text_hash text NULL,
  answer_text_len integer NULL,
  memory_ids text[] NOT NULL DEFAULT '{}'::text[],
  request_id text NULL
);

CREATE INDEX IF NOT EXISTS vantage_answer_trace_request_id_idx
  ON public.vantage_answer_trace (request_id);

CREATE TABLE IF NOT EXISTS telemetry_event (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  subject_type text NOT NULL,
  subject_id text NOT NULL,
  target_model_id text NULL,
  target_model_version text NULL,
  judge_model_id text NULL,
  judge_model_version text NULL,
  vantage_id text NULL,
  condition_id text NULL,
  thread_id text NULL,
  turn_id text NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now()
);
