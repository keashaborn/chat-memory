from typing import Any, Dict, List, Optional
import os, time, uuid, hashlib, hmac, json
import socket
from datetime import datetime
from fastapi import FastAPI, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.openapi.utils import get_openapi
from openai import OpenAI
from seebx.capabilities.conversation.zep_runtime import (
    ZEP_PROMPT_SETTINGS,
    ZEP_MEMORY_RUNTIME,
)
from seebx.capabilities.conversation.router import (
    router as conversation_router,
)
from seebx.capabilities.search.trusted_health import router as trusted_web_router
from seebx.capabilities.search.current_news import router as current_news_router
from seebx.capabilities.search.execution import (
    router as search_execution_router_v1,
)
from seebx.capabilities.observability.telemetry import router as telemetry_router
from seebx.capabilities.nutrition.meals import router as lifeswitch_meals_router
from seebx.capabilities.nutrition.logs import router as lifeswitch_nutrition_log_router
from seebx.capabilities.nutrition.routes import router as lifeswitch_nutrition_router
from seebx.capabilities.nutrition.batch import router as lifeswitch_nutrition_log_batch_router
from seebx.capabilities.training.routes import router as lifeswitch_training_router
from seebx.capabilities.measurements.routes import router as lifeswitch_measurements_router
from seebx.capabilities.plans.routes import router as lifeswitch_plan_router
from seebx.capabilities.preferences.timezone import (
    router as lifeswitch_account_timezone_router_v1,
)
from seebx.capabilities.catalog.routes import router as catalog_router
from seebx.capabilities.forms.routes import (
    forms_enabled,
    router as forms_router,
)
from seebx.capabilities.conversation.tagging import infer_vb_tags
from seebx.capabilities.conversation.attachment_routes import (
    router as conversation_attachment_router,
)
from seebx.capabilities.conversation.thread_routes import (
    create_thread_lifecycle_router,
)
from seebx.capabilities.conversation.erasure_routes import (
    create_conversation_erasure_router,
)
from seebx.capabilities.conversation.attachments import (
    MAX_ATTACHMENT_COUNT,
)
from seebx.adapters.conversation_erasure import (
    PostgresConversationErasureRepository,
)
from seebx.adapters.conversation_export import (
    PostgresConversationExportRepository,
)
from seebx.capabilities.conversation.export import (
    ConversationExportService,
)
from seebx.capabilities.conversation.export_routes import (
    create_conversation_export_router,
)
from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.adapters.conversation_persistence import (
    UserTranscriptPersistenceError,
    persist_user_transcript,
)


from seebx.capabilities.voice.synthesis import router as voice_tts_router
from seebx.capabilities.voice.transcription import (
    router as voice_transcription_router,
)
from seebx.capabilities.voice.realtime_preview import (
    router as voice_realtime_preview_router,
)
from seebx.capabilities.voice.session import router as voice_session_router
from seebx.core.voice_identity import require_active_voice_session
from seebx.core.voice_observability import voice_turn_id_from_request
from seebx.core.ownership import require_actor_matches_owner
from seebx.core.identity import require_actor
from seebx.capabilities.conversation.erasure import (
    ConversationErasureService,
)
from seebx.capabilities.operations.ai_operations_routes import (
    create_ai_operations_router,
)
app = FastAPI(title="Brains API", version="1.0.0")
app.include_router(conversation_router, prefix="/response")
app.include_router(trusted_web_router, prefix="/trusted-web")
app.include_router(current_news_router, prefix="/current-news")
app.include_router(search_execution_router_v1, prefix="/search")
app.include_router(telemetry_router)
app.include_router(lifeswitch_nutrition_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_meals_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_nutrition_log_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_nutrition_log_batch_router, prefix="/lifeswitch/nutrition")
app.include_router(catalog_router, prefix="/catalog")
app.include_router(lifeswitch_training_router, prefix="/lifeswitch/training")
app.include_router(lifeswitch_plan_router, prefix="/lifeswitch/plan")
app.include_router(lifeswitch_measurements_router, prefix="/lifeswitch/measurements")
app.include_router(
    lifeswitch_account_timezone_router_v1,
    prefix="/lifeswitch/account",
)
if forms_enabled():
    app.include_router(forms_router, prefix="/forms")
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
    "/admin/",
    "/voice/",
    "/telemetry/",
    "/metrics/",
    "/trusted-web/",
    "/threads/active",
    "/attachments",
    "/memory/",
    "/chat-history/",
    "/conversation/",
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


DSN = os.environ["POSTGRES_DSN"]
POSTGRES = PostgresConnectionProvider(DSN)
app.include_router(create_ai_operations_router(DSN))
CONVERSATION_ERASURE = ConversationErasureService(
    repository=PostgresConversationErasureRepository(POSTGRES),
    zep_runtime=ZEP_MEMORY_RUNTIME,
)
CONVERSATION_EXPORT = ConversationExportService(
    repository=PostgresConversationExportRepository(POSTGRES),
    memory_runtime=ZEP_MEMORY_RUNTIME,
)
app.include_router(
    create_conversation_export_router(CONVERSATION_EXPORT)
)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()[:16]

