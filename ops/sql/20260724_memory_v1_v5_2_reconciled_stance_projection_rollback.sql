BEGIN;

DO $standard_dispatch_v2_compatibility_rollback$
DECLARE
  renderer_definition text;
  preflight_definition text;
  new_renderer constant text := $new$
    ELSIF source.predicate='stance.reported' THEN
      result_value := subject_label||' reports this position: "'||
        regexp_replace(
          btrim(source.object_literal::jsonb#>>'{value,position}'),
          '[[:space:]]+',' ','g'
        )||
        CASE WHEN right(regexp_replace(
          btrim(source.object_literal::jsonb#>>'{value,position}'),
          '[[:space:]]+',' ','g'
        ),1) ~ '[.!?]' THEN '' ELSE '.' END||'"';
$new$;
  old_renderer constant text := $old$
    ELSIF source.predicate='stance.reported' THEN
      result_value := subject_label||' reports the position that '||
        (source.object_literal::jsonb#>>'{value,position}')||'.';
$old$;
  new_preflight constant text :=
    $new$     OR packet->>'projector_version' NOT IN (
       'semantic_dispatch_v1','semantic_dispatch_v2'
     )$new$;
  old_preflight constant text :=
    $old$     OR packet->>'projector_version'<>'semantic_dispatch_v1'$old$;
BEGIN
  SELECT pg_get_functiondef(
    'memory.render_projection_claim_text_v5_2(uuid)'::regprocedure
  )
  INTO renderer_definition;
  SELECT pg_get_functiondef(
    'memory.preflight_projection_packet_v5_2(uuid,text)'::regprocedure
  )
  INTO preflight_definition;
  IF length(renderer_definition)
       - length(replace(renderer_definition, new_renderer, ''))
       <> length(new_renderer)
     OR length(preflight_definition)
       - length(replace(preflight_definition, new_preflight, ''))
       <> length(new_preflight) THEN
    RAISE EXCEPTION 'standard V5.2 dispatch v2 compatibility is absent or drifted';
  END IF;
  EXECUTE replace(renderer_definition, new_renderer, old_renderer);
  EXECUTE replace(preflight_definition, new_preflight, old_preflight);
END
$standard_dispatch_v2_compatibility_rollback$;

DO $guard_compatibility_rollback$
DECLARE
  definition text;
  new_fragment constant text := $new$
        OR (
          item.object_kind = 'literal'
          AND memory.v5_digest_text(memory.v5_canonical_json_text(
            observation.object_literal
          )) <> item.object_literal_sha256
          AND NOT (
            link.stance = 'context'
            AND item.predicate = 'stance.reported'
            AND EXISTS (
              SELECT 1
              FROM memory.projection_plan AS plan
              WHERE plan.owner_user_id = item.owner_user_id
                AND plan.plan_id = item.plan_id
                AND plan.projector =
                      'memory_v1_deterministic_projection_v5_2'
                AND plan.projector_version = 'stance_reconciliation_v1'
            )
          )
        )
$new$;
  old_fragment constant text := $old$
        OR (
          item.object_kind = 'literal'
          AND memory.v5_digest_text(memory.v5_canonical_json_text(
            observation.object_literal
          )) <> item.object_literal_sha256
        )
$old$;
BEGIN
  SELECT pg_get_functiondef(
    'memory.guard_projection_item_complete_v5()'::regprocedure
  )
  INTO definition;
  IF length(definition) - length(replace(definition, new_fragment, ''))
       <> length(new_fragment) THEN
    RAISE EXCEPTION 'reconciled projection guard compatibility is absent or drifted';
  END IF;
  EXECUTE replace(definition, new_fragment, old_fragment);
END
$guard_compatibility_rollback$;

DROP FUNCTION IF EXISTS
  memory.stage_reconciled_stance_projection_v5_2(uuid, text, text);
DROP FUNCTION IF EXISTS
  memory.preflight_reconciled_stance_projection_v5_2(uuid, text);
DROP FUNCTION IF EXISTS
  memory.render_reconciled_stance_claim_text_v5_2(uuid);

COMMIT;
