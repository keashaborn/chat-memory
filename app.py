from typing import Any, Dict, List, Optional
import os, time, uuid, hashlib, asyncpg, json
import asyncio
import websockets
import socket
from datetime import datetime
from fastapi import FastAPI, Body, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.openapi.utils import get_openapi
from qdrant_client import QdrantClient
from rag_engine.qdrant_compat import make_qdrant_client
from qdrant_client.http import models as qmodels
from openai import OpenAI
from pydantic import BaseModel
from rag_engine.rag_router import router as rag_router
from rag_engine.vantage_router import router as vantage_router
from rag_engine.forms_router import router as forms_router
from rag_engine.telemetry_router import router as telemetry_router
from rag_engine.lifeswitch_meals_router import router as lifeswitch_meals_router
from rag_engine.lifeswitch_nutrition_log_router import router as lifeswitch_nutrition_log_router
from rag_engine.lifeswitch_nutrition_router import router as lifeswitch_nutrition_router
from rag_engine.lifeswitch_training_router import router as lifeswitch_training_router
from rag_engine.catalog_router import router as catalog_router
from rag_engine.vb_tagging import infer_vb_tags
from rag_engine.gravity import compute_gravity, write_gravity_card
from rag_engine.vb_desire_profile import build_vb_desire_profile, write_vb_desire_profile_card
class NewThreadReq(BaseModel):
    user_id: str
    title: Optional[str] = None
    vantage_id: Optional[str] = "default"

app = FastAPI(title="Brains API", version="1.0.0")
app.include_router(rag_router, prefix="/rag")
app.include_router(vantage_router, prefix="/vantage")
app.include_router(forms_router, prefix="/forms")
app.include_router(telemetry_router)
app.include_router(lifeswitch_nutrition_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_meals_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_nutrition_log_router, prefix="/lifeswitch/nutrition")
app.include_router(catalog_router, prefix="/catalog")
app.include_router(lifeswitch_training_router, prefix="/lifeswitch/training")

# ---------- request correlation ----------
def _sanitize_request_id(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Prevent header/log abuse
    if len(s) > 128:
        return None
    return s

def _get_request_id(req: Request) -> str:
    rid = _sanitize_request_id(req.headers.get("x-request-id")) or _sanitize_request_id(
        req.headers.get("x-correlation-id")
    )
    return rid or str(uuid.uuid4())

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = _get_request_id(request)
    request.state.request_id = rid
    response = await call_next(request)
    # Echo for end-to-end correlation
    try:
        response.headers["x-request-id"] = rid
    except Exception:
        pass
    return response


def parse_uuid(s: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(s))
    except Exception:
        return None


# single global qdrant client
qdrant_client = None

DSN = os.getenv("POSTGRES_DSN", "postgres://sage:strongpassword@localhost:5432/memory")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# --- defaults from environment ---
DEFAULT_COLLECTION = os.environ.get("RETRIEVAL_COLLECTION", "fm_canon_v1")
EMBED_MODEL       = os.environ.get("EMBED_MODEL", "text-embedding-3-large")
QDRANT_URL        = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")


async def db():
    return await asyncpg.connect(DSN)

def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()[:16]

@app.get("/openapi.json", include_in_schema=False)
async def openapi_json():
    return get_openapi(title="Brains API", version="1.0.0", routes=app.routes)

@app.post("/tts")
async def tts(req: Request):
    """
    Text-to-speech proxy (keep OPENAI_API_KEY on Brains only).
    """
    if not OPENAI_API_KEY:
        return Response("Server missing OPENAI_API_KEY", status_code=500, media_type="text/plain")

    try:
        body = await req.json()
    except Exception:
        body = {}

    text = str((body or {}).get("text") or "").strip()
    if not text:
        return Response("Missing text", status_code=400, media_type="text/plain")

    voice = str((body or {}).get("voice") or "sage").strip()
    model = str((body or {}).get("model") or "gpt-4o-mini-tts").strip()
    instructions = str((body or {}).get("instructions") or "").strip()

    try:
        speed = float((body or {}).get("speed", 1.0))
    except Exception:
        speed = 1.0
    speed = max(0.25, min(4.0, speed))

    payload = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
        "speed": speed,
    }
    if instructions and model == "gpt-4o-mini-tts":
        payload["instructions"] = instructions

    import requests

    def _do():
        return requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )

    r = await asyncio.to_thread(_do)

    if not r.ok:
        return Response(
            f"TTS upstream error: HTTP {r.status_code}\n{r.text}",
            status_code=502,
            media_type="text/plain",
        )

    return Response(content=r.content, status_code=200, media_type="audio/mpeg")


async def get_seconds_since_last_user_message(user_id: str) -> Optional[float]:
    """
    Returns seconds since the most recent chat_log row for this user_id.
    Uses Postgres timestamps (chat_log.created_at).
    """
    try:
        conn = await asyncpg.connect(DSN)
        row = await conn.fetchrow(
            "SELECT created_at FROM chat_log WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1",
            user_id
        )
        await conn.close()
    except Exception as e:
        print("[temporal] pg lookup error:", e)
        return None

    if not row:
        return None

    last_ts = row["created_at"]
    if not last_ts:
        return None

    # last_ts is a datetime with tz info; compare to now in UTC
    now = datetime.utcnow().replace(tzinfo=last_ts.tzinfo)
    delta = (now - last_ts).total_seconds()
    return float(delta)

