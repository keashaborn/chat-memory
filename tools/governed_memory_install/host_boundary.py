from __future__ import annotations

"""Closed subprocess boundary for the dormant store package.

The runner has no shell, caller-controlled environment, working directory, or
timeout.  It deliberately rejects image acquisition and remote endpoint
arguments.  Higher-level adapters must narrow the allowed argv further.
"""

from dataclasses import dataclass
import os
from pathlib import PurePath
import re
import selectors
import subprocess
import time
from typing import Final, Sequence


DOCKER_BINARY: Final = "/usr/bin/docker"
PERMITTED_EXECUTABLES: Final = frozenset({DOCKER_BINARY})
FIXED_ENVIRONMENT: Final = {
    "HOME": "/",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "TZ": "UTC",
}
DEFAULT_TIMEOUT_SECONDS: Final = 20
MAX_TIMEOUT_SECONDS: Final = 30
DEFAULT_MAX_OUTPUT_BYTES: Final = 64 * 1024
MAX_OUTPUT_BYTES: Final = 256 * 1024

_FORBIDDEN_COMMAND_WORDS = frozenset(
    {
        "build",
        "buildx",
        "compose",
        "cp",
        "exec",
        "export",
        "import",
        "load",
        "login",
        "pull",
        "push",
        "run",
        "save",
    }
)
_FORBIDDEN_ENDPOINT_MARKERS = (
    "api.openai.com",
    ".supabase.co",
    "provider_endpoint",
    "provider-endpoint",
    "source_postgres",
    "source-postgres",
    "source_database",
    "source-database",
)
_URI_RE = re.compile(r"(?:https?|postgres(?:ql)?|qdrant)://", re.IGNORECASE)
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_IMAGE_REFERENCE_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}\Z",
    re.ASCII,
)
_COMMAND_PROFILES: Final = frozenset({"image_inspect", "store_supervisor"})


class HostBoundaryError(RuntimeError):
    """Content-free refusal at the host command boundary."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise HostBoundaryError("command_argv_invalid")
    normalized = tuple(argv)
    if any(type(item) is not str or not item for item in normalized):
        raise HostBoundaryError("command_argv_invalid")
    if not PurePath(normalized[0]).is_absolute():
        raise HostBoundaryError("command_executable_not_absolute")
    if any("\x00" in item or "\n" in item or "\r" in item for item in normalized):
        raise HostBoundaryError("command_argument_control_character")

    for item in normalized[1:]:
        lowered = item.lower()
        if lowered in _FORBIDDEN_COMMAND_WORDS:
            raise HostBoundaryError("command_operation_forbidden")
        if _URI_RE.search(lowered) is not None:
            raise HostBoundaryError("command_endpoint_forbidden")
        if any(marker in lowered for marker in _FORBIDDEN_ENDPOINT_MARKERS):
            raise HostBoundaryError("command_endpoint_forbidden")
    return normalized


class CommandRunner:
    """Run one bounded absolute argv with a fixed, minimal environment."""

    def __init__(
        self,
        *,
        allowed_executables: frozenset[str] = PERMITTED_EXECUTABLES,
        command_profile: str = "image_inspect",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if not allowed_executables or not allowed_executables.issubset(
            PERMITTED_EXECUTABLES
        ) or any(
            type(path) is not str or not PurePath(path).is_absolute()
            for path in allowed_executables
        ):
            raise HostBoundaryError("allowed_executables_invalid")
        if type(command_profile) is not str or command_profile not in _COMMAND_PROFILES:
            raise HostBoundaryError("command_profile_invalid")
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise HostBoundaryError("command_timeout_invalid")
        if (
            type(max_output_bytes) is not int
            or not 1 <= max_output_bytes <= MAX_OUTPUT_BYTES
        ):
            raise HostBoundaryError("command_output_limit_invalid")
        self._allowed_executables = allowed_executables
        self._command_profile = command_profile
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    def run(self, argv: Sequence[str]) -> CommandResult:
        exact_argv = validate_argv(argv)
        if exact_argv[0] not in self._allowed_executables:
            raise HostBoundaryError("command_executable_forbidden")
        self._validate_profile(exact_argv)
        try:
            process = subprocess.Popen(
                exact_argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=dict(FIXED_ENVIRONMENT),
                close_fds=True,
            )
        except OSError as error:
            raise HostBoundaryError("command_launch_failed") from error

        if process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise HostBoundaryError("command_pipe_unavailable")
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + self._timeout_seconds
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HostBoundaryError("command_timeout")
                events = selector.select(remaining)
                if not events:
                    raise HostBoundaryError("command_timeout")
                for key, _ in events:
                    block = os.read(key.fileobj.fileno(), 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    streams[key.data].extend(block)
                    if sum(len(value) for value in streams.values()) > self._max_output_bytes:
                        raise HostBoundaryError("command_output_limit_exceeded")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostBoundaryError("command_timeout")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise HostBoundaryError("command_timeout") from error
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            selector.close()

        try:
            stdout = bytes(streams["stdout"]).decode("utf-8", errors="strict")
            stderr = bytes(streams["stderr"]).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise HostBoundaryError("command_output_not_utf8") from error
        result = CommandResult(
            argv=exact_argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if returncode != os.EX_OK:
            raise HostBoundaryError("command_failed")
        return result

    def _validate_profile(self, argv: tuple[str, ...]) -> None:
        if self._command_profile == "image_inspect":
            if (
                len(argv) != 6
                or argv[1:5] != ("image", "inspect", "--format", "{{json .}}")
                or _IMAGE_REFERENCE_RE.fullmatch(argv[5]) is None
            ):
                raise HostBoundaryError("command_profile_refused")
            return
        if self._command_profile == "store_supervisor":
            inspect = (
                len(argv) == 6
                and argv[1:5]
                == ("container", "inspect", "--format", "{{json .}}")
                and _CONTAINER_ID_RE.fullmatch(argv[5]) is not None
            )
            start = (
                len(argv) == 3
                and argv[1] == "start"
                and _CONTAINER_ID_RE.fullmatch(argv[2]) is not None
            )
            stop = (
                len(argv) == 4
                and argv[1:3] == ("stop", "--time=30")
                and _CONTAINER_ID_RE.fullmatch(argv[3]) is not None
            )
            if not (inspect or start or stop):
                raise HostBoundaryError("command_profile_refused")
            return
        raise HostBoundaryError("command_profile_refused")
