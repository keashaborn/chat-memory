from __future__ import annotations

import base64
import csv
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.governed_memory_release.build_candidate_runtime import (
    CandidateBuildError,
    DIST_INFO_PREFIX,
    PROJECT_WHEEL,
    PROVIDER_ASSET_SOURCE_PATHS,
    _copy_candidate_source,
    _package_source_material,
    _source_tree_material,
    _source_tree_sha256,
    _verify_project_wheel,
)


METADATA = (
    "Metadata-Version: 2.4\n"
    "Name: governed-memory-successor\n"
    "Version: 0.0.0\n"
    "Requires-Python: ==3.12.*\n"
    "Requires-Dist: asyncpg==0.30.0\n"
    "Requires-Dist: cryptography==49.0.0\n"
    "Requires-Dist: fastapi==0.120.4\n"
    "Requires-Dist: PyJWT==2.13.0\n"
    "Requires-Dist: uvicorn==0.38.0\n"
).encode("ascii")
WHEEL = (
    "Wheel-Version: 1.0\n"
    "Generator: setuptools (84.0.0)\n"
    "Root-Is-Purelib: true\n"
    "Tag: py3-none-any\n"
).encode("ascii")
ENTRY_POINTS = (
    "[console_scripts]\n"
    "governed-memory-http = "
    "rag_engine.governed_memory.runtime.application:main\n"
).encode("ascii")
TOP_LEVEL = b"rag_engine\n"


def _record_hash(value: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(value).digest())
    return "sha256=" + encoded.rstrip(b"=").decode("ascii")


def _make_package(root: Path) -> Path:
    package = root / "rag_engine" / "governed_memory"
    provider_assets = package / "provider_assets"
    provider_assets.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "runtime.py").write_text("RUNTIME = 1\n", encoding="utf-8")
    (provider_assets / "__init__.py").write_text("", encoding="utf-8")
    for index, relative in enumerate(sorted(PROVIDER_ASSET_SOURCE_PATHS)):
        path = package / relative
        path.write_bytes(f"synthetic-asset-{index}\n".encode("ascii"))
    return package


def _make_source_root(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\n",
        encoding="utf-8",
    )
    (root / "rag_engine").mkdir()
    (root / "rag_engine" / "__init__.py").write_text(
        "NAMESPACE = 1\n",
        encoding="utf-8",
    )
    _make_package(root)
    return root


def _write_wheel(
    wheel: Path,
    package: Path,
    *,
    omit: str | None = None,
    extra_dist_info: bool = False,
    entry_points: bytes = ENTRY_POINTS,
    wheel_metadata: bytes = WHEEL,
    top_level: bytes = TOP_LEVEL,
    corrupt_record: bool = False,
) -> None:
    package_prefix = "rag_engine/governed_memory/"
    members = {
        package_prefix + relative: (package / relative).read_bytes()
        for relative, _digest in _package_source_material(package)
    }
    members.update(
        {
            f"{DIST_INFO_PREFIX}METADATA": METADATA,
            f"{DIST_INFO_PREFIX}WHEEL": wheel_metadata,
            f"{DIST_INFO_PREFIX}entry_points.txt": entry_points,
            f"{DIST_INFO_PREFIX}top_level.txt": top_level,
        }
    )
    if extra_dist_info:
        members[f"{DIST_INFO_PREFIX}stale.json"] = b"{}\n"
    if omit is not None and omit != "RECORD":
        members.pop(f"{DIST_INFO_PREFIX}{omit}")

    record_name = f"{DIST_INFO_PREFIX}RECORD"
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    corrupt_name = min(name for name in members if name.startswith(package_prefix))
    for name in sorted(members):
        digest = _record_hash(members[name])
        if corrupt_record and name == corrupt_name:
            digest = "sha256=" + "A" * 43
        writer.writerow((name, digest, str(len(members[name]))))
    writer.writerow((record_name, "", ""))
    if omit != "RECORD":
        members[record_name] = output.getvalue().encode("utf-8")

    with zipfile.ZipFile(wheel, "w") as archive:
        for name in sorted(members):
            archive.writestr(name, members[name])


