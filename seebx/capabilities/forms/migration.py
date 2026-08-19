from __future__ import annotations

"""Deterministic planning primitives for the legacy Forms data migration."""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

try:
    import jsonschema
except ImportError:  # pragma: no cover - guarded at runtime
    jsonschema = None  # type: ignore[assignment]


ALLOWED_TEMPLATE_STATUSES = frozenset({"draft", "published", "archived"})


def _canonical_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _raw_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return _canonical_value(dict(row))


def _ordered_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_raw_row(row) for row in rows]
    return sorted(
        normalized,
        key=lambda row: (
            str(row.get("id") or ""),
            canonical_sha256(row),
        ),
    )


def _uuid_text(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _bounded_text(value: Any, *, minimum: int, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not minimum <= len(normalized) <= maximum:
        return None
    return normalized


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and str(parsed) == str(value).strip() else None


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, Mapping):
        return None
    return _canonical_value(dict(value))


def _valid_json_schema(value: Any) -> dict[str, Any] | None:
    schema = _json_object(value)
    if schema is None:
        return None
    if jsonschema is None:
        raise RuntimeError("jsonschema is required for Forms migration")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError:
        return None
    return schema


def _timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, datetime):
        parsed = value
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _quarantine(
    table: str,
    row: Mapping[str, Any],
    reasons: Sequence[str],
) -> dict[str, Any]:
    return {
        "table": table,
        "reason_codes": sorted(set(reasons)),
        "row": _raw_row(row),
    }


@dataclass(frozen=True)
class FormsMigrationPlan:
    templates: tuple[dict[str, Any], ...]
    versions: tuple[dict[str, Any], ...]
    entries: tuple[dict[str, Any], ...]
    quarantine: tuple[dict[str, Any], ...]
    source_bundle_sha256: str
    valid_bundle_sha256: str
    quarantine_sha256: str

    def summary(self) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        quarantine_tables: dict[str, int] = {}
        for item in self.quarantine:
            table = str(item["table"])
            quarantine_tables[table] = quarantine_tables.get(table, 0) + 1
            for reason in item["reason_codes"]:
                reasons[str(reason)] = reasons.get(str(reason), 0) + 1
        return {
            "contract_version": "lifeswitch_forms_migration_plan_v1",
            "source_counts": {
                "templates": len(self.templates)
                + quarantine_tables.get("vb_form_templates", 0),
                "versions": len(self.versions)
                + quarantine_tables.get("vb_form_versions", 0),
                "entries": len(self.entries)
                + quarantine_tables.get("vb_form_entries", 0),
            },
            "eligible_counts": {
                "templates": len(self.templates),
                "versions": len(self.versions),
                "entries": len(self.entries),
            },
            "quarantine_count": len(self.quarantine),
            "quarantine_table_counts": dict(sorted(quarantine_tables.items())),
            "quarantine_reason_counts": dict(sorted(reasons.items())),
            "source_bundle_sha256": self.source_bundle_sha256,
            "valid_bundle_sha256": self.valid_bundle_sha256,
            "quarantine_sha256": self.quarantine_sha256,
        }


