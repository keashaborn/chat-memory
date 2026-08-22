#!/usr/bin/env python3
from __future__ import annotations

"""Clone and verify the isolated LifeSwitch database without touching source data."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5, NAMESPACE_URL


SCHEMA_VERSION = "seebx-lifeswitch-disposable-restore-receipt-v1"
MANIFEST_VERSION = "seebx-lifeswitch-database-manifest-v1"
CONTAINER = "lifeswitch-postgres-current"
SOURCE_DATABASE = "lifeswitch"
ADMIN_ROLE = "lifeswitch_bootstrap"
APP_ROLE = "lifeswitch_app_login"
OUTPUT_ROOT = Path("/var/backups/seebx-cleanup/lifeswitch-disposable-verify-v1")
CONFIG_PATH = Path("/etc/verbalsage/lifeswitch-postgres.env")
DOCKER = "/usr/bin/docker"
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,39}$")
SAFE_DATABASE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class VerificationContractError(RuntimeError):
    pass


class VerificationExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnerSurface:
    relation: str
    owner_filter: str


SURFACES = (
    OwnerSurface("lifeswitch_chat.account_timezone_v1", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_chat.account_timezone_history_v1", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_nutrition.meal", "row.owner_user_id::text={owner}"),
    OwnerSurface(
        "lifeswitch_nutrition.meal_item",
        "exists (select 1 from lifeswitch_nutrition.meal p "
        "where p.meal_id=row.meal_id and p.owner_user_id::text={owner})",
    ),
    OwnerSurface("lifeswitch_nutrition.meal_plan", "row.owner_user_id::text={owner}"),
    OwnerSurface(
        "lifeswitch_nutrition.meal_plan_item",
        "exists (select 1 from lifeswitch_nutrition.meal_plan p "
        "where p.meal_plan_id=row.meal_plan_id and p.owner_user_id::text={owner})",
    ),
    OwnerSurface("lifeswitch_nutrition.my_food", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_nutrition.my_food_override", "row.owner_user_id::text={owner}"),
    OwnerSurface(
        "lifeswitch_nutrition.my_food_serving",
        "exists (select 1 from lifeswitch_nutrition.my_food p "
        "where p.my_food_id=row.my_food_id and p.owner_user_id::text={owner})",
    ),
    OwnerSurface("lifeswitch_nutrition.nutrition_day", "row.owner_user_id::text={owner}"),
    OwnerSurface(
        "lifeswitch_nutrition.nutrition_day_completion_event",
        "row.owner_user_id::text={owner}",
    ),
    OwnerSurface(
        "lifeswitch_nutrition.nutrition_entry",
        "exists (select 1 from lifeswitch_nutrition.nutrition_day p "
        "where p.nutrition_day_id=row.nutrition_day_id "
        "and p.owner_user_id::text={owner})",
    ),
    OwnerSurface("lifeswitch_plan.plan_profile", "row.owner_user_id::text={owner}"),
    OwnerSurface(
        "lifeswitch_plan.plan_comment",
        "row.target_user_id::text={owner} and exists ("
        "select 1 from lifeswitch_plan.plan_profile p "
        "where p.plan_profile_id=row.plan_profile_id "
        "and p.owner_user_id::text={owner})",
    ),
    OwnerSurface("lifeswitch_plan.plan_profile_history", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.conditioning_session_log", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.my_conditioning_prescription", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.my_exercise", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.my_exercise_role_event", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.training_observation_event", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.training_session", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.training_session_current_v", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.training_session_role_event", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.training_set_effective_role_v1", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.training_set_log", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.training_set_log_segment", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.workout_template", "row.owner_user_id::text={owner}"),
    OwnerSurface(
        "lifeswitch_training.workout_template_exercise",
        "exists (select 1 from lifeswitch_training.workout_template p "
        "where p.workout_template_id=row.workout_template_id "
        "and p.owner_user_id::text={owner})",
    ),
    OwnerSurface(
        "lifeswitch_training.workout_template_exercise_segment",
        "exists (select 1 from lifeswitch_training.workout_template_exercise e "
        "join lifeswitch_training.workout_template p "
        "on p.workout_template_id=e.workout_template_id "
        "where e.workout_template_exercise_id=row.workout_template_exercise_id "
        "and p.owner_user_id::text={owner})",
    ),
    OwnerSurface("lifeswitch_training.workout_template_role_event", "row.owner_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.workout_template_share", "row.created_by_user_id::text={owner}"),
    OwnerSurface("lifeswitch_training.conditioning_session_current_v", "row.owner_user_id::text={owner}"),
    OwnerSurface("public.lifeswitch_measurement_entries", "row.owner_user_id={owner}"),
)


OWNER_SQL = """
select distinct owner_id from (
  select owner_user_id::text owner_id from lifeswitch_chat.account_timezone_v1
  union select owner_user_id::text from lifeswitch_nutrition.meal
  union select owner_user_id::text from lifeswitch_nutrition.meal_plan
  union select owner_user_id::text from lifeswitch_nutrition.my_food
  union select owner_user_id::text from lifeswitch_nutrition.nutrition_day
  union select owner_user_id::text from lifeswitch_plan.plan_profile
  union select owner_user_id::text from lifeswitch_training.training_session
  union select owner_user_id::text from lifeswitch_training.workout_template
  union select owner_user_id::text from public.lifeswitch_measurement_entries
) owners where owner_id is not null and owner_id <> '' order by owner_id
"""


SCHEMAS_SQL = """
select coalesce(jsonb_agg(jsonb_build_object(
  'name',nspname,'owner',pg_get_userbyid(nspowner),
  'acl',coalesce((select jsonb_agg(item::text order by item::text)
                  from unnest(coalesce(nspacl,acldefault('n',nspowner))) item),
                 '[]'::jsonb)
) order by nspname),'[]'::jsonb)::text
from pg_namespace
where nspname not like 'pg\\_%' escape '\\'
  and nspname <> 'information_schema'
