from typing import Any, Dict, List, Optional
import os, time, uuid, hashlib, hmac, asyncpg, json
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
from rag_engine.telemetry_router import router as telemetry_router
from rag_engine.lifeswitch_meals_router import router as lifeswitch_meals_router
from rag_engine.lifeswitch_nutrition_log_router import router as lifeswitch_nutrition_log_router
from rag_engine.lifeswitch_nutrition_router import router as lifeswitch_nutrition_router
from rag_engine.lifeswitch_nutrition_log_batch_router import router as lifeswitch_nutrition_log_batch_router
from rag_engine.lifeswitch_training_router import router as lifeswitch_training_router
from rag_engine.lifeswitch_plan_router import router as lifeswitch_plan_router
from rag_engine.lifeswitch_measurements_router import router as lifeswitch_measurements_router
from rag_engine.lifeswitch_people_router import router as lifeswitch_people_router
from rag_engine.catalog_router import router as catalog_router
from rag_engine.vb_tagging import infer_vb_tags
from rag_engine.gravity import compute_gravity, write_gravity_card
from rag_engine.vb_desire_profile import build_vb_desire_profile, write_vb_desire_profile_card
class NewThreadReq(BaseModel):
    user_id: str
    title: Optional[str] = None
    vantage_id: Optional[str] = "default"
from rag_engine.voice_realtime_router import router as voice_realtime_router
from rag_engine.voice_tts_router import router as voice_tts_router
from rag_engine.lifeswitch_auth import require_actor_matches_owner
from scripts.review_promotion_plan import build_personal_event_promotion_preview


app = FastAPI(title="Brains API", version="1.0.0")
app.include_router(rag_router, prefix="/rag")
app.include_router(vantage_router, prefix="/vantage")
app.include_router(telemetry_router)
app.include_router(lifeswitch_nutrition_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_meals_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_nutrition_log_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_nutrition_log_batch_router, prefix="/lifeswitch/nutrition")
app.include_router(catalog_router, prefix="/catalog")
app.include_router(lifeswitch_training_router, prefix="/lifeswitch/training")
app.include_router(lifeswitch_plan_router, prefix="/lifeswitch/plan")
app.include_router(lifeswitch_measurements_router, prefix="/lifeswitch/measurements")
app.include_router(lifeswitch_people_router, prefix="/lifeswitch/people")
app.include_router(voice_realtime_router)
app.include_router(voice_tts_router)

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


# ---------- service boundary ----------
# Brains is an internal backend. Protected routes must be called by a trusted
# server-side proxy using X-VS-Service-Token.
# This is not user auth. It is the first wall: browser/client traffic should not
# directly reach user-owned Brains routes.
PUBLIC_SERVICE_TOKEN_EXACT = {
    "/openapi.json",
    "/docs",
    "/redoc",
}

PUBLIC_GET_SERVICE_TOKEN_PREFIXES = (
    "/catalog/",
    "/lifeswitch/training/workout_template_shares/preview",
    "/lifeswitch/people/invitations/preview",
)

def _service_token_required(path: str, method: str) -> bool:
    path = str(path or "")
    if path in PUBLIC_SERVICE_TOKEN_EXACT:
        return False
    if path.startswith("/docs/") or path.startswith("/redoc/") or path.startswith("/openapi"):
        return False
    if str(method or "").upper() == "GET":
        for prefix in PUBLIC_GET_SERVICE_TOKEN_PREFIXES:
            if path.startswith(prefix):
                return False
    return True

@app.middleware("http")
async def service_token_middleware(request: Request, call_next):
    expected = (os.getenv("VS_SERVICE_TOKEN") or "").strip()

    if not expected:
        rid = _get_request_id(request)
        return JSONResponse(
            {"status": "unavailable", "detail": "service_token_not_configured"},
            status_code=503,
            headers={"x-request-id": rid},
        )

    path = request.url.path
    if not _service_token_required(path, request.method):
        return await call_next(request)

    provided = (request.headers.get("x-vs-service-token") or "").strip()
    if not hmac.compare_digest(provided, expected):
        rid = _get_request_id(request)
        return JSONResponse(
            {"status": "unauthorized", "detail": "missing_or_invalid_service_token"},
            status_code=401,
            headers={"x-request-id": rid},
        )

    return await call_next(request)


def parse_uuid(s: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(s))
    except Exception:
        return None


# ---------- actor / owner enforcement ----------
def _actor_user_id(req: Request) -> Optional[str]:
    raw = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not raw or len(raw) > 128:
        return None
    return raw


