from __future__ import annotations

"""Inspect-only validation for already-local immutable store images."""

from dataclasses import dataclass
import json
import re
from typing import Final, Mapping, Protocol, Sequence

from .host_boundary import CommandResult, DOCKER_BINARY


_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_REPO_DIGEST_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}\Z")
_REFERENCE_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}\Z"
)
_INSPECT_FORMAT: Final = "{{json .}}"


class ImagePreflightError(RuntimeError):
    """Content-free refusal for a missing or mismatched local image."""


class Runner(Protocol):
    def run(self, argv: Sequence[str]) -> CommandResult:
        ...


@dataclass(frozen=True, slots=True)
class ImageExpectation:
    name: str
    reference: str
    repo_digest: str

    def __post_init__(self) -> None:
        if not self.name or _REFERENCE_RE.fullmatch(self.reference) is None:
            raise ImagePreflightError("image_expectation_invalid")
        if _REPO_DIGEST_RE.fullmatch(self.repo_digest) is None:
            raise ImagePreflightError("image_repo_digest_invalid")
        if not self.reference.endswith("@" + self.repo_digest.split("@", 1)[1]):
            raise ImagePreflightError("image_reference_digest_mismatch")
        reference_repository = self.reference.rsplit("@", 1)[0]
        final_slash = reference_repository.rfind("/")
        tag_separator = reference_repository.find(":", final_slash + 1)
        if tag_separator >= 0:
            reference_repository = reference_repository[:tag_separator]
        if reference_repository != self.repo_digest.split("@", 1)[0]:
            raise ImagePreflightError("image_reference_repository_mismatch")


@dataclass(frozen=True, slots=True)
class LocalImageIdentity:
    name: str
    reference: str
    repo_digest: str
    image_id: str
    os: str
    architecture: str

    def as_dict(self) -> dict[str, str]:
        return {
            "architecture": self.architecture,
            "image_id": self.image_id,
            "name": self.name,
            "os": self.os,
            "reference": self.reference,
            "repo_digest": self.repo_digest,
        }


def _inspect_one(runner: Runner, expected: ImageExpectation) -> LocalImageIdentity:
    result = runner.run(
        (
            DOCKER_BINARY,
            "image",
            "inspect",
            "--format",
            _INSPECT_FORMAT,
            expected.reference,
        )
    )
    try:
        observed = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ImagePreflightError("image_inspect_json_invalid") from error
    if type(observed) is not dict:
        raise ImagePreflightError("image_inspect_shape_invalid")

    image_id = observed.get("Id")
    repo_digests = observed.get("RepoDigests")
    os_name = observed.get("Os")
    architecture = observed.get("Architecture")
    if type(image_id) is not str or _SHA256_RE.fullmatch(image_id) is None:
        raise ImagePreflightError("image_id_invalid")
    if (
        type(repo_digests) is not list
        or not repo_digests
        or any(type(value) is not str for value in repo_digests)
    ):
        raise ImagePreflightError("image_repo_digests_invalid")
    if repo_digests.count(expected.repo_digest) != 1:
        raise ImagePreflightError("image_exact_repo_digest_absent")
    if os_name != "linux" or architecture != "amd64":
        raise ImagePreflightError("image_platform_mismatch")
    return LocalImageIdentity(
        name=expected.name,
        reference=expected.reference,
        repo_digest=expected.repo_digest,
        image_id=image_id,
        os=os_name,
        architecture=architecture,
    )


def inspect_local_images(
    runner: Runner,
    expectations: Sequence[ImageExpectation],
) -> tuple[LocalImageIdentity, ...]:
    if not expectations or len({item.name for item in expectations}) != len(
        expectations
    ):
        raise ImagePreflightError("image_expectations_not_closed")
    identities = tuple(_inspect_one(runner, item) for item in expectations)
    if len({item.image_id for item in identities}) != len(identities):
        raise ImagePreflightError("image_ids_not_distinct")
    return identities


def expectations_from_store_spec(
    spec: Mapping[str, object],
) -> tuple[ImageExpectation, ...]:
    try:
        containers = spec["resources"]["containers"]  # type: ignore[index]
        return tuple(
            ImageExpectation(
                name=name,
                reference=containers[name]["image"]["reference"],
                repo_digest=containers[name]["image"]["repo_digest"],
            )
            for name in ("postgres", "qdrant")
        )
    except (KeyError, TypeError) as error:
        raise ImagePreflightError("store_spec_images_invalid") from error
