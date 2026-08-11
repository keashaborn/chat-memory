from __future__ import annotations

"""Fail-closed placeholder: Phase 8A does not ship a live Linux adapter."""

from .controller import ControllerError


class LiveControllerUnavailableError(ControllerError):
    pass


class LinuxControllerBackend:
    """Reserved type that cannot be constructed in Phase 8A."""

    controller_mode = "phase8a_live_backend_unavailable"

    def __init__(self, *unused_args: object, **unused_kwargs: object) -> None:
        raise LiveControllerUnavailableError(
            "phase8a_has_no_live_install_or_rollback_adapter"
        )


def build_linux_controller_backend(
    *unused_args: object, **unused_kwargs: object
) -> None:
    raise LiveControllerUnavailableError(
        "phase8a_has_no_live_install_or_rollback_adapter"
    )
