BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'evidence privilege rollback requires sage';
  END IF;
  IF to_regclass('memory.evidence') IS NULL
     OR to_regrole('brains_app') IS NULL THEN
    RAISE EXCEPTION 'evidence privilege rollback prerequisites are absent';
  END IF;
END
$guard$;

GRANT INSERT ON memory.evidence TO brains_app;

COMMIT;