def bucket_time_gap(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 300:
        return "very_recent"
    if seconds < 3600:
        return "recent"
    if seconds < 86400:
        return "same_day"
    if seconds < 7 * 86400:
        return "days_gap"
    return "long_gap"

# --- Qdrant lazy client (prevents NameError after restarts) ---
def get_qdrant():
    """Return a singleton QdrantClient, creating it on first use."""
    global qdrant_client
    if qdrant_client is None:
        try:
            qdrant_client = make_qdrant_client(
                url=QDRANT_URL,
                timeout=60,
                prefer_grpc=False,
                https=False,
            )
        except TypeError:
            qdrant_client = make_qdrant_client(
                url=QDRANT_URL,
                timeout=60,
                prefer_grpc=False,
                https=False,
            )
    return qdrant_client

def current_default_collection() -> str:
    """
    Use the runtime env value if present; otherwise fall back to the
    import-time DEFAULT_COLLECTION. This lets us switch datasets via env
    or alias without redeploying code.
    """
    return (os.getenv("RETRIEVAL_COLLECTION") or DEFAULT_COLLECTION).strip()

# Collections that should NEVER be used as knowledge corpus
IGNORED_COLLECTIONS = {"memory_raw"}


def get_corpus_collections() -> List[str]:
    """
    Return all Qdrant collections that are valid knowledge sources.
    Currently: everything except memory_raw.
    """
    cols_resp = get_qdrant().get_collections()
    collections = getattr(cols_resp, "collections", [])

    names: List[str] = []
    for c in collections:
        # qdrant_client >=1.7 usually gives objects with .name
        name = getattr(c, "name", None)
        if not name:
            continue
        if name in IGNORED_COLLECTIONS:
            continue
        names.append(name)

    return names

def _vb_source_normalize(source: Optional[str]) -> str:
    """
    Normalize upstream 'source' strings to the canonical labels expected by vb_tagging.py.
    Only used when VB_TAG_SOURCE_NORMALIZE=1.
    """
    s = (source or "").lower()
    if s == "user" or s.endswith(":user") or "chat:user" in s:
        return "user"
    if s == "assistant" or s.endswith(":assistant") or "chat:assistant" in s:
        return "assistant"
    return s or "unknown"


def infer_extra_tags(text: str, source: str = "frontend") -> List[str]:
    """
    Very simple heuristic tagging for memory entries.
    We can refine this later or replace with an OpenAI classifier.
    """
    t = (text or "").lower()
    extra: List[str] = []

    # ---------- Formatting intent ----------
    if "bullet" in t or "bulleted" in t or "outline" in t or "skeleton" in t:
        extra.append("format:skeleton")
    if "paragraph" in t or "prose" in t or "narrative" in t or "story" in t:
        extra.append("format:prose")

    # ---------- Meta / design / testing language ----------
    if (
        "testing memory" in t
        or "see how memory" in t
        or ("shape" in t and "behavior" in t)
    ):
        extra.append("tone:meta")
    if "design" in t and "rag" in t:
        extra.append("tone:design")

    # ---------- Topic hints (rough) ----------
    # workout / lifting / gym
    if any(w in t for w in [
        "hammer strength", "hammer plate", "hammer equipment",
        "workout", "lift weights", "lifting weights", "gym routine"
    ]):
        extra.append("topic:workout")

    # fractal monism / FM metaphysics
    if any(w in t for w in [
        "fractal monism", "fm axioms", "fm_", "monistic field",
        "undivided field", "differentiation", "lucifer", "self-deception"
    ]):
        extra.append("topic:fm")

    # human vantage / HV axioms
    if any(w in t for w in [
        "human vantage", "hv axioms", "hv-", "identity is enacted",
        "agency lives in the next act"
    ]):
        extra.append("topic:hv")

    # ---------- Intent tags ----------
    # Explain / why / what is
    if any(w in t for w in [
        "explain", "what is", "why is", "how does", "could you describe"
    ]):
        extra.append("intent:explain")

    # Instruct / how-to / steps
    if any(w in t for w in [
        "how do i", "show me how", "step by step", "steps", "instructions"
    ]):
        extra.append("intent:instruct")

    # Summarize / compress
    if "summary" in t or "summarize" in t or "short version" in t:
        extra.append("intent:summarize")

    # Analyze
    if "analyze" in t or "analysis" in t or "break down" in t:
        extra.append("intent:analyze")

    # Compare / contrast
    if "compare" in t or "difference between" in t or "vs." in t:
        extra.append("intent:compare")

    # Reflective / psychological introspection
    if any(w in t for w in [
        "i feel", "why do i", "help me understand", "reflect on",
        "what does it mean for me", "in my life"
    ]):
        extra.append("intent:reflect")

    # Generate / create content
    if any(w in t for w in [
        "write", "create", "make a", "generate", "draft", "compose"
    ]):
        extra.append("intent:generate")

    # Rewrite / edit
    if "rewrite" in t or "edit this" in t or "make this better" in t:
        extra.append("intent:rewrite")

    # Evaluate / critique / opinion
    if any(w in t for w in [
        "evaluate", "critique", "what do you think of", "rate this"
    ]):
        extra.append("intent:evaluate")

    # ---- VB TAGGING ----
    # ---- VB TAGGING ----
    vb_source = source
    if os.getenv("VB_TAG_SOURCE_NORMALIZE", "0") == "1":
        vb_source = _vb_source_normalize(source)

    vb_tags = infer_vb_tags(text, source=vb_source)
    for t in vb_tags:
        extra.append(t)

    return extra



# ---------- persistent chat memory ----------
@app.post("/log")
async def log_chat(req: Request):
    try:
        body: Dict[str, Any] = await req.json()
    except Exception:
        return JSONResponse({"status":"bad_request","detail":"invalid json"}, status_code=400)

    text = body.get("text") or body.get("input") or ""
    user_id_alias = (body.get("user_id") or "anon")
    user_id_alias = (user_id_alias or "").strip() or "anon"
    source = body.get("source") or "frontend"
    tags = body.get("tags") or []
    vantage_id = (body.get("vantage_id") or "").strip() or "default"
    request_id = _sanitize_request_id(getattr(req.state, "request_id", None)) or str(uuid.uuid4())

    # Canonicalize user_id (alias -> canonical) for ALL writes
    user_id, _alias_uid = await resolve_canonical_user_id(vantage_id, user_id_alias)


    # Optional thread id for "real threads"
    thread_id = None
    raw_thread_id = body.get("thread_id")
    if raw_thread_id:
        try:
            thread_id = uuid.UUID(str(raw_thread_id))
        except Exception:
            thread_id = None

    if not text.strip():
        return {"status":"empty","detail":"no text"}

    # Heuristic extra tags based on content
    extra_tags = infer_extra_tags(text, source=source)
    if extra_tags:

        existing = set(str(x) for x in tags)
        for t in extra_tags:
            tt = str(t)
            if tt not in existing:
                tags.append(tt)
                existing.add(tt)

    # Special case: identity logs from frontend (FULL_NAME:...)
    if source == "frontend/identity" and text.startswith("FULL_NAME:"):
        full_name = text.split("FULL_NAME:", 1)[1].strip()

        # canonicalize user_id for identity card writes (alias -> canonical)
        user_id, _alias_uid = await resolve_canonical_user_id(vantage_id, user_id)

        if not full_name:
            return {"status": "empty", "detail": "no full_name"}

        created = datetime.utcnow().isoformat() + "Z"

        card_payload = {
            "text": f"The user's preferred name is {full_name}.",
            "user_id": user_id,
            "user_id_alias": user_id_alias,
            "source": "memory_card",
            "tags": ["summary", "card", "user_identity"],
            "kind": "user_identity",
            "topic_key": "__singleton__", "base_importance": 0.9,
            "created_at": created,
            "updated_at": created,
        }

        try:
            emb = client.embeddings.create(model=EMBED_MODEL, input=card_payload["text"])
            vec = emb.data[0].embedding
            rec_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_id}|user_identity|__singleton__"))
            qpoint = qmodels.PointStruct(id=rec_id, vector=vec, payload=card_payload)
            get_qdrant().upsert(collection_name="memory_raw", points=[qpoint])


        except Exception as e:
            print("identity upsert error:", e)

        return {"status": "ok", "id": user_id, "note": "identity_card"}

    # Stable id used for BOTH Postgres row id and Qdrant point id
    rec_id = str(uuid.uuid4())

    # Single timestamp used for BOTH Postgres + Qdrant payload
    # - asyncpg wants a datetime object for timestamptz
    # - Qdrant payload wants an ISO string (we store Z form)
    created_dt = datetime.utcnow()
    created = created_dt.isoformat() + "Z"

    # 1) Save to Postgres (authoritative transcript)
    conn = None
    try:
        conn = await asyncpg.connect(DSN)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_log(
            id uuid PRIMARY KEY,
            user_id text,
            source text,
            text text,
            tags text[],
            created_at timestamptz DEFAULT now()
            )
        """)

        # Ensure thread_id column exists (safe even if already added)
        await conn.execute("ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS thread_id uuid")
        await conn.execute("ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS vantage_id text")
        await conn.execute("ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS user_id_alias text")
        await conn.execute("ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS request_id text")

        # If thread_id was provided but the thread row doesn't exist (or belongs to another user),
        # fix it so the sidebar can show the thread.
        if thread_id:
            owner = await conn.fetchval("SELECT user_id FROM threads WHERE id=$1", thread_id)

            if owner is None:
                # Create the thread with the provided id so the transcript is attached.
                await conn.execute(
                    "INSERT INTO threads(id, user_id, title) VALUES($1, $2, $3)",
                    thread_id, user_id, "New chat"
                )
            elif str(owner) != str(user_id):
                # Safety: never attach messages to another user's thread id.
                # Self-heal: if stored owner is an alias for this user, rewrite thread owner to canonical.
                owner_canon, _ = await resolve_canonical_user_id(vantage_id, str(owner))
                if str(owner_canon) == str(user_id):
                    await conn.execute(
                        "UPDATE threads SET user_id=$1, updated_at=now() WHERE id=$2",
                        user_id, thread_id
                    )
                else:
                    thread_id = None

        await conn.execute(
            "INSERT INTO chat_log("
            "id,user_id,user_id_alias,source,text,tags,thread_id,vantage_id,request_id,created_at"
            ") VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            rec_id, user_id, user_id_alias, source, text, tags, thread_id, vantage_id, request_id, created_dt
        )

        # Touch thread timestamp so list ordering works
        if thread_id:
            await conn.execute(
                "UPDATE threads SET updated_at=now() WHERE id=$1 AND user_id=$2",
                thread_id, user_id
            )

    except Exception as e:
        print("pg error:", e)
    finally:
        if conn:
            await conn.close()

    # 2) Embed + upsert into Qdrant (best-effort)
    if client:
        try:
            emb = client.embeddings.create(model=EMBED_MODEL, input=text)
            vec = emb.data[0].embedding

            payload = {
                "text": text,
                "user_id": user_id,
                "request_id": request_id,
                "user_id_alias": user_id_alias,
                "source": source,
                "tags": tags,
                "thread_id": str(thread_id) if thread_id else None,
                "vantage_id": vantage_id,
                "created_at": created,
                "updated_at": created,
            }

            qpoint = qmodels.PointStruct(id=rec_id, vector=vec, payload=payload)
            get_qdrant().upsert(collection_name="memory_raw", points=[qpoint])
        except Exception as e:
            # Don't fail the request if Qdrant/OpenAI is down; Postgres transcript is authoritative.
            print("qdrant upsert error:", e)
    else:
        print("log_chat: OPENAI_API_KEY missing; skipping Qdrant upsert")

    return {"status": "ok", "id": rec_id, "request_id": request_id}

# ---------- retrieval ----------
# Use env defaults already defined above:
#   DEFAULT_COLLECTION, EMBED_MODEL, QDRANT_URL
# and the lazy client helper we added earlier:
#   get_qdrant()

class RetrieveReq(BaseModel):
    query: str
    top_k: Optional[int] = 5
    score_threshold: Optional[float] = 0.0
    collection: Optional[str] = None  # if set and != "ALL", restrict to that one

@app.post("/threads/new")
async def threads_new(body: NewThreadReq):
    user_id_alias = (body.user_id or "").strip() or "anon"
    title = (body.title or "New chat").strip() or "New chat"
    vantage_id = (getattr(body, "vantage_id", None) or "default").strip() or "default"
    user_id, _alias_uid = await resolve_canonical_user_id(vantage_id, user_id_alias)

    conn = await asyncpg.connect(DSN)
    try:
        row = await conn.fetchrow(
            "INSERT INTO threads(user_id, title) VALUES ($1,$2) RETURNING id, title, updated_at",
            user_id, title
        )
        return {"thread_id": str(row["id"]), "title": row["title"], "updated_at": row["updated_at"].isoformat()}
    finally:
        await conn.close()

@app.get("/threads/list/{user_id}")
async def threads_list(user_id: str, vantage_id: str = "default"):
    user_id_alias = (user_id or "").strip() or "anon"
    user_id, _alias_uid = await resolve_canonical_user_id(vantage_id, user_id_alias)
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT id, title, updated_at FROM threads WHERE user_id=$1 AND archived=false ORDER BY updated_at DESC",
            user_id
        )
        return [{"thread_id": str(r["id"]), "title": r["title"], "updated_at": r["updated_at"].isoformat()} for r in rows]
    finally:
        await conn.close()

@app.get("/threads/{thread_id}/messages")
async def threads_messages(thread_id: str, limit: int = 200):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse({"status":"bad_request","detail":"invalid thread_id"}, status_code=400)

    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT source, text, created_at FROM chat_log WHERE thread_id=$1 ORDER BY created_at ASC LIMIT $2",
            tid, limit
        )
        out = []
        for r in rows:
            src = (r["source"] or "")
            role = "assistant" if "assistant" in src else "user"
            out.append({"role": role, "content": r["text"], "created_at": r["created_at"].isoformat()})
        return out
    finally:
        await conn.close()


class RenameThreadReq(BaseModel):
    title: str

@app.post("/threads/{thread_id}/rename")
async def threads_rename(thread_id: str, body: RenameThreadReq):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse({"status":"bad_request","detail":"invalid thread_id"}, status_code=400)

    title = (body.title or "").strip() or "New chat"

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "UPDATE threads SET title=$1, updated_at=now() WHERE id=$2",
            title, tid
        )
        return {"status": "ok", "thread_id": str(tid), "title": title}
    finally:
        await conn.close()

@app.post("/threads/{thread_id}/archive")
async def threads_archive(thread_id: str):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse({"status":"bad_request","detail":"invalid thread_id"}, status_code=400)

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "UPDATE threads SET archived=true, updated_at=now() WHERE id=$1",
            tid
        )
        return {"status": "ok", "thread_id": str(tid), "archived": True}
    finally:
        await conn.close()

@app.delete("/threads/{thread_id}")
async def threads_delete(thread_id: str):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse({"status":"bad_request","detail":"invalid thread_id"}, status_code=400)

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DELETE FROM chat_log WHERE thread_id=$1", tid)
        await conn.execute("DELETE FROM threads WHERE id=$1", tid)
    finally:
        await conn.close()

    # Optional: remove Qdrant points for this thread IF thread_id is stored in payload
    try:
        get_qdrant().delete(
            collection_name="memory_raw",
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="thread_id",
                            match=qmodels.MatchValue(value=str(tid))
                        )
                    ]
                )
            ),
        )
    except Exception as e:
        print("[threads_delete] qdrant cleanup skipped/failed:", e)

    return {"status": "ok", "thread_id": str(tid), "deleted": True}


@app.post("/retrieve")
async def retrieve(body: RetrieveReq):
    q = (body.query or "").strip()
    if not q:
        return {"status": "bad_request", "detail": "missing query", "results": []}
    if not client:
        return {"status": "error", "detail": "OPENAI_API_KEY missing", "results": []}

    # 1) Embed the query once
    emb = client.embeddings.create(model=EMBED_MODEL, input=q)
    vec = emb.data[0].embedding

    # 2) Decide which collections to search:
    #    - if body.collection is set and not "ALL" → just that collection
    #    - otherwise → all corpus collections except memory_raw
    if body.collection and body.collection != "ALL":
        collections = [body.collection]
    else:
        collections = get_corpus_collections()

    # 3) Global retrieval parameters
    per_coll_limit = int(body.top_k or int(os.getenv("RETRIEVE_TOP_K", "8")))
    thr_env = os.getenv("RETRIEVE_THRESHOLD")
    thr = float(body.score_threshold) if body.score_threshold is not None else (
        float(thr_env) if thr_env is not None else 0.30
    )

    all_hits: List[Dict[str, Any]] = []

    qdrant = get_qdrant()

    for coll in collections:
        # Detect named vector set (if collection was created with named vectors)
        vector_name = None
        try:
            info = qdrant.get_collection(coll)
            vectors_cfg = getattr(info.config.params, "vectors", None)
            if isinstance(vectors_cfg, dict) and vectors_cfg:
                # use the first named vector
                vector_name = next(iter(vectors_cfg.keys()))
        except Exception as e:
            # non-fatal: proceed with default unnamed vector for this collection
            print(f"get_collection error for {coll}:", e)

        qvec = qmodels.NamedVector(name=vector_name, vector=vec) if vector_name else vec

        try:
            hits = qdrant.search(
                collection_name=coll,
                query_vector=qvec,
                limit=per_coll_limit,
                with_payload=True,
                score_threshold=thr,
                query_filter=None,  # no payload filter yet
            )
        except Exception as e:
            print(f"qdrant search error for {coll}:", e)
            continue

        for h in (hits or []):
            all_hits.append(
                {
                    "collection": coll,
                    "id": h.id,
                    "score": float(h.score),
                    "payload": h.payload,
                }
            )

    # 4) Merge & sort all hits across all collections, then keep global top_k
    all_hits.sort(key=lambda x: x["score"], reverse=True)
    global_top_k = int(body.top_k or int(os.getenv("RETRIEVE_TOP_K", "8")))
    results = all_hits[:global_top_k]

    return {
        "status": "ok",
        "top_k": global_top_k,
        "results": results,
    }

# ---------- retrieve_memory ----------
class MemoryReq(BaseModel):
    query: str
    top_k: Optional[int] = 5
    score_threshold: Optional[float] = 0.0
    user_id: Optional[str] = None
    vantage_id: Optional[str] = "default"

# NEW: feedback signal model
class FeedbackSignal(BaseModel):
    user_id: str
    memory_id: str
    signal: str  # "positive", "negative", or "neutral"
    tag: Optional[str] = None

class GravityReq(BaseModel):
    user_id: str

@app.post("/retrieve_memory")
async def retrieve_memory(body: MemoryReq):
    """
    Retrieve personal/episodic memory from the memory_raw collection.
    Filters:
      - collection: memory_raw
      - payload.source == "frontend"
      - optional: payload.user_id == supplied user_id
    """
    q = (body.query or "").strip()
    if not q:
        return {"status":"bad_request","detail":"missing query","results":[]}
    if not client:
        return {"status":"error","detail":"OPENAI_API_KEY missing","results":[]}

    # Embed the query
    emb = client.embeddings.create(model=EMBED_MODEL, input=q)
    vec = emb.data[0].embedding

    # Build filters for memory_raw
    must_conditions = []
    if body.user_id:
        uid_alias = (body.user_id or "").strip()
        vid = (getattr(body, "vantage_id", None) or "default").strip() or "default"
        uid, _alias_uid = await resolve_canonical_user_id(vid, uid_alias)
        must_conditions.append(
            qmodels.FieldCondition(
                key="user_id",
                match=qmodels.MatchValue(value=uid)
            )
        )

    qry_filter = qmodels.Filter(must=must_conditions) if must_conditions else None

    # Search memory_raw
    hits = get_qdrant().search(
        collection_name="memory_raw",
        query_vector=vec,
        limit=int(body.top_k or 5),
        with_payload=True,
        score_threshold=float(body.score_threshold or 0.0),
        query_filter=qry_filter,
    )

    results = [
        {"collection":"memory_raw","id":h.id,"score":float(h.score),"payload":h.payload}
        for h in (hits or [])
    ]
    return {"status":"ok","top_k":int(body.top_k or 5),"results":results}


# NEW: feedback endpoint
@app.post("/memory_feedback")
async def memory_feedback(sig: FeedbackSignal):
    """
    Attach a positive/negative feedback signal to a specific memory point in memory_raw.
    This does not change ranking directly; it just updates payload.feedback.
    """
    qdrant = get_qdrant()

    # 1) Retrieve the point by id
    try:
        res = qdrant.retrieve(
            collection_name="memory_raw",
            ids=[sig.memory_id],
            with_payload=True,
            with_vectors=True,
        )
    except Exception as e:
        print(f"[feedback] retrieve error for id={sig.memory_id}: {e}")
        return {"status": "error", "detail": "retrieve_failed"}

    if not res:
        print(f"[feedback] no point found for id={sig.memory_id}")
        return {"status": "ok", "note": "point_not_found"}

    point = res[0]
    payload = point.payload or {}
    vec = point.vector

    # 2) Check user_id matches
    payload_user = (payload.get("user_id") or "").strip()
    if payload_user and payload_user.lower() != sig.user_id.lower():
        print(f"[feedback] user_id mismatch for id={sig.memory_id}: payload={payload_user}, req={sig.user_id}")
        return {"status": "ok", "note": "user_mismatch"}

    # 3) Update feedback counters
    fb = payload.get("feedback") or {}
    pos = int(fb.get("positive_signals") or 0)
    neg = int(fb.get("negative_signals") or 0)

    sig_lower = (sig.signal or "").lower()
    if sig_lower == "positive":
        pos += 1
    elif sig_lower == "negative":
        neg += 1
    # if "neutral" or anything else: don't change counts

    fb["positive_signals"] = pos
    fb["negative_signals"] = neg
    fb["last_feedback_at"] = datetime.utcnow().isoformat() + "Z"

    payload["feedback"] = fb

    # 4) Handle optional user tag (e.g. "fractal_monism_expansion")
    tag = (sig.tag or "").strip() if hasattr(sig, "tag") else ""
    if tag:
        current_user_tags = payload.get("user_tags") or []
        if tag not in current_user_tags:
            current_user_tags.append(tag)
        payload["user_tags"] = current_user_tags

    # 5) Upsert updated point back into Qdrant
    updated_point = qmodels.PointStruct(
        id=point.id,
        vector=vec,
        payload=payload,
    )

    try:
        up = qdrant.upsert(collection_name="memory_raw", points=[updated_point])
        print(f"[feedback] updated id={sig.memory_id} with signal={sig.signal}, pos={pos}, neg={neg}, status={up.status}")
    except Exception as e:
        print(f"[feedback] upsert error for id={sig.memory_id}: {e}")
        return {"status": "error", "detail": "upsert_failed"}

    return {"status": "ok", "memory_id": sig.memory_id, "positive_signals": pos, "negative_signals": neg}


@app.post("/gravity/rebuild")
async def gravity_rebuild(body: GravityReq, vantage_id: str = "default"):
    """
    Compute and store a gravity_profile card for the given user_id (canonicalized).
    """
    user_id = (body.user_id or "").strip() or "anon"
    user_id, alias_user_id = await resolve_canonical_user_id(vantage_id, user_id)

    gravity = compute_gravity(user_id)
    write_gravity_card(user_id, gravity)

    return {
        "status": "ok",
        "user_id": user_id,
        "alias_user_id": alias_user_id,
        "weights": gravity,
        "note": "gravity_profile updated",
    }

@app.get("/temporal/{user_id}")
async def temporal(user_id: str):
    secs = await get_seconds_since_last_user_message(user_id)
    return {
        "user_id": user_id,
        "seconds_since_last_user_message": secs,
        "bucket": bucket_time_gap(secs),
    }


@app.post("/vb_desire/rebuild")
async def vb_desire_rebuild(body: GravityReq, vantage_id: str = "default"):
    """
    Compute and store a vb_desire_profile card for the given user_id (canonicalized).
    """
    user_id = (body.user_id or "").strip() or "anon"
    user_id, alias_user_id = await resolve_canonical_user_id(vantage_id, user_id)

    card = build_vb_desire_profile(user_id)
    write_vb_desire_profile_card(user_id, card)

    return {
        "status": "ok",
        "user_id": user_id,
        "alias_user_id": alias_user_id,
        "card": card,
        "note": "vb_desire_profile updated",
    }

@app.get("/healthz")
async def health():
    return {
        "status": "ok",
        "time": time.time(),
        "default_collection": DEFAULT_COLLECTION,           # import-time default
        "env_default": os.getenv("RETRIEVAL_COLLECTION"),   # live env value
        "embed_model": EMBED_MODEL,
        "qdrant_url": QDRANT_URL,
    }

# ---------- cards (artifact console) ----------
CARD_KINDS_DEFAULT = [
    "user_identity",
    "assistant_identity",
    "user_instructions",
    "style",
    "style_mode",
    "preference",
    "gravity_profile",
    "vb_desire_profile",
    "persona_profile",
    "preference_profile",
]
# ---------- identity canonicalization (alias -> canonical) ----------
async def resolve_canonical_user_id(vantage_id: str, alias_user_id: str) -> tuple[str, str]:
    """
    Returns (canonical_user_id, alias_user_id). Falls back to alias if lookup fails.
    Source of truth: Postgres table vantage_identity.user_alias.
    """
    vid = (vantage_id or "default").strip() or "default"
    alias = (alias_user_id or "").strip() or "anon"
    canon = alias

    try:
        conn = await asyncpg.connect(DSN)
        try:
            row = await conn.fetchrow(
                "select canonical_user_id from vantage_identity.user_alias where vantage_id=$1 and alias_user_id=$2",
                vid, alias
            )
        finally:
            await conn.close()

        if row and row["canonical_user_id"]:
            canon = str(row["canonical_user_id"])
    except Exception as e:
        print(f"[identity] user_alias lookup failed vid={vid} alias={alias}: {e}")

    return canon, alias


@app.get("/cards/{user_id}")
async def cards_list(user_id: str, limit: int = 50, kinds: Optional[str] = None, vantage_id: str = "default"):
    """
    Lists card-like artifacts in Qdrant memory_raw for a user.
    kinds: comma-separated list. Defaults to CARD_KINDS_DEFAULT.
    """
    uid = (user_id or "").strip() or "anon"
    uid, _alias_uid = await resolve_canonical_user_id(vantage_id, uid)
    vid = (vantage_id or "default").strip() or "default"

    klist = [k.strip() for k in (kinds.split(",") if kinds else CARD_KINDS_DEFAULT) if k.strip()]

    qdrant = get_qdrant()

    limit_n = int(limit)
    scan_limit = max(limit_n * 8, 256)

    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=uid)),
            qmodels.FieldCondition(key="kind", match=qmodels.MatchAny(any=klist)),
        ]
    )

    points, _next = qdrant.scroll(
        collection_name="memory_raw",
        scroll_filter=flt,
        limit=int(scan_limit),
        with_payload=True,
        with_vectors=False,
    )

    items = []
    for p in (points or []):
        payload = p.payload or {}
        # payload_vantage_id_filter: enforce namespace
        pv = payload.get("vantage_id", None)
        if not ((pv == vid) or (pv in (None, "") and vid == "default")):
            continue
        items.append({
            "id": str(p.id),
            "kind": payload.get("kind"),
            "source": payload.get("source"),
            "tags": payload.get("tags") or [],
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "text": payload.get("text") or "",
            "payload": payload,  # full payload for viewing weights/request_patterns/etc
        })

    # newest first if timestamps exist
    def _ts(x):
        return x.get("updated_at") or x.get("created_at") or ""

    items.sort(key=_ts, reverse=True)
    if limit_n > 0 and len(items) > limit_n:
        items = items[:limit_n]
    return {"status": "ok", "user_id": uid, "count": len(items), "items": items}

class CardUpsertReq(BaseModel):
    kind: str
    topic_key: str | None = "__singleton__"
    text: str | None = ""
    tags: List[str] | None = None
    base_importance: float | None = None
    payload: Dict[str, Any] | None = None
    if_match_updated_at: str | None = None


@app.post("/cards/{user_id}")
async def cards_upsert(user_id: str, req: CardUpsertReq, vantage_id: str = "default"):
    """
    Idempotent card upsert into Qdrant memory_raw.

    Deterministic identity:
      card_id = uuid5(NAMESPACE_DNS, f"{user_id}|{kind}|{topic_key}")

    topic_key defaults to "__singleton__" for true singletons.
    """
    uid = (user_id or "").strip() or "anon"
    uid, _alias_uid = await resolve_canonical_user_id(vantage_id, uid)
    kind = (req.kind or "").strip()
    if not kind:
        return JSONResponse({"status": "bad_request", "detail": "missing kind"}, status_code=400)

    topic_key = (req.topic_key or "__singleton__").strip() or "__singleton__"
    vid = (vantage_id or "default").strip() or "default"
    card_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{uid}|{vid}|{kind}|{topic_key}"))

    qdrant = get_qdrant()

    # Retrieve existing (created_at preservation + optimistic concurrency)
    existing = qdrant.retrieve(
        collection_name="memory_raw",
        ids=[card_id],
        with_payload=True,
        with_vectors=False,
    )
    old = (existing[0].payload or {}) if existing else {}
    old_updated_at = (old.get("updated_at") or "")
    if req.if_match_updated_at and old_updated_at and req.if_match_updated_at != old_updated_at:
        return JSONResponse(
            {
                "status": "conflict",
                "detail": "updated_at_mismatch",
                "card_id": card_id,
                "current_updated_at": old_updated_at,
            },
            status_code=409,
        )

    now = datetime.utcnow().isoformat() + "Z"
    created = old.get("created_at") or now

    payload = {
        "user_id": uid,
        "vantage_id": vid,
        "kind": kind,
        "topic_key": topic_key,
        "source": "memory_card",
        "tags": (req.tags if req.tags is not None else (old.get("tags") or ["card", kind])),
        "base_importance": float(req.base_importance) if req.base_importance is not None else float(old.get("base_importance") or 0.7),
        "created_at": created,
        "updated_at": now,
        "text": (req.text if req.text is not None else (old.get("text") or "")),
    }

    # Merge extra fields (non-destructive to identity fields)
    extra = req.payload or {}
    for k, v in extra.items():
        if k in ("user_id", "kind", "topic_key", "source", "created_at"):
            continue
        payload[k] = v

    # Embed
    if not client:
        return {"status": "error", "detail": "OPENAI_API_KEY missing"}
    embed_text = payload.get("text") or f"{kind} card for {uid}"
    emb = client.embeddings.create(model=EMBED_MODEL, input=embed_text)
    vec = emb.data[0].embedding

    point = qmodels.PointStruct(id=card_id, vector=vec, payload=payload)
    qdrant.upsert(collection_name="memory_raw", points=[point])

    return {
        "status": "ok",
        "user_id": uid,
        "vantage_id": vid,
        "card_id": card_id,
        "kind": kind,
        "topic_key": topic_key,
        "created_at": created,
        "updated_at": now,
    }

@app.delete("/cards/{user_id}/{card_id}")
async def cards_delete(user_id: str, card_id: str, vantage_id: str = "default"):
    """
    Deletes a card point from Qdrant memory_raw.
    Safety: only delete if payload.user_id matches.
    """
    uid = (user_id or "").strip() or "anon"
    uid, _alias_uid = await resolve_canonical_user_id(vantage_id, uid)
    qdrant = get_qdrant()

    # verify ownership
    res = qdrant.retrieve(
        collection_name="memory_raw",
        ids=[card_id],
        with_payload=True,
        with_vectors=False,
    )
    if not res:
        return {"status": "ok", "note": "not_found"}

    payload = res[0].payload or {}
    if (payload.get("user_id") or "").strip() != uid:
        return JSONResponse({"status":"bad_request","detail":"user_mismatch"}, status_code=400)


    # Lock singleton cards (system-managed). Edit/update via POST; rebuild via daemon endpoints.
    topic_key = (payload.get("topic_key") or "").strip()
    if topic_key == "__singleton__":
        return JSONResponse(
            {
                "status": "forbidden",
                "detail": "singleton_locked",
                "card_id": card_id,
                "kind": payload.get("kind"),
                "topic_key": topic_key,
            },
            status_code=403,
        )
    qdrant.delete(
        collection_name="memory_raw",
        points_selector=qmodels.PointIdsList(points=[card_id]),
    )

    return {"status": "ok", "deleted": card_id}

# ---------- security/privacy: delete all user data ----------
@app.delete("/user/{user_id}/data")
async def delete_all_user_data(user_id: str):
    uid = (user_id or "").strip() or "anon"

    # 1) Delete Postgres transcript + threads
    pg_chat = None
    pg_threads = None
    try:
        conn = await asyncpg.connect(DSN)
        try:
            pg_chat = await conn.execute("DELETE FROM chat_log WHERE user_id=$1", uid)
            pg_threads = await conn.execute("DELETE FROM threads WHERE user_id=$1", uid)
        finally:
            await conn.close()
    except Exception as e:
        return JSONResponse({"status":"error","detail":f"pg_delete_failed: {e}"}, status_code=500)

    # 2) Delete Qdrant memory points for this user (best-effort)
    qdrant_deleted = False
    try:
        get_qdrant().delete(
            collection_name="memory_raw",
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="user_id",
                            match=qmodels.MatchValue(value=uid)
                        )
                    ]
                )
            ),
        )
        qdrant_deleted = True
    except Exception as e:
        print("[delete_all_user_data] qdrant delete failed:", e)

    return {
        "status": "ok",
        "user_id": uid,
        "pg_chat_log": pg_chat,
        "pg_threads": pg_threads,
        "qdrant_deleted": qdrant_deleted
    }

# ---------- security/privacy: export + forget recent ----------
from datetime import timedelta
from fastapi.responses import Response

@app.delete("/user/{user_id}/recent")
async def delete_recent_user_data(user_id: str, minutes: int = 60):
    """
    Soft-delete: remove recent chat_log rows for user_id and delete matching Qdrant points by id.
    minutes: how far back to delete (default 60).
    """
    uid = (user_id or "").strip() or "anon"
    minutes = int(minutes or 60)
    if minutes < 1:
        return JSONResponse({"status":"bad_request","detail":"minutes must be >= 1"}, status_code=400)
    if minutes > 60 * 24 * 30:
        return JSONResponse({"status":"bad_request","detail":"minutes too large"}, status_code=400)

    cutoff = datetime.utcnow() - timedelta(minutes=minutes)

    # 1) gather ids to delete (these ids match Qdrant point ids)
    ids: List[str] = []
    try:
        conn = await asyncpg.connect(DSN)
        try:
            rows = await conn.fetch(
                "SELECT id FROM chat_log WHERE user_id=$1 AND created_at >= $2",
                uid, cutoff
            )
            ids = [str(r["id"]) for r in (rows or [])]

            pg_del = await conn.execute(
                "DELETE FROM chat_log WHERE user_id=$1 AND created_at >= $2",
                uid, cutoff
            )
        finally:
            await conn.close()
    except Exception as e:
        return JSONResponse({"status":"error","detail":f"pg_delete_failed: {e}"}, status_code=500)

    # 2) delete matching Qdrant points by id (best-effort)
    qdrant_deleted = 0
    try:
        qdrant = get_qdrant()
        # delete in batches to avoid huge payloads
        batch_size = 256
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i+batch_size]
            qdrant.delete(
                collection_name="memory_raw",
                points_selector=qmodels.PointIdsList(points=batch),
            )
            qdrant_deleted += len(batch)
    except Exception as e:
        print("[delete_recent_user_data] qdrant delete failed:", e)

    return {
        "status": "ok",
        "user_id": uid,
        "minutes": minutes,
        "pg_deleted": pg_del,
        "qdrant_deleted_points": qdrant_deleted,
    }


@app.get("/user/{user_id}/export")
async def export_user_data(user_id: str, limit: int = 20000):
    """
    Export: threads + chat_log transcript + latest cards.
    limit: max chat_log rows to include (default 20k).
    """
    uid = (user_id or "").strip() or "anon"
    limit = int(limit or 20000)
    if limit < 1:
        return JSONResponse({"status":"bad_request","detail":"limit must be >= 1"}, status_code=400)
    if limit > 200000:
        return JSONResponse({"status":"bad_request","detail":"limit too large"}, status_code=400)

    # Threads + transcript from Postgres
    threads = []
    messages = []
    try:
        conn = await asyncpg.connect(DSN)
        try:
            threads = await conn.fetch(
                "SELECT id, title, created_at, updated_at, archived FROM threads WHERE user_id=$1 ORDER BY updated_at DESC",
                uid
            )
            messages = await conn.fetch(
                "SELECT id, thread_id, source, text, tags, created_at FROM chat_log WHERE user_id=$1 ORDER BY created_at ASC LIMIT $2",
                uid, limit
            )
        finally:
            await conn.close()
    except Exception as e:
        return JSONResponse({"status":"error","detail":f"pg_export_failed: {e}"}, status_code=500)

    # Cards from Qdrant (same kinds list as /cards)
    card_kinds = CARD_KINDS_DEFAULT if "CARD_KINDS_DEFAULT" in globals() else [
        "user_identity","gravity_profile","vb_desire_profile","persona_profile","style_profile","preference_profile"
    ]

    cards = []
    try:
        qdrant = get_qdrant()
        flt = qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=uid)),
                qmodels.FieldCondition(key="kind", match=qmodels.MatchAny(any=card_kinds)),
            ]
        )
        points, _next = qdrant.scroll(
            collection_name="memory_raw",
            scroll_filter=flt,
            limit=200,
            with_payload=True,
            with_vectors=False,
        )
        for p in (points or []):
            cards.append({"id": str(p.id), "payload": (p.payload or {})})
    except Exception as e:
        print("[export_user_data] qdrant cards export failed:", e)

    export = {
        "status": "ok",
        "user_id": uid,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "threads": [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "archived": bool(r["archived"]),
            }
            for r in (threads or [])
        ],
        "messages": [
            {
                "id": str(r["id"]),
                "thread_id": str(r["thread_id"]) if r["thread_id"] else None,
                "source": r["source"],
                "text": r["text"],
                "tags": r["tags"] or [],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in (messages or [])
        ],
        "cards": cards,
    }

    # Return as downloadable JSON
    filename = f"verbalsage_export_{uid}.json"
    return Response(
        content=json.dumps(export, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
# ---------- profiles (server-persisted UI defaults) ----------
class ProfileUpsertReq(BaseModel):
    user_id: str
    profile_id: Optional[str] = None
    name: Optional[str] = None
    is_default: bool = True
    payload: Dict[str, Any] = {}

async def _ensure_vs_profiles(conn):
    await conn.execute("""
      CREATE TABLE IF NOT EXISTS vs_profiles(
        user_id    text NOT NULL,
        profile_id text NOT NULL,
        name       text NOT NULL,
        payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
        is_default boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(user_id, profile_id)
      )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS vs_profiles_user_idx ON vs_profiles(user_id)")

