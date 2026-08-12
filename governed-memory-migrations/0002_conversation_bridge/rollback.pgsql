-- Empty-only rollback for governed_memory_conversation_bridge_0002.
-- Run only in the canonical `memory` conversation database as `sage`. The migration
-- runner supplies BEGIN/COMMIT, timeouts, and the migration advisory lock.

DO $preflight$
BEGIN
  IF pg_catalog.current_database() <> 'memory' OR current_user <> 'sage' THEN
    RAISE EXCEPTION 'conversation bridge rollback requires sage';
  END IF;
  IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'conversation bridge rollback requires read committed';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_auth_members AS membership
    JOIN pg_catalog.pg_roles AS granted_role
      ON granted_role.oid = membership.roleid
    JOIN pg_catalog.pg_roles AS member_role
      ON member_role.oid = membership.member
    WHERE granted_role.rolname IN (
      'governed_memory_api',
      'governed_memory_worker',
      'memory_ingest_writer',
      'memory_erasure_requester'
    ) OR member_role.rolname IN (
      'governed_memory_api',
      'governed_memory_worker',
      'memory_ingest_writer',
      'memory_erasure_requester'
    )
  ) THEN
    RAISE EXCEPTION
      'source runtime membership graph must be empty before rollback';
  END IF;
  IF pg_catalog.to_regnamespace('memory') IS NULL
     OR pg_catalog.to_regprocedure(
          'memory.current_actor_user_id()'
        ) IS NULL
     OR pg_catalog.to_regprocedure(
          'memory.enqueue_chat_log_consolidation()'
        ) IS NULL THEN
    RAISE EXCEPTION 'legacy rollback authority functions are absent';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('memory.current_actor_user_id()'::text, 's'::"char", false,
       'sql'::text),
      ('memory.enqueue_chat_log_consolidation()'::text, 'v'::"char", true,
       'plpgsql'::text)
    ) AS expected(signature, volatility, security_definer, language_name)
    LEFT JOIN pg_catalog.pg_proc AS routine
      ON routine.oid = pg_catalog.to_regprocedure(expected.signature)
    LEFT JOIN pg_catalog.pg_language AS language
      ON language.oid = routine.prolang
    WHERE routine.oid IS NULL
       OR routine.proowner <> 'sage'::regrole
       OR routine.provolatile <> expected.volatility
       OR routine.prosecdef <> expected.security_definer
       OR routine.proconfig IS DISTINCT FROM
            ARRAY['search_path=pg_catalog']::text[]
       OR language.lanname <> expected.language_name
  ) THEN
    RAISE EXCEPTION 'legacy rollback authority function definitions differ';
  END IF;
END;
$preflight$;

-- Serialize with ordinary chat inserts before removing the erasure trigger,
-- then wait for bridge and coordinator calls that already entered a function.
-- The separate DO statement gets a fresh READ COMMITTED snapshot after waits.
LOCK TABLE public.threads IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.chat_log IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.chat_attachments IN ACCESS EXCLUSIVE MODE;
LOCK TABLE chat_integrity.assistant_transcript_attestation_v1
  IN ACCESS EXCLUSIVE MODE;
DO $catalog_lock$
BEGIN
  IF pg_catalog.to_regclass('public.active_thread_selection') IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE public.active_thread_selection IN ACCESS EXCLUSIVE MODE';
  END IF;
  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE trusted_web.response_transcript_v1 IN ACCESS EXCLUSIVE MODE';
  END IF;
END;
$catalog_lock$;
LOCK TABLE memory_ingest_private.memory_ingest_outbox
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_operation
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_target
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_thread_target
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_message_tombstone
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_thread_tombstone
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_receipt
  IN ACCESS EXCLUSIVE MODE;

DO $catalog_assert$
BEGIN
  PERFORM memory_ingest_private.assert_chat_deletion_catalog();
END;
$catalog_assert$;

