#!/usr/bin/env python3
from __future__ import annotations

"""Create and verify an invocation-owned governed-Memory runtime.

The builder is create-only: every output path must be absent.  It performs no
provider, Supabase, PostgreSQL, Qdrant, or production service calls.  Runtime
dependencies install from an invocation-owned wheelhouse with --require-hashes.
The build backend is safely extracted from one exact hash-locked wheel into a
separate no-pip build venv.  Pip is removed from the final runtime before the
content-free receipt is emitted.
"""

from collections.abc import Mapping, Sequence
import argparse
import base64
import configparser
import csv
from email import policy
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import MappingProxyType
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_LOCK = ROOT / "ops" / "governed_memory" / "runtime-requirements.lock"
BUILD_LOCK = ROOT / "ops" / "governed_memory" / "build-requirements.lock"
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_ROOT = ROOT / "rag_engine" / "governed_memory"
PROVIDER_ASSET_SOURCE_PATHS = frozenset(
    {
        "provider_assets/extraction_instructions.txt",
        "provider_assets/extraction_output.schema.json",
        "provider_assets/predicate_catalog.json",
    }
)
SETUPTOOLS_WHEEL = "setuptools-84.0.0-py3-none-any.whl"
SETUPTOOLS_SHA256 = (
    "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670"
)
PROJECT_DISTRIBUTION = "governed-memory-successor"
PROJECT_VERSION = "0.0.0"
PROJECT_WHEEL = "governed_memory_successor-0.0.0-py3-none-any.whl"
DIST_INFO_PREFIX = "governed_memory_successor-0.0.0.dist-info/"
DIST_INFO_MEMBERS = frozenset(
    {
        f"{DIST_INFO_PREFIX}METADATA",
        f"{DIST_INFO_PREFIX}WHEEL",
        f"{DIST_INFO_PREFIX}entry_points.txt",
        f"{DIST_INFO_PREFIX}top_level.txt",
        f"{DIST_INFO_PREFIX}RECORD",
    }
)
EXPECTED_WHEEL_HEADERS = MappingProxyType(
    {
        "Generator": "setuptools (84.0.0)",
        "Root-Is-Purelib": "true",
        "Tag": "py3-none-any",
        "Wheel-Version": "1.0",
    }
)
EXPECTED_CONSOLE_SCRIPTS = MappingProxyType(
    {
        "governed-memory-http": (
            "rag_engine.governed_memory.runtime.application:main"
        )
    }
)
SOURCE_DATE_EPOCH = "1786381200"
HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
WHEEL_NAME_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,510}\.whl\Z",
    re.ASCII,
)
LOCK_RE = re.compile(
    r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)\s+"
    r"--hash=sha256:([0-9a-f]{64})\Z",
    re.ASCII,
)


class CandidateBuildError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_hash_lock(path: Path) -> dict[str, tuple[str, str]]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 64 * 1024:
        raise CandidateBuildError("candidate_lock_invalid")
    logical: list[str] = []
    current = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        piece = line[:-1].rstrip() if continued else line
        current = f"{current} {piece}".strip()
        if not continued:
            logical.append(current)
            current = ""
    if current or not logical:
        raise CandidateBuildError("candidate_lock_invalid")
    result: dict[str, tuple[str, str]] = {}
    for item in logical:
        match = LOCK_RE.fullmatch(item)
        if match is None:
            raise CandidateBuildError("candidate_lock_invalid")
        raw_name, version, digest = match.groups()
        name = _normalize_distribution(raw_name)
        if name in result:
            raise CandidateBuildError("candidate_lock_invalid")
        result[name] = (version, digest)
    return result