def _actor_missing_response() -> JSONResponse:
    return JSONResponse(
        {"status": "unauthorized", "detail": "missing_actor_user_id"},
        status_code=401,
    )


def _owner_mismatch_response() -> JSONResponse:
    return JSONResponse(
        {"status": "forbidden", "detail": "actor_owner_mismatch"},
        status_code=403,
    )


async def _require_actor_for_user(req: Request, requested_user_id: str, vantage_id: str = "default"):
    """
    Service token proves trusted infrastructure.
    x-vs-actor-user-id identifies the authenticated user resolved by the frontend.
    This helper canonicalizes both actor and requested user and requires equality.
    Returns (response_or_none, canonical_requested_user_id).
    """
    actor = _actor_user_id(req)
    if not actor:
        return _actor_missing_response(), None

    requested_alias = (requested_user_id or "").strip() or "anon"
    vid = (vantage_id or "default").strip() or "default"

    requested_uid, _ = await resolve_canonical_user_id(vid, requested_alias)
    actor_uid, _ = await resolve_canonical_user_id(vid, actor)

    if str(actor_uid) != str(requested_uid):
        return _owner_mismatch_response(), requested_uid

    return None, requested_uid


async def _require_actor_for_thread(req: Request, thread_id: uuid.UUID):
    """
    Require the requested thread row to belong to x-vs-actor-user-id.
    Returns (response_or_none, actor_user_id).
    """
    actor = _actor_user_id(req)
    if not actor:
        return _actor_missing_response(), None

    conn = await asyncpg.connect(DSN)
    try:
        row = await conn.fetchrow(
            "SELECT user_id FROM threads WHERE id=$1",
            thread_id,
        )
    finally:
        await conn.close()

    if not row:
        return JSONResponse(
            {"status": "not_found", "detail": "thread_not_found"},
            status_code=404,
        ), None

    owner_uid = str(row["user_id"])
    actor_uid, _ = await resolve_canonical_user_id("default", actor)

    if str(actor_uid) != owner_uid:
        return _owner_mismatch_response(), actor_uid

    return None, actor_uid


# single global qdrant client
qdrant_client = None

DSN = os.environ["POSTGRES_DSN"]
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




# ---------- admin memory review ----------
@app.get("/admin/memory/review-plan")
async def admin_memory_review_plan(req: Request):
    """
    Read-only memory promotion review plan.

    Service-token middleware protects this route at the Brains boundary.
    The frontend admin proxy is responsible for user/admin capability checks.
    No writes are performed here.
    """
    actor = _actor_user_id(req)
    if not actor:
        return _actor_missing_response()

    try:
        # The planner reuses CLI/inventory code that may call asyncio.run().
        # Execute it in a worker thread so it does not run inside FastAPI's
        # already-running event loop.
        plan = await asyncio.to_thread(build_personal_event_promotion_preview)
    except Exception as e:
        rid = getattr(req.state, "request_id", None) or _get_request_id(req)
        return JSONResponse(
            {
                "ok": False,
                "error": "review_plan_failed",
                "detail": str(e),
                "request_id": rid,
            },
            status_code=500,
            headers={"x-request-id": rid},
        )

    plan = dict(plan or {})
    plan["ok"] = True
    plan["endpoint"] = "admin_memory_review_plan"
    plan["read_only"] = True
    plan["actor_user_id"] = actor
    return plan


