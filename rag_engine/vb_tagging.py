from typing import List

def infer_vb_tags(text: str, source: str = "user") -> List[str]:
    """
    Lightweight verbal-behavior functional tagging.
    Returns flat tags like:
        vb_desire:...
        vb_ontology:...
        vb_stance:...
        vb_relation:...
        vb_fiction:...
    """
    t = (text or "").lower()
    tags = []

    # --- Desire / Mand-ish ---
    if any(w in t for w in ["can you", "could you", "please", "i want", "i need", "show me", "help me"]):
        tags.append("vb_desire:explicit_request")

    # --- Ontology / Tact-ish ---
    if any(w in t for w in ["pattern", "field", "vantage", "identity", "system", "constraint", "fractal"]):
        tags.append("vb_ontology:high_abstraction")
    elif any(w in t for w in ["thing", "stuff", "that one", "it is like"]):
        tags.append("vb_ontology:low_abstraction")

    # --- Stance / Autoclitic-ish ---
    if any(w in t for w in ["i think", "maybe", "sort of", "kinda", "possibly"]):
        tags.append("vb_stance:hedged")
    if any(w in t for w in ["clearly", "obviously", "definitely", "for sure"]):
        tags.append("vb_stance:high_certainty")

    # --- Relation / Intraverbal network ---
    if any(w in t for w in ["because", "so", "therefore", "thus"]):
        tags.append("vb_relation:causal")
    if any(w in t for w in ["but", "however", "yet"]):
        tags.append("vb_relation:contrast")

    # --- Fiction / Mentalism detector ---
    if any(w in t for w in ["lazy", "unmotivated", "wired this way", "i can't help", "that's just who i am"]):
        tags.append("vb_fiction:mentalistic_term")

    # ---- Filter tags based on source ----
    if source != "user":
        # Assistant should NOT have desires or mentalistic fictions
        tags = [
            t for t in tags
            if not t.startswith("vb_desire:") and not t.startswith("vb_fiction:")
        ]

    return tags