DO $empty_only$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM chat_integrity.assistant_transcript_attestation_v1
    LIMIT 1
  ) THEN
    RAISE EXCEPTION
      'conversation bridge rollback is empty-only; chat attestations exist';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_operation
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_target
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_message_tombstone
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_tombstone
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_receipt
    LIMIT 1
  ) THEN
    RAISE EXCEPTION 'conversation bridge rollback is empty-only; erasure rows exist';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.memory_ingest_outbox
    WHERE state = 'erasure_cancelled'
    LIMIT 1
  ) THEN
    RAISE EXCEPTION 'conversation bridge rollback blocked by erasure outbox state';
  END IF;
  IF EXISTS (SELECT 1 FROM memory_ingest_private.memory_ingest_outbox LIMIT 1) THEN
    RAISE EXCEPTION 'conversation bridge rollback is empty-only; rows exist';
  END IF;
END;
$empty_only$;

DROP TRIGGER source_erasure_thread_tombstone_immutable
  ON memory_ingest_private.source_erasure_thread_tombstone;
DROP TRIGGER source_erasure_message_tombstone_immutable
  ON memory_ingest_private.source_erasure_message_tombstone;
DROP TRIGGER source_erasure_receipt_immutable
  ON memory_ingest_private.source_erasure_receipt;
DROP TRIGGER chat_attachments_serialize_source_erasure
  ON public.chat_attachments;
DO $drop_response_transcript_trigger$
BEGIN
  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'DROP TRIGGER response_transcript_serialize_source_erasure '
      'ON trusted_web.response_transcript_v1';
  END IF;
END;
$drop_response_transcript_trigger$;
DROP TRIGGER threads_serialize_source_erasure ON public.threads;
DROP TRIGGER chat_log_serialize_source_erasure ON public.chat_log;
DROP FUNCTION memory_ingest_private.serialize_attachment_source_erasure();
DROP FUNCTION
  memory_ingest_private.serialize_response_transcript_source_erasure();
DROP FUNCTION memory_ingest_private.serialize_thread_source_erasure();
DROP FUNCTION memory_ingest_private.serialize_chat_source_erasure();
DROP FUNCTION
  memory_ingest_private.guard_source_erasure_receipt_immutable();
DROP FUNCTION memory_ingest_private.begin_source_erasure(
  uuid,text,uuid,uuid,integer,text
);
DROP FUNCTION memory_ingest_private.read_source_erasure(uuid);
DROP FUNCTION memory_ingest_private.lease_source_erasure(text,integer);
DROP FUNCTION memory_ingest_private.read_source_erasure_targets(
  uuid,uuid,timestamptz,uuid,integer
);
DROP FUNCTION memory_ingest_private.release_source_erasure_lease(uuid,uuid);
DROP FUNCTION memory_ingest_private.mark_source_erasure_governed_deleted(
  uuid,uuid,text,integer,text
);
DROP FUNCTION memory_ingest_private.finalize_source_erasure(uuid,uuid,text);
DROP FUNCTION memory_ingest_private.ack_source_erasure_completion(
  uuid,uuid,text
);
DROP FUNCTION memory_ingest_private.fail_source_erasure(uuid,uuid,text,text);

DROP FUNCTION memory_ingest_private.enqueue_chat_log_message(uuid,text);
DROP FUNCTION memory_ingest_private.lease_memory_ingest(
  text,integer,integer
);
DROP FUNCTION memory_ingest_private.read_leased_chat_log_message(uuid,uuid);
DROP FUNCTION memory_ingest_private.mark_memory_ingest_context_review(
  uuid,uuid
);
DROP FUNCTION memory_ingest_private.ack_memory_ingest(
  uuid,uuid,text,uuid,uuid
);
DROP FUNCTION memory_ingest_private.fail_memory_ingest(
  uuid,uuid,text,text,integer
);
DROP FUNCTION memory_ingest_private.expire_memory_ingest(integer);
DROP FUNCTION memory_ingest_private.purge_terminal_memory_ingest(integer);