# ---------- persistent chat memory ----------
@app.post("/log")
async def log_chat(req: Request):
    try:
        body: Dict[str, Any] = await req.json()
    except Exception:
        return JSONResponse({"status":"bad_request","detail":"invalid json"}, status_code=400)

    text = body.get("text") or body.get("input") or ""
    user_id_alias = require_actor_matches_owner(req, body.get("user_id") or "")
    source = body.get("source") or "frontend"
    tags = body.get("tags") or []
    vantage_id = (body.get("vantage_id") or "").strip() or "default"
    request_id = _sanitize_request_id(getattr(req.state, "request_id", None)) or str(uuid.uuid4())

    # New writes are owned by the exact authenticated Supabase UUID. Vantage
    # aliases must not partition or remap factual memory.
    user_id = user_id_alias


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
async def threads_new(body: NewThreadReq, req: Request):
    user_id_alias = (body.user_id or "").strip() or "anon"
    title = (body.title or "New chat").strip() or "New chat"
    vantage_id = (getattr(body, "vantage_id", None) or "default").strip() or "default"

    actor_err, user_id = await _require_actor_for_user(req, user_id_alias, vantage_id)
    if actor_err:
        return actor_err

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
async def threads_list(user_id: str, req: Request, vantage_id: str = "default"):
    user_id_alias = (user_id or "").strip() or "anon"

    actor_err, user_id = await _require_actor_for_user(req, user_id_alias, vantage_id)
    if actor_err:
        return actor_err
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
async def threads_messages(thread_id: str, req: Request, limit: int = 200):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse({"status":"bad_request","detail":"invalid thread_id"}, status_code=400)

    actor_err, _actor_uid = await _require_actor_for_thread(req, tid)
    if actor_err:
        return actor_err

    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            """
            SELECT id, source, text, created_at
            FROM chat_log
            WHERE thread_id=$1
            ORDER BY created_at ASC
            LIMIT $2
            """,
            tid,
            limit,
        )
        out = []
        for r in rows:
            src = (r["source"] or "")
            role = "assistant" if "assistant" in src else "user"
            out.append({
                "id": str(r["id"]),
                "role": role,
                "content": r["text"],
                "created_at": r["created_at"].isoformat(),
            })
        return out
    finally:
        await conn.close()


@app.delete("/threads/{thread_id}/messages/{message_id}/truncate")
async def threads_truncate_from_message(thread_id: str, message_id: str, req: Request):
    tid = parse_uuid(thread_id)
    mid = parse_uuid(message_id)
    if not tid or not mid:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid thread_id or message_id"},
            status_code=400,
        )

    actor_err, _actor_uid = await _require_actor_for_thread(req, tid)
    if actor_err:
        return actor_err

    conn = await asyncpg.connect(DSN)
    try:
        async with conn.transaction():
            target = await conn.fetchrow(
                """
                SELECT id, source, created_at
                FROM chat_log
                WHERE thread_id=$1 AND id=$2
                """,
                tid,
                mid,
            )
            if not target:
                return JSONResponse(
                    {"status": "not_found", "detail": "message not found in thread"},
                    status_code=404,
                )

            src = str(target["source"] or "")
            if "user" not in src:
                return JSONResponse(
                    {"status": "bad_request", "detail": "only user messages can be edited"},
                    status_code=400,
                )

            result = await conn.execute(
                """
                DELETE FROM chat_log
                WHERE thread_id=$1
                  AND created_at >= $2
                """,
                tid,
                target["created_at"],
            )

            await conn.execute(
                "UPDATE threads SET updated_at=now() WHERE id=$1",
                tid,
            )

        deleted = 0
        try:
            deleted = int(str(result).split()[-1])
        except Exception:
            deleted = 0

        return {
            "status": "ok",
            "thread_id": str(tid),
            "message_id": str(mid),
            "deleted": deleted,
        }
    finally:
        await conn.close()




class RenameThreadReq(BaseModel):
    title: str

@app.post("/threads/{thread_id}/rename")
async def threads_rename(thread_id: str, body: RenameThreadReq, req: Request):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse({"status":"bad_request","detail":"invalid thread_id"}, status_code=400)

    actor_err, _actor_uid = await _require_actor_for_thread(req, tid)
    if actor_err:
        return actor_err

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
async def threads_archive(thread_id: str, req: Request):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse({"status":"bad_request","detail":"invalid thread_id"}, status_code=400)

    actor_err, _actor_uid = await _require_actor_for_thread(req, tid)
    if actor_err:
        return actor_err

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
async def threads_delete(thread_id: str, req: Request):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse({"status":"bad_request","detail":"invalid thread_id"}, status_code=400)

    actor_err, _actor_uid = await _require_actor_for_thread(req, tid)
    if actor_err:
        return actor_err

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

# NEW: feedback signal model
class FeedbackSignal(BaseModel):
    user_id: str
    memory_id: str
    signal: str  # "positive", "negative", or "neutral"
    tag: Optional[str] = None

class GravityReq(BaseModel):
    user_id: str

# NEW: feedback endpoint
@app.post("/memory_feedback")
async def memory_feedback(sig: FeedbackSignal, req: Request):
    """
    Attach a positive/negative feedback signal to a specific memory point in memory_raw.
    This does not change ranking directly; it just updates payload.feedback.
    """
    actor_err, _uid = await _require_actor_for_user(req, sig.user_id, "default")
    if actor_err:
        return actor_err

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
async def cards_list(user_id: str, req: Request, limit: int = 50, kinds: Optional[str] = None, vantage_id: str = "default"):
    """
    Lists card-like artifacts in Qdrant memory_raw for a user.
    kinds: comma-separated list. Defaults to CARD_KINDS_DEFAULT.
    """
    actor_err, uid = await _require_actor_for_user(req, user_id, vantage_id)
    if actor_err:
        return actor_err
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


