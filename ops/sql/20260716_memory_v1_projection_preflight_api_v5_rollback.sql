BEGIN;

DO $guard$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'projection preflight API rollback requires sage';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.preflight_projection_packet_v5(uuid,text);
DROP FUNCTION IF EXISTS memory.preflight_projection_source_v5(uuid);

COMMIT;
