from typing import Any, Dict, List, Literal, Optional
import os, time, uuid, hashlib, hmac, asyncpg, json
import asyncio
import socket
from datetime import datetime
from fastapi import FastAPI, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.openapi.utils import get_openapi
from openai import OpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from seebx.capabilities.conversation.zep_runtime import (
    ZEP_PROMPT_SETTINGS,
    ZEP_MEMORY_RUNTIME,
)
from seebx.capabilities.conversation.router import (
    router as conversation_router,
)
from rag_engine.zep_shadow_memory_v1 import ZepShadowConfigurationError
from seebx.capabilities.search.trusted_health import router as trusted_web_router
from seebx.capabilities.search.current_news import router as current_news_router
from seebx.capabilities.search.execution import (
    router as search_execution_router_v1,
)
from rag_engine.telemetry_router import router as telemetry_router
from rag_engine.lifeswitch_meals_router import router as lifeswitch_meals_router
from rag_engine.lifeswitch_nutrition_log_router import router as lifeswitch_nutrition_log_router
from rag_engine.lifeswitch_nutrition_router import router as lifeswitch_nutrition_router
from rag_engine.lifeswitch_nutrition_log_batch_router import router as lifeswitch_nutrition_log_batch_router
from rag_engine.lifeswitch_training_router import router as lifeswitch_training_router
from rag_engine.lifeswitch_measurements_router import router as lifeswitch_measurements_router
from rag_engine.lifeswitch_plan_router import router as lifeswitch_plan_router
from rag_engine.lifeswitch_account_timezone_router_v1 import (
    router as lifeswitch_account_timezone_router_v1,
)
from rag_engine.catalog_router import router as catalog_router
from rag_engine.vb_tagging import infer_vb_tags
from seebx.capabilities.conversation.attachment_routes import (
    router as conversation_attachment_router,
)
from seebx.capabilities.conversation.attachments import (
    MAX_ATTACHMENT_COUNT,
)
from seebx.contracts.identifiers import CanonicalJsonUUID
from seebx.adapters.conversation_history import fetch_thread_message_rows
from seebx.adapters.conversation_persistence import (
    UserTranscriptPersistenceError,
    persist_user_transcript,
)
from seebx.adapters.conversation_threads import (
    archive_thread,
    create_thread,
    fetch_thread_title_state,
    fetch_thread_title_transcript,
    list_visible_threads,
    rename_thread_manual,
    set_thread_pinned,
    thread_belongs_to_owner,
    update_thread_automatic_title,
)


class NewThreadReq(BaseModel):
    user_id: str
    title: Optional[str] = None
    vantage_id: Optional[str] = "default"

class PinThreadReq(BaseModel):
    pinned: bool

class ActiveThreadReq(BaseModel):
    user_id: str
    thread_id: str


