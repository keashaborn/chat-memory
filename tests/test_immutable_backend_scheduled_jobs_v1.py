from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.verify_immutable_backend_release import (
    ReleaseIntegrityError,
    _content_manifest_sha256,
    verify_release_integrity,
)


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "ops/systemd"
MANIFEST = SYSTEMD / "lifeswitch-backend-scheduled-jobs-v1.json"


class ImmutableBackendScheduledJobsTests(unittest.TestCase):
    def test_manifest_closes_over_all_retained_schedules(self) -> None:
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(value["schema_version"], "lifeswitch-backend-scheduled-jobs-v1")
        self.assertEqual(
            value["retire_units"],
            ["chat-memory-git-sync.service", "chat-memory-git-sync.timer"],
        )
        self.assertEqual(
            {record["timer"] for record in value["jobs"]},
            {
                "ai-operations-alert-delivery-v1.timer",
                "lifeswitch-release-integrity-v1.timer",
                "telemetry-retention-v1.timer",
                "trusted-web-monitor-v1.timer",
                "trusted-web-ods-cache.timer",
                "voice-synthetic-canary.timer",
            },
        )
        for record in [*value["jobs"], *value["manual_services"]]:
            template = ROOT / record["service_template"]
            raw = template.read_text(encoding="utf-8")
            expected_user = (
                "User=root"
                if record["service"] == "lifeswitch-release-integrity-v1.service"
                else "User=ubuntu"
            )
            self.assertIn(expected_user, raw)
            self.assertIn("WorkingDirectory=/opt/lifeswitch/releases/@COMMIT@", raw)
            self.assertIn("PYTHONDONTWRITEBYTECODE=1", raw)
            self.assertIn("@COMMIT@", raw)
            self.assertNotIn("ExecStart=/opt/chat-memory", raw)
            self.assertNotIn("ReadWritePaths=/opt/chat-memory", raw)
        integrity = (SYSTEMD / "lifeswitch-release-integrity-v1.service.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("CapabilityBoundingSet=\n", integrity)
        self.assertIn("ProtectSystem=strict", integrity)
        for record in value["jobs"]:
            timer = ROOT / record["timer_file"]
            self.assertIn(f"Unit={record['timer'].removesuffix('.timer')}.service", timer.read_text(encoding="utf-8"))

    def test_obsolete_git_mirror_assets_are_absent(self) -> None:
        self.assertFalse((SYSTEMD / "chat-memory-git-sync.service").exists())
        self.assertFalse((SYSTEMD / "chat-memory-git-sync.timer").exists())
        self.assertFalse((ROOT / "tools/git_daily_sync.py").exists())

    def _seal_tree(self, root: Path, role: str, commit: str, binding: str) -> None:
        root.mkdir(parents=True)
        payload = root / "payload.txt"
        payload.write_text("bound\n", encoding="utf-8")
        payload.chmod(0o444)
        receipt = {
            "role": role,
            "commit": commit,
            "content_sha256": _content_manifest_sha256(root, frozenset()),
            ("tree" if role == "backend" else "archive_sha256"): binding,
        }
        receipt_path = root / ".lifeswitch-release.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        receipt_path.chmod(0o400)
        root.chmod(0o555)

    def test_integrity_monitor_accepts_bound_sealed_trees_and_rejects_drift(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release_root = root / "releases"
            runtime_root = root / "runtimes"
            self._seal_tree(release_root / commit, "backend", commit, "b" * 64)
            self._seal_tree(runtime_root / commit, "backend_runtime", commit, "c" * 64)
            dropin = root / "zz-immutable-release.conf"
            dropin.write_text(
                "[Service]\n"
                f"WorkingDirectory=/opt/lifeswitch/releases/{commit}\n"
                f"ExecStart=/opt/lifeswitch/runtimes/{commit}/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8088 --app-dir /opt/lifeswitch/releases/{commit}\n",
                encoding="utf-8",
            )
            dropin.chmod(0o644)
            evidence = verify_release_integrity(
                commit,
                release_root=release_root,
                runtime_root=runtime_root,
                dropin=dropin,
                required_uid=os.getuid(),
            )
            self.assertEqual(evidence["commit"], commit)
            payload = release_root / commit / "payload.txt"
            payload.chmod(0o644)
            payload.write_text("drift\n", encoding="utf-8")
            payload.chmod(0o444)
            with self.assertRaisesRegex(ReleaseIntegrityError, "release_content_drift"):
                verify_release_integrity(
                    commit,
                    release_root=release_root,
                    runtime_root=runtime_root,
                    dropin=dropin,
                    required_uid=os.getuid(),
                )

    def test_brains_template_canonicalizes_zep_and_unsets_legacy_shadow(self) -> None:
        raw = (SYSTEMD / "brains-immutable-release.conf.in").read_text(encoding="utf-8")
        for name in (
            "ZEP_SYNC_MODE",
            "ZEP_SYNC_OWNER_IDS",
            "ZEP_TIMEOUT_SECONDS",
            "ZEP_PROMPT_MODE",
            "ZEP_PROMPT_OWNER_IDS",
        ):
            self.assertIn(f"Environment={name}=@{name}@", raw)
        self.assertIn(
            "UnsetEnvironment=ZEP_SHADOW_MODE ZEP_SHADOW_OWNER_IDS ZEP_SHADOW_TIMEOUT_SECONDS",
            raw,
        )


if __name__ == "__main__":
    unittest.main()
