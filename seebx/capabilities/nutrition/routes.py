from __future__ import annotations

from fastapi import APIRouter

from .foods import (
    create_my_food_from_catalog,
    create_my_food_from_usda,
    create_my_food_serving,
    deactivate_my_food,
    delete_my_food_override,
    list_my_food_overrides,
    list_my_food_servings,
    list_my_foods,
    update_my_food,
    update_my_food_serving,
    upsert_my_food_override,
)
from .meal_plans import (
    add_item,
    create_meal_plan,
    delete_meal_plan_item,
    list_items,
    list_meal_plans,
    update_meal_plan_item,
)


router = APIRouter()

# Preserve the established route registration order exactly. Implementation
# ownership lives in the aggregate modules above; this file is composition only.
router.get("/meal_plans")(list_meal_plans)
router.post("/meal_plans/create")(create_meal_plan)
router.post("/my_foods/create_from_usda")(create_my_food_from_usda)
router.post("/meal_plans/{meal_plan_id}/items/add")(add_item)
router.patch("/meal_plans/{meal_plan_id}/items/{meal_plan_item_id}")(update_meal_plan_item)
router.delete("/meal_plans/{meal_plan_id}/items/{meal_plan_item_id}")(delete_meal_plan_item)
router.get("/meal_plans/{meal_plan_id}/items")(list_items)
router.get("/my_foods")(list_my_foods)
router.post("/my_foods/create_from_catalog")(create_my_food_from_catalog)
router.patch("/my_foods/{my_food_id}")(update_my_food)
router.post("/my_foods/{my_food_id}/deactivate")(deactivate_my_food)
router.get("/my_foods/{my_food_id}/servings")(list_my_food_servings)
router.post("/my_foods/{my_food_id}/servings/create")(create_my_food_serving)
router.patch("/my_foods/{my_food_id}/servings/{my_food_serving_id}")(update_my_food_serving)
router.get("/my_food_overrides")(list_my_food_overrides)
router.post("/my_food_overrides/upsert")(upsert_my_food_override)
router.delete("/my_food_overrides")(delete_my_food_override)
