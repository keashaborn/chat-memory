\set ON_ERROR_STOP on

DO $$
DECLARE
  missing text;
BEGIN
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname)
  INTO missing
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'memory'
    AND c.relkind = 'r'
    AND c.relname <> 'predicate'
    AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity);

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'tables missing enabled+forced RLS: %', missing;
  END IF;
END
$$;

BEGIN;
SET LOCAL ROLE brains_app;

SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

INSERT INTO memory.entity(
  entity_id, owner_user_id, entity_key, entity_type, canonical_name, normalized_name
)
VALUES (
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  '11111111-1111-4111-8111-111111111111',
  'self',
  'person',
  'Actor A',
  'actor a'
)
RETURNING entity_id AS actor_a_entity_id \gset

INSERT INTO memory.evidence(
  owner_user_id, kind, source_system, external_id, content, directness
)
VALUES (
  '11111111-1111-4111-8111-111111111111',
  'user_statement',
  'memory_v1_rls_test',
  'actor-a-evidence',
  'My preferred name is Actor A.',
  1.000
)
RETURNING evidence_id AS actor_a_evidence_id \gset

INSERT INTO memory.claim(
  owner_user_id,
  subject_entity_id,
  predicate,
  object_literal,
  canonical_text,
  canonical_key,
  status,
  confidence,
  importance,
  salience,
  sensitivity
)
VALUES (
  '11111111-1111-4111-8111-111111111111',
  :'actor_a_entity_id',
  'identity.preferred_name',
  '{"type":"str","v":"Actor A"}'::jsonb,
  'The user prefers the name Actor A.',
  'rls-test:actor-a:preferred-name',
  'supported',
  0.990,
  0.800,
  0.800,
  'low'
)
RETURNING claim_id AS actor_a_claim_id \gset

INSERT INTO memory.claim_evidence(
  owner_user_id, claim_id, evidence_id, stance, relevance
)
VALUES (
  '11111111-1111-4111-8111-111111111111',
  :'actor_a_claim_id',
  :'actor_a_evidence_id',
  'supports',
  1.000
);

DO $$
BEGIN
  IF (SELECT count(*) FROM memory.claim) <> 1 THEN
    RAISE EXCEPTION 'actor A should see exactly one claim';
  END IF;
END
$$;

SELECT set_config('app.user_id', '22222222-2222-4222-8222-222222222222', true);

DO $$
DECLARE
  blocked boolean := false;
  affected integer := 0;
BEGIN
  IF (SELECT count(*) FROM memory.claim) <> 0 THEN
    RAISE EXCEPTION 'actor B can see actor A claims';
  END IF;

  IF (SELECT count(*) FROM memory.evidence) <> 0 THEN
    RAISE EXCEPTION 'actor B can see actor A evidence';
  END IF;

  UPDATE memory.entity
  SET canonical_name = 'Cross-user update'
  WHERE entity_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  GET DIAGNOSTICS affected = ROW_COUNT;
  IF affected <> 0 THEN
    RAISE EXCEPTION 'actor B updated actor A entity';
  END IF;

  DELETE FROM memory.entity
  WHERE entity_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  GET DIAGNOSTICS affected = ROW_COUNT;
  IF affected <> 0 THEN
    RAISE EXCEPTION 'actor B deleted actor A entity';
  END IF;

  BEGIN
    INSERT INTO memory.entity(
      owner_user_id, entity_key, entity_type, canonical_name, normalized_name
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'cross-owner-write',
      'person',
      'Forbidden',
      'forbidden'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;

  IF NOT blocked THEN
    RAISE EXCEPTION 'actor B wrote actor A entity';
  END IF;
END
$$;

INSERT INTO memory.entity(
  entity_id, owner_user_id, entity_key, entity_type, canonical_name, normalized_name
)
VALUES (
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
  '22222222-2222-4222-8222-222222222222',
  'self',
  'person',
  'Actor B',
  'actor b'
);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.claim(
      owner_user_id,
      subject_entity_id,
      predicate,
      object_literal,
      canonical_text,
      canonical_key
    ) VALUES (
      '22222222-2222-4222-8222-222222222222',
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      'identity.preferred_name',
      '{"type":"str","v":"Forbidden"}'::jsonb,
      'Forbidden cross-owner claim.',
      'rls-test:cross-owner-subject'
    );
  EXCEPTION WHEN foreign_key_violation THEN
    blocked := true;
  END;

  IF NOT blocked THEN
    RAISE EXCEPTION 'cross-owner entity/claim link was accepted';
  END IF;
END
$$;

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.predicate(predicate, object_kind, cardinality)
    VALUES ('forbidden.runtime.predicate', 'literal', 'one');
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;

  IF NOT blocked THEN
    RAISE EXCEPTION 'runtime role modified predicate registry';
  END IF;
END
$$;

RESET app.user_id;

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  IF (SELECT count(*) FROM memory.claim) <> 0 THEN
    RAISE EXCEPTION 'unset actor can see claims';
  END IF;

  BEGIN
    INSERT INTO memory.entity(
      owner_user_id, entity_key, entity_type, canonical_name, normalized_name
    ) VALUES (
      '22222222-2222-4222-8222-222222222222',
      'missing-actor-write',
      'person',
      'Forbidden',
      'forbidden'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;

  IF NOT blocked THEN
    RAISE EXCEPTION 'unset actor wrote an entity';
  END IF;
END
$$;

ROLLBACK;

SELECT 'memory_v1_rls: PASS' AS result;