@app.get("/openapi.json", include_in_schema=False)
async def openapi_json():
    return get_openapi(title="Brains API", version="1.0.0", routes=app.routes)

def _vb_source_normalize(source: Optional[str]) -> str:
    """
    Normalize upstream 'source' strings to the canonical labels expected by seebx.capabilities.conversation.tagging.
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
app.include_router(conversation_attachment_router)


@app.post("/log")
async def log_chat(req: Request):
    try:
        body: Dict[str, Any] = await req.json()
    except Exception:
        return JSONResponse({"status":"bad_request","detail":"invalid json"}, status_code=400)

    no_store = body.get("no_store", False)
    if type(no_store) is not bool:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_no_store"},
            status_code=400,
        )
    text = body.get("text") or body.get("input") or ""
    source = body.get("source") or "frontend"
    if source == "frontend/identity" and text.startswith("FULL_NAME:"):
        return JSONResponse(
            {
                "status": "retired",
                "detail": "legacy_identity_memory_retired",
            },
            status_code=410,
        )
    user_id_alias = await require_actor(
        req, body.get("user_id") or ""
    )
    if no_store:
        return {
            "status": "no_store",
            "detail": "transcript_and_memory_not_stored",
        }
    voice_turn_id = voice_turn_id_from_request(req)
    tags = body.get("tags") or []
    vantage_id = (body.get("vantage_id") or "").strip() or "default"
    request_id = _sanitize_request_id(getattr(req.state, "request_id", None)) or str(uuid.uuid4())
    raw_submission_id = body.get("submission_id")
    submission_id = (
        parse_uuid(str(raw_submission_id))
        if raw_submission_id is not None
        else None
    )
    if raw_submission_id is not None and submission_id is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_submission_id"},
            status_code=400,
        )

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

    raw_attachment_ids = body.get("attachment_ids") or []
    if not isinstance(raw_attachment_ids, list) or len(raw_attachment_ids) > MAX_ATTACHMENT_COUNT:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_attachment_ids"},
            status_code=400,
        )
    attachment_ids: list[uuid.UUID] = []
    for raw_attachment_id in raw_attachment_ids:
        parsed_attachment_id = parse_uuid(str(raw_attachment_id))
        if parsed_attachment_id is None:
            return JSONResponse(
                {"status": "bad_request", "detail": "invalid_attachment_ids"},
                status_code=400,
            )
        attachment_ids.append(parsed_attachment_id)
    if len(set(attachment_ids)) != len(attachment_ids) or (attachment_ids and thread_id is None):
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_attachment_ids"},
            status_code=400,
        )

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

    # The transcript is canonical chat history. Zep ingestion is dispatched by
    # the response route after the completed user/assistant turn is persisted.
    rec_id = str(submission_id or uuid.uuid4())
    created_dt = datetime.utcnow()

    # Save to PostgreSQL (authoritative transcript).
    try:
        async with POSTGRES.owner_connection(user_id) as conn:
            result = await persist_user_transcript(
                conn,
                owner_user_id=uuid.UUID(user_id),
                user_id_alias=user_id_alias,
                source=source,
                text=text,
                tags=tags,
                thread_id=thread_id,
                vantage_id=vantage_id,
                request_id=request_id,
                message_id=uuid.UUID(rec_id),
                submission_id=submission_id,
                created_at=created_dt,
                attachment_ids=attachment_ids,
            )
    except UserTranscriptPersistenceError as exc:
        print("pg error:", exc.__cause__ or exc)
        return JSONResponse(
            {
                "status": "conflict" if exc.conflict else "unavailable",
                "detail": exc.code,
            },
            status_code=409 if exc.conflict else 503,
        )
    except Exception as exc:
        print("pg error:", exc)
        return JSONResponse(
            {"status": "unavailable", "detail": "transcript_write_failed"},
            status_code=503,
        )

    response_payload = {
        "status": "ok",
        "id": str(result.message_id),
        "request_id": request_id,
    }
    if result.replayed:
        response_payload["replayed"] = True
    return response_payload

app.include_router(
    create_thread_lifecycle_router(POSTGRES, title_client=client)
)


app.include_router(
    create_conversation_erasure_router(CONVERSATION_ERASURE)
)


@app.get("/healthz")
async def health():
    return {
        "status": "ok",
        "time": time.time(),
        "memory": {
            "provider": "zep",
            "prompt_mode": ZEP_PROMPT_SETTINGS.mode,
            "chat_history_store": "postgres",
            "chat_deletion_retains_memory": True,
            "full_erasure_route": "/memory/chat-and-zep/clear",
            "retired_governed_memory": {
                "capture": "disabled",
                "response_fallback": "disabled",
                "lifecycle_commands": "disabled",
                "erasure_proxy": "disabled",
                "postgres_access": "disabled",
            },
        },
    }

@app.get("/readyz", include_in_schema=False)
async def readyz():
    """
    Readiness: Postgres connectivity only.
    Avoids OpenAPI generation.
    """
    try:
        if await POSTGRES.readiness_value() != 1:
            raise RuntimeError("postgres select 1 failed")
    except Exception as e:
        return JSONResponse({"ok": False, "postgres": str(e)}, status_code=503)
    return {"ok": True, "postgres": True}
