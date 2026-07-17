#!/usr/bin/env python3

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


REPO_TEMPLATE = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_TEMPLATE / "scripts" / "memory_v1_projection_v5_install_plan.py"
SPEC = importlib.util.spec_from_file_location("install_plan", MODULE_PATH)
assert SPEC and SPEC.loader
install_plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_plan)


class InstallPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        source_plan_path = (
            REPO_TEMPLATE
            / "ops/manifests/memory_v1_projection_v5_production_install_plan_20260715.json"
        )
        self.plan = json.loads(source_plan_path.read_text(encoding="utf-8"))
        for index, entry in enumerate(self.plan["source_files"], start=1):
            path = self.repo / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"fixture-{index}-{entry['kind']}\n".encode()
            path.write_bytes(payload)
            entry["sha256"] = hashlib.sha256(payload).hexdigest()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Memory Test"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "fixture"], check=True
        )
        head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        self.plan["source"]["required_ancestor_commit"] = head

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_plan_is_hash_locked_and_unauthorized(self) -> None:
        result = install_plan.validate_plan(self.plan, self.repo)
        self.assertTrue(result["repository_valid"])
        self.assertFalse(result["production_authorized"])
        self.assertRegex(result["plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(result["verified_source_files"]), 10)

    def test_committed_authorization_is_rejected(self) -> None:
        changed = copy.deepcopy(self.plan)
        changed["production_authorized"] = True
        with self.assertRaisesRegex(install_plan.PlanError, "permanently"):
            install_plan.validate_plan(changed, self.repo)

    def test_tampered_source_is_rejected(self) -> None:
        path = self.repo / self.plan["source_files"][0]["path"]
        path.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(install_plan.PlanError, "source hash mismatch"):
            install_plan.validate_plan(self.plan, self.repo)

    def test_missing_migration_ordinal_is_rejected(self) -> None:
        changed = copy.deepcopy(self.plan)
        changed["source_files"] = [
            entry
            for entry in changed["source_files"]
            if not (entry["kind"] == "migration" and entry["ordinal"] == 5)
        ]
        with self.assertRaisesRegex(install_plan.PlanError, "migration ordinals"):
            install_plan.validate_plan(changed, self.repo)

    def test_authorization_is_short_lived_mode_0600_and_commit_bound(self) -> None:
        verification = install_plan.validate_plan(self.plan, self.repo)
        now = dt.datetime.now(dt.timezone.utc)
        authorization = {
            "contract_version": install_plan.AUTH_CONTRACT,
            "authorization_id": str(uuid.uuid4()),
            "authorized": True,
            "authorized_by": "test",
            "authorized_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + dt.timedelta(minutes=10))
            .isoformat()
            .replace("+00:00", "Z"),
            "plan_sha256": verification["plan_sha256"],
            "expected_head_commit": verification["head_commit"],
            "target_server": "seebx",
        }
        auth_path = self.repo.parent / f"authorization-{uuid.uuid4()}.json"
        try:
            auth_path.write_text(json.dumps(authorization), encoding="utf-8")
            auth_path.chmod(0o600)
            result = install_plan.validate_authorization(
                auth_path, self.plan, verification, self.repo
            )
            self.assertTrue(result["authorization_valid"])
            auth_path.chmod(0o644)
            with self.assertRaisesRegex(install_plan.PlanError, "0600"):
                install_plan.validate_authorization(
                    auth_path, self.plan, verification, self.repo
                )
        finally:
            auth_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