"""


RELATIONS_SQL = """
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',n.nspname,'name',c.relname,'kind',c.relkind,
  'owner',pg_get_userbyid(c.relowner),
  'acl',coalesce((select jsonb_agg(item::text order by item::text)
                  from unnest(coalesce(
                    c.relacl,acldefault(
                      (case when c.relkind='S' then 'S' else 'r' end)::"char",
                      c.relowner
                    )
                  )) item),'[]'::jsonb),
  'rls_enabled',c.relrowsecurity,'rls_forced',c.relforcerowsecurity,
  'definition',case when c.relkind in ('v','m') then pg_get_viewdef(c.oid,true) else '' end
) order by n.nspname,c.relname),'[]'::jsonb)::text
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where n.nspname not like 'pg\\_%' escape '\\'
  and n.nspname <> 'information_schema'
  and c.relkind in ('r','p','v','m','S')
"""


FUNCTIONS_SQL = """
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',n.nspname,'name',p.proname,
  'kind',p.prokind,
  'identity_arguments',pg_get_function_identity_arguments(p.oid),
  'result',pg_get_function_result(p.oid),'language',l.lanname,
  'owner',pg_get_userbyid(p.proowner),
  'acl',coalesce((select jsonb_agg(item::text order by item::text)
                  from unnest(coalesce(p.proacl,acldefault('f',p.proowner))) item),
                 '[]'::jsonb),
  'security_definer',p.prosecdef,'leakproof',p.proleakproof,
  'strict',p.proisstrict,'volatility',p.provolatile,'parallel',p.proparallel,
  'configuration',coalesce(p.proconfig::text,''),
  'source',p.prosrc,'binary',coalesce(p.probin,'')
) order by n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)),
  '[]'::jsonb)::text
from pg_proc p
join pg_namespace n on n.oid=p.pronamespace
join pg_language l on l.oid=p.prolang
where n.nspname not like 'pg\\_%' escape '\\'
  and n.nspname <> 'information_schema'
"""


POLICIES_SQL = """
select coalesce(jsonb_agg(jsonb_build_object(
  'schema',schemaname,'table',tablename,'name',policyname,
  'permissive',permissive,'roles',roles,'command',cmd,
  'using',coalesce(qual,''),'check',coalesce(with_check,'')
) order by schemaname,tablename,policyname),'[]'::jsonb)::text
from pg_policies
where schemaname not like 'pg\\_%' escape '\\'
  and schemaname <> 'information_schema'
