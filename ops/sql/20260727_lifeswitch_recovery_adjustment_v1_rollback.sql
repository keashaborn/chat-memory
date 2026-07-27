\set ON_ERROR_STOP on

begin;

drop trigger if exists recovery_adjustments_protect_history
  on lifeswitch_agentic.recovery_adjustments;

drop function if exists lifeswitch_agentic.protect_recovery_adjustment();

alter table lifeswitch_agentic.recovery_adjustments
  rename to recovery_adjustments_rollback_20260727;

commit;
