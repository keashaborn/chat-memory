#!/usr/bin/env python3
from __future__ import annotations

"""Render a commit-addressed systemd release drop-in without writing it."""

import argparse
import re
from pathlib import Path


HEX40 = re.compile(r"^[0-9a-f]{40}$")
PLACEHOLDER = "@COMMIT@"
UNKNOWN_PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")


class ReleaseUnitError(RuntimeError):
    pass


def render_release_unit(template: str, commit: str) -> str:
    if not HEX40.fullmatch(commit):
        raise ReleaseUnitError("commit_invalid")
    count = template.count(PLACEHOLDER)
    if count < 1:
        raise ReleaseUnitError("commit_placeholder_missing")
    unknown = sorted(set(UNKNOWN_PLACEHOLDER.findall(template)) - {PLACEHOLDER})
    if unknown:
        raise ReleaseUnitError("unknown_placeholder")
    rendered = template.replace(PLACEHOLDER, commit)
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
    arguments = parser.parse_args(argv)
    try:
        template = arguments.template.read_text(encoding="utf-8")
        rendered = render_release_unit(template, arguments.commit)
    except (OSError, UnicodeError, ReleaseUnitError) as error:
        reason = str(error) if isinstance(error, ReleaseUnitError) else "template_unavailable"
        print(f"release_unit_error={reason}")
        return 2
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
