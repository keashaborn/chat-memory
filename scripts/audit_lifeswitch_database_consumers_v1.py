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


def rendered_sql(template: str) -> str:
    schemas = ",".join(sql_literal(schema) for schema in SCOPED_SCHEMAS)
    return template.format(schemas=schemas)


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


def psql_json(sql: str, *, label: str) -> list[dict[str, Any]]:
    wrapped = "begin isolation level repeatable read read only;" + sql + ";rollback;"
    raw = run_text(
        [
            DOCKER,
            "exec",
            CONTAINER,
            "psql",
            "-X",
            "--no-psqlrc",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            ADMIN_ROLE,
            "-d",
            DATABASE,
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


def current_source_manifest_sha256(verifier: Path | None = None) -> str:
    verifier = verifier or Path(__file__).with_name("verify_lifeswitch_disposable_restore_v1.py")
    if verifier.is_symlink() or not verifier.is_file():
        raise AuditContractError("source_manifest_verifier_missing")
    namespace = runpy.run_path(str(verifier))
    collector = namespace.get("collect_manifest")
    if not callable(collector):
        raise AuditContractError("source_manifest_collector_missing")
    manifest = collector(DATABASE)
    if not isinstance(manifest, dict):
        raise AuditExecutionError("source_manifest_shape_invalid")
    value = str(manifest.get("manifest_sha256") or "")
    return validate_manifest_sha256(value)


def parse_objects(rows: Iterable[dict[str, Any]]) -> list[DatabaseObject]:
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
        if item.schema not in SCOPED_SCHEMAS or not item.name or "\x00" in item.name:
            raise AuditExecutionError("database_object_identity_invalid")
        oid_key = (item.object_type, item.oid)
        if item.identity in seen_identity or oid_key in seen_oid:
            raise AuditExecutionError("database_object_duplicate")
        seen_identity.add(item.identity)
        seen_oid.add(oid_key)
        objects.append(item)
    return sorted(objects, key=lambda item: item.identity)


def _regular_source_files(repository: Path, roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for relative_root in roots:
        root = repository / relative_root
        candidates = [root] if root.is_file() else root.rglob("*") if root.is_dir() else []
        for path in candidates:
            if path.suffix not in SOURCE_SUFFIXES or "__pycache__" in path.parts:
                continue
            if path.is_symlink() or not path.is_file():
                raise AuditContractError("source_path_not_regular")
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise AuditContractError("source_file_too_large")
            files.append(path)
    return sorted(set(files))


def scan_sources(repository: Path, roots: Iterable[Path], objects: list[DatabaseObject]) -> dict[str, Any]:
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
    for path in _regular_source_files(repository, roots):
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


def prepare_output(run_id: str) -> Path:
    OUTPUT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if OUTPUT_ROOT.is_symlink() or not OUTPUT_ROOT.is_dir():
        raise AuditContractError("output_root_invalid")
    os.chmod(OUTPUT_ROOT, 0o700)
    output = OUTPUT_ROOT / validate_run_id(run_id)
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
    source_manifest_sha256: str,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise AuditContractError("root_required")
    repository = repository.resolve(strict=True)
    if not (repository / ".git").exists() or repository.is_symlink():
        raise AuditContractError("repository_invalid")
    candidate_commit = validate_commit(candidate_commit)
    source_manifest_sha256 = validate_manifest_sha256(source_manifest_sha256)
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
    if run_text(
        [DOCKER, "inspect", "--format", "{{.State.Running}}", CONTAINER],
        label="container_state",
    ) != "true":
        raise AuditExecutionError("container_not_running")
    if current_source_manifest_sha256() != source_manifest_sha256:
        raise AuditContractError("source_manifest_sha256_mismatch")

    objects = parse_objects(psql_json(rendered_sql(OBJECTS_SQL), label="objects"))
    definitions = psql_json(rendered_sql(DEFINITIONS_SQL), label="definitions")
    database_edges = definition_edges(objects, definitions)
    database_edges.extend(
        explicit_edges(objects, psql_json(rendered_sql(EXPLICIT_EDGES_SQL), label="edges"))
    )
    database_edges = [dict(item) for item in {tuple(sorted(edge.items())) for edge in database_edges}]
    database_edges.sort(key=lambda edge: (edge["consumer"], edge["referenced"], edge["evidence"]))

    runtime = scan_sources(repository, RUNTIME_ROOTS, objects)
    operational = scan_sources(repository, OPERATIONAL_ROOTS, objects)
    migration = scan_sources(repository, MIGRATION_ROOTS, objects)
    classified = classify_objects(
        objects,
        runtime["matches"],
        operational["matches"],
        migration["matches"],
        database_edges,
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
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "candidate_commit": candidate_commit,
        "source_manifest_sha256": source_manifest_sha256,
        "database": {"container": CONTAINER, "name": DATABASE, "transaction_read_only": True},
        "scope": {
            "schemas": list(SCOPED_SCHEMAS),
            "object_types": ["relation", "function"],
            "unqualified_source_references_are_evidence": False,
            "schema_bound_template_references_are_evidence": True,
            "overload_resolution": "conservative_all_matching_overloads",
            "operational_references_are_retention_seeds": False,
            "deletion_authority": False,
        },
        "source_trees": {
            "runtime": {key: value for key, value in runtime.items() if key != "matches"},
            "operational": {key: value for key, value in operational.items() if key != "matches"},
            "migration": {key: value for key, value in migration.items() if key != "matches"},
        },
        "object_catalog_sha256": sha256_bytes(canonical_bytes(object_catalog)),
        "object_count": len(objects),
        "dependency_edge_count": len(database_edges),
        "classification_counts": dict(sorted(counts.items())),
        "objects": classified,
    }
    output = prepare_output(run_id)
    report = output / "consumer-audit.json"
    report_bytes = canonical_bytes(document) + b"\n"
    atomic_write(report, report_bytes)
    return {
        "status": "pass",
        "report": str(report),
        "report_sha256": sha256_bytes(report_bytes),
        "object_catalog_sha256": document["object_catalog_sha256"],
        "object_count": len(objects),
        "dependency_edge_count": len(database_edges),
        "classification_counts": document["classification_counts"],
        "deletion_authority": False,
    }


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