@app.get("/vantage-cards/{user_id}")
async def vantage_cards_list(
    user_id: str,
    req: Request,
    vantage_id: str = "default",
    kinds: Optional[str] = None,
    limit: int = 100,
):
    """
    List Postgres Vantage cards from vantage_card.card_head.

    This is the newer Vantage-scoped card system, distinct from legacy Qdrant
    memory cards served by /cards/{user_id}.
    """
    vid = (vantage_id or "default").strip() or "default"
    actor_err, uid = await _require_actor_for_user(req, user_id, vid)
    if actor_err:
        return actor_err

    klist = [k.strip() for k in (kinds.split(",") if kinds else []) if k.strip()]
    limit_n = max(1, min(int(limit or 100), 500))

    conn = await asyncpg.connect(DSN)
    try:
        where = """
          WHERE vantage_id=$1
            AND (
              topic_key LIKE $2
              OR payload->>'user_id' = $3
            )
        """
        args = [vid, f"user/{uid}/%", uid]

        if klist:
            where += " AND kind = ANY($4::text[])"
            args.append(klist)

        sql = f"""
          SELECT
            card_id,
            vantage_id,
            kind,
            topic_key,
            status::text as status,
            summary,
            payload,
            strength,
            confidence,
            created_at,
            updated_at
          FROM vantage_card.card_head
          {where}
          ORDER BY updated_at DESC NULLS LAST, card_id DESC
          LIMIT {limit_n}
        """

        rows = await conn.fetch(sql, *args)

        items = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d.get("card_id"))
            d["source"] = "vantage_card"
            if isinstance(d.get("payload"), str):
                try:
                    d["payload"] = json.loads(d["payload"])
                except Exception:
                    pass
            d["text"] = d.get("summary") or ""
            payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            d["use_scope"] = payload.get("use_scope")
            d["surface_policy"] = payload.get("surface_policy")
            d["sensitivity"] = payload.get("sensitivity")
            d["domains"] = payload.get("domains") or []
            d["suppressed_reason"] = payload.get("suppressed_reason")
            d["source_vantage_counts"] = payload.get("source_vantage_counts") or {}
            d["value_counts"] = payload.get("value_counts") or {}
            items.append(d)

        return {
            "status": "ok",
            "source": "vantage_card",
            "user_id": uid,
            "vantage_id": vid,
            "count": len(items),
            "items": items,
        }
    finally:
        await conn.close()


@app.post("/cards/{user_id}")
async def cards_upsert(user_id: str, req: CardUpsertReq, request: Request, vantage_id: str = "default"):
    """
    Idempotent card upsert into Qdrant memory_raw.

    Deterministic identity:
      card_id = uuid5(NAMESPACE_DNS, f"{user_id}|{kind}|{topic_key}")

    topic_key defaults to "__singleton__" for true singletons.
    """
    actor_err, uid = await _require_actor_for_user(request, user_id, vantage_id)
    if actor_err:
        return actor_err
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
async def cards_delete(user_id: str, card_id: str, req: Request, vantage_id: str = "default"):
    """
    Deletes a card point from Qdrant memory_raw.
    Safety: only delete if payload.user_id matches.
    """
    actor_err, uid = await _require_actor_for_user(req, user_id, vantage_id)
    if actor_err:
        return actor_err
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
async def delete_all_user_data(user_id: str, req: Request):
    actor_err, uid = await _require_actor_for_user(req, user_id, "default")
    if actor_err:
        return actor_err

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
async def delete_recent_user_data(user_id: str, req: Request, minutes: int = 60):
    """
    Soft-delete: remove recent chat_log rows for user_id and delete matching Qdrant points by id.
    minutes: how far back to delete (default 60).
    """
    actor_err, uid = await _require_actor_for_user(req, user_id, "default")
    if actor_err:
        return actor_err
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
async def export_user_data(user_id: str, req: Request, limit: int = 20000):
    """
    Export: threads + chat_log transcript + latest cards.
    limit: max chat_log rows to include (default 20k).
    """
    actor_err, uid = await _require_actor_for_user(req, user_id, "default")
    if actor_err:
        return actor_err
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


