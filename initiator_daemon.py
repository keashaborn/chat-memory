#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

import fact_jobs
import card_jobs


def _norm_dsn(dsn: str) -> str:
    # asyncpg is happy with postgresql://; normalize from postgres:// if present
    if dsn.startswith("postgres://"):
        return "postgresql://" + dsn[len("postgres://") :]
    return dsn


def _jsonb(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def _load_env_file(path: str) -> None:
    """
    Minimal .env loader (KEY=VALUE lines). Lets initiator_daemon run the same way
    brains.service does (EnvironmentFile=/opt/chat-memory/.env).

    Rules:
      - ignores blank lines and comments (#...)
      - strips surrounding single/double quotes
      - does NOT override already-set environment variables
    """
    if not path or not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if not k:
                    continue
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                os.environ.setdefault(k, v)
    except Exception:
        return


async def fetch_controller_config(conn: asyncpg.Connection, vantage_id: str) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT vantage_id, enabled, tick_seconds, max_jobs_per_tick, max_running_jobs,
               daily_cost_budget_usd, allowed_job_types, updated_at
        FROM vantage_initiator.controller_config
        WHERE vantage_id = $1
        """,
        vantage_id,
    )
    if not row:
        raise RuntimeError(f"Missing controller_config for vantage_id={vantage_id!r}")

    allowed = row["allowed_job_types"]
    if isinstance(allowed, str):
        try:
            allowed = json.loads(allowed)
        except Exception:
            allowed = [allowed]

    return {
        "vantage_id": row["vantage_id"],
        "enabled": bool(row["enabled"]),
        "tick_seconds": int(row["tick_seconds"]),
        "max_jobs_per_tick": int(row["max_jobs_per_tick"]),
        "max_running_jobs": int(row["max_running_jobs"]),
        "daily_cost_budget_usd": float(row["daily_cost_budget_usd"]),
        "allowed_job_types": allowed if isinstance(allowed, list) else [],
        "updated_at": str(row["updated_at"]),
    }


async def compute_drives_v1(conn: asyncpg.Connection, vantage_id: str) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM vantage_initiator.job WHERE vantage_id=$1 AND status='queued')     AS queued,
          (SELECT count(*) FROM vantage_initiator.job WHERE vantage_id=$1 AND status='running')    AS running,
          (SELECT count(*) FROM vantage_initiator.job WHERE vantage_id=$1 AND status='succeeded')  AS succeeded,
          (SELECT count(*) FROM vantage_initiator.job WHERE vantage_id=$1 AND status='failed')     AS failed,

          (SELECT EXTRACT(EPOCH FROM (now() - min(scheduled_at)))
             FROM vantage_initiator.job
            WHERE vantage_id=$1 AND status='queued') AS queued_oldest_age_s,

          (SELECT EXTRACT(EPOCH FROM (now() - min(locked_at)))
             FROM vantage_initiator.job
            WHERE vantage_id=$1 AND status='running' AND locked_at IS NOT NULL) AS running_oldest_lock_age_s,

          (SELECT count(*)
             FROM vantage_initiator.job_run jr
             JOIN vantage_initiator.job j ON j.job_id = jr.job_id
            WHERE j.vantage_id=$1
              AND jr.finished_at >= now() - interval '1 hour'
              AND jr.error IS NULL) AS runs_ok_1h,

          (SELECT count(*)
             FROM vantage_initiator.job_run jr
             JOIN vantage_initiator.job j ON j.job_id = jr.job_id
            WHERE j.vantage_id=$1
              AND jr.finished_at >= now() - interval '1 hour'
              AND jr.error IS NOT NULL) AS runs_fail_1h
        """,
        vantage_id,
    )
    return {
        "mode": "drives_v1",
        "ts_unix": time.time(),
        "queued_jobs": int(row["queued"]),
        "running_jobs": int(row["running"]),
        "succeeded_jobs": int(row["succeeded"]),
        "failed_jobs": int(row["failed"]),
        "queued_oldest_age_s": float(row["queued_oldest_age_s"]) if row["queued_oldest_age_s"] is not None else None,
        "running_oldest_lock_age_s": float(row["running_oldest_lock_age_s"]) if row["running_oldest_lock_age_s"] is not None else None,
        "runs_ok_1h": int(row["runs_ok_1h"]),
        "runs_fail_1h": int(row["runs_fail_1h"]),
    }


