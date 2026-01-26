BEGIN;

-- Fix modality mismatches discovered via equipment_required

UPDATE catalog_dev.exercise
SET modality='free_weight', updated_at=now()
WHERE slug='overhead_press'
  AND modality='machine_selectorized';

UPDATE catalog_dev.exercise
SET modality='cable', updated_at=now()
WHERE slug='face_pull'
  AND modality='other';

COMMIT;
