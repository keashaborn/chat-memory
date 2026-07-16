BEGIN;

DO $migration$
DECLARE
  definition text;
  predecessor constant text := $new$IF entity_row.entity_type <> 'self'
           OR NOT (
             entity_row.entity_key = 'self'
             OR COALESCE(
               entity_row.metadata->>'identity_state', ''
             ) = 'trusted_owner_self'
           ) THEN$new$;
  replacement constant text := $old$IF entity_row.entity_type <> 'self' OR entity_row.entity_key <> 'self' THEN$old$;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'trusted-self apply rollback requires sage';
  END IF;
  SELECT pg_get_functiondef(
    'memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'::regprocedure
  ) INTO definition;
  IF position(replacement IN definition) > 0 THEN
    RETURN;
  END IF;
  IF position(predecessor IN definition) = 0 THEN
    RAISE EXCEPTION 'trusted-self apply rollback predecessor does not match';
  END IF;
  definition := replace(definition, predecessor, replacement);
  IF position(predecessor IN definition) > 0
     OR position(replacement IN definition) = 0 THEN
    RAISE EXCEPTION 'trusted-self apply rollback replacement was not exact';
  END IF;
  EXECUTE definition;
END
$migration$;

COMMIT;