@app.post("/profiles/upsert")
async def profiles_upsert(req: ProfileUpsertReq, vantage_id: str = "default"):
    alias_user_id = (req.user_id or "").strip()
    if not alias_user_id:
        return JSONResponse({"status": "bad_request", "detail": "missing user_id"}, status_code=400)

    # canonicalize user_id (alias -> canonical)
    user_id, _alias_uid = await resolve_canonical_user_id(vantage_id, alias_user_id)

    profile_id = (req.profile_id or "").strip() or str(uuid.uuid4())
    name = (req.name or "").strip() or "default"

    payload = req.payload or {}

    # preserve provenance without colliding with user payload
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta.update({
        "vantage_id": (vantage_id or "default").strip() or "default",
        "user_id_alias": alias_user_id,
        "canonical_user_id": user_id,
    })
    payload["_meta"] = meta

    payload_json = json.dumps(payload, ensure_ascii=False)

    conn = await asyncpg.connect(DSN)
    try:
        await _ensure_vs_profiles(conn)

        await conn.execute("""
          INSERT INTO vs_profiles(user_id, profile_id, name, payload, is_default)
          VALUES($1,$2,$3,$4::jsonb,$5)
          ON CONFLICT (user_id, profile_id)
          DO UPDATE SET
            name=EXCLUDED.name,
            payload=EXCLUDED.payload,
            is_default=EXCLUDED.is_default,
            updated_at=now()
        """, user_id, profile_id, name, payload_json, bool(req.is_default))

        if req.is_default:
            await conn.execute("""
              UPDATE vs_profiles
              SET is_default=false, updated_at=now()
              WHERE user_id=$1 AND profile_id<>$2 AND is_default=true
            """, user_id, profile_id)

        row = await conn.fetchrow("""
          SELECT user_id, profile_id, name, is_default, created_at, updated_at, payload
          FROM vs_profiles
          WHERE user_id=$1 AND profile_id=$2
        """, user_id, profile_id)

        d = dict(row) if row else None
        if d and isinstance(d.get("payload"), str):
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                pass
        return {"status": "ok", "profile": d}
    finally:
        await conn.close()

