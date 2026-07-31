BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $preflight$
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'AI Operations alert delivery rollback requires sage'
      USING ERRCODE = '42501';
  END IF;
END
$preflight$;

DROP TRIGGER IF EXISTS enqueue_monitor_alert_delivery_v1
  ON ai_operations.monitor_incident_event_v1;
DROP FUNCTION IF EXISTS ai_operations.enqueue_monitor_alert_delivery_v1();
DROP FUNCTION IF EXISTS ai_operations.claim_monitor_alert_delivery_v1(uuid);
DROP FUNCTION IF EXISTS ai_operations.complete_monitor_alert_delivery_v1(
  uuid,uuid,text,text,text
);
DROP TRIGGER IF EXISTS monitor_alert_delivery_event_append_only_v1
  ON ai_operations.monitor_alert_delivery_event_v1;
DROP FUNCTION IF EXISTS
  ai_operations.reject_monitor_alert_event_mutation_v1();
DROP TABLE IF EXISTS ai_operations.monitor_alert_delivery_event_v1;
DROP TABLE IF EXISTS ai_operations.monitor_alert_delivery_v1;

COMMIT;