def _verify_runtime_wheelhouse(
    path: Path,
    runtime_packages: Mapping[str, tuple[str, str]],
) -> dict[str, str]:
    try:
        root_stat = path.lstat()
    except OSError as exc:
        raise CandidateBuildError("candidate_runtime_wheelhouse_invalid") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.getuid()
        or root_stat.st_gid != os.getgid()
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise CandidateBuildError("candidate_runtime_wheelhouse_invalid")
    expected_hashes = {
        digest for _version, digest in runtime_packages.values()
    }
    if (
        not runtime_packages
        or len(expected_hashes) != len(runtime_packages)
        or any(HASH_RE.fullmatch(digest) is None for digest in expected_hashes)
    ):
        raise CandidateBuildError("candidate_runtime_lock_invalid")
    try:
        children = sorted(path.iterdir())
    except OSError as exc:
        raise CandidateBuildError("candidate_runtime_wheelhouse_invalid") from exc
    if len(children) != len(expected_hashes):
        raise CandidateBuildError("candidate_runtime_wheelhouse_invalid")
    observed: dict[str, str] = {}
    for wheel in children:
        try:
            wheel_stat = wheel.lstat()
        except OSError as exc:
            raise CandidateBuildError(
                "candidate_runtime_wheelhouse_invalid"
            ) from exc
        if (
            wheel.is_symlink()
            or not stat.S_ISREG(wheel_stat.st_mode)
            or WHEEL_NAME_RE.fullmatch(wheel.name) is None
            or wheel_stat.st_uid != root_stat.st_uid
            or wheel_stat.st_gid != root_stat.st_gid
            or stat.S_IMODE(wheel_stat.st_mode) & 0o022
            or not 0 < wheel_stat.st_size <= 128 * 1024 * 1024
        ):
            raise CandidateBuildError("candidate_runtime_wheelhouse_invalid")
        digest = _sha256(wheel)
        if digest not in expected_hashes or digest in observed.values():
            raise CandidateBuildError("candidate_runtime_wheelhouse_invalid")
        observed[wheel.name] = digest
    if set(observed.values()) != expected_hashes:
        raise CandidateBuildError("candidate_runtime_wheelhouse_invalid")
    return observed


def _assert_host_runtime() -> None:
    if (
        sys.implementation.name != "cpython"
        or sys.version_info[:3] != (3, 12, 3)
        or sys.platform != "linux"
        or platform.machine() != "x86_64"
    ):
        raise CandidateBuildError("candidate_host_runtime_invalid")


