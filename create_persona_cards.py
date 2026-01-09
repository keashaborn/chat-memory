#!/usr/bin/env python3
"""
create_persona_cards.py

Create initial persona cards for a given user_id:
- Assistant Identity card (kind: assistant_identity)
- One Style card (kind: style)

Run inside /opt/chat-memory venv on seebx:

  cd /opt/chat-memory
  source venv/bin/activate
  python3 create_persona_cards.py <user_id>

"""

import sys
import uuid
import os
from datetime import datetime
from typing import List

from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = "memory_raw"
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")

def iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"

def embed_text(client: OpenAI, text: str) -> List[float]:
    emb = client.embeddings.create(model=EMBED_MODEL, input=text)
    return emb.data[0].embedding

def upsert_card(qdrant: QdrantClient, client: OpenAI, card_payload: dict) -> None:
    text = card_payload.get("text", "").strip()
    if not text:
        print("Skipping card with empty text.")
        return

    vec = embed_text(client, text)
    rec_id = str(uuid.uuid4())
    point = qmodels.PointStruct(id=rec_id, vector=vec, payload=card_payload)
    print(f"Upserting card id={rec_id}, kind={card_payload.get('kind')}")
    res = qdrant.upsert(collection_name=COLLECTION, points=[point])
    print("  upsert status:", res.status)

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 create_persona_cards.py <user_id>")
        sys.exit(1)

    user_id = sys.argv[1].strip()
    if not user_id:
        print("Empty user_id; aborting.")
        sys.exit(1)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment.")

    client = OpenAI(api_key=api_key)
    qdrant = QdrantClient(
        url=QDRANT_URL,
        timeout=60,
        prefer_grpc=False,
        https=False,
        check_compatibility=False,
    )

    now = iso_now()

    # ---- Assistant Identity card (kind: assistant_identity) ----
    assistant_identity = {
        "text": "For this user, the assistant’s name is RESSE and she speaks in a warm, behaviorally precise tone.",
        "user_id": user_id,
        "source": "memory_card",
        "tags": ["summary", "card", "assistant_identity"],
        "kind": "assistant_identity",

        "ai_name": "RESSE",
        "ai_pronouns": "she/her",
        "voice_preference": "female_1",
        "role_hint": "lab_partner",

        "created_at": now,
        "updated_at": now,

        "stability": 0.85,
        "evidence": 1,
        "feedback": {
            "positive_signals": 0,
            "negative_signals": 0,
            "last_feedback_at": None
        }
    }

    # ---- Style card (kind: style) ----
    style_card = {
        "text": (
            "When speaking to this user, use deep, recursive explanations grounded in Fractal Monism "
            "and behaviorally precise language. Avoid vague metaphors unless they are explicitly cashed "
            "out in terms of contingencies and vantage shifts. Prefer concise answers with optional deeper "
            "expansions when requested."
        ),
        "user_id": user_id,
        "source": "memory_card",
        "tags": ["summary", "card", "style"],
        "kind": "style",

        "base_importance": 0.9,
        "created_at": now,
        "updated_at": now,

        "stability": 0.5,
        "evidence": 5,
        "themes": ["depth", "fractal_monism", "behavioral_precision"],
        "emotional_tone": "engaged",
        "timescale": "long_term",
        "source_raw_ids": [],

        "feedback": {
            "positive_signals": 0,
            "negative_signals": 0,
            "last_feedback_at": None
        },
        "eval_meta": {
            "salience": 0.9,
            "stability": 0.5,
            "source_eval_run": "manual-seed"
        }
    }

    print(f"[persona] Creating assistant_identity + style cards for user_id={user_id}")
    upsert_card(qdrant, client, assistant_identity)
    upsert_card(qdrant, client, style_card)
    print("[persona] Done.")

if __name__ == "__main__":
    main()
