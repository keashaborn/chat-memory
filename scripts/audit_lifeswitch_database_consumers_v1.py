#!/usr/bin/env python3
from __future__ import annotations

"""Compatibility CLI for the canonical database consumer audit engine."""

from database_consumer_audit_v1 import *  # noqa: F403


if __name__ == "__main__":
    raise SystemExit(main())  # noqa: F405