DROP TABLE memory_ingest_private.source_erasure_thread_target;
DROP TABLE memory_ingest_private.source_erasure_target;
DROP TABLE memory_ingest_private.source_erasure_message_tombstone;
DROP TABLE memory_ingest_private.source_erasure_thread_tombstone;
DROP TABLE memory_ingest_private.source_erasure_receipt;
DROP TABLE memory_ingest_private.source_erasure_operation;
DROP TABLE memory_ingest_private.memory_ingest_outbox;
DROP FUNCTION memory_ingest_private.source_erasure_target_sha256(
  uuid,uuid,uuid,uuid,timestamptz
);
DROP FUNCTION memory_ingest_private.terminal_receipt_sha256(
  text,text,text,integer,uuid,uuid,text,timestamptz
);
DROP FUNCTION memory_ingest_private.source_binding_sha256(
  uuid,uuid,uuid,uuid,uuid,integer,text,text,text,timestamptz
);
DROP FUNCTION memory_ingest_private.ingest_window_sha256(
  uuid,uuid,uuid,uuid,uuid,text
);
DROP FUNCTION memory_ingest_private.assert_chat_deletion_catalog();
DROP FUNCTION memory_ingest_private.timestamp_utc_text(timestamptz);
DROP FUNCTION memory_ingest_private.deletion_confirmation_sha256(
  uuid,text,uuid,uuid,integer
);
DROP FUNCTION memory_ingest_private.framed_utf8_field(text,text);
DROP SCHEMA memory_ingest_private;

-- Return the chat roots to the exact pre-migration authority only while the
-- legacy Memory schema is still present.  The empty-only guard above prevents
-- this rollback after any neutral assistant attestation has been written.
DROP POLICY chat_log_owner_isolation ON public.chat_log;
CREATE POLICY raw_owner_isolation ON public.chat_log
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());
DROP POLICY threads_owner_isolation ON public.threads;
CREATE POLICY raw_owner_isolation ON public.threads
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());
DROP POLICY chat_attachments_owner_isolation ON public.chat_attachments;
CREATE POLICY chat_attachments_owner_isolation ON public.chat_attachments
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());
DO $restore_active_thread_policy$
BEGIN
  IF pg_catalog.to_regclass('public.active_thread_selection') IS NOT NULL THEN
    EXECUTE 'DROP POLICY active_thread_selection_owner_isolation '
      'ON public.active_thread_selection';
    EXECUTE 'CREATE POLICY active_thread_owner_isolation '
      'ON public.active_thread_selection '
      'USING (owner_user_id = memory.current_actor_user_id()) '
      'WITH CHECK (owner_user_id = memory.current_actor_user_id())';
  END IF;
END;
$restore_active_thread_policy$;

DROP TABLE chat_integrity.assistant_transcript_attestation_v1;
DROP SCHEMA chat_integrity;

CREATE TRIGGER chat_log_enqueue_memory_v1_consolidation
AFTER INSERT ON public.chat_log
FOR EACH ROW
EXECUTE FUNCTION memory.enqueue_chat_log_consolidation();
ALTER TABLE public.chat_log
  DISABLE TRIGGER chat_log_enqueue_memory_v1_consolidation;

-- Restore the exact direct privilege removed by the forward migration. The
-- forward preflight sealed SELECT/INSERT/UPDATE/DELETE without grant options,
-- and the empty-only guard prevents rollback after successor state exists.
GRANT DELETE ON TABLE
  public.chat_log, public.threads, public.chat_attachments
TO brains_app;

