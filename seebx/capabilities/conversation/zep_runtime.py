from __future__ import annotations

"""Shared Zep memory runtime and server-owned prompt activation settings."""

import logging
import os

from rag_engine.zep_memory_provider_v1 import ZepPromptSettingsV1
from rag_engine.zep_shadow_memory_v1 import ZepShadowRuntimeV1


logger = logging.getLogger("uvicorn.error")

ZEP_MEMORY_RUNTIME = ZepShadowRuntimeV1.from_environment(
    os.environ,
    logger=logger,
)
ZEP_PROMPT_SETTINGS = ZepPromptSettingsV1.from_environment(os.environ)


__all__ = ["ZEP_MEMORY_RUNTIME", "ZEP_PROMPT_SETTINGS"]
