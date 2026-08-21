import os
from fastapi import FastAPI
from seebx.adapters.openai import (
    get_optional_openai_client,
)
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
from seebx.capabilities.training.routes import router as lifeswitch_training_router
from seebx.capabilities.measurements.routes import router as lifeswitch_measurements_router
from seebx.capabilities.plans.routes import router as lifeswitch_plan_router
from seebx.capabilities.preferences.timezone import (
    router as lifeswitch_account_timezone_router_v1,
)
from seebx.capabilities.preferences.assistant_routes import (
    create_assistant_preferences_router,
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
from seebx.adapters.ai_operations_postgres import (
    PostgresAiOperationsRepository,
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
from seebx.core.http_boundary import install_http_boundary
from seebx.capabilities.conversation.erasure import (
    ConversationErasureService,
)
from seebx.capabilities.operations.ai_operations_routes import (
    create_ai_operations_router,
)
from seebx.capabilities.operations.health_routes import (
    create_operational_health_router,
)
app = FastAPI(title="SeeBx API", version="1.0.0")
install_http_boundary(app)
app.include_router(conversation_router, prefix="/response")
app.include_router(trusted_web_router, prefix="/trusted-web")
app.include_router(current_news_router, prefix="/current-news")
app.include_router(search_execution_router_v1, prefix="/search")
app.include_router(telemetry_router)
app.include_router(lifeswitch_nutrition_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_meals_router, prefix="/lifeswitch/nutrition")
app.include_router(lifeswitch_nutrition_log_router, prefix="/lifeswitch/nutrition")
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


DSN = os.environ["POSTGRES_DSN"]
POSTGRES = PostgresConnectionProvider(DSN)
AI_OPERATIONS_POSTGRES = PostgresConnectionProvider(
    DSN,
    connect_kwargs={"command_timeout": 10, "timeout": 5},
)
AI_OPERATIONS = PostgresAiOperationsRepository(AI_OPERATIONS_POSTGRES)
app.include_router(create_assistant_preferences_router(POSTGRES))
app.include_router(create_ai_operations_router(AI_OPERATIONS))
app.include_router(
    create_operational_health_router(
        POSTGRES,
        zep_prompt_mode=ZEP_PROMPT_SETTINGS.mode,
    )
)
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
client = get_optional_openai_client()

# ---------- persistent chat memory ----------
app.include_router(conversation_attachment_router)


app.include_router(create_transcript_ingest_router(POSTGRES))


app.include_router(
    create_thread_lifecycle_router(POSTGRES, title_client=client)
)


app.include_router(
    create_conversation_erasure_router(CONVERSATION_ERASURE)
)
