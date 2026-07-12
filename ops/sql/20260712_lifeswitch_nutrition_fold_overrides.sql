BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- my_food is already owner-scoped. Fold the user-facing alias into its
-- display_name so the database, not browser storage, is the sole source.
UPDATE lifeswitch_nutrition.my_food f
SET display_name = btrim(o.alias),
    updated_at = now()
FROM lifeswitch_nutrition.my_food_override o
WHERE o.owner_user_id = f.owner_user_id
  AND o.my_food_id = f.my_food_id
  AND NULLIF(btrim(o.alias), '') IS NOT NULL
  AND f.display_name IS DISTINCT FROM btrim(o.alias);

COMMIT;
