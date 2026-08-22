from __future__ import annotations

from fastapi import APIRouter

from .conditioning import (
    correct_conditioning_session, create_conditioning_session,
    deactivate_conditioning_session, deactivate_my_conditioning_prescription,
    get_conditioning_session, list_conditioning_library,
    list_conditioning_sessions, list_my_conditioning_prescriptions,
    upsert_my_conditioning_prescription,
)
from .exercises import deactivate_my_exercise, list_my_exercises, upsert_my_exercise
from .sessions import (
    add_training_set_log, add_training_set_log_segment, complete_training_session,
    correct_training_session, create_training_session, deactivate_training_session,
    delete_training_set_log, delete_training_set_log_segment, get_training_session,
    list_strength_progression, list_training_session_sets,
    list_training_sessions, list_training_set_log_segments, update_training_set_log,
)
from .sharing import (
    create_workout_template_share, import_workout_template_share,
    list_workout_template_shares, preview_workout_template_share,
    revoke_workout_template_share,
)
from .templates import (
    classify_historical_workout_sessions, deactivate_workout_template,
    delete_workout_template_exercise, delete_workout_template_exercise_segment,
    list_workout_template_exercises, list_workout_template_exercise_segments,
    list_workout_templates, upsert_workout_template,
    upsert_workout_template_exercise, upsert_workout_template_exercise_segment,
)


router = APIRouter()

# Preserve the established route registration order exactly. Implementation
# ownership lives in the aggregate modules above; this file is composition only.
router.get("/my_exercises")(list_my_exercises)
router.post("/my_exercises/upsert")(upsert_my_exercise)
router.post("/my_exercises/{my_exercise_id}/deactivate")(deactivate_my_exercise)
router.get("/conditioning_library")(list_conditioning_library)
router.get("/my_conditioning_prescriptions")(list_my_conditioning_prescriptions)
router.post("/my_conditioning_prescriptions/upsert")(upsert_my_conditioning_prescription)
router.post("/my_conditioning_prescriptions/{my_conditioning_prescription_id}/deactivate")(deactivate_my_conditioning_prescription)
router.post("/conditioning_sessions/create")(create_conditioning_session)
router.get("/conditioning_sessions")(list_conditioning_sessions)
router.get("/conditioning_sessions/{conditioning_session_log_id}")(get_conditioning_session)
router.post("/conditioning_sessions/{conditioning_session_log_id}/deactivate")(deactivate_conditioning_session)
router.post("/conditioning_sessions/{conditioning_session_log_id}/correct")(correct_conditioning_session)
router.post("/workout_template_shares/create")(create_workout_template_share)
router.get("/workout_template_shares")(list_workout_template_shares)
router.get("/workout_template_shares/preview")(preview_workout_template_share)
router.post("/workout_template_shares/import")(import_workout_template_share)
router.post("/workout_template_shares/{workout_template_share_id}/revoke")(revoke_workout_template_share)
router.get("/workout_templates")(list_workout_templates)
router.post("/workout_templates/upsert")(upsert_workout_template)
router.post("/workout_templates/{workout_template_id}/classify_historical_sessions")(classify_historical_workout_sessions)
router.post("/workout_templates/{workout_template_id}/deactivate")(deactivate_workout_template)
router.get("/workout_templates/{workout_template_id}/exercises")(list_workout_template_exercises)
router.post("/workout_templates/{workout_template_id}/exercises/upsert")(upsert_workout_template_exercise)
router.post("/workout_templates/{workout_template_id}/exercises/{workout_template_exercise_id}/delete")(delete_workout_template_exercise)
router.get("/workout_template_exercises/{workout_template_exercise_id}/segments")(list_workout_template_exercise_segments)
router.post("/workout_template_exercises/{workout_template_exercise_id}/segments/upsert")(upsert_workout_template_exercise_segment)
router.post("/workout_template_exercises/{workout_template_exercise_id}/segments/{workout_template_exercise_segment_id}/delete")(delete_workout_template_exercise_segment)
router.post("/sessions/complete")(complete_training_session)
router.post("/sessions/create")(create_training_session)
router.get("/sessions")(list_training_sessions)
router.get("/sessions/{training_session_id}")(get_training_session)
router.get("/progression")(list_strength_progression)
router.post("/sessions/{training_session_id}/deactivate")(deactivate_training_session)
router.post("/sessions/{training_session_id}/correct")(correct_training_session)
router.get("/sessions/{training_session_id}/sets")(list_training_session_sets)
router.post("/sessions/{training_session_id}/sets/add")(add_training_set_log)
router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/update")(update_training_set_log)
router.get("/sessions/{training_session_id}/sets/{training_set_log_id}/segments")(list_training_set_log_segments)
router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/segments/add")(add_training_set_log_segment)
router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/segments/{training_set_log_segment_id}/delete")(delete_training_set_log_segment)
router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/delete")(delete_training_set_log)
