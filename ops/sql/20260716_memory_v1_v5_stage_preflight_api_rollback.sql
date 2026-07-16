BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 stage preflight rollback requires sage';
  END IF;
END
$guard$;

DROP FUNCTION memory.preflight_relational_stage_bundle_v5(
  uuid,text,text,timestamptz
);

COMMIT;
