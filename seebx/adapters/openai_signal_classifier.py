from __future__ import annotations

"""OpenAI transport for the provider-neutral conversation signal classifier."""

from typing import Any

from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel

from seebx.capabilities.conversation.signal_classifier import (
    DomainRiskProviderError,
    DomainRiskProviderUnavailableError,
)


_TRANSIENT_STATUS_CODES = frozenset({408, 409, 429})


class OpenAIDomainRiskClassificationProvider:
    """Execute one bounded OpenAI Structured Outputs classification call."""

    def __init__(self, client: Any) -> None:
        if client is None or not callable(getattr(client, "with_options", None)):
            raise ValueError("an OpenAI client with request options is required")
        self._client = client

    def classify(
        self,
        *,
        model: str,
        instructions: str,
        payload: str,
        text_format: type[BaseModel],
        max_output_tokens: int,
        safety_identifier: str,
        timeout_seconds: float,
    ) -> Any:
        try:
            client = self._client.with_options(
                max_retries=0,
                timeout=timeout_seconds,
            )
            return client.responses.parse(
                model=model,
                input=(
                    {"role": "developer", "content": instructions},
                    {"role": "user", "content": payload},
                ),
                text_format=text_format,
                max_output_tokens=max_output_tokens,
                store=False,
                safety_identifier=safety_identifier,
            )
        except (APIConnectionError, TimeoutError) as error:
            raise DomainRiskProviderUnavailableError(
                "domain_risk_provider_unavailable"
            ) from error
        except APIStatusError as error:
            status_code = getattr(error, "status_code", None)
            if status_code in _TRANSIENT_STATUS_CODES or (
                isinstance(status_code, int) and 500 <= status_code <= 599
            ):
                raise DomainRiskProviderUnavailableError(
                    "domain_risk_provider_unavailable"
                ) from error
            raise DomainRiskProviderError("domain_risk_provider_failed") from error
        except Exception as error:
            raise DomainRiskProviderError("domain_risk_provider_failed") from error


__all__ = ["OpenAIDomainRiskClassificationProvider"]