class BuildProvenanceTests(unittest.TestCase):
    def test_full_source_tree_binds_packaging_files_and_copied_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _make_source_root(Path(directory).resolve() / "project")
            original_pyproject = (root / "pyproject.toml").read_bytes()
            original_namespace = (root / "rag_engine" / "__init__.py").read_bytes()
            first = _source_tree_sha256(root)
            paths = {path for path, _digest in _source_tree_material(root)}
            self.assertIn("pyproject.toml", paths)
            self.assertIn("rag_engine/__init__.py", paths)
            self.assertIn("rag_engine/governed_memory/__init__.py", paths)

            (root / "pyproject.toml").write_bytes(original_pyproject + b"# drift\n")
            pyproject_changed = _source_tree_sha256(root)
            self.assertNotEqual(first, pyproject_changed)
            (root / "pyproject.toml").write_bytes(original_pyproject)

            (root / "rag_engine" / "__init__.py").write_bytes(
                original_namespace + b"# drift\n"
            )
            namespace_changed = _source_tree_sha256(root)
            self.assertNotEqual(first, namespace_changed)
            (root / "rag_engine" / "__init__.py").write_bytes(
                original_namespace
            )

            destination = Path(directory).resolve() / "build"
            copied = _copy_candidate_source(
                destination,
                expected_source_tree_sha256=first,
                source_root=root,
            )
            self.assertEqual(copied, first)
            self.assertEqual(_source_tree_sha256(destination / "source"), first)
            self.assertEqual(
                (destination / "source" / "pyproject.toml").read_bytes(),
                original_pyproject,
            )
            self.assertEqual(
                (
                    destination / "source" / "rag_engine" / "__init__.py"
                ).read_bytes(),
                original_namespace,
            )

            (root / "pyproject.toml").write_bytes(original_pyproject + b"# changed\n")
            with self.assertRaisesRegex(
                CandidateBuildError,
                "candidate_source_changed_during_build",
            ):
                _copy_candidate_source(
                    Path(directory).resolve() / "refused-build",
                    expected_source_tree_sha256=first,
                    source_root=root,
                )

    def test_exact_wheel_and_record_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = _make_package(root)
            wheel = root / PROJECT_WHEEL
            _write_wheel(wheel, package)
            _verify_project_wheel(wheel, expected_package_root=package)

    def test_unexpected_and_missing_dist_info_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = _make_package(root)
            wheel = root / PROJECT_WHEEL
            cases = (
                {"extra_dist_info": True},
                {"omit": "WHEEL"},
                {"omit": "RECORD"},
            )
            for options in cases:
                with self.subTest(options=options):
                    _write_wheel(wheel, package, **options)  # type: ignore[arg-type]
                    with self.assertRaisesRegex(
                        CandidateBuildError,
                        "candidate_project_wheel_invalid",
                    ):
                        _verify_project_wheel(
                            wheel,
                            expected_package_root=package,
                        )

    def test_wrong_entrypoint_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = _make_package(root)
            wheel = root / PROJECT_WHEEL
            _write_wheel(
                wheel,
                package,
                entry_points=(
                    b"[console_scripts]\n"
                    b"governed-memory-http = stale.module:main\n"
                ),
            )
            with self.assertRaisesRegex(
                CandidateBuildError,
                "candidate_project_wheel_invalid",
            ):
                _verify_project_wheel(wheel, expected_package_root=package)

    def test_wheel_and_top_level_semantics_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = _make_package(root)
            wheel = root / PROJECT_WHEEL
            cases = (
                {"wheel_metadata": WHEEL + b"Tag: cp312-none-any\n"},
                {"top_level": b"rag_engine\nstale\n"},
            )
            for options in cases:
                with self.subTest(options=options):
                    _write_wheel(wheel, package, **options)  # type: ignore[arg-type]
                    with self.assertRaisesRegex(
                        CandidateBuildError,
                        "candidate_project_wheel_invalid",
                    ):
                        _verify_project_wheel(
                            wheel,
                            expected_package_root=package,
                        )

    def test_corrupt_record_hash_and_record_self_row_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = _make_package(root)
            wheel = root / PROJECT_WHEEL
            _write_wheel(wheel, package, corrupt_record=True)
            with self.assertRaisesRegex(
                CandidateBuildError,
                "candidate_project_wheel_invalid",
            ):
                _verify_project_wheel(wheel, expected_package_root=package)

            _write_wheel(wheel, package)
            record_name = f"{DIST_INFO_PREFIX}RECORD"
            with zipfile.ZipFile(wheel) as source:
                members = {
                    name: source.read(name) for name in source.namelist()
                }
            rows = list(
                csv.reader(
                    io.StringIO(members[record_name].decode("utf-8"), newline="")
                )
            )
            for row in rows:
                if row[0] == record_name:
                    row[1:] = ["sha256=" + "A" * 43, "1"]
            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\n")
            writer.writerows(rows)
            members[record_name] = output.getvalue().encode("utf-8")
            with zipfile.ZipFile(wheel, "w") as archive:
                for name in sorted(members):
                    archive.writestr(name, members[name])
            with self.assertRaisesRegex(
                CandidateBuildError,
                "candidate_project_wheel_invalid",
            ):
                _verify_project_wheel(wheel, expected_package_root=package)


if __name__ == "__main__":
    unittest.main()
