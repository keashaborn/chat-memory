\set ON_ERROR_STOP on

BEGIN READ ONLY;

DO $fixture$
DECLARE owner_a uuid; owner_b uuid;
BEGIN
  SELECT owner_user_id INTO owner_a
  FROM memory.observation
  GROUP BY owner_user_id ORDER BY count(*) DESC,owner_user_id LIMIT 1;
  SELECT owner_user_id INTO owner_b
  FROM memory.observation
  WHERE owner_user_id<>owner_a
  GROUP BY owner_user_id ORDER BY count(*) DESC,owner_user_id LIMIT 1;
  IF owner_a IS NULL OR owner_b IS NULL THEN
    RAISE EXCEPTION 'production clone requires two observed owners';
  END IF;
  PERFORM set_config('test.owner_a',owner_a::text,false);
  PERFORM set_config('test.owner_b',owner_b::text,false);
  PERFORM set_config('test.owner_a_hash',
    memory.v5_digest_text(owner_a::text),false);
  PERFORM set_config('test.owner_b_hash',
    memory.v5_digest_text(owner_b::text),false);
  PERFORM set_config('test.owner_a_count',(
    SELECT count(*)::text FROM memory.observation AS observation
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=observation.owner_user_id
     AND evidence.evidence_id=observation.evidence_id
     AND evidence.status='active'
    WHERE observation.owner_user_id=owner_a
  ),false);
  PERFORM set_config('test.owner_b_count',(
    SELECT count(*)::text FROM memory.observation AS observation
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=observation.owner_user_id
     AND evidence.evidence_id=observation.evidence_id
     AND evidence.status='active'
    WHERE observation.owner_user_id=owner_b
  ),false);
END
$fixture$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',current_setting('test.owner_a'),false);

DO $owner_a$
DECLARE first_value jsonb; second_value jsonb;
BEGIN
  first_value:=memory.load_pattern_salience_shadow_inputs_v5_1(256,256);
  second_value:=memory.load_pattern_salience_shadow_inputs_v5_1(256,256);
  IF first_value<>second_value
     OR first_value->>'contract_version'<>
       'memory_v1_pattern_salience_shadow_input_v5_1'
     OR first_value->>'owner_user_id_sha256'<>
       current_setting('test.owner_a_hash')
     OR (first_value->>'observation_total')::integer<>
       current_setting('test.owner_a_count')::integer
     OR first_value ?| ARRAY[
       'owner_user_id','content','canonical_text','metadata','rationale','value'
     ] THEN
    RAISE EXCEPTION 'owner A shadow input contract failed';
  END IF;
  PERFORM set_config('test.owner_a_packet',first_value::text,false);
END
$owner_a$;

DO $direct_read_denied$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    PERFORM 1 FROM memory.claim_observation LIMIT 1;
  EXCEPTION WHEN insufficient_privilege THEN denied:=true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'brains_app gained direct reference-table access';
  END IF;
END
$direct_read_denied$;

SELECT set_config('app.user_id',current_setting('test.owner_b'),false);
DO $owner_b$
DECLARE value jsonb;
BEGIN
  value:=memory.load_pattern_salience_shadow_inputs_v5_1(256,256);
  IF value->>'owner_user_id_sha256'<>
       current_setting('test.owner_b_hash')
     OR (value->>'observation_total')::integer<>
       current_setting('test.owner_b_count')::integer THEN
    RAISE EXCEPTION 'owner B shadow input contract failed';
  END IF;
  PERFORM set_config('test.owner_b_packet',value::text,false);
END
$owner_b$;

DO $invalid_limit_denied$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    PERFORM memory.load_pattern_salience_shadow_inputs_v5_1(257,256);
  EXCEPTION WHEN invalid_parameter_value THEN denied:=true;
  END;
  IF NOT denied THEN RAISE EXCEPTION 'unsafe input limit was accepted'; END IF;
END
$invalid_limit_denied$;

SELECT set_config('app.user_id','',false);
DO $missing_actor_denied$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    PERFORM memory.load_pattern_salience_shadow_inputs_v5_1(256,256);
  EXCEPTION WHEN OTHERS THEN denied:=true;
  END;
  IF NOT denied THEN RAISE EXCEPTION 'missing actor was accepted'; END IF;
END
$missing_actor_denied$;

RESET SESSION AUTHORIZATION;

DO $isolation$
DECLARE packet_a jsonb:=current_setting('test.owner_a_packet')::jsonb;
        packet_b jsonb:=current_setting('test.owner_b_packet')::jsonb;
        owner_a uuid:=current_setting('test.owner_a')::uuid;
        owner_b uuid:=current_setting('test.owner_b')::uuid;
BEGIN
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(packet_a->'observations') AS item
    LEFT JOIN memory.observation AS observation
      ON observation.observation_id=(item->>'observation_id')::uuid
     AND observation.owner_user_id=owner_a
    WHERE observation.observation_id IS NULL
  ) OR EXISTS (
    SELECT 1 FROM jsonb_array_elements(packet_b->'observations') AS item
    LEFT JOIN memory.observation AS observation
      ON observation.observation_id=(item->>'observation_id')::uuid
     AND observation.owner_user_id=owner_b
    WHERE observation.observation_id IS NULL
  ) THEN
    RAISE EXCEPTION 'cross-owner observation entered a shadow packet';
  END IF;
  IF packet_a->>'owner_user_id_sha256'=packet_b->>'owner_user_id_sha256' THEN
    RAISE EXCEPTION 'distinct owner bindings collapsed';
  END IF;
END
$isolation$;

ROLLBACK;
