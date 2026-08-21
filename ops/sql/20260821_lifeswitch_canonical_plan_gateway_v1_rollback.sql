\set ON_ERROR_STOP on

BEGIN;

CREATE OR REPLACE FUNCTION lifeswitch_chat.read_plan_v1(p_context_id uuid)
RETURNS TABLE(plan_source text,document jsonb)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
  SELECT NULL::text,NULL::jsonb
  FROM (
    SELECT lifeswitch_chat.resolve_owner_read_context_v1(p_context_id)
  ) authority
  WHERE false
$$;

ALTER FUNCTION lifeswitch_chat.read_plan_v1(uuid) OWNER TO lifeswitch_owner;
REVOKE ALL ON FUNCTION lifeswitch_chat.read_plan_v1(uuid)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.read_plan_v1(uuid)
  TO lifeswitch_chat_reader;

DROP FUNCTION lifeswitch_chat.whitelist_plan_document_v1(jsonb);
DROP FUNCTION lifeswitch_chat.whitelist_target_section_v1(jsonb,text[]);
DROP FUNCTION lifeswitch_chat.whitelist_target_value_v1(jsonb);

COMMIT;
