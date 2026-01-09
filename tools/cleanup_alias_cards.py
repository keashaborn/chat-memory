import os
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

Q = os.environ["QDRANT_URL"].rstrip("/")
ALIAS = os.environ["ALIAS"].strip()
DO_DELETE = os.environ.get("DO_DELETE") == "1"

KINDS = ["style", "gravity_profile"]

c = QdrantClient(url=Q, check_compatibility=False)

def list_ids(kind: str):
    ids = []
    offset = None
    flt = qmodels.Filter(must=[
        qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=ALIAS)),
        qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind)),
    ])
    while True:
        pts, offset = c.scroll(
            collection_name="memory_raw",
            scroll_filter=flt,
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.extend([str(p.id) for p in (pts or [])])
        if offset is None:
            break
    return ids

def delete_kind(kind: str):
    flt = qmodels.Filter(must=[
        qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=ALIAS)),
        qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind)),
    ])
    c.delete(
        collection_name="memory_raw",
        points_selector=qmodels.FilterSelector(filter=flt),
    )

print("QDRANT_URL:", Q)
print("ALIAS:", ALIAS)
print("DO_DELETE:", DO_DELETE)

for kind in KINDS:
    ids = list_ids(kind)
    print(f"\n== {kind} ==")
    print("count:", len(ids))
    print("ids_sample:", ids[:10])

if DO_DELETE:
    for kind in KINDS:
        ids = list_ids(kind)
        if ids:
            delete_kind(kind)

for kind in KINDS:
    ids2 = list_ids(kind)
    print(f"\n== AFTER {kind} ==")
    print("count:", len(ids2))
    print("ids_sample:", ids2[:10])
