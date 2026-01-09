import os, uuid, datetime, asyncio
import asyncpg

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

VANTAGE_ID = (os.environ.get("VANTAGE_ID") or "default").strip() or "default"
ALIAS_USER_ID = (os.environ.get("USER_ID") or "").strip()
POSTGRES_DSN = (os.environ.get("POSTGRES_DSN") or "").strip()
QDRANT_URL = (os.environ.get("QDRANT_URL") or "").strip()

DRY_RUN = (os.environ.get("DRY_RUN") or "").strip().lower() in {"1", "true", "yes"}
CANONICALIZE = (os.environ.get("CANONICALIZE_USER_ID") or "1").strip().lower() not in {"0", "false", "no"}

if not ALIAS_USER_ID:
    raise SystemExit("ERROR: USER_ID missing")
if not QDRANT_URL:
    raise SystemExit("ERROR: QDRANT_URL missing")
if CANONICALIZE and not POSTGRES_DSN:
    raise SystemExit(
        "ERROR: POSTGRES_DSN missing (needed to canonicalize USER_ID). "
        "Set CANONICALIZE_USER_ID=0 only if USER_ID is already canonical."
    )

TOPIC_KEY = "__singleton__"
KINDS = ["style", "user_identity", "gravity_profile", "vb_desire_profile"]

async def resolve_canonical_user_id(vantage_id: str, alias_user_id: str) -> tuple[str, str]:
    alias = (alias_user_id or "").strip() or "anon"
    if not CANONICALIZE:
        return alias, alias

    conn = await asyncpg.connect(POSTGRES_DSN)
    try:
        row = await conn.fetchrow(
            "select canonical_user_id "
            "from vantage_identity.user_alias "
            "where vantage_id=$1 and alias_user_id=$2",
            vantage_id,
            alias,
        )
    finally:
        await conn.close()

    if row and row["canonical_user_id"]:
        return str(row["canonical_user_id"]), alias
    return alias, alias

CANON_USER_ID, ALIAS_USER_ID = asyncio.run(resolve_canonical_user_id(VANTAGE_ID, ALIAS_USER_ID))

c = QdrantClient(url=QDRANT_URL, check_compatibility=False)

def keep_id(kind: str) -> str:
    # NEW contract: deterministic ids must be based on canonical user_id
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{CANON_USER_ID}|{kind}|{TOPIC_KEY}"))

def scroll(kind: str, limit: int = 256):
    # include both alias + canonical so we can migrate old per-alias points into canonical
    should = [
        qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=ALIAS_USER_ID)),
    ]
    if CANON_USER_ID != ALIAS_USER_ID:
        should.append(qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=CANON_USER_ID)))

    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind)),
        ],
        should=should,
    )
    pts, _ = c.scroll(
        collection_name="memory_raw",
        scroll_filter=flt,
        limit=limit,
        with_payload=True,
        with_vectors=True,
    )
    return pts or []

def vec_of(p):
    return getattr(p, "vector", None)

now = datetime.datetime.utcnow().isoformat() + "Z"
print("QDRANT_URL:", QDRANT_URL)
print("vantage_id:", VANTAGE_ID)
print("alias_user_id:", ALIAS_USER_ID)
print("canonical_user_id:", CANON_USER_ID)
print("topic_key:", TOPIC_KEY)
print("now:", now)
print("DRY_RUN:", DRY_RUN)

for kind in KINDS:
    pts = scroll(kind)
    ids = [str(p.id) for p in pts]
    kid = keep_id(kind)

    print(f"\n== {kind} ==")
    print("found_count:", len(ids))
    print("keep_id:", kid)
    print("keep_present_before:", kid in set(ids))
    print("ids_before:", ids)

    if not pts:
        print("note: no points -> skip")
        continue

    # Prefer existing keep_id if already present, else use first legacy point as source.
    src = None
    for p in pts:
        if str(p.id) == kid:
            src = p
            break
    if src is None:
        src = pts[0]

    payload = dict(src.payload or {})
    v = vec_of(src)
    if not v:
        raise SystemExit(f"ERROR: missing vector for kind={kind} id={src.id}")

    payload["user_id"] = CANON_USER_ID
    payload["user_id_alias"] = ALIAS_USER_ID
    payload["kind"] = kind
    payload["topic_key"] = TOPIC_KEY

    created_at = payload.get("created_at") or now
    payload["created_at"] = created_at
    payload["updated_at"] = now

    if DRY_RUN:
        print("DRY_RUN=1 -> skipping upsert/delete")
        continue

    c.upsert(
        collection_name="memory_raw",
        points=[qmodels.PointStruct(id=kid, vector=v, payload=payload)],
    )

    legacy = [i for i in ids if i != kid]
    if legacy:
        c.delete(
            collection_name="memory_raw",
            points_selector=qmodels.PointIdsList(points=legacy),
        )
        print("deleted_legacy_ids:", legacy)
    else:
        print("deleted_legacy_ids: []")

    pts2 = scroll(kind)
    ids2 = [str(p.id) for p in pts2]
    topic_keys2 = sorted({((p.payload or {}).get("topic_key") or "") for p in pts2})
    user_ids2 = sorted({((p.payload or {}).get("user_id") or "") for p in pts2})
    print("after_count:", len(ids2))
    print("keep_present_after:", kid in set(ids2))
    print("topic_keys_after:", topic_keys2)
    print("user_ids_after:", user_ids2)
    print("ids_after:", ids2)
