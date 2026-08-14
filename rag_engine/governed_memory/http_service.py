from __future__ import annotations

"""Standalone, inactive-by-default HTTP service for governed Memory.

The service deliberately does not import the legacy Brains application.  Off
mode mounts the closed owner route manifest without constructing authentication
or database dependencies.  On mode remains fail-closed until a caller injects
a live Supabase account-and-session authority verifier.
"""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import inspect
import json
import os
import re
from typing import Any, Protocol
from uuid import UUID

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHttpException

from .auth import ActorScope, VerifiedActor
from .conversation_deletion import (
    DeletionRepositoryError,
    DeletionRepositoryFailure,
)
from .conversation_erasure_http import create_conversation_erasure_router
from .deletion_contracts import (
    BoundConversationDeletion,
    ConversationErasureStatus,
    DeletionAuthority,
)
from .http_api import ActorResolver, create_owner_memory_router
from .http_auth import HttpAuthError
from .http_runtime import (
    JwksFetcher,
    SupabaseHttpRuntimeConfig,
    create_supabase_actor_resolver,
)
from .http_store import OwnerStoreError, PostgresOwnerStore
from .runtime.deletion_postgres import (
    PostgresConversationDeletionRepository,
)


HTTP_MODE_ENV = "GOVERNED_MEMORY_HTTP_MODE"
POSTGRES_DSN_ENV = "GOVERNED_MEMORY_POSTGRES_DSN"
CONVERSATION_POSTGRES_DSN_ENV = (
    "GOVERNED_MEMORY_CONVERSATION_POSTGRES_DSN"
)
CONVERSATION_BRIDGE_CATALOG_SHA256_ENV = (
    "GOVERNED_MEMORY_CONVERSATION_BRIDGE_CATALOG_SHA256"
)
SUPABASE_ISSUER_ENV = "GOVERNED_MEMORY_SUPABASE_ISSUER"
SUPABASE_JWKS_URL_ENV = "GOVERNED_MEMORY_SUPABASE_JWKS_URL"
SERVICE_TOKEN_ENV = "GOVERNED_MEMORY_SERVICE_TOKEN"
SERVICE_TOKEN_HEADER = "x-governed-memory-service-token"
EXPECTED_DATABASE_NAME = "governed_memory"
EXPECTED_DATABASE_ROLE = "governed_memory_api"
EXPECTED_CONVERSATION_DATABASE_NAME = "memory"
EXPECTED_CONVERSATION_DATABASE_ROLE = "governed_memory_api"
EXPECTED_CONVERSATION_REQUESTER_ROLE = "memory_erasure_requester"

_EXPECTED_CONVERSATION_PUBLIC_TABLE_COUNT = 3
_EXPECTED_CONVERSATION_PRIVATE_TABLE_COUNT = 7

