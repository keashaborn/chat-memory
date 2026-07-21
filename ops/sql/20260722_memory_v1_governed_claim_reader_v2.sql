BEGIN;

CREATE OR REPLACE FUNCTION memory.read_governed_claims_v2(p_claim_ids uuid[])
RETURNS TABLE(
  owner_user_id uuid,
  claim_id uuid,
  revision_id uuid,
  canonical_key text,
  canonical_text text,
  predicate text,
  status text,
  sensitivity text,
  importance numeric,
  salience numeric,
  valid_from timestamptz,
  valid_to timestamptz,
  superseded_by uuid,
  metadata jsonb,
  retrieval_policy jsonb,
  projection_review_decision text,
  projection_apply_outcome text,
  evidence_by_stance jsonb,
  observation_ids text[],
  project_key text,
  component_key text,
  source_content_sha256 text,
  subject_entity_id uuid,
  subject_entity_type text,
  object_entity_id uuid,
  object_entity_type text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
BEGIN
  actor := memory.require_v5_reader_context();

  RETURN QUERY
  SELECT source.owner_user_id,
         source.claim_id,
         NULL::uuid AS revision_id,
         source.canonical_key,
         source.canonical_text,
         source.predicate,
         source.status,
         source.sensitivity,
         source.importance,
         source.salience,
         source.valid_from,
         source.valid_to,
         NULL::uuid AS superseded_by,
         source.metadata,
         source.retrieval_policy,
         source.projection_review_decision,
         source.projection_apply_outcome,
         source.evidence_by_stance,
         source.observation_ids,
         source.project_key,
         NULL::text AS component_key,
         encode(
           public.digest(
             jsonb_build_object(
               'canonical_key',source.canonical_key,
               'canonical_text',source.canonical_text,
               'predicate',source.predicate,
               'status',source.status,
               'sensitivity',source.sensitivity,
               'valid_from',source.valid_from,
               'valid_to',source.valid_to,
               'metadata',source.metadata,
               'retrieval_policy',source.retrieval_policy,
               'subject_entity_id',claim.subject_entity_id,
               'object_entity_id',claim.object_entity_id
             )::text,
             'sha256'
           ),
           'hex'
         ) AS source_content_sha256,
         claim.subject_entity_id,
         subject_entity.entity_type AS subject_entity_type,
         claim.object_entity_id,
         object_entity.entity_type AS object_entity_type
    FROM memory.read_v5_shadow_claims(p_claim_ids) AS source
    JOIN memory.claim AS claim
      ON claim.owner_user_id=source.owner_user_id
     AND claim.claim_id=source.claim_id
    JOIN memory.entity AS subject_entity
      ON subject_entity.owner_user_id=claim.owner_user_id
     AND subject_entity.entity_id=claim.subject_entity_id
    LEFT JOIN memory.entity AS object_entity
      ON object_entity.owner_user_id=claim.owner_user_id
     AND object_entity.entity_id=claim.object_entity_id
   WHERE source.owner_user_id=actor
   ORDER BY source.claim_id;
END
$function$;

ALTER FUNCTION memory.read_governed_claims_v2(uuid[]) OWNER TO memory_v5_reader;
REVOKE ALL ON FUNCTION memory.read_governed_claims_v2(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.read_governed_claims_v2(uuid[]) TO brains_app;

COMMIT;
