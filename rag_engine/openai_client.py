# rag_engine/openai_client.py

import os
from typing import List, Dict, Any, Tuple
from openai import OpenAI

# ----------------------------
# Provider routing (OpenAI-compatible)
# ----------------------------
# Model strings can be:
#   "gpt-5.1"                     -> defaults to provider "openai"
#   "xai:grok-2"                  -> provider "xai", model "grok-2"
#   "groq:llama-3.3-70b-versatile"-> provider "groq", model "llama-3.3-70b-versatile"
#
# Env vars:
#   OPENAI_API_KEY
#   XAI_API_KEY
#   GROQ_API_KEY
#   TOGETHER_API_KEY
#   FIREWORKS_API_KEY
#   OPENROUTER_API_KEY
#   PERPLEXITY_API_KEY
#   DEEPSEEK_API_KEY
#
# Optional base URL overrides (rarely needed):
#   OPENAI_BASE_URL, XAI_BASE_URL, GROQ_BASE_URL, TOGETHER_BASE_URL, FIREWORKS_BASE_URL,
#   OPENROUTER_BASE_URL, PERPLEXITY_BASE_URL, DEEPSEEK_BASE_URL
#
# Defaults:
_DEFAULT_BASE_URL = {
    "openai":    "https://api.openai.com/v1",
    "xai":       "https://api.x.ai/v1",
    "groq":      "https://api.groq.com/openai/v1",
    "together":  "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "openrouter":"https://openrouter.ai/api/v1",
    "perplexity":"https://api.perplexity.ai",
    "deepseek":  "https://api.deepseek.com",
}

_DEFAULT_KEY_ENV = {
    "openai":    "OPENAI_API_KEY",
    "xai":       "XAI_API_KEY",
    "groq":      "GROQ_API_KEY",
    "together":  "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "openrouter":"OPENROUTER_API_KEY",
    "perplexity":"PERPLEXITY_API_KEY",
    "deepseek":  "DEEPSEEK_API_KEY",
}

_client_cache: Dict[Tuple[str, str, str], OpenAI] = {}

def _split_model(model: str) -> Tuple[str, str]:
    """Return (provider, model_name). Default provider is 'openai'."""
    if model and ":" in model:
        prov, name = model.split(":", 1)
        prov = prov.strip().lower()
        name = name.strip()
        if prov and name:
            return prov, name
    return "openai", (model or "gpt-5.1")

def _get_client(provider: str) -> OpenAI:
    provider = (provider or "openai").strip().lower()

    base_env = f"{provider.upper()}_BASE_URL"
    key_env = _DEFAULT_KEY_ENV.get(provider, f"{provider.upper()}_API_KEY")

    base_url = os.getenv(base_env) or _DEFAULT_BASE_URL.get(provider)
    api_key = os.getenv(key_env)

    if not base_url:
        raise RuntimeError(f"Unknown provider '{provider}' and no {base_env} set")

    if not api_key:
        raise RuntimeError(f"Missing API key for provider '{provider}'. Set {key_env}.")

    cache_key = (provider, base_url, key_env)
    c = _client_cache.get(cache_key)
    if c is None:
        # OpenAI SDK supports OpenAI-compatible base_url + api_key routing.
        c = OpenAI(api_key=api_key, base_url=base_url)
        _client_cache[cache_key] = c
    return c

# ----------------------------
# Public helpers
# ----------------------------

def embed_text(text: str, model: str = "text-embedding-3-large") -> List[float]:
    """
    Embeddings are assumed to be OpenAI by default.
    If you want embeddings per-provider later, we can extend this the same way as chat.
    """
    provider, model_name = _split_model(model)
    c = _get_client(provider)
    r = c.embeddings.create(model=model_name, input=text)
    return r.data[0].embedding

def complete_chat_messages(
    messages: List[Dict[str, str]],
    model: str = "gpt-5.1",
    temperature: float = 0.4,
    top_p: float = 1.0
) -> str:
    """Run a chat completion with an explicit OpenAI messages[] list."""
    provider, model_name = _split_model(model)
    c = _get_client(provider)
    r = c.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature,
        top_p=top_p,
    )
    return r.choices[0].message.content

def complete_chat(system_prompt: str, user_message: str, model: str = "gpt-5.1") -> str:
    """Run a chat completion with a system prompt and user message."""
    provider, model_name = _split_model(model)
    c = _get_client(provider)
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
