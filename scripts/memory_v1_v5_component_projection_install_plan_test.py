#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile

import memory_v1_v5_component_projection_install_plan as subject


def expect_failure(action, expected: str) -> None:
    try:
        action()
    except subject.PlanError as exc:
        if expected not in str(exc):
            raise AssertionError(f"expected {expected!r}, got {exc!r}") from exc
    else:
        raise AssertionError(f"expected PlanError containing {expected!r}")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    manifest_path = (
        repo_root
        / "ops/manifests/memory_v1_v5_component_projection_install_plan_20260717.json"
    )
    plan = subject.load_json(manifest_path)
    verification = subject.validate_plan(plan, repo_root)
    if verification["production_authorized"] is not False:
        raise AssertionError("committed plan became authorized")

    now = dt.datetime.now(dt.timezone.utc)
    authorization = {
        "contract_version": subject.AUTH_CONTRACT,
        "authorization_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "authorized": True,
        "authorized_by": "unit-test",
        "authorized_at": (now - dt.timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "plan_sha256": verification["plan_sha256"],
        "expected_head_commit": verification["head_commit"],
        "target_server": "seebx",
    }
    with tempfile.TemporaryDirectory(prefix="memory-v1-component-plan-") as directory:
        auth_path = Path(directory) / "authorization.json"
        auth_path.write_text(json.dumps(authorization), encoding="utf-8")
        os.chmod(auth_path, 0o600)
        result = subject.validate_authorization(
            auth_path, plan, verification, repo_root
        )
        if result["authorization_valid"] is not True:
            raise AssertionError("valid authorization was rejected")

        forged = dict(authorization)
        forged["plan_sha256"] = "0" * 64
        auth_path.write_text(json.dumps(forged), encoding="utf-8")
        expect_failure(
            lambda: subject.validate_authorization(
                auth_path, plan, verification, repo_root
            ),
            "plan_sha256 mismatch",
        )

        auth_path.write_text(json.dumps(authorization), encoding="utf-8")
        os.chmod(auth_path, 0o644)
        expect_failure(
            lambda: subject.validate_authorization(
                auth_path, plan, verification, repo_root
            ),
            "file mode must be 0600",
        )

    forged_plan = json.loads(json.dumps(plan))
    forged_plan["production_authorized"] = True
    expect_failure(
        lambda: subject.validate_plan(forged_plan, repo_root, require_git=False),
        "production_authorized=false",
    )

    forged_plan = json.loads(json.dumps(plan))
    forged_plan["forbidden_effects"].remove("write_qdrant_or_redis")
    expect_failure(
        lambda: subject.validate_plan(forged_plan, repo_root, require_git=False),
        "forbidden_effects",
    )

    print("memory_v1_v5_component_projection_install_plan_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