# ---------- vantages (Supabase mirror for backend processing) ----------
class VantageSyncReq(BaseModel):
    user_id: str
    profiles: List[Dict[str, Any]] = []
    defaultId: Optional[str] = None
    active: Optional[Dict[str, Any]] = None
    source_updated_at: Optional[str] = None
    mode: Optional[str] = "full"  # "full" replaces mirrored presets; "active" only updates active state


def _clean_vantage_id(v: Any) -> str:
    s = str(v or "").strip()[:64]
    return s or "default"


def _clean_profile_name(v: Any, fallback: str) -> str:
    s = str(v or "").strip()[:128]
    return s or fallback or "default"


async def _ensure_vantage_profile_registry(conn):
    await conn.execute("""
      CREATE SCHEMA IF NOT EXISTS vantage_profile
    """)
    await conn.execute("""
      CREATE TABLE IF NOT EXISTS vantage_profile.registry (
        user_id text NOT NULL,
        vantage_id text NOT NULL,

        profile_id text,
        name text NOT NULL,

        is_default boolean NOT NULL DEFAULT false,
        is_active boolean NOT NULL DEFAULT false,

        state jsonb NOT NULL DEFAULT '{}'::jsonb,

        source text NOT NULL DEFAULT 'supabase',
        source_updated_at timestamptz,
        last_applied_at timestamptz,

        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),

        PRIMARY KEY (user_id, vantage_id)
      )
    """)
    await conn.execute("""
      CREATE INDEX IF NOT EXISTS registry_user_idx
      ON vantage_profile.registry(user_id)
    """)
    await conn.execute("""
      CREATE INDEX IF NOT EXISTS registry_active_idx
      ON vantage_profile.registry(user_id, is_active, updated_at DESC)
    """)
    await conn.execute("""
      CREATE INDEX IF NOT EXISTS registry_default_idx
      ON vantage_profile.registry(user_id, is_default, updated_at DESC)
    """)


async def _fetch_vantage_registry_items(conn, user_id: str) -> List[Dict[str, Any]]:
    rows = await conn.fetch("""
      SELECT user_id, vantage_id, profile_id, name, is_default, is_active,
             state, source, source_updated_at, last_applied_at, created_at, updated_at
      FROM vantage_profile.registry
      WHERE user_id=$1
      ORDER BY is_active DESC, is_default DESC, name ASC
    """, user_id)

    items = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("state"), str):
            try:
                d["state"] = json.loads(d["state"])
            except Exception:
                pass
        items.append(d)
    return items


def _profile_to_registry_row(profile: Dict[str, Any], default_id: str | None) -> Dict[str, Any]:
    profile = profile or {}
    state = profile.get("state") if isinstance(profile.get("state"), dict) else {}
    profile_id = str(profile.get("id") or "").strip() or None
    vid = _clean_vantage_id(state.get("vantageId") or profile.get("name") or profile_id)
    name = _clean_profile_name(profile.get("name"), vid)
    return {
        "profile_id": profile_id,
        "vantage_id": vid,
        "name": name,
        "state": state,
        "is_default": bool(profile_id and default_id and profile_id == default_id),
        "source_updated_at": profile.get("updated_at"),
    }


