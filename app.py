from typing import Any, Dict, List, Literal, Optional
import os, time, uuid, hashlib, hmac, asyncpg, json
import asyncio
import socket
from datetime import datetime
from fastapi import FastAPI, Body, Request
from fastapi.responses import JSONResponse, Response
from fastapi.openapi.utils import get_openapi
from qdrant_client import QdrantClient
from rag_engine.qdrant_compat import make_qdrant_client
from qdrant_client.http import models as qmodels
from openai import OpenAI
from pydantic import BaseModel
from rag_engine.vantage_router import router as vantage_router
from rag_engine.resse_response_router import router as resse_response_router
from rag_engine.trusted_web_router import router as trusted_web_router
from rag_engine.telemetry_router import router as telemetry_router
from rag_engine.lifeswitch_meals_router import router as lifeswitch_meals_router
from rag_engine.lifeswitch_nutrition_log_router import router as lifeswitch_nutrition_log_router
from rag_engine.lifeswitch_nutrition_router import router as lifeswitch_nutrition_router
from rag_engine.lifeswitch_nutrition_log_batch_router import router as lifeswitch_nutrition_log_batch_router
from rag_engine.lifeswitch_training_router import router as lifeswitch_training_router
from rag_engine.lifeswitch_plan_router import router as lifeswitch_plan_router
from lifeswitch_agentic.app_adapter import create_lifeswitch_plan_app_router
from rag_engine.lifeswitch_measurements_router import router as lifeswitch_measurements_router
from rag_engine.lifeswitch_people_router import router as lifeswitch_people_router
from rag_engine.catalog_router import router as catalog_router
from rag_engine.vb_tagging import infer_vb_tags
class NewThreadReq(BaseModel):
    user_id: str
    title: Optional[str] = None
    vantage_id: Optional[str] = "default"
from rag_engine.voice_tts_router import router as voice_tts_router
from rag_engine.voice_transcription_router import router as voice_transcription_router
from rag_engine.voice_realtime_preview_router import (
    router as voice_realtime_preview_router,
)
from rag_engine.voice_session_router import (
    require_active_voice_session,
    router as voice_session_router,
)
from rag_engine.voice_observability_v1 import voice_turn_id_from_request
from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.raw_memory_ownership import (
    RawMemoryOwnershipError,
    assert_raw_payload_owner,
    assert_raw_points_owner,
    owned_raw_payload,
)
from rag_engine.thread_deletion_v1 import (
    ThreadDeletionV1Error,
    delete_thread_v1,
)
from rag_engine.thread_title_v1 import (
    generate_semantic_title,
    select_first_meaningful_exchange,
)
from scripts.review_promotion_plan import build_personal_event_promotion_preview