"""


EXTENSIONS_SQL = """
select coalesce(jsonb_agg(jsonb_build_object(
  'name',e.extname,'version',e.extversion,'schema',n.nspname
) order by e.extname),'[]'::jsonb)::text
from pg_extension e join pg_namespace n on n.oid=e.extnamespace
"""


ROLES_SQL = """
select coalesce(jsonb_agg(jsonb_build_object(
  'name',rolname,'superuser',rolsuper,'inherit',rolinherit,
  'create_role',rolcreaterole,'create_db',rolcreatedb,'can_login',rolcanlogin,
  'replication',rolreplication,'bypass_rls',rolbypassrls,
  'configuration',coalesce(rolconfig::text,'')
) order by rolname),'[]'::jsonb)::text
from pg_roles where rolname like 'lifeswitch\\_%' escape '\\'
"""


MEMBERSHIPS_SQL = """
select coalesce(jsonb_agg(jsonb_build_object(
  'member',member_role.rolname,'granted',granted_role.rolname,
  'admin_option',m.admin_option
) order by member_role.rolname,granted_role.rolname),'[]'::jsonb)::text
from pg_auth_members m
join pg_roles member_role on member_role.oid=m.member
join pg_roles granted_role on granted_role.oid=m.roleid
where member_role.rolname like 'lifeswitch\\_%' escape '\\'
   or granted_role.rolname like 'lifeswitch\\_%' escape '\\'
