#!/usr/bin/env python3
"""
create_test_memory_card.py

Tiny helper script to create a single Memory Card for a given user_id
in the Qdrant `memory_raw` collection, using the agreed schema.

Run on seebx (Brains) with OPENAI_API_KEY set.
"""

import uuid
import os
from datetime import datetime

from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

# ---- CONFIG ----
QDRANT_URL = "http://127.0.0.1:6333"
COLLECTION = "memory_raw"
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")

# TODO: replace this with whichever user you want to create a card for
USER_ID = "dcb8fb63-6613-4a09-8db2-7a879dc90146"  # Lucifer's Supabase user_id

# The content of the test Memory Card
CARD_TEXT = (
    "User prefers deep, recursive explanations and wants memory injections to "
    "be shaped by explicit feedback about relevance."
)

CARD_KIND = "preference"       # one of: identity, preference, goal, boundary, pattern, event, fact
BASE_IMPORTANCE = 0.85         # 0.0 – 1.0

def iso_now() -> str:
    """UTC ISO8601 with Z suffix."""
    return datetime.utcnow().isoformat() + "Z"

def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment on seebx.")

    client = OpenAI(api_key=api_key)

    # 1) Embed the card text
    print(f"Embedding card text with model={EMBED_MODEL}...")
    emb = client.embeddings.create(model=EMBED_MODEL, input=CARD_TEXT)
    vec = emb.data[0].embedding

    # 2) Prepare Qdrant point payload
    rec_id = str(uuid.uuid4())
    now = iso_now()

    payload = {
        "text": CARD_TEXT,
        "user_id": USER_ID,
        "source": "memory_card",
        "tags": ["summary", "card", CARD_KIND],
        "kind": CARD_KIND,
        "base_importance": BASE_IMPORTANCE,
        "created_at": now,
        "updated_at": now,
        # optional fields we can add later:
        # "themes": ["memory", "interaction_style"],
        # "emotional_tone": "engaged",
        # "timescale": "long_term",
        # "source_raw_ids": [],
        # "feedback": {"upvotes": 0, "downvotes": 0},
        # "eval_meta": {...}
    }

    point = qmodels.PointStruct(id=rec_id, vector=vec, payload=payload)

    # 3) Upsert into Qdrant
    print(f"Upserting Memory Card into collection={COLLECTION} with id={rec_id}...")
    qdrant = QdrantClient(
        url=QDRANT_URL,
        timeout=60,
        prefer_grpc=False,
        https=False,
        check_compatibility=False,
    )
    res = qdrant.upsert(collection_name=COLLECTION, points=[point])
    print("Upsert result:", res.status)

    print("Done. You can now retrieve this card via /retrieve_memory for this user.")

if __name__ == "__main__":
    main()