app = FastAPI(title="Brains API", version="1.0.0")
app.include_router(vantage_router, prefix="/vantage")
app.include_router(resse_response_router, prefix="/response")
app.include_router(trusted_web_router, prefix="/trusted-web")
app.include_router(telemetry_router)
app.include_router(lifeswitch_nutrition_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_meals_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_nutrition_log_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_nutrition_log_batch_router, prefix="/lifeswitch/nutrition")
app.include_router(catalog_router, prefix="/catalog")
app.include_router(lifeswitch_training_router, prefix="/lifeswitch/training")
app.include_router(lifeswitch_plan_router, prefix="/lifeswitch/plan")
app.include_router(
    create_lifeswitch_plan_app_router(
        dsn=os.environ["POSTGRES_DSN"],
        people_schema=os.getenv("LIFESWITCH_PEOPLE_SCHEMA", "lifeswitch_people"),
        legacy_plan_schema=os.getenv("LIFESWITCH_PLAN_SCHEMA", "lifeswitch_plan"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        plan_recommendation_model=(
            os.getenv("LIFESWITCH_PLAN_MODEL")
            or os.getenv("OPENAI_CHAT_MODEL")
            or os.getenv("VANTAGE_MODEL")
            or "gpt-5.2"
        ),
    ),
    prefix="/lifeswitch/plan",
)
app.include_router(lifeswitch_measurements_router, prefix="/lifeswitch/measurements")
app.include_router(lifeswitch_people_router, prefix="/lifeswitch/people")
app.include_router(voice_tts_router)
app.include_router(voice_transcription_router)
app.include_router(voice_realtime_preview_router)
app.include_router(voice_session_router)

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


SENSITIVE_NO_STORE_PREFIXES = (
    "/voice/",
    "/telemetry/",
    "/metrics/",
    "/trusted-web/",
)
SENSITIVE_NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


@app.middleware("http")
async def sensitive_no_store_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(SENSITIVE_NO_STORE_PREFIXES):
        for name, value in SENSITIVE_NO_STORE_HEADERS.items():
            response.headers[name] = value
    return response


def parse_uuid(s: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(s))
    except Exception:
        return None


async def _set_connection_actor(
    conn: asyncpg.Connection,
    owner_user_id: str | uuid.UUID,
) -> str:
    owner = parse_uuid(str(owner_user_id))
    if owner is None:
        raise ValueError("owner_user_id must be a UUID")
    canonical = str(owner)
    await conn.execute("SELECT set_config('app.user_id', $1, false)", canonical)
    return canonical


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
    Memory ownership is the exact authenticated Supabase UUID. Legacy Vantage
    aliases are not owners and are never resolved here.
    """
    actor = _actor_user_id(req)
    if not actor:
        return _actor_missing_response(), None

    actor_uuid = parse_uuid(actor)
    requested_uuid = parse_uuid(requested_user_id)
    if actor_uuid is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_actor_user_id"},
            status_code=400,
        ), None
    if requested_uuid is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_owner_user_id"},
            status_code=400,
        ), None
    if actor_uuid != requested_uuid:
        return _owner_mismatch_response(), str(requested_uuid)

    return None, str(requested_uuid)


async def _require_actor_for_thread(req: Request, thread_id: uuid.UUID):
    """
    Require the requested thread row to belong to x-vs-actor-user-id.
    Returns (response_or_none, actor_user_id).
    """
    actor = _actor_user_id(req)
    if not actor:
        return _actor_missing_response(), None

    actor_uuid = parse_uuid(actor)
    if actor_uuid is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_actor_user_id"},
            status_code=400,
        ), None

    conn = await asyncpg.connect(DSN)
    try:
        await _set_connection_actor(conn, actor_uuid)
        found = await conn.fetchval(
            "SELECT 1 FROM threads WHERE id=$1 AND owner_user_id=$2",
            thread_id,
            actor_uuid,
        )
    finally:
        await conn.close()

    if not found:
        return JSONResponse(
            {"status": "not_found", "detail": "thread_not_found"},
            status_code=404,
        ), None

    return None, str(actor_uuid)


# single global qdrant client
qdrant_client = None

DSN = os.environ["POSTGRES_DSN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# --- defaults from environment ---
DEFAULT_COLLECTION = os.environ.get("RETRIEVAL_COLLECTION", "fm_canon_v1")
EMBED_MODEL       = os.environ.get("EMBED_MODEL", "text-embedding-3-large")
QDRANT_URL        = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()[:16]

@app.get("/openapi.json", include_in_schema=False)
async def openapi_json():
    return get_openapi(title="Brains API", version="1.0.0", routes=app.routes)

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
    voice_turn_id = voice_turn_id_from_request(req)
    if voice_turn_id is not None:
        await require_active_voice_session(req, user_id_alias)
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

        card_payload = owned_raw_payload(user_id, {
            "text": f"The user's preferred name is {full_name}.",
            "user_id_alias": user_id_alias,
            "source": "memory_card",
            "tags": ["summary", "card", "user_identity"],
            "kind": "user_identity",
            "topic_key": "__singleton__", "base_importance": 0.9,
            "created_at": created,
            "updated_at": created,
        })

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
        await _set_connection_actor(conn, user_id)

        # If thread_id was provided but the thread row doesn't exist (or belongs to another user),
        # fix it so the sidebar can show the thread.
        if thread_id:
            thread_row = await conn.fetchrow(
                "SELECT owner_user_id FROM threads WHERE id=$1 AND owner_user_id=$2",
                thread_id,
                user_id,
            )

            if thread_row is None:
                # Create the thread with the provided id so the transcript is attached.
                await conn.execute(
                    "INSERT INTO threads(id, owner_user_id, user_id, title) VALUES($1, $2, $3, $4)",
                    thread_id, user_id, user_id, "New chat"
                )
            elif str(thread_row["owner_user_id"] or "") != str(user_id):
                # Never attach a message to an unowned, legacy, or foreign thread.
                thread_id = None

        await conn.execute(
            "INSERT INTO chat_log("
            "id,owner_user_id,user_id,user_id_alias,source,text,tags,thread_id,vantage_id,request_id,created_at"
            ") VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            rec_id, user_id, user_id, user_id_alias, source, text, tags, thread_id, vantage_id, request_id, created_dt
        )

        # Touch thread timestamp so list ordering works
        if thread_id:
            await conn.execute(
                "UPDATE threads SET updated_at=now() WHERE id=$1 AND owner_user_id=$2",
                thread_id, user_id
            )

    except Exception as e:
        print("pg error:", e)
        return JSONResponse(
            {"status": "unavailable", "detail": "transcript_write_failed"},
            status_code=503,
        )
    finally:
        if conn:
            await conn.close()

    # 2) Embed + upsert into Qdrant (best-effort)
    if client:
        try:
            emb = client.embeddings.create(model=EMBED_MODEL, input=text)
            vec = emb.data[0].embedding

            payload = owned_raw_payload(user_id, {
                "text": text,
                "request_id": request_id,
                "user_id_alias": user_id_alias,
                "source": source,
                "tags": tags,
                "thread_id": str(thread_id) if thread_id else None,
                "vantage_id": vantage_id,
                "created_at": created,
                "updated_at": created,
            })

            qpoint = qmodels.PointStruct(id=rec_id, vector=vec, payload=payload)
            get_qdrant().upsert(collection_name="memory_raw", points=[qpoint])
        except Exception as e:
            # Don't fail the request if Qdrant/OpenAI is down; Postgres transcript is authoritative.
            print("qdrant upsert error:", e)
    else:
        print("log_chat: OPENAI_API_KEY missing; skipping Qdrant upsert")

    return {"status": "ok", "id": rec_id, "request_id": request_id}

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
        await _set_connection_actor(conn, user_id)
        row = await conn.fetchrow(
            "INSERT INTO threads(owner_user_id, user_id, title) VALUES ($1,$2,$3) RETURNING id, title, updated_at",
            user_id, user_id, title
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
        await _set_connection_actor(conn, user_id)
        rows = await conn.fetch(
            "SELECT id, title, updated_at FROM threads WHERE owner_user_id=$1 AND archived=false ORDER BY updated_at DESC",
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
        await _set_connection_actor(conn, _actor_uid)
        rows = await conn.fetch(
            """
            SELECT id, source, text, created_at
            FROM chat_log
            WHERE owner_user_id=$1 AND thread_id=$2
            ORDER BY created_at ASC
            LIMIT $3
            """,
            _actor_uid,
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
        await _set_connection_actor(conn, _actor_uid)
        async with conn.transaction():
            target = await conn.fetchrow(
                """
                SELECT id, source, created_at
                FROM chat_log
                WHERE owner_user_id=$1 AND thread_id=$2 AND id=$3
                """,
                _actor_uid,
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
                WHERE owner_user_id=$1
                  AND thread_id=$2
                  AND created_at >= $3
                """,
                _actor_uid,
                tid,
                target["created_at"],
            )

            await conn.execute(
                "UPDATE threads SET updated_at=now() WHERE owner_user_id=$1 AND id=$2",
                _actor_uid,
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
    title_source: Literal["automatic", "manual"] = "manual"

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
        await _set_connection_actor(conn, _actor_uid)
        if body.title_source == "automatic":
            return JSONResponse(
                {
                    "status": "conflict",
                    "detail": "automatic_title_is_backend_owned",
                },
                status_code=409,
            )

        updated = await conn.fetchrow(
            """
            UPDATE threads
            SET title=$1, title_source='manual', updated_at=now()
            WHERE owner_user_id=$2 AND id=$3
            RETURNING title, title_source
            """,
            title, _actor_uid, tid
        )
        if not updated:
            return JSONResponse(
                {"status": "not_found", "detail": "thread not found"},
                status_code=404,
            )
        return {
            "status": "ok",
            "thread_id": str(tid),
            "title": updated["title"],
            "title_source": updated["title_source"],
            "updated": True,
        }
    finally:
        await conn.close()


@app.post("/threads/{thread_id}/auto-title")
async def threads_auto_title(thread_id: str, req: Request):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_thread_id"},
            status_code=400,
        )

    actor_err, actor_uid = await _require_actor_for_thread(req, tid)
    if actor_err:
        return actor_err

    conn = await asyncpg.connect(DSN)
    try:
        await _set_connection_actor(conn, actor_uid)
        current = await conn.fetchrow(
            """
            SELECT title, title_source
            FROM threads
            WHERE owner_user_id=$1 AND id=$2
            """,
            actor_uid,
            tid,
        )
        if not current:
            return JSONResponse(
                {"status": "not_found", "detail": "thread_not_found"},
                status_code=404,
            )
        if current["title_source"] != "placeholder":
            return {
                "status": "ok",
                "thread_id": str(tid),
                "title": current["title"],
                "title_source": current["title_source"],
                "updated": False,
                "skipped": f"{current['title_source']}_title_preserved",
            }

        transcript = await conn.fetch(
            """
            SELECT source, text, created_at, id
            FROM chat_log
            WHERE owner_user_id=$1 AND thread_id=$2
            ORDER BY created_at ASC, id ASC
            LIMIT 40
            """,
            actor_uid,
            tid,
        )
    finally:
        await conn.close()

    exchange = select_first_meaningful_exchange(transcript)
    if exchange is None:
        return {
            "status": "ok",
            "thread_id": str(tid),
            "title": current["title"],
            "title_source": "placeholder",
            "updated": False,
            "skipped": "no_meaningful_exchange",
        }

    if client is None:
        return JSONResponse(
            {"status": "unavailable", "detail": "title_generation_unavailable"},
            status_code=503,
        )

    title_model = (
        os.getenv("THREAD_TITLE_MODEL")
        or "gpt-4.1-mini"
    ).strip()
    try:
        title = await asyncio.wait_for(
            asyncio.to_thread(
                generate_semantic_title,
                client,
                title_model,
                exchange[0],
                exchange[1],
            ),
            timeout=12.0,
        )
    except Exception:
        print(
            "[threads_auto_title] generation unavailable",
            str(getattr(req.state, "request_id", "")),
        )
        return JSONResponse(
            {"status": "unavailable", "detail": "title_generation_unavailable"},
            status_code=503,
        )

    if not title:
        return {
            "status": "ok",
            "thread_id": str(tid),
            "title": current["title"],
            "title_source": "placeholder",
            "updated": False,
            "skipped": "no_meaningful_exchange",
        }

    conn = await asyncpg.connect(DSN)
    try:
        await _set_connection_actor(conn, actor_uid)
        updated = await conn.fetchrow(
            """
            UPDATE threads
            SET title=$1, title_source='automatic', updated_at=now()
            WHERE owner_user_id=$2
              AND id=$3
              AND title_source='placeholder'
            RETURNING title, title_source
            """,
            title,
            actor_uid,
            tid,
        )
        if updated:
            return {
                "status": "ok",
                "thread_id": str(tid),
                "title": updated["title"],
                "title_source": updated["title_source"],
                "updated": True,
            }

        current = await conn.fetchrow(
            """
            SELECT title, title_source
            FROM threads
            WHERE owner_user_id=$1 AND id=$2
            """,
            actor_uid,
            tid,
        )
        if not current:
            return JSONResponse(
                {"status": "not_found", "detail": "thread_not_found"},
                status_code=404,
            )
        return {
            "status": "ok",
            "thread_id": str(tid),
            "title": current["title"],
            "title_source": current["title_source"],
            "updated": False,
            "skipped": f"{current['title_source']}_title_preserved",
        }
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
        await _set_connection_actor(conn, _actor_uid)
        await conn.execute(
            "UPDATE threads SET archived=true, updated_at=now() WHERE owner_user_id=$1 AND id=$2",
            _actor_uid, tid
        )
        return {"status": "ok", "thread_id": str(tid), "archived": True}
    finally:
        await conn.close()


