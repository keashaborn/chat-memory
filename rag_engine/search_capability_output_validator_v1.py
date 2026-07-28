from __future__ import annotations

"""Deterministic scope checks for answers that receive a search manifest."""

import re

from rag_engine.search_capability_manifest_v1 import SearchCapabilityManifestV1


SEARCH_CAPABILITY_OUTPUT_VALIDATOR_VERSION = (
    "search_capability_output_validator_v1"
)

_GLOBAL_DENIAL_PATTERNS = (
    re.compile(
        r"""\b(?:i|we)\s+(?:do\s+not|don't|cannot|can't)\s+"""
        r"""(?:have\s+)?(?:access(?:\s+to)?|browse|search|check|use|consult)\s+"""
        r"""(?:the\s+)?(?:web|internet|news|current\s+news|external\s+"""
        r"""sources?|sources?)\b""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""\b(?:i|we)\s+(?:do\s+not|don't)\s+have\s+(?:the\s+)?"""
        r"""(?:ability|capability)\s+to\s+(?:access|browse|search|check|use|"""
        r"""consult)\s+(?:the\s+)?(?:web|internet|news|current\s+news|"""
        r"""external\s+sources?|sources?)\b""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""\b(?:i\s+am|we\s+are|i'm|we're)\s+unable\s+to\s+"""
        r"""(?:access|browse|search|check|use|consult)\s+(?:the\s+)?"""
        r"""(?:web|internet|news|current\s+news|external\s+sources?|sources?)"""
        r"""\b""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""\b(?:this\s+(?:chat|system|service)|the\s+(?:chat|system|service))"""
        r"""\s+(?:does\s+not|doesn't|cannot|can't)\s+(?:have\s+)?"""
        r"""(?:access(?:\s+to)?|browse|search|check|use|consult)\s+(?:the\s+)?"""
        r"""(?:web|internet|news|current\s+news|external\s+sources?|sources?)\b""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""\b(?:i|we|this\s+(?:chat|system|service)|the\s+"""
        r"""(?:chat|system|service))\s+(?:have|has)\s+no\s+(?:web|internet|"""
        r"""news|current\s+news|external\s+source|source)\s+access\b""",
        re.IGNORECASE,
    ),
)

_OVERBROAD_CAPABILITY_PATTERNS = (
    re.compile(
        r"""\bother\s+supported\s+(?:fact[-\s]?checking|source[-\s]?"""
        r"""verification)\s+(?:or\s+(?:fact[-\s]?checking|source[-\s]?"""
        r"""verification)\s+)?tasks?\b""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""\b(?:i|we|this\s+(?:chat|system|service)|the\s+"""
        r"""(?:chat|system|service))\s+(?:can|may|will)\s+(?:browse|access|"""
        r"""search|research|look\s+up)\s+(?:the\s+web\s+for\s+)?"""
        r"""(?:anything|any\s+(?:topic|subject|website|webpage|page|site))\b""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""\b(?:offers?|provides?|supports?|has)\s+unrestricted\s+"""
        r"""(?:web\s+)?(?:browsing|search|research|internet\s+access)\b""",
        re.IGNORECASE,
    ),
)


class SearchCapabilityOutputValidationError(RuntimeError):
    pass


def _normalize(value: str) -> str:
    return (
        str(value or "")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
    )


def validate_search_capability_output_v1(
    answer: str,
    manifest: SearchCapabilityManifestV1 | None,
) -> None:
    """Reject global denials and capabilities broader than the signed manifest."""

    if manifest is None:
        return
    SearchCapabilityManifestV1.model_validate_json(manifest.model_dump_json())
    normalized = _normalize(answer)
    if any(pattern.search(normalized) for pattern in _GLOBAL_DENIAL_PATTERNS):
        raise SearchCapabilityOutputValidationError(
            "search_capability_answer_denies_authorized_access"
        )
    if any(
        pattern.search(normalized)
        for pattern in _OVERBROAD_CAPABILITY_PATTERNS
    ):
        raise SearchCapabilityOutputValidationError(
            "search_capability_answer_exceeds_authorized_scope"
        )


__all__ = [
    "SEARCH_CAPABILITY_OUTPUT_VALIDATOR_VERSION",
    "SearchCapabilityOutputValidationError",
    "validate_search_capability_output_v1",
]