_OWNER_TABLES = (
    "answer_binding",
    "audit_event",
    "claim",
    "claim_deletion_receipt",
    "claim_evidence",
    "claim_revision",
    "entity",
    "evidence",
    "extraction_job",
    "projection_outbox",
    "proposal",
    "provider_call",
)
_ROLE_PREFLIGHT_SQL = """
SELECT
  pg_catalog.current_database()::text AS database_name,
  session_user::text AS session_user,
  current_user::text AS current_user,
  role.rolcanlogin AS can_login,
  role.rolinherit AS inherits,
  role.rolsuper AS is_superuser,
  role.rolbypassrls AS bypasses_rls
FROM pg_catalog.pg_roles AS role
WHERE role.rolname = session_user
"""
_RLS_PREFLIGHT_SQL = """
SELECT
  pg_catalog.count(*)::integer AS table_count,
  COALESCE(
    pg_catalog.bool_and(
      relation.relrowsecurity
      AND relation.relforcerowsecurity
      AND pg_catalog.pg_get_userbyid(relation.relowner) = 'governed_memory_owner'
    ),
    false
  ) AS exact_forced_rls,
  COALESCE(
    pg_catalog.bool_or(
      pg_catalog.has_table_privilege(
        session_user,
        relation.oid,
        'SELECT,INSERT,UPDATE,DELETE'
      )
    ),
    true
  ) AS api_has_direct_dml
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace
  ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'memory'
  AND relation.relkind = 'r'
  AND relation.relname = ANY($1::text[])
"""
_CONVERSATION_ROLE_PREFLIGHT_SQL = """
WITH api AS (
  SELECT * FROM pg_catalog.pg_roles WHERE rolname = session_user
), requester AS (
  SELECT * FROM pg_catalog.pg_roles
  WHERE rolname = 'memory_erasure_requester'
), exact_edge AS (
  SELECT membership.*
  FROM pg_catalog.pg_auth_members AS membership
  JOIN api ON api.oid = membership.member
  JOIN requester ON requester.oid = membership.roleid
)
SELECT
  pg_catalog.current_database()::text AS database_name,
  session_user::text AS session_user,
  current_user::text AS current_user,
  api.rolcanlogin AS api_can_login,
  api.rolinherit AS api_inherits,
  api.rolsuper AS api_is_superuser,
  api.rolcreatedb AS api_can_create_database,
  api.rolcreaterole AS api_can_create_role,
  api.rolreplication AS api_can_replicate,
  api.rolbypassrls AS api_bypasses_rls,
  requester.rolcanlogin AS requester_can_login,
  requester.rolinherit AS requester_inherits,
  requester.rolsuper AS requester_is_superuser,
  requester.rolcreatedb AS requester_can_create_database,
  requester.rolcreaterole AS requester_can_create_role,
  requester.rolreplication AS requester_can_replicate,
  requester.rolbypassrls AS requester_bypasses_rls,
  (SELECT pg_catalog.count(*)::integer
   FROM pg_catalog.pg_auth_members AS membership
   WHERE membership.member = api.oid) AS api_direct_membership_count,
  (SELECT pg_catalog.count(*)::integer
   FROM pg_catalog.pg_auth_members AS membership
   WHERE membership.roleid = requester.oid)
    AS requester_direct_member_count,
  (SELECT pg_catalog.count(*)::integer
   FROM pg_catalog.pg_roles AS granted_role
   WHERE granted_role.oid <> api.oid
     AND pg_catalog.pg_has_role(api.oid, granted_role.oid, 'MEMBER'))
    AS api_effective_membership_count,
  (SELECT pg_catalog.count(*)::integer
   FROM pg_catalog.pg_roles AS granted_role
   WHERE granted_role.oid <> requester.oid
     AND pg_catalog.pg_has_role(requester.oid, granted_role.oid, 'MEMBER'))
    AS requester_effective_membership_count,
  pg_catalog.pg_has_role(api.oid, requester.oid, 'MEMBER')
    AS requester_member,
  exact_edge.admin_option AS requester_admin_option,
  exact_edge.inherit_option AS requester_inherit_option,
  exact_edge.set_option AS requester_set_option,
  pg_catalog.pg_has_role(
    'brains_app'::regrole, requester.oid, 'MEMBER'
  ) AS brains_app_requester_member
FROM api
CROSS JOIN requester
LEFT JOIN exact_edge ON true
"""
_CONVERSATION_SCHEMA_PREFLIGHT_SQL = """
WITH schema_acl AS (
  SELECT namespace.nspowner, acl.*
  FROM pg_catalog.pg_namespace AS namespace
  CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(
      namespace.nspacl,
      pg_catalog.acldefault('n', namespace.nspowner)
    )
  ) AS acl
  WHERE namespace.nspname = 'memory_ingest_private'
)
SELECT
  (SELECT nspowner = 'sage'::regrole::oid
   FROM pg_catalog.pg_namespace
   WHERE nspname = 'memory_ingest_private') AS schema_owner_exact,
  pg_catalog.has_schema_privilege(
    'memory_erasure_requester', 'memory_ingest_private', 'USAGE'
  ) AS requester_has_usage,
  pg_catalog.has_schema_privilege(
    'memory_erasure_requester', 'memory_ingest_private', 'CREATE'
  ) AS requester_has_create,
  (SELECT pg_catalog.count(*)::integer FROM schema_acl)
    AS schema_acl_entry_count,
  (SELECT pg_catalog.count(*)::integer FROM schema_acl AS acl
   WHERE acl.grantee = 'sage'::regrole::oid
     AND acl.is_grantable) AS schema_owner_grantable_entry_count,
  (SELECT pg_catalog.count(*)::integer FROM schema_acl AS acl
   WHERE acl.grantee IN (
     'memory_ingest_writer'::regrole::oid,
     'memory_erasure_requester'::regrole::oid,
     'governed_memory_worker'::regrole::oid
   ) AND acl.is_grantable) AS schema_runtime_grantable_entry_count,
  NOT EXISTS (
    SELECT 1 FROM schema_acl AS acl
    WHERE acl.grantee IN (0, 'governed_memory_api'::regrole::oid)
       OR NOT (
        (acl.grantee = 'sage'::regrole::oid
          AND acl.privilege_type IN ('USAGE', 'CREATE')
          AND NOT acl.is_grantable)
         OR
         (acl.grantee IN (
            'memory_ingest_writer'::regrole::oid,
            'memory_erasure_requester'::regrole::oid,
            'governed_memory_worker'::regrole::oid
          ) AND acl.privilege_type = 'USAGE'
            AND NOT acl.is_grantable)
       )
  ) AS schema_acl_exact
"""
_CONVERSATION_FUNCTION_PREFLIGHT_SQL = """
WITH expected(signature, volatility) AS (VALUES
  ('memory_ingest_private.begin_source_erasure(uuid,text,uuid,uuid,integer,text)'::text, 'v'::char),
  ('memory_ingest_private.read_source_erasure(uuid)'::text, 's'::char)
), expected_routine AS (
  SELECT signature, volatility,
    pg_catalog.to_regprocedure(signature) AS oid
  FROM expected
), routine_acl AS (
  SELECT routine.oid, acl.grantee, acl.privilege_type, acl.is_grantable
  FROM pg_catalog.pg_proc AS routine
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = routine.pronamespace
  CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(
      routine.proacl,
      pg_catalog.acldefault('f', routine.proowner)
    )
  ) AS acl
  WHERE namespace.nspname = 'memory_ingest_private'
    AND routine.prokind = 'f'
)
SELECT
  (SELECT pg_catalog.count(*)::integer
   FROM expected_routine WHERE oid IS NOT NULL) AS expected_function_count,
  NOT EXISTS (
    SELECT 1
    FROM expected_routine AS expected
    LEFT JOIN pg_catalog.pg_proc AS routine ON routine.oid = expected.oid
    LEFT JOIN pg_catalog.pg_language AS language
      ON language.oid = routine.prolang
    WHERE routine.oid IS NULL
       OR routine.proowner <> 'sage'::regrole
       OR routine.prokind <> 'f'
       OR NOT routine.prosecdef
       OR routine.provolatile <> expected.volatility
       OR routine.proconfig IS DISTINCT FROM
          ARRAY['search_path=pg_catalog']::text[]
       OR language.lanname <> 'plpgsql'
  ) AS expected_function_identity_exact,
  (SELECT pg_catalog.count(DISTINCT acl.oid)::integer
   FROM routine_acl AS acl
   WHERE acl.grantee = 'memory_erasure_requester'::regrole::oid
     AND acl.privilege_type = 'EXECUTE') AS requester_execute_count,
  (SELECT pg_catalog.count(DISTINCT expected_routine.oid)::integer
   FROM expected_routine
   JOIN routine_acl AS acl ON acl.oid = expected_routine.oid
   WHERE acl.grantee = 'memory_erasure_requester'::regrole::oid
     AND acl.privilege_type = 'EXECUTE') AS requester_expected_execute_count,
  (SELECT pg_catalog.count(*)::integer
   FROM routine_acl AS acl
   WHERE acl.grantee IN (0, 'governed_memory_api'::regrole::oid)
     AND acl.privilege_type = 'EXECUTE') AS public_or_api_execute_count,
  (SELECT pg_catalog.count(*)::integer
   FROM routine_acl AS acl
   JOIN expected_routine AS expected ON expected.oid = acl.oid)
    AS expected_function_acl_entry_count,
  (SELECT pg_catalog.count(*)::integer
   FROM routine_acl AS acl
   JOIN expected_routine AS expected ON expected.oid = acl.oid
   WHERE acl.grantee = 'sage'::regrole::oid
     AND acl.is_grantable)
    AS expected_function_owner_grantable_entry_count,
  (SELECT pg_catalog.count(*)::integer
   FROM routine_acl AS acl
   JOIN expected_routine AS expected ON expected.oid = acl.oid
   WHERE acl.grantee = 'memory_erasure_requester'::regrole::oid
     AND acl.is_grantable)
    AS expected_function_requester_grantable_entry_count,
  NOT EXISTS (
    SELECT 1
    FROM routine_acl AS acl
    JOIN expected_routine AS expected ON expected.oid = acl.oid
    WHERE acl.privilege_type <> 'EXECUTE'
       OR NOT (
         (acl.grantee = 'sage'::regrole::oid AND NOT acl.is_grantable)
         OR
         (acl.grantee = 'memory_erasure_requester'::regrole::oid
          AND NOT acl.is_grantable)
       )
  ) AS expected_function_acl_exact,
  (SELECT pg_catalog.count(*)::integer
   FROM pg_catalog.pg_proc AS routine
   JOIN pg_catalog.pg_namespace AS namespace
     ON namespace.oid = routine.pronamespace
   WHERE routine.prosecdef
     AND routine.oid NOT IN (
       SELECT oid FROM expected_routine WHERE oid IS NOT NULL
     )
     AND namespace.nspname <> 'information_schema'
     AND namespace.nspname !~ '^pg_'
     AND (
       pg_catalog.has_function_privilege(
         'governed_memory_api', routine.oid, 'EXECUTE'
       )
       OR pg_catalog.has_function_privilege(
         'memory_erasure_requester', routine.oid, 'EXECUTE'
       )
       OR EXISTS (
         SELECT 1
         FROM pg_catalog.aclexplode(
           COALESCE(
             routine.proacl,
             pg_catalog.acldefault('f', routine.proowner)
           )
         ) AS acl
         WHERE acl.grantee = 0
           AND acl.privilege_type = 'EXECUTE'
       )
     )) AS unexpected_public_api_or_requester_security_definer_count
"""
_CONVERSATION_DML_PREFLIGHT_SQL = """
WITH application_relations AS (
  SELECT namespace.nspname, relation.relname, relation.oid,
    relation.relkind, relation.relowner, relation.relrowsecurity,
    relation.relforcerowsecurity, relation.relacl
  FROM pg_catalog.pg_class AS relation
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
    AND namespace.nspname !~ '^pg_'
    AND namespace.nspname <> 'information_schema'
), application_columns AS (
  SELECT relation.oid AS relation_oid,
    attribute.attnum, attribute.attacl
  FROM application_relations AS relation
  JOIN pg_catalog.pg_attribute AS attribute
    ON attribute.attrelid = relation.oid
  WHERE attribute.attnum > 0
    AND NOT attribute.attisdropped
), application_sequences AS (
  SELECT relation.oid, relation.relowner, relation.relacl
  FROM pg_catalog.pg_class AS relation
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  WHERE relation.relkind = 'S'
    AND namespace.nspname !~ '^pg_'
    AND namespace.nspname <> 'information_schema'
), expected_private(relname) AS (VALUES
  ('memory_ingest_outbox'::text),
  ('source_erasure_operation'::text),
  ('source_erasure_target'::text),
  ('source_erasure_thread_target'::text),
  ('source_erasure_message_tombstone'::text),
  ('source_erasure_thread_tombstone'::text),
  ('source_erasure_receipt'::text)
)
SELECT
  (SELECT pg_catalog.count(*)::integer
   FROM application_relations) AS application_relation_count,
  (SELECT pg_catalog.count(*)::integer
   FROM application_sequences) AS application_sequence_count,
  (SELECT pg_catalog.count(*)::integer
   FROM application_relations
   WHERE nspname = 'public'
     AND relname IN ('chat_log', 'chat_attachments', 'threads'))
    AS public_table_count,
  (SELECT COALESCE(pg_catalog.bool_and(
     relkind = 'r' AND relowner = 'sage'::regrole
     AND relrowsecurity AND relforcerowsecurity
   ), false)
   FROM application_relations
   WHERE nspname = 'public'
     AND relname IN ('chat_log', 'chat_attachments', 'threads'))
    AS chat_root_identity_exact,
  (SELECT pg_catalog.count(*)::integer
   FROM application_relations
   WHERE nspname = 'memory_ingest_private') AS private_table_count,
  (SELECT pg_catalog.count(*) = 7
          AND pg_catalog.count(expected_private.relname) = 7
          AND pg_catalog.bool_and(
            relation.relkind = 'r'
            AND relation.relowner = 'sage'::regrole
          )
   FROM application_relations AS relation
   LEFT JOIN expected_private
     ON expected_private.relname = relation.relname
   WHERE relation.nspname = 'memory_ingest_private')
    AS private_table_identity_exact,
  EXISTS (
    SELECT 1 FROM application_relations AS relation
    WHERE pg_catalog.has_table_privilege(
      'governed_memory_api', relation.oid,
      'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
    ) OR pg_catalog.has_table_privilege(
      'memory_erasure_requester', relation.oid,
      'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
    )
  ) AS api_or_requester_has_relation_privilege,
  EXISTS (
    SELECT 1
    FROM application_relations AS relation
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(
        relation.relacl,
        pg_catalog.acldefault('r', relation.relowner)
      )
    ) AS acl
    WHERE acl.grantee IN (
      0,
      'governed_memory_api'::regrole::oid,
      'memory_erasure_requester'::regrole::oid
    )
  ) AS public_api_or_requester_direct_relation_grant,
  EXISTS (
    SELECT 1 FROM application_columns AS column_acl
    WHERE pg_catalog.has_column_privilege(
      'governed_memory_api', column_acl.relation_oid,
      column_acl.attnum, 'SELECT,INSERT,UPDATE,REFERENCES'
    ) OR pg_catalog.has_column_privilege(
      'memory_erasure_requester', column_acl.relation_oid,
      column_acl.attnum, 'SELECT,INSERT,UPDATE,REFERENCES'
    )
  ) AS api_or_requester_has_column_privilege,
  EXISTS (
    SELECT 1
    FROM application_columns AS column_acl
    CROSS JOIN LATERAL pg_catalog.aclexplode(column_acl.attacl) AS acl
    WHERE acl.grantee IN (
      0,
      'governed_memory_api'::regrole::oid,
      'memory_erasure_requester'::regrole::oid
    )
  ) AS public_api_or_requester_direct_column_grant,
  EXISTS (
    SELECT 1 FROM application_sequences AS sequence
    WHERE pg_catalog.has_sequence_privilege(
      'governed_memory_api', sequence.oid, 'USAGE,SELECT,UPDATE'
    ) OR pg_catalog.has_sequence_privilege(
      'memory_erasure_requester', sequence.oid, 'USAGE,SELECT,UPDATE'
    )
  ) AS api_or_requester_has_sequence_privilege,
  EXISTS (
    SELECT 1
    FROM application_sequences AS sequence
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(
        sequence.relacl,
        pg_catalog.acldefault('S', sequence.relowner)
      )
    ) AS acl
    WHERE acl.grantee IN (
      0,
      'governed_memory_api'::regrole::oid,
      'memory_erasure_requester'::regrole::oid
    )
  ) AS public_api_or_requester_direct_sequence_grant
"""
_CONVERSATION_LOGGING_PREFLIGHT_SQL = """
SELECT
  pg_catalog.current_setting('log_statement') = 'none'
    AS log_statement_disabled,
  pg_catalog.current_setting(
    'log_parameter_max_length_on_error'
  )::integer = 0 AS error_parameter_logging_disabled,
  pg_catalog.current_setting('log_duration') = 'off'
    AS duration_logging_disabled,
  pg_catalog.current_setting(
    'log_min_duration_statement'
  )::integer = -1 AS duration_statement_logging_disabled,
  pg_catalog.current_setting(
    'log_min_duration_sample'
  )::integer = -1 AS duration_sample_logging_disabled,
  pg_catalog.current_setting(
    'log_transaction_sample_rate'
  )::numeric = 0 AS transaction_sampling_disabled,
  pg_catalog.current_setting(
    'log_parameter_max_length'
  )::integer = 0 AS ordinary_parameter_logging_disabled,
  NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_extension AS extension
    WHERE extension.extname = 'pgaudit'
  ) AS pgaudit_not_preloaded,
  (
    pg_catalog.current_setting(
      'auto_explain.log_parameter_max_length', true
    ) IS NULL
    OR pg_catalog.current_setting(
      'auto_explain.log_parameter_max_length', true
    )::integer = 0
  ) AS auto_explain_parameter_logging_disabled
"""
_CONVERSATION_BRIDGE_CATALOG_SQL = """
WITH target_relations AS (
  SELECT relation.oid, namespace.nspname, relation.relname,
    relation.relkind, relation.relowner, relation.relrowsecurity,
    relation.relforcerowsecurity, relation.relpersistence, relation.relacl
  FROM pg_catalog.pg_class AS relation
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
    AND (
      namespace.nspname = 'memory_ingest_private'
      OR (namespace.nspname = 'public' AND relation.relname IN (
        'chat_log', 'chat_attachments', 'threads',
        'active_thread_selection'
      ))
      OR (namespace.nspname = 'trusted_web'
          AND relation.relname = 'response_transcript_v1')
    )
), target_policies AS (
  SELECT policy.oid, policy.polrelid, policy.polname, policy.polcmd,
    policy.polpermissive, policy.polroles, policy.polqual,
    policy.polwithcheck
  FROM pg_catalog.pg_policy AS policy
  WHERE policy.polrelid IN (SELECT oid FROM target_relations)
), target_trigger_routines AS (
  SELECT DISTINCT trigger_value.tgfoid AS routine_oid
  FROM pg_catalog.pg_trigger AS trigger_value
  WHERE NOT trigger_value.tgisinternal
    AND trigger_value.tgrelid IN (SELECT oid FROM target_relations)
), target_policy_routines AS (
  SELECT DISTINCT dependency.refobjid AS routine_oid
  FROM target_policies AS policy
  JOIN pg_catalog.pg_depend AS dependency
    ON dependency.classid = 'pg_catalog.pg_policy'::pg_catalog.regclass
   AND dependency.objid = policy.oid
   AND dependency.refclassid = 'pg_catalog.pg_proc'::pg_catalog.regclass
  JOIN pg_catalog.pg_proc AS routine
    ON routine.oid = dependency.refobjid
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = routine.pronamespace
  WHERE namespace.nspname <> 'pg_catalog'
), authority_routine_oids AS (
  SELECT routine.oid AS routine_oid
  FROM pg_catalog.pg_proc AS routine
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = routine.pronamespace
  WHERE namespace.nspname = 'memory_ingest_private'
  UNION
  SELECT routine_oid FROM target_trigger_routines
  UNION
  SELECT routine_oid FROM target_policy_routines
), routine_facts AS (
  SELECT 'routine'::text AS catalog_kind,
    pg_catalog.format(
      '%I.%I(%s)', namespace.nspname, routine.proname,
      pg_catalog.pg_get_function_identity_arguments(routine.oid)
    ) AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'definition', pg_catalog.pg_get_functiondef(routine.oid),
      'owner', pg_catalog.pg_get_userbyid(routine.proowner),
      'language', language.lanname,
      'kind', routine.prokind,
      'security_definer', routine.prosecdef,
      'config', pg_catalog.to_jsonb(routine.proconfig),
      'volatility', routine.provolatile,
      'acl', COALESCE((
        SELECT pg_catalog.jsonb_agg(
          pg_catalog.jsonb_build_array(
            CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                 ELSE pg_catalog.pg_get_userbyid(acl.grantee) END,
            acl.privilege_type, acl.is_grantable
          ) ORDER BY acl.grantee, acl.privilege_type, acl.is_grantable
        )
        FROM pg_catalog.aclexplode(COALESCE(
          routine.proacl,
          pg_catalog.acldefault('f', routine.proowner)
        )) AS acl
      ), '[]'::jsonb)
    )::text AS catalog_payload
  FROM pg_catalog.pg_proc AS routine
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = routine.pronamespace
  JOIN pg_catalog.pg_language AS language ON language.oid = routine.prolang
  WHERE routine.oid IN (SELECT routine_oid FROM authority_routine_oids)
), relation_facts AS (
  SELECT 'relation'::text AS catalog_kind,
    pg_catalog.format('%I.%I', relation.nspname, relation.relname)
      AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'kind', relation.relkind,
      'owner', pg_catalog.pg_get_userbyid(relation.relowner),
      'row_security', relation.relrowsecurity,
      'force_row_security', relation.relforcerowsecurity,
      'persistence', relation.relpersistence,
      'acl', COALESCE((
        SELECT pg_catalog.jsonb_agg(
          pg_catalog.jsonb_build_array(
            CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                 ELSE pg_catalog.pg_get_userbyid(acl.grantee) END,
            acl.privilege_type, acl.is_grantable
          ) ORDER BY acl.grantee, acl.privilege_type, acl.is_grantable
        )
        FROM pg_catalog.aclexplode(COALESCE(
          relation.relacl,
          pg_catalog.acldefault('r', relation.relowner)
        )) AS acl
      ), '[]'::jsonb)
    )::text AS catalog_payload
  FROM target_relations AS relation
), column_facts AS (
  SELECT 'column'::text AS catalog_kind,
    pg_catalog.format(
      '%I.%I.%I', relation.nspname, relation.relname, attribute.attname
    ) AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'number', attribute.attnum,
      'type', pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
      'not_null', attribute.attnotnull,
      'identity', attribute.attidentity,
      'generated', attribute.attgenerated,
      'default', pg_catalog.pg_get_expr(
        default_value.adbin, default_value.adrelid, true
      ),
      'acl', COALESCE((
        SELECT pg_catalog.jsonb_agg(
          pg_catalog.jsonb_build_array(
            CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                 ELSE pg_catalog.pg_get_userbyid(acl.grantee) END,
            acl.privilege_type, acl.is_grantable
          ) ORDER BY acl.grantee, acl.privilege_type, acl.is_grantable
        )
        FROM pg_catalog.aclexplode(attribute.attacl) AS acl
      ), '[]'::jsonb)
    )::text AS catalog_payload
  FROM target_relations AS relation
  JOIN pg_catalog.pg_attribute AS attribute
    ON attribute.attrelid = relation.oid
   AND attribute.attnum > 0
   AND NOT attribute.attisdropped
  LEFT JOIN pg_catalog.pg_attrdef AS default_value
    ON default_value.adrelid = attribute.attrelid
   AND default_value.adnum = attribute.attnum
), constraint_facts AS (
  SELECT 'constraint'::text AS catalog_kind,
    pg_catalog.format(
      '%s.%I', constraint_value.conrelid::regclass::text,
      constraint_value.conname
    ) AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'type', constraint_value.contype,
      'definition', pg_catalog.pg_get_constraintdef(
        constraint_value.oid, true
      ),
      'validated', constraint_value.convalidated,
      'deferrable', constraint_value.condeferrable,
      'deferred', constraint_value.condeferred,
      'referenced_relation', CASE
        WHEN constraint_value.confrelid = 0 THEN NULL
        ELSE constraint_value.confrelid::regclass::text
      END
    )::text AS catalog_payload
  FROM pg_catalog.pg_constraint AS constraint_value
  WHERE constraint_value.conrelid IN (SELECT oid FROM target_relations)
     OR constraint_value.confrelid IN (SELECT oid FROM target_relations)
), index_facts AS (
  SELECT 'index'::text AS catalog_kind,
    index_relation.oid::regclass::text AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'table', index_value.indrelid::regclass::text,
      'definition', pg_catalog.pg_get_indexdef(index_relation.oid),
      'unique', index_value.indisunique,
      'primary', index_value.indisprimary,
      'valid', index_value.indisvalid,
      'ready', index_value.indisready,
      'owner', pg_catalog.pg_get_userbyid(index_relation.relowner)
    )::text AS catalog_payload
  FROM pg_catalog.pg_index AS index_value
  JOIN pg_catalog.pg_class AS index_relation
    ON index_relation.oid = index_value.indexrelid
  WHERE index_value.indrelid IN (SELECT oid FROM target_relations)
), policy_facts AS (
  SELECT 'policy'::text AS catalog_kind,
    pg_catalog.format(
      '%s.%I', policy.polrelid::regclass::text, policy.polname
    ) AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'command', policy.polcmd,
      'permissive', policy.polpermissive,
      'roles', COALESCE((
        SELECT pg_catalog.jsonb_agg(
          CASE WHEN role_oid = 0 THEN 'PUBLIC'
               ELSE pg_catalog.pg_get_userbyid(role_oid) END
          ORDER BY role_oid
        ) FROM pg_catalog.unnest(policy.polroles) AS role_oid
      ), '[]'::jsonb),
      'using', pg_catalog.pg_get_expr(policy.polqual, policy.polrelid, true),
      'check', pg_catalog.pg_get_expr(
        policy.polwithcheck, policy.polrelid, true
      )
    )::text AS catalog_payload
  FROM target_policies AS policy
), trigger_facts AS (
  SELECT 'trigger'::text AS catalog_kind,
    pg_catalog.format(
      '%s.%I', trigger_value.tgrelid::regclass::text,
      trigger_value.tgname
    ) AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'definition', pg_catalog.pg_get_triggerdef(trigger_value.oid, true),
      'enabled', trigger_value.tgenabled,
      'function', trigger_value.tgfoid::regprocedure::text
    )::text AS catalog_payload
  FROM pg_catalog.pg_trigger AS trigger_value
  WHERE NOT trigger_value.tgisinternal
    AND trigger_value.tgrelid IN (SELECT oid FROM target_relations)
), rule_facts AS (
  SELECT 'rule'::text AS catalog_kind,
    pg_catalog.format(
      '%s.%I', rule_value.ev_class::regclass::text, rule_value.rulename
    ) AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'definition', pg_catalog.pg_get_ruledef(rule_value.oid, true),
      'enabled', rule_value.ev_enabled
    )::text AS catalog_payload
  FROM pg_catalog.pg_rewrite AS rule_value
  WHERE rule_value.ev_class IN (SELECT oid FROM target_relations)
), inheritance_facts AS (
  SELECT 'inheritance'::text AS catalog_kind,
    pg_catalog.format(
      '%s->%s', inheritance.inhrelid::regclass::text,
      inheritance.inhparent::regclass::text
    ) AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'sequence', inheritance.inhseqno,
      'detached_pending', inheritance.inhdetachpending
    )::text AS catalog_payload
  FROM pg_catalog.pg_inherits AS inheritance
  WHERE inheritance.inhrelid IN (SELECT oid FROM target_relations)
     OR inheritance.inhparent IN (SELECT oid FROM target_relations)
), schema_facts AS (
  SELECT 'schema'::text AS catalog_kind,
    namespace.nspname::text AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'owner', pg_catalog.pg_get_userbyid(namespace.nspowner),
      'acl', COALESCE((
        SELECT pg_catalog.jsonb_agg(
          pg_catalog.jsonb_build_array(
            CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                 ELSE pg_catalog.pg_get_userbyid(acl.grantee) END,
            acl.privilege_type, acl.is_grantable
          ) ORDER BY acl.grantee, acl.privilege_type, acl.is_grantable
        )
        FROM pg_catalog.aclexplode(COALESCE(
          namespace.nspacl,
          pg_catalog.acldefault('n', namespace.nspowner)
        )) AS acl
      ), '[]'::jsonb)
    )::text AS catalog_payload
  FROM pg_catalog.pg_namespace AS namespace
  WHERE namespace.nspname IN (SELECT nspname FROM target_relations)
), role_facts AS (
  SELECT 'role'::text AS catalog_kind, role.rolname AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'login', role.rolcanlogin,
      'inherit', role.rolinherit,
      'superuser', role.rolsuper,
      'create_database', role.rolcreatedb,
      'create_role', role.rolcreaterole,
      'replication', role.rolreplication,
      'bypass_rls', role.rolbypassrls
    )::text AS catalog_payload
  FROM pg_catalog.pg_roles AS role
  WHERE role.rolname IN (
    'brains_app', 'governed_memory_api', 'governed_memory_worker',
    'memory_ingest_writer', 'memory_erasure_requester'
  )
), membership_facts AS (
  SELECT 'membership'::text AS catalog_kind,
    pg_catalog.format('%I->%I', member.rolname, granted.rolname)
      AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'admin_option', membership.admin_option,
      'inherit_option', membership.inherit_option,
      'set_option', membership.set_option
    )::text AS catalog_payload
  FROM pg_catalog.pg_auth_members AS membership
  JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
  JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
  WHERE member.rolname IN (
      'brains_app', 'governed_memory_api', 'governed_memory_worker',
      'memory_ingest_writer', 'memory_erasure_requester'
    ) OR granted.rolname IN (
      'brains_app', 'governed_memory_api', 'governed_memory_worker',
      'memory_ingest_writer', 'memory_erasure_requester'
    )
), database_facts AS (
  SELECT 'database'::text AS catalog_kind,
    database.datname::text AS catalog_identity,
    pg_catalog.jsonb_build_object(
      'owner', pg_catalog.pg_get_userbyid(database.datdba),
      'encoding', pg_catalog.pg_encoding_to_char(database.encoding)
    )::text AS catalog_payload
  FROM pg_catalog.pg_database AS database
  WHERE database.datname = pg_catalog.current_database()
)
SELECT catalog_kind, catalog_identity, catalog_payload
FROM (
  SELECT * FROM routine_facts
  UNION ALL SELECT * FROM relation_facts
  UNION ALL SELECT * FROM column_facts
  UNION ALL SELECT * FROM constraint_facts
  UNION ALL SELECT * FROM index_facts
  UNION ALL SELECT * FROM policy_facts
  UNION ALL SELECT * FROM trigger_facts
  UNION ALL SELECT * FROM rule_facts
  UNION ALL SELECT * FROM inheritance_facts
  UNION ALL SELECT * FROM schema_facts
  UNION ALL SELECT * FROM role_facts
  UNION ALL SELECT * FROM membership_facts
  UNION ALL SELECT * FROM database_facts
) AS catalog
ORDER BY catalog_kind, catalog_identity, catalog_payload
"""
_CONVERSATION_BRIDGE_CATALOG_DOMAIN = (
    "governed_memory.conversation_bridge_catalog.v1"
)
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}