def _clean_environment() -> dict[str, str]:
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
    }
    return environment


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    maximum_output_bytes: int = 256 * 1024,
) -> str:
    if not isinstance(arguments, (list, tuple)) or not arguments:
        raise CandidateBuildError("candidate_subprocess_invalid")
    try:
        completed = subprocess.run(
            list(arguments),
            cwd=cwd,
            env=_clean_environment(),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateBuildError("candidate_subprocess_failed") from exc
    output = completed.stdout
    if not isinstance(output, bytes) or len(output) > maximum_output_bytes:
        raise CandidateBuildError("candidate_subprocess_output_invalid")
    if completed.returncode != 0:
        raise CandidateBuildError("candidate_subprocess_failed")
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateBuildError("candidate_subprocess_output_invalid") from exc


def _safe_extract_wheel(wheel: Path, destination: Path) -> None:
    destination_root = destination.resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        members = archive.infolist()
        if not members or len(members) > 4096:
            raise CandidateBuildError("candidate_build_wheel_invalid")
        seen: set[str] = set()
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            mode = member.external_attr >> 16
            if (
                not name
                or name in seen
                or "\\" in name
                or path.is_absolute()
                or any(part in ("", ".", "..") for part in path.parts)
                or stat.S_ISLNK(mode)
            ):
                raise CandidateBuildError("candidate_build_wheel_invalid")
            seen.add(name)
            target = destination.joinpath(*path.parts)
            resolved_parent = target.parent.resolve(strict=False)
            if not resolved_parent.is_relative_to(destination_root):
                raise CandidateBuildError("candidate_build_wheel_invalid")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(member) as source, target.open("xb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
            except (OSError, ValueError) as exc:
                raise CandidateBuildError("candidate_build_wheel_invalid") from exc
            target.chmod(0o644)


def _package_source_material(package_root: Path) -> list[tuple[str, str]]:
    if (
        not package_root.is_dir()
        or package_root.is_symlink()
        or not package_root.is_absolute()
    ):
        raise CandidateBuildError("candidate_source_inventory_invalid")
    material: list[tuple[str, str]] = []
    observed_assets: set[str] = set()
    for source in sorted(package_root.rglob("*")):
        relative = source.relative_to(package_root)
        relative_text = relative.as_posix()
        if "__pycache__" in relative.parts:
            continue
        if source.is_symlink():
            raise CandidateBuildError("candidate_source_inventory_invalid")
        if source.is_dir():
            continue
        if not source.is_file():
            raise CandidateBuildError("candidate_source_inventory_invalid")
        if source.suffix != ".py":
            if relative_text not in PROVIDER_ASSET_SOURCE_PATHS:
                raise CandidateBuildError("candidate_source_inventory_invalid")
            observed_assets.add(relative_text)
        material.append((relative_text, _sha256(source)))
    if not material or observed_assets != PROVIDER_ASSET_SOURCE_PATHS:
        raise CandidateBuildError("candidate_source_inventory_invalid")
    return material


def _package_source_tree_sha256(package_root: Path = PACKAGE_ROOT) -> str:
    """Hash only the installable governed-memory package inventory."""

    encoded = json.dumps(
        _package_source_material(package_root),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _required_source_file(path: Path, *, maximum_bytes: int) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CandidateBuildError("candidate_source_inventory_invalid") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 0 <= metadata.st_size <= maximum_bytes
    ):
        raise CandidateBuildError("candidate_source_inventory_invalid")
    return _sha256(path)


def _source_tree_material(source_root: Path = ROOT) -> list[tuple[str, str]]:
    """Bind packaging authority and every installable package/asset byte."""

    if (
        not source_root.is_absolute()
        or not source_root.is_dir()
        or source_root.is_symlink()
    ):
        raise CandidateBuildError("candidate_source_inventory_invalid")
    pyproject = source_root / "pyproject.toml"
    namespace_init = source_root / "rag_engine" / "__init__.py"
    package_root = source_root / "rag_engine" / "governed_memory"
    material = [
        (
            "pyproject.toml",
            _required_source_file(pyproject, maximum_bytes=128 * 1024),
        ),
        (
            "rag_engine/__init__.py",
            _required_source_file(namespace_init, maximum_bytes=128 * 1024),
        ),
    ]
    material.extend(
        (
            f"rag_engine/governed_memory/{relative}",
            digest,
        )
        for relative, digest in _package_source_material(package_root)
    )
    return sorted(material)


def _source_tree_sha256(source_root: Path = ROOT) -> str:
    encoded = json.dumps(
        _source_tree_material(source_root),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _source_bound_root(
    *,
    kind: str,
    lock_sha256: str,
    source_tree_sha256: str,
) -> Path:
    if (
        kind not in {"build", "runtime"}
        or HASH_RE.fullmatch(lock_sha256) is None
        or HASH_RE.fullmatch(source_tree_sha256) is None
    ):
        raise CandidateBuildError("candidate_source_binding_invalid")
    return Path(
        f"/tmp/governed-memory-successor-{kind}-{lock_sha256}-{source_tree_sha256}"
    )


def _copy_candidate_source(
    destination: Path,
    *,
    expected_source_tree_sha256: str,
    source_root: Path = ROOT,
) -> str:
    if HASH_RE.fullmatch(expected_source_tree_sha256) is None:
        raise CandidateBuildError("candidate_source_binding_invalid")
    if _source_tree_sha256(source_root) != expected_source_tree_sha256:
        raise CandidateBuildError("candidate_source_changed_during_build")
    copied_root = destination / "source"
    copied_package = copied_root / "rag_engine" / "governed_memory"
    source_package = source_root / "rag_engine" / "governed_memory"
    try:
        copied_package.mkdir(parents=True)
        shutil.copyfile(
            source_root / "pyproject.toml",
            copied_root / "pyproject.toml",
        )
        shutil.copyfile(
            source_root / "rag_engine" / "__init__.py",
            copied_root / "rag_engine" / "__init__.py",
        )
        for relative_text, _source_sha256 in _package_source_material(
            source_package
        ):
            relative = Path(relative_text)
            source = source_package / relative
            target = copied_package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    except OSError as exc:
        raise CandidateBuildError("candidate_source_copy_failed") from exc
    copied_source_tree_sha256 = _source_tree_sha256(copied_root)
    if copied_source_tree_sha256 != expected_source_tree_sha256:
        raise CandidateBuildError("candidate_source_changed_during_build")
    return copied_source_tree_sha256


def _build_project_wheel(
    *,
    setuptools_wheel: Path,
    build_root: Path,
    expected_source_tree_sha256: str,
) -> tuple[Path, str]:
    if build_root.exists() or build_root.is_symlink():
        raise CandidateBuildError("candidate_build_root_exists")
    if (
        setuptools_wheel.name != SETUPTOOLS_WHEEL
        or not setuptools_wheel.is_file()
        or setuptools_wheel.is_symlink()
        or _sha256(setuptools_wheel) != SETUPTOOLS_SHA256
    ):
        raise CandidateBuildError("candidate_build_backend_invalid")
    build_root.mkdir(mode=0o700)
    build_venv = build_root / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(build_venv)
    build_python = build_venv / "bin" / "python"
    if build_python.is_symlink() or not build_python.is_file():
        raise CandidateBuildError("candidate_build_python_invalid")
    purelib_text = _run(
        [
            str(build_python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ]
    ).strip()
    purelib = Path(purelib_text)
    if not purelib.is_absolute() or not purelib.is_relative_to(build_venv):
        raise CandidateBuildError("candidate_build_python_invalid")
    _safe_extract_wheel(setuptools_wheel, purelib)
    observed_backend = _run(
        [
            str(build_python),
            "-I",
            "-c",
            "import importlib.metadata as m; print(m.version('setuptools'))",
        ]
    ).strip()
    if observed_backend != "84.0.0":
        raise CandidateBuildError("candidate_build_backend_invalid")

    source_sha256 = _copy_candidate_source(
        build_root,
        expected_source_tree_sha256=expected_source_tree_sha256,
    )
    source_root = build_root / "source"
    dist = build_root / "dist"
    dist.mkdir(mode=0o700)
    script = (
        "import sys; from setuptools.build_meta import build_wheel; "
        "print(build_wheel(sys.argv[1], config_settings={}))"
    )
    output = _run(
        [str(build_python), "-I", "-c", script, str(dist)],
        cwd=source_root,
    )
    wheel_names = [
        line.strip()
        for line in output.splitlines()
        if line.strip().endswith(".whl")
    ]
    if wheel_names[-1:] != [PROJECT_WHEEL]:
        raise CandidateBuildError("candidate_project_wheel_invalid")
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].name != PROJECT_WHEEL:
        raise CandidateBuildError("candidate_project_wheel_invalid")
    _verify_project_wheel(
        wheels[0],
        expected_package_root=source_root / "rag_engine" / "governed_memory",
    )
    return wheels[0], source_sha256


def _read_dist_info_member(
    archive: zipfile.ZipFile,
    name: str,
) -> bytes:
    try:
        member = archive.getinfo(name)
        if member.is_dir() or not 0 < member.file_size <= 512 * 1024:
            raise CandidateBuildError("candidate_project_wheel_invalid")
        return archive.read(member)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CandidateBuildError("candidate_project_wheel_invalid") from exc


def _verify_metadata_member(raw: bytes) -> None:
    try:
        metadata = BytesParser(policy=policy.default).parsebytes(raw)
    except (TypeError, ValueError) as exc:
        raise CandidateBuildError("candidate_project_wheel_invalid") from exc
    if metadata.defects:
        raise CandidateBuildError("candidate_project_wheel_invalid")

    expected_single = {
        "Name": PROJECT_DISTRIBUTION,
        "Requires-Python": "==3.12.*",
        "Version": PROJECT_VERSION,
    }
    for name, expected in expected_single.items():
        observed = [str(value) for value in metadata.get_all(name, [])]
        if observed != [expected]:
            raise CandidateBuildError("candidate_project_wheel_invalid")
    expected_requirements = [
        "asyncpg==0.30.0",
        "cryptography==49.0.0",
        "fastapi==0.120.4",
        "PyJWT==2.13.0",
        "uvicorn==0.38.0",
    ]
    if [
        str(value) for value in metadata.get_all("Requires-Dist", [])
    ] != expected_requirements:
        raise CandidateBuildError("candidate_project_wheel_invalid")


def _verify_wheel_member(raw: bytes) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateBuildError("candidate_project_wheel_invalid") from exc
    if not text.endswith("\n") or "\r" in text:
        raise CandidateBuildError("candidate_project_wheel_invalid")
    lines = text.splitlines()
    while lines and not lines[-1]:
        lines.pop()
    if not lines or any(not line or ": " not in line for line in lines):
        raise CandidateBuildError("candidate_project_wheel_invalid")
    headers: dict[str, str] = {}
    for line in lines:
        name, value = line.split(": ", 1)
        if not name or not value or name in headers:
            raise CandidateBuildError("candidate_project_wheel_invalid")
        headers[name] = value
    if headers != EXPECTED_WHEEL_HEADERS:
        raise CandidateBuildError("candidate_project_wheel_invalid")


def _verify_entry_points_member(raw: bytes) -> None:
    try:
        text = raw.decode("utf-8")
        parser = configparser.ConfigParser(
            allow_no_value=False,
            comment_prefixes=(),
            delimiters=("=",),
            empty_lines_in_values=False,
            inline_comment_prefixes=None,
            interpolation=None,
            strict=True,
        )
        parser.optionxform = str
        parser.read_string(text)
    except (UnicodeDecodeError, configparser.Error, ValueError) as exc:
        raise CandidateBuildError("candidate_project_wheel_invalid") from exc
    if (
        not text.endswith("\n")
        or "\r" in text
        or parser.defaults()
        or parser.sections() != ["console_scripts"]
        or dict(parser.items("console_scripts", raw=True))
        != EXPECTED_CONSOLE_SCRIPTS
    ):
        raise CandidateBuildError("candidate_project_wheel_invalid")


def _record_sha256(value: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(value).digest())
    return "sha256=" + encoded.rstrip(b"=").decode("ascii")


def _verify_record_member(
    archive: zipfile.ZipFile,
    names: set[str],
    raw: bytes,
) -> None:
    try:
        text = raw.decode("utf-8")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error, TypeError, ValueError) as exc:
        raise CandidateBuildError("candidate_project_wheel_invalid") from exc
    if not text.endswith("\n") or len(rows) != len(names):
        raise CandidateBuildError("candidate_project_wheel_invalid")
    records: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or not row[0] or row[0] in records:
            raise CandidateBuildError("candidate_project_wheel_invalid")
        records[row[0]] = (row[1], row[2])
    if set(records) != names:
        raise CandidateBuildError("candidate_project_wheel_invalid")
    record_name = f"{DIST_INFO_PREFIX}RECORD"
    if records[record_name] != ("", ""):
        raise CandidateBuildError("candidate_project_wheel_invalid")
    for name in sorted(names - {record_name}):
        try:
            member = archive.read(name)
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise CandidateBuildError("candidate_project_wheel_invalid") from exc
        if records[name] != (_record_sha256(member), str(len(member))):
            raise CandidateBuildError("candidate_project_wheel_invalid")


def _verify_project_wheel(
    path: Path,
    *,
    expected_package_root: Path,
) -> None:
    if path.name != PROJECT_WHEEL or path.is_symlink() or not path.is_file():
        raise CandidateBuildError("candidate_project_wheel_invalid")
    expected_material = dict(_package_source_material(expected_package_root))
    package_prefix = "rag_engine/governed_memory/"
    expected_package_members = {
        package_prefix + relative for relative in expected_material
    }
    expected_names = expected_package_members | set(DIST_INFO_MEMBERS)
    expected_sizes = {
        package_prefix + relative: (expected_package_root / relative).stat().st_size
        for relative in expected_material
    }
    try:
        archive_context = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise CandidateBuildError("candidate_project_wheel_invalid") from exc
    with archive_context as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if (
            not names
            or len(names) != len(set(names))
            or set(names) != expected_names
            or any(
                member.is_dir()
                or stat.S_ISLNK(member.external_attr >> 16)
                or PurePosixPath(member.filename).is_absolute()
                or "\\" in member.filename
                or any(
                    part in {"", ".", "..", "__pycache__"}
                    for part in PurePosixPath(member.filename).parts
                )
                or member.filename.endswith((".pyc", ".pyo"))
                for member in members
            )
        ):
            raise CandidateBuildError("candidate_project_wheel_invalid")
        for name, expected_size in expected_sizes.items():
            if archive.getinfo(name).file_size != expected_size:
                raise CandidateBuildError("candidate_project_wheel_invalid")
        for relative, expected_sha256 in expected_material.items():
            try:
                member = archive.read(package_prefix + relative)
            except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise CandidateBuildError("candidate_project_wheel_invalid") from exc
            if hashlib.sha256(member).hexdigest() != expected_sha256:
                raise CandidateBuildError("candidate_project_wheel_invalid")

        metadata = _read_dist_info_member(
            archive,
            f"{DIST_INFO_PREFIX}METADATA",
        )
        wheel = _read_dist_info_member(archive, f"{DIST_INFO_PREFIX}WHEEL")
        entry_points = _read_dist_info_member(
            archive,
            f"{DIST_INFO_PREFIX}entry_points.txt",
        )
        top_level = _read_dist_info_member(
            archive,
            f"{DIST_INFO_PREFIX}top_level.txt",
        )
        record = _read_dist_info_member(archive, f"{DIST_INFO_PREFIX}RECORD")
        _verify_metadata_member(metadata)
        _verify_wheel_member(wheel)
        _verify_entry_points_member(entry_points)
        if top_level != b"rag_engine\n":
            raise CandidateBuildError("candidate_project_wheel_invalid")
        _verify_record_member(archive, set(names), record)


def _distribution_inventory(python: Path) -> dict[str, str]:
    script = (
        "import importlib.metadata as m,json; "
        "print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()},"
        "sort_keys=True,separators=(',',':')))"
    )
    output = _run([str(python), "-I", "-c", script]).strip()
    try:
        document = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CandidateBuildError("candidate_runtime_inventory_invalid") from exc
    if not isinstance(document, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in document.items()
    ):
        raise CandidateBuildError("candidate_runtime_inventory_invalid")
    return {
        _normalize_distribution(key): value
        for key, value in document.items()
    }


def _remove_group_world_write(root: Path) -> None:
    for path in (root, *sorted(root.rglob("*"))):
        if path.is_symlink():
            continue
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode & ~0o022)
        except OSError as exc:
            raise CandidateBuildError("candidate_runtime_permissions_invalid") from exc


def _create_runtime(
    *,
    runtime_root: Path,
    runtime_wheelhouse: Path,
    project_wheel: Path,
    runtime_packages: Mapping[str, tuple[str, str]],
) -> dict[str, str]:
    if runtime_root.exists() or runtime_root.is_symlink():
        raise CandidateBuildError("candidate_runtime_root_exists")
    _verify_runtime_wheelhouse(runtime_wheelhouse, runtime_packages)
    venv.EnvBuilder(with_pip=True, symlinks=False).create(runtime_root)
    runtime_python = runtime_root / "bin" / "python"
    if runtime_python.is_symlink() or not runtime_python.is_file():
        raise CandidateBuildError("candidate_runtime_python_invalid")
    expected_python = runtime_root / "bin" / "python"
    if runtime_python.resolve() != expected_python:
        raise CandidateBuildError("candidate_runtime_python_invalid")
    pip_version = _run(
        [str(runtime_python), "-I", "-m", "pip", "--version"]
    ).split()
    if pip_version[:2] != ["pip", "24.0"]:
        raise CandidateBuildError("candidate_bootstrap_pip_invalid")
    _run(
        [
            str(runtime_python),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(runtime_wheelhouse),
            "--require-hashes",
            "--only-binary=:all:",
            "--no-compile",
            "-r",
            str(RUNTIME_LOCK),
        ]
    )
    _run(
        [
            str(runtime_python),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            str(project_wheel),
        ]
    )
    _run([str(runtime_python), "-I", "-m", "pip", "check"])
    import_sweep = """\
import importlib
import importlib.util
from pathlib import Path
import pkgutil
import sys

SUCCESSOR_PACKAGE = "rag_engine.governed_memory"
MAX_SUCCESSOR_MODULES = 128

package = importlib.import_module(SUCCESSOR_PACKAGE)
module_names = [SUCCESSOR_PACKAGE]
module_names.extend(
    item.name
    for item in pkgutil.walk_packages(
        package.__path__,
        SUCCESSOR_PACKAGE + ".",
    )
)
module_names = sorted(set(module_names))
if not module_names or len(module_names) > MAX_SUCCESSOR_MODULES:
    raise SystemExit("candidate successor module inventory invalid")
for module_name in module_names:
    importlib.import_module(module_name)

prefix = Path(sys.prefix).resolve(strict=True)
successor_modules = sorted(
    (name, module)
    for name, module in sys.modules.items()
    if name == SUCCESSOR_PACKAGE or name.startswith(SUCCESSOR_PACKAGE + ".")
)
if not successor_modules or len(successor_modules) > MAX_SUCCESSOR_MODULES:
    raise SystemExit("candidate successor import inventory invalid")
for _name, module in successor_modules:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        raise SystemExit("candidate successor module origin missing")
    try:
        resolved = Path(module_file).resolve(strict=True)
    except OSError as exc:
        raise SystemExit("candidate successor module origin invalid") from exc
    if not resolved.is_relative_to(prefix):
        raise SystemExit("candidate successor module outside runtime")

if any(
    name.startswith("rag_engine.")
    and name != SUCCESSOR_PACKAGE
    and not name.startswith(SUCCESSOR_PACKAGE + ".")
    for name in sys.modules
):
    raise SystemExit("candidate successor imported external rag_engine module")
if importlib.util.find_spec("openai") is not None:
    raise SystemExit("openai unexpectedly importable")
if any(name == "openai" or name.startswith("openai.") for name in sys.modules):
    raise SystemExit("openai unexpectedly loaded")
"""
    _run(
        [str(runtime_python), "-I", "-B", "-c", import_sweep],
        cwd=runtime_root,
    )
    _run(
        [
            str(runtime_python),
            "-I",
            "-c",
            "from rag_engine.governed_memory.runtime.application import create_runtime_application; "
            "a=create_runtime_application({}); assert a.state.governed_memory_runtime.mode=='off'",
        ]
    )
    _run(
        [str(runtime_python), "-I", "-m", "pip", "uninstall", "-y", "pip"]
    )
    inventory = _distribution_inventory(runtime_python)
    expected = {
        name: version for name, (version, _digest) in runtime_packages.items()
    }
    expected[PROJECT_DISTRIBUTION] = PROJECT_VERSION
    if inventory != expected:
        raise CandidateBuildError("candidate_runtime_inventory_invalid")
    site_probe = _run(
        [
            str(runtime_python),
            "-I",
            "-c",
            "import importlib.util,json,site,sys; print(json.dumps({"
            "'enable_user_site':site.ENABLE_USER_SITE,"
            "'pip':importlib.util.find_spec('pip') is not None,"
            "'setuptools':importlib.util.find_spec('setuptools') is not None,"
            "'wheel':importlib.util.find_spec('wheel') is not None,"
            "'legacy_path':any(p.startswith('/opt/chat-memory') for p in sys.path)},"
            "sort_keys=True,separators=(',',':')))"
        ]
    ).strip()
    if site_probe != (
        '{"enable_user_site":false,"legacy_path":false,'
        '"pip":false,"setuptools":false,"wheel":false}'
    ):
        raise CandidateBuildError("candidate_runtime_isolation_invalid")
    _remove_group_world_write(runtime_root)
    return inventory


def build_candidate_runtime(
    *,
    runtime_wheelhouse: Path,
    setuptools_wheel: Path,
    receipt_out: Path,
) -> dict[str, object]:
    _assert_host_runtime()
    runtime_packages = _parse_hash_lock(RUNTIME_LOCK)
    build_packages = _parse_hash_lock(BUILD_LOCK)
    if build_packages != {
        "setuptools": ("84.0.0", SETUPTOOLS_SHA256),
    }:
        raise CandidateBuildError("candidate_build_lock_invalid")
    if len(runtime_packages) != 19:
        raise CandidateBuildError("candidate_runtime_lock_invalid")
    _verify_runtime_wheelhouse(runtime_wheelhouse, runtime_packages)
    runtime_lock_sha256 = _sha256(RUNTIME_LOCK)
    build_lock_sha256 = _sha256(BUILD_LOCK)
    source_tree_sha256 = _source_tree_sha256()
    runtime_root = _source_bound_root(
        kind="runtime",
        lock_sha256=runtime_lock_sha256,
        source_tree_sha256=source_tree_sha256,
    )
    build_root = _source_bound_root(
        kind="build",
        lock_sha256=build_lock_sha256,
        source_tree_sha256=source_tree_sha256,
    )
    if receipt_out.exists() or receipt_out.is_symlink():
        raise CandidateBuildError("candidate_receipt_exists")
    project_wheel, copied_source_tree_sha256 = _build_project_wheel(
        setuptools_wheel=setuptools_wheel,
        build_root=build_root,
        expected_source_tree_sha256=source_tree_sha256,
    )
    if copied_source_tree_sha256 != source_tree_sha256:
        raise CandidateBuildError("candidate_source_changed_during_build")
    inventory = _create_runtime(
        runtime_root=runtime_root,
        runtime_wheelhouse=runtime_wheelhouse,
        project_wheel=project_wheel,
        runtime_packages=runtime_packages,
    )
    runtime_python = runtime_root / "bin" / "python"
    receipt: dict[str, object] = {
        "schema_version": "governed-memory-runtime-build-receipt-v1",
        "candidate_python": str(runtime_python),
        "candidate_python_sha256": _sha256(runtime_python),
        "python_version": "3.12.3",
        "platform": "linux_x86_64",
        "runtime_lock": str(RUNTIME_LOCK.relative_to(ROOT)),
        "runtime_lock_sha256": runtime_lock_sha256,
        "build_lock": str(BUILD_LOCK.relative_to(ROOT)),
        "build_lock_sha256": build_lock_sha256,
        "source_tree_sha256": source_tree_sha256,
        "project_wheel": str(project_wheel),
        "project_wheel_sha256": _sha256(project_wheel),
        "runtime_packages": {
            name: version
            for name, version in sorted(inventory.items())
            if name != PROJECT_DISTRIBUTION
        },
        "project_distribution": {
            "name": PROJECT_DISTRIBUTION,
            "version": PROJECT_VERSION,
        },
        "runtime_package_count": len(runtime_packages),
        "candidate_python_is_symlink": False,
        "pip_present": False,
        "setuptools_present": False,
        "wheel_present": False,
        "user_site_enabled": False,
        "legacy_environment_imported": False,
        "network_calls": 0,
        "provider_calls": 0,
        "persistent_resources_created": False,
        "production_state_changed": False,
    }
    encoded = (
        json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("ascii")
    receipt_out.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            receipt_out,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
    except OSError as exc:
        raise CandidateBuildError("candidate_receipt_write_failed") from exc
    print(encoded.decode("ascii"), end="")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build-candidate-runtime")
    parser.add_argument("--runtime-wheelhouse", type=Path, required=True)
    parser.add_argument("--setuptools-wheel", type=Path, required=True)
    parser.add_argument("--receipt-out", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        build_candidate_runtime(
            runtime_wheelhouse=arguments.runtime_wheelhouse.resolve(),
            setuptools_wheel=arguments.setuptools_wheel.resolve(),
            receipt_out=arguments.receipt_out.resolve(),
        )
    except CandidateBuildError as exc:
        print(
            json.dumps(
                {
                    "error": {"code": str(exc)},
                    "schema_version": "governed-memory-runtime-build-error-v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = [
    "CandidateBuildError",
    "build_candidate_runtime",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
