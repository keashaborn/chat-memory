#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ops/systemd/memory-v1-v5-local-inference-scheduler.service"
TIMER = ROOT / "ops/systemd/memory-v1-v5-local-inference-scheduler.timer"
SCHEDULER = ROOT / "scripts/memory_v1_v5_local_inference_scheduler.py"
GPU_SERVICE = ROOT / "ops/systemd/vs-memory-gpu-inference.service"


def require_once(text: str, value: str) -> None:
    if text.count(value) != 1:
        raise AssertionError(f"expected exactly one occurrence of {value!r}")


def main() -> int:
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")
    scheduler = SCHEDULER.read_text(encoding="utf-8")
    gpu_service = GPU_SERVICE.read_text(encoding="utf-8")

    for value in (
        "--owner-user-id 1240822d-ac9a-4096-95aa-e2b24d36ef50",
        "--contract-profile v5_2",
        "--max-jobs 100",
        "--max-runtime-seconds 21600",
        "--max-attempts 2",
        "--max-output-tokens 2048",
        "--rolling-window-seconds 3600",
        "--max-reserved-jobs 100",
        "--failure-threshold 3",
    ):
        require_once(service, value)

    for retained_boundary in (
        "LoadCredential=local_api_key:",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateDevices=true",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        "CapabilityBoundingSet=",
    ):
        require_once(service, retained_boundary)

    require_once(service, "TimeoutStartSec=6h15min")
    require_once(timer, "OnBootSec=5min")
    require_once(timer, "OnUnitInactiveSec=5min")
    require_once(timer, "RandomizedDelaySec=10s")
    require_once(gpu_service, "--ctx-size 32768")
    require_once(gpu_service, "--parallel 1")
    require_once(gpu_service, "--reasoning off")
    require_once(gpu_service, "--reasoning-budget 0")
    require_once(gpu_service, "IPAddressDeny=any")
    require_once(gpu_service, "IPAddressAllow=localhost")

    # The batch worker remains sequential. It must not fan out model calls.
    require_once(scheduler, "while len(results) < args.max_jobs:")
    require_once(scheduler, "result = canary_invoker(")
    for forbidden in ("asyncio.gather(", "ThreadPoolExecutor(", "ProcessPoolExecutor("):
        if forbidden in scheduler:
            raise AssertionError(f"concurrent inference primitive present: {forbidden}")

    print("memory_v1_v5_local_inference_continuous_drain_contract_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
