BEGIN;

-- Shoulder Press (Plate-Loaded) should not advertise selectorized equipment.
-- Align it with the existing plate-loaded equipment taxonomy used elsewhere.
UPDATE catalog_dev.exercise
SET equipment_required = ARRAY['plate_loaded_press','plates'],
    updated_at = now()
WHERE slug = 'shoulder_press_plate_loaded'
  AND modality = 'machine_plate_loaded'
  AND equipment_required @> ARRAY['shoulder_press_machine'];

COMMIT;