@app.get("/profiles/{user_id}/default")
async def profiles_get_default(user_id: str, vantage_id: str = "default"):
    alias_user_id = (user_id or "").strip()
    if not alias_user_id:
        return JSONResponse({"status": "bad_request", "detail": "missing user_id"}, status_code=400)

    # canonicalize user_id (alias -> canonical)
    uid, _alias_uid = await resolve_canonical_user_id(vantage_id, alias_user_id)

    conn = await asyncpg.connect(DSN)
    try:
        await _ensure_vs_profiles(conn)

        row = await conn.fetchrow("""
          SELECT user_id, profile_id, name, is_default, created_at, updated_at, payload
          FROM vs_profiles
          WHERE user_id=$1 AND is_default=true
          ORDER BY updated_at DESC
          LIMIT 1
        """, uid)

        if not row:
            row = await conn.fetchrow("""
              SELECT user_id, profile_id, name, is_default, created_at, updated_at, payload
              FROM vs_profiles
              WHERE user_id=$1
              ORDER BY updated_at DESC
              LIMIT 1
            """, uid)

        d = dict(row) if row else None
        if d and isinstance(d.get("payload"), str):
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                pass
        return {"status": "ok", "profile": d}
    finally:
        await conn.close()


# --- xAI Grok Voice Agent relay (server-side) ---
# Thin WebSocket bridge: hides XAI_API_KEY server-side and relays xAI realtime events.
# Client connects to:  ws://<brains>/ws/voice?voice=Ara&turn=none
# Server connects to:  wss://api.x.ai/v1/realtime (Authorization: Bearer XAI_API_KEY)
#
# Client should send xAI *client events* as JSON (forwarded verbatim), e.g.:
#   {"type":"conversation.item.create","item":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}}
#   {"type":"response.create","response":{"modalities":["text","audio"]}}
#
# Server forwards xAI *server events* back to the client unchanged.
@app.websocket("/ws/voice")
async def ws_voice_relay(ws: WebSocket):
    await ws.accept()

    # Token gate (prevents public abuse)
    expected = os.getenv("VOICE_WS_TOKEN")
    if expected:
        provided = ws.query_params.get("token", "")
        if not provided or provided != expected:
            await ws.send_text(json.dumps({"type":"error","error":"unauthorized"}))
            await ws.close(code=1008)
            return

    xai_key = os.getenv("XAI_API_KEY")
    if not xai_key:
        await ws.send_text(json.dumps({"type":"error","error":"XAI_API_KEY missing on server"}))
        await ws.close(code=1011)
        return

    voice = ws.query_params.get("voice", "Ara")
    instructions = ws.query_params.get("instructions", "You are a helpful assistant.")
    # turn=server_vad for automatic turn detection, otherwise manual ("none")
    turn = (ws.query_params.get("turn", "none") or "none").strip().lower()

    in_rate = int(ws.query_params.get("in_rate", "24000"))
    out_rate = int(ws.query_params.get("out_rate", "24000"))

    turn_detection = {"type": "server_vad"} if turn == "server_vad" else {"type": None}

    async def _send_err(msg: str):
        try:
            await ws.send_text(json.dumps({"type":"error","error":msg}))
        except Exception:
            pass

    try:
        async with websockets.connect(
            uri="wss://api.x.ai/v1/realtime",
            ssl=True,
            family=socket.AF_INET,
            additional_headers={"Authorization": f"Bearer {xai_key}"},
            open_timeout=30,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
        ) as xws:
            # Configure xAI session immediately
            session_update = {
            "type": "session.update",
            "session": {
                "instructions": instructions,
                "voice": voice,
                "turn_detection": turn_detection,

                "audio": {
                "input":  {"format": {"type": "audio/pcm", "rate": in_rate}},
                "output": {"format": {"type": "audio/pcm", "rate": out_rate}},
                },

                "input_audio_transcription": {"model": "default"},
            },
            }
            await xws.send(json.dumps(session_update))

            async def pump_client_to_xai():
                while True:
                    try:
                        raw = await ws.receive_text()
                    except WebSocketDisconnect:
                        break
                    # only forward JSON text frames
                    try:
                        json.loads(raw)
                    except Exception:
                        await _send_err("client sent non-JSON message (expected xAI realtime event JSON)")
                        continue
                    await xws.send(raw)

            async def pump_xai_to_client():
                while True:
                    try:
                        msg = await xws.recv()
                    except websockets.exceptions.ConnectionClosed:
                        break
                    if isinstance(msg, bytes):
                        msg = msg.decode("utf-8", "ignore")
                    await ws.send_text(msg)

            t1 = asyncio.create_task(pump_client_to_xai())
            t2 = asyncio.create_task(pump_xai_to_client())
            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()

    except Exception as e:
        await _send_err(str(e))
        try:
            await ws.close(code=1011)
        except Exception:
            pass

@app.get("/readyz", include_in_schema=False)
async def readyz():
    """
    Readiness: Postgres connectivity only.
    Avoids OpenAPI generation (currently broken) and avoids Qdrant dependency.
    """
    try:
        conn = await asyncpg.connect(DSN)
        v = await conn.fetchval("select 1")
        await conn.close()
        if v != 1:
            raise RuntimeError("postgres select 1 failed")
    except Exception as e:
        return JSONResponse({"ok": False, "postgres": str(e)}, status_code=503)
    return {"ok": True, "postgres": True}

