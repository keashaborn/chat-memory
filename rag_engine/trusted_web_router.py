"""Compatibility exports for the canonical SeeBx trusted-health search path."""

from seebx.capabilities.search.trusted_health import (
    NO_STORE_HEADERS,
    TrustedWebRequestV1,
    TrustedWebResponseV1,
    apply_trusted_web_no_store_headers,
    router,
    trusted_web_query,
)

__all__ = [
    "TrustedWebRequestV1",
    "TrustedWebResponseV1",
    "apply_trusted_web_no_store_headers",
    "router",
    "trusted_web_query",
]
