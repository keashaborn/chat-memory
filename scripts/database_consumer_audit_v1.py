#!/usr/bin/env python3
from __future__ import annotations

"""Build a content-free consumer map for isolated LifeSwitch database objects."""

import argparse
import hashlib
import json
import os
import re
import runpy
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "seebx-lifeswitch-database-consumer-audit-v1"
CONTAINER = "lifeswitch-postgres-current"
DATABASE = "lifeswitch"
ADMIN_ROLE = "lifeswitch_bootstrap"
DOCKER = "/usr/bin/docker"
OUTPUT_ROOT = Path("/var/backups/seebx-cleanup/lifeswitch-database-consumers-v1")
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,39}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SCOPED_SCHEMAS = (
    "catalog_dev",
    "lifeswitch_chat",
    "lifeswitch_nutrition",
    "lifeswitch_plan",
    "lifeswitch_snapshot",
    "lifeswitch_training",
    "public",
)
RUNTIME_ROOTS = (Path("app.py"), Path("seebx"))
OPERATIONAL_ROOTS = (Path("scripts"),)
MIGRATION_ROOTS = (Path("ops/sql"),)
SOURCE_SUFFIXES = frozenset({".py", ".sql"})
MAX_SOURCE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class AuditSpec:
    schema_version: str
    container: str
    database: str
    admin_role: str
    output_root: Path
    scoped_schemas: tuple[str, ...]
    runtime_roots: tuple[Path, ...]
    operational_roots: tuple[Path, ...]
    migration_roots: tuple[Path, ...]
    require_source_manifest: bool = True
    include_governance_manifest: bool = False
    runtime_excluded_paths: tuple[Path, ...] = ()


DEFAULT_SPEC = AuditSpec(
    schema_version=SCHEMA_VERSION,
    container=CONTAINER,
    database=DATABASE,
    admin_role=ADMIN_ROLE,
    output_root=OUTPUT_ROOT,
    scoped_schemas=SCOPED_SCHEMAS,
    runtime_roots=RUNTIME_ROOTS,
    operational_roots=OPERATIONAL_ROOTS,
    migration_roots=MIGRATION_ROOTS,
)


class AuditContractError(RuntimeError):
    pass


class AuditExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseObject:
    oid: int
    object_type: str
    schema: str
    name: str
    identity_arguments: str = ""
    relation_kind: str = ""
    extension: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def identity(self) -> str:
        if self.object_type == "function":
            return f"{self.qualified_name}({self.identity_arguments})"
        return self.qualified_name


OBJECTS_SQL = r"""
with relation_objects as (
  select c.oid::bigint oid,'relation'::text object_type,n.nspname schema,
         c.relname name,''::text identity_arguments,c.relkind::text relation_kind,
         coalesce((select e.extname from pg_depend d join pg_extension e on e.oid=d.refobjid
                   where d.classid='pg_class'::regclass and d.objid=c.oid
                     and d.refclassid='pg_extension'::regclass and d.deptype='e'
                   limit 1),'') extension
  from pg_class c join pg_namespace n on n.oid=c.relnamespace
  where n.nspname = any(array[{schemas}]::text[])
    and c.relkind in ('r','p','v','m','S','f')
), function_objects as (
  select p.oid::bigint oid,'function'::text object_type,n.nspname schema,
         p.proname name,pg_get_function_identity_arguments(p.oid) identity_arguments,
         ''::text relation_kind,
         coalesce((select e.extname from pg_depend d join pg_extension e on e.oid=d.refobjid
                   where d.classid='pg_proc'::regclass and d.objid=p.oid
                     and d.refclassid='pg_extension'::regclass and d.deptype='e'
                   limit 1),'') extension
  from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname = any(array[{schemas}]::text[])
)
select coalesce(jsonb_agg(to_jsonb(objects) order by schema,object_type,name,identity_arguments),'[]'::jsonb)
from (select * from relation_objects union all select * from function_objects) objects
"""


