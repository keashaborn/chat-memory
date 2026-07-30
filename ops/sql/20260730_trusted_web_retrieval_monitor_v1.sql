BEGIN;

CREATE INDEX IF NOT EXISTS retrieval_audit_created_at_idx
    ON trusted_web.retrieval_audit (created_at DESC);

CREATE OR REPLACE VIEW trusted_web.retrieval_monitor_hourly_v1
WITH (security_invoker = true)
AS
SELECT
    date_trunc('hour', created_at) AS bucket_start,
    topic,
    policy_version,
    count(*)::bigint AS request_count,
    count(*) FILTER (
        WHERE status = 'completed'
    )::bigint AS completed_count,
    count(*) FILTER (
        WHERE status = 'failed'
    )::bigint AS failed_count,
    count(*) FILTER (
        WHERE status = 'blocked'
    )::bigint AS blocked_count,
    count(*) FILTER (
        WHERE status IN ('failed', 'blocked')
    )::bigint AS fail_closed_count,
    count(*) FILTER (
        WHERE error_code = 'ncbi_no_relevant_records'
    )::bigint AS relevance_fail_closed_count,
    count(*) FILTER (
        WHERE error_code = 'ncbi_no_pubmed_results'
    )::bigint AS no_result_fail_closed_count,
    count(*) FILTER (
        WHERE status = 'failed'
          AND error_code IS DISTINCT FROM 'ncbi_no_relevant_records'
          AND error_code IS DISTINCT FROM 'ncbi_no_pubmed_results'
    )::bigint AS dependency_failure_count
FROM trusted_web.retrieval_audit
GROUP BY
    date_trunc('hour', created_at),
    topic,
    policy_version;

COMMENT ON VIEW trusted_web.retrieval_monitor_hourly_v1 IS
    'Metadata-only trusted-web hourly monitoring; excludes actors, queries, '
    'request identifiers, provider identifiers, and source metadata.';

REVOKE ALL
    ON trusted_web.retrieval_monitor_hourly_v1
    FROM PUBLIC;

DO $trusted_web_monitor_role_acl$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'anon',
        'authenticated',
        'service_role'
    ]
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname = role_name
        ) THEN
            EXECUTE format(
                'REVOKE ALL ON trusted_web.retrieval_monitor_hourly_v1 '
                'FROM %I',
                role_name
            );
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'brains_app'
    ) THEN
        GRANT SELECT
            ON trusted_web.retrieval_monitor_hourly_v1
            TO brains_app;
    END IF;
END
$trusted_web_monitor_role_acl$;

COMMIT;
