"""Run the inactive-by-default governed-Memory HTTP candidate."""

from .application import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