DEFINITIONS_SQL = r"""
with function_definitions as (
  select 'function'::text consumer_type,p.oid::bigint consumer_oid,
         pg_get_functiondef(p.oid) definition
  from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname = any(array[{schemas}]::text[])
    and not exists (
      select 1 from pg_depend d
      where d.classid='pg_proc'::regclass and d.objid=p.oid
        and d.refclassid='pg_extension'::regclass and d.deptype='e'
    )
), view_definitions as (
  select 'relation'::text consumer_type,c.oid::bigint consumer_oid,
         pg_get_viewdef(c.oid,true) definition
  from pg_class c join pg_namespace n on n.oid=c.relnamespace
  where n.nspname = any(array[{schemas}]::text[]) and c.relkind in ('v','m')
), policy_definitions as (
  select 'relation'::text consumer_type,p.polrelid::bigint consumer_oid,
         concat_ws(' ',pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid)) definition
  from pg_policy p join pg_class c on c.oid=p.polrelid
  join pg_namespace n on n.oid=c.relnamespace
  where n.nspname = any(array[{schemas}]::text[])
)
select coalesce(jsonb_agg(to_jsonb(definitions) order by consumer_type,consumer_oid),'[]'::jsonb)
from (
  select * from function_definitions
  union all select * from view_definitions
  union all select * from policy_definitions
) definitions
"""


EXPLICIT_EDGES_SQL = r"""
with trigger_edges as (
  select 'relation'::text consumer_type,t.tgrelid::bigint consumer_oid,
         'function'::text referenced_type,t.tgfoid::bigint referenced_oid,
         'trigger'::text evidence
  from pg_trigger t join pg_class c on c.oid=t.tgrelid
  join pg_namespace n on n.oid=c.relnamespace
  where not t.tgisinternal and n.nspname = any(array[{schemas}]::text[])
), foreign_key_edges as (
  select 'relation'::text consumer_type,k.conrelid::bigint consumer_oid,
         'relation'::text referenced_type,k.confrelid::bigint referenced_oid,
         'foreign_key'::text evidence
  from pg_constraint k join pg_class c on c.oid=k.conrelid
  join pg_namespace n on n.oid=c.relnamespace
  where k.contype='f' and n.nspname = any(array[{schemas}]::text[])
), default_edges as (
  select 'relation'::text consumer_type,a.adrelid::bigint consumer_oid,
         case d.refclassid when 'pg_class'::regclass then 'relation'::text
                           when 'pg_proc'::regclass then 'function'::text end referenced_type,
         d.refobjid::bigint referenced_oid,'column_default'::text evidence
  from pg_attrdef a join pg_class c on c.oid=a.adrelid
  join pg_namespace n on n.oid=c.relnamespace
  join pg_depend d on d.classid='pg_attrdef'::regclass and d.objid=a.oid
  where n.nspname = any(array[{schemas}]::text[])
    and d.refclassid in ('pg_class'::regclass,'pg_proc'::regclass)
    and d.deptype in ('n','a')
)
select coalesce(jsonb_agg(to_jsonb(edges) order by consumer_type,consumer_oid,referenced_type,referenced_oid,evidence),'[]'::jsonb)
from (
  select * from trigger_edges union all select * from foreign_key_edges
  union all select * from default_edges
) edges
"""


GOVERNANCE_SCHEMAS_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'name',n.nspname,'owner',pg_get_userbyid(n.nspowner),
  'acl',coalesce((select jsonb_agg(item::text order by item::text)
                  from unnest(coalesce(n.nspacl,acldefault('n',n.nspowner))) item),
                 '[]'::jsonb)
) order by n.nspname),'[]'::jsonb)
from pg_namespace n
where n.nspname = any(array[{schemas}]::text[])
"""


GOVERNANCE_RELATIONS_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',n.nspname,'name',c.relname,'kind',c.relkind::text,
  'persistence',c.relpersistence::text,'owner',pg_get_userbyid(c.relowner),
  'acl',coalesce((select jsonb_agg(item::text order by item::text)
                  from unnest(coalesce(c.relacl,acldefault(
                    (case when c.relkind='S' then 'S' else 'r' end)::"char",
                    c.relowner
                  ))) item),'[]'::jsonb),
  'rls_enabled',c.relrowsecurity,'rls_forced',c.relforcerowsecurity,
  'replica_identity',c.relreplident::text,'is_partition',c.relispartition,
  'definition',case when c.relkind in ('v','m') then pg_get_viewdef(c.oid,true) else '' end
) order by n.nspname,c.relname),'[]'::jsonb)
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where n.nspname = any(array[{schemas}]::text[])
  and c.relkind in ('r','p','v','m','S','f')
"""