@app.delete("/threads/{thread_id}")
async def threads_delete(thread_id: str, req: Request):
    tid = parse_uuid(thread_id)
    if not tid:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_thread_id"},
            status_code=400,
        )

    actor_err, _actor_uid = await _require_actor_for_thread(req, tid)
    if actor_err:
        return actor_err

    conn = await asyncpg.connect(DSN)
    try:
        result = await delete_thread_v1(
            conn,
            get_qdrant(),
            owner_user_id=_actor_uid,
            thread_id=tid,
        )
    except ThreadDeletionV1Error as exc:
        if exc.code == "thread_not_found":
            return JSONResponse(
                {"status": "not_found", "detail": "thread_not_found"},
                status_code=404,
            )
        status_code = 503 if exc.retryable else 409
        return JSONResponse(
            {
                "status": "retry_required" if exc.retryable else "conflict",
                "detail": exc.code,
                "thread_id": str(tid),
                "deleted": False,
            },
            status_code=status_code,
        )
    except Exception:
        print(
            "[threads_delete] deletion contract failed",
            str(getattr(req.state, "request_id", "")),
        )
        return JSONResponse(
            {
                "status": "retry_required",
                "detail": "thread_deletion_failed",
                "thread_id": str(tid),
                "deleted": False,
            },
            status_code=503,
        )
    finally:
        await conn.close()

    payload = result.as_dict()
    payload["deleted"] = True
    return payload


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
    "persona_profile",
    "preference_profile",
]
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
            qmodels.FieldCondition(key="owner_user_id", match=qmodels.MatchValue(value=uid)),
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
    assert_raw_points_owner(points or [], uid)

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
    if old:
        assert_raw_payload_owner(old, uid)
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

    payload = owned_raw_payload(uid, {
        "vantage_id": vid,
        "kind": kind,
        "topic_key": topic_key,
        "source": "memory_card",
        "tags": (req.tags if req.tags is not None else (old.get("tags") or ["card", kind])),
        "base_importance": float(req.base_importance) if req.base_importance is not None else float(old.get("base_importance") or 0.7),
        "created_at": created,
        "updated_at": now,
        "text": (req.text if req.text is not None else (old.get("text") or "")),
    })

    # Merge extra fields (non-destructive to identity fields)
    extra = req.payload or {}
    for k, v in extra.items():
        if k in ("owner_user_id", "user_id", "kind", "topic_key", "source", "created_at"):
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
    try:
        assert_raw_payload_owner(payload, uid)
    except RawMemoryOwnershipError:
        return JSONResponse({"status":"forbidden","detail":"owner_mismatch"}, status_code=403)


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