async def insert_drive_snapshot(conn: asyncpg.Connection, vantage_id: str, drives: Dict[str, Any], notes: str = "") -> int:
    snapshot_id = await conn.fetchval(
        """
        INSERT INTO vantage_initiator.drive_snapshot(vantage_id, drives, notes)
        VALUES ($1, $2::jsonb, $3)
        RETURNING snapshot_id
        """,
        vantage_id,
        _jsonb(drives),
        notes,
    )
    return int(snapshot_id)


async def ensure_singleton_job(
    conn: asyncpg.Connection,
    vantage_id: str,
    job_type: str,
    payload: Optional[Dict[str, Any]] = None,
    priority: int = 100,
) -> Optional[int]:
    payload = payload or {}
    existing = await conn.fetchval(
        """
        SELECT job_id
        FROM vantage_initiator.job
        WHERE vantage_id=$1
          AND job_type=$2
          AND status IN ('queued','running')
        ORDER BY job_id DESC
        LIMIT 1
        """,
        vantage_id,
        job_type,
    )
    if existing:
        return None

    job_id = await conn.fetchval(
        """
        INSERT INTO vantage_initiator.job(job_type, vantage_id, payload, priority)
        VALUES ($1, $2, $3::jsonb, $4)
        RETURNING job_id
        """,
        job_type,
        vantage_id,
        _jsonb(payload),
        int(priority),
    )
    return int(job_id)


async def ensure_heartbeat_job(conn: asyncpg.Connection, vantage_id: str) -> Optional[int]:
    return await ensure_singleton_job(conn, vantage_id, "heartbeat", payload={}, priority=100)


async def claim_one_job(
    conn: asyncpg.Connection,
    vantage_id: str,
    worker_id: str,
    before_drives: Dict[str, Any],
    allowed_job_types: List[str],
    max_running_jobs: int,
) -> Optional[Tuple[int, str, Dict[str, Any], int]]:
    allowed_job_types = [str(x) for x in (allowed_job_types or []) if str(x).strip()]
    if not allowed_job_types:
        return None

    async with conn.transaction():
        # Serialize claims per-vantage_id so max_running_jobs is actually enforced.
        await conn.execute(
            """
            SELECT 1
            FROM vantage_initiator.controller_config
            WHERE vantage_id=$1
            FOR UPDATE
            """,
            vantage_id,
        )

        running = await conn.fetchval(
            """
            SELECT count(*)
            FROM vantage_initiator.job
            WHERE vantage_id=$1 AND status='running'
            """,
            vantage_id,
        )
        if int(running) >= int(max_running_jobs):
            return None

        row = await conn.fetchrow(
            """
            SELECT job_id, job_type, payload, attempts, max_attempts
            FROM vantage_initiator.job
            WHERE status='queued'
              AND scheduled_at <= now()
              AND vantage_id=$1
              AND attempts < max_attempts
              AND job_type = ANY($2::text[])
            ORDER BY priority ASC, scheduled_at ASC, job_id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            vantage_id,
            allowed_job_types,
        )
        if not row:
            return None

        job_id = int(row["job_id"])
        job_type = str(row["job_type"])
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        await conn.execute(
            """
            UPDATE vantage_initiator.job
            SET status='running',
                locked_by=$1,
                locked_at=now(),
                attempts=attempts+1,
                last_error=NULL
            WHERE job_id=$2
            """,
            worker_id,
            job_id,
        )

        run_id = await conn.fetchval(
            """
            INSERT INTO vantage_initiator.job_run(job_id, worker_id, before_drives)
            VALUES ($1, $2, $3::jsonb)
            RETURNING run_id
            """,
            job_id,
            worker_id,
            _jsonb(before_drives),
        )

        return job_id, job_type, payload, int(run_id)


async def finish_job_success(
    conn: asyncpg.Connection,
    job_id: int,
    run_id: int,
    after_drives: Dict[str, Any],
    outcome: Dict[str, Any],
) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE vantage_initiator.job
            SET status='succeeded',
                locked_by=NULL,
                locked_at=NULL,
                last_error=NULL
            WHERE job_id=$1
            """,
            job_id,
        )
        await conn.execute(
            """
            UPDATE vantage_initiator.job_run
            SET finished_at=now(),
                after_drives=$1::jsonb,
                outcome=$2::jsonb,
                error=NULL
            WHERE run_id=$3
            """,
            _jsonb(after_drives),
            _jsonb(outcome),
            run_id,
        )