def build_forms_migration_plan(
    template_rows: Sequence[Mapping[str, Any]],
    version_rows: Sequence[Mapping[str, Any]],
    entry_rows: Sequence[Mapping[str, Any]],
) -> FormsMigrationPlan:
    raw_templates = _ordered_rows(template_rows)
    raw_versions = _ordered_rows(version_rows)
    raw_entries = _ordered_rows(entry_rows)
    source_bundle = {
        "templates": raw_templates,
        "versions": raw_versions,
        "entries": raw_entries,
    }

    templates: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    template_owners: dict[str, str] = {}
    version_owners: dict[str, str] = {}
    seen_template_ids: set[str] = set()
    seen_version_ids: set[str] = set()
    seen_template_versions: set[tuple[str, int]] = set()
    seen_entry_ids: set[str] = set()

    for row in raw_templates:
        reasons: list[str] = []
        template_id = _uuid_text(row.get("id"))
        owner = _uuid_text(row.get("owner_user_id"))
        name = _bounded_text(row.get("name"), minimum=1, maximum=200)
        status = str(row.get("status") or "")
        created_at = _timestamp(row.get("created_at"))
        if template_id is None:
            reasons.append("template_id_invalid_uuid")
        elif template_id in seen_template_ids:
            reasons.append("template_id_duplicate")
        if owner is None:
            reasons.append("template_owner_invalid_uuid")
        if name is None:
            reasons.append("template_name_invalid")
        if status not in ALLOWED_TEMPLATE_STATUSES:
            reasons.append("template_status_invalid")
        if created_at is None:
            reasons.append("template_created_at_invalid")
        if reasons:
            quarantine.append(_quarantine("vb_form_templates", row, reasons))
            continue
        assert template_id and owner and name and created_at
        seen_template_ids.add(template_id)
        template_owners[template_id] = owner
        templates.append(
            {
                "form_template_id": template_id,
                "owner_user_id": owner,
                "name": name,
                "status": status,
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

    for row in raw_versions:
        reasons = []
        version_id = _uuid_text(row.get("id"))
        template_id = _uuid_text(row.get("template_id"))
        number = _positive_int(row.get("version"))
        json_schema = _valid_json_schema(row.get("json_schema"))
        ui_schema = _json_object(row.get("ui_schema"))
        metadata = _json_object(row.get("metadata"))
        created_at = _timestamp(row.get("created_at"))
        owner = template_owners.get(template_id or "")
        if version_id is None:
            reasons.append("version_id_invalid_uuid")
        elif version_id in seen_version_ids:
            reasons.append("version_id_duplicate")
        if template_id is None:
            reasons.append("version_template_id_invalid_uuid")
        elif owner is None:
            reasons.append("version_parent_template_ineligible")
        if number is None:
            reasons.append("version_number_invalid")
        elif template_id is not None and (template_id, number) in seen_template_versions:
            reasons.append("version_number_duplicate_for_template")
        if json_schema is None:
            reasons.append("version_json_schema_invalid")
        if ui_schema is None:
            reasons.append("version_ui_schema_invalid")
        if metadata is None:
            reasons.append("version_metadata_invalid")
        if created_at is None:
            reasons.append("version_created_at_invalid")
        if reasons:
            quarantine.append(_quarantine("vb_form_versions", row, reasons))
            continue
        assert version_id and template_id and number and owner and created_at
        assert json_schema is not None and ui_schema is not None and metadata is not None
        seen_version_ids.add(version_id)
        seen_template_versions.add((template_id, number))
        version_owners[version_id] = owner
        versions.append(
            {
                "form_version_id": version_id,
                "form_template_id": template_id,
                "owner_user_id": owner,
                "version": number,
                "json_schema": json_schema,
                "ui_schema": ui_schema,
                "metadata": metadata,
                "created_at": created_at,
            }
        )

    for row in raw_entries:
        reasons = []
        entry_id = _uuid_text(row.get("id"))
        owner = _uuid_text(row.get("owner_user_id"))
        version_id = _uuid_text(row.get("template_version_id"))
        expected_owner = version_owners.get(version_id or "")
        subject_id = _bounded_text(row.get("subject_id"), minimum=1, maximum=200)
        occurred_at = _timestamp(row.get("occurred_at"))
        created_at = _timestamp(row.get("created_at"))
        data = _json_object(row.get("data"))
        if entry_id is None:
            reasons.append("entry_id_invalid_uuid")
        elif entry_id in seen_entry_ids:
            reasons.append("entry_id_duplicate")
        if owner is None:
            reasons.append("entry_owner_invalid_uuid")
        if version_id is None:
            reasons.append("entry_version_id_invalid_uuid")
        elif expected_owner is None:
            reasons.append("entry_parent_version_ineligible")
        if owner is not None and expected_owner is not None and owner != expected_owner:
            reasons.append("entry_owner_mismatch")
        if subject_id is None:
            reasons.append("entry_subject_id_invalid")
        if occurred_at is None:
            reasons.append("entry_occurred_at_invalid")
        if created_at is None:
            reasons.append("entry_created_at_invalid")
        if data is None:
            reasons.append("entry_data_invalid")
        if reasons:
            quarantine.append(_quarantine("vb_form_entries", row, reasons))
            continue
        assert entry_id and owner and version_id and subject_id
        assert occurred_at and created_at and data is not None
        seen_entry_ids.add(entry_id)
        entries.append(
            {
                "form_entry_id": entry_id,
                "owner_user_id": owner,
                "subject_id": subject_id,
                "form_version_id": version_id,
                "occurred_at": occurred_at,
                "data": data,
                "created_at": created_at,
            }
        )

    templates.sort(key=lambda row: row["form_template_id"])
    versions.sort(key=lambda row: row["form_version_id"])
    entries.sort(key=lambda row: row["form_entry_id"])
    quarantine.sort(
        key=lambda item: (
            item["table"],
            str(item["row"].get("id") or ""),
            canonical_sha256(item),
        )
    )
    valid_bundle = {
        "templates": templates,
        "versions": versions,
        "entries": entries,
    }
    return FormsMigrationPlan(
        templates=tuple(templates),
        versions=tuple(versions),
        entries=tuple(entries),
        quarantine=tuple(quarantine),
        source_bundle_sha256=canonical_sha256(source_bundle),
        valid_bundle_sha256=canonical_sha256(valid_bundle),
        quarantine_sha256=canonical_sha256(quarantine),
    )