class ChatHistoryClearReq(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scope: Literal["all", "recent", "thread", "message_tail"]
    thread_id: Optional[CanonicalJsonUUID] = None
    anchor_message_id: Optional[CanonicalJsonUUID] = None
    recent_window_seconds: Optional[int] = None
    confirmation: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def exact_scope(self) -> "ChatHistoryClearReq":
        expected_confirmation = {
            "all": "CLEAR CHAT HISTORY",
            "recent": "CLEAR RECENT CHAT HISTORY",
            "thread": "CLEAR CHAT",
            "message_tail": "CLEAR MESSAGE TAIL",
        }[self.scope]
        if self.confirmation != expected_confirmation:
            raise ValueError("invalid confirmation")
        if self.scope == "thread":
            if (
                self.thread_id is None
                or self.anchor_message_id is not None
                or self.recent_window_seconds is not None
            ):
                raise ValueError("invalid thread clear shape")
        elif self.scope == "message_tail":
            if (
                self.thread_id is None
                or self.anchor_message_id is None
                or self.recent_window_seconds is not None
            ):
                raise ValueError("invalid message tail clear shape")
        elif self.scope == "recent":
            if (
                self.thread_id is not None
                or self.anchor_message_id is not None
                or self.recent_window_seconds
                not in {3_600, 86_400, 604_800, 2_592_000}
            ):
                raise ValueError("invalid recent clear shape")
        elif (
            self.thread_id is not None
            or self.anchor_message_id is not None
            or self.recent_window_seconds is not None
        ):
            raise ValueError("invalid all clear shape")
        return self


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
from seebx.adapters.thread_selection import (
    ActiveThreadSelectionV1Error,
    clear_active_thread_v1,
    get_active_thread_v1,
    select_active_thread_v1,
)
from rag_engine.chat_history_clear_v1 import (
    ChatHistoryClearError,
    clear_chat_history_v1,
)
from rag_engine.thread_title_v1 import (
    generate_semantic_title,
    select_first_meaningful_exchange,
)
from seebx.contracts.conversation import WEB_ASSISTANT_SOURCE
from rag_engine.admin_ai_operations_v1 import (
    AiOperationsError,
    acknowledge_admin_ai_operations_incident_v1,
    list_admin_ai_operations_incidents_v1,
    resolve_admin_ai_operations_incident_v1,
)
SUCCESSOR_MEMORY_REFUSAL_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


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
app.include_router(voice_tts_router)
app.include_router(voice_transcription_router)
app.include_router(voice_realtime_preview_router)
app.include_router(voice_session_router)


def _legacy_memory_retired(operation: str) -> JSONResponse:
    return JSONResponse(
        {
            "status": "conflict",
            "detail": "legacy_memory_surface_retired",
            "operation": operation,
        },
        status_code=409,
    )


def _conversation_erasure_required(
    operation: str,
    selector_kind: str,
) -> JSONResponse:
    return JSONResponse(
        {
            "status": "conflict",
            "detail": "legacy_conversation_deletion_route_retired",
            "operation": operation,
            "selector_kind": selector_kind,
            "canonical_route": (
                "/memory/chat-and-zep/clear"
                if selector_kind == "all_conversations"
                else "/chat-history/clear"
            ),
        },
        status_code=410,
        headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
    )


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
        found = await thread_belongs_to_owner(
            conn,
            owner_user_id=actor_uuid,
            thread_id=thread_id,
        )
    finally:
        await conn.close()

    if not found:
        return JSONResponse(
            {"status": "not_found", "detail": "thread_not_found"},
            status_code=404,
        ), None

    return None, str(actor_uuid)


DSN = os.environ["POSTGRES_DSN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()[:16]

@app.get("/openapi.json", include_in_schema=False)
async def openapi_json():
    return get_openapi(title="Brains API", version="1.0.0", routes=app.routes)

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




# ---------- AI Operations ----------
AI_OPERATIONS_INSPECTION_CAPABILITY = "inspector.view"
AI_OPERATIONS_MANAGEMENT_CAPABILITY = "incident.manage"


def _require_ai_operations_actor(
    req: Request,
    required_capability: str,
):
    actor = _actor_user_id(req)
    if not actor:
        return _actor_missing_response(), None
    actor_uuid = parse_uuid(actor)
    if actor_uuid is None:
        return JSONResponse(
            {"ok": False, "error": "invalid_actor_user_id"},
            status_code=400,
        ), None
    capability = (
        req.headers.get("x-vs-authorized-capability") or ""
    ).strip()
    if not hmac.compare_digest(capability, required_capability):
        return JSONResponse(
            {"ok": False, "error": "capability_required"},
            status_code=403,
        ), None
    return None, str(actor_uuid)


def _ai_operations_error_response(
    req: Request,
    exc: AiOperationsError,
) -> JSONResponse:
    rid = getattr(req.state, "request_id", None) or _get_request_id(req)
    return JSONResponse(
        {
            "ok": False,
            "error": exc.code,
            "request_id": rid,
        },
        status_code=exc.status_code,
        headers={"x-request-id": rid},
    )


@app.get("/admin/ai-operations/incidents")
async def admin_ai_operations_incidents(req: Request):
    denied, actor = _require_ai_operations_actor(
        req,
        AI_OPERATIONS_INSPECTION_CAPABILITY,
    )
    if denied is not None:
        return denied
    params = req.query_params
    state = (params.get("state") or "").strip() or None
    try:
        limit = int(params.get("limit") or 50)
        return await list_admin_ai_operations_incidents_v1(
            dsn=DSN,
            actor_user_id=actor,
            state=state,
            limit=limit,
        )
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "invalid_ai_operations_query"},
            status_code=400,
        )
    except AiOperationsError as exc:
        return _ai_operations_error_response(req, exc)


