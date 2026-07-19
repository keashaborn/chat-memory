\set ON_ERROR_STOP on
BEGIN;
SET LOCAL statement_timeout='30s';

DO $catalog$
DECLARE
  definition text;
  rendered text;
BEGIN
  definition:=pg_get_functiondef(
    'memory.preflight_claim_projection_source_v5_1(uuid)'::regprocedure
  );
  IF position(
       '''relationship.parent_of'',''relationship.sibling_of'''
       IN definition
     )=0
     OR position(
       'observation.predicate IN (''relationship.has_pet'',''relationship.parent_of'',''relationship.sibling_of'')'
       IN definition
     )=0 THEN
    RAISE EXCEPTION 'relationship claim source policy is absent';
  END IF;
  FOREACH definition IN ARRAY ARRAY[
    pg_get_functiondef(
      'memory.plan_owner_v5_local_claim_projection_v1(integer)'::regprocedure
    ),
    pg_get_functiondef(
      'memory.register_owner_v5_local_claim_projection_v1(uuid,uuid,uuid,uuid,text,text,text,text)'::regprocedure
    )
  ] LOOP
    IF position(
         '''relationship.parent_of'',''relationship.sibling_of'''
         IN definition
       )=0 THEN
      RAISE EXCEPTION 'local relationship claim policy is absent';
    END IF;
  END LOOP;

  rendered:=memory.render_claim_projection_text_v5_1(
    'person','Dad','relationship.parent_of','entity','self','Self',NULL
  );
  IF rendered<>'Dad is the user''s parent.' THEN
    RAISE EXCEPTION 'parent rendering drifted';
  END IF;
  rendered:=memory.render_claim_projection_text_v5_1(
    'self','Self','relationship.sibling_of','entity','person','Avery',NULL
  );
  IF rendered<>'The user and Avery are siblings.' THEN
    RAISE EXCEPTION 'sibling rendering drifted';
  END IF;

  BEGIN
    PERFORM memory.render_claim_projection_text_v5_1(
      'self','Self','relationship.parent_of','entity','person','Dad',NULL
    );
    RAISE EXCEPTION 'reversed parent relationship rendered';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
END
$catalog$;

ROLLBACK;
SELECT 'memory_v1_v5_relationship_claim_projection: PASS' AS result;
