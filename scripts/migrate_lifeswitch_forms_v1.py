#!/usr/bin/env python3
from __future__ import annotations

"""Plan or apply the owner-bound legacy Forms migration.

The default mode is read-only. Apply mode is hash-bound, insert-only,
transactional, and writes invalid rows to a protected quarantine artifact.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seebx.capabilities.forms.migration import (
    FormsMigrationPlan,
    build_forms_migration_plan,
    canonical_json_bytes,
    canonical_sha256,
)


SOURCE_ENV = "FORMS_LEGACY_SOURCE_DSN"
DESTINATION_ENV = "FORMS_LIFESWITCH_DESTINATION_DSN"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply the LifeSwitch Forms v1 data migration.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the exact hash-bound plan; default is read-only plan mode.",
    )
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--expected-valid-sha256")
    parser.add_argument("--expected-quarantine-sha256")
    parser.add_argument(
        "--approval-id",
        help="Identifier for the separate production migration authorization.",
    )
    parser.add_argument(
        "--encryption-evidence-sha256",
        help="SHA-256 of evidence that the artifact target is encrypted at rest.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="New protected receipt/quarantine directory required in apply mode.",
    )
    return parser


def _required_dsn(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RuntimeError("migration timestamp lost timezone")
    return parsed


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


def _json(value: Mapping[str, Any]) -> str:
    return canonical_json_bytes(dict(value)).decode("utf-8")


def _stored_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise RuntimeError("destination JSON value is not an object")
    return dict(value)


def _fingerprint(values: Mapping[str, Any]) -> str:
    return canonical_sha256({key: str(value) for key, value in values.items()})


async def _database_fingerprint(conn: asyncpg.Connection) -> str:
    row = await conn.fetchrow(
        """
        select current_database() as database_name,
               coalesce(inet_server_addr()::text, 'local') as server_address,
               inet_server_port() as server_port
        """
    )
    assert row is not None
    return _fingerprint(dict(row))


async def _load_source_plan(
    dsn: str,
) -> tuple[FormsMigrationPlan, str]:
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            tables = await conn.fetchval(
                """
                select count(*)
                from (values
                  (to_regclass('public.vb_form_templates')),
                  (to_regclass('public.vb_form_versions')),
                  (to_regclass('public.vb_form_entries'))
                ) as required(relation)
                where relation is not null
                """
            )
            if int(tables or 0) != 3:
                raise RuntimeError("legacy Forms source tables are incomplete")
            templates = await conn.fetch(
                """
                select id, owner_user_id, name, status, created_at
                from public.vb_form_templates
                order by id
                """
            )
            versions = await conn.fetch(
                """
                select id, template_id, version, json_schema, ui_schema,
                       metadata, created_at
                from public.vb_form_versions
                order by id
                """
            )
            entries = await conn.fetch(
                """
                select id, owner_user_id, subject_id, template_version_id,
                       occurred_at, data, created_at
                from public.vb_form_entries
                order by id
                """
            )
            plan = build_forms_migration_plan(templates, versions, entries)
            source_fingerprint = await _database_fingerprint(conn)
        return plan, source_fingerprint
    finally:
        await conn.close()


def _owners(plan: FormsMigrationPlan) -> list[str]:
    return sorted(
        {
            str(row["owner_user_id"])
            for rows in (plan.templates, plan.versions, plan.entries)
            for row in rows
        }
    )


def _rows_for_owner(
    rows: Sequence[Mapping[str, Any]],
    owner: str,
) -> list[Mapping[str, Any]]:
    return [row for row in rows if row["owner_user_id"] == owner]


async def _destination_preflight(conn: asyncpg.Connection) -> None:
    role = await conn.fetchrow(
        """
        select current_user as role_name,
               r.rolsuper,
               r.rolbypassrls,
               r.rolcreaterole,
               r.rolcreatedb,
               r.rolreplication,
               r.rolinherit,
               pg_has_role(current_user, 'lifeswitch_app', 'member') as app_member,
               pg_has_role(current_user, 'lifeswitch_owner', 'member') as owner_member
        from pg_roles r
        where r.rolname = current_user
        """
    )
    if not role or role["app_member"] is not True:
        raise RuntimeError("destination role must inherit lifeswitch_app")
    if role["owner_member"] is True:
        raise RuntimeError("destination role must not inherit lifeswitch_owner")
    forbidden_attributes = (
        "rolsuper",
        "rolbypassrls",
        "rolcreaterole",
        "rolcreatedb",
        "rolreplication",
    )
    if any(role[attribute] is True for attribute in forbidden_attributes):
        raise RuntimeError("destination role has elevated PostgreSQL authority")
    if role["rolinherit"] is not True:
        raise RuntimeError("destination role must inherit application privileges")
    catalog = await conn.fetchrow(
        """
        select count(*) as table_count,
               bool_and(c.relrowsecurity) as rls_enabled,
               bool_and(c.relforcerowsecurity) as rls_forced
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'lifeswitch_forms'
          and c.relname in ('form_template', 'form_version', 'form_entry')
          and c.relkind = 'r'
        """
    )
    if not catalog or (
        int(catalog["table_count"]) != 3
        or catalog["rls_enabled"] is not True
        or catalog["rls_forced"] is not True
    ):
        raise RuntimeError("destination Forms RLS catalog is not ready")
    policies = await conn.fetchval(
        """
        select count(*)
        from pg_policies
        where schemaname = 'lifeswitch_forms'
          and tablename in ('form_template', 'form_version', 'form_entry')
          and cmd = 'ALL'
          and roles @> array['lifeswitch_app']::name[]
        """
    )
    if int(policies or 0) != 3:
        raise RuntimeError("destination Forms owner policies are incomplete")


async def _insert_templates(
    conn: asyncpg.Connection,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    await conn.executemany(
        """
        insert into lifeswitch_forms.form_template (
          form_template_id, owner_user_id, name, status, created_at, updated_at
        ) values ($1, $2, $3, $4, $5, $6)
        on conflict (form_template_id) do nothing
        """,
        [
            (
                _uuid(str(row["form_template_id"])),
                _uuid(str(row["owner_user_id"])),
                str(row["name"]),
                str(row["status"]),
                _timestamp(str(row["created_at"])),
                _timestamp(str(row["updated_at"])),
            )
            for row in rows
        ],
    )


async def _insert_versions(
    conn: asyncpg.Connection,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    await conn.executemany(
        """
        insert into lifeswitch_forms.form_version (
          form_version_id, form_template_id, owner_user_id, version,
          json_schema, ui_schema, metadata, created_at
        ) values ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8)
        on conflict (form_version_id) do nothing
        """,
        [
            (
                _uuid(str(row["form_version_id"])),
                _uuid(str(row["form_template_id"])),
                _uuid(str(row["owner_user_id"])),
                int(row["version"]),
                _json(row["json_schema"]),
                _json(row["ui_schema"]),
                _json(row["metadata"]),
                _timestamp(str(row["created_at"])),
            )
            for row in rows
        ],
    )


async def _insert_entries(
    conn: asyncpg.Connection,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    await conn.executemany(
        """
        insert into lifeswitch_forms.form_entry (
          form_entry_id, owner_user_id, subject_id, form_version_id,
          occurred_at, data, created_at
        ) values ($1, $2, $3, $4, $5, $6::jsonb, $7)
        on conflict (form_entry_id) do nothing
        """,
        [
            (
                _uuid(str(row["form_entry_id"])),
                _uuid(str(row["owner_user_id"])),
                str(row["subject_id"]),
                _uuid(str(row["form_version_id"])),
                _timestamp(str(row["occurred_at"])),
                _json(row["data"]),
                _timestamp(str(row["created_at"])),
            )
            for row in rows
        ],
    )


async def _fetch_destination_owner(
    conn: asyncpg.Connection,
    owner: str,
    plan: FormsMigrationPlan,
) -> dict[str, list[dict[str, Any]]]:
    template_ids = [
        _uuid(str(row["form_template_id"]))
        for row in _rows_for_owner(plan.templates, owner)
    ]
    version_ids = [
        _uuid(str(row["form_version_id"]))
        for row in _rows_for_owner(plan.versions, owner)
    ]
    entry_ids = [
        _uuid(str(row["form_entry_id"]))
        for row in _rows_for_owner(plan.entries, owner)
    ]
    templates = await conn.fetch(
        """
        select form_template_id, owner_user_id, name, status,
               created_at, updated_at
        from lifeswitch_forms.form_template
        where form_template_id = any($1::uuid[])
        order by form_template_id
        """,
        template_ids,
    ) if template_ids else []
    versions = await conn.fetch(
        """
        select form_version_id, form_template_id, owner_user_id, version,
               json_schema, ui_schema, metadata, created_at
        from lifeswitch_forms.form_version
        where form_version_id = any($1::uuid[])
        order by form_version_id
        """,
        version_ids,
    ) if version_ids else []
    entries = await conn.fetch(
        """
        select form_entry_id, owner_user_id, subject_id, form_version_id,
               occurred_at, data, created_at
        from lifeswitch_forms.form_entry
        where form_entry_id = any($1::uuid[])
        order by form_entry_id
        """,
        entry_ids,
    ) if entry_ids else []
    normalized_templates = [dict(row) for row in templates]
    normalized_versions = []
    for row in versions:
        item = dict(row)
        for key in ("json_schema", "ui_schema", "metadata"):
            item[key] = _stored_object(item[key])
        normalized_versions.append(item)
    normalized_entries = []
    for row in entries:
        item = dict(row)
        item["data"] = _stored_object(item["data"])
        normalized_entries.append(item)
    return {
        "templates": normalized_templates,
        "versions": normalized_versions,
        "entries": normalized_entries,
    }


async def _apply_destination(
    dsn: str,
    plan: FormsMigrationPlan,
    source_fingerprint: str,
) -> str:
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        destination_fingerprint = await _database_fingerprint(conn)
        if destination_fingerprint == source_fingerprint:
            raise RuntimeError("source and destination database must differ")
        async with conn.transaction(isolation="serializable"):
            await _destination_preflight(conn)
            observed = {"templates": [], "versions": [], "entries": []}
            for owner in _owners(plan):
                await conn.fetchval(
                    "select set_config('app.user_id', $1, true)",
                    owner,
                )
                templates = _rows_for_owner(plan.templates, owner)
                versions = _rows_for_owner(plan.versions, owner)
                entries = _rows_for_owner(plan.entries, owner)
                await _insert_templates(conn, templates)
                await _insert_versions(conn, versions)
                await _insert_entries(conn, entries)
                owner_rows = await _fetch_destination_owner(conn, owner, plan)
                for key in observed:
                    observed[key].extend(owner_rows[key])
            observed["templates"].sort(key=lambda row: str(row["form_template_id"]))
            observed["versions"].sort(key=lambda row: str(row["form_version_id"]))
            observed["entries"].sort(key=lambda row: str(row["form_entry_id"]))
            if canonical_sha256(observed) != plan.valid_bundle_sha256:
                raise RuntimeError("destination Forms rows differ from migration plan")
        return destination_fingerprint
    finally:
        await conn.close()


def _exclusive_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _prepare_artifact(
    output_dir: Path,
    plan: FormsMigrationPlan,
    source_fingerprint: str,
    approval_id: str,
    encryption_evidence_sha256: str,
) -> Path:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise RuntimeError("output directory already exists")
    if not output_dir.parent.is_dir():
        raise RuntimeError("output directory parent does not exist")
    partial = output_dir.with_name(f".{output_dir.name}.partial-{uuid.uuid4()}")
    os.mkdir(partial, 0o700)
    quarantine_bytes = b"".join(
        canonical_json_bytes(item) + b"\n" for item in plan.quarantine
    )
    _exclusive_write(partial / "quarantine.jsonl", quarantine_bytes)
    prepared = {
        "contract_version": "lifeswitch_forms_migration_receipt_v1",
        "status": "prepared",
        "source_database_fingerprint": source_fingerprint,
        "approval_id": approval_id,
        "encryption_evidence_sha256": encryption_evidence_sha256,
        "plan": plan.summary(),
        "quarantine_file_sha256": hashlib.sha256(quarantine_bytes).hexdigest(),
        "quarantine_file_bytes": len(quarantine_bytes),
    }
    _exclusive_write(
        partial / "prepared-receipt.json",
        canonical_json_bytes(prepared) + b"\n",
    )
    return partial


def _finish_artifact(
    partial: Path,
    output_dir: Path,
    plan: FormsMigrationPlan,
    source_fingerprint: str,
    destination_fingerprint: str,
    approval_id: str,
    encryption_evidence_sha256: str,
) -> dict[str, Any]:
    receipt = {
        "contract_version": "lifeswitch_forms_migration_receipt_v1",
        "status": "applied",
        "source_database_fingerprint": source_fingerprint,
        "destination_database_fingerprint": destination_fingerprint,
        "approval_id": approval_id,
        "encryption_evidence_sha256": encryption_evidence_sha256,
        "plan": plan.summary(),
        "quarantine_artifact": "quarantine.jsonl",
        "quarantine_file_sha256": hashlib.sha256(
            (partial / "quarantine.jsonl").read_bytes()
        ).hexdigest(),
    }
    _exclusive_write(partial / "receipt.json", canonical_json_bytes(receipt) + b"\n")
    os.rename(partial, output_dir.resolve())
    return receipt


def _assert_expected(args: argparse.Namespace, plan: FormsMigrationPlan) -> None:
    expected = {
        "--expected-source-sha256": (
            args.expected_source_sha256,
            plan.source_bundle_sha256,
        ),
        "--expected-valid-sha256": (
            args.expected_valid_sha256,
            plan.valid_bundle_sha256,
        ),
        "--expected-quarantine-sha256": (
            args.expected_quarantine_sha256,
            plan.quarantine_sha256,
        ),
    }
    for flag, (provided, actual) in expected.items():
        if not provided:
            raise RuntimeError(f"{flag} is required in apply mode")
        if provided != actual:
            raise RuntimeError(f"{flag} does not match the current source plan")
    if not isinstance(args.approval_id, str) or not args.approval_id.strip():
        raise RuntimeError("--approval-id is required in apply mode")
    encryption_hash = str(args.encryption_evidence_sha256 or "").lower()
    if len(encryption_hash) != 64 or any(
        character not in "0123456789abcdef" for character in encryption_hash
    ):
        raise RuntimeError(
            "--encryption-evidence-sha256 must be a lowercase SHA-256"
        )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    source_dsn = _required_dsn(SOURCE_ENV)
    plan, source_fingerprint = await _load_source_plan(source_dsn)
    if not args.apply:
        return {
            "mode": "plan",
            "source_database_fingerprint": source_fingerprint,
            "plan": plan.summary(),
            "production_writes": 0,
        }
    _assert_expected(args, plan)
    if args.output_dir is None:
        raise RuntimeError("--output-dir is required in apply mode")
    destination_dsn = _required_dsn(DESTINATION_ENV)
    partial = _prepare_artifact(
        args.output_dir,
        plan,
        source_fingerprint,
        args.approval_id.strip(),
        args.encryption_evidence_sha256.lower(),
    )
    try:
        destination_fingerprint = await _apply_destination(
            destination_dsn,
            plan,
            source_fingerprint,
        )
    except Exception as error:
        failure = {
            "contract_version": "lifeswitch_forms_migration_failure_v1",
            "error_type": type(error).__name__,
            "error_message_sha256": hashlib.sha256(
                str(error).encode("utf-8")
            ).hexdigest(),
            "plan": plan.summary(),
        }
        _exclusive_write(partial / "failure-receipt.json", canonical_json_bytes(failure) + b"\n")
        raise
    receipt = _finish_artifact(
        partial,
        args.output_dir,
        plan,
        source_fingerprint,
        destination_fingerprint,
        args.approval_id.strip(),
        args.encryption_evidence_sha256.lower(),
    )
    return {
        "mode": "apply",
        "output_dir": str(args.output_dir.resolve()),
        "receipt_sha256": hashlib.sha256(
            canonical_json_bytes(receipt) + b"\n"
        ).hexdigest(),
        "plan": plan.summary(),
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as error:
        message_hash = hashlib.sha256(str(error).encode("utf-8")).hexdigest()
        print(
            json.dumps(
                {
                    "error_type": type(error).__name__,
                    "error_message_sha256": message_hash,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
