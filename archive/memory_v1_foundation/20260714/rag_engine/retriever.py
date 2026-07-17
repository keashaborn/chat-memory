# rag_engine/retriever.py

import httpx
from typing import List, Dict, Any

from .openai_client import embed_text

QDRANT_URL = "http://127.0.0.1:6333"
COLLECTIONS = [
    "memory_raw",
    "fm_context_v1",
    "supporting_principles",
    "foundational_core_RESSE_v4",
    "nta_secondary_core_ai_v1",
    "nta_secondary_core_metaphy_v1",
    "nta_secondary_core_science_v1",
    "hv_behavioral_v1",
    "core_axioms_v2",
    "nta_secondary_core_applied_v1",
    "nta_primary_core_v1",
    "fm_principles_index_v1",
    "nta_ref_misc_v1",
    "hv_philosophical_v1",
    "nta_ref_psy_v1",
    "nta_secondary_core_psych_v1",
    "external_context_v1",
    "nta_ref_phi_v1",
    "fm_canon_v1",
    "gfmc_cc_v5"
]


def search_qdrant(collection: str, vector: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Synchronous vector search against a Qdrant collection.
    """
    payload = {
        "vector": vector,
        "limit": top_k,
        "with_payload": True,
        "with_vectors": False
    }

    # Synchronous HTTP client
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json=payload
        )
        response.raise_for_status()
        return response.json().get("result", [])


def retrieve_relevant_memory(message: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Retrieve relevant memory across all configured collections.
    """
    # 1. Embed user message
    vector = embed_text(message)

    results: List[Dict[str, Any]] = []

    # 2. Search each collection
    for col in COLLECTIONS:
        try:
            col_results = search_qdrant(col, vector, top_k=top_k)
            for item in col_results:
                payload = item.get("payload", {})
                score = item.get("score", 0)
                results.append({
                    "collection": col,
                    "payload": payload,
                    "score": score
                })
        except Exception as e:
            print(f"[WARN] Search failed for {col}: {e}")

    # 3. Sort by score
    results.sort(key=lambda x: x["score"], reverse=True)

    # 4. Return top-k
    return results[:top_k]
