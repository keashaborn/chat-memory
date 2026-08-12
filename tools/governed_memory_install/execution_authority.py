from __future__ import annotations

"""Bind verified scope evidence to trusted time and durable single use.

This module has no live executor or command-line interface.  It accepts only an
opaque capability minted by exact local-binding and signature verification,
then requires a held global lock, one trusted UTC reading, and an atomic nonce
claim.  The resulting content-free permit cannot perform effects.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Final, Protocol

from .authority_v2 import (
    AuthorityVerificationError,
    CryptographicallyValidScopeNotExecution,
    _execution_capability_evidence,
)

from .authority_state import (
    AuthorityClaimNotAllowedError,
    AuthorityState,
    HASH_RE,
    NonceClaim,
)
from .execution_lock import (
    ExecutionLockError,
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)


EVIDENCE_RESULT_TYPE: Final = "cryptographically_valid_scope_not_execution"
MAX_AUTHORIZATION_WINDOW_SECONDS: Final = 15 * 60
MIN_TRUSTED_UTC: Final = datetime(2025, 1, 1, tzinfo=timezone.utc)
MAX_TRUSTED_UTC: Final = datetime(2100, 1, 1, tzinfo=timezone.utc)
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z\Z",
    re.ASCII,
)
_EXECUTION_BINDING_DOMAIN: Final = b"governed-memory-execution-binding-v1\x00"


class ExecutionAuthorityError(RuntimeError):
    """Content-free refusal to mint an execution permit."""


class TrustedUtcClock(Protocol):
    """Clock boundary selected and secured by the future Linux executor."""

    def read_utc(self) -> datetime:
        """Return one timezone-aware reading whose tzinfo is ``timezone.utc``."""


class KernelUtcClock:
    """Read the host kernel wall clock.

    Instantiating this class does not establish NTP or host trust.  The future
    executor must use it only after the host clock is independently covered by
    its installation preflight and receipt.
    """

    def read_utc(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class TrustedClockReading:
    observed_at: datetime
    observed_at_utc: str


@dataclass(frozen=True, slots=True)
class ExecutionAuthorityPermit:
    result: str
    nonce_sha256: str
    operation_sha256: str
    execution_sha256: str
    authorization_sha256: str
    scope_sha256: str
    trust_bundle_sha256: str
    claim_sha256: str
    trusted_clock_utc: str


def read_trusted_utc(clock: TrustedUtcClock) -> TrustedClockReading:
    """Read exactly once and strictly validate an injected UTC clock."""

    try:
        observed = clock.read_utc()
    except Exception as error:
        raise ExecutionAuthorityError("trusted_clock_read_failed") from error
    if type(observed) is not datetime or observed.tzinfo is not timezone.utc:
        raise ExecutionAuthorityError("trusted_clock_not_exact_utc")
    if not MIN_TRUSTED_UTC <= observed < MAX_TRUSTED_UTC:
        raise ExecutionAuthorityError("trusted_clock_out_of_range")
    rendered = observed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return TrustedClockReading(observed, rendered)


def _parse_authorization_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise ExecutionAuthorityError("execution_authority_time_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ExecutionAuthorityError("execution_authority_time_invalid") from error
    return parsed


def _validate_evidence(
    capability: object,
    *,
    expected_operation: str,
) -> tuple[CryptographicallyValidScopeNotExecution, datetime, datetime, str]:
    try:
        evidence = _execution_capability_evidence(capability)
    except AuthorityVerificationError as error:
        raise ExecutionAuthorityError(
            "execution_authority_evidence_invalid"
        ) from error
    if evidence.result_type != EVIDENCE_RESULT_TYPE:
        raise ExecutionAuthorityError("execution_authority_evidence_invalid")
    if evidence.operation != expected_operation:
        raise ExecutionAuthorityError("execution_authority_operation_mismatch")
    for value in (
        evidence.scope_sha256,
        evidence.authorization_sha256,
        evidence.trust_bundle_sha256,
    ):
        if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
            raise ExecutionAuthorityError("execution_authority_hash_invalid")
    not_before = _parse_authorization_timestamp(evidence.not_before)
    expires_at = _parse_authorization_timestamp(evidence.expires_at)
    if (
        expires_at <= not_before
        or (expires_at - not_before).total_seconds()
        > MAX_AUTHORIZATION_WINDOW_SECONDS
    ):
        raise ExecutionAuthorityError("execution_authority_window_invalid")
    execution_material = b"\x00".join(
        value.encode("ascii")
        for value in (
            evidence.operation,
            evidence.scope_sha256,
            evidence.authorization_sha256,
            evidence.trust_bundle_sha256,
        )
    )
    execution_sha256 = hashlib.sha256(
        _EXECUTION_BINDING_DOMAIN + execution_material
    ).hexdigest()
    return evidence, not_before, expires_at, execution_sha256


def _permit_from_claim(
    claim: NonceClaim,
    reading: TrustedClockReading,
) -> ExecutionAuthorityPermit:
    if claim.result == "nonce_claimed":
        result = "execution_authority_claimed"
    elif claim.result == "exact_execution_resumed":
        result = "execution_authority_exact_resume"
    else:
        raise ExecutionAuthorityError("execution_authority_claim_invalid")
    return ExecutionAuthorityPermit(
        result=result,
        nonce_sha256=claim.nonce_sha256,
        operation_sha256=claim.operation_sha256,
        execution_sha256=claim.execution_sha256,
        authorization_sha256=claim.authorization_sha256,
        scope_sha256=claim.scope_sha256,
        trust_bundle_sha256=claim.trust_bundle_sha256,
        claim_sha256=claim.claim_sha256,
        trusted_clock_utc=reading.observed_at_utc,
    )


def claim_execution_authority(
    capability: object,
    *,
    state: AuthorityState,
    clock: TrustedUtcClock,
    held_lock: HeldExecutionLockCapability,
    expected_operation: str,
) -> ExecutionAuthorityPermit:
    """Claim a new execution or resume the exact already-claimed execution.

    A new claim requires the trusted reading to be within the signed window.
    An exact durable claim may resume after expiry, but never before
    ``not_before``; that distinction prevents expiry from making crash recovery
    impossible while still treating a backwards clock as a refusal.
    """

    try:
        validate_held_execution_lock(held_lock)
    except ExecutionLockError as error:
        raise ExecutionAuthorityError("execution_authority_lock_not_held") from error
    evidence, not_before, expires_at, execution_sha256 = _validate_evidence(
        capability,
        expected_operation=expected_operation,
    )
    reading = read_trusted_utc(clock)
    if reading.observed_at < not_before:
        raise ExecutionAuthorityError("execution_authority_not_yet_valid")
    allow_new_claim = reading.observed_at < expires_at
    try:
        validate_held_execution_lock(held_lock)
        claim = state.claim_nonce(
            evidence.nonce,
            operation=expected_operation,
            execution_sha256=execution_sha256,
            authorization_sha256=evidence.authorization_sha256,
            scope_sha256=evidence.scope_sha256,
            trust_bundle_sha256=evidence.trust_bundle_sha256,
            allow_new_claim=allow_new_claim,
        )
        validate_held_execution_lock(held_lock)
    except ExecutionLockError as error:
        raise ExecutionAuthorityError("execution_authority_lock_not_held") from error
    except AuthorityClaimNotAllowedError as error:
        raise ExecutionAuthorityError("execution_authority_expired") from error
    return _permit_from_claim(claim, reading)