@app.post("/vantages/sync")
async def vantages_sync(req: VantageSyncReq, request: Request):
    """
    Mirror Supabase Vantage presets into Brains/Postgres.

    Supabase remains the account/settings source.
    This table gives Brains background jobs a local registry of saved vantages.
    """
    user_id = require_actor_matches_owner(request, req.user_id)

    default_id = (req.defaultId or "").strip() or None
    active = req.active if isinstance(req.active, dict) else {}
    active_vid = _clean_vantage_id(active.get("vantageId") or active.get("vantage_id") or "")
    active_state = active.get("state") if isinstance(active.get("state"), dict) else active

    sync_mode = str(req.mode or "full").strip().lower()
    if sync_mode not in ("full", "active"):
        return JSONResponse({"status": "bad_request", "detail": "mode must be full or active"}, status_code=400)

    rows = []
    seen = set()

    for p in (req.profiles or []):
        if not isinstance(p, dict):
            continue
        row = _profile_to_registry_row(p, default_id)
        key = row["vantage_id"]
        seen.add(key)
        rows.append(row)

    # If active vantage is not in saved profiles, mirror it as an active ad-hoc row.
    if active_vid and active_vid not in seen:
        rows.append({
            "profile_id": None,
            "vantage_id": active_vid,
            "name": active_vid,
            "state": active_state if isinstance(active_state, dict) else {},
            "is_default": False,
            "source_updated_at": req.source_updated_at,
        })
        seen.add(active_vid)

    conn = await asyncpg.connect(DSN)
    try:
        await _ensure_vantage_profile_registry(conn)

        if sync_mode == "active":
            if not active_vid:
                return JSONResponse({"status": "bad_request", "detail": "missing active vantage"}, status_code=400)

            active_state_json = json.dumps(active_state if isinstance(active_state, dict) else {}, ensure_ascii=False)

            async with conn.transaction():
                await conn.execute("""
                  UPDATE vantage_profile.registry
                     SET is_active=false,
                         updated_at=now()
                   WHERE user_id=$1
                     AND source='supabase'
                """, user_id)

                await conn.execute("""
                  INSERT INTO vantage_profile.registry(
                    user_id, vantage_id, profile_id, name,
                    is_default, is_active, state,
                    source, source_updated_at, last_applied_at
                  )
                  VALUES(
                    $1, $2, NULL, $2,
                    false, true, $3::jsonb,
                    'supabase', NULLIF($4::text, '')::timestamptz, now()
                  )
                  ON CONFLICT (user_id, vantage_id)
                  DO UPDATE SET
                    is_active=true,
                    state=EXCLUDED.state,
                    source='supabase',
                    source_updated_at=COALESCE(EXCLUDED.source_updated_at, vantage_profile.registry.source_updated_at),
                    last_applied_at=now(),
                    updated_at=now()
                """, user_id, active_vid, active_state_json, req.source_updated_at)

            items = await _fetch_vantage_registry_items(conn, user_id)
            return {"status": "ok", "mode": "active", "user_id": user_id, "count": len(items), "items": items}

        async with conn.transaction():
            # Reset flags for this user's mirrored rows before setting current values.
            await conn.execute("""
              UPDATE vantage_profile.registry
                 SET is_default=false,
                     is_active=false,
                     updated_at=now()
               WHERE user_id=$1
                 AND source='supabase'
            """, user_id)

            for row in rows:
                state_json = json.dumps(row["state"] or {}, ensure_ascii=False)
                row_source_updated = row.get("source_updated_at") or req.source_updated_at
                is_active = row["vantage_id"] == active_vid

                await conn.execute("""
                  INSERT INTO vantage_profile.registry(
                    user_id, vantage_id, profile_id, name,
                    is_default, is_active, state,
                    source, source_updated_at, last_applied_at
                  )
                  VALUES(
                    $1, $2, $3, $4,
                    $5, $6, $7::jsonb,
                    'supabase', NULLIF($8::text, '')::timestamptz,
                    CASE WHEN $6 THEN now() ELSE NULL END
                  )
                  ON CONFLICT (user_id, vantage_id)
                  DO UPDATE SET
                    profile_id=EXCLUDED.profile_id,
                    name=EXCLUDED.name,
                    is_default=EXCLUDED.is_default,
                    is_active=EXCLUDED.is_active,
                    state=EXCLUDED.state,
                    source=EXCLUDED.source,
                    source_updated_at=EXCLUDED.source_updated_at,
                    last_applied_at=CASE
                      WHEN EXCLUDED.is_active THEN now()
                      ELSE vantage_profile.registry.last_applied_at
                    END,
                    updated_at=now()
                """,
                    user_id,
                    row["vantage_id"],
                    row["profile_id"],
                    row["name"],
                    bool(row["is_default"]),
                    bool(is_active),
                    state_json,
                    row_source_updated,
                )

            # Mirror semantics: remove Supabase rows no longer present in the saved/active payload.
            if seen:
                await conn.execute("""
                  DELETE FROM vantage_profile.registry
                   WHERE user_id=$1
                     AND source='supabase'
                     AND NOT (vantage_id = ANY($2::text[]))
                """, user_id, list(seen))

        items = await _fetch_vantage_registry_items(conn, user_id)
        return {"status": "ok", "mode": "full", "user_id": user_id, "count": len(items), "items": items}
    finally:
        await conn.close()


@app.get("/vantages/{user_id}")
async def vantages_list(user_id: str, req: Request):
    """
    List mirrored Vantage registry rows for a user.
    """
    uid = require_actor_matches_owner(req, user_id)

    conn = await asyncpg.connect(DSN)
    try:
        await _ensure_vantage_profile_registry(conn)
        items = await _fetch_vantage_registry_items(conn, uid)
        return {"status": "ok", "user_id": uid, "count": len(items), "items": items}
    finally:
        await conn.close()

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
