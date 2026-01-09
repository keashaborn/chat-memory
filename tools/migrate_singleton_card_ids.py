import os, uuid, datetime, asyncio
from typing import List, Tuple

import asyncpg
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

ALIAS_USER_ID = (os.environ.get("USER_ID") or "").strip() or "anon"
VANTAGE_ID = (os.environ.get("VANTAGE_ID") or "default").strip() or "default"
POSTGRES_DSN = (os.environ.get("POSTGRES_DSN") or os.environ.get("DSN") or "").strip()
QDRANT_URL = (os.environ.get("QDRANT_URL") or "").strip()

TOPIC_KEY = (os.environ.get("TOPIC_KEY") or "__singleton__").strip() or "__singleton__"
KINDS = (os.environ.get("KINDS") or "style,user_identity,gravity_profile,vb_desire_profile").strip()
KINDS_LIST = [k.strip() for k in KINDS.split(",") if k.strip()]
LIMIT = int(os.environ.get("LIMIT") or "256")

DRY_RUN = (os.environ.get("DRY_RUN") or "").strip().lower() in {"1", "true", "yes"}

if not QDRANT_URL:
    raise SystemExit("ERROR: QDRANT_URL missing")

def utc_now_z() -> str:
    # timezone-aware UTC (avoid datetime.utcnow deprecation warnings)
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

async def resolve_canonical_user(vantage_id: str, alias_user_id: str) -> tuple[str, list[str]]:
    """
    Returns (canonical_user_id, alias_user_ids_for_canonical).
    If POSTGRES_DSN missing or lookup fails: treats alias as canonical.
    """
    if not POSTGRES_DSN:
        return alias_user_id, [alias_user_id]

    conn = await asyncpg.connect(POSTGRES_DSN)
    try:
        row = await conn.fetchrow(
            """
            SELECT canonical_user_id
            FROM vantage_identity.user_alias
            WHERE vantage_id=$1 AND alias_user_id=$2
            """,
            vantage_id, alias_user_id,
        )
        canon = str(row["canonical_user_id"]) if row and row["canonical_user_id"] else alias_user_id

        rows = await conn.fetch(
            """
            SELECT alias_user_id
            FROM vantage_identity.user_alias
            WHERE vantage_id=$1 AND canonical_user_id=$2
            """,
            vantage_id, canon,
        )
        aliases = sorted({str(r["alias_user_id"]) for r in rows if r and r["alias_user_id"]})
        if alias_user_id not in aliases:
            aliases.append(alias_user_id)
        return canon, aliases
    finally:
        await conn.close()

def keep_id(canon_user_id: str, kind: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{canon_user_id}|{kind}|{TOPIC_KEY}"))

def scroll_kind_user(c: QdrantClient, user_id: str, kind: str):
    flt = qmodels.Filter(must=[
        qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id)),
        qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind)),
    ])
    pts, _ = c.scroll(
        collection_name="memory_raw",
        scroll_filter=flt,
        limit=int(LIMIT),
        with_payload=True,
        with_vectors=True,
    )
    return pts or []

def vec_of(p):
    return getattr(p, "vector", None)

def main():
    now = utc_now_z()
    canon_user_id, alias_ids = asyncio.run(resolve_canonical_user(VANTAGE_ID, ALIAS_USER_ID))
    search_user_ids = sorted(set(alias_ids + [canon_user_id]))

    print("QDRANT_URL:", QDRANT_URL)
    print("vantage_id:", VANTAGE_ID)
    print("alias_user_id:", ALIAS_USER_ID)
    print("canonical_user_id:", canon_user_id)
    print("search_user_ids:", search_user_ids)
    print("topic_key:", TOPIC_KEY)
    print("now:", now)
    print("DRY_RUN:", DRY_RUN)
    print("limit:", LIMIT)

    c = QdrantClient(url=QDRANT_URL, check_compatibility=False)

    for kind in KINDS_LIST:
        pts = []
        for uid in search_user_ids:
            pts.extend(scroll_kind_user(c, uid, kind))

        # de-dupe by point id
        by_id = {str(p.id): p for p in pts}
        pts = list(by_id.values())
        ids = [str(p.id) for p in pts]

        kid = keep_id(canon_user_id, kind)

        print(f"\n== {kind} ==")
        print("found_count:", len(ids))
        print("keep_id:", kid)
        print("keep_present_before:", kid in set(ids))
        print("ids_before:", ids)

        if not pts:
            print("note: no points -> skip")
            continue

        # choose src: prefer keep_id, else prefer canonical payload, else first
        src = None
        for p in pts:
            if str(p.id) == kid:
                src = p
                break
        if src is None:
            for p in pts:
                pu = (p.payload or {}).get("user_id")
                if pu and str(pu) == canon_user_id:
                    src = p
                    break
        if src is None:
            src = pts[0]

        payload = dict(src.payload or {})
        v = vec_of(src)
        if not v:
            raise SystemExit(f"ERROR: missing vector for kind={kind} id={src.id}")

        orig_user_id = str(payload.get("user_id") or "")
        payload["user_id"] = canon_user_id
        payload["kind"] = kind
        payload["topic_key"] = TOPIC_KEY
        payload.setdefault("source", "memory_card")

        if orig_user_id and orig_user_id != canon_user_id:
            payload["user_id_alias"] = payload.get("user_id_alias") or orig_user_id

        created_at = payload.get("created_at") or now
        payload["created_at"] = created_at
        payload["updated_at"] = now

        legacy = [i for i in ids if i != kid]

        if DRY_RUN:
            print("DRY_RUN=1 -> would_upsert:", kid, "from_src:", str(src.id))
            print("DRY_RUN=1 -> would_delete_legacy_ids:", legacy)
            continue

        c.upsert(
            collection_name="memory_raw",
            points=[qmodels.PointStruct(id=kid, vector=v, payload=payload)],
        )

        if legacy:
            c.delete(
                collection_name="memory_raw",
                points_selector=qmodels.PointIdsList(points=legacy),
            )
            print("deleted_legacy_ids:", legacy)
        else:
            print("deleted_legacy_ids: []")

if __name__ == "__main__":
    main()
