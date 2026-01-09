#!/usr/bin/env python3
"""
tools/rebuild_gravity.py

Rebuild gravity_profile cards for all users that appear in memory_raw.

Usage (from /opt/chat-memory):

    ./venv/bin/python tools/rebuild_gravity.py
"""

import os
import sys
from pathlib import Path
from typing import Set

# Ensure project root (/opt/chat-memory) is on sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qdrant_client.http import models as qmodels

# Reuse gravity engine + qdrant client from rag_engine
from rag_engine.gravity import (
    qdrant,
    compute_gravity,
    write_gravity_card,
)

MEMORY_COLLECTION = os.getenv("MEMORY_COLLECTION", "memory_raw")


def get_all_user_ids() -> Set[str]:
    """
    Scan memory_raw and collect all distinct user_id values.
    For now we use a single scroll with a generous limit.
    If your DB grows huge, we can add pagination later.
    """
    user_ids: Set[str] = set()

    try:
        points, next_page = qdrant.scroll(
            collection_name=MEMORY_COLLECTION,
            scroll_filter=None,
            limit=20000,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        print(f"[rebuild_gravity] error scrolling {MEMORY_COLLECTION}: {e}")
        return user_ids

    for p in points or []:
        payload = p.payload or {}
        uid = (payload.get("user_id") or "").strip()
        if uid:
            user_ids.add(uid)

    # If you ever need to support more than 20k points, we can loop using next_page.
    if next_page is not None:
        print("[rebuild_gravity] Warning: next_page is not None; "
              "you may want to add pagination if your dataset grows.")

    return user_ids


def main():
    print("[rebuild_gravity] Starting gravity rebuild...")

    user_ids = get_all_user_ids()
    if not user_ids:
        print("[rebuild_gravity] No user_ids found in memory_raw. Nothing to do.")
        return

    print(f"[rebuild_gravity] Found {len(user_ids)} user(s): {sorted(user_ids)}")

    updated = 0
    for uid in sorted(user_ids):
        print(f"[rebuild_gravity] Computing gravity for user_id={uid!r}...")
        try:
            gravity = compute_gravity(uid)
            if not gravity:
                print(f"[rebuild_gravity]   -> gravity empty; skipping write for {uid!r}")
                continue

            write_gravity_card(uid, gravity)
            print(f"[rebuild_gravity]   -> gravity_profile written with {len(gravity)} weights")
            updated += 1
        except Exception as e:
            print(f"[rebuild_gravity]   -> error for {uid!r}: {e}")

    print(f"[rebuild_gravity] Done. Gravity profiles updated for {updated} user(s).")


if __name__ == "__main__":
    main()
