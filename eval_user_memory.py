#!/usr/bin/env python3
"""
eval_user_memory.py

Per-user memory evaluation script for Brains.

Given a user_id, it:
  1) Fetches raw chat memories for that user from `memory_raw`
  2) Asks OpenAI to generate 1–3 durable Memory Cards
  3) Upserts those cards back into Qdrant using the agreed schema

Run inside the /opt/chat-memory venv on seebx:
  cd /opt/chat-memory
  source venv/bin/activate
  python3 eval_user_memory.py <user_id>
"""

import sys
import uuid
import os
import re  # at the top of the file if not already
from datetime import datetime
from typing import List

from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = "memory_raw"
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # adjust if needed

MAX_RAW_POINTS = 50  # how many raw memories to consider per user

def classify_card_kind(text: str) -> str:
    """
    Heuristic: decide whether a generated memory card is a general preference
    or a style_mode rule.

    Returns one of: "style_mode" or "preference".
    """
    t = (text or "").lower()

    # Strong hints it's a mode / command about formatting or style
    keywords_style = ["bullet", "bulleted", "outline", "skeleton", "paragraph", "prose", "format"]
    if any(k in t for k in keywords_style):
        return "style_mode"

    # More explicit “command-like” phrases
    if "when i say" in t or "when the user says" in t:
        return "style_mode"
    if "for this answer, i want" in t or "for this one, i want" in t:
        return "style_mode"

    # Fallback: general preference/fact
    return "preference"


def iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"

def fetch_raw_texts_for_user(qdrant: QdrantClient, user_id: str) -> List[str]:
    """
    Fetch up to MAX_RAW_POINTS raw chat texts for the given user_id from memory_raw.
    We treat source starting with 'frontend/chat' as raw episodic memory.
    """
    # Build filter: user_id == given user
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="user_id",
                match=qmodels.MatchValue(value=user_id),
            )
        ]
    )

    points, _next = qdrant.scroll(
        collection_name=COLLECTION,
        scroll_filter=flt,
        limit=MAX_RAW_POINTS,
        with_payload=True,
    )

    texts: List[str] = []
    for p in points:
        payload = p.payload or {}
        src = str(payload.get("source", ""))
        if src.startswith("frontend/chat"):
            text = str(payload.get("text", "")).strip()
            if text:
                texts.append(text)
    return texts

def assistant_identity_exists(qdrant: QdrantClient, user_id: str) -> bool:
  """
  Check if this user already has an assistant_identity card in memory_raw.
  """
  flt = qmodels.Filter(
      must=[
          qmodels.FieldCondition(
              key="user_id",
              match=qmodels.MatchValue(value=user_id),
          ),
          qmodels.FieldCondition(
              key="kind",
              match=qmodels.MatchValue(value="assistant_identity"),
          ),
      ]
  )

  points, _next = qdrant.scroll(
      collection_name=COLLECTION,
      scroll_filter=flt,
      limit=1,
      with_payload=False,
  )
  return len(points) > 0

