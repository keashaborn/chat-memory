import os, time, hmac, json
from fastapi import FastAPI, Request
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
from seebx.capabilities.conversation.attachment_routes import (
    router as conversation_attachment_router,
)
from seebx.capabilities.conversation.thread_routes import (
    create_thread_lifecycle_router,
)
from seebx.capabilities.conversation.erasure_routes import (
    create_conversation_erasure_router,
)
from seebx.capabilities.conversation.transcript_routes import (
    create_transcript_ingest_router,
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


from seebx.capabilities.voice.synthesis import router as voice_tts_router
from seebx.capabilities.voice.transcription import (
    router as voice_transcription_router,
)
from seebx.capabilities.voice.realtime_preview import (
    router as voice_realtime_preview_router,
)
from seebx.capabilities.voice.session import router as voice_session_router
from seebx.core.ownership import require_actor_matches_owner
from seebx.core.request_ids import get_request_id
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
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = get_request_id(request)
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
        rid = get_request_id(request)
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
        rid = get_request_id(request)
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


@app.get("/openapi.json", include_in_schema=False)
async def openapi_json():
    return get_openapi(title="Brains API", version="1.0.0", routes=app.routes)

# ---------- persistent chat memory ----------
app.include_router(conversation_attachment_router)


app.include_router(create_transcript_ingest_router(POSTGRES))


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
