\set ON_ERROR_STOP on

CREATE OR REPLACE FUNCTION pg_temp.table_state(target regclass)
RETURNS TABLE(row_count bigint, row_md5 text)
LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY EXECUTE format(
    'SELECT count(*)::bigint, '
    'md5(coalesce(string_agg(to_jsonb(value)::text, '''' '
    'ORDER BY to_jsonb(value)::text), '''')) '
    'FROM %s value',
    target
  );
END
$function$;

SELECT
  namespace.nspname || '.' || relation.relname,
  state.row_count,
  state.row_md5
FROM pg_class relation
JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
CROSS JOIN LATERAL pg_temp.table_state(relation.oid::regclass) state
WHERE relation.relkind IN ('r', 'p')
  AND (
    namespace.nspname IN (
      'catalog_dev',
      'lifeswitch_nutrition',
      'lifeswitch_training'
    )
    OR (
      namespace.nspname = 'lifeswitch_chat'
      AND relation.relname IN (
        'account_timezone_v1',
        'account_timezone_history_v1'
      )
    )
    OR (
      namespace.nspname = 'public'
      AND relation.relname = 'lifeswitch_measurement_entries'
    )
  )
ORDER BY 1;