@app.post(
    "/admin/ai-operations/incidents/{incident_id}/acknowledge"
)
async def admin_ai_operations_acknowledge(
    incident_id: str,
    req: Request,
):
    denied, actor = _require_ai_operations_actor(
        req,
        AI_OPERATIONS_MANAGEMENT_CAPABILITY,
    )
    if denied is not None:
        return denied
    try:
        return await acknowledge_admin_ai_operations_incident_v1(
            dsn=DSN,
            actor_user_id=actor,
            incident_id=incident_id,
        )
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "invalid_incident_id"},
            status_code=400,
        )
    except AiOperationsError as exc:
        return _ai_operations_error_response(req, exc)


@app.post(
    "/admin/ai-operations/incidents/{incident_id}/resolve"
)
async def admin_ai_operations_resolve(
    incident_id: str,
    req: Request,
):
    denied, actor = _require_ai_operations_actor(
        req,
        AI_OPERATIONS_MANAGEMENT_CAPABILITY,
    )
    if denied is not None:
        return denied
    try:
        return await resolve_admin_ai_operations_incident_v1(
            dsn=DSN,
            actor_user_id=actor,
            incident_id=incident_id,
        )
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "invalid_incident_id"},
            status_code=400,
        )
    except AiOperationsError as exc:
        return _ai_operations_error_response(req, exc)


# ---------- admin usage ----------
# ---------- admin memory health ----------
@app.get("/admin/memory/health")
async def admin_memory_health(req: Request):
    return _legacy_memory_retired("admin_memory_health")


@app.get("/admin/memory/workbench")
async def admin_memory_workbench(req: Request):
    return _legacy_memory_retired("admin_memory_workbench")


@app.post("/admin/memory/workbench/feedback")
async def admin_memory_workbench_feedback(
    req: Request,
):
    return _legacy_memory_retired("admin_memory_workbench_feedback")