GOVERNANCE_COLUMNS_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',n.nspname,'relation',c.relname,'name',a.attname,
  'ordinal',a.attnum,'type',pg_catalog.format_type(a.atttypid,a.atttypmod),
  'not_null',a.attnotnull,'identity',a.attidentity::text,
  'generated',a.attgenerated::text,'dropped',a.attisdropped,
  'collation',case when a.attcollation=0 then '' else coalesce(coll.collname,'') end,
  'acl',coalesce((select jsonb_agg(item::text order by item::text)
                  from unnest(coalesce(a.attacl,'{}'::aclitem[])) item),'[]'::jsonb),
  'default_definition',coalesce(pg_get_expr(ad.adbin,ad.adrelid),'')
) order by n.nspname,c.relname,a.attnum),'[]'::jsonb)
from pg_attribute a
join pg_class c on c.oid=a.attrelid
join pg_namespace n on n.oid=c.relnamespace
left join pg_attrdef ad on ad.adrelid=a.attrelid and ad.adnum=a.attnum
left join pg_collation coll on coll.oid=a.attcollation
where n.nspname = any(array[{schemas}]::text[])
  and c.relkind in ('r','p','v','m','f') and a.attnum>0
"""


GOVERNANCE_FUNCTIONS_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',n.nspname,'name',p.proname,'kind',p.prokind::text,
  'identity_arguments',pg_get_function_identity_arguments(p.oid),
  'result',pg_get_function_result(p.oid),'language',l.lanname,
  'owner',pg_get_userbyid(p.proowner),
  'acl',coalesce((select jsonb_agg(item::text order by item::text)
                  from unnest(coalesce(p.proacl,acldefault('f',p.proowner))) item),
                 '[]'::jsonb),
  'security_definer',p.prosecdef,'leakproof',p.proleakproof,
  'strict',p.proisstrict,'volatility',p.provolatile::text,
  'parallel',p.proparallel::text,'configuration',coalesce(p.proconfig::text,''),
  'definition',case when p.prokind in ('f','p') then pg_get_functiondef(p.oid)
                    else concat_ws('|',p.prosrc,coalesce(p.probin,''),
                                   pg_get_function_result(p.oid)) end
) order by n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)),'[]'::jsonb)
from pg_proc p
join pg_namespace n on n.oid=p.pronamespace
join pg_language l on l.oid=p.prolang
where n.nspname = any(array[{schemas}]::text[])
"""


GOVERNANCE_POLICIES_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',schemaname,'relation',tablename,'name',policyname,
  'permissive',permissive,'roles',roles,'command',cmd,
  'using_definition',coalesce(qual,''),'check_definition',coalesce(with_check,'')
) order by schemaname,tablename,policyname),'[]'::jsonb)
from pg_policies
where schemaname = any(array[{schemas}]::text[])
"""


GOVERNANCE_TRIGGERS_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',n.nspname,'relation',c.relname,'name',t.tgname,
  'enabled',t.tgenabled::text,
  'function',pn.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')',
  'definition',pg_get_triggerdef(t.oid,true)
) order by n.nspname,c.relname,t.tgname),'[]'::jsonb)
from pg_trigger t
join pg_class c on c.oid=t.tgrelid
join pg_namespace n on n.oid=c.relnamespace
join pg_proc p on p.oid=t.tgfoid
join pg_namespace pn on pn.oid=p.pronamespace
where not t.tgisinternal and n.nspname = any(array[{schemas}]::text[])
"""


GOVERNANCE_CONSTRAINTS_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',n.nspname,'relation',c.relname,'name',k.conname,
  'type',k.contype::text,'validated',k.convalidated,
  'deferrable',k.condeferrable,'deferred',k.condeferred,
  'definition',pg_get_constraintdef(k.oid,true)
) order by n.nspname,c.relname,k.conname),'[]'::jsonb)
from pg_constraint k
join pg_class c on c.oid=k.conrelid
join pg_namespace n on n.oid=c.relnamespace
where n.nspname = any(array[{schemas}]::text[])
"""


GOVERNANCE_INDEXES_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',n.nspname,'relation',table_class.relname,'name',index_class.relname,
  'unique',i.indisunique,'primary',i.indisprimary,'valid',i.indisvalid,
  'ready',i.indisready,'live',i.indislive,'replica_identity',i.indisreplident,
  'definition',pg_get_indexdef(i.indexrelid)
) order by n.nspname,table_class.relname,index_class.relname),'[]'::jsonb)
from pg_index i
join pg_class table_class on table_class.oid=i.indrelid
join pg_class index_class on index_class.oid=i.indexrelid
join pg_namespace n on n.oid=table_class.relnamespace
where n.nspname = any(array[{schemas}]::text[])
"""


GOVERNANCE_EXTENSIONS_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'name',e.extname,'version',e.extversion,'schema',n.nspname,
  'relocatable',e.extrelocatable
) order by e.extname),'[]'::jsonb)
from pg_extension e join pg_namespace n on n.oid=e.extnamespace
"""


GOVERNANCE_ROLES_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'name',rolname,'superuser',rolsuper,'inherit',rolinherit,
  'create_role',rolcreaterole,'create_db',rolcreatedb,'can_login',rolcanlogin,
  'replication',rolreplication,'bypass_rls',rolbypassrls,
  'connection_limit',rolconnlimit,'valid_until',coalesce(rolvaliduntil::text,''),
  'configuration',coalesce(rolconfig::text,'')
) order by rolname),'[]'::jsonb)
from pg_roles where rolname !~ '^pg_'
"""