async def finish_job_failure(
    conn: asyncpg.Connection,
    job_id: int,
    run_id: int,
    after_drives: Dict[str, Any],
    error: str,
) -> None:
    # Retry if attempts < max_attempts; otherwise mark failed. Linear backoff.
    err = (error or "")[:5000]
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE vantage_initiator.job
            SET status = CASE WHEN attempts < max_attempts THEN 'queued'::vantage_initiator.job_status
                              ELSE 'failed'::vantage_initiator.job_status END,
                scheduled_at = CASE WHEN attempts < max_attempts THEN now() + (attempts * interval '10 seconds')
                                    ELSE scheduled_at END,
                locked_by=NULL,
                locked_at=NULL,
                last_error=$2
            WHERE job_id=$1
            """,
            job_id,
            err,
        )
        await conn.execute(
            """
            UPDATE vantage_initiator.job_run
            SET finished_at=now(),
                after_drives=$1::jsonb,
                outcome=NULL,
                error=$2
            WHERE run_id=$3
            """,
            _jsonb(after_drives),
            err,
            run_id,
        )


async def _reap_stale_running_jobs(conn: asyncpg.Connection, vantage_id: str, stale_running_seconds: int) -> Dict[str, Any]:
    stale_running_seconds = int(stale_running_seconds)
    moved = await conn.fetchval(
        """
        WITH moved AS (
            UPDATE vantage_initiator.job
               SET status='queued'::vantage_initiator.job_status,
                   scheduled_at=now(),
                   locked_by=NULL,
                   locked_at=NULL,
                   last_error=$3
             WHERE vantage_id=$1
               AND status='running'::vantage_initiator.job_status
               AND locked_at IS NOT NULL
               AND locked_at < now() - ($2::int * interval '1 second')
            RETURNING job_id
        )
        SELECT count(*) FROM moved
        """,
        vantage_id,
        stale_running_seconds,
        f"reaped stale running job (locked_at older than {stale_running_seconds}s)",
    )
    return {"requeued_count": int(moved), "stale_running_seconds": stale_running_seconds}


async def process_job(
    conn: asyncpg.Connection,
    cfg: Dict[str, Any],
    job_type: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    vantage_id = str(cfg["vantage_id"])

    if job_type == "heartbeat":
        return {
            "ok": True,
            "job_type": "heartbeat",
            "ts_unix": time.time(),
            "payload_keys": sorted(list(payload.keys())),
        }

    if job_type == "sense_drives_v1":
        drives = await compute_drives_v1(conn, vantage_id)
        drives["mode"] = "sense_drives_v1"
        drives["controller_enabled"] = bool(cfg.get("enabled"))
        drives["allowed_job_types"] = cfg.get("allowed_job_types", [])
        snapshot_id = await insert_drive_snapshot(conn, vantage_id, drives, notes="sense_drives_v1")
        return {"ok": True, "job_type": "sense_drives_v1", "snapshot_id": snapshot_id, "drives": drives}

    if job_type == "enqueue_passes_v1":
        # Deterministic planner v1: keep liveness job queued; optionally enqueue a stale-lock reaper.
        drives = await compute_drives_v1(conn, vantage_id)
        allowed = set(str(x) for x in (cfg.get("allowed_job_types") or []))

        enqueued: List[Dict[str, Any]] = []

        if "heartbeat" in allowed:
            jid = await ensure_heartbeat_job(conn, vantage_id)
            if jid:
                enqueued.append({"job_type": "heartbeat", "job_id": jid})

        stale_s = int(payload.get("stale_running_seconds", 3600))
        rold = drives.get("running_oldest_lock_age_s")
        if rold is not None and rold > stale_s and "reap_stale_jobs_v1" in allowed:
            jid = await ensure_singleton_job(
                conn,
                vantage_id,
                "reap_stale_jobs_v1",
                payload={"stale_running_seconds": stale_s},
                priority=50,
            )
            if jid:
                enqueued.append({"job_type": "reap_stale_jobs_v1", "job_id": jid})


        # --- fact-field loop (v1) ---

        # --- card consolidation loop (v1) ---

        # --- card decay loop (v1) ---
        if "card_decay_v1" in allowed:
            jid = await ensure_singleton_job(
                conn,
                vantage_id,
                "card_decay_v1",
                payload={"limit_cards": 50, "half_life_days": 45.0, "signal_window_days": 180},
                priority=90,
            )
            if jid:
                enqueued.append({"job_type": "card_decay_v1", "job_id": jid})

        if "card_consolidate_kv_v1" in allowed:
            jid = await ensure_singleton_job(
                conn,
                vantage_id,
                "card_consolidate_kv_v1",
                payload={"limit_sources": 5},
                priority=60,
            )
            if jid:
                enqueued.append({"job_type": "card_consolidate_kv_v1", "job_id": jid})

        if any(x in allowed for x in ("fact_seed_from_chat_log_v1","fact_drives_v1","fact_extract_v1","fact_contradiction_scan_v1")):
            try:
                fdr = await fact_jobs.compute_fact_drives(conn)
            except Exception as e:
                fdr = {"pending_sources": 0, "active_claims": 0, "open_contradictions": 0, "error": f"{type(e).__name__}: {e}"}


            seed_enabled = "fact_seed_from_chat_log_v1" in allowed
            pending_sources = int((fdr.get("pending_sources", 0) or 0))
            seed_backlog_cap = int(payload.get("seed_backlog_cap", 25))
            seed_limit = int(payload.get("seed_limit", 5))
            if seed_enabled and pending_sources < seed_backlog_cap:
                jid = await ensure_singleton_job(
                    conn,
                    vantage_id,
                    "fact_seed_from_chat_log_v1",
                    payload={"limit": seed_limit},
                    priority=23,
                )
                if jid:
                    enqueued.append({"job_type": "fact_seed_from_chat_log_v1", "job_id": jid})

            if "fact_drives_v1" in allowed:
                jid = await ensure_singleton_job(conn, vantage_id, "fact_drives_v1", payload={}, priority=25)
                if jid:
                    enqueued.append({"job_type": "fact_drives_v1", "job_id": jid})

            if "fact_extract_v1" in allowed and (int(fdr.get("pending_sources", 0)) > 0 or seed_enabled):
                jid = await ensure_singleton_job(conn, vantage_id, "fact_extract_v1", payload={}, priority=30)
                if jid:
                    enqueued.append({"job_type": "fact_extract_v1", "job_id": jid})

            if "fact_contradiction_scan_v1" in allowed and int(fdr.get("active_claims", 0)) > 0:
                jid = await ensure_singleton_job(conn, vantage_id, "fact_contradiction_scan_v1", payload={"max_groups": 10}, priority=40)
                if jid:
                    enqueued.append({"job_type": "fact_contradiction_scan_v1", "job_id": jid})

        return {"ok": True, "job_type": "enqueue_passes_v1", "enqueued": enqueued, "drives": drives}

    if job_type == "reap_stale_jobs_v1":
        stale_s = int(payload.get("stale_running_seconds", 3600))
        out = await _reap_stale_running_jobs(conn, vantage_id, stale_s)
        return {"ok": True, "job_type": "reap_stale_jobs_v1", **out}


    if job_type == "fact_drives_v1":
        drives = await fact_jobs.compute_fact_drives(conn)
        snapshot_id = await insert_drive_snapshot(conn, vantage_id, drives, notes="fact_drives_v1")
        return {"ok": True, "job_type": "fact_drives_v1", "snapshot_id": snapshot_id, "drives": drives}

    if job_type == "fact_extract_v1":
        out = await fact_jobs.fact_extract_once(conn, max_facts=int(payload.get("max_facts", 50)))
        return {"ok": True, "job_type": "fact_extract_v1", **out}

    if job_type == "fact_contradiction_scan_v1":
        out = await fact_jobs.fact_contradiction_scan_once(conn, max_groups=int(payload.get("max_groups", 10)))
        return {"ok": True, "job_type": "fact_contradiction_scan_v1", **out}


    if job_type == "fact_seed_from_chat_log_v1":
        out = await fact_jobs.fact_seed_from_chat_log_once(
            conn,
            vantage_id,
            limit=int(payload.get("limit", 50)),
        )
        return {"ok": True, "job_type": "fact_seed_from_chat_log_v1", **out}


    if job_type == "card_consolidate_kv_v1":
        out = await card_jobs.card_consolidate_from_kv_once(
            conn,
            vantage_id=vantage_id,
            limit_sources=int(payload.get("limit_sources", 5)),
        )
        return {"ok": True, "job_type": "card_consolidate_kv_v1", **out}


    if job_type == "card_decay_v1":
        out = await card_jobs.card_decay_once(
            conn,
            vantage_id=vantage_id,
            limit_cards=int(payload.get("limit_cards", 50)),
            half_life_days=float(payload.get("half_life_days", 45.0)),
            signal_window_days=int(payload.get("signal_window_days", 180)),
        )
        return {"ok": True, "job_type": "card_decay_v1", **out}

    raise RuntimeError(f"Unknown job_type: {job_type!r}")


async def tick(pool: asyncpg.Pool, vantage_id: str, worker_id: str) -> None:
    async with pool.acquire() as conn:
        cfg = await fetch_controller_config(conn, vantage_id)

        # Always snapshot drives, even if disabled (debugging)
        before = await compute_drives_v1(conn, vantage_id)
        before["controller_enabled"] = cfg["enabled"]
        before["allowed_job_types"] = cfg["allowed_job_types"]
        snapshot_id = await insert_drive_snapshot(conn, vantage_id, before, notes="tick(before)")
        logging.info("tick: snapshot(before) id=%s drives=%s", snapshot_id, before)

        if not cfg["enabled"]:
            return

        allowed = set(str(x) for x in (cfg["allowed_job_types"] or []))

        # Controller loop jobs (autonomy scaffold)
        if "sense_drives_v1" in allowed:
            enq = await ensure_singleton_job(conn, vantage_id, "sense_drives_v1", payload={}, priority=10)
            if enq:
                logging.info("enqueue: sense_drives_v1 job_id=%s", enq)

        if "enqueue_passes_v1" in allowed:
            enq = await ensure_singleton_job(conn, vantage_id, "enqueue_passes_v1", payload={}, priority=20)
            if enq:
                logging.info("enqueue: enqueue_passes_v1 job_id=%s", enq)

        # Liveness job
        if "heartbeat" in allowed:
            enq = await ensure_heartbeat_job(conn, vantage_id)
            if enq:
                logging.info("enqueue: heartbeat job_id=%s", enq)

        # Claim + run up to max_jobs_per_tick
        for _ in range(max(0, int(cfg["max_jobs_per_tick"]))):
            claimed = await claim_one_job(
                conn,
                vantage_id,
                worker_id,
                before,
                allowed_job_types=cfg["allowed_job_types"],
                max_running_jobs=int(cfg["max_running_jobs"]),
            )
            if not claimed:
                break

            job_id, job_type, payload, run_id = claimed
            logging.info("claim: job_id=%s type=%s run_id=%s", job_id, job_type, run_id)

            try:
                outcome = await process_job(conn, cfg, job_type, payload)
                after = await compute_drives_v1(conn, vantage_id)
                await finish_job_success(conn, job_id, run_id, after, outcome)
                logging.info("finish: job_id=%s succeeded outcome=%s", job_id, outcome)
            except Exception as e:
                after = await compute_drives_v1(conn, vantage_id)
                await finish_job_failure(conn, job_id, run_id, after, f"{type(e).__name__}: {e}")
                logging.exception("finish: job_id=%s failed", job_id)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vantage-id", default=os.getenv("VANTAGE_ID", "default"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument(
        "--env-file",
        default=os.getenv("ENV_FILE", "/opt/chat-memory/.env"),
        help="Optional .env file to load if POSTGRES_DSN is not already set.",
    )
    args = ap.parse_args()

    if not os.getenv("POSTGRES_DSN"):
        _load_env_file(args.env_file)

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        print("ERROR: POSTGRES_DSN is not set in environment", file=sys.stderr)
        print(f"       (also tried env-file {args.env_file!r})", file=sys.stderr)
        return 2

    dsn = _norm_dsn(dsn)

    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s")
    logging.info("initiator starting worker_id=%s vantage_id=%s", worker_id, args.vantage_id)

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3)

    try:
        if args.once:
            await tick(pool, args.vantage_id, worker_id)
            logging.info("initiator --once complete")
            return 0

        while True:
            async with pool.acquire() as conn:
                cfg = await fetch_controller_config(conn, args.vantage_id)
                tick_seconds = max(1, int(cfg["tick_seconds"]))
            await tick(pool, args.vantage_id, worker_id)
            await asyncio.sleep(tick_seconds)

    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
