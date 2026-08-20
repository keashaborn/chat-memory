"""Shared OpenAI SDK adapter for SeeBx capabilities."""

import hashlib
import os
from typing import List, Dict, Any, Tuple
from openai import OpenAI

# ----------------------------
# OpenAI-only gateway
# ----------------------------
#
# This module intentionally supports OpenAI only.
#
# Historical provider-prefixed model strings such as:
#
#   xai:grok-*
#   groq:*
#   openrouter:*
#
# are normalized back to the default OpenAI chat model instead of being
# routed to another provider. This prevents stale cookies, old UI settings,
# or future env vars from reactivating non-OpenAI providers accidentally.

DEFAULT_CHAT_MODEL = (os.getenv("OPENAI_CHAT_MODEL") or "gpt-5.2").strip()

_ALLOWED_CHAT_MODELS = {
    "gpt-5.2",
    "gpt-5.1",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4o",
    "gpt-4o-mini",
}

_client_cache: Dict[Tuple[str, str], OpenAI] = {}


def normalize_chat_model(model: str | None, default: str | None = None) -> str:
    """
    Return an allowed OpenAI chat model.

    Any provider-prefixed model, unknown model, blank value, or stale Grok/xAI
    cookie is normalized to the default OpenAI model.
    """
    fallback = (default or DEFAULT_CHAT_MODEL or "gpt-5.2").strip()
    if fallback not in _ALLOWED_CHAT_MODELS:
        fallback = "gpt-5.2"

    raw = str(model or "").strip()
    if not raw:
        return fallback

    # OpenAI consolidation rule: provider prefixes are not accepted.
    if ":" in raw:
        return fallback

    if raw in _ALLOWED_CHAT_MODELS:
        return raw

    return fallback


def _split_model(model: str | None) -> Tuple[str, str]:
    """
    Compatibility helper for older callers.

    Provider is always openai. Model is normalized to the OpenAI allowlist.
    """
    return "openai", normalize_chat_model(model)


def _get_client(provider: str = "openai") -> OpenAI:
    provider = (provider or "openai").strip().lower()
    if provider != "openai":
        raise RuntimeError("Only OpenAI provider is enabled")

    base_url = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY")

    key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    cache_key = (base_url, key_fingerprint)
    c = _client_cache.get(cache_key)
    if c is None:
        c = OpenAI(api_key=api_key, base_url=base_url)
        _client_cache[cache_key] = c
    return c


def get_openai_client() -> OpenAI:
    """Return the process-wide OpenAI client without exposing credentials."""

    return _get_client("openai")


def get_optional_openai_client() -> OpenAI | None:
    """Return the shared client only when OpenAI credentials are configured."""

    if not (os.getenv("OPENAI_API_KEY") or "").strip():
        return None
    return get_openai_client()


# ----------------------------
# Public helpers
# ----------------------------

def embed_text(text: str, model: str = "text-embedding-3-large") -> List[float]:
    """
    Run an OpenAI embedding request.

    Embeddings are OpenAI-only. Provider-prefixed embedding model strings are
    not accepted; callers should pass a plain OpenAI embedding model name.
    """
    model_name = str(model or "text-embedding-3-large").strip()
    if ":" in model_name:
        model_name = "text-embedding-3-large"

    c = _get_client("openai")
    r = c.embeddings.create(model=model_name, input=text)
    return r.data[0].embedding


def complete_chat_messages(
    messages: List[Dict[str, str]],
    model: str = "gpt-5.2",
    temperature: float = 0.4,
    top_p: float = 1.0
) -> str:
    """Run an OpenAI chat completion with an explicit messages[] list."""
    model_name = normalize_chat_model(model)
    c = _get_client("openai")
    r = c.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature,
        top_p=top_p,
    )
    return r.choices[0].message.content


def complete_chat(system_prompt: str, user_message: str, model: str = "gpt-5.2") -> str:
    """Run an OpenAI chat completion with a system prompt and user message."""
    model_name = normalize_chat_model(model)
    c = _get_client("openai")
    r = c.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.4,
        top_p=1.0,
    )
    return r.choices[0].message.content
