#!/usr/bin/env python3
from __future__ import annotations

"""Render a commit-addressed systemd release drop-in without writing it."""

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path


HEX40 = re.compile(r"^[0-9a-f]{40}$")
PLACEHOLDER = "@COMMIT@"
UNKNOWN_PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")
MODE = re.compile(r"^(off|canary|on)$")
OWNER_IDS = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"(?:,[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})*)?$"
)
ZEP_BINDINGS = frozenset(
    {
        "ZEP_SYNC_MODE",
        "ZEP_SYNC_OWNER_IDS",
        "ZEP_TIMEOUT_SECONDS",
        "ZEP_PROMPT_MODE",
        "ZEP_PROMPT_OWNER_IDS",
    }
)


class ReleaseUnitError(RuntimeError):
    pass


def _validate_zep_bindings(bindings: Mapping[str, str]) -> None:
    if set(bindings) != ZEP_BINDINGS or any(not isinstance(value, str) for value in bindings.values()):
        raise ReleaseUnitError("zep_binding_fields_invalid")
    if not MODE.fullmatch(bindings["ZEP_SYNC_MODE"]) or not MODE.fullmatch(bindings["ZEP_PROMPT_MODE"]):
        raise ReleaseUnitError("zep_binding_mode_invalid")
    if not OWNER_IDS.fullmatch(bindings["ZEP_SYNC_OWNER_IDS"]) or not OWNER_IDS.fullmatch(bindings["ZEP_PROMPT_OWNER_IDS"]):
        raise ReleaseUnitError("zep_binding_owner_ids_invalid")
    try:
        timeout = float(bindings["ZEP_TIMEOUT_SECONDS"])
    except ValueError as error:
        raise ReleaseUnitError("zep_binding_timeout_invalid") from error
    if (
        not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", bindings["ZEP_TIMEOUT_SECONDS"])
        or not 0.1 <= timeout <= 30
    ):
        raise ReleaseUnitError("zep_binding_timeout_invalid")
    if bindings["ZEP_SYNC_MODE"] == "canary" and not bindings["ZEP_SYNC_OWNER_IDS"]:
        raise ReleaseUnitError("zep_binding_owner_ids_required")
    if bindings["ZEP_PROMPT_MODE"] == "canary" and not bindings["ZEP_PROMPT_OWNER_IDS"]:
        raise ReleaseUnitError("zep_binding_owner_ids_required")


def render_release_unit(
    template: str,
    commit: str,
    zep_bindings: Mapping[str, str] | None = None,
) -> str:
    if not HEX40.fullmatch(commit):
        raise ReleaseUnitError("commit_invalid")
    count = template.count(PLACEHOLDER)
    if count < 1:
        raise ReleaseUnitError("commit_placeholder_missing")
    found = {value[1:-1] for value in UNKNOWN_PLACEHOLDER.findall(template)}
    unknown = found - {"COMMIT"} - ZEP_BINDINGS
    if unknown:
        raise ReleaseUnitError("unknown_placeholder")
    required_zep = found & ZEP_BINDINGS
    if required_zep:
        if zep_bindings is None:
            raise ReleaseUnitError("zep_bindings_missing")
        _validate_zep_bindings(zep_bindings)
        if required_zep != ZEP_BINDINGS:
            raise ReleaseUnitError("zep_placeholders_incomplete")
    elif zep_bindings is not None:
        raise ReleaseUnitError("zep_bindings_unexpected")
    rendered = template.replace(PLACEHOLDER, commit)
    if zep_bindings is not None:
        for name, value in zep_bindings.items():
            rendered = rendered.replace(f"@{name}@", value)
    if UNKNOWN_PLACEHOLDER.search(rendered):
        raise ReleaseUnitError("unresolved_placeholder")
    if "\x00" in rendered or not rendered.endswith("\n"):
        raise ReleaseUnitError("template_encoding_invalid")
    release_root = f"/opt/lifeswitch/releases/{commit}"
    if release_root not in rendered:
        raise ReleaseUnitError("release_path_missing")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--zep-bindings", type=Path)
    arguments = parser.parse_args(argv)
    try:
        template = arguments.template.read_text(encoding="utf-8")
        bindings = None
        if arguments.zep_bindings is not None:
            value = json.loads(arguments.zep_bindings.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ReleaseUnitError("zep_binding_fields_invalid")
            bindings = value
        rendered = render_release_unit(template, arguments.commit, bindings)
    except (OSError, UnicodeError, json.JSONDecodeError, ReleaseUnitError) as error:
        reason = str(error) if isinstance(error, ReleaseUnitError) else "template_unavailable"
        print(f"release_unit_error={reason}")
        return 2
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