# ---------- legacy admin memory review ----------
@app.get("/admin/memory/review-plan")
async def admin_memory_review_plan(req: Request):
    return _legacy_memory_retired("admin_memory_review_plan")


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
    conn = None
    try:
        conn = await asyncpg.connect(DSN)
        await _set_connection_actor(conn, user_id)
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
    finally:
        if conn:
            await conn.close()

    response_payload = {
        "status": "ok",
        "id": str(result.message_id),
        "request_id": request_id,
    }
    if result.replayed:
        response_payload["replayed"] = True
    return response_payload

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
        async with conn.transaction():
            row = await create_thread(
                conn,
                owner_user_id=user_id,
                title=title,
            )
            await select_active_thread_v1(
                conn, owner_user_id=user_id, thread_id=row["id"]
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
        rows = await list_visible_threads(
            conn,
            owner_user_id=user_id,
        )
        return [
            {
                "thread_id": str(r["id"]),
                "title": r["title"],
                "updated_at": r["updated_at"].isoformat(),
                "pinned": bool(r["pinned"]),
                "pinned_at": r["pinned_at"].isoformat() if r["pinned_at"] else None,
            }
            for r in rows
        ]
    finally:
        await conn.close()

@app.get("/threads/active/{user_id}")
async def threads_active_get(user_id: str, req: Request, vantage_id: str = "default"):
    user_id_alias = (user_id or "").strip() or "anon"
    actor_err, owner_user_id = await _require_actor_for_user(
        req, user_id_alias, vantage_id
    )
    if actor_err:
        return actor_err

    conn = await asyncpg.connect(DSN)
    try:
        await _set_connection_actor(conn, owner_user_id)
        selected = await get_active_thread_v1(
            conn,
            owner_user_id=owner_user_id,
        )
        return selected or {"thread_id": None}
    finally:
        await conn.close()


@app.post("/threads/active")
async def threads_active_select(body: ActiveThreadReq, req: Request):
    user_id_alias = (body.user_id or "").strip() or "anon"
    actor_err, owner_user_id = await _require_actor_for_user(
        req, user_id_alias
    )
    if actor_err:
        return actor_err

    thread_id = parse_uuid(body.thread_id)
    if thread_id is None:
        return JSONResponse(
            {"status": "bad_request", "detail": "invalid_thread_id"},
            status_code=400,
        )

    conn = await asyncpg.connect(DSN)
    try:
        await _set_connection_actor(conn, owner_user_id)
        try:
            return await select_active_thread_v1(
                conn,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
            )
        except ActiveThreadSelectionV1Error as exc:
            return JSONResponse(
                {"status": "not_found", "detail": exc.code},
                status_code=404,
            )
    finally:
        await conn.close()


@app.delete("/threads/active/{user_id}")
async def threads_active_clear(
    user_id: str,
    req: Request,
    vantage_id: str = "default",
):
    user_id_alias = (user_id or "").strip() or "anon"
    actor_err, owner_user_id = await _require_actor_for_user(
        req, user_id_alias, vantage_id
    )
    if actor_err:
        return actor_err

    conn = await asyncpg.connect(DSN)
    try:
        await _set_connection_actor(conn, owner_user_id)
        await clear_active_thread_v1(
            conn,
            owner_user_id=owner_user_id,
        )
        return {"status": "ok", "thread_id": None}
    finally:
        await conn.close()


@app.post("/chat-history/clear")
async def chat_history_clear(body: ChatHistoryClearReq, req: Request):
    actor = _actor_user_id(req)
    owner_user_id = parse_uuid(actor or "")
    if owner_user_id is None:
        return _actor_missing_response()
    authorization = (req.headers.get("authorization") or "").strip()
    operation_id = (
        body.anchor_message_id
        if body.scope == "message_tail"
        else uuid.uuid4()
    )

    conn = await asyncpg.connect(DSN)
    try:
        result = await clear_chat_history_v1(
            conn,
            owner_user_id=owner_user_id,
            authorization=authorization,
            operation_id=operation_id,
            scope=body.scope,
            thread_id=body.thread_id,
            recent_window_seconds=body.recent_window_seconds,
        )
    except ChatHistoryClearError as exc:
        return JSONResponse(
            {"status": "error", "detail": exc.code},
            status_code=exc.status_code,
            headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
        )
    finally:
        await conn.close()
    return result.as_dict()


@app.delete("/memory/chat-and-zep/clear")
async def chat_and_zep_full_clear(req: Request):
    actor = _actor_user_id(req)
    owner_user_id = parse_uuid(actor or "")
    if owner_user_id is None:
        return _actor_missing_response()
    authorization = (req.headers.get("authorization") or "").strip()
    operation_id = uuid.uuid4()

    try:
        async with ZEP_MEMORY_RUNTIME.owner_erasure_barrier(owner_user_id):
            conn = await asyncpg.connect(DSN)
            try:
                result = await clear_chat_history_v1(
                    conn,
                    owner_user_id=owner_user_id,
                    authorization=authorization,
                    operation_id=operation_id,
                    scope="all",
                )
            finally:
                await conn.close()
            await ZEP_MEMORY_RUNTIME.delete_owner_memory(owner_user_id)
    except ChatHistoryClearError as exc:
        return JSONResponse(
            {"status": "error", "detail": exc.code},
            status_code=exc.status_code,
            headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
        )
    except (ZepShadowConfigurationError, asyncio.TimeoutError):
        return JSONResponse(
            {"status": "error", "detail": "zep_memory_deletion_unavailable"},
            status_code=503,
            headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
        )
    except Exception:
        return JSONResponse(
            {"status": "error", "detail": "full_ai_data_deletion_unavailable"},
            status_code=503,
            headers=SUCCESSOR_MEMORY_REFUSAL_HEADERS,
        )

    return {
        "contract_version": "chat_and_zep_full_clear_v1",
        "status": "completed",
        "operation_id": str(result.operation_id),
        "deleted_message_count": result.deleted_message_count,
        "deleted_thread_count": result.deleted_thread_count,
        "deleted_outbox_count": result.deleted_outbox_count,
        "chat_receipt_sha256": result.receipt_sha256,
        "completed_at": result.completed_at,
        "memory_retained": False,
        "zep_called": True,
        "zep_deleted": True,
    }


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
        rows = await fetch_thread_message_rows(
            conn,
            owner_user_id=_actor_uid,
            thread_id=tid,
            limit=limit,
        )
        out = []
        for r in rows:
            src = (r["source"] or "")
            role = "assistant" if "assistant" in src else "user"
            message = {
                "id": str(r["id"]),
                "role": role,
                "content": r["text"],
                "created_at": r["created_at"].isoformat(),
                "attachments": r["attachments"] or [],
            }
            if src == WEB_ASSISTANT_SOURCE:
                cited = r["cited_sources"] or []
                admitted = r["admitted_sources"] or []
                if isinstance(cited, str):
                    cited = json.loads(cited)
                if isinstance(admitted, str):
                    admitted = json.loads(admitted)
                message.update(
                    {
                        "web_search": True,
                        "trusted_web_sources": cited,
                        "trusted_web_admitted_sources": admitted,
                    }
                )
            out.append(message)
        return out
    finally:
        await conn.close()


@app.delete("/threads/{thread_id}/messages/{message_id}/truncate")
async def threads_truncate_from_message(thread_id: str, message_id: str, req: Request):
    return _conversation_erasure_required(
        "message_tail_delete",
        "message_tail",
    )




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

        updated = await rename_thread_manual(
            conn,
            owner_user_id=_actor_uid,
            thread_id=tid,
            title=title,
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


@app.post("/threads/{thread_id}/pin")
async def threads_pin(thread_id: str, body: PinThreadReq, req: Request):
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
        updated = await set_thread_pinned(
            conn,
            owner_user_id=actor_uid,
            thread_id=tid,
            pinned=body.pinned,
        )
        if not updated:
            return JSONResponse(
                {"status": "not_found", "detail": "thread_not_found"},
                status_code=404,
            )

        pinned_at = updated["pinned_at"]
        return {
            "status": "ok",
            "thread_id": str(tid),
            "pinned": pinned_at is not None,
            "pinned_at": pinned_at.isoformat() if pinned_at else None,
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
        current = await fetch_thread_title_state(
            conn,
            owner_user_id=actor_uid,
            thread_id=tid,
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

        transcript = await fetch_thread_title_transcript(
            conn,
            owner_user_id=actor_uid,
            thread_id=tid,
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
        updated = await update_thread_automatic_title(
            conn,
            owner_user_id=actor_uid,
            thread_id=tid,
            title=title,
        )
        if updated:
            return {
                "status": "ok",
                "thread_id": str(tid),
                "title": updated["title"],
                "title_source": updated["title_source"],
                "updated": True,
            }

        current = await fetch_thread_title_state(
            conn,
            owner_user_id=actor_uid,
            thread_id=tid,
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
        await archive_thread(
            conn,
            owner_user_id=_actor_uid,
            thread_id=tid,
        )
        return {"status": "ok", "thread_id": str(tid), "archived": True}
    finally:
        await conn.close()


@app.delete("/threads/{thread_id}")
async def threads_delete(thread_id: str, req: Request):
    return _conversation_erasure_required("thread_delete", "thread")


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

# ---------- retired legacy Memory compatibility routes ----------
@app.get("/cards/{user_id}")
async def cards_list(user_id: str, req: Request, limit: int = 50, kinds: Optional[str] = None, vantage_id: str = "default"):
    return _legacy_memory_retired("cards_list")

@app.get("/vantage-cards/{user_id}")
async def vantage_cards_list(
    user_id: str,
    req: Request,
    vantage_id: str = "default",
    kinds: Optional[str] = None,
    limit: int = 100,
):
    return _legacy_memory_retired("vantage_cards_list")


@app.post("/cards/{user_id}")
async def cards_upsert(user_id: str, request: Request, vantage_id: str = "default"):
    return _legacy_memory_retired("cards_upsert")

@app.delete("/cards/{user_id}/{card_id}")
async def cards_delete(user_id: str, card_id: str, req: Request, vantage_id: str = "default"):
    return _legacy_memory_retired("cards_delete")

# ---------- security/privacy: delete all user data ----------
@app.delete("/user/{user_id}/data")
async def delete_all_user_data(user_id: str, req: Request):
    return _conversation_erasure_required(
        "delete_all_user_data",
        "all_conversations",
    )

# ---------- security/privacy: export + forget recent ----------
@app.delete("/user/{user_id}/recent")
async def delete_recent_user_data(user_id: str, req: Request, minutes: int = 60):
    return _conversation_erasure_required(
        "delete_recent_user_data",
        "recent",
    )


@app.get("/user/{user_id}/export")
async def export_user_data(user_id: str, req: Request, limit: int = 20000):
    return _legacy_memory_retired("export_user_data")


@app.get("/readyz", include_in_schema=False)
async def readyz():
    """
    Readiness: Postgres connectivity only.
    Avoids OpenAPI generation (currently broken).
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