async def _has_governed_memory(conn: asyncpg.Connection, owner_user_id: str) -> bool:
    async with conn.transaction(readonly=True):
        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)",
            owner_user_id,
        )
        return bool(
            await conn.fetchval(
                """
                SELECT
                  EXISTS(SELECT 1 FROM memory.evidence WHERE owner_user_id=$1)
                  OR EXISTS(SELECT 1 FROM memory.claim WHERE owner_user_id=$1)
                  OR EXISTS(SELECT 1 FROM memory.preference WHERE owner_user_id=$1)
                  OR EXISTS(SELECT 1 FROM memory.project_space WHERE owner_user_id=$1)
                  OR EXISTS(SELECT 1 FROM memory.consolidation_job WHERE owner_user_id=$1)
                """,
                owner_user_id,
            )
        )


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
            await _set_connection_actor(conn, uid)
            if await _has_governed_memory(conn, uid):
                return JSONResponse(
                    {
                        "status": "conflict",
                        "detail": "governed_account_erasure_required",
                    },
                    status_code=409,
                )
            pg_chat = await conn.execute("DELETE FROM chat_log WHERE owner_user_id=$1", uid)
            pg_threads = await conn.execute("DELETE FROM threads WHERE owner_user_id=$1", uid)
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
                            key="owner_user_id",
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
            await _set_connection_actor(conn, uid)
            rows = await conn.fetch(
                "SELECT id FROM chat_log WHERE owner_user_id=$1 AND created_at >= $2",
                uid, cutoff
            )
            ids = [str(r["id"]) for r in (rows or [])]

            if ids:
                async with conn.transaction(readonly=True):
                    await conn.execute(
                        "SELECT set_config('app.user_id', $1, true)",
                        uid,
                    )
                    governed = await conn.fetchval(
                        """
                        SELECT
                          EXISTS(
                            SELECT 1 FROM memory.consolidation_job
                            WHERE owner_user_id=$1
                              AND source_system='public.chat_log'
                              AND source_external_id=ANY($2::text[])
                          )
                          OR EXISTS(
                            SELECT 1 FROM memory.evidence
                            WHERE owner_user_id=$1
                              AND source_system='public.chat_log'
                              AND external_id=ANY($3::text[])
                          )
                        """,
                        uid,
                        ids,
                        [f"chat_log:{record_id}" for record_id in ids],
                    )
                if governed:
                    return JSONResponse(
                        {
                            "status": "conflict",
                            "detail": "governed_recent_erasure_required",
                        },
                        status_code=409,
                    )

            pg_del = await conn.execute(
                "DELETE FROM chat_log WHERE owner_user_id=$1 AND created_at >= $2",
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
            existing = qdrant.retrieve(
                collection_name="memory_raw",
                ids=batch,
                with_payload=True,
                with_vectors=False,
            )
            assert_raw_points_owner(existing or [], uid)
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
            await _set_connection_actor(conn, uid)
            threads = await conn.fetch(
                "SELECT id, title, created_at, updated_at, archived FROM threads WHERE owner_user_id=$1 ORDER BY updated_at DESC",
                uid
            )
            messages = await conn.fetch(
                "SELECT id, thread_id, source, text, tags, created_at FROM chat_log WHERE owner_user_id=$1 ORDER BY created_at ASC LIMIT $2",
                uid, limit
            )
        finally:
            await conn.close()
    except Exception as e:
        return JSONResponse({"status":"error","detail":f"pg_export_failed: {e}"}, status_code=500)

    # Cards from Qdrant (same kinds list as /cards)
    card_kinds = CARD_KINDS_DEFAULT if "CARD_KINDS_DEFAULT" in globals() else [
        "user_identity","persona_profile","style_profile","preference_profile"
    ]

    cards = []
    try:
        qdrant = get_qdrant()
        flt = qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="owner_user_id", match=qmodels.MatchValue(value=uid)),
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
        assert_raw_points_owner(points or [], uid)
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
