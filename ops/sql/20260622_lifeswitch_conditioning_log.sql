BEGIN;

CREATE TABLE IF NOT EXISTS lifeswitch_training.conditioning_session_log (
  conditioning_session_log_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id                    uuid NOT NULL,

  my_conditioning_prescription_id  uuid REFERENCES lifeswitch_training.my_conditioning_prescription(my_conditioning_prescription_id),

  day                              date NOT NULL,

  name                             text NOT NULL,
  category                         text NOT NULL DEFAULT '',
  modality                         text NOT NULL DEFAULT '',

  duration_min                     numeric NOT NULL DEFAULT 0,
  intensity                        text NOT NULL DEFAULT '',
  distance                         text NOT NULL DEFAULT '',
  heart_rate_avg                   numeric,
  recovery_impact                  text NOT NULL DEFAULT '',
  notes                            text NOT NULL DEFAULT '',

  is_active                        boolean NOT NULL DEFAULT true,

  created_at                       timestamptz NOT NULL DEFAULT now(),
  updated_at                       timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_conditioning_session_log_updated_at ON lifeswitch_training.conditioning_session_log;
CREATE TRIGGER trg_conditioning_session_log_updated_at
BEFORE UPDATE ON lifeswitch_training.conditioning_session_log
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_conditioning_session_owner_day
  ON lifeswitch_training.conditioning_session_log(owner_user_id, day DESC);

CREATE INDEX IF NOT EXISTS ix_conditioning_session_prescription
  ON lifeswitch_training.conditioning_session_log(my_conditioning_prescription_id);

CREATE INDEX IF NOT EXISTS ix_conditioning_session_owner_active
  ON lifeswitch_training.conditioning_session_log(owner_user_id, is_active, day DESC);

COMMIT;
