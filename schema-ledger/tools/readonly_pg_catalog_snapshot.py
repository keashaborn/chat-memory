#!/usr/bin/env python3
"""Emit a sanitized PostgreSQL catalog snapshot through a fixed read-only path.

Designed to run on seebx from SSH stdin.  It queries only PostgreSQL system
catalogs inside one REPEATABLE READ, READ ONLY transaction.  Definitions and
expressions are represented by SHA-256/length, never raw SQL bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys


SCHEMA_VERSION = "postgres-catalog-snapshot-v1"
CANONICALIZATION = "json-sort-keys-utf8-ensure-ascii-no-floats-lf-v1"
DOCKER = "/usr/bin/docker"
PSQL = "/usr/bin/psql"
PG_DUMP = "/usr/bin/pg_dump"
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MAX_OUTPUT = 128 * 1024 * 1024
SENSITIVE_TOKEN_SHA256 = {"3a5a2512949399115565867a73a413ec6ba215c8f2df385f78b33238a6639b7c"}


CATALOG_SQL = r"""
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '120s';
SET LOCAL lock_timeout = '5s';
SELECT jsonb_build_object(
  'observed_at_utc', to_char(transaction_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'read_only_proof', jsonb_build_object(
    'transaction_read_only', current_setting('transaction_read_only'),
    'transaction_isolation', current_setting('transaction_isolation'),
    'row_data_read', false,
    'vector_payload_read', false
  ),
  'database', jsonb_build_object(
    'name', current_database(),
    'owner', (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()),
    'server_version', current_setting('server_version'),
    'server_version_num', current_setting('server_version_num'),
    'encoding', current_setting('server_encoding'),
    'collation', (SELECT datcollate FROM pg_database WHERE datname=current_database()),
    'ctype', (SELECT datctype FROM pg_database WHERE datname=current_database()),
    'acl', COALESCE((SELECT to_jsonb(ARRAY(SELECT value::text FROM unnest(COALESCE(datacl, acldefault('d',datdba))) value ORDER BY value::text)) FROM pg_database WHERE datname=current_database()), '[]'::jsonb)
  ),
  'objects', jsonb_build_object(
    'schema', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', n.nspname, 'identity', n.nspname,
        'owner', pg_get_userbyid(n.nspowner),
        'acl', to_jsonb(ARRAY(SELECT value::text FROM unnest(COALESCE(n.nspacl, acldefault('n',n.nspowner))) value ORDER BY value::text))
      ) ORDER BY n.nspname)
      FROM pg_namespace n WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    ), '[]'::jsonb),
    'relation', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', c.relname, 'identity', c.relname,
        'relation_kind', c.relkind, 'persistence', c.relpersistence,
        'owner', pg_get_userbyid(c.relowner), 'rls_enabled', c.relrowsecurity,
        'rls_forced', c.relforcerowsecurity, 'replica_identity', c.relreplident,
        'is_partition', c.relispartition,
        'partition_bound_sha256', CASE WHEN c.relpartbound IS NULL THEN NULL ELSE encode(digest(convert_to(pg_get_expr(c.relpartbound,c.oid,true),'UTF8'),'sha256'),'hex') END,
        'partition_bound_length', CASE WHEN c.relpartbound IS NULL THEN 0 ELSE length(pg_get_expr(c.relpartbound,c.oid,true)) END,
        'acl', to_jsonb(ARRAY(SELECT value::text FROM unnest(COALESCE(c.relacl, acldefault(CASE WHEN c.relkind='S' THEN 'S'::"char" ELSE 'r'::"char" END,c.relowner))) value ORDER BY value::text))
      ) ORDER BY n.nspname,c.relname)
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','f')
    ), '[]'::jsonb),
    'column', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', a.attname, 'identity', c.relname||'.'||a.attname,
        'relation', c.relname, 'position', a.attnum,
        'type', format_type(a.atttypid,a.atttypmod), 'not_null', a.attnotnull,
        'identity_kind', a.attidentity, 'generated_kind', a.attgenerated,
        'collation', CASE WHEN a.attcollation=0 THEN NULL ELSE (SELECT nc.nspname||'.'||co.collname FROM pg_collation co JOIN pg_namespace nc ON nc.oid=co.collnamespace WHERE co.oid=a.attcollation) END,
        'default_sha256', CASE WHEN d.adbin IS NULL THEN NULL ELSE encode(digest(convert_to(pg_get_expr(d.adbin,d.adrelid,true),'UTF8'),'sha256'),'hex') END,
        'default_length', CASE WHEN d.adbin IS NULL THEN 0 ELSE length(pg_get_expr(d.adbin,d.adrelid,true)) END,
        'acl', to_jsonb(ARRAY(SELECT value::text FROM unnest(COALESCE(a.attacl,'{}'::aclitem[])) value ORDER BY value::text))
      ) ORDER BY n.nspname,c.relname,a.attnum)
      FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','f','v','m') AND a.attnum>0 AND NOT a.attisdropped
    ), '[]'::jsonb),
    'constraint', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', con.conname, 'identity', c.relname||'.'||con.conname,
        'relation', c.relname, 'constraint_type', con.contype,
        'deferrable', con.condeferrable, 'initially_deferred', con.condeferred,
        'validated', con.convalidated,
        'referenced_relation', CASE WHEN con.confrelid=0 THEN NULL ELSE con.confrelid::regclass::text END,
        'definition_sha256', encode(digest(convert_to(pg_get_constraintdef(con.oid,true),'UTF8'),'sha256'),'hex'),
        'definition_length', length(pg_get_constraintdef(con.oid,true))
      ) ORDER BY n.nspname,c.relname,con.conname)
      FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    ), '[]'::jsonb),
    'index', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', i.relname, 'identity', t.relname||'.'||i.relname,
        'relation', t.relname, 'owner', pg_get_userbyid(i.relowner),
        'access_method', am.amname, 'unique', x.indisunique, 'primary', x.indisprimary,
        'valid', x.indisvalid, 'ready', x.indisready, 'clustered', x.indisclustered,
        'replica_identity', x.indisreplident,
        'definition_sha256', encode(digest(convert_to(pg_get_indexdef(i.oid,0,true),'UTF8'),'sha256'),'hex'),
        'definition_length', length(pg_get_indexdef(i.oid,0,true)),
        'predicate_sha256', CASE WHEN x.indpred IS NULL THEN NULL ELSE encode(digest(convert_to(pg_get_expr(x.indpred,x.indrelid,true),'UTF8'),'sha256'),'hex') END,
        'predicate_length', CASE WHEN x.indpred IS NULL THEN 0 ELSE length(pg_get_expr(x.indpred,x.indrelid,true)) END
      ) ORDER BY n.nspname,t.relname,i.relname)
      FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_class t ON t.oid=x.indrelid
      JOIN pg_namespace n ON n.oid=t.relnamespace JOIN pg_am am ON am.oid=i.relam
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    ), '[]'::jsonb),
    'sequence', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', c.relname, 'identity', c.relname,
        'owner', pg_get_userbyid(c.relowner), 'data_type', format_type(s.seqtypid,NULL),
        'start', s.seqstart, 'increment', s.seqincrement, 'minimum', s.seqmin,
        'maximum', s.seqmax, 'cache', s.seqcache, 'cycle', s.seqcycle,
        'owned_by', (SELECT nt.nspname||'.'||t.relname||'.'||a.attname FROM pg_depend dep JOIN pg_class t ON t.oid=dep.refobjid JOIN pg_namespace nt ON nt.oid=t.relnamespace JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=dep.refobjsubid WHERE dep.classid='pg_class'::regclass AND dep.objid=c.oid AND dep.deptype IN ('a','i') ORDER BY dep.deptype LIMIT 1),
        'acl', to_jsonb(ARRAY(SELECT value::text FROM unnest(COALESCE(c.relacl, acldefault('S',c.relowner))) value ORDER BY value::text))
      ) ORDER BY n.nspname,c.relname)
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_sequence s ON s.seqrelid=c.oid
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    ), '[]'::jsonb),
    'view', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', c.relname, 'identity', c.relname,
        'view_kind', c.relkind, 'owner', pg_get_userbyid(c.relowner),
        'populated', CASE WHEN c.relkind='m' THEN c.relispopulated ELSE NULL END,
        'definition_sha256', encode(digest(convert_to(pg_get_viewdef(c.oid,true),'UTF8'),'sha256'),'hex'),
        'definition_length', length(pg_get_viewdef(c.oid,true)),
        'acl', to_jsonb(ARRAY(SELECT value::text FROM unnest(COALESCE(c.relacl, acldefault('r',c.relowner))) value ORDER BY value::text))
      ) ORDER BY n.nspname,c.relname)
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND c.relkind IN ('v','m')
    ), '[]'::jsonb),
    'function', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', p.proname,
        'identity', p.proname||'('||pg_get_function_identity_arguments(p.oid)||')',
        'kind', p.prokind, 'result', pg_get_function_result(p.oid),
        'owner', pg_get_userbyid(p.proowner), 'language', l.lanname,
        'volatility', p.provolatile, 'parallel', p.proparallel,
        'security_definer', p.prosecdef, 'leakproof', p.proleakproof, 'strict', p.proisstrict,
        'definition_sha256', encode(digest(convert_to(pg_get_functiondef(p.oid),'UTF8'),'sha256'),'hex'),
        'definition_length', length(pg_get_functiondef(p.oid)),
        'config_sha256', CASE WHEN p.proconfig IS NULL THEN NULL ELSE encode(digest(convert_to(array_to_string(p.proconfig,E'\\n'),'UTF8'),'sha256'),'hex') END,
        'acl', to_jsonb(ARRAY(SELECT value::text FROM unnest(COALESCE(p.proacl, acldefault('f',p.proowner))) value ORDER BY value::text))
      ) ORDER BY n.nspname,p.proname,pg_get_function_identity_arguments(p.oid))
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND p.prokind IN ('f','p','w')
    ), '[]'::jsonb),
    'trigger', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', tg.tgname, 'identity', c.relname||'.'||tg.tgname,
        'relation', c.relname, 'enabled', tg.tgenabled, 'internal', tg.tgisinternal,
        'function', tg.tgfoid::regprocedure::text,
        'definition_sha256', encode(digest(convert_to(pg_get_triggerdef(tg.oid,true),'UTF8'),'sha256'),'hex'),
        'definition_length', length(pg_get_triggerdef(tg.oid,true))
      ) ORDER BY n.nspname,c.relname,tg.tgname)
      FROM pg_trigger tg JOIN pg_class c ON c.oid=tg.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    ), '[]'::jsonb),
    'policy', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', pol.polname, 'identity', c.relname||'.'||pol.polname,
        'relation', c.relname, 'permissive', pol.polpermissive, 'command', pol.polcmd,
        'roles', to_jsonb(ARRAY(SELECT pg_get_userbyid(role_oid) FROM unnest(pol.polroles) role_oid ORDER BY pg_get_userbyid(role_oid))),
        'using_sha256', CASE WHEN pol.polqual IS NULL THEN NULL ELSE encode(digest(convert_to(pg_get_expr(pol.polqual,pol.polrelid,true),'UTF8'),'sha256'),'hex') END,
        'using_length', CASE WHEN pol.polqual IS NULL THEN 0 ELSE length(pg_get_expr(pol.polqual,pol.polrelid,true)) END,
        'check_sha256', CASE WHEN pol.polwithcheck IS NULL THEN NULL ELSE encode(digest(convert_to(pg_get_expr(pol.polwithcheck,pol.polrelid,true),'UTF8'),'sha256'),'hex') END,
        'check_length', CASE WHEN pol.polwithcheck IS NULL THEN 0 ELSE length(pg_get_expr(pol.polwithcheck,pol.polrelid,true)) END
      ) ORDER BY n.nspname,c.relname,pol.polname)
      FROM pg_policy pol JOIN pg_class c ON c.oid=pol.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    ), '[]'::jsonb),
    'type', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', t.typname, 'identity', t.typname,
        'type_kind', t.typtype, 'category', t.typcategory, 'owner', pg_get_userbyid(t.typowner),
        'base_type', CASE WHEN t.typbasetype=0 THEN NULL ELSE format_type(t.typbasetype,t.typtypmod) END,
        'not_null', t.typnotnull,
        'default_sha256', CASE WHEN t.typdefault IS NULL THEN NULL ELSE encode(digest(convert_to(t.typdefault,'UTF8'),'sha256'),'hex') END,
        'enum_labels', COALESCE((SELECT to_jsonb(ARRAY(SELECT e.enumlabel FROM pg_enum e WHERE e.enumtypid=t.oid ORDER BY e.enumsortorder))), '[]'::jsonb),
        'acl', to_jsonb(ARRAY(SELECT value::text FROM unnest(COALESCE(t.typacl, acldefault('T',t.typowner))) value ORDER BY value::text))
      ) ORDER BY n.nspname,t.typname)
      FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
      WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND t.typtype IN ('e','d','r','m') AND t.typelem=0
    ), '[]'::jsonb),
    'extension', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'schema', n.nspname, 'name', e.extname, 'identity', e.extname,
        'version', e.extversion, 'owner', pg_get_userbyid(e.extowner), 'relocatable', e.extrelocatable
      ) ORDER BY e.extname)
      FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
    ), '[]'::jsonb),
    'grant', COALESCE((
      SELECT jsonb_agg(item ORDER BY item->>'identity') FROM (
        SELECT jsonb_build_object('schema',n.nspname,'name',value::text,'identity','schema:'||n.nspname||':'||value::text,'object_kind','schema','object_name',n.nspname) item
        FROM pg_namespace n CROSS JOIN LATERAL unnest(COALESCE(n.nspacl,acldefault('n',n.nspowner))) value
        WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
        UNION ALL
        SELECT jsonb_build_object('schema',n.nspname,'name',value::text,'identity','relation:'||c.relname||':'||value::text,'object_kind','relation','object_name',c.relname)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL unnest(COALESCE(c.relacl,acldefault(CASE WHEN c.relkind='S' THEN 'S'::"char" ELSE 'r'::"char" END,c.relowner))) value
        WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','f','v','m','S')
        UNION ALL
        SELECT jsonb_build_object('schema',n.nspname,'name',value::text,'identity','function:'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||'):'||value::text,'object_kind','function','object_name',p.proname)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN LATERAL unnest(COALESCE(p.proacl,acldefault('f',p.proowner))) value
        WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND p.prokind IN ('f','p','w')
      ) grants
    ), '[]'::jsonb),
    'scheduled_job', '[]'::jsonb
  ),
  'scheduler_capabilities', jsonb_build_object(
    'pg_cron_installed', EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_cron'),
    'timescaledb_installed', EXISTS(SELECT 1 FROM pg_extension WHERE extname='timescaledb'),
    'database_job_rows_read', false
  ),
  'migration_history_relations', COALESCE((
    SELECT jsonb_agg(jsonb_build_object('schema',n.nspname,'name',c.relname,'kind',c.relkind) ORDER BY n.nspname,c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND c.relname ~* '(migration|schema.*version|alembic|flyway)'
  ), '[]'::jsonb)
)::text;
COMMIT;
"""


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def sanitize_text(value: str) -> str:
    context = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        digest = hashlib.sha256(token.lower().encode("utf-8")).hexdigest()
        return "subject_" + digest[:16] + "_" + context if digest in SENSITIVE_TOKEN_SHA256 else token
    return re.sub(r"[A-Za-z0-9]+", replace, value)


def sanitize_value(value: object) -> object:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    return value


def canonical_schema_dump(payload: bytes) -> bytes:
    if not payload or b"\0" in payload or b"\r" in payload:
        raise RuntimeError("schema-only dump encoding is invalid")
    restrict_tokens: list[bytes] = []
    unrestrict_tokens: list[bytes] = []
    retained: list[bytes] = []
    for line in payload.splitlines():
        if line.startswith(b"\\restrict "):
            restrict_tokens.append(line.removeprefix(b"\\restrict "))
        elif line.startswith(b"\\unrestrict "):
            unrestrict_tokens.append(line.removeprefix(b"\\unrestrict "))
        else:
            retained.append(line)
    if len(restrict_tokens) != len(unrestrict_tokens) or len(restrict_tokens) > 1 or restrict_tokens != unrestrict_tokens or any(not token for token in restrict_tokens):
        raise RuntimeError("schema-only dump safety token is malformed")
    return b"\n".join(retained) + b"\n"


def run(argv: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=timeout,
        env={"LANG": "C", "LC_ALL": "C"},
    )
    if len(result.stdout) > MAX_OUTPUT or len(result.stderr) > 1024 * 1024:
        raise RuntimeError("subprocess output exceeded the bound")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    for name, value in vars(args).items():
        if not TOKEN_RE.fullmatch(value):
            raise RuntimeError(name + " is malformed")
    prefix = [DOCKER, "exec", "--user", "postgres", args.container]
    query = run(prefix + [PSQL, "-X", "-v", "ON_ERROR_STOP=1", "-U", args.user, "-d", args.database, "-Atqc", CATALOG_SQL], 180)
    if query.returncode != 0 or not query.stdout:
        diagnostic = query.stderr.decode("utf-8", "replace").replace("\r", " ").replace("\0", " ")[:1000]
        raise RuntimeError("read-only catalog query failed: " + diagnostic)
    try:
        payload = json.loads(query.stdout.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("catalog response was malformed") from error
    if not isinstance(payload, dict):
        raise RuntimeError("catalog response was not an object")
    payload = sanitize_value(payload)
    proof = payload.get("read_only_proof")
    if not isinstance(proof, dict) or proof.get("transaction_read_only") != "on" or proof.get("transaction_isolation") != "repeatable read":
        raise RuntimeError("database did not enforce the read-only transaction")
    dump = run(prefix + [PG_DUMP, "-U", args.user, "-d", args.database, "--schema-only", "--no-password"], 180)
    if dump.returncode != 0 or not dump.stdout:
        raise RuntimeError("schema-only dump fingerprint failed")
    canonical_dump = canonical_schema_dump(dump.stdout)
    output = {
        "schema_version": SCHEMA_VERSION,
        "canonicalization": CANONICALIZATION,
        "source": {"container": args.container, "database": args.database, "user": args.user},
        "read_only_proof": payload.pop("read_only_proof"),
        "observed_at_utc": payload.pop("observed_at_utc"),
        "database": payload.pop("database"),
        "objects": payload.pop("objects"),
        "scheduler_capabilities": payload.pop("scheduler_capabilities"),
        "migration_history_relations": payload.pop("migration_history_relations"),
        "schema_only_dump": {
            "sha256": hashlib.sha256(canonical_dump).hexdigest(),
            "raw_size": len(dump.stdout),
            "canonical_size": len(canonical_dump),
            "canonicalization": "strip-matched-pg-restrict-token-lf-v1",
            "contains_row_data": False,
            "retained": False,
        },
    }
    if payload:
        raise RuntimeError("unexpected catalog response fields")
    sys.stdout.buffer.write(canonical_bytes(output))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("catalog snapshot failed: " + type(error).__name__ + ": " + str(error)[:1200], file=sys.stderr)
        raise SystemExit(2)
