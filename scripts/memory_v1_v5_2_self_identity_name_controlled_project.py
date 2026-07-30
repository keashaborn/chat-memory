#!/usr/bin/env python3
from __future__ import annotations

import asyncio

import memory_v1_v5_claim_projection_controlled_project as base


base.QUERY_BY_PREDICATE["identity.name"] = "What is my name?"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(base.run()))