GOVERNANCE_MEMBERSHIPS_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'member',member_role.rolname,'granted',granted_role.rolname,
  'grantor',grantor_role.rolname,'admin_option',m.admin_option
) order by member_role.rolname,granted_role.rolname,grantor_role.rolname),'[]'::jsonb)
from pg_auth_members m
join pg_roles member_role on member_role.oid=m.member
join pg_roles granted_role on granted_role.oid=m.roleid
join pg_roles grantor_role on grantor_role.oid=m.grantor
where member_role.rolname !~ '^pg_' or granted_role.rolname !~ '^pg_'
"""


GOVERNANCE_SEQUENCES_SQL = r"""
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',schemaname,'name',sequencename,'owner',sequenceowner,
  'type',data_type,'start',start_value,'minimum',min_value,'maximum',max_value,
  'increment',increment_by,'cycle',cycle,'cache',cache_size,'last_value',last_value
) order by schemaname,sequencename),'[]'::jsonb)
from pg_sequences
where schemaname = any(array[{schemas}]::text[])
"""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_run_id(value: str) -> str:
    if RUN_ID.fullmatch(value) is None:
        raise AuditContractError("run_id_invalid")
    return value


def validate_commit(value: str) -> str:
    if COMMIT.fullmatch(value) is None:
        raise AuditContractError("candidate_commit_invalid")
    return value


def validate_manifest_sha256(value: str) -> str:
    if SHA256.fullmatch(value) is None:
        raise AuditContractError("source_manifest_sha256_invalid")
    return value


def sql_literal(value: str) -> str:
    if "\x00" in value:
        raise AuditContractError("sql_literal_invalid")
    return "'" + value.replace("'", "''") + "'"


def rendered_sql(
    template: str,
    scoped_schemas: tuple[str, ...] = SCOPED_SCHEMAS,
) -> str:
    schemas = ",".join(sql_literal(schema) for schema in scoped_schemas)
    if "{schemas}" not in template:
        return template
    return template.replace("{schemas}", schemas)


def run_text(command: list[str], *, label: str, timeout: int = 180) -> str:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AuditExecutionError(f"{label}_execution_failed") from error
    if completed.returncode != 0:
        raise AuditExecutionError(f"{label}_failed")
    return completed.stdout.strip()


def verify_repository_state(repository: Path, candidate_commit: str) -> None:
    actual_commit = run_text(
        [
            "/usr/bin/git",
            "-c",
            f"safe.directory={repository}",
            "-C",
            str(repository),
            "rev-parse",
            "HEAD",
        ],
        label="candidate_commit",
    )
    if actual_commit != candidate_commit:
        raise AuditContractError("candidate_commit_mismatch")
    worktree_status = run_text(
        [
            "/usr/bin/git",
            "-c",
            f"safe.directory={repository}",
            "-C",
            str(repository),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        label="candidate_worktree",
    )
    if worktree_status:
        raise AuditContractError("candidate_worktree_not_clean")


def psql_json(
    sql: str,
    *,
    label: str,
    spec: AuditSpec = DEFAULT_SPEC,
) -> list[dict[str, Any]]:
    wrapped = "begin isolation level repeatable read read only;" + sql + ";rollback;"
    raw = run_text(
        [
            DOCKER,
            "exec",
            spec.container,
            "psql",
            "-X",
            "--no-psqlrc",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            spec.admin_role,
            "-d",
            spec.database,
            "-c",
            wrapped,
        ],
        label=label,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AuditExecutionError(f"{label}_json_invalid") from error
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AuditExecutionError(f"{label}_shape_invalid")
    return value


def psql_scalar(
    sql: str,
    *,
    label: str,
    spec: AuditSpec = DEFAULT_SPEC,
) -> str:
    wrapped = "begin isolation level repeatable read read only;" + sql + ";rollback;"
    return run_text(
        [
            DOCKER,
            "exec",
            spec.container,
            "psql",
            "-X",
            "--no-psqlrc",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            spec.admin_role,
            "-d",
            spec.database,
            "-c",
            wrapped,
        ],
        label=label,
    )


def quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise AuditContractError("identifier_invalid")
    return '"' + value.replace('"', '""') + '"'


def hash_definition_fields(
    rows: Iterable[dict[str, Any]],
    fields: Iterable[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for field in fields:
            if field not in item:
                continue
            value = item.pop(field)
            if not isinstance(value, str):
                raise AuditExecutionError("governance_definition_invalid")
            item[field + "_sha256"] = sha256_bytes(value.encode("utf-8"))
        result.append(item)
    return result


def collect_exact_table_counts(
    relations: Iterable[dict[str, Any]],
    *,
    spec: AuditSpec = DEFAULT_SPEC,
) -> list[dict[str, Any]]:
    counts: list[dict[str, Any]] = []
    for relation in relations:
        if str(relation.get("kind") or "") not in {"r", "p"}:
            continue
        schema = str(relation.get("schema") or "")
        name = str(relation.get("name") or "")
        if schema not in spec.scoped_schemas:
            raise AuditExecutionError("governance_relation_schema_invalid")
        raw = psql_scalar(
            "select count(*)::text from "
            + quote_identifier(schema)
            + "."
            + quote_identifier(name),
            label="governance_table_count",
            spec=spec,
        )
        try:
            count = int(raw)
        except ValueError as error:
            raise AuditExecutionError("governance_table_count_invalid") from error
        if count < 0:
            raise AuditExecutionError("governance_table_count_invalid")
        counts.append({"schema": schema, "relation": name, "row_count": count})
    return sorted(counts, key=lambda item: (item["schema"], item["relation"]))


def collect_governance_manifest(
    *,
    spec: AuditSpec = DEFAULT_SPEC,
) -> dict[str, Any]:
    queries = (
        ("schemas", GOVERNANCE_SCHEMAS_SQL, ()),
        ("relations", GOVERNANCE_RELATIONS_SQL, ("definition",)),
        ("columns", GOVERNANCE_COLUMNS_SQL, ("default_definition",)),
        ("functions", GOVERNANCE_FUNCTIONS_SQL, ("definition",)),
        (
            "policies",
            GOVERNANCE_POLICIES_SQL,
            ("using_definition", "check_definition"),
        ),
        ("triggers", GOVERNANCE_TRIGGERS_SQL, ("definition",)),
        ("constraints", GOVERNANCE_CONSTRAINTS_SQL, ("definition",)),
        ("indexes", GOVERNANCE_INDEXES_SQL, ("definition",)),
        ("extensions", GOVERNANCE_EXTENSIONS_SQL, ()),
        ("roles", GOVERNANCE_ROLES_SQL, ()),
        ("memberships", GOVERNANCE_MEMBERSHIPS_SQL, ()),
        ("sequences", GOVERNANCE_SEQUENCES_SQL, ()),
    )
    sections: dict[str, list[dict[str, Any]]] = {}
    raw_relations: list[dict[str, Any]] = []
    for name, query, definition_fields in queries:
        rows = psql_json(
            rendered_sql(query, spec.scoped_schemas),
            label="governance_" + name,
            spec=spec,
        )
        if name == "relations":
            raw_relations = rows
        sections[name] = hash_definition_fields(rows, definition_fields)
    sections["table_counts"] = collect_exact_table_counts(
        raw_relations,
        spec=spec,
    )
    section_hashes = {
        name: sha256_bytes(canonical_bytes(rows))
        for name, rows in sorted(sections.items())
    }
    document = {
        "schema_version": "seebx-database-governance-manifest-v1",
        "database": {"container": spec.container, "name": spec.database},
        "scope": {
            "schemas": list(spec.scoped_schemas),
            "row_content_included": False,
            "definitions_included": False,
            "definition_hash_algorithm": "sha256",
            "table_counts_exact": True,
            "password_verifiers_included": False,
        },
        "section_hashes": section_hashes,
        "sections": sections,
    }
    return {
        **document,
        "manifest_sha256": sha256_bytes(canonical_bytes(document)),
    }


def current_source_manifest_sha256(
    verifier: Path | None = None,
    *,
    database: str = DATABASE,
) -> str:
    verifier = verifier or Path(__file__).with_name("verify_lifeswitch_disposable_restore_v1.py")
    if verifier.is_symlink() or not verifier.is_file():
        raise AuditContractError("source_manifest_verifier_missing")
    namespace = runpy.run_path(str(verifier))
    collector = namespace.get("collect_manifest")
    if not callable(collector):
        raise AuditContractError("source_manifest_collector_missing")
    manifest = collector(database)
    if not isinstance(manifest, dict):
        raise AuditExecutionError("source_manifest_shape_invalid")
    value = str(manifest.get("manifest_sha256") or "")
    return validate_manifest_sha256(value)


def parse_objects(
    rows: Iterable[dict[str, Any]],
    scoped_schemas: tuple[str, ...] = SCOPED_SCHEMAS,
) -> list[DatabaseObject]:
    objects: list[DatabaseObject] = []
    seen_identity: set[str] = set()
    seen_oid: set[tuple[str, int]] = set()
    for row in rows:
        try:
            item = DatabaseObject(
                oid=int(row["oid"]),
                object_type=str(row["object_type"]),
                schema=str(row["schema"]),
                name=str(row["name"]),
                identity_arguments=str(row.get("identity_arguments") or ""),
                relation_kind=str(row.get("relation_kind") or ""),
                extension=str(row.get("extension") or ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AuditExecutionError("database_object_invalid") from error
        if item.object_type not in {"relation", "function"}:
            raise AuditExecutionError("database_object_type_invalid")
        if item.schema not in scoped_schemas or not item.name or "\x00" in item.name:
            raise AuditExecutionError("database_object_identity_invalid")
        oid_key = (item.object_type, item.oid)
        if item.identity in seen_identity or oid_key in seen_oid:
            raise AuditExecutionError("database_object_duplicate")
        seen_identity.add(item.identity)
        seen_oid.add(oid_key)
        objects.append(item)
    return sorted(objects, key=lambda item: item.identity)


def _regular_source_files(
    repository: Path,
    roots: Iterable[Path],
    excluded_paths: Iterable[Path] = (),
) -> list[Path]:
    excluded: set[Path] = set()
    for path in excluded_paths:
        if path.is_absolute() or ".." in path.parts:
            raise AuditContractError("source_exclusion_invalid")
        excluded.add(path)
    files: list[Path] = []
    for relative_root in roots:
        root = repository / relative_root
        candidates = [root] if root.is_file() else root.rglob("*") if root.is_dir() else []
        for path in candidates:
            if path.suffix not in SOURCE_SUFFIXES or "__pycache__" in path.parts:
                continue
            if path.relative_to(repository) in excluded:
                continue
            if path.is_symlink() or not path.is_file():
                raise AuditContractError("source_path_not_regular")
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise AuditContractError("source_file_too_large")
            files.append(path)
    return sorted(set(files))


def scan_sources(
    repository: Path,
    roots: Iterable[Path],
    objects: list[DatabaseObject],
    excluded_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    matches: dict[str, list[str]] = {item.identity: [] for item in objects}
    file_hashes: list[dict[str, str]] = []
    exact_patterns = {
        item.identity: re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(item.qualified_name)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        for item in objects
    }
    template_patterns = {
        item.identity: re.compile(
            rf"\{{(?:self\.)?(?:schema|[A-Za-z_][A-Za-z0-9_]*schema[A-Za-z0-9_]*)\}}\.{re.escape(item.name)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        for item in objects
    }
    schema_bindings = {
        item.identity: re.compile(rf"['\"]{re.escape(item.schema)}['\"]", re.IGNORECASE)
        for item in objects
    }
    selector_templates = {
        item.identity: re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(item.schema)}\.\{{[A-Za-z_][A-Za-z0-9_]*\}}",
            re.IGNORECASE,
        )
        for item in objects
    }
    quoted_names = {
        item.identity: re.compile(rf"['\"]{re.escape(item.name)}['\"]", re.IGNORECASE)
        for item in objects
    }
    for path in _regular_source_files(repository, roots, excluded_paths):
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AuditContractError("source_not_utf8") from error
        relative = path.relative_to(repository).as_posix()
        file_hashes.append({"path": relative, "sha256": sha256_bytes(data)})
        for item in objects:
            identity = item.identity
            exact = exact_patterns[identity].search(text) is not None
            bound_template = (
                template_patterns[identity].search(text) is not None
                and schema_bindings[identity].search(text) is not None
            )
            selector = (
                item.object_type == "function"
                and selector_templates[identity].search(text) is not None
                and quoted_names[identity].search(text) is not None
            )
            if exact or bound_template or selector:
                matches[identity].append(relative)
    tree_hash = sha256_bytes(canonical_bytes(file_hashes))
    return {
        "tree_sha256": tree_hash,
        "file_count": len(file_hashes),
        "matches": {key: value for key, value in matches.items() if value},
    }


def definition_edges(
    objects: list[DatabaseObject], definitions: Iterable[dict[str, Any]]
) -> list[dict[str, str]]:
    by_oid = {(item.object_type, item.oid): item for item in objects}
    patterns = {
        item.identity: re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(item.qualified_name)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        for item in objects
    }
    edges: set[tuple[str, str, str]] = set()
    for row in definitions:
        try:
            consumer = by_oid[(str(row["consumer_type"]), int(row["consumer_oid"]))]
            definition = str(row.get("definition") or "")
        except (KeyError, TypeError, ValueError) as error:
            raise AuditExecutionError("definition_row_invalid") from error
        for referenced in objects:
            if referenced.identity == consumer.identity:
                continue
            if patterns[referenced.identity].search(definition):
                edges.add((consumer.identity, referenced.identity, "definition"))
    return [
        {"consumer": consumer, "referenced": referenced, "evidence": evidence}
        for consumer, referenced, evidence in sorted(edges)
    ]


def explicit_edges(
    objects: list[DatabaseObject], rows: Iterable[dict[str, Any]]
) -> list[dict[str, str]]:
    by_oid = {(item.object_type, item.oid): item for item in objects}
    edges: set[tuple[str, str, str]] = set()
    for row in rows:
        try:
            consumer = by_oid[(str(row["consumer_type"]), int(row["consumer_oid"]))]
            referenced = by_oid[(str(row["referenced_type"]), int(row["referenced_oid"]))]
            evidence = str(row["evidence"])
        except KeyError:
            continue
        except (TypeError, ValueError) as error:
            raise AuditExecutionError("explicit_edge_invalid") from error
        if consumer.identity != referenced.identity:
            edges.add((consumer.identity, referenced.identity, evidence))
    return [
        {"consumer": consumer, "referenced": referenced, "evidence": evidence}
        for consumer, referenced, evidence in sorted(edges)
    ]


def classify_objects(
    objects: list[DatabaseObject],
    runtime_matches: dict[str, list[str]],
    operational_matches: dict[str, list[str]],
    migration_matches: dict[str, list[str]],
    edges: list[dict[str, str]],
) -> list[dict[str, Any]]:
    seeds = {
        item.identity
        for item in objects
        if item.extension or runtime_matches.get(item.identity)
    }
    adjacency: dict[str, set[str]] = {}
    incoming: dict[str, list[dict[str, str]]] = {}
    for edge in edges:
        adjacency.setdefault(edge["consumer"], set()).add(edge["referenced"])
        incoming.setdefault(edge["referenced"], []).append(edge)
    reachable = set(seeds)
    frontier = list(sorted(seeds))
    while frontier:
        consumer = frontier.pop()
        for referenced in sorted(adjacency.get(consumer, set())):
            if referenced not in reachable:
                reachable.add(referenced)
                frontier.append(referenced)
    classified: list[dict[str, Any]] = []
    for item in objects:
        if item.extension:
            classification = "extension_owned"
        elif runtime_matches.get(item.identity):
            classification = "application_direct"
        elif item.identity in reachable:
            classification = "database_internal_reachable"
        elif operational_matches.get(item.identity):
            classification = "operational_reference_only"
        elif migration_matches.get(item.identity):
            classification = "migration_only"
        else:
            classification = "unproven"
        classified.append(
            {
                "identity": item.identity,
                "object_type": item.object_type,
                "relation_kind": item.relation_kind,
                "extension": item.extension,
                "classification": classification,
                "runtime_consumers": runtime_matches.get(item.identity, []),
                "operational_consumers": operational_matches.get(item.identity, []),
                "migration_consumers": migration_matches.get(item.identity, []),
                "database_consumers": sorted(
                    incoming.get(item.identity, []),
                    key=lambda edge: (edge["consumer"], edge["evidence"]),
                ),
            }
        )
    return classified


def prepare_output(
    run_id: str,
    output_root: Path = OUTPUT_ROOT,
) -> Path:
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_root.is_symlink() or not output_root.is_dir():
        raise AuditContractError("output_root_invalid")
    os.chmod(output_root, 0o700)
    output = output_root / validate_run_id(run_id)
    output.mkdir(mode=0o700)
    if output.is_symlink() or stat.S_IMODE(output.stat().st_mode) != 0o700:
        raise AuditContractError("output_directory_invalid")
    return output


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def execute(
    repository: Path,
    run_id: str,
    candidate_commit: str,
    source_manifest_sha256: str | None,
    *,
    spec: AuditSpec = DEFAULT_SPEC,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise AuditContractError("root_required")
    repository = repository.resolve(strict=True)
    if not (repository / ".git").exists() or repository.is_symlink():
        raise AuditContractError("repository_invalid")
    candidate_commit = validate_commit(candidate_commit)
    if spec.require_source_manifest:
        source_manifest_sha256 = validate_manifest_sha256(
            str(source_manifest_sha256 or "")
        )
    elif source_manifest_sha256 is not None:
        raise AuditContractError("source_manifest_not_supported")
    verify_repository_state(repository, candidate_commit)
    if run_text(
        [DOCKER, "inspect", "--format", "{{.State.Running}}", spec.container],
        label="container_state",
    ) != "true":
        raise AuditExecutionError("container_not_running")
    if spec.require_source_manifest and (
        current_source_manifest_sha256(database=spec.database)
        != source_manifest_sha256
    ):
        raise AuditContractError("source_manifest_sha256_mismatch")

    objects = parse_objects(
        psql_json(
            rendered_sql(OBJECTS_SQL, spec.scoped_schemas),
            label="objects",
            spec=spec,
        ),
        spec.scoped_schemas,
    )
    definitions = psql_json(
        rendered_sql(DEFINITIONS_SQL, spec.scoped_schemas),
        label="definitions",
        spec=spec,
    )
    database_edges = definition_edges(objects, definitions)
    database_edges.extend(
        explicit_edges(
            objects,
            psql_json(
                rendered_sql(EXPLICIT_EDGES_SQL, spec.scoped_schemas),
                label="edges",
                spec=spec,
            ),
        )
    )
    database_edges = [dict(item) for item in {tuple(sorted(edge.items())) for edge in database_edges}]
    database_edges.sort(key=lambda edge: (edge["consumer"], edge["referenced"], edge["evidence"]))

    runtime = scan_sources(
        repository,
        spec.runtime_roots,
        objects,
        spec.runtime_excluded_paths,
    )
    operational = scan_sources(repository, spec.operational_roots, objects)
    migration = scan_sources(repository, spec.migration_roots, objects)
    classified = classify_objects(
        objects,
        runtime["matches"],
        operational["matches"],
        migration["matches"],
        database_edges,
    )
    governance_manifest = (
        collect_governance_manifest(spec=spec)
        if spec.include_governance_manifest
        else None
    )
    counts: dict[str, int] = {}
    for item in classified:
        counts[item["classification"]] = counts.get(item["classification"], 0) + 1
    object_catalog = [
        {
            "identity": item.identity,
            "object_type": item.object_type,
            "relation_kind": item.relation_kind,
            "extension": item.extension,
        }
        for item in objects
    ]
    document = {
        "schema_version": spec.schema_version,
        "status": "pass",
        "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "candidate_commit": candidate_commit,
        "source_manifest_sha256": source_manifest_sha256,
        "database": {
            "container": spec.container,
            "name": spec.database,
            "transaction_read_only": True,
        },
        "scope": {
            "schemas": list(spec.scoped_schemas),
            "object_types": ["relation", "function"],
            "unqualified_source_references_are_evidence": False,
            "schema_bound_template_references_are_evidence": True,
            "overload_resolution": "conservative_all_matching_overloads",
            "operational_references_are_retention_seeds": False,
            "governance_manifest_included": spec.include_governance_manifest,
            "deletion_authority": False,
        },
        "source_trees": {
            "runtime": {
                **{key: value for key, value in runtime.items() if key != "matches"},
                "excluded_paths": [path.as_posix() for path in spec.runtime_excluded_paths],
            },
            "operational": {key: value for key, value in operational.items() if key != "matches"},
            "migration": {key: value for key, value in migration.items() if key != "matches"},
        },
        "object_catalog_sha256": sha256_bytes(canonical_bytes(object_catalog)),
        "object_count": len(objects),
        "dependency_edge_count": len(database_edges),
        "classification_counts": dict(sorted(counts.items())),
        "objects": classified,
    }
    if governance_manifest is not None:
        document["governance_manifest"] = governance_manifest
    output = prepare_output(run_id, spec.output_root)
    report = output / "consumer-audit.json"
    report_bytes = canonical_bytes(document) + b"\n"
    atomic_write(report, report_bytes)
    result = {
        "status": "pass",
        "report": str(report),
        "report_sha256": sha256_bytes(report_bytes),
        "object_catalog_sha256": document["object_catalog_sha256"],
        "object_count": len(objects),
        "dependency_edge_count": len(database_edges),
        "classification_counts": document["classification_counts"],
        "deletion_authority": False,
    }
    if governance_manifest is not None:
        result["governance_manifest_sha256"] = governance_manifest["manifest_sha256"]
        result["governance_section_counts"] = {
            name: len(rows)
            for name, rows in governance_manifest["sections"].items()
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = execute(
            arguments.repository,
            arguments.run_id,
            arguments.candidate_commit,
            arguments.source_manifest_sha256,
        )
    except (AuditContractError, AuditExecutionError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