DO $postflight$
BEGIN
  IF pg_catalog.to_regnamespace('memory_ingest_private') IS NOT NULL
     OR pg_catalog.to_regnamespace('chat_integrity') IS NOT NULL
     OR pg_catalog.to_regprocedure(
          'memory_ingest_private.assert_chat_deletion_catalog()'
        ) IS NOT NULL
     OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger
    WHERE tgname IN (
        'chat_log_serialize_source_erasure',
        'threads_serialize_source_erasure',
        'chat_attachments_serialize_source_erasure',
        'response_transcript_serialize_source_erasure'
      )
      AND tgrelid IN (
        'public.chat_log'::regclass,
        'public.threads'::regclass,
        'public.chat_attachments'::regclass,
        pg_catalog.to_regclass('trusted_web.response_transcript_v1')
      )
      AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'conversation bridge rollback left private objects';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('public.chat_log'::regclass::oid, 'raw_owner_isolation'::text),
      ('public.threads'::regclass::oid, 'raw_owner_isolation'::text),
      ('public.chat_attachments'::regclass::oid,
       'chat_attachments_owner_isolation'::text),
      (pg_catalog.to_regclass('public.active_thread_selection')::oid,
       'active_thread_owner_isolation'::text)
    ) AS expected(relation_oid, policy_name)
    WHERE expected.relation_oid IS NOT NULL
      AND (
        (
          SELECT pg_catalog.count(*)
          FROM pg_catalog.pg_policy AS policy
          WHERE policy.polrelid = expected.relation_oid
        ) <> 1
        OR NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_policy AS policy
          WHERE policy.polrelid = expected.relation_oid
            AND policy.polname = expected.policy_name
            AND pg_catalog.strpos(
              COALESCE(
                pg_catalog.pg_get_expr(policy.polqual, policy.polrelid), ''
              ) || COALESCE(
                pg_catalog.pg_get_expr(
                  policy.polwithcheck, policy.polrelid
                ), ''
              ),
              'memory.current_actor_user_id'
            ) > 0
        )
      )
  ) THEN
    RAISE EXCEPTION 'conversation bridge rollback did not restore chat policies';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('public.chat_log'::regclass::oid),
      ('public.threads'::regclass::oid),
      ('public.chat_attachments'::regclass::oid)
    ) AS expected(relation_oid)
    WHERE NOT pg_catalog.has_table_privilege(
                'brains_app', expected.relation_oid, 'DELETE'
              )
       OR (
         SELECT pg_catalog.array_agg(
           acl.privilege_type || ':' || acl.is_grantable::text
           ORDER BY acl.privilege_type
         )
         FROM pg_catalog.pg_class AS relation
         CROSS JOIN LATERAL pg_catalog.aclexplode(
           COALESCE(
             relation.relacl,
             pg_catalog.acldefault('r', relation.relowner)
           )
         ) AS acl
         WHERE relation.oid = expected.relation_oid
           AND acl.grantee = 'brains_app'::regrole::oid
       ) IS DISTINCT FROM ARRAY[
         'DELETE:false', 'INSERT:false', 'SELECT:false', 'UPDATE:false'
       ]::text[]
  ) THEN
    RAISE EXCEPTION 'conversation bridge rollback did not restore exact chat grants';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE trigger_row.tgrelid = 'public.chat_log'::regclass
      AND trigger_row.tgname =
          'chat_log_enqueue_memory_v1_consolidation'
      AND NOT trigger_row.tgisinternal
      AND trigger_row.tgenabled = 'D'
      AND trigger_row.tgtype = 5
      AND trigger_row.tgfoid = pg_catalog.to_regprocedure(
        'memory.enqueue_chat_log_consolidation()'
      )
  ) THEN
    RAISE EXCEPTION 'conversation bridge rollback did not restore enqueue trigger';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger
    WHERE tgrelid = 'public.chat_log'::regclass
      AND tgname = 'chat_log_enqueue_memory_v1_consolidation'
      AND NOT tgisinternal
      AND (
        tgenabled <> 'D'
        OR tgtype <> 5
        OR tgfoid IS DISTINCT FROM pg_catalog.to_regprocedure(
             'memory.enqueue_chat_log_consolidation()'
           )
      )
  ) THEN
    RAISE EXCEPTION 'legacy chat capture trigger changed during rollback';
  END IF;
END;
$postflight$;