PoolFactory = Callable[..., Awaitable[Any]]
ActorResolverFactory = Callable[..., ActorResolver]
DEFAULT_GOVERNED_MEMORY_POOL_FACTORY: PoolFactory = asyncpg.create_pool


class LiveAuthorityVerifier(Protocol):
    """Verify the request's account and JWT session against live authority.

    An implementation must make bounded uncached authority requests, require
    the authoritative user UUID to equal ``actor.owner_user_id``, and require
    ``actor.session_id`` to be present in the Supabase session ledger.  The
    verifier must fail closed on ambiguity or authority unavailability.
    """

    async def __call__(self, request: Request, actor: VerifiedActor) -> None: ...


class HttpServiceConfigurationError(RuntimeError):
    """Stable content-free service configuration failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HttpServicePreflightError(RuntimeError):
    """Stable content-free database metadata preflight failure."""

    def __init__(self, code: str = "governed_memory_database_preflight_failed") -> None:
        self.code = code
        super().__init__(code)


def _required_text(value: object, *, secret: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise HttpServiceConfigurationError(
            "governed_memory_secret_invalid"
            if secret
            else "governed_memory_configuration_invalid"
        )
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise HttpServiceConfigurationError(
            "governed_memory_configuration_invalid"
        ) from exc
    maximum = 4_096 if secret else 16_384
    if size > maximum:
        raise HttpServiceConfigurationError(
            "governed_memory_secret_invalid"
            if secret
            else "governed_memory_configuration_invalid"
        )
    return value


def _required_sha256(value: object) -> str:
    text = _required_text(value)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise HttpServiceConfigurationError(
            "governed_memory_configuration_invalid"
        )
    return text


def _conversation_bridge_catalog_sha256(rows: object) -> str:
    if not isinstance(rows, (list, tuple)) or not rows:
        raise HttpServicePreflightError()
    normalized: list[tuple[str, str, str]] = []
    identities: set[tuple[str, str]] = set()
    for row in rows:
        values = _row_values(
            row,
            ("catalog_kind", "catalog_identity", "catalog_payload"),
        )
        triple: list[str] = []
        for name in (
            "catalog_kind",
            "catalog_identity",
            "catalog_payload",
        ):
            value = values[name]
            if (
                type(value) is not str
                or not value
                or len(value.encode("utf-8")) > 4_000_000
            ):
                raise HttpServicePreflightError()
            triple.append(value)
        identity = (triple[0], triple[1])
        if identity in identities:
            raise HttpServicePreflightError()
        identities.add(identity)
        normalized.append((triple[0], triple[1], triple[2]))
    material = json.dumps(
        {
            "domain": _CONVERSATION_BRIDGE_CATALOG_DOMAIN,
            "rows": sorted(normalized),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class GovernedMemoryHttpServiceSettings:
    mode: str = "off"
    postgres_dsn: str | None = field(default=None, repr=False)
    conversation_postgres_dsn: str | None = field(default=None, repr=False)
    conversation_bridge_catalog_sha256: str | None = None
    supabase_issuer: str | None = None
    supabase_jwks_url: str | None = None
    service_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.mode not in {"off", "on"}:
            raise HttpServiceConfigurationError("governed_memory_http_mode_invalid")
        active_values = (
            self.postgres_dsn,
            self.conversation_postgres_dsn,
            self.conversation_bridge_catalog_sha256,
            self.supabase_issuer,
            self.supabase_jwks_url,
            self.service_token,
        )
        if self.mode == "off":
            if any(value is not None for value in active_values):
                raise HttpServiceConfigurationError(
                    "governed_memory_off_configuration_must_be_empty"
                )
            return
        _required_text(self.postgres_dsn)
        _required_text(self.conversation_postgres_dsn)
        _required_sha256(self.conversation_bridge_catalog_sha256)
        issuer = _required_text(self.supabase_issuer)
        jwks_url = _required_text(self.supabase_jwks_url)
        service_token = _required_text(self.service_token, secret=True)
        try:
            SupabaseHttpRuntimeConfig(
                issuer=issuer,
                audience="authenticated",
                jwks_url=jwks_url,
                expected_service_token=service_token,
                service_token_header=SERVICE_TOKEN_HEADER,
            )
        except HttpAuthError as exc:
            raise HttpServiceConfigurationError(
                "governed_memory_supabase_configuration_invalid"
            ) from exc

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "GovernedMemoryHttpServiceSettings":
        values = environment if environment is not None else os.environ
        mode = values.get(HTTP_MODE_ENV, "off")
        if mode != "on":
            return cls(mode=mode)
        return cls(
            mode="on",
            postgres_dsn=values.get(POSTGRES_DSN_ENV),
            conversation_postgres_dsn=values.get(
                CONVERSATION_POSTGRES_DSN_ENV
            ),
            conversation_bridge_catalog_sha256=values.get(
                CONVERSATION_BRIDGE_CATALOG_SHA256_ENV
            ),
            supabase_issuer=values.get(SUPABASE_ISSUER_ENV),
            supabase_jwks_url=values.get(SUPABASE_JWKS_URL_ENV),
            service_token=values.get(SERVICE_TOKEN_ENV),
        )

    def authentication_config(self) -> SupabaseHttpRuntimeConfig:
        if self.mode != "on":
            raise HttpServiceConfigurationError("governed_memory_http_disabled")
        assert self.supabase_issuer is not None
        assert self.supabase_jwks_url is not None
        assert self.service_token is not None
        return SupabaseHttpRuntimeConfig(
            issuer=self.supabase_issuer,
            audience="authenticated",
            jwks_url=self.supabase_jwks_url,
            expected_service_token=self.service_token,
            service_token_header=SERVICE_TOKEN_HEADER,
        )


class _PoolHandle:
    __slots__ = ("_pool",)

    def __init__(self) -> None:
        self._pool: Any | None = None

    def attach(self, pool: Any) -> None:
        if pool is None or self._pool is not None:
            raise HttpServicePreflightError()
        self._pool = pool

    def detach(self, pool: Any) -> None:
        if self._pool is not pool:
            raise HttpServicePreflightError()
        self._pool = None

    def acquire(self) -> Any:
        if self._pool is None:
            raise OwnerStoreError("database_unavailable")
        return self._pool.acquire()


class _ServiceRuntime:
    __slots__ = (
        "mode",
        "pool_handle",
        "conversation_pool_handle",
        "ready",
    )

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.pool_handle = _PoolHandle()
        self.conversation_pool_handle = _PoolHandle()
        self.ready = False


class _ConversationErasurePoolRequester:
    __slots__ = ("_pool_handle",)

    def __init__(self, pool_handle: _PoolHandle) -> None:
        self._pool_handle = pool_handle

    async def request_erasure(
        self,
        command: BoundConversationDeletion,
    ) -> ConversationErasureStatus:
        try:
            async with self._pool_handle.acquire() as connection:
                return await PostgresConversationDeletionRepository(
                    connection
                ).request_erasure(command)
        except DeletionRepositoryError:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None

    async def read_erasure_status(
        self,
        authority: DeletionAuthority,
        operation_id: UUID,
    ) -> ConversationErasureStatus | None:
        try:
            async with self._pool_handle.acquire() as connection:
                return await PostgresConversationDeletionRepository(
                    connection
                ).read_erasure_status(authority, operation_id)
        except DeletionRepositoryError:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None


def _row_values(row: object, names: tuple[str, ...]) -> dict[str, object]:
    if row is None:
        raise HttpServicePreflightError()
    try:
        return {name: row[name] for name in names}  # type: ignore[index]
    except (KeyError, TypeError, IndexError) as exc:
        raise HttpServicePreflightError() from exc


async def _preflight_connection(connection: Any) -> None:
    identity = _row_values(
        await connection.fetchrow(_ROLE_PREFLIGHT_SQL),
        (
            "database_name",
            "session_user",
            "current_user",
            "can_login",
            "inherits",
            "is_superuser",
            "bypasses_rls",
        ),
    )
    if (
        identity["database_name"] != EXPECTED_DATABASE_NAME
        or identity["session_user"] != EXPECTED_DATABASE_ROLE
        or identity["current_user"] != EXPECTED_DATABASE_ROLE
        or identity["can_login"] is not True
        or identity["inherits"] is not False
        or identity["is_superuser"] is not False
        or identity["bypasses_rls"] is not False
    ):
        raise HttpServicePreflightError()

    rls = _row_values(
        await connection.fetchrow(_RLS_PREFLIGHT_SQL, list(_OWNER_TABLES)),
        ("table_count", "exact_forced_rls", "api_has_direct_dml"),
    )
    if (
        type(rls["table_count"]) is not int
        or rls["table_count"] != len(_OWNER_TABLES)
        or rls["exact_forced_rls"] is not True
        or rls["api_has_direct_dml"] is not False
    ):
        raise HttpServicePreflightError()


async def _preflight_conversation_connection(
    connection: Any,
    expected_bridge_catalog_sha256: str,
) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", expected_bridge_catalog_sha256) is None:
        raise HttpServicePreflightError()
    identity = _row_values(
        await connection.fetchrow(_CONVERSATION_ROLE_PREFLIGHT_SQL),
        (
            "database_name",
            "session_user",
            "current_user",
            "api_can_login",
            "api_inherits",
            "api_is_superuser",
            "api_can_create_database",
            "api_can_create_role",
            "api_can_replicate",
            "api_bypasses_rls",
            "requester_can_login",
            "requester_inherits",
            "requester_is_superuser",
            "requester_can_create_database",
            "requester_can_create_role",
            "requester_can_replicate",
            "requester_bypasses_rls",
            "api_direct_membership_count",
            "requester_direct_member_count",
            "api_effective_membership_count",
            "requester_effective_membership_count",
            "requester_member",
            "requester_admin_option",
            "requester_inherit_option",
            "requester_set_option",
            "brains_app_requester_member",
        ),
    )
    if (
        identity["database_name"] != EXPECTED_CONVERSATION_DATABASE_NAME
        or identity["session_user"] != EXPECTED_CONVERSATION_DATABASE_ROLE
        or identity["current_user"] != EXPECTED_CONVERSATION_DATABASE_ROLE
        or identity["api_can_login"] is not True
        or identity["api_inherits"] is not False
        or identity["api_is_superuser"] is not False
        or identity["api_can_create_database"] is not False
        or identity["api_can_create_role"] is not False
        or identity["api_can_replicate"] is not False
        or identity["api_bypasses_rls"] is not False
        or identity["requester_can_login"] is not False
        or identity["requester_inherits"] is not False
        or identity["requester_is_superuser"] is not False
        or identity["requester_can_create_database"] is not False
        or identity["requester_can_create_role"] is not False
        or identity["requester_can_replicate"] is not False
        or identity["requester_bypasses_rls"] is not False
        or type(identity["api_direct_membership_count"]) is not int
        or identity["api_direct_membership_count"] != 1
        or type(identity["requester_direct_member_count"]) is not int
        or identity["requester_direct_member_count"] != 1
        or type(identity["api_effective_membership_count"]) is not int
        or identity["api_effective_membership_count"] != 1
        or type(identity["requester_effective_membership_count"]) is not int
        or identity["requester_effective_membership_count"] != 0
        or identity["requester_member"] is not True
        or identity["requester_admin_option"] is not False
        or identity["requester_inherit_option"] is not False
        or identity["requester_set_option"] is not True
        or identity["brains_app_requester_member"] is not False
    ):
        raise HttpServicePreflightError()

    logging = _row_values(
        await connection.fetchrow(_CONVERSATION_LOGGING_PREFLIGHT_SQL),
        (
            "log_statement_disabled",
            "error_parameter_logging_disabled",
            "duration_logging_disabled",
            "duration_statement_logging_disabled",
            "duration_sample_logging_disabled",
            "transaction_sampling_disabled",
            "ordinary_parameter_logging_disabled",
            "pgaudit_not_preloaded",
            "auto_explain_parameter_logging_disabled",
        ),
    )
    if any(value is not True for value in logging.values()):
        raise HttpServicePreflightError()

    schema = _row_values(
        await connection.fetchrow(_CONVERSATION_SCHEMA_PREFLIGHT_SQL),
        (
            "schema_owner_exact",
            "requester_has_usage",
            "requester_has_create",
            "schema_acl_entry_count",
            "schema_owner_grantable_entry_count",
            "schema_runtime_grantable_entry_count",
            "schema_acl_exact",
        ),
    )
    if (
        schema["schema_owner_exact"] is not True
        or schema["requester_has_usage"] is not True
        or schema["requester_has_create"] is not False
        or type(schema["schema_acl_entry_count"]) is not int
        or schema["schema_acl_entry_count"] != 5
        or type(schema["schema_owner_grantable_entry_count"]) is not int
        or schema["schema_owner_grantable_entry_count"] != 0
        or type(schema["schema_runtime_grantable_entry_count"]) is not int
        or schema["schema_runtime_grantable_entry_count"] != 0
        or schema["schema_acl_exact"] is not True
    ):
        raise HttpServicePreflightError()

    catalog_rows = await connection.fetch(_CONVERSATION_BRIDGE_CATALOG_SQL)
    if (
        _conversation_bridge_catalog_sha256(catalog_rows)
        != expected_bridge_catalog_sha256
    ):
        raise HttpServicePreflightError()

    await connection.execute("SET ROLE memory_erasure_requester")
    try:
        function_row = await connection.fetchrow(
            _CONVERSATION_FUNCTION_PREFLIGHT_SQL
        )
    finally:
        await connection.execute("RESET ROLE")
    functions = _row_values(
        function_row,
        (
            "expected_function_count",
            "expected_function_identity_exact",
            "requester_execute_count",
            "requester_expected_execute_count",
            "public_or_api_execute_count",
            "expected_function_acl_entry_count",
            "expected_function_owner_grantable_entry_count",
            "expected_function_requester_grantable_entry_count",
            "expected_function_acl_exact",
            "unexpected_public_api_or_requester_security_definer_count",
        ),
    )
    if (
        type(functions["expected_function_count"]) is not int
        or functions["expected_function_count"] != 2
        or functions["expected_function_identity_exact"] is not True
        or type(functions["requester_execute_count"]) is not int
        or functions["requester_execute_count"] != 2
        or type(functions["requester_expected_execute_count"]) is not int
        or functions["requester_expected_execute_count"] != 2
        or type(functions["public_or_api_execute_count"]) is not int
        or functions["public_or_api_execute_count"] != 0
        or type(functions["expected_function_acl_entry_count"]) is not int
        or functions["expected_function_acl_entry_count"] != 4
        or type(
            functions["expected_function_owner_grantable_entry_count"]
        ) is not int
        or functions["expected_function_owner_grantable_entry_count"] != 0
        or type(
            functions["expected_function_requester_grantable_entry_count"]
        ) is not int
        or functions[
            "expected_function_requester_grantable_entry_count"
        ] != 0
        or functions["expected_function_acl_exact"] is not True
        or type(
            functions[
                "unexpected_public_api_or_requester_security_definer_count"
            ]
        ) is not int
        or functions[
            "unexpected_public_api_or_requester_security_definer_count"
        ] != 0
    ):
        raise HttpServicePreflightError()

    dml = _row_values(
        await connection.fetchrow(_CONVERSATION_DML_PREFLIGHT_SQL),
        (
            "application_relation_count",
            "application_sequence_count",
            "public_table_count",
            "chat_root_identity_exact",
            "private_table_count",
            "private_table_identity_exact",
            "api_or_requester_has_relation_privilege",
            "public_api_or_requester_direct_relation_grant",
            "api_or_requester_has_column_privilege",
            "public_api_or_requester_direct_column_grant",
            "api_or_requester_has_sequence_privilege",
            "public_api_or_requester_direct_sequence_grant",
        ),
    )
    if (
        type(dml["application_relation_count"]) is not int
        or dml["application_relation_count"] < 10
        or type(dml["application_sequence_count"]) is not int
        or dml["application_sequence_count"] < 0
        or type(dml["public_table_count"]) is not int
        or dml["public_table_count"]
        != _EXPECTED_CONVERSATION_PUBLIC_TABLE_COUNT
        or dml["chat_root_identity_exact"] is not True
        or type(dml["private_table_count"]) is not int
        or dml["private_table_count"]
        != _EXPECTED_CONVERSATION_PRIVATE_TABLE_COUNT
        or dml["private_table_identity_exact"] is not True
        or dml["api_or_requester_has_relation_privilege"] is not False
        or dml[
            "public_api_or_requester_direct_relation_grant"
        ] is not False
        or dml["api_or_requester_has_column_privilege"] is not False
        or dml["public_api_or_requester_direct_column_grant"] is not False
        or dml["api_or_requester_has_sequence_privilege"] is not False
        or dml[
            "public_api_or_requester_direct_sequence_grant"
        ] is not False
    ):
        raise HttpServicePreflightError()


async def _configure_pool_connection(connection: Any) -> None:
    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
    )


async def _reset_connection(connection: Any) -> None:
    await connection.reset()


def _compose_authoritative_actor_resolver(
    resolver: ActorResolver,
    authority_verifier: LiveAuthorityVerifier,
) -> ActorResolver:
    async def resolve(
        request: Request,
        scopes: tuple[ActorScope, ...],
    ) -> VerifiedActor:
        actor = await resolver(request, scopes)
        if not isinstance(actor, VerifiedActor):
            raise HttpAuthError("auth_token_invalid")
        outcome = authority_verifier(request, actor)
        if not inspect.isawaitable(outcome):
            raise HttpAuthError("auth_configuration_invalid")
        await outcome
        return actor

    return resolve


def _service_response(
    status_code: int,
    content: Mapping[str, object],
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=dict(content),
        headers=_NO_STORE_HEADERS,
    )


def create_governed_memory_http_service(
    settings: GovernedMemoryHttpServiceSettings | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    pool_factory: PoolFactory = DEFAULT_GOVERNED_MEMORY_POOL_FACTORY,
    actor_resolver_factory: ActorResolverFactory = create_supabase_actor_resolver,
    authority_verifier: LiveAuthorityVerifier | None = None,
    token_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    jwks_fetcher: JwksFetcher | None = None,
) -> FastAPI:
    """Build an isolated service; construction performs no external I/O."""

    if settings is not None and environment is not None:
        raise HttpServiceConfigurationError(
            "governed_memory_configuration_source_ambiguous"
        )
    active = settings or GovernedMemoryHttpServiceSettings.from_environment(
        environment
    )
    runtime = _ServiceRuntime(active.mode)
    actor_resolver: ActorResolver | None = None
    facade: PostgresOwnerStore | None = None
    erasure_requester: _ConversationErasurePoolRequester | None = None

    if active.mode == "on":
        if not callable(pool_factory) or not callable(actor_resolver_factory):
            raise HttpServiceConfigurationError(
                "governed_memory_runtime_factory_invalid"
            )
        if authority_verifier is None or not callable(authority_verifier):
            raise HttpServiceConfigurationError(
                "governed_memory_live_authority_verifier_required"
            )
        auth_kwargs: dict[str, object] = {"token_clock": token_clock}
        if jwks_fetcher is not None:
            auth_kwargs["fetcher"] = jwks_fetcher
        base_resolver = actor_resolver_factory(
            active.authentication_config(),
            **auth_kwargs,
        )
        if not callable(base_resolver):
            raise HttpServiceConfigurationError(
                "governed_memory_actor_resolver_invalid"
            )
        actor_resolver = _compose_authoritative_actor_resolver(
            base_resolver,
            authority_verifier,
        )
        facade = PostgresOwnerStore(runtime.pool_handle)
        erasure_requester = _ConversationErasurePoolRequester(
            runtime.conversation_pool_handle
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if active.mode == "off":
            yield
            return

        assert active.postgres_dsn is not None
        assert active.conversation_postgres_dsn is not None
        assert active.conversation_bridge_catalog_sha256 is not None
        successor_pool: Any | None = None
        conversation_pool: Any | None = None
        successor_attached = False
        conversation_attached = False
        try:
            successor_pool = await pool_factory(
                dsn=active.postgres_dsn,
                min_size=1,
                max_size=1,
                max_queries=500,
                max_inactive_connection_lifetime=60.0,
                command_timeout=8.0,
                reset=_reset_connection,
                init=_configure_pool_connection,
                server_settings={
                    "application_name": "governed_memory_http",
                    "statement_timeout": "8000",
                    "lock_timeout": "2000",
                    "idle_in_transaction_session_timeout": "8000",
                },
            )
            async with successor_pool.acquire() as connection:
                await _preflight_connection(connection)

            conversation_pool = await pool_factory(
                dsn=active.conversation_postgres_dsn,
                min_size=1,
                max_size=1,
                max_queries=500,
                max_inactive_connection_lifetime=60.0,
                command_timeout=8.0,
                reset=_reset_connection,
                init=_configure_pool_connection,
                server_settings={
                    "application_name": (
                        "governed_memory_conversation_http"
                    ),
                    "statement_timeout": "8000",
                    "lock_timeout": "2000",
                    "idle_in_transaction_session_timeout": "8000",
                },
            )
            async with conversation_pool.acquire() as connection:
                await _preflight_conversation_connection(
                    connection,
                    active.conversation_bridge_catalog_sha256,
                )

            runtime.pool_handle.attach(successor_pool)
            successor_attached = True
            runtime.conversation_pool_handle.attach(conversation_pool)
            conversation_attached = True
            runtime.ready = True
        except Exception:
            if conversation_attached:
                runtime.conversation_pool_handle.detach(conversation_pool)
            if successor_attached:
                runtime.pool_handle.detach(successor_pool)
            if conversation_pool is not None:
                await conversation_pool.close()
            if successor_pool is not None:
                await successor_pool.close()
            raise

        try:
            yield
        finally:
            runtime.ready = False
            if conversation_attached:
                runtime.conversation_pool_handle.detach(conversation_pool)
            if successor_attached:
                runtime.pool_handle.detach(successor_pool)
            if conversation_pool is not None:
                await conversation_pool.close()
            if successor_pool is not None:
                await successor_pool.close()

    service = FastAPI(
        title="Governed Memory API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    service.state.governed_memory_runtime = runtime

    @service.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return _service_response(
            200,
            {"status": "ok", "service": "governed_memory_http"},
        )

    @service.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        if active.mode == "off":
            return _service_response(
                503,
                {"error": {"code": "governed_memory_disabled"}},
            )
        if runtime.ready is not True:
            return _service_response(
                503,
                {"error": {"code": "governed_memory_not_ready"}},
            )
        return _service_response(
            200,
            {"status": "ready", "service": "governed_memory_http"},
        )

    service.include_router(
        create_owner_memory_router(
            actor_resolver=actor_resolver,
            facade=facade,
            feature_enabled=active.mode == "on",
        ),
        include_in_schema=False,
    )
    service.include_router(
        create_conversation_erasure_router(
            actor_resolver=actor_resolver,
            requester=erasure_requester,
            feature_enabled=active.mode == "on",
        ),
        include_in_schema=False,
    )

    @service.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        _exception: RequestValidationError,
    ) -> JSONResponse:
        return _service_response(
            400,
            {"error": {"code": "memory_request_invalid"}},
        )

    @service.exception_handler(StarletteHttpException)
    async def http_error_handler(
        _request: Request,
        exception: StarletteHttpException,
    ) -> JSONResponse:
        if exception.status_code == 404:
            return _service_response(
                404,
                {"error": {"code": "memory_route_not_found"}},
            )
        if exception.status_code == 405:
            return _service_response(
                405,
                {"error": {"code": "memory_method_not_allowed"}},
            )
        return _service_response(
            500,
            {"error": {"code": "memory_internal_error"}},
        )

    return service


__all__ = [
    "CONVERSATION_POSTGRES_DSN_ENV",
    "DEFAULT_GOVERNED_MEMORY_POOL_FACTORY",
    "EXPECTED_CONVERSATION_DATABASE_NAME",
    "EXPECTED_CONVERSATION_DATABASE_ROLE",
    "EXPECTED_CONVERSATION_REQUESTER_ROLE",
    "EXPECTED_DATABASE_NAME",
    "EXPECTED_DATABASE_ROLE",
    "GovernedMemoryHttpServiceSettings",
    "HttpServiceConfigurationError",
    "HttpServicePreflightError",
    "LiveAuthorityVerifier",
    "SERVICE_TOKEN_HEADER",
    "create_governed_memory_http_service",
]