"""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise VerificationContractError("identifier_invalid")
    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    if "\x00" in value:
        raise VerificationContractError("literal_invalid")
    return "'" + value.replace("'", "''") + "'"


def validate_run_id(value: str) -> str:
    if RUN_ID.fullmatch(value) is None:
        raise VerificationContractError("run_id_invalid")
    return value


def target_database(run_id: str) -> str:
    value = "lifeswitch_verify_" + validate_run_id(run_id).replace("-", "_")
    if SAFE_DATABASE.fullmatch(value) is None:
        raise VerificationContractError("target_database_invalid")
    return value


def docker_command(*arguments: str, interactive: bool = False) -> list[str]:
    command = [DOCKER, "exec"]
    if interactive:
        command.append("-i")
    command.extend([CONTAINER, *arguments])
    return command


def run_text(command: list[str], *, label: str, timeout: int = 120) -> str:
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
        raise VerificationExecutionError(f"{label}_execution_failed") from error
    if completed.returncode != 0:
        raise VerificationExecutionError(f"{label}_failed")
    return completed.stdout.strip()


def run_dump(path: Path) -> None:
    command = docker_command(
        "pg_dump", "-U", ADMIN_ROLE, "-d", SOURCE_DATABASE,
        "--format=custom", "--compress=6", "--no-password",
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
                timeout=600,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
            output.flush()
            os.fsync(output.fileno())
    except (OSError, subprocess.SubprocessError) as error:
        path.unlink(missing_ok=True)
        raise VerificationExecutionError("pg_dump_execution_failed") from error
    if completed.returncode != 0 or not path.is_file() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise VerificationExecutionError("pg_dump_failed")


def run_restore(database: str, archive: Path) -> None:
    command = docker_command(
        "pg_restore", "-U", ADMIN_ROLE, "-d", database,
        "--exit-on-error", "--single-transaction", "--no-password",
        interactive=True,
    )
    try:
        with archive.open("rb") as source:
            completed = subprocess.run(
                command,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=600,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise VerificationExecutionError("pg_restore_execution_failed") from error
    if completed.returncode != 0:
        raise VerificationExecutionError("pg_restore_failed")


def psql(database: str, sql: str, *, label: str) -> str:
    return run_text(
        docker_command(
            "psql", "-X", "--no-psqlrc", "-qAt",
            "-v", "ON_ERROR_STOP=1", "-U", ADMIN_ROLE, "-d", database,
            "-c", sql,
        ),
        label=label,
    )


def read_json_query(database: str, sql: str, label: str) -> list[dict[str, Any]]:
    raw = psql(database, sql, label=label)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise VerificationExecutionError(f"{label}_json_invalid") from error
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise VerificationExecutionError(f"{label}_shape_invalid")
    return value


def relation_sql(name: str) -> str:
    pieces = name.split(".")
    if len(pieces) != 2:
        raise VerificationContractError("relation_name_invalid")
    return ".".join(quote_identifier(piece) for piece in pieces)


def collect_manifest(database: str) -> dict[str, Any]:
    schemas = read_json_query(database, SCHEMAS_SQL, "schemas")
    relations = read_json_query(database, RELATIONS_SQL, "relations")
    functions = read_json_query(database, FUNCTIONS_SQL, "functions")
    policies = read_json_query(database, POLICIES_SQL, "policies")
    extensions = read_json_query(database, EXTENSIONS_SQL, "extensions")
    table_counts: dict[str, int] = {}
    for relation in relations:
        if relation.get("kind") not in {"r", "p"}:
            continue
        name = f"{relation['schema']}.{relation['name']}"
        raw = psql(
            database,
            f"select count(*) from {relation_sql(name)}",
            label="table_count",
        )
        try:
            table_counts[name] = int(raw)
        except ValueError as error:
            raise VerificationExecutionError("table_count_invalid") from error
    document = {
        "schema_version": MANIFEST_VERSION,
        "schemas": schemas,
        "relations": relations,
        "functions": functions,
        "policies": policies,
        "extensions": extensions,
        "table_counts": table_counts,
    }
    return {**document, "manifest_sha256": sha256_bytes(canonical_bytes(document))}


def manifest_difference(source: dict[str, Any], restored: dict[str, Any]) -> dict[str, Any]:
    identity_fields = {
        "schemas": ("name",),
        "relations": ("schema", "name", "kind"),
        "functions": ("schema", "name", "identity_arguments", "kind"),
        "policies": ("schema", "table", "name"),
        "extensions": ("name",),
    }
    result: dict[str, Any] = {}
    for section, fields in identity_fields.items():
        source_items = {
            tuple(str(item.get(field, "")) for field in fields): item
            for item in source[section]
        }
        restored_items = {
            tuple(str(item.get(field, "")) for field in fields): item
            for item in restored[section]
        }
        changed = []
        for identity in sorted(set(source_items) | set(restored_items)):
            left = source_items.get(identity)
            right = restored_items.get(identity)
            if left == right:
                continue
            changed.append(
                {
                    "identity": list(identity),
                    "changed_fields": sorted(
                        key
                        for key in set(left or {}) | set(right or {})
                        if (left or {}).get(key) != (right or {}).get(key)
                    ),
                    "source_sha256": (
                        sha256_bytes(canonical_bytes(left)) if left is not None else None
                    ),
                    "restored_sha256": (
                        sha256_bytes(canonical_bytes(right)) if right is not None else None
                    ),
                }
            )
        if changed:
            result[section] = changed
    count_changes = {
        name: {
            "source": source["table_counts"].get(name),
            "restored": restored["table_counts"].get(name),
        }
        for name in sorted(set(source["table_counts"]) | set(restored["table_counts"]))
        if source["table_counts"].get(name) != restored["table_counts"].get(name)
    }
    if count_changes:
        result["table_counts"] = count_changes
    return result


def read_owners(database: str) -> list[str]:
    values = [line for line in psql(database, OWNER_SQL, label="owners").splitlines() if line]
    try:
        normalized = sorted({str(UUID(value)) for value in values})
    except ValueError as error:
        raise VerificationExecutionError("owner_identifier_invalid") from error
    if not normalized:
        raise VerificationExecutionError("owner_set_empty")
    return normalized


def app_visible_count(database: str, owner: str, relation: str) -> int:
    value = quote_literal(owner)
    sql = (
        "begin; set local role " + quote_identifier(APP_ROLE) + "; "
        "set local app.user_id=" + value + "; "
        "set local app.lifeswitch_owner_id=" + value + "; "
        "select count(*) from " + relation_sql(relation) + "; rollback;"
    )
    raw = psql(database, sql, label="app_visible_count")
    try:
        return int(raw)
    except ValueError as error:
        raise VerificationExecutionError("app_visible_count_invalid") from error


def verify_owner_rls(database: str) -> dict[str, Any]:
    owners = read_owners(database)
    comparisons = 0
    for owner in owners:
        owner_literal = quote_literal(owner)
        for surface in SURFACES:
            expected_sql = surface.owner_filter.format(owner=owner_literal)
            expected = int(
                psql(
                    database,
                    f"select count(*) from {relation_sql(surface.relation)} row "
                    f"where {expected_sql}",
                    label="admin_owner_count_" + surface.relation.replace(".", "_"),
                )
            )
            actual = app_visible_count(database, owner, surface.relation)
            if actual != expected:
                raise VerificationExecutionError("owner_visible_count_mismatch")
            comparisons += 1

    other = owners[1] if len(owners) > 1 else "00000000-0000-4000-8000-000000000099"
    actor = owners[0]
    actor_literal = quote_literal(actor)
    other_literal = quote_literal(other)
    identifiers = [str(uuid5(NAMESPACE_URL, f"{database}:{index}")) for index in range(3)]
    writes = (
        "insert into lifeswitch_nutrition.meal "
        "(meal_id,owner_user_id,name,meal_type) values "
        f"({quote_literal(identifiers[0])}::uuid,{other_literal}::uuid,'rls denial','other')",
        "insert into lifeswitch_training.workout_template "
        "(workout_template_id,owner_user_id,name,notes,workout_role) values "
        f"({quote_literal(identifiers[1])}::uuid,{other_literal}::uuid,'rls denial','rollback','strength')",
        "insert into public.lifeswitch_measurement_entries "
        "(measurement_entry_id,owner_user_id,local_date,weight_value,weight_unit,"
        "measurement_unit,source,entry_kind) values "
        f"({quote_literal(identifiers[2])}::uuid,{other_literal},date '2099-12-30',"
        "1,'lb','in','rls_denial','verification')",
    )
    for index, statement in enumerate(writes):
        sql = (
            "begin; set local role " + quote_identifier(APP_ROLE) + "; "
            "set local app.user_id=" + actor_literal + "; "
            "set local app.lifeswitch_owner_id=" + actor_literal + "; "
            "do $verify$ begin begin " + statement + "; "
            "exception when insufficient_privilege then return; end; "
            "raise exception 'cross_owner_write_unexpectedly_succeeded'; "
            "end $verify$; rollback;"
        )
        psql(database, sql, label=f"cross_owner_write_{index}")

    snapshot_sql = (
        "begin; set local role " + quote_identifier(APP_ROLE) + "; "
        "do $verify$ begin begin perform count(*) from "
        "lifeswitch_snapshot.analysis_source_row; "
        "exception when insufficient_privilege then return; end; "
        "raise exception 'snapshot_access_unexpectedly_succeeded'; "
        "end $verify$; rollback;"
    )
    psql(database, snapshot_sql, label="snapshot_denial")
    int(
        psql(
            database,
            "begin; set local role " + quote_identifier(APP_ROLE)
            + "; select count(*) from catalog_dev.food; rollback;",
            label="catalog_read",
        )
    )
    return {
        "owner_count": len(owners),
        "owner_set_sha256": sha256_bytes(canonical_bytes(owners)),
        "surface_count": len(SURFACES),
        "owner_surface_comparisons": comparisons,
        "cross_owner_writes_denied": len(writes),
        "snapshot_access_denied": True,
        "catalog_read_succeeded": True,
    }


def role_manifest(database: str) -> dict[str, Any]:
    roles = read_json_query(database, ROLES_SQL, "roles")
    memberships = read_json_query(database, MEMBERSHIPS_SQL, "memberships")
    app = next((item for item in roles if item.get("name") == APP_ROLE), None)
    if app is None or app.get("superuser") or app.get("bypass_rls") or not app.get("can_login"):
        raise VerificationExecutionError("application_role_boundary_invalid")
    if not any(
        item.get("member") == APP_ROLE and item.get("granted") == "lifeswitch_app"
        for item in memberships
    ):
        raise VerificationExecutionError("application_role_membership_missing")
    document = {"roles": roles, "memberships": memberships}
    return {
        "manifest_sha256": sha256_bytes(canonical_bytes(document)),
        "role_count": len(roles),
        "membership_count": len(memberships),
        "application_login": APP_ROLE,
        "application_superuser": False,
        "application_bypass_rls": False,
    }


def read_nonsecret_config() -> dict[str, bool]:
    values: dict[str, str] = {}
    for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name in {"LIFESWITCH_DELEGATED_READS_ENABLED", "LIFESWITCH_PEOPLE_ENABLED"}:
            values[name] = value.strip().strip('"').strip("'")
    if values.get("LIFESWITCH_DELEGATED_READS_ENABLED", "0") != "0":
        raise VerificationExecutionError("delegated_reads_not_fail_closed")
    if values.get("LIFESWITCH_PEOPLE_ENABLED", "0") != "0":
        raise VerificationExecutionError("people_dependency_not_disabled")
    return {"delegated_reads_enabled": False, "people_dependency_enabled": False}


def atomic_write(path: Path, value: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def prepare_output(run_id: str) -> Path:
    OUTPUT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if OUTPUT_ROOT.is_symlink() or not OUTPUT_ROOT.is_dir():
        raise VerificationContractError("output_root_invalid")
    os.chmod(OUTPUT_ROOT, 0o700)
    output = OUTPUT_ROOT / run_id
    output.mkdir(mode=0o700)
    if output.is_symlink() or stat.S_IMODE(output.stat().st_mode) != 0o700:
        raise VerificationContractError("output_directory_invalid")
    return output


def database_exists(database: str) -> bool:
    raw = psql(
        SOURCE_DATABASE,
        "select count(*) from pg_database where datname=" + quote_literal(database),
        label="database_exists",
    )
    return int(raw) == 1


def drop_database(database: str, *, label: str) -> None:
    run_text(
        docker_command(
            "dropdb", "-U", ADMIN_ROLE, "--maintenance-db", SOURCE_DATABASE,
            "--if-exists", database,
        ),
        label=label,
    )


def execute(run_id: str, candidate_commit: str) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise VerificationContractError("root_required")
    run_id = validate_run_id(run_id)
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_commit):
        raise VerificationContractError("candidate_commit_invalid")
    target = target_database(run_id)
    if run_text(
        [DOCKER, "inspect", "--format", "{{.State.Running}}", CONTAINER],
        label="container_state",
    ) != "true":
        raise VerificationExecutionError("container_not_running")
    if database_exists(target):
        raise VerificationContractError("target_database_already_exists")
    output = prepare_output(run_id)
    archive = output / "lifeswitch.pgcustom"
    source_manifest_path = output / "source-manifest.json"
    receipt_path = output / "verification-receipt.json"
    target_created = False
    try:
        run_dump(archive)
        source_manifest = collect_manifest(SOURCE_DATABASE)
        atomic_write(source_manifest_path, canonical_bytes(source_manifest) + b"\n")
        run_text(
            docker_command(
                "createdb", "-U", ADMIN_ROLE, "--maintenance-db", SOURCE_DATABASE,
                "--template", "template0", "--encoding", "UTF8", target,
            ),
            label="createdb",
        )
        target_created = True
        run_restore(target, archive)
        restored_manifest = collect_manifest(target)
        if restored_manifest != source_manifest:
            difference = manifest_difference(source_manifest, restored_manifest)
            atomic_write(
                output / "manifest-difference.json",
                canonical_bytes(difference) + b"\n",
            )
            raise VerificationExecutionError("restored_manifest_mismatch")
        roles = role_manifest(target)
        owner_rls = verify_owner_rls(target)
        configuration = read_nonsecret_config()
        drop_database(target, label="dropdb")
        target_created = False
        if database_exists(target):
            raise VerificationExecutionError("target_database_cleanup_unproved")
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "pass",
            "run_id": run_id,
            "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "candidate_commit": candidate_commit,
            "source": {
                "container": CONTAINER,
                "database": SOURCE_DATABASE,
                "manifest_sha256": source_manifest["manifest_sha256"],
                "table_count": len(source_manifest["table_counts"]),
                "exact_row_count": sum(source_manifest["table_counts"].values()),
            },
            "backup": {
                "relative_path": archive.name,
                "format": "pg_dump custom",
                "bytes": archive.stat().st_size,
                "sha256": sha256_file(archive),
                "mode": oct(stat.S_IMODE(archive.stat().st_mode)),
            },
            "restore": {
                "database_name_sha256": sha256_bytes(target.encode()),
                "manifest_sha256": restored_manifest["manifest_sha256"],
                "exact_manifest_match": True,
            },
            "roles": roles,
            "owner_rls": owner_rls,
            "configuration": configuration,
            "cleanup": {"target_database_absent": True},
            "production_changes": {
                "source_database_writes": 0,
                "service_restarts": 0,
                "source_code_changes": 0,
            },
        }
        receipt_bytes = canonical_bytes(receipt) + b"\n"
        atomic_write(receipt_path, receipt_bytes)
        return {
            "status": "pass",
            "receipt": str(receipt_path),
            "receipt_sha256": sha256_bytes(receipt_bytes),
            "backup_sha256": receipt["backup"]["sha256"],
            "manifest_sha256": source_manifest["manifest_sha256"],
            "owner_count": owner_rls["owner_count"],
            "comparisons": owner_rls["owner_surface_comparisons"],
            "cleanup": "proved",
        }
    finally:
        if target_created:
            drop_database(target, label="failure_cleanup_dropdb")
            if database_exists(target):
                raise VerificationExecutionError("failure_cleanup_unproved")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = execute(arguments.run_id, arguments.candidate_commit)
    except (VerificationContractError, VerificationExecutionError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
