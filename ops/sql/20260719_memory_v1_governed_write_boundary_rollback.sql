BEGIN;

DO $guard$
DECLARE
  table_name text;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'governed write-boundary rollback requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL THEN
    RAISE EXCEPTION 'brains_app role is absent';
  END IF;
  FOREACH table_name IN ARRAY ARRAY[
    'entity',
    'entity_alias',
    'candidate',
    'claim',
    'claim_revision',
    'claim_evidence',
    'claim_assessment',
    'claim_relation'
  ]
  LOOP
    IF to_regclass(format('memory.%I', table_name)) IS NULL THEN
      RAISE EXCEPTION 'required governed table memory.% is absent', table_name;
    END IF;
  END LOOP;
END
$guard$;

GRANT SELECT, INSERT, UPDATE, DELETE ON
  memory.entity,
  memory.entity_alias,
  memory.candidate,
  memory.claim,
  memory.claim_revision,
  memory.claim_evidence,
  memory.claim_assessment,
  memory.claim_relation
TO brains_app;

COMMIT;