def detect_assistant_name(texts: list[str]) -> str | None:
    """
    Look through raw texts for naming patterns like:
    - 'call you Eva'
    - 'your name is Mira'
    - 'I'll call you Sage'
    Returns the first name it finds, or None.
    """
    patterns = [
        r"\bcall you ([A-Z][a-zA-Z]+)\b",
        r"\byour name is ([A-Z][a-zA-Z]+)\b",
        r"\bi(?:'m)? going to call you ([A-Z][a-zA-Z]+)\b",
        r"\bcan i call you ([A-Z][a-zA-Z]+)\b",
        r"\bdo you mind if i call you ([A-Z][a-zA-Z]+)\b",
    ]

    for text in texts:
        lower = text.strip()
        for pat in patterns:
            m = re.search(pat, lower, flags=re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                # normalize capitalization
                return name[0].upper() + name[1:]
    return None

def build_assistant_identity_payload(user_id: str, ai_name: str, now: str) -> dict:
    """
    Create an assistant_identity card payload given a user id and chosen name.
    Text is simple; style/tone can evolve later via eval.
    """
    text = f"For this user, the assistant’s name is {ai_name}."
    return {
        "text": text,
        "user_id": user_id,
        "source": "memory_card",
        "tags": ["summary", "card", "assistant_identity"],
        "kind": "assistant_identity",
        "ai_name": ai_name,
        "ai_pronouns": None,
        "voice_preference": None,
        "role_hint": None,
        "created_at": now,
        "updated_at": now,
        "stability": 0.4,  # start moderate; will increase with evidence
        "evidence": 1,
        "feedback": {
            "positive_signals": 0,
            "negative_signals": 0,
            "last_feedback_at": None,
        },
    }

def generate_memory_cards(client: OpenAI, user_id: str, texts: List[str]) -> List[dict]:
    """
    Use OpenAI to generate 1–3 Memory Cards from the given raw texts.
    For now, we treat them as 'preference' or 'pattern' style summaries.
    """
    if not texts:
        return []

    joined = "\n---\n".join(texts)
    prompt = (
        "You are constructing durable memory cards about a specific user. "
        "Given the following chat snippets from this user and the assistant, "
        "extract 1–3 concise, useful memory statements that will help future conversations "
        "stay relevant to this user.\n\n"
        "Each memory card should be a single sentence.\n"
        "Focus on:\n"
        "- stable preferences, identity, and patterns\n"
        "- what they care about\n"
        "- how they want the system to behave.\n\n"
        "Return the memory cards as a numbered list.\n\n"
        f"CHAT SNIPPETS:\n{joined}\n"
    )

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You create durable memory cards for a single user."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )

    content = resp.choices[0].message.content or ""
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    cards: List[dict] = []

    for line in lines:
        # Strip leading numbering like "1." or "2)"
        cleaned = line.lstrip("0123456789). ").strip()
        if not cleaned:
            continue
        kind = classify_card_kind(cleaned)
        tags = ["summary", "card", kind]

        cards.append(
            {
                "text": cleaned,
                "user_id": user_id,
                "source": "memory_card",
                "tags": tags,
                "kind": kind,
                "base_importance": 0.75,
                "created_at": iso_now(),
                "updated_at": iso_now(),
            }
        )
    return cards

def upsert_cards(qdrant: QdrantClient, client: OpenAI, cards: List[dict]) -> None:
    if not cards:
        print("No cards to upsert.")
        return

    points = []
    for card in cards:
        rec_id = str(uuid.uuid4())
        text = card.get("text", "").strip()
        if not text:
            continue

        # Embed the card text so it matches the 3072-dim vector requirement
        emb = client.embeddings.create(model=EMBED_MODEL, input=text)
        vec = emb.data[0].embedding

        points.append(
            qmodels.PointStruct(
                id=rec_id,
                vector=vec,
                payload=card,
            )
        )

    if not points:
        print("No valid points to upsert.")
        return

    res = qdrant.upsert(collection_name=COLLECTION, points=points)
    print("Upsert:", res.status)


def upsert_card(qdrant: QdrantClient, client: OpenAI, card: dict) -> None:
    """
    Upsert a single card into Qdrant.
    Embeds the card's text and stores it in the same way as upsert_cards.
    """
    text = (card.get("text") or "").strip()
    if not text:
        print("Skipping card with empty text.")
        return

    # Embed the card text so the vector matches the 3072-dim expectation
    emb = client.embeddings.create(model=EMBED_MODEL, input=text)
    vec = emb.data[0].embedding

    rec_id = str(uuid.uuid4())
    point = qmodels.PointStruct(
        id=rec_id,
        vector=vec,
        payload=card,
    )

    print(f"Upserting single card id={rec_id}, kind={card.get('kind')}")
    res = qdrant.upsert(collection_name=COLLECTION, points=[point])
    print("  upsert status:", res.status)

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 eval_user_memory.py <user_id>")
        sys.exit(1)

    user_id = sys.argv[1]
    print(f"[eval_user_memory] Evaluating memory for user_id={user_id}")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment (inside /opt/chat-memory venv).")

    client = OpenAI(api_key=api_key)
    qdrant = QdrantClient(
        url=QDRANT_URL,
        timeout=60,
        prefer_grpc=False,
        https=False,
        check_compatibility=False,
    )

    texts = fetch_raw_texts_for_user(qdrant, user_id)
    print(f"Fetched {len(texts)} raw texts for this user.")
    if not texts:
        print("No raw memory for this user; nothing to do.")
        return

    # ---- NEW: auto-detect assistant identity name from raw texts ----
    now = iso_now()
    if not assistant_identity_exists(qdrant, user_id):
        ai_name = detect_assistant_name(texts)
        if ai_name:
            print(f"Detected assistant name for user {user_id}: {ai_name}")
            identity_payload = build_assistant_identity_payload(user_id, ai_name, now)
            upsert_card(qdrant, client, identity_payload)
        else:
            print("No assistant name detected for this user.")

    cards = generate_memory_cards(client, user_id, texts)
    print(f"Generated {len(cards)} candidate Memory Cards.")
    if not cards:
        print("Model produced no usable cards.")
        return

    upsert_cards(qdrant, client, cards)
    print("Done.")

if __name__ == "__main__":
    main()
