from __future__ import annotations

"""OpenAI WebSocket transport boundary for realtime transcription."""

from typing import Any, Callable

from websockets.asyncio.client import connect

from seebx.adapters.openai_chat import safety_identifier_v1


OPENAI_REALTIME_SIDEBAND_URL = (
    "wss://api.openai.com/v1/realtime?call_id={call_id}"
)


def open_realtime_sideband_connection(
    *,
    call_id: str,
    api_key: str,
    owner_user_id: str,
    max_message_bytes: int,
    connect_factory: Callable[..., Any] = connect,
) -> Any:
    sideband_url = OPENAI_REALTIME_SIDEBAND_URL.format(
        call_id=call_id
    )
    return connect_factory(
        sideband_url,
        additional_headers={
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Safety-Identifier": safety_identifier_v1(
                owner_user_id
            ),
        },
        compression=None,
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
        max_size=max_message_bytes,
        max_queue=32,
    )


__all__ = [
    "open_realtime_sideband_connection",
]
