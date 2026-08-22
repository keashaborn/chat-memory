#!/usr/bin/env python3
from __future__ import annotations

"""Install and verify a clean SeeBx schema in a disposable rootless Podman DB."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "seebx-platform-clean-install-verification-v1"
BASELINE_SCHEMA_VERSION = "seebx-platform-clean-baseline-receipt-v1"
POSTGRES_IMAGE = (
    "docker.io/library/postgres@"
    "sha256:e17e86066e5ef83e0952a9347f5c792b7ece00972e2aa787a6986f471b3dd3d5"
)
POSTGRES_IMAGE_DIGEST = (
    "sha256:e17e86066e5ef83e0952a9347f5c792b7ece00972e2aa787a6986f471b3dd3d5"
)
PODMAN = "/usr/bin/podman"
DATABASE = "platform_clean"
ADMIN_ROLE = "postgres"
SERVICE_USER = "999:999"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,39}$")
IDENTITY = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*(?:\(.*\))?$")
ARTIFACT_NAMES = {
    "platform-clean-index-map-v1.json",
    "platform-clean-install-v1.sql",
    "platform-clean-namespaces-v1.sql",
    "platform-clean-owners-v1.sql",
    "platform-clean-privileges-v1.sql",
    "platform-clean-restore-v1.list",
    "platform-clean-roles-v1.sql",
    "platform-clean-schema-v1.sql",
    "platform-clean-source-schema-v1.sql",
}
FORBIDDEN_SCHEMAS = {
    "catalog_dev",
    "chat_history_private",
    "chat_integrity",
    "lifeswitch_usage",
    "memory",
    "memory_ingest_private",
}
FORBIDDEN_ROLES = {
    "brains_app",
    "lifeswitch_usage_admin_v1",
    "lifeswitch_usage_writer_v1",
    "sage",
}
FORBIDDEN_ROLE_PREFIXES = (
    "governed_memory_",
    "lifeswitch_chat_",
    "memory_",
)
PODMAN_DEFAULT_CAPABILITIES = {
    "chown",
    "dac_override",
    "fowner",
    "fsetid",
    "kill",
    "net_bind_service",
    "setfcap",
    "setgid",
    "setpcap",
    "setuid",
    "sys_chroot",
}


class InstallContractError(RuntimeError):
    pass


class InstallExecutionError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secure_directory(path: Path, *, label: str) -> Path:
    try:
        item = path.lstat()
    except OSError as error:
        raise InstallContractError(label + "_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        raise InstallContractError(label + "_type_invalid")
    if item.st_uid != os.geteuid() or stat.S_IMODE(item.st_mode) & 0o077:
        raise InstallContractError(label + "_permissions_invalid")
    return path.resolve(strict=True)


def _secure_file(path: Path, *, label: str) -> Path:
    try:
        item = path.lstat()
    except OSError as error:
        raise InstallContractError(label + "_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        raise InstallContractError(label + "_type_invalid")
    if item.st_uid != os.geteuid() or stat.S_IMODE(item.st_mode) & 0o077:
        raise InstallContractError(label + "_permissions_invalid")
    return path.resolve(strict=True)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallContractError(label + "_json_invalid") from error
    if not isinstance(value, dict):
        raise InstallContractError(label + "_shape_invalid")
    return value


def _target_plan(receipt: Mapping[str, Any]) -> dict[str, Any]:
    plan = receipt.get("object_plan")
    if not isinstance(plan, dict):
        raise InstallContractError("baseline_object_plan_invalid")
    targets = plan.get("targets")
    roles = plan.get("target_roles")
    schemas = plan.get("target_schemas")
    memberships = plan.get("target_memberships")
    extensions = plan.get("extensions")
    if (
        not isinstance(targets, list)
        or len(targets) != 38
        or not isinstance(roles, list)
        or len(roles) != 6
        or not isinstance(schemas, list)
        or len(schemas) != 9
        or not isinstance(memberships, list)
        or memberships
        != [{"granted": "seebx_usage_writer_v1", "member": "seebx_platform_app_v1"}]
        or extensions != {"pgcrypto": "1.3", "plpgsql": "1.0"}
    ):
        raise InstallContractError("baseline_object_plan_contract_invalid")
    identities: set[str] = set()
    for item in targets:
        if not isinstance(item, dict):
            raise InstallContractError("baseline_target_invalid")
        identity = str(item.get("target_identity") or "")
        if (
            IDENTITY.fullmatch(identity) is None
            or item.get("object_type") not in {"relation", "function"}
            or not isinstance(item.get("target_owner"), str)
            or identity in identities
        ):
            raise InstallContractError("baseline_target_invalid")
        identities.add(identity)
    return dict(plan)


def verify_baseline(
    baseline: Path,
    receipt_sha256: str,
    candidate_commit: str,
) -> tuple[dict[str, Any], dict[str, Path]]:
    if SHA256.fullmatch(receipt_sha256) is None:
        raise InstallContractError("baseline_receipt_sha256_invalid")
    if COMMIT.fullmatch(candidate_commit) is None:
        raise InstallContractError("candidate_commit_invalid")
    baseline = _secure_directory(baseline, label="baseline_directory")
    receipt_path = _secure_file(
        baseline / "platform-clean-baseline-receipt-v1.json",
        label="baseline_receipt",
    )
    if sha256_file(receipt_path) != receipt_sha256:
        raise InstallContractError("baseline_receipt_sha256_mismatch")
    receipt = _read_json(receipt_path, label="baseline_receipt")
    if (
        receipt.get("schema_version") != BASELINE_SCHEMA_VERSION
        or receipt.get("status") != "candidate_schema_ready"
        or receipt.get("candidate_commit") != candidate_commit
        or receipt.get("schema_only") is not True
        or receipt.get("data_copy_authority") is not False
        or receipt.get("deletion_authority") is not False
        or receipt.get("installation_authority") is not False
        or receipt.get("production_change_authority") is not False
    ):
        raise InstallContractError("baseline_receipt_contract_invalid")
    _target_plan(receipt)
    raw_artifacts = receipt.get("artifacts")
    if not isinstance(raw_artifacts, dict) or set(raw_artifacts) != ARTIFACT_NAMES:
        raise InstallContractError("baseline_artifacts_invalid")
    artifacts: dict[str, Path] = {}
    for name, raw in raw_artifacts.items():
        if not isinstance(raw, dict) or set(raw) != {"bytes", "mode", "sha256"}:
            raise InstallContractError("baseline_artifact_record_invalid")
        path = _secure_file(baseline / name, label="baseline_artifact")
        if (
            path.parent != baseline
            or raw.get("mode") != "0o600"
            or raw.get("bytes") != path.stat().st_size
            or raw.get("sha256") != sha256_file(path)
        ):
            raise InstallContractError("baseline_artifact_binding_invalid")
        artifacts[name] = path
    return receipt, artifacts


def _environment() -> dict[str, str]:
    return {
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", ""),
    }


def run_checked(
    command: list[str],
    *,
    label: str,
    timeout: int = 300,
) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallExecutionError(label + "_execution_failed") from error
    if completed.returncode != 0:
        raise InstallExecutionError(label + "_failed")
    return completed.stdout


def _run_status(command: list[str], *, timeout: int = 60) -> int:
    try:
        return subprocess.run(
            command,
            check=False,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).returncode
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallExecutionError("runtime_status_execution_failed") from error


def sandbox_names(run_id: str) -> dict[str, str]:
    if RUN_ID.fullmatch(run_id) is None:
        raise InstallContractError("run_id_invalid")
    suffix = run_id.replace("-", "_")
    return {
        "container": "lwr-platform-clean-" + run_id,
        "data_volume": "lwr_platform_clean_data_" + suffix,
        "socket_volume": "lwr_platform_clean_socket_" + suffix,
    }


def build_create_command(
    *,
    names: Mapping[str, str],
    baseline: Path,
    image: str = POSTGRES_IMAGE,
) -> list[str]:
    return [
        PODMAN,
        "create",
        "--name=" + names["container"],
        "--label=lifeswitch.work-runner.boundary=platform-clean-install-v1",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=4g",
        "--cpus=2",
        "--user=" + SERVICE_USER,
        "--env=POSTGRES_HOST_AUTH_METHOD=trust",
        "--env=POSTGRES_DB=" + DATABASE,
        "--env=PGDATA=/var/lib/postgresql/data/pgdata",
        "--volume="
        + names["data_volume"]
        + ":/var/lib/postgresql/data:rw,U",
        "--volume="
        + names["socket_volume"]
        + ":/var/run/postgresql:rw,U",
        "--volume=" + str(baseline) + ":/baseline:ro,rprivate",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
        image,
        "postgres",
        "-c",
        "listen_addresses=",
        "-c",
        "unix_socket_directories=/var/run/postgresql",
        "-c",
        "max_connections=20",
    ]


def _podman_exec(
    container: str,
    arguments: list[str],
    *,
    label: str,
    user: str = SERVICE_USER,
) -> str:
    return run_checked(
        [
            PODMAN,
            "exec",
            "--user=" + user,
            "--env=PGHOST=/var/run/postgresql",
            "--env=PGUSER=" + ADMIN_ROLE,
            "--env=PGDATABASE=" + DATABASE,
            container,
            *arguments,
        ],
        label=label,
    )


def query_json(container: str, sql: str, *, label: str) -> Any:
    output = _podman_exec(
        container,
        [
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
            "--command=" + sql,
        ],
        label=label,
    ).strip()
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise InstallExecutionError(label + "_json_invalid") from error


def _quoted_sql_array(values: list[str]) -> str:
    if any(re.fullmatch(r"[a-z_][a-z0-9_]*", item) is None for item in values):
        raise InstallContractError("catalog_identifier_invalid")
    return "ARRAY[" + ",".join("'" + item + "'" for item in values) + "]::text[]"


def collect_catalog(container: str, plan: Mapping[str, Any], schema_sql: str) -> dict[str, Any]:
    schemas = sorted(str(item["name"]) for item in plan["target_schemas"])
    roles = sorted(str(item["name"]) for item in plan["target_roles"])
    schema_array = _quoted_sql_array(schemas)
    role_array = _quoted_sql_array(roles)
    catalog = {
        "schemas": query_json(
            container,
            "SELECT COALESCE(jsonb_agg(jsonb_build_object('name',nspname,'owner',pg_get_userbyid(nspowner)) ORDER BY nspname),'[]'::jsonb)::text FROM pg_namespace WHERE nspname=ANY("
            + schema_array
            + ");",
            label="catalog_schemas",
        ),
        "roles": query_json(
            container,
            "SELECT COALESCE(jsonb_agg(jsonb_build_object('name',rolname,'can_login',rolcanlogin,'superuser',rolsuper,'create_db',rolcreatedb,'create_role',rolcreaterole,'replication',rolreplication,'bypass_rls',rolbypassrls) ORDER BY rolname),'[]'::jsonb)::text FROM pg_roles WHERE rolname=ANY("
            + role_array
            + ");",
            label="catalog_roles",
        ),
        "memberships": query_json(
            container,
            "SELECT COALESCE(jsonb_agg(jsonb_build_object('member',member.rolname,'granted',granted.rolname) ORDER BY member.rolname,granted.rolname),'[]'::jsonb)::text FROM pg_auth_members m JOIN pg_roles granted ON granted.oid=m.roleid JOIN pg_roles member ON member.oid=m.member WHERE member.rolname=ANY("
            + role_array
            + ") OR granted.rolname=ANY("
            + role_array
            + ");",
            label="catalog_memberships",
        ),
        "relations": query_json(
            container,
            "SELECT COALESCE(jsonb_agg(jsonb_build_object('identity',n.nspname||'.'||c.relname,'owner',pg_get_userbyid(c.relowner),'kind',c.relkind,'rls_enabled',c.relrowsecurity,'rls_forced',c.relforcerowsecurity) ORDER BY n.nspname,c.relname),'[]'::jsonb)::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY("
            + schema_array
            + ") AND c.relkind IN ('r','p','v','m','f');",
            label="catalog_relations",
        ),
        "functions": query_json(
            container,
            "SELECT COALESCE(jsonb_agg(jsonb_build_object('identity',n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')','owner',pg_get_userbyid(p.proowner),'security_definer',p.prosecdef,'configuration',COALESCE(p.proconfig::text,'')) ORDER BY n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)),'[]'::jsonb)::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY("
            + schema_array
            + ");",
            label="catalog_functions",
        ),
        "extensions": query_json(
            container,
            "SELECT COALESCE(jsonb_object_agg(extname,extversion),'{}'::jsonb)::text FROM pg_extension;",
            label="catalog_extensions",
        ),
        "policies": query_json(
            container,
            "SELECT COALESCE(jsonb_agg(jsonb_build_object('identity',schemaname||'.'||tablename||'.'||policyname,'roles',roles) ORDER BY schemaname,tablename,policyname),'[]'::jsonb)::text FROM pg_policies WHERE schemaname=ANY("
            + schema_array
            + ");",
            label="catalog_policies",
        ),
        "public_privilege_count": query_json(
            container,
            "SELECT to_jsonb((SELECT count(*) FROM ((SELECT 1 FROM pg_namespace n,LATERAL aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a WHERE n.nspname=ANY("
            + schema_array
            + ") AND a.grantee=0) UNION ALL (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace,LATERAL aclexplode(COALESCE(c.relacl,acldefault(CASE WHEN c.relkind='S' THEN 'S'::\"char\" ELSE 'r'::\"char\" END,c.relowner))) a WHERE n.nspname=ANY("
            + schema_array
            + ") AND c.relkind IN ('r','p','v','m','f','S') AND a.grantee=0) UNION ALL (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace,LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE n.nspname=ANY("
            + schema_array
            + ") AND a.grantee=0)) q))::text;",
            label="catalog_public_privileges",
        ),
        "forbidden_schema_count": query_json(
            container,
            "SELECT to_jsonb(count(*))::text FROM pg_namespace WHERE nspname=ANY("
            + _quoted_sql_array(sorted(FORBIDDEN_SCHEMAS))
            + ");",
            label="catalog_forbidden_schemas",
        ),
        "forbidden_role_count": query_json(
            container,
            "SELECT to_jsonb(count(*))::text FROM pg_roles WHERE rolname=ANY("
            + _quoted_sql_array(sorted(FORBIDDEN_ROLES))
            + ") OR "
            + " OR ".join(
                "rolname LIKE '" + prefix + "%'" for prefix in FORBIDDEN_ROLE_PREFIXES
            )
            + ";",
            label="catalog_forbidden_roles",
        ),
    }
    table_identities = [
        str(item["identity"])
        for item in catalog["relations"]
        if item.get("kind") in {"r", "p"}
    ]
    rows = []
    for identity in table_identities:
        schema, table = identity.split(".", 1)
        count = query_json(
            container,
            'SELECT to_jsonb(count(*))::text FROM "'
            + schema
            + '"."'
            + table
            + '";',
            label="catalog_table_rows",
        )
        rows.append({"identity": identity, "rows": int(count)})
    catalog["table_rows"] = rows
    catalog["expected_rls_enabled"] = sorted(
        set(
            re.findall(
                r"(?im)^ALTER TABLE ONLY ([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*) ENABLE ROW LEVEL SECURITY;",
                schema_sql,
            )
        )
    )
    catalog["expected_rls_forced"] = sorted(
        set(
            re.findall(
                r"(?im)^ALTER TABLE ONLY ([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*) FORCE ROW LEVEL SECURITY;",
                schema_sql,
            )
        )
    )
    return catalog


def validate_catalog(catalog: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    expected_schemas = sorted(
        (
            {"name": str(item["name"]), "owner": str(item["owner"])}
            for item in plan["target_schemas"]
        ),
        key=lambda item: item["name"],
    )
    if catalog.get("schemas") != expected_schemas:
        raise InstallExecutionError("installed_schema_contract_mismatch")
    expected_roles = sorted(
        (
            {
                "name": str(item["name"]),
                "can_login": bool(item["can_login"]),
                "superuser": False,
                "create_db": False,
                "create_role": False,
                "replication": False,
                "bypass_rls": False,
            }
            for item in plan["target_roles"]
        ),
        key=lambda item: item["name"],
    )
    if catalog.get("roles") != expected_roles:
        raise InstallExecutionError("installed_role_contract_mismatch")
    if catalog.get("memberships") != plan["target_memberships"]:
        raise InstallExecutionError("installed_membership_contract_mismatch")
    expected_relations = {
        str(item["target_identity"]): str(item["target_owner"])
        for item in plan["targets"]
        if item["object_type"] == "relation"
    }
    installed_relations = {
        str(item["identity"]): str(item["owner"])
        for item in catalog.get("relations", [])
    }
    if installed_relations != expected_relations:
        raise InstallExecutionError("installed_relation_contract_mismatch")
    expected_functions = {
        str(item["target_identity"]): str(item["target_owner"])
        for item in plan["targets"]
        if item["object_type"] == "function"
    }
    installed_functions = {
        str(item["identity"]): str(item["owner"])
        for item in catalog.get("functions", [])
    }
    if installed_functions != expected_functions:
        raise InstallExecutionError("installed_function_contract_mismatch")
    if catalog.get("extensions") != plan["extensions"]:
        raise InstallExecutionError("installed_extension_contract_mismatch")
    if (
        catalog.get("public_privilege_count") != 0
        or catalog.get("forbidden_schema_count") != 0
        or catalog.get("forbidden_role_count") != 0
        or any(int(item.get("rows", -1)) != 0 for item in catalog.get("table_rows", []))
    ):
        raise InstallExecutionError("installed_isolation_contract_mismatch")
    allowed_policy_roles = {
        "public",
        *(str(item["name"]) for item in plan["target_roles"]),
    }
    for policy in catalog.get("policies", []):
        if not set(policy.get("roles") or []).issubset(allowed_policy_roles):
            raise InstallExecutionError("installed_policy_role_contract_mismatch")
    observed_enabled = sorted(
        str(item["identity"])
        for item in catalog["relations"]
        if item.get("rls_enabled") is True
    )
    observed_forced = sorted(
        str(item["identity"])
        for item in catalog["relations"]
        if item.get("rls_forced") is True
    )
    if (
        observed_enabled != catalog.get("expected_rls_enabled")
        or observed_forced != catalog.get("expected_rls_forced")
    ):
        raise InstallExecutionError("installed_rls_contract_mismatch")
    return {
        "extension_count": len(catalog["extensions"]),
        "function_count": len(installed_functions),
        "membership_count": len(catalog["memberships"]),
        "policy_count": len(catalog["policies"]),
        "relation_count": len(installed_relations),
        "rls_enabled_count": len(observed_enabled),
        "rls_forced_count": len(observed_forced),
        "role_count": len(catalog["roles"]),
        "schema_count": len(catalog["schemas"]),
        "table_row_count": sum(int(item["rows"]) for item in catalog["table_rows"]),
    }


def _verify_image() -> dict[str, Any]:
    try:
        records = json.loads(
            run_checked(
                [PODMAN, "image", "inspect", POSTGRES_IMAGE],
                label="postgres_image_inspect",
            )
        )
    except json.JSONDecodeError as error:
        raise InstallExecutionError("postgres_image_inspect_json_invalid") from error
    if not isinstance(records, list) or len(records) != 1:
        raise InstallExecutionError("postgres_image_inspect_shape_invalid")
    record = records[0]
    digests = {str(record.get("Digest") or "")}
    digests.update(str(item).rsplit("@", 1)[-1] for item in record.get("RepoDigests") or [])
    if POSTGRES_IMAGE_DIGEST not in digests:
        raise InstallExecutionError("postgres_image_digest_mismatch")
    return {"digest": POSTGRES_IMAGE_DIGEST, "reference": POSTGRES_IMAGE}


def _verify_runtime() -> dict[str, Any]:
    try:
        info = json.loads(run_checked([PODMAN, "info", "--format=json"], label="podman_info"))
    except json.JSONDecodeError as error:
        raise InstallExecutionError("podman_info_json_invalid") from error
    host = info.get("host") or info.get("Host") or {}
    security = host.get("security") or host.get("Security") or {}
    rootless = security.get("rootless")
    if rootless is None:
        rootless = security.get("Rootless")
    if rootless is not True:
        raise InstallExecutionError("podman_not_rootless")
    return {
        "cgroup_version": host.get("cgroupVersion") or host.get("CgroupsVersion"),
        "rootless": True,
    }


def _verify_container_sandbox(container: str, baseline: Path) -> dict[str, Any]:
    try:
        records = json.loads(run_checked([PODMAN, "inspect", container], label="container_inspect"))
    except json.JSONDecodeError as error:
        raise InstallExecutionError("container_inspect_json_invalid") from error
    if not isinstance(records, list) or len(records) != 1:
        raise InstallExecutionError("container_inspect_shape_invalid")
    record = records[0]
    host = record.get("HostConfig") or {}
    config = record.get("Config") or {}
    mounts = record.get("Mounts") or []
    baseline_mount = [item for item in mounts if item.get("Destination") == "/baseline"]
    security = [str(item).lower() for item in host.get("SecurityOpt") or []]
    cap_drop = {
        str(item).lower().removeprefix("cap_")
        for item in host.get("CapDrop") or []
    }
    cap_add = {
        str(item).lower().removeprefix("cap_")
        for item in host.get("CapAdd") or []
    }
    ports = (record.get("NetworkSettings") or {}).get("Ports") or {}
    if (
        str(host.get("NetworkMode") or "") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or int(host.get("PidsLimit") or 0) != 256
        or int(host.get("Memory") or 0) != 4 * 1024 * 1024 * 1024
        or int(host.get("NanoCpus") or 0) != 2_000_000_000
        or str(config.get("User") or "") != SERVICE_USER
        or cap_add
        or not (
            "all" in cap_drop
            or PODMAN_DEFAULT_CAPABILITIES.issubset(cap_drop)
        )
        or not any("no-new-privileges" in item for item in security)
        or ports
        or len(baseline_mount) != 1
        or baseline_mount[0].get("RW") is not False
        or Path(str(baseline_mount[0].get("Source") or "")).resolve() != baseline
    ):
        raise InstallExecutionError("container_sandbox_contract_mismatch")
    return {
        "capabilities_dropped": "all",
        "cpus": 2,
        "memory_bytes": 4 * 1024 * 1024 * 1024,
        "network_mode": "none",
        "no_new_privileges": True,
        "pids_limit": 256,
        "privileged": False,
        "published_ports": 0,
        "read_only_root": True,
        "schema_installer_user": "0:0 (rootless user namespace)",
        "service_user": SERVICE_USER,
    }


def _resource_exists(kind: str, name: str) -> bool:
    return _run_status([PODMAN, kind, "exists", name]) == 0


def cleanup(names: Mapping[str, str]) -> dict[str, bool]:
    errors: list[str] = []
    if _resource_exists("container", names["container"]):
        try:
            run_checked(
                [PODMAN, "rm", "--force", names["container"]],
                label="cleanup_container",
                timeout=120,
            )
        except InstallExecutionError:
            errors.append("container")
    for key in ("data_volume", "socket_volume"):
        name = names[key]
        if _resource_exists("volume", name):
            try:
                run_checked(
                    [PODMAN, "volume", "rm", "--force", name],
                    label="cleanup_" + key,
                    timeout=120,
                )
            except InstallExecutionError:
                errors.append(key)
    result = {
        "container_absent": not _resource_exists("container", names["container"]),
        "data_volume_absent": not _resource_exists("volume", names["data_volume"]),
        "socket_volume_absent": not _resource_exists("volume", names["socket_volume"]),
    }
    if errors or not all(result.values()):
        raise InstallExecutionError("sandbox_cleanup_failed")
    return result


def _prepare_output(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise InstallContractError("output_already_exists")
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise InstallContractError("output_parent_invalid")
    path.mkdir(mode=0o700)
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise InstallContractError("output_permissions_invalid")
    return path


def _write_exclusive(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    baseline_receipt, artifacts = verify_baseline(
        arguments.baseline,
        arguments.baseline_receipt_sha256,
        arguments.candidate_commit,
    )
    baseline = arguments.baseline.resolve(strict=True)
    plan = _target_plan(baseline_receipt)
    names = sandbox_names(arguments.run_id)
    output = _prepare_output(arguments.output)
    if any(_resource_exists(kind, names[key]) for kind, key in (
        ("container", "container"),
        ("volume", "data_volume"),
        ("volume", "socket_volume"),
    )):
        raise InstallContractError("sandbox_name_already_exists")
    runtime = _verify_runtime()
    image = _verify_image()
    created = False
    cleanup_result: dict[str, bool] | None = None
    try:
        for key in ("data_volume", "socket_volume"):
            run_checked(
                [
                    PODMAN,
                    "volume",
                    "create",
                    "--label=lifeswitch.work-runner.boundary=platform-clean-install-v1",
                    names[key],
                ],
                label="create_" + key,
            )
        run_checked(
            build_create_command(names=names, baseline=baseline),
            label="container_create",
        )
        created = True
        sandbox = _verify_container_sandbox(names["container"], baseline)
        run_checked([PODMAN, "start", names["container"]], label="container_start")
        ready = False
        for _ in range(60):
            if _run_status(
                [
                    PODMAN,
                    "exec",
                    "--user=" + SERVICE_USER,
                    names["container"],
                    "pg_isready",
                    "--host=/var/run/postgresql",
                    "--username=" + ADMIN_ROLE,
                    "--dbname=" + DATABASE,
                    "--quiet",
                ]
            ) == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise InstallExecutionError("postgres_readiness_timeout")
        _podman_exec(
            names["container"],
            [
                "psql",
                "--no-psqlrc",
                "--set=ON_ERROR_STOP=1",
                "--file=/baseline/platform-clean-install-v1.sql",
            ],
            label="schema_install",
            user="0:0",
        )
        schema_sql = artifacts["platform-clean-schema-v1.sql"].read_text(encoding="utf-8")
        catalog = collect_catalog(names["container"], plan, schema_sql)
        catalog_summary = validate_catalog(catalog, plan)
        catalog_sha256 = sha256_bytes(canonical_bytes(catalog))
        cleanup_result = cleanup(names)
        created = False
        receipt = {
            "authority": {
                "data_copy_authority": False,
                "deployment_authority": False,
                "production_change_authority": False,
                "retirement_authority": False,
            },
            "baseline": {
                "candidate_commit": arguments.candidate_commit,
                "canonical_schema_sha256": baseline_receipt["artifacts"]["platform-clean-schema-v1.sql"]["sha256"],
                "receipt_sha256": arguments.baseline_receipt_sha256,
            },
            "catalog": {
                **catalog_summary,
                "catalog_sha256": catalog_sha256,
            },
            "cleanup": {**cleanup_result, "status": "pass"},
            "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "image": image,
            "production_database_writes": 0,
            "run_id": arguments.run_id,
            "runtime": {**runtime, **sandbox},
            "schema_version": SCHEMA_VERSION,
            "status": "pass",
            "tool_sha256": sha256_file(Path(__file__).resolve()),
        }
        receipt_bytes = canonical_bytes(receipt) + b"\n"
        receipt_path = output / "platform-clean-install-verification-v1.json"
        _write_exclusive(receipt_path, receipt_bytes)
        return {
            "catalog_sha256": catalog_sha256,
            "output_directory": str(output),
            "receipt_sha256": sha256_bytes(receipt_bytes),
            "status": "pass",
            "tool_sha256": receipt["tool_sha256"],
        }
    except Exception:
        if created or any(_resource_exists(kind, names[key]) for kind, key in (
            ("container", "container"),
            ("volume", "data_volume"),
            ("volume", "socket_volume"),
        )):
            cleanup(names)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--baseline-receipt-sha256", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = execute(arguments)
    except InstallContractError as error:
        result = {"error": "install_contract_error", "reason": str(error), "status": "error"}
        exit_code = 2
    except InstallExecutionError as error:
        result = {"error": "install_execution_error", "reason": str(error), "status": "error"}
        exit_code = 3
    except Exception:
        result = {"error": "install_unexpected_error", "status": "error"}
        exit_code = 3
    else:
        exit_code = 0
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
