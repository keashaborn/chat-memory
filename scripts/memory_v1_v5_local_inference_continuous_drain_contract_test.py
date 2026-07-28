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
        "--contract-profile v5_2",
        "--max-jobs 100",
        "--max-runtime-seconds 21600",
        "--max-attempts 2",
        "--max-output-tokens 2048",
        "--rolling-window-seconds 3600",
        "--max-reserved-jobs 100",
        "--failure-threshold 10",
    ):
        require_once(service, value)

    if "--owner-user-id" in service:
        raise AssertionError(
            "continuous drain must derive owners from authenticated registry"
        )
    for universal_owner_boundary in (
        "from scripts.memory_v1_authenticated_owners import "
        "resolve_authenticated_owners",
        "owners = await resolve_authenticated_owners("
    ):
        require_once(scheduler, universal_owner_boundary)

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

    # Only source-bound evidence may enter private inference. Legacy rows that
    # need append-only rebinding remain visible in aggregate reporting.
    for readiness_boundary in (
        "CONTEXT_READY_PREDICATE_SQL",
        "evidence.content_sha256=job.evidence_content_sha256",
        "evidence.source_system='public.chat_log'",
        "source.thread_id::text=evidence.metadata->>'thread_id'",
        "source.request_id::text=evidence.metadata->>'request_id'",
        "public.digest(convert_to(source.text,'UTF8'),'sha256')",
        "substring(",
        '"context_ready_count": context_ready_count',
        '"context_rebind_required_count": max(',
    ):
        if readiness_boundary not in scheduler:
            raise AssertionError(
                f"context-readiness boundary absent: {readiness_boundary}"
            )

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
