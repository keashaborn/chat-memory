-- Content-free conversation-to-Memory bridge. Apply only to the canonical
-- `memory` conversation database as `sage`.  The migration reads catalog
-- metadata only: it never scans chat, thread, or attachment rows.  Runtime
-- source reads are possible only for the exact newly inserted chat_log row
-- bound to an active worker lease.

\if :{?governed_memory_inactive_installation}
\else
  \set governed_memory_inactive_installation off
\endif
SELECT CASE :'governed_memory_inactive_installation'
  WHEN 'on' THEN 'on'
  WHEN 'off' THEN 'off'
  ELSE NULL
END AS governed_memory_installation_mode
\gset
\if :{?governed_memory_installation_mode}
\else
  \echo 'governed_memory_inactive_installation must be exactly on or off'
  \quit 3
\endif
SELECT pg_catalog.set_config(
  'governed_memory.inactive_installation',
  :'governed_memory_installation_mode',
  false
);

DO $preflight$
DECLARE
  expected_column record;
  forbidden_relation text;
  inbound_fk record;
  privilege_name text;
BEGIN
  IF pg_catalog.current_setting('server_version_num')::integer < 150000 THEN
    RAISE EXCEPTION 'PostgreSQL 15 or newer is required';
  END IF;
  IF pg_catalog.current_database() <> 'memory'
     OR pg_catalog.current_setting('server_encoding') <> 'UTF8' THEN
    RAISE EXCEPTION 'conversation bridge requires UTF8 database memory';
  END IF;
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'conversation bridge migration requires sage';
  END IF;
  IF pg_catalog.current_setting('log_statement') <> 'none'
     OR pg_catalog.current_setting(
       'log_parameter_max_length_on_error'
     )::integer <> 0
     OR pg_catalog.current_setting('log_duration') <> 'off'
     OR pg_catalog.current_setting(
       'log_min_duration_statement'
     )::integer <> -1
     OR pg_catalog.current_setting(
       'log_min_duration_sample'
     )::integer <> -1
     OR pg_catalog.current_setting(
       'log_transaction_sample_rate'
     )::numeric <> 0
     OR pg_catalog.current_setting(
       'log_parameter_max_length'
     )::integer <> 0
     OR (
       pg_catalog.current_setting(
         'auto_explain.log_parameter_max_length', true
       ) IS NOT NULL
       AND pg_catalog.current_setting(
         'auto_explain.log_parameter_max_length', true
       )::integer <> 0
     )
     OR EXISTS (
       SELECT 1
       FROM pg_catalog.unnest(
         pg_catalog.string_to_array(
           pg_catalog.current_setting('shared_preload_libraries'), ','
         )
       ) AS configured(library_name)
       WHERE pg_catalog.lower(
         pg_catalog.btrim(configured.library_name)
       ) = 'pgaudit'
     ) THEN
    RAISE EXCEPTION 'source parameter logging preflight failed';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname = 'memory_ingest_writer'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname = 'memory_erasure_requester'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname = 'governed_memory_api'
      AND rolcanlogin = (
        pg_catalog.current_setting(
          'governed_memory.inactive_installation'
        ) = 'off'
      )
      AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname = 'governed_memory_worker'
      AND rolcanlogin = (
        pg_catalog.current_setting(
          'governed_memory.inactive_installation'
        ) = 'off'
      )
      AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) THEN
    RAISE EXCEPTION 'bridge runtime roles are absent or unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname = 'brains_app'
      AND rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND rolinherit
  ) THEN
    RAISE EXCEPTION 'brains_app role is absent or unsafe';
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
    RAISE EXCEPTION 'source runtime membership graph must be empty before migration';
  END IF;
  IF pg_catalog.to_regnamespace('memory_ingest_private') IS NOT NULL
     OR pg_catalog.to_regnamespace('chat_integrity') IS NOT NULL THEN
    RAISE EXCEPTION 'conversation bridge or chat integrity schema already exists';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('chat_log'), ('threads'), ('chat_attachments')
    ) AS expected(relname)
    LEFT JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.nspname = 'public'
    LEFT JOIN pg_catalog.pg_class AS relation
      ON relation.relnamespace = namespace.oid
     AND relation.relname = expected.relname
     AND relation.relkind = 'r'
    WHERE relation.oid IS NULL
       OR relation.relowner <> 'sage'::regrole
       OR NOT relation.relrowsecurity
       OR NOT relation.relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'conversation source table owner or forced RLS differs';
  END IF;

  FOR expected_column IN
    SELECT * FROM (VALUES
      ('chat_log', 'id', 'uuid'),
      ('chat_log', 'owner_user_id', 'uuid'),
      ('chat_log', 'thread_id', 'uuid'),
      ('chat_log', 'source', 'text'),
      ('chat_log', 'text', 'text'),
      ('chat_log', 'created_at', 'timestamp with time zone'),
      ('threads', 'id', 'uuid'),
      ('threads', 'owner_user_id', 'uuid'),
      ('threads', 'created_at', 'timestamp with time zone'),
      ('chat_attachments', 'owner_user_id', 'uuid'),
      ('chat_attachments', 'thread_id', 'uuid'),
      ('chat_attachments', 'message_id', 'uuid')
    ) AS required(relation_name, column_name, type_name)
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_attribute AS attribute
      WHERE attribute.attrelid = pg_catalog.to_regclass(
              'public.' || expected_column.relation_name
            )
        AND attribute.attname = expected_column.column_name
        AND attribute.atttypid = expected_column.type_name::regtype
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
    ) THEN
      RAISE EXCEPTION 'conversation source column contract differs: %.%',
        expected_column.relation_name, expected_column.column_name;
    END IF;
  END LOOP;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute
    WHERE attrelid = 'public.chat_log'::regclass
      AND attname = 'id' AND attnotnull AND NOT attisdropped
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint
    WHERE conrelid = 'public.chat_log'::regclass
      AND conname = 'chat_log_pkey' AND contype = 'p' AND convalidated
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint
    WHERE conrelid = 'public.chat_log'::regclass
      AND conname = 'chat_log_owner_thread_fk' AND contype = 'f'
      AND NOT convalidated
      AND pg_catalog.pg_get_constraintdef(oid, true) =
        'FOREIGN KEY (owner_user_id, thread_id) '
        'REFERENCES threads(owner_user_id, id) NOT VALID'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_index
    WHERE indexrelid = pg_catalog.to_regclass(
            'public.chat_log_id_owner_thread_chat_attachments_uq'
          )
      AND indrelid = 'public.chat_log'::regclass
      AND indisunique AND indisvalid AND indisready
      AND indpred IS NULL AND indexprs IS NULL
      AND pg_catalog.pg_get_indexdef(indexrelid) =
        'CREATE UNIQUE INDEX chat_log_id_owner_thread_chat_attachments_uq '
        'ON public.chat_log USING btree (id, owner_user_id, thread_id)'
  ) THEN
    RAISE EXCEPTION 'chat_log key or owner/thread lineage contract differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.contype = 'f'
      AND constraint_row.conrelid = 'public.chat_attachments'::regclass
      AND constraint_row.confrelid = 'public.threads'::regclass
      AND constraint_row.convalidated
      AND constraint_row.confdeltype = 'c'
      AND constraint_row.confupdtype = 'a'
      AND constraint_row.confmatchtype = 's'
      AND NOT constraint_row.condeferrable
      AND NOT constraint_row.condeferred
      AND constraint_row.conkey = ARRAY[
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.chat_attachments'::regclass
            AND attribute.attname = 'thread_id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        ),
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.chat_attachments'::regclass
            AND attribute.attname = 'owner_user_id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        )
      ]
      AND constraint_row.confkey = ARRAY[
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.threads'::regclass
            AND attribute.attname = 'id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        ),
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.threads'::regclass
            AND attribute.attname = 'owner_user_id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        )
      ]
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.contype = 'f'
      AND constraint_row.conrelid = 'public.chat_attachments'::regclass
      AND constraint_row.confrelid = 'public.chat_log'::regclass
      AND constraint_row.convalidated
      AND constraint_row.confdeltype = 'c'
      AND constraint_row.confupdtype = 'a'
      AND constraint_row.confmatchtype = 's'
      AND NOT constraint_row.condeferrable
      AND NOT constraint_row.condeferred
      AND constraint_row.conkey = ARRAY[
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.chat_attachments'::regclass
            AND attribute.attname = 'message_id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        ),
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.chat_attachments'::regclass
            AND attribute.attname = 'owner_user_id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        ),
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.chat_attachments'::regclass
            AND attribute.attname = 'thread_id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        )
      ]
      AND constraint_row.confkey = ARRAY[
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.chat_log'::regclass
            AND attribute.attname = 'id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        ),
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.chat_log'::regclass
            AND attribute.attname = 'owner_user_id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        ),
        (
          SELECT attribute.attnum
          FROM pg_catalog.pg_attribute AS attribute
          WHERE attribute.attrelid = 'public.chat_log'::regclass
            AND attribute.attname = 'thread_id'
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        )
      ]
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute
    WHERE attrelid = 'public.chat_attachments'::regclass
      AND attname = 'owner_user_id' AND attnotnull AND NOT attisdropped
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute
    WHERE attrelid = 'public.chat_attachments'::regclass
      AND attname = 'thread_id' AND attnotnull AND NOT attisdropped
  ) THEN
    RAISE EXCEPTION 'attachment owner/thread/message lineage contract differs';
  END IF;
  IF pg_catalog.to_regclass('trusted_web.response_transcript_v1') IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM (VALUES
         ('owner_user_id', 'uuid', true),
         ('thread_id', 'uuid', true),
         ('user_chat_log_id', 'uuid', true),
         ('assistant_chat_log_id', 'uuid', true)
       ) AS expected(column_name, type_name, required_not_null)
       LEFT JOIN pg_catalog.pg_attribute AS attribute
         ON attribute.attrelid =
              pg_catalog.to_regclass('trusted_web.response_transcript_v1')
        AND attribute.attname = expected.column_name
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
       WHERE attribute.attnum IS NULL
          OR attribute.atttypid <> expected.type_name::regtype
          OR attribute.attnotnull IS DISTINCT FROM expected.required_not_null
     ) THEN
    RAISE EXCEPTION 'response transcript source columns differ';
  END IF;

  IF pg_catalog.to_regclass('public.active_thread_selection') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1
       FROM pg_catalog.pg_constraint AS constraint_row
       WHERE constraint_row.contype = 'f'
         AND constraint_row.conrelid =
             pg_catalog.to_regclass('public.active_thread_selection')
         AND constraint_row.confrelid = 'public.threads'::regclass
         AND constraint_row.convalidated
         AND constraint_row.confdeltype = 'c'
         AND constraint_row.confupdtype = 'a'
         AND constraint_row.confmatchtype = 's'
         AND NOT constraint_row.condeferrable
         AND NOT constraint_row.condeferred
         AND constraint_row.conkey = ARRAY[
           (
             SELECT attribute.attnum
             FROM pg_catalog.pg_attribute AS attribute
             WHERE attribute.attrelid = constraint_row.conrelid
               AND attribute.attname = 'owner_user_id'
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
           ),
           (
             SELECT attribute.attnum
             FROM pg_catalog.pg_attribute AS attribute
             WHERE attribute.attrelid = constraint_row.conrelid
               AND attribute.attname = 'thread_id'
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
           )
         ]
         AND constraint_row.confkey = ARRAY[
           (
             SELECT attribute.attnum
             FROM pg_catalog.pg_attribute AS attribute
             WHERE attribute.attrelid = 'public.threads'::regclass
               AND attribute.attname = 'owner_user_id'
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
           ),
           (
             SELECT attribute.attnum
             FROM pg_catalog.pg_attribute AS attribute
             WHERE attribute.attrelid = 'public.threads'::regclass
               AND attribute.attname = 'id'
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
           )
         ]
     ) THEN
    RAISE EXCEPTION 'active-thread chat-auxiliary lineage contract differs';
  END IF;
  IF pg_catalog.to_regclass('trusted_web.response_transcript_v1') IS NOT NULL
     AND (
       NOT EXISTS (
         SELECT 1
         FROM pg_catalog.pg_constraint AS constraint_row
         WHERE constraint_row.contype = 'f'
           AND constraint_row.conrelid =
               pg_catalog.to_regclass('trusted_web.response_transcript_v1')
           AND constraint_row.confrelid = 'public.chat_log'::regclass
           AND constraint_row.convalidated
           AND constraint_row.confdeltype = 'c'
           AND constraint_row.confupdtype = 'a'
           AND constraint_row.confmatchtype = 's'
           AND NOT constraint_row.condeferrable
           AND NOT constraint_row.condeferred
           AND constraint_row.conkey = ARRAY[
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = constraint_row.conrelid
                 AND attribute.attname = 'user_chat_log_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             ),
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = constraint_row.conrelid
                 AND attribute.attname = 'owner_user_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             ),
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = constraint_row.conrelid
                 AND attribute.attname = 'thread_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             )
           ]
           AND constraint_row.confkey = ARRAY[
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = 'public.chat_log'::regclass
                 AND attribute.attname = 'id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             ),
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = 'public.chat_log'::regclass
                 AND attribute.attname = 'owner_user_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             ),
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = 'public.chat_log'::regclass
                 AND attribute.attname = 'thread_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             )
           ]
       ) OR NOT EXISTS (
         SELECT 1
         FROM pg_catalog.pg_constraint AS constraint_row
         WHERE constraint_row.contype = 'f'
           AND constraint_row.conrelid =
               pg_catalog.to_regclass('trusted_web.response_transcript_v1')
           AND constraint_row.confrelid = 'public.chat_log'::regclass
           AND constraint_row.convalidated
           AND constraint_row.confdeltype = 'c'
           AND constraint_row.confupdtype = 'a'
           AND constraint_row.confmatchtype = 's'
           AND NOT constraint_row.condeferrable
           AND NOT constraint_row.condeferred
           AND constraint_row.conkey = ARRAY[
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = constraint_row.conrelid
                 AND attribute.attname = 'assistant_chat_log_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             ),
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = constraint_row.conrelid
                 AND attribute.attname = 'owner_user_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             ),
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = constraint_row.conrelid
                 AND attribute.attname = 'thread_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             )
           ]
           AND constraint_row.confkey = ARRAY[
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = 'public.chat_log'::regclass
                 AND attribute.attname = 'id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             ),
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = 'public.chat_log'::regclass
                 AND attribute.attname = 'owner_user_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             ),
             (
               SELECT attribute.attnum
               FROM pg_catalog.pg_attribute AS attribute
               WHERE attribute.attrelid = 'public.chat_log'::regclass
                 AND attribute.attname = 'thread_id'
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
             )
           ]
       )
     ) THEN
    RAISE EXCEPTION 'trusted-web chat-auxiliary lineage contract differs';
  END IF;

  FOR inbound_fk IN
    SELECT constraint_row.conname,
      child_namespace.nspname AS child_schema,
      child_relation.relname AS child_table,
      parent_namespace.nspname AS parent_schema,
      parent_relation.relname AS parent_table,
      constraint_row.confdeltype,
      constraint_row.confupdtype,
      constraint_row.confmatchtype,
      constraint_row.condeferrable,
      constraint_row.condeferred,
      constraint_row.convalidated,
      ARRAY(
        SELECT attribute.attname::text
        FROM pg_catalog.unnest(constraint_row.conkey)
          WITH ORDINALITY AS key_column(attnum, ordinal_position)
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = constraint_row.conrelid
         AND attribute.attnum = key_column.attnum
        ORDER BY key_column.ordinal_position
      ) AS child_columns,
      ARRAY(
        SELECT attribute.attname::text
        FROM pg_catalog.unnest(constraint_row.confkey)
          WITH ORDINALITY AS key_column(attnum, ordinal_position)
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = constraint_row.confrelid
         AND attribute.attnum = key_column.attnum
        ORDER BY key_column.ordinal_position
      ) AS parent_columns
    FROM pg_catalog.pg_constraint AS constraint_row
    JOIN pg_catalog.pg_class AS child_relation
      ON child_relation.oid = constraint_row.conrelid
    JOIN pg_catalog.pg_namespace AS child_namespace
      ON child_namespace.oid = child_relation.relnamespace
    JOIN pg_catalog.pg_class AS parent_relation
      ON parent_relation.oid = constraint_row.confrelid
    JOIN pg_catalog.pg_namespace AS parent_namespace
      ON parent_namespace.oid = parent_relation.relnamespace
    WHERE constraint_row.contype = 'f'
      AND (
        constraint_row.confrelid IN (
          'public.chat_log'::regclass,
          'public.threads'::regclass,
          'public.chat_attachments'::regclass
        )
        OR constraint_row.confrelid =
             pg_catalog.to_regclass('public.active_thread_selection')
        OR constraint_row.confrelid =
             pg_catalog.to_regclass('trusted_web.response_transcript_v1')
      )
  LOOP
    IF inbound_fk.confupdtype <> 'a'
       OR inbound_fk.confmatchtype <> 's'
       OR inbound_fk.condeferrable
       OR inbound_fk.condeferred
       OR NOT (
         (
           inbound_fk.child_schema = 'public'
           AND inbound_fk.child_table = 'chat_log'
           AND inbound_fk.conname = 'chat_log_owner_thread_fk'
           AND inbound_fk.parent_schema = 'public'
           AND inbound_fk.parent_table = 'threads'
           AND inbound_fk.child_columns =
               ARRAY['owner_user_id', 'thread_id']::text[]
           AND inbound_fk.parent_columns =
               ARRAY['owner_user_id', 'id']::text[]
           AND inbound_fk.confdeltype = 'a'
           AND NOT inbound_fk.convalidated
         ) OR (
           inbound_fk.child_schema = 'public'
           AND inbound_fk.child_table = 'chat_attachments'
           AND inbound_fk.conname = 'chat_attachments_thread_owner_fk'
           AND inbound_fk.parent_schema = 'public'
           AND inbound_fk.parent_table = 'threads'
           AND inbound_fk.child_columns =
               ARRAY['thread_id', 'owner_user_id']::text[]
           AND inbound_fk.parent_columns =
               ARRAY['id', 'owner_user_id']::text[]
           AND inbound_fk.confdeltype = 'c'
           AND inbound_fk.convalidated
         ) OR (
           inbound_fk.child_schema = 'public'
           AND inbound_fk.child_table = 'chat_attachments'
           AND inbound_fk.conname =
               'chat_attachments_message_owner_thread_fk'
           AND inbound_fk.parent_schema = 'public'
           AND inbound_fk.parent_table = 'chat_log'
           AND inbound_fk.child_columns =
               ARRAY['message_id', 'owner_user_id', 'thread_id']::text[]
           AND inbound_fk.parent_columns =
               ARRAY['id', 'owner_user_id', 'thread_id']::text[]
           AND inbound_fk.confdeltype = 'c'
           AND inbound_fk.convalidated
         ) OR (
           inbound_fk.child_schema = 'public'
           AND inbound_fk.child_table = 'active_thread_selection'
           AND inbound_fk.conname =
               'active_thread_selection_owner_thread_fk'
           AND inbound_fk.parent_schema = 'public'
           AND inbound_fk.parent_table = 'threads'
           AND inbound_fk.child_columns =
               ARRAY['owner_user_id', 'thread_id']::text[]
           AND inbound_fk.parent_columns =
               ARRAY['owner_user_id', 'id']::text[]
           AND inbound_fk.confdeltype = 'c'
           AND inbound_fk.convalidated
         ) OR (
           inbound_fk.child_schema = 'trusted_web'
           AND inbound_fk.child_table = 'response_transcript_v1'
           AND inbound_fk.parent_schema = 'public'
           AND inbound_fk.parent_table = 'chat_log'
           AND (
             (
               inbound_fk.conname =
                   'response_transcript_v1_user_chat_log_id_fkey'
               AND inbound_fk.child_columns =
                   ARRAY[
                     'user_chat_log_id', 'owner_user_id', 'thread_id'
                   ]::text[]
             ) OR (
               inbound_fk.conname =
                   'response_transcript_v1_assistant_chat_log_id_fkey'
               AND inbound_fk.child_columns =
                   ARRAY[
                     'assistant_chat_log_id', 'owner_user_id', 'thread_id'
                   ]::text[]
             )
           )
           AND inbound_fk.parent_columns =
               ARRAY['id', 'owner_user_id', 'thread_id']::text[]
           AND inbound_fk.confdeltype = 'c'
           AND inbound_fk.convalidated
         )
       ) THEN
      RAISE EXCEPTION
        'unclassified inbound chat deletion dependency: %.% -> %.%',
        inbound_fk.child_schema, inbound_fk.child_table,
        inbound_fk.parent_schema, inbound_fk.parent_table;
    END IF;
  END LOOP;

  IF pg_catalog.to_regclass('trusted_web.response_transcript_v1') IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM pg_catalog.pg_trigger AS trigger_row
       WHERE trigger_row.tgrelid =
             pg_catalog.to_regclass('trusted_web.response_transcript_v1')
         AND NOT trigger_row.tgisinternal
     ) THEN
    RAISE EXCEPTION 'preexisting response transcript trigger is forbidden';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE NOT trigger_row.tgisinternal
      AND (trigger_row.tgtype::integer & 8) = 8
      AND (
        trigger_row.tgrelid IN (
          'public.chat_log'::regclass,
          'public.threads'::regclass,
          'public.chat_attachments'::regclass
        )
        OR trigger_row.tgrelid =
             pg_catalog.to_regclass('public.active_thread_selection')
        OR trigger_row.tgrelid =
             pg_catalog.to_regclass('trusted_web.response_transcript_v1')
      )
  ) THEN
    RAISE EXCEPTION 'unclassified user DELETE trigger reaches chat deletion';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_rewrite AS rewrite_row
    WHERE rewrite_row.ev_type = '4'
      AND (
        rewrite_row.ev_class IN (
          'public.chat_log'::regclass,
          'public.threads'::regclass,
          'public.chat_attachments'::regclass
        )
        OR rewrite_row.ev_class =
             pg_catalog.to_regclass('public.active_thread_selection')
        OR rewrite_row.ev_class =
             pg_catalog.to_regclass('trusted_web.response_transcript_v1')
      )
  ) THEN
    RAISE EXCEPTION 'unclassified DELETE rewrite rule reaches chat deletion';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_inherits AS inheritance_row
    WHERE inheritance_row.inhrelid IN (
        'public.chat_log'::regclass,
        'public.threads'::regclass,
        'public.chat_attachments'::regclass,
        pg_catalog.to_regclass('public.active_thread_selection'),
        pg_catalog.to_regclass('trusted_web.response_transcript_v1')
      )
      OR inheritance_row.inhparent IN (
        'public.chat_log'::regclass,
        'public.threads'::regclass,
        'public.chat_attachments'::regclass,
        pg_catalog.to_regclass('public.active_thread_selection'),
        pg_catalog.to_regclass('trusted_web.response_transcript_v1')
      )
  ) THEN
    RAISE EXCEPTION 'chat deletion root inheritance is forbidden';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_trigger
    WHERE tgrelid = 'public.chat_log'::regclass
      AND tgname = 'chat_log_guard_canonical_owner'
      AND tgenabled = 'O' AND NOT tgisinternal
      AND tgfoid = 'public.guard_canonical_owner()'::regprocedure
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_trigger
    WHERE tgrelid = 'public.chat_log'::regclass
      AND tgname = 'chat_log_guard_immutable'
      AND tgenabled = 'O' AND NOT tgisinternal
      AND tgfoid = 'public.guard_chat_log_immutable()'::regprocedure
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_trigger
    WHERE tgrelid = 'public.chat_log'::regclass
      AND tgname = 'chat_log_enqueue_memory_v1_consolidation'
      AND NOT tgisinternal
      AND tgenabled = 'D'
      AND tgtype = 5
      AND tgfoid = pg_catalog.to_regprocedure(
        'memory.enqueue_chat_log_consolidation()'
      )
  ) OR EXISTS (
    SELECT 1 FROM pg_catalog.pg_trigger
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
  ) OR EXISTS (
    SELECT 1 FROM pg_catalog.pg_trigger
    WHERE tgrelid = 'public.chat_log'::regclass
      AND tgname NOT IN (
        'chat_log_guard_canonical_owner',
        'chat_log_guard_immutable',
        'chat_log_enqueue_memory_v1_consolidation'
      )
      AND tgenabled <> 'D'
      AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'chat_log trigger contract differs or legacy capture is enabled';
  END IF;

  -- This migration replaces every chat-root policy that still calls the
  -- legacy Memory schema.  Refuse an unknown or additional policy rather than
  -- silently leaving a second authority path installed.
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
            AND policy.polcmd = '*'
            AND policy.polpermissive
            AND policy.polroles = ARRAY[0::oid]
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
    RAISE EXCEPTION 'legacy chat owner policy contract differs';
  END IF;

  -- Rollback may restore only the exact direct legacy grant removed below.
  -- Refuse an unknown baseline rather than granting DELETE where it was not
  -- present, or preserving broader table authority by accident.
  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('public.chat_log'::regclass::oid),
      ('public.threads'::regclass::oid),
      ('public.chat_attachments'::regclass::oid)
    ) AS expected(relation_oid)
    WHERE (
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
    RAISE EXCEPTION 'brains_app legacy chat-root authority differs';
  END IF;

  FOREACH forbidden_relation IN ARRAY ARRAY[
    'public.chat_log', 'public.threads', 'public.chat_attachments'
  ] LOOP
    FOREACH privilege_name IN ARRAY ARRAY[
      'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
      'REFERENCES', 'TRIGGER'
    ] LOOP
      IF pg_catalog.has_table_privilege(
        'memory_ingest_writer', forbidden_relation, privilege_name
      ) OR pg_catalog.has_table_privilege(
        'governed_memory_worker', forbidden_relation, privilege_name
      ) THEN
        RAISE EXCEPTION 'bridge role already has direct authority on %',
          forbidden_relation;
      END IF;
    END LOOP;
  END LOOP;
END;
$preflight$;

-- All chat deletion now enters through the owner-bound erasure coordinator.
-- Ordinary application SQL retains read/append/update authority but cannot
-- bypass governed-memory coordination or its immutable receipt.
REVOKE DELETE ON TABLE
  public.chat_log, public.threads, public.chat_attachments
FROM brains_app;

-- Remove the final hard pg_depend edge from the active chat table to the
-- retired Memory schema.  Rollback recreates this exact disabled trigger only
-- while the legacy function still exists.
DROP TRIGGER chat_log_enqueue_memory_v1_consolidation ON public.chat_log;

-- Neutral chat provenance.  This table is deliberately outside both the
-- legacy and successor Memory schemas.  It contains no claims or retrieval
-- material and has no historical backfill path.
CREATE SCHEMA chat_integrity AUTHORIZATION sage;
REVOKE ALL ON SCHEMA chat_integrity FROM PUBLIC;
GRANT USAGE ON SCHEMA chat_integrity TO brains_app;

CREATE TABLE chat_integrity.assistant_transcript_attestation_v1 (
  answer_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  chat_log_id uuid NOT NULL,
  request_id_sha256 text NOT NULL,
  conversation_snapshot_sha256 text NOT NULL,
  trusted_plan_sha256 text NOT NULL,
  provider_request_sha256 text NOT NULL,
  provider_response_sha256 text NOT NULL,
  provider_response_id text NOT NULL,
  output_kind text NOT NULL,
  assistant_text_sha256 text NOT NULL,
  attestation_sha256 text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL,
  CONSTRAINT assistant_transcript_attestation_answer_chat_ck CHECK (
    answer_id = chat_log_id
  ),
  CONSTRAINT assistant_transcript_attestation_output_ck CHECK (
    output_kind IN ('content', 'refusal')
  ),
  CONSTRAINT assistant_transcript_attestation_provider_id_ck CHECK (
    pg_catalog.octet_length(provider_response_id) BETWEEN 1 AND 240
  ),
  CONSTRAINT assistant_transcript_attestation_hashes_ck CHECK (
    request_id_sha256 ~ '^[0-9a-f]{64}$'
    AND conversation_snapshot_sha256 ~ '^[0-9a-f]{64}$'
    AND trusted_plan_sha256 ~ '^[0-9a-f]{64}$'
    AND provider_request_sha256 ~ '^[0-9a-f]{64}$'
    AND provider_response_sha256 ~ '^[0-9a-f]{64}$'
    AND assistant_text_sha256 ~ '^[0-9a-f]{64}$'
    AND attestation_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT assistant_transcript_attestation_chat_log_fk
    FOREIGN KEY (chat_log_id, owner_user_id, thread_id)
    REFERENCES public.chat_log(id, owner_user_id, thread_id)
    ON DELETE CASCADE
);
CREATE INDEX assistant_transcript_attestation_chat_fk_idx
  ON chat_integrity.assistant_transcript_attestation_v1(
    chat_log_id, owner_user_id, thread_id
  );
ALTER TABLE chat_integrity.assistant_transcript_attestation_v1 OWNER TO sage;
ALTER TABLE chat_integrity.assistant_transcript_attestation_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_integrity.assistant_transcript_attestation_v1
  FORCE ROW LEVEL SECURITY;
CREATE POLICY assistant_transcript_attestation_owner_select
  ON chat_integrity.assistant_transcript_attestation_v1
  FOR SELECT TO brains_app, sage
  USING (
    owner_user_id = (
      SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
    )
  );
CREATE POLICY assistant_transcript_attestation_owner_insert
  ON chat_integrity.assistant_transcript_attestation_v1
  FOR INSERT TO brains_app, sage
  WITH CHECK (
    owner_user_id = (
      SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
    )
  );
REVOKE ALL ON chat_integrity.assistant_transcript_attestation_v1
  FROM PUBLIC, brains_app, memory_ingest_writer, memory_erasure_requester,
       governed_memory_worker, governed_memory_api;
GRANT SELECT, INSERT ON chat_integrity.assistant_transcript_attestation_v1
  TO brains_app;

-- Remove the last required chat-table dependency on the legacy memory schema.
-- These direct owner policies preserve the pre-cutover DML surface while
-- refusing every request without an exact transaction-local actor UUID.
DROP POLICY IF EXISTS raw_owner_isolation ON public.chat_log;
DROP POLICY IF EXISTS chat_log_owner_isolation ON public.chat_log;
CREATE POLICY chat_log_owner_isolation ON public.chat_log
  FOR ALL TO brains_app, sage
  USING (
    owner_user_id = (
      SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
    )
  )
  WITH CHECK (
    owner_user_id = (
      SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
    )
  );
DROP POLICY IF EXISTS raw_owner_isolation ON public.threads;
DROP POLICY IF EXISTS threads_owner_isolation ON public.threads;
CREATE POLICY threads_owner_isolation ON public.threads
  FOR ALL TO brains_app, sage
  USING (
    owner_user_id = (
      SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
    )
  )
  WITH CHECK (
    owner_user_id = (
      SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
    )
  );
DROP POLICY IF EXISTS chat_attachments_owner_isolation
  ON public.chat_attachments;
CREATE POLICY chat_attachments_owner_isolation ON public.chat_attachments
  FOR ALL TO brains_app, sage
  USING (
    owner_user_id = (
      SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
    )
  )
  WITH CHECK (
    owner_user_id = (
      SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
    )
  );
DO $active_thread_policy$
BEGIN
  IF pg_catalog.to_regclass('public.active_thread_selection') IS NOT NULL THEN
    EXECUTE 'ALTER TABLE public.active_thread_selection '
      'ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE public.active_thread_selection '
      'FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS active_thread_owner_isolation '
      'ON public.active_thread_selection';
    EXECUTE 'DROP POLICY IF EXISTS active_thread_selection_owner_isolation '
      'ON public.active_thread_selection';
    EXECUTE 'CREATE POLICY active_thread_selection_owner_isolation '
      'ON public.active_thread_selection FOR ALL TO brains_app, sage '
      'USING (owner_user_id = (SELECT NULLIF('
      'pg_catalog.current_setting(''app.user_id'', true), '''')::uuid)) '
      'WITH CHECK (owner_user_id = (SELECT NULLIF('
      'pg_catalog.current_setting(''app.user_id'', true), '''')::uuid))';
  END IF;
END;
$active_thread_policy$;

CREATE SCHEMA memory_ingest_private AUTHORIZATION sage;
REVOKE ALL ON SCHEMA memory_ingest_private FROM PUBLIC;
GRANT USAGE ON SCHEMA memory_ingest_private
  TO memory_ingest_writer, memory_erasure_requester,
     governed_memory_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE sage IN SCHEMA memory_ingest_private
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE FUNCTION memory_ingest_private.framed_utf8_field(
  p_name text,
  p_value text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT p_name || ':' || CASE
    WHEN p_value IS NULL THEN '-:' || E'\n'
    ELSE pg_catalog.octet_length(
           pg_catalog.convert_to(normalize(p_value, NFC), 'UTF8')
         )::text
         || ':' || normalize(p_value, NFC) || E'\n'
  END
$function$;
REVOKE ALL ON FUNCTION memory_ingest_private.framed_utf8_field(text,text)
  FROM PUBLIC;

CREATE FUNCTION memory_ingest_private.deletion_confirmation_sha256(
  p_operation_id uuid,
  p_selector_kind text,
  p_thread_id uuid,
  p_anchor_message_id uuid,
  p_recent_seconds integer
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.conversation_deletion_confirmation.v1' || E'\n'
      || memory_ingest_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'selector_kind', p_selector_kind
         )
      || memory_ingest_private.framed_utf8_field(
           'thread_id', p_thread_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'anchor_message_id', p_anchor_message_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'recent_seconds', p_recent_seconds::text
         )
      || memory_ingest_private.framed_utf8_field(
           'confirmation_phrase', CASE p_selector_kind
             WHEN 'message_tail' THEN 'DELETE MESSAGE AND FOLLOWING'
             WHEN 'thread' THEN 'DELETE CHAT'
             WHEN 'recent' THEN 'FORGET RECENT CONVERSATIONS'
             WHEN 'all_conversations' THEN 'DELETE CHAT DATA'
             ELSE NULL::text
           END
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION
  memory_ingest_private.deletion_confirmation_sha256(
    uuid,text,uuid,uuid,integer
  ) FROM PUBLIC;

CREATE FUNCTION memory_ingest_private.timestamp_utc_text(p_value timestamptz)
RETURNS text
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT CASE WHEN p_value IS NULL THEN NULL::text ELSE pg_catalog.to_char(
    p_value AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
  ) END
$function$;
REVOKE ALL ON FUNCTION memory_ingest_private.timestamp_utc_text(timestamptz)
  FROM PUBLIC;

CREATE FUNCTION memory_ingest_private.assert_chat_deletion_catalog()
RETURNS void
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  active_edge_count integer := 0;
  attestation_message_edge_count integer := 0;
  attachment_message_edge_count integer := 0;
  attachment_thread_edge_count integer := 0;
  chat_thread_edge_count integer := 0;
  catalog_relation record;
  deletion_roots oid[];
  inbound_fk record;
  message_tombstone_operation_edge_count integer := 0;
  source_target_operation_edge_count integer := 0;
  thread_target_operation_edge_count integer := 0;
  thread_tombstone_operation_edge_count integer := 0;
  trusted_assistant_edge_count integer := 0;
  trusted_user_edge_count integer := 0;
BEGIN
  deletion_roots := ARRAY[
    'public.chat_log'::regclass::oid,
    'public.threads'::regclass::oid,
    'public.chat_attachments'::regclass::oid,
    'memory_ingest_private.memory_ingest_outbox'::regclass::oid,
    'memory_ingest_private.source_erasure_operation'::regclass::oid,
    'memory_ingest_private.source_erasure_target'::regclass::oid,
    'memory_ingest_private.source_erasure_thread_target'::regclass::oid,
    'memory_ingest_private.source_erasure_message_tombstone'::regclass::oid,
    'memory_ingest_private.source_erasure_thread_tombstone'::regclass::oid,
    'memory_ingest_private.source_erasure_receipt'::regclass::oid,
    'chat_integrity.assistant_transcript_attestation_v1'::regclass::oid,
    pg_catalog.to_regclass('public.active_thread_selection')::oid,
    pg_catalog.to_regclass('trusted_web.response_transcript_v1')::oid
  ];
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.unnest(deletion_roots) AS root(root_oid)
    JOIN pg_catalog.pg_class AS relation ON relation.oid = root.root_oid
    WHERE root.root_oid IS NOT NULL
      AND relation.relkind <> 'r'
  ) THEN
    RAISE EXCEPTION 'chat deletion roots must be ordinary tables';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('public.chat_log'::regclass::oid),
      ('public.threads'::regclass::oid),
      ('public.chat_attachments'::regclass::oid)
    ) AS expected(relation_oid)
    WHERE pg_catalog.has_table_privilege(
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
         'INSERT:false', 'SELECT:false', 'UPDATE:false'
       ]::text[]
  ) THEN
    RAISE EXCEPTION 'brains_app retains direct chat deletion authority';
  END IF;
  FOR catalog_relation IN
    SELECT * FROM (VALUES
      ('chat_integrity.assistant_transcript_attestation_v1'::regclass::oid,
       ARRAY[
         'answer_id', 'owner_user_id', 'thread_id', 'chat_log_id',
         'request_id_sha256', 'conversation_snapshot_sha256',
         'trusted_plan_sha256', 'provider_request_sha256',
         'provider_response_sha256', 'provider_response_id', 'output_kind',
         'assistant_text_sha256', 'attestation_sha256', 'created_at'
       ]::text[], ARRAY[
         'uuid'::regtype, 'uuid'::regtype, 'uuid'::regtype,
         'uuid'::regtype, 'text'::regtype, 'text'::regtype,
         'text'::regtype, 'text'::regtype, 'text'::regtype,
         'text'::regtype, 'text'::regtype, 'text'::regtype,
         'text'::regtype, 'timestamptz'::regtype
       ]::oid[]),
      ('memory_ingest_private.source_erasure_target'::regclass::oid,
       ARRAY[
         'owner_user_id', 'operation_id', 'message_id', 'thread_id',
         'source_created_at', 'target_sha256'
       ]::text[], ARRAY[
         'uuid'::regtype, 'uuid'::regtype, 'uuid'::regtype,
         'uuid'::regtype, 'timestamptz'::regtype, 'text'::regtype
       ]::oid[]),
      ('memory_ingest_private.source_erasure_thread_target'::regclass::oid,
       ARRAY[
         'owner_user_id', 'operation_id', 'thread_id',
         'source_created_at', 'target_sha256'
       ]::text[], ARRAY[
         'uuid'::regtype, 'uuid'::regtype, 'uuid'::regtype,
         'timestamptz'::regtype, 'text'::regtype
       ]::oid[]),
      ('memory_ingest_private.source_erasure_message_tombstone'::regclass::oid,
       ARRAY[
         'message_id', 'owner_user_id', 'operation_id', 'erased_at'
       ]::text[], ARRAY[
         'uuid'::regtype, 'uuid'::regtype, 'uuid'::regtype,
         'timestamptz'::regtype
       ]::oid[]),
      ('memory_ingest_private.source_erasure_thread_tombstone'::regclass::oid,
       ARRAY[
         'thread_id', 'owner_user_id', 'operation_id', 'erased_at'
       ]::text[], ARRAY[
         'uuid'::regtype, 'uuid'::regtype, 'uuid'::regtype,
         'timestamptz'::regtype
       ]::oid[]),
      ('memory_ingest_private.source_erasure_receipt'::regclass::oid,
       ARRAY[
         'receipt_id', 'owner_user_id', 'operation_id', 'selector_sha256',
         'target_manifest_sha256', 'target_count',
         'thread_target_manifest_sha256', 'thread_target_count',
         'deleted_message_count', 'deleted_thread_count',
         'deleted_attachment_count', 'deleted_bridge_row_count',
         'message_tombstone_count', 'thread_tombstone_count',
         'tombstone_manifest_sha256', 'governed_receipt_sha256',
         'receipt_sha256', 'completed_at'
       ]::text[], ARRAY[
         'uuid'::regtype, 'uuid'::regtype, 'uuid'::regtype,
         'text'::regtype, 'text'::regtype, 'integer'::regtype,
         'text'::regtype, 'integer'::regtype, 'integer'::regtype,
         'integer'::regtype, 'integer'::regtype, 'integer'::regtype,
         'integer'::regtype, 'integer'::regtype, 'text'::regtype,
         'text'::regtype, 'text'::regtype, 'timestamptz'::regtype
       ]::oid[])
    ) AS expected(relation_oid, column_names, column_types)
  LOOP
    IF (
      SELECT pg_catalog.array_agg(attribute.attname::text
               ORDER BY attribute.attnum)
      FROM pg_catalog.pg_attribute AS attribute
      WHERE attribute.attrelid = catalog_relation.relation_oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
    ) IS DISTINCT FROM catalog_relation.column_names OR (
      SELECT pg_catalog.array_agg(attribute.atttypid
               ORDER BY attribute.attnum)
      FROM pg_catalog.pg_attribute AS attribute
      WHERE attribute.attrelid = catalog_relation.relation_oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
    ) IS DISTINCT FROM catalog_relation.column_types OR EXISTS (
      SELECT 1
      FROM pg_catalog.pg_attribute AS attribute
      WHERE attribute.attrelid = catalog_relation.relation_oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
        AND NOT attribute.attnotnull
    ) THEN
      RAISE EXCEPTION 'chat deletion private schema differs: %',
        catalog_relation.relation_oid::regclass;
    END IF;
  END LOOP;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_class AS relation
    WHERE relation.oid =
          'chat_integrity.assistant_transcript_attestation_v1'::regclass
      AND relation.relkind = 'r'
      AND relation.relowner = 'sage'::regrole
      AND relation.relrowsecurity
      AND relation.relforcerowsecurity
  ) OR (
    SELECT pg_catalog.count(*)
    FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'chat_integrity.assistant_transcript_attestation_v1'::regclass
  ) <> 7 OR NOT pg_catalog.has_schema_privilege(
    'brains_app', 'chat_integrity', 'USAGE'
  ) OR pg_catalog.has_schema_privilege(
    'public', 'chat_integrity', 'USAGE'
  ) OR NOT pg_catalog.has_table_privilege(
    'brains_app',
    'chat_integrity.assistant_transcript_attestation_v1', 'SELECT'
  ) OR NOT pg_catalog.has_table_privilege(
    'brains_app',
    'chat_integrity.assistant_transcript_attestation_v1', 'INSERT'
  ) OR pg_catalog.has_table_privilege(
    'brains_app',
    'chat_integrity.assistant_transcript_attestation_v1', 'UPDATE'
  ) OR pg_catalog.has_table_privilege(
    'brains_app',
    'chat_integrity.assistant_transcript_attestation_v1', 'DELETE'
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_index AS index_row
    WHERE index_row.indexrelid = pg_catalog.to_regclass(
            'chat_integrity.assistant_transcript_attestation_chat_fk_idx'
          )
      AND index_row.indrelid =
          'chat_integrity.assistant_transcript_attestation_v1'::regclass
      AND NOT index_row.indisunique
      AND index_row.indisvalid
      AND index_row.indisready
      AND index_row.indnkeyatts = 3
      AND index_row.indnatts = 3
      AND index_row.indpred IS NULL
      AND index_row.indexprs IS NULL
      AND pg_catalog.pg_get_indexdef(index_row.indexrelid, 1, true) =
          'chat_log_id'
      AND pg_catalog.pg_get_indexdef(index_row.indexrelid, 2, true) =
          'owner_user_id'
      AND pg_catalog.pg_get_indexdef(index_row.indexrelid, 3, true) =
          'thread_id'
  ) OR (
    SELECT pg_catalog.count(*)
    FROM pg_catalog.pg_policy AS policy
    WHERE policy.polrelid =
          'chat_integrity.assistant_transcript_attestation_v1'::regclass
  ) <> 2 OR (
    SELECT pg_catalog.count(*)
    FROM pg_catalog.pg_policy AS policy
    WHERE policy.polrelid =
          'chat_integrity.assistant_transcript_attestation_v1'::regclass
      AND policy.polpermissive
      AND pg_catalog.cardinality(policy.polroles) = 2
      AND policy.polroles @> ARRAY[
        'brains_app'::regrole::oid,
        'sage'::regrole::oid
      ]
      AND (
        (
          policy.polname = 'assistant_transcript_attestation_owner_select'
          AND policy.polcmd = 'r'
          AND policy.polqual IS NOT NULL
          AND policy.polwithcheck IS NULL
        ) OR (
          policy.polname = 'assistant_transcript_attestation_owner_insert'
          AND policy.polcmd = 'a'
          AND policy.polqual IS NULL
          AND policy.polwithcheck IS NOT NULL
        )
      )
      AND pg_catalog.strpos(
        COALESCE(pg_catalog.pg_get_expr(policy.polqual, policy.polrelid), '')
        || COALESCE(
             pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid), ''
           ),
        'current_setting'
      ) > 0
      AND pg_catalog.strpos(
        COALESCE(pg_catalog.pg_get_expr(policy.polqual, policy.polrelid), '')
        || COALESCE(
             pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid), ''
           ),
        'app.user_id'
      ) > 0
      AND pg_catalog.strpos(
        COALESCE(pg_catalog.pg_get_expr(policy.polqual, policy.polrelid), '')
        || COALESCE(
             pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid), ''
           ),
        'owner_user_id'
      ) > 0
      AND pg_catalog.strpos(
        COALESCE(pg_catalog.pg_get_expr(policy.polqual, policy.polrelid), '')
        || COALESCE(
             pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid), ''
           ),
        'memory.'
      ) = 0
  ) <> 2 THEN
    RAISE EXCEPTION 'chat integrity relation or authority differs';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('public.chat_log'::regclass::oid, 'chat_log_owner_isolation'::text),
      ('public.threads'::regclass::oid, 'threads_owner_isolation'::text),
      ('public.chat_attachments'::regclass::oid,
       'chat_attachments_owner_isolation'::text),
      (pg_catalog.to_regclass('public.active_thread_selection')::oid,
       'active_thread_selection_owner_isolation'::text)
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
            AND policy.polcmd = '*'
            AND policy.polpermissive
            AND pg_catalog.cardinality(policy.polroles) = 2
            AND policy.polroles @> ARRAY[
              'brains_app'::regrole::oid,
              'sage'::regrole::oid
            ]
            AND policy.polqual IS NOT NULL
            AND policy.polwithcheck IS NOT NULL
            AND pg_catalog.strpos(
              pg_catalog.pg_get_expr(policy.polqual, policy.polrelid),
              'owner_user_id'
            ) > 0
            AND pg_catalog.strpos(
              pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid),
              'owner_user_id'
            ) > 0
            AND pg_catalog.strpos(
              pg_catalog.pg_get_expr(policy.polqual, policy.polrelid),
              'app.user_id'
            ) > 0
            AND pg_catalog.strpos(
              pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid),
              'app.user_id'
            ) > 0
        )
      )
  ) OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_policy AS policy
    WHERE policy.polrelid IN (
      'public.chat_log'::regclass,
      'public.threads'::regclass,
      'public.chat_attachments'::regclass,
      pg_catalog.to_regclass('public.active_thread_selection')
    )
      AND (
        pg_catalog.strpos(
          COALESCE(pg_catalog.pg_get_expr(policy.polqual, policy.polrelid), '')
          || COALESCE(
               pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid), ''
             ),
          'memory.current_actor_user_id'
        ) > 0
        OR pg_catalog.strpos(
          COALESCE(pg_catalog.pg_get_expr(policy.polqual, policy.polrelid), '')
          || COALESCE(
               pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid), ''
             ),
          'current_setting'
        ) = 0
      )
  ) THEN
    RAISE EXCEPTION 'chat owner policy retains legacy Memory authority';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('memory_ingest_private.source_erasure_target'::regclass::oid,
       'source_erasure_target_pkey'::text, 'p'::"char"),
      ('memory_ingest_private.source_erasure_target'::regclass::oid,
       'source_erasure_target_operation_fk'::text, 'f'::"char"),
      ('memory_ingest_private.source_erasure_target'::regclass::oid,
       'source_erasure_target_hash'::text, 'c'::"char"),
      ('memory_ingest_private.source_erasure_thread_target'::regclass::oid,
       'source_erasure_thread_target_pkey'::text, 'p'::"char"),
      ('memory_ingest_private.source_erasure_thread_target'::regclass::oid,
       'source_erasure_thread_target_operation_fk'::text, 'f'::"char"),
      ('memory_ingest_private.source_erasure_thread_target'::regclass::oid,
       'source_erasure_thread_target_hash'::text, 'c'::"char"),
      ('memory_ingest_private.source_erasure_message_tombstone'::regclass::oid,
       'source_erasure_message_tombstone_pkey'::text, 'p'::"char"),
      ('memory_ingest_private.source_erasure_message_tombstone'::regclass::oid,
       'source_erasure_message_tombstone_operation_fk'::text, 'f'::"char"),
      ('memory_ingest_private.source_erasure_thread_tombstone'::regclass::oid,
       'source_erasure_thread_tombstone_pkey'::text, 'p'::"char"),
      ('memory_ingest_private.source_erasure_thread_tombstone'::regclass::oid,
       'source_erasure_thread_tombstone_operation_fk'::text, 'f'::"char"),
      ('memory_ingest_private.source_erasure_receipt'::regclass::oid,
       'source_erasure_receipt_pkey'::text, 'p'::"char"),
      ('memory_ingest_private.source_erasure_receipt'::regclass::oid,
       'source_erasure_receipt_operation_unique'::text, 'u'::"char"),
      ('memory_ingest_private.source_erasure_receipt'::regclass::oid,
       'source_erasure_receipt_hashes'::text, 'c'::"char"),
      ('memory_ingest_private.source_erasure_receipt'::regclass::oid,
       'source_erasure_receipt_counts'::text, 'c'::"char")
    ) AS expected(relation_oid, constraint_name, constraint_type)
    LEFT JOIN pg_catalog.pg_constraint AS constraint_row
      ON constraint_row.conrelid = expected.relation_oid
     AND constraint_row.conname = expected.constraint_name
     AND constraint_row.contype = expected.constraint_type
     AND constraint_row.convalidated
    WHERE constraint_row.oid IS NULL
  ) OR (
    SELECT pg_catalog.count(*)
    FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid IN (
      'memory_ingest_private.source_erasure_target'::regclass,
      'memory_ingest_private.source_erasure_thread_target'::regclass,
      'memory_ingest_private.source_erasure_message_tombstone'::regclass,
      'memory_ingest_private.source_erasure_thread_tombstone'::regclass,
      'memory_ingest_private.source_erasure_receipt'::regclass
    )
  ) <> 14 THEN
    RAISE EXCEPTION 'chat deletion private constraint inventory differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_target'::regclass
      AND constraint_row.conname = 'source_erasure_target_pkey'
      AND pg_catalog.pg_get_constraintdef(constraint_row.oid, true) =
          'PRIMARY KEY (owner_user_id, operation_id, message_id)'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_thread_target'::regclass
      AND constraint_row.conname = 'source_erasure_thread_target_pkey'
      AND pg_catalog.pg_get_constraintdef(constraint_row.oid, true) =
          'PRIMARY KEY (owner_user_id, operation_id, thread_id)'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_message_tombstone'::regclass
      AND constraint_row.conname =
          'source_erasure_message_tombstone_pkey'
      AND pg_catalog.pg_get_constraintdef(constraint_row.oid, true) =
          'PRIMARY KEY (message_id)'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_thread_tombstone'::regclass
      AND constraint_row.conname =
          'source_erasure_thread_tombstone_pkey'
      AND pg_catalog.pg_get_constraintdef(constraint_row.oid, true) =
          'PRIMARY KEY (thread_id)'
  ) THEN
    RAISE EXCEPTION 'chat deletion target or tombstone identity differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_target'::regclass
      AND constraint_row.conname = 'source_erasure_target_hash'
      AND pg_catalog.strpos(
            pg_catalog.pg_get_constraintdef(constraint_row.oid, true),
            'target_sha256'
          ) > 0
      AND pg_catalog.strpos(
            pg_catalog.pg_get_constraintdef(constraint_row.oid, true),
            '64'
          ) > 0
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_thread_target'::regclass
      AND constraint_row.conname = 'source_erasure_thread_target_hash'
      AND pg_catalog.strpos(
            pg_catalog.pg_get_constraintdef(constraint_row.oid, true),
            'target_sha256'
          ) > 0
      AND pg_catalog.strpos(
            pg_catalog.pg_get_constraintdef(constraint_row.oid, true),
            '64'
          ) > 0
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_receipt'::regclass
      AND constraint_row.conname = 'source_erasure_receipt_hashes'
      AND pg_catalog.strpos(
            pg_catalog.pg_get_constraintdef(constraint_row.oid, true),
            'thread_target_manifest_sha256'
          ) > 0
      AND pg_catalog.strpos(
            pg_catalog.pg_get_constraintdef(constraint_row.oid, true),
            'tombstone_manifest_sha256'
          ) > 0
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_receipt'::regclass
      AND constraint_row.conname = 'source_erasure_receipt_counts'
      AND pg_catalog.strpos(
            pg_catalog.pg_get_constraintdef(constraint_row.oid, true),
            'message_tombstone_count'
          ) > 0
      AND pg_catalog.strpos(
            pg_catalog.pg_get_constraintdef(constraint_row.oid, true),
            'thread_tombstone_count'
          ) > 0
  ) THEN
    RAISE EXCEPTION 'chat deletion target or receipt check differs';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.contype = 'f'
      AND constraint_row.conrelid IN (
        'memory_ingest_private.memory_ingest_outbox'::regclass,
        'memory_ingest_private.source_erasure_operation'::regclass,
        'memory_ingest_private.source_erasure_target'::regclass,
        'memory_ingest_private.source_erasure_thread_target'::regclass,
        'memory_ingest_private.source_erasure_message_tombstone'::regclass,
        'memory_ingest_private.source_erasure_thread_tombstone'::regclass,
        'memory_ingest_private.source_erasure_receipt'::regclass
      )
      AND NOT (
        constraint_row.conrelid =
          'memory_ingest_private.source_erasure_target'::regclass
        AND constraint_row.conname = 'source_erasure_target_operation_fk'
        AND constraint_row.confrelid =
          'memory_ingest_private.source_erasure_operation'::regclass
        AND constraint_row.confdeltype = 'r'
        AND constraint_row.confupdtype = 'a'
        AND constraint_row.convalidated
        AND NOT constraint_row.condeferrable
        AND NOT constraint_row.condeferred
      )
      AND NOT (
        constraint_row.conrelid =
          'memory_ingest_private.source_erasure_thread_target'::regclass
        AND constraint_row.conname =
          'source_erasure_thread_target_operation_fk'
        AND constraint_row.confrelid =
          'memory_ingest_private.source_erasure_operation'::regclass
        AND constraint_row.confdeltype = 'r'
        AND constraint_row.confupdtype = 'a'
        AND constraint_row.convalidated
        AND NOT constraint_row.condeferrable
        AND NOT constraint_row.condeferred
      )
      AND NOT (
        constraint_row.conrelid =
          'memory_ingest_private.source_erasure_message_tombstone'::regclass
        AND constraint_row.conname =
          'source_erasure_message_tombstone_operation_fk'
        AND constraint_row.confrelid =
          'memory_ingest_private.source_erasure_operation'::regclass
        AND constraint_row.confdeltype = 'r'
        AND constraint_row.confupdtype = 'a'
        AND constraint_row.convalidated
        AND NOT constraint_row.condeferrable
        AND NOT constraint_row.condeferred
      )
      AND NOT (
        constraint_row.conrelid =
          'memory_ingest_private.source_erasure_thread_tombstone'::regclass
        AND constraint_row.conname =
          'source_erasure_thread_tombstone_operation_fk'
        AND constraint_row.confrelid =
          'memory_ingest_private.source_erasure_operation'::regclass
        AND constraint_row.confdeltype = 'r'
        AND constraint_row.confupdtype = 'a'
        AND constraint_row.convalidated
        AND NOT constraint_row.condeferrable
        AND NOT constraint_row.condeferred
      )
  ) THEN
    RAISE EXCEPTION 'private deletion root outbound dependency differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.chat_attachments'::regclass
      AND attribute.attname = 'owner_user_id'
      AND attribute.atttypid = 'uuid'::regtype
      AND attribute.attnotnull
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.chat_attachments'::regclass
      AND attribute.attname = 'thread_id'
      AND attribute.atttypid = 'uuid'::regtype
      AND attribute.attnotnull
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.chat_attachments'::regclass
      AND attribute.attname = 'message_id'
      AND attribute.atttypid = 'uuid'::regtype
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
  ) THEN
    RAISE EXCEPTION 'attachment deletion columns differ';
  END IF;
  IF pg_catalog.to_regclass('trusted_web.response_transcript_v1') IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM (VALUES
         ('owner_user_id', 'uuid', true),
         ('thread_id', 'uuid', true),
         ('user_chat_log_id', 'uuid', true),
         ('assistant_chat_log_id', 'uuid', true)
       ) AS expected(column_name, type_name, required_not_null)
       LEFT JOIN pg_catalog.pg_attribute AS attribute
         ON attribute.attrelid =
              pg_catalog.to_regclass('trusted_web.response_transcript_v1')
        AND attribute.attname = expected.column_name
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
       WHERE attribute.attnum IS NULL
          OR attribute.atttypid <> expected.type_name::regtype
          OR attribute.attnotnull IS DISTINCT FROM expected.required_not_null
     ) THEN
    RAISE EXCEPTION 'response transcript deletion columns differ';
  END IF;

  FOR inbound_fk IN
    SELECT constraint_row.conname,
      child_namespace.nspname AS child_schema,
      child_relation.relname AS child_table,
      parent_namespace.nspname AS parent_schema,
      parent_relation.relname AS parent_table,
      constraint_row.confdeltype,
      constraint_row.confupdtype,
      constraint_row.confmatchtype,
      constraint_row.condeferrable,
      constraint_row.condeferred,
      constraint_row.convalidated,
      ARRAY(
        SELECT attribute.attname::text
        FROM pg_catalog.unnest(constraint_row.conkey)
          WITH ORDINALITY AS key_column(attnum, ordinal_position)
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = constraint_row.conrelid
         AND attribute.attnum = key_column.attnum
        ORDER BY key_column.ordinal_position
      ) AS child_columns,
      ARRAY(
        SELECT attribute.attname::text
        FROM pg_catalog.unnest(constraint_row.confkey)
          WITH ORDINALITY AS key_column(attnum, ordinal_position)
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = constraint_row.confrelid
         AND attribute.attnum = key_column.attnum
        ORDER BY key_column.ordinal_position
      ) AS parent_columns
    FROM pg_catalog.pg_constraint AS constraint_row
    JOIN pg_catalog.pg_class AS child_relation
      ON child_relation.oid = constraint_row.conrelid
    JOIN pg_catalog.pg_namespace AS child_namespace
      ON child_namespace.oid = child_relation.relnamespace
    JOIN pg_catalog.pg_class AS parent_relation
      ON parent_relation.oid = constraint_row.confrelid
    JOIN pg_catalog.pg_namespace AS parent_namespace
      ON parent_namespace.oid = parent_relation.relnamespace
    WHERE constraint_row.contype = 'f'
      AND constraint_row.confrelid = ANY(deletion_roots)
  LOOP
    IF inbound_fk.confupdtype <> 'a'
       OR inbound_fk.confmatchtype <> 's'
       OR inbound_fk.condeferrable
       OR inbound_fk.condeferred THEN
      RAISE EXCEPTION
        'unsafe inbound chat deletion dependency: %.% -> %.%',
        inbound_fk.child_schema, inbound_fk.child_table,
        inbound_fk.parent_schema, inbound_fk.parent_table;
    ELSIF inbound_fk.child_schema = 'public'
       AND inbound_fk.child_table = 'chat_log'
       AND inbound_fk.conname = 'chat_log_owner_thread_fk'
       AND inbound_fk.parent_schema = 'public'
       AND inbound_fk.parent_table = 'threads'
       AND inbound_fk.child_columns =
           ARRAY['owner_user_id', 'thread_id']::text[]
       AND inbound_fk.parent_columns =
           ARRAY['owner_user_id', 'id']::text[]
       AND inbound_fk.confdeltype = 'a'
       AND NOT inbound_fk.convalidated THEN
      chat_thread_edge_count := chat_thread_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'public'
       AND inbound_fk.child_table = 'chat_attachments'
       AND inbound_fk.conname = 'chat_attachments_thread_owner_fk'
       AND inbound_fk.parent_schema = 'public'
       AND inbound_fk.parent_table = 'threads'
       AND inbound_fk.child_columns =
           ARRAY['thread_id', 'owner_user_id']::text[]
       AND inbound_fk.parent_columns =
           ARRAY['id', 'owner_user_id']::text[]
       AND inbound_fk.confdeltype = 'c'
       AND inbound_fk.convalidated THEN
      attachment_thread_edge_count := attachment_thread_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'public'
       AND inbound_fk.child_table = 'chat_attachments'
       AND inbound_fk.conname =
           'chat_attachments_message_owner_thread_fk'
       AND inbound_fk.parent_schema = 'public'
       AND inbound_fk.parent_table = 'chat_log'
       AND inbound_fk.child_columns =
           ARRAY['message_id', 'owner_user_id', 'thread_id']::text[]
       AND inbound_fk.parent_columns =
           ARRAY['id', 'owner_user_id', 'thread_id']::text[]
       AND inbound_fk.confdeltype = 'c'
       AND inbound_fk.convalidated THEN
      attachment_message_edge_count := attachment_message_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'public'
       AND inbound_fk.child_table = 'active_thread_selection'
       AND inbound_fk.conname =
           'active_thread_selection_owner_thread_fk'
       AND inbound_fk.parent_schema = 'public'
       AND inbound_fk.parent_table = 'threads'
       AND inbound_fk.child_columns =
           ARRAY['owner_user_id', 'thread_id']::text[]
       AND inbound_fk.parent_columns =
           ARRAY['owner_user_id', 'id']::text[]
       AND inbound_fk.confdeltype = 'c'
       AND inbound_fk.convalidated THEN
      active_edge_count := active_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'chat_integrity'
       AND inbound_fk.child_table = 'assistant_transcript_attestation_v1'
       AND inbound_fk.conname =
           'assistant_transcript_attestation_chat_log_fk'
       AND inbound_fk.parent_schema = 'public'
       AND inbound_fk.parent_table = 'chat_log'
       AND inbound_fk.child_columns =
           ARRAY['chat_log_id', 'owner_user_id', 'thread_id']::text[]
       AND inbound_fk.parent_columns =
           ARRAY['id', 'owner_user_id', 'thread_id']::text[]
       AND inbound_fk.confdeltype = 'c'
       AND inbound_fk.convalidated THEN
      attestation_message_edge_count :=
        attestation_message_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'trusted_web'
       AND inbound_fk.child_table = 'response_transcript_v1'
       AND inbound_fk.conname =
           'response_transcript_v1_user_chat_log_id_fkey'
       AND inbound_fk.parent_schema = 'public'
       AND inbound_fk.parent_table = 'chat_log'
       AND inbound_fk.child_columns = ARRAY[
         'user_chat_log_id', 'owner_user_id', 'thread_id'
       ]::text[]
       AND inbound_fk.parent_columns =
           ARRAY['id', 'owner_user_id', 'thread_id']::text[]
       AND inbound_fk.confdeltype = 'c'
       AND inbound_fk.convalidated THEN
      trusted_user_edge_count := trusted_user_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'trusted_web'
       AND inbound_fk.child_table = 'response_transcript_v1'
       AND inbound_fk.conname =
           'response_transcript_v1_assistant_chat_log_id_fkey'
       AND inbound_fk.parent_schema = 'public'
       AND inbound_fk.parent_table = 'chat_log'
       AND inbound_fk.child_columns =
           ARRAY[
             'assistant_chat_log_id', 'owner_user_id', 'thread_id'
           ]::text[]
       AND inbound_fk.parent_columns =
           ARRAY['id', 'owner_user_id', 'thread_id']::text[]
       AND inbound_fk.confdeltype = 'c'
       AND inbound_fk.convalidated THEN
      trusted_assistant_edge_count := trusted_assistant_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'memory_ingest_private'
       AND inbound_fk.child_table = 'source_erasure_target'
       AND inbound_fk.conname = 'source_erasure_target_operation_fk'
       AND inbound_fk.parent_schema = 'memory_ingest_private'
       AND inbound_fk.parent_table = 'source_erasure_operation'
       AND inbound_fk.child_columns =
           ARRAY['owner_user_id', 'operation_id']::text[]
       AND inbound_fk.parent_columns =
           ARRAY['owner_user_id', 'operation_id']::text[]
       AND inbound_fk.confdeltype = 'r'
       AND inbound_fk.convalidated THEN
      source_target_operation_edge_count :=
        source_target_operation_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'memory_ingest_private'
       AND inbound_fk.child_table = 'source_erasure_thread_target'
       AND inbound_fk.conname = 'source_erasure_thread_target_operation_fk'
       AND inbound_fk.parent_schema = 'memory_ingest_private'
       AND inbound_fk.parent_table = 'source_erasure_operation'
       AND inbound_fk.child_columns =
           ARRAY['owner_user_id', 'operation_id']::text[]
       AND inbound_fk.parent_columns =
           ARRAY['owner_user_id', 'operation_id']::text[]
       AND inbound_fk.confdeltype = 'r'
       AND inbound_fk.convalidated THEN
      thread_target_operation_edge_count :=
        thread_target_operation_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'memory_ingest_private'
       AND inbound_fk.child_table = 'source_erasure_message_tombstone'
       AND inbound_fk.conname =
           'source_erasure_message_tombstone_operation_fk'
       AND inbound_fk.parent_schema = 'memory_ingest_private'
       AND inbound_fk.parent_table = 'source_erasure_operation'
       AND inbound_fk.child_columns =
           ARRAY['owner_user_id', 'operation_id']::text[]
       AND inbound_fk.parent_columns =
           ARRAY['owner_user_id', 'operation_id']::text[]
       AND inbound_fk.confdeltype = 'r'
       AND inbound_fk.convalidated THEN
      message_tombstone_operation_edge_count :=
        message_tombstone_operation_edge_count + 1;
    ELSIF inbound_fk.child_schema = 'memory_ingest_private'
       AND inbound_fk.child_table = 'source_erasure_thread_tombstone'
       AND inbound_fk.conname =
           'source_erasure_thread_tombstone_operation_fk'
       AND inbound_fk.parent_schema = 'memory_ingest_private'
       AND inbound_fk.parent_table = 'source_erasure_operation'
       AND inbound_fk.child_columns =
           ARRAY['owner_user_id', 'operation_id']::text[]
       AND inbound_fk.parent_columns =
           ARRAY['owner_user_id', 'operation_id']::text[]
       AND inbound_fk.confdeltype = 'r'
       AND inbound_fk.convalidated THEN
      thread_tombstone_operation_edge_count :=
        thread_tombstone_operation_edge_count + 1;
    ELSE
      RAISE EXCEPTION
        'unclassified inbound chat deletion dependency: %.% -> %.%',
        inbound_fk.child_schema, inbound_fk.child_table,
        inbound_fk.parent_schema, inbound_fk.parent_table;
    END IF;
  END LOOP;

  IF chat_thread_edge_count <> 1
     OR attachment_thread_edge_count <> 1
     OR attachment_message_edge_count <> 1
     OR attestation_message_edge_count <> 1
     OR source_target_operation_edge_count <> 1
     OR thread_target_operation_edge_count <> 1
     OR message_tombstone_operation_edge_count <> 1
     OR thread_tombstone_operation_edge_count <> 1
     OR active_edge_count <> (
          CASE WHEN
            pg_catalog.to_regclass('public.active_thread_selection') IS NULL
          THEN 0 ELSE 1 END
        )
     OR trusted_user_edge_count <> (
          CASE WHEN
            pg_catalog.to_regclass('trusted_web.response_transcript_v1') IS NULL
          THEN 0 ELSE 1 END
        )
     OR trusted_assistant_edge_count <> (
          CASE WHEN
            pg_catalog.to_regclass('trusted_web.response_transcript_v1') IS NULL
          THEN 0 ELSE 1 END
        ) THEN
    RAISE EXCEPTION 'chat deletion dependency inventory differs';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('public.chat_log'::regclass::oid,
       'chat_log_guard_canonical_owner'::text, 'O'::"char", 23::smallint,
       pg_catalog.to_regprocedure('public.guard_canonical_owner()')),
      ('public.chat_log'::regclass::oid,
       'chat_log_guard_immutable'::text, 'O'::"char", 19::smallint,
       pg_catalog.to_regprocedure('public.guard_chat_log_immutable()')),
      ('public.chat_log'::regclass::oid,
       'chat_log_serialize_source_erasure'::text, 'O'::"char", 7::smallint,
       pg_catalog.to_regprocedure(
         'memory_ingest_private.serialize_chat_source_erasure()'
       )),
      ('public.threads'::regclass::oid,
       'threads_guard_canonical_owner'::text, 'O'::"char", 23::smallint,
       pg_catalog.to_regprocedure('public.guard_canonical_owner()')),
      ('public.threads'::regclass::oid,
       'threads_serialize_source_erasure'::text, 'O'::"char", 23::smallint,
       pg_catalog.to_regprocedure(
         'memory_ingest_private.serialize_thread_source_erasure()'
       )),
      ('public.chat_attachments'::regclass::oid,
       'chat_attachments_serialize_source_erasure'::text,
       'O'::"char", 23::smallint,
       pg_catalog.to_regprocedure(
         'memory_ingest_private.serialize_attachment_source_erasure()'
       )),
      (pg_catalog.to_regclass('trusted_web.response_transcript_v1')::oid,
       'response_transcript_serialize_source_erasure'::text,
       'O'::"char", 23::smallint,
       pg_catalog.to_regprocedure(
         'memory_ingest_private.serialize_response_transcript_source_erasure()'
       )),
      ('memory_ingest_private.source_erasure_receipt'::regclass::oid,
       'source_erasure_receipt_immutable'::text,
       'O'::"char", 27::smallint,
       pg_catalog.to_regprocedure(
         'memory_ingest_private.guard_source_erasure_receipt_immutable()'
       )),
      ('memory_ingest_private.source_erasure_message_tombstone'::regclass::oid,
       'source_erasure_message_tombstone_immutable'::text,
       'O'::"char", 27::smallint,
       pg_catalog.to_regprocedure(
         'memory_ingest_private.guard_source_erasure_receipt_immutable()'
       )),
      ('memory_ingest_private.source_erasure_thread_tombstone'::regclass::oid,
       'source_erasure_thread_tombstone_immutable'::text,
       'O'::"char", 27::smallint,
       pg_catalog.to_regprocedure(
         'memory_ingest_private.guard_source_erasure_receipt_immutable()'
       ))
    ) AS expected(
      relation_oid, trigger_name, enabled_state, trigger_type, function_oid
    )
    LEFT JOIN pg_catalog.pg_trigger AS trigger_row
      ON trigger_row.tgrelid = expected.relation_oid
     AND trigger_row.tgname = expected.trigger_name
     AND trigger_row.tgenabled = expected.enabled_state
     AND trigger_row.tgtype = expected.trigger_type
     AND trigger_row.tgfoid = expected.function_oid
     AND NOT trigger_row.tgisinternal
    WHERE expected.relation_oid IS NOT NULL
      AND trigger_row.oid IS NULL
  ) OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE trigger_row.tgrelid = 'public.chat_log'::regclass
      AND trigger_row.tgname =
          'chat_log_enqueue_memory_v1_consolidation'
      AND NOT trigger_row.tgisinternal
  ) OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE trigger_row.tgrelid = ANY(deletion_roots)
      AND NOT trigger_row.tgisinternal
      AND NOT EXISTS (
        SELECT 1
        FROM (VALUES
          ('public.chat_log'::regclass::oid,
           'chat_log_guard_canonical_owner'::text),
          ('public.chat_log'::regclass::oid,
           'chat_log_guard_immutable'::text),
          ('public.chat_log'::regclass::oid,
           'chat_log_serialize_source_erasure'::text),
          ('public.threads'::regclass::oid,
           'threads_guard_canonical_owner'::text),
          ('public.threads'::regclass::oid,
           'threads_serialize_source_erasure'::text),
          ('public.chat_attachments'::regclass::oid,
           'chat_attachments_serialize_source_erasure'::text),
          (pg_catalog.to_regclass(
             'trusted_web.response_transcript_v1'
           )::oid,
           'response_transcript_serialize_source_erasure'::text),
          ('memory_ingest_private.source_erasure_receipt'::regclass::oid,
           'source_erasure_receipt_immutable'::text),
          ('memory_ingest_private.source_erasure_message_tombstone'::regclass::oid,
           'source_erasure_message_tombstone_immutable'::text),
          ('memory_ingest_private.source_erasure_thread_tombstone'::regclass::oid,
           'source_erasure_thread_tombstone_immutable'::text)
        ) AS expected(relation_oid, trigger_name)
        WHERE expected.relation_oid = trigger_row.tgrelid
          AND expected.trigger_name = trigger_row.tgname
      )
  ) THEN
    RAISE EXCEPTION 'chat deletion mutation trigger inventory differs';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_rewrite AS rewrite_row
    WHERE rewrite_row.ev_class = ANY(deletion_roots)
  ) THEN
    RAISE EXCEPTION 'rewrite rule reaches chat deletion mutation state';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_inherits AS inheritance_row
    WHERE inheritance_row.inhrelid = ANY(deletion_roots)
       OR inheritance_row.inhparent = ANY(deletion_roots)
  ) THEN
    RAISE EXCEPTION 'chat deletion root inheritance is forbidden';
  END IF;
END;
$function$;
REVOKE ALL ON FUNCTION
  memory_ingest_private.assert_chat_deletion_catalog()
FROM PUBLIC;

CREATE FUNCTION memory_ingest_private.ingest_window_sha256(
  p_owner_user_id uuid,
  p_thread_id uuid,
  p_exchange_id uuid,
  p_window_id uuid,
  p_message_id uuid,
  p_content_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    '{"domain":"governed_memory.ingest_window","material":{'
      || '"content_sha256":'
      || pg_catalog.to_json(p_content_sha256)::text || ','
      || '"exchange_id":'
      || pg_catalog.to_json(p_exchange_id::text)::text || ','
      || '"message_id":'
      || pg_catalog.to_json(p_message_id::text)::text || ','
      || '"owner_user_id":'
      || pg_catalog.to_json(p_owner_user_id::text)::text || ','
      || '"thread_id":'
      || pg_catalog.to_json(p_thread_id::text)::text || ','
      || '"window_id":'
      || pg_catalog.to_json(p_window_id::text)::text
      || '}}',
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_ingest_private.ingest_window_sha256(
  uuid,uuid,uuid,uuid,uuid,text
) FROM PUBLIC;

DO $ingest_window_hash_self_check$
BEGIN
  IF memory_ingest_private.ingest_window_sha256(
       '00000000-0000-4000-8000-000000000001'::uuid,
       '00000000-0000-4000-8000-000000000002'::uuid,
       '00000000-0000-4000-8000-000000000003'::uuid,
       '00000000-0000-4000-8000-000000000004'::uuid,
       '00000000-0000-4000-8000-000000000005'::uuid,
       pg_catalog.repeat('1', 64)
     ) <> 'e9ffcbf890a6ce88bc705ba71354e77060449e0a378b5c3b2dc1138ef8105206'
  THEN
    RAISE EXCEPTION 'bridge ingest-window hash self-check failed';
  END IF;
END;
$ingest_window_hash_self_check$;

CREATE FUNCTION memory_ingest_private.source_binding_sha256(
  p_owner_user_id uuid,
  p_message_id uuid,
  p_thread_id uuid,
  p_exchange_id uuid,
  p_window_id uuid,
  p_window_ordinal integer,
  p_window_sha256 text,
  p_content_sha256 text,
  p_policy_sha256 text,
  p_source_created_at timestamptz
)
RETURNS text
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.bridge_source.v1' || E'\n'
      || memory_ingest_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'message_id', p_message_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'thread_id', p_thread_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'exchange_id', p_exchange_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'window_id', p_window_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'window_ordinal', p_window_ordinal::text
         )
      || memory_ingest_private.framed_utf8_field(
           'window_sha256', p_window_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'content_sha256', p_content_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'policy_sha256', p_policy_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'source_created_at',
           memory_ingest_private.timestamp_utc_text(p_source_created_at)
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_ingest_private.source_binding_sha256(
  uuid,uuid,uuid,uuid,uuid,integer,text,text,text,timestamptz
) FROM PUBLIC;

DO $source_binding_hash_self_check$
BEGIN
  IF memory_ingest_private.source_binding_sha256(
       '00000000-0000-4000-8000-000000000001'::uuid,
       '00000000-0000-4000-8000-000000000002'::uuid,
       '00000000-0000-4000-8000-000000000003'::uuid,
       '00000000-0000-4000-8000-000000000004'::uuid,
       '00000000-0000-4000-8000-000000000005'::uuid,
       7, pg_catalog.repeat('1', 64), pg_catalog.repeat('2', 64),
       pg_catalog.repeat('3', 64),
       '2026-08-09 12:34:56.123456+00'::timestamptz
     ) <> 'f1ed425554af394ea4a0b12cfcbf5dbd069b36131a55cb5411b3fc34d49ec279'
  THEN
    RAISE EXCEPTION 'bridge source binding hash self-check failed';
  END IF;
END;
$source_binding_hash_self_check$;

CREATE FUNCTION memory_ingest_private.terminal_receipt_sha256(
  p_source_binding_sha256 text,
  p_state text,
  p_decision text,
  p_context_review_count integer,
  p_successor_evidence_id uuid,
  p_successor_job_id uuid,
  p_error_code text,
  p_completed_at timestamptz
)
RETURNS text
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.conversation_terminal_receipt.v1' || E'\n'
      || memory_ingest_private.framed_utf8_field(
           'source_binding_sha256', p_source_binding_sha256
         )
      || memory_ingest_private.framed_utf8_field('state', p_state)
      || memory_ingest_private.framed_utf8_field('decision', p_decision)
      || memory_ingest_private.framed_utf8_field(
           'context_review_count', p_context_review_count::text
         )
      || memory_ingest_private.framed_utf8_field(
           'successor_evidence_id', p_successor_evidence_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'successor_job_id', p_successor_job_id::text
         )
      || memory_ingest_private.framed_utf8_field('error_code', p_error_code)
      || memory_ingest_private.framed_utf8_field(
           'completed_at',
           memory_ingest_private.timestamp_utc_text(p_completed_at)
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_ingest_private.terminal_receipt_sha256(
  text,text,text,integer,uuid,uuid,text,timestamptz
) FROM PUBLIC;

CREATE TABLE memory_ingest_private.memory_ingest_outbox (
  outbox_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  message_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  exchange_id uuid NOT NULL,
  window_id uuid NOT NULL,
  window_ordinal integer NOT NULL,
  window_sha256 text NOT NULL,
  content_sha256 text,
  source_binding_sha256 text NOT NULL,
  ingest_after timestamptz NOT NULL,
  source_created_at timestamptz NOT NULL,
  policy_sha256 text NOT NULL,
  state text NOT NULL DEFAULT 'pending',
  eligibility_decision text,
  context_review_count integer NOT NULL DEFAULT 0,
  attempt_count integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  available_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  lease_token uuid,
  claimed_by text,
  claimed_at timestamptz,
  lease_expires_at timestamptz,
  successor_evidence_id uuid,
  successor_job_id uuid,
  last_error_code text,
  content_hash_expires_at timestamptz NOT NULL,
  purge_after timestamptz NOT NULL,
  completed_at timestamptz,
  terminal_receipt_sha256 text,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT memory_ingest_outbox_owner_id UNIQUE (owner_user_id, outbox_id),
  CONSTRAINT memory_ingest_outbox_operation_unique UNIQUE (
    owner_user_id, operation_id
  ),
  CONSTRAINT memory_ingest_outbox_message_unique UNIQUE (
    owner_user_id, message_id
  ),
  CONSTRAINT memory_ingest_outbox_owner_nonzero CHECK (
    owner_user_id <> '00000000-0000-0000-0000-000000000000'::uuid
  ),
  CONSTRAINT memory_ingest_outbox_hashes CHECK (
    (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$')
    AND source_binding_sha256 ~ '^[0-9a-f]{64}$'
    AND window_sha256 ~ '^[0-9a-f]{64}$'
    AND policy_sha256 ~ '^[0-9a-f]{64}$'
    AND (
      terminal_receipt_sha256 IS NULL
      OR terminal_receipt_sha256 ~ '^[0-9a-f]{64}$'
    )
  ),
  CONSTRAINT memory_ingest_outbox_cutover CHECK (
    source_created_at >= ingest_after
    AND content_hash_expires_at > created_at
    AND content_hash_expires_at <= created_at + interval '24 hours'
    AND purge_after > content_hash_expires_at
    AND purge_after <= created_at + interval '31 days'
  ),
  CONSTRAINT memory_ingest_outbox_window CHECK (
    window_id <> '00000000-0000-0000-0000-000000000000'::uuid
    AND window_ordinal BETWEEN 0 AND 10000
  ),
  CONSTRAINT memory_ingest_outbox_context_review CHECK (
    context_review_count BETWEEN 0 AND 1
    AND (
      context_review_count = 0
      OR eligibility_decision = 'review_context'
      OR state IN (
        'completed', 'skipped', 'failed_terminal', 'erasure_cancelled'
      )
    )
  ),
  CONSTRAINT memory_ingest_outbox_state CHECK (
    state IN (
      'pending', 'claimed', 'retryable', 'completed', 'skipped',
      'expired', 'failed_terminal', 'erasure_cancelled'
    )
  ),
  CONSTRAINT memory_ingest_outbox_decision CHECK (
    eligibility_decision IS NULL OR eligibility_decision IN (
      'send_external', 'skip_zero_call', 'route_internal',
      'block_local', 'review_context'
    )
  ),
  CONSTRAINT memory_ingest_outbox_attempts CHECK (
    max_attempts BETWEEN 1 AND 5
    AND attempt_count BETWEEN 0 AND max_attempts
  ),
  CONSTRAINT memory_ingest_outbox_lease_shape CHECK (
    (state = 'claimed'
      AND lease_token IS NOT NULL
      AND claimed_by IS NOT NULL
      AND claimed_at IS NOT NULL
      AND lease_expires_at > claimed_at)
    OR
    (state <> 'claimed'
      AND lease_token IS NULL
      AND claimed_by IS NULL
      AND claimed_at IS NULL
      AND lease_expires_at IS NULL)
  ),
  CONSTRAINT memory_ingest_outbox_terminal_shape CHECK (
    (state IN (
      'completed', 'skipped', 'expired', 'failed_terminal',
      'erasure_cancelled'
    )
      AND completed_at IS NOT NULL
      AND content_sha256 IS NULL
      AND terminal_receipt_sha256 IS NOT NULL)
    OR
    (state IN ('pending', 'claimed', 'retryable')
      AND completed_at IS NULL
      AND content_sha256 IS NOT NULL
      AND terminal_receipt_sha256 IS NULL)
  ),
  CONSTRAINT memory_ingest_outbox_result_shape CHECK (
    (state = 'completed'
      AND eligibility_decision = 'send_external'
      AND successor_evidence_id IS NOT NULL
      AND successor_job_id IS NOT NULL)
    OR
    (state = 'skipped'
      AND eligibility_decision IN (
        'skip_zero_call', 'route_internal', 'block_local', 'review_context'
      )
      AND successor_evidence_id IS NULL
      AND successor_job_id IS NULL)
    OR
    (state NOT IN ('completed', 'skipped')
      AND successor_evidence_id IS NULL
      AND successor_job_id IS NULL)
  ),
  CONSTRAINT memory_ingest_outbox_error_size CHECK (
    last_error_code IS NULL
    OR (
      pg_catalog.octet_length(last_error_code) BETWEEN 1 AND 128
      AND last_error_code ~ '^[a-z][a-z0-9_]{0,127}$'
    )
  ),
  CONSTRAINT memory_ingest_outbox_error_semantics CHECK (
    (state IN ('pending', 'claimed', 'completed')
      AND last_error_code IS NULL)
    OR
    (state = 'skipped'
      AND (
        (eligibility_decision = 'review_context'
          AND last_error_code = 'context_review_unresolved')
        OR
        (eligibility_decision <> 'review_context'
          AND last_error_code IS NULL)
      ))
    OR
    (state = 'expired'
      AND last_error_code = 'content_hash_retention_expired')
    OR
    (state = 'retryable'
      AND last_error_code IN (
        'conversation_read_failed', 'lease_expired',
        'successor_write_failed', 'worker_transient_failure'
      ))
    OR
    (state = 'failed_terminal'
      AND last_error_code IN (
        'bridge_contract_violation', 'conversation_read_failed',
        'eligibility_contract_violation', 'lease_expired',
        'source_binding_mismatch', 'successor_receipt_mismatch',
        'successor_write_failed', 'worker_transient_failure'
      ))
    OR
    (state = 'erasure_cancelled'
      AND last_error_code = 'source_erasure_fenced')
  )
);

CREATE INDEX memory_ingest_outbox_lease_idx
  ON memory_ingest_private.memory_ingest_outbox(
    state, available_at, source_created_at, outbox_id
  ) WHERE state IN ('pending', 'retryable', 'claimed');
CREATE INDEX memory_ingest_outbox_hash_expiry_idx
  ON memory_ingest_private.memory_ingest_outbox(content_hash_expires_at)
  WHERE content_sha256 IS NOT NULL;

ALTER TABLE memory_ingest_private.memory_ingest_outbox OWNER TO sage;
REVOKE ALL ON TABLE memory_ingest_private.memory_ingest_outbox FROM PUBLIC;
REVOKE ALL ON TABLE memory_ingest_private.memory_ingest_outbox FROM memory_ingest_writer;
REVOKE ALL ON TABLE memory_ingest_private.memory_ingest_outbox FROM governed_memory_worker;
REVOKE ALL ON TABLE public.chat_log, public.threads, public.chat_attachments
  FROM memory_ingest_writer, governed_memory_worker;
ALTER TABLE memory_ingest_private.memory_ingest_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.memory_ingest_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_internal ON memory_ingest_private.memory_ingest_outbox
  TO sage USING (true) WITH CHECK (true);

CREATE FUNCTION memory_ingest_private.source_erasure_target_sha256(
  p_owner_user_id uuid,
  p_operation_id uuid,
  p_message_id uuid,
  p_thread_id uuid,
  p_source_created_at timestamptz
)
RETURNS text
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.source_erasure_target.v1' || E'\n'
      || memory_ingest_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'message_id', p_message_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'thread_id', p_thread_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'source_created_at',
           memory_ingest_private.timestamp_utc_text(p_source_created_at)
         ),
    'UTF8'
  )), 'hex')
$function$;

CREATE TABLE memory_ingest_private.source_erasure_operation (
  operation_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  selector_kind text NOT NULL,
  selector_thread_id uuid,
  selector_anchor_message_id uuid,
  selector_duration_seconds integer,
  selector_from_inclusive timestamptz,
  selector_through_inclusive timestamptz NOT NULL,
  selector_sha256 text NOT NULL,
  confirmation_sha256 text NOT NULL,
  target_count integer NOT NULL,
  target_manifest_sha256 text NOT NULL,
  thread_target_count integer NOT NULL,
  thread_target_manifest_sha256 text NOT NULL,
  state text NOT NULL,
  governed_receipt_sha256 text,
  attempt_count integer NOT NULL DEFAULT 0,
  lease_token uuid,
  leased_by text,
  lease_expires_at timestamptz,
  last_error_code text,
  created_at timestamptz NOT NULL,
  governed_completed_at timestamptz,
  completed_at timestamptz,
  CONSTRAINT source_erasure_operation_owner_id UNIQUE (
    owner_user_id, operation_id
  ),
  CONSTRAINT source_erasure_operation_owner_nonzero CHECK (
    owner_user_id <> '00000000-0000-0000-0000-000000000000'::uuid
  ),
  CONSTRAINT source_erasure_operation_selector CHECK (
    (selector_kind = 'thread'
      AND selector_thread_id IS NOT NULL
      AND selector_anchor_message_id IS NULL
      AND selector_duration_seconds IS NULL)
    OR
    (selector_kind = 'message_tail'
      AND selector_thread_id IS NOT NULL
      AND selector_anchor_message_id IS NOT NULL
      AND selector_duration_seconds IS NULL
      AND selector_from_inclusive IS NOT NULL)
    OR
    (selector_kind = 'recent'
      AND selector_thread_id IS NULL
      AND selector_anchor_message_id IS NULL
      AND selector_duration_seconds IN (3600,86400,604800,2592000)
      AND selector_from_inclusive IS NOT NULL)
    OR
    (selector_kind = 'all_conversations'
      AND selector_thread_id IS NULL
      AND selector_anchor_message_id IS NULL
      AND selector_duration_seconds IS NULL
      AND selector_from_inclusive IS NULL)
  ),
  CONSTRAINT source_erasure_operation_hashes CHECK (
    selector_sha256 ~ '^[0-9a-f]{64}$'
    AND confirmation_sha256 ~ '^[0-9a-f]{64}$'
    AND target_manifest_sha256 ~ '^[0-9a-f]{64}$'
    AND thread_target_manifest_sha256 ~ '^[0-9a-f]{64}$'
    AND (
      governed_receipt_sha256 IS NULL
      OR governed_receipt_sha256 ~ '^[0-9a-f]{64}$'
    )
  ),
  CONSTRAINT source_erasure_operation_state CHECK (
    state IN (
      'fenced', 'retryable', 'governed_deletion_pending',
      'governed_deleted', 'conversation_deleted_pending_ack',
      'completed', 'manual_review'
    )
  ),
  CONSTRAINT source_erasure_operation_counts CHECK (
    target_count BETWEEN 0 AND 100000
    AND thread_target_count BETWEEN 0 AND 100000
    AND attempt_count BETWEEN 0 AND 1000
  ),
  CONSTRAINT source_erasure_operation_lease CHECK (
    (lease_token IS NULL AND leased_by IS NULL AND lease_expires_at IS NULL)
    OR
    (state <> 'completed' AND lease_token IS NOT NULL
      AND leased_by IS NOT NULL AND lease_expires_at > created_at)
  ),
  CONSTRAINT source_erasure_operation_completion CHECK (
    (state IN ('conversation_deleted_pending_ack', 'completed')
      AND governed_receipt_sha256 IS NOT NULL
      AND governed_completed_at IS NOT NULL AND completed_at IS NOT NULL)
    OR
    (state NOT IN ('conversation_deleted_pending_ack', 'completed')
      AND completed_at IS NULL)
  )
);

CREATE UNIQUE INDEX source_erasure_one_active_owner_idx
  ON memory_ingest_private.source_erasure_operation(owner_user_id)
  WHERE state <> 'completed';

CREATE TABLE memory_ingest_private.source_erasure_target (
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  message_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  source_created_at timestamptz NOT NULL,
  target_sha256 text NOT NULL,
  PRIMARY KEY (owner_user_id, operation_id, message_id),
  CONSTRAINT source_erasure_target_operation_fk FOREIGN KEY (
    owner_user_id, operation_id
  ) REFERENCES memory_ingest_private.source_erasure_operation(
    owner_user_id, operation_id
  ) ON DELETE RESTRICT,
  CONSTRAINT source_erasure_target_hash CHECK (
    target_sha256 ~ '^[0-9a-f]{64}$'
  )
);

CREATE INDEX source_erasure_target_page_idx
  ON memory_ingest_private.source_erasure_target(
    owner_user_id, operation_id, source_created_at, message_id
  );

CREATE TABLE memory_ingest_private.source_erasure_thread_target (
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  source_created_at timestamptz NOT NULL,
  target_sha256 text NOT NULL,
  PRIMARY KEY (owner_user_id, operation_id, thread_id),
  CONSTRAINT source_erasure_thread_target_operation_fk FOREIGN KEY (
    owner_user_id, operation_id
  ) REFERENCES memory_ingest_private.source_erasure_operation(
    owner_user_id, operation_id
  ) ON DELETE RESTRICT,
  CONSTRAINT source_erasure_thread_target_hash CHECK (
    target_sha256 ~ '^[0-9a-f]{64}$'
  )
);

CREATE INDEX source_erasure_thread_target_page_idx
  ON memory_ingest_private.source_erasure_thread_target(
    owner_user_id, operation_id, source_created_at, thread_id
  );

CREATE TABLE memory_ingest_private.source_erasure_message_tombstone (
  message_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  erased_at timestamptz NOT NULL,
  CONSTRAINT source_erasure_message_tombstone_operation_fk FOREIGN KEY (
    owner_user_id, operation_id
  ) REFERENCES memory_ingest_private.source_erasure_operation(
    owner_user_id, operation_id
  ) ON DELETE RESTRICT
);

CREATE TABLE memory_ingest_private.source_erasure_thread_tombstone (
  thread_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  erased_at timestamptz NOT NULL,
  CONSTRAINT source_erasure_thread_tombstone_operation_fk FOREIGN KEY (
    owner_user_id, operation_id
  ) REFERENCES memory_ingest_private.source_erasure_operation(
    owner_user_id, operation_id
  ) ON DELETE RESTRICT
);

CREATE TABLE memory_ingest_private.source_erasure_receipt (
  receipt_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  selector_sha256 text NOT NULL,
  target_manifest_sha256 text NOT NULL,
  target_count integer NOT NULL,
  thread_target_manifest_sha256 text NOT NULL,
  thread_target_count integer NOT NULL,
  deleted_message_count integer NOT NULL,
  deleted_thread_count integer NOT NULL,
  deleted_attachment_count integer NOT NULL,
  deleted_bridge_row_count integer NOT NULL,
  message_tombstone_count integer NOT NULL,
  thread_tombstone_count integer NOT NULL,
  tombstone_manifest_sha256 text NOT NULL,
  governed_receipt_sha256 text NOT NULL,
  receipt_sha256 text NOT NULL,
  completed_at timestamptz NOT NULL,
  CONSTRAINT source_erasure_receipt_operation_unique UNIQUE (
    owner_user_id, operation_id
  ),
  CONSTRAINT source_erasure_receipt_hashes CHECK (
    selector_sha256 ~ '^[0-9a-f]{64}$'
    AND target_manifest_sha256 ~ '^[0-9a-f]{64}$'
    AND thread_target_manifest_sha256 ~ '^[0-9a-f]{64}$'
    AND tombstone_manifest_sha256 ~ '^[0-9a-f]{64}$'
    AND governed_receipt_sha256 ~ '^[0-9a-f]{64}$'
    AND receipt_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT source_erasure_receipt_counts CHECK (
    target_count BETWEEN 0 AND 100000
    AND thread_target_count BETWEEN 0 AND 100000
    AND deleted_message_count = target_count
    AND deleted_thread_count BETWEEN 0 AND 100000
    AND deleted_attachment_count BETWEEN 0 AND 1000000
    AND deleted_bridge_row_count BETWEEN 0 AND target_count
    AND message_tombstone_count = target_count
    AND thread_tombstone_count = deleted_thread_count
    AND thread_tombstone_count <= thread_target_count
  )
);

ALTER TABLE memory_ingest_private.source_erasure_operation OWNER TO sage;
ALTER TABLE memory_ingest_private.source_erasure_target OWNER TO sage;
ALTER TABLE memory_ingest_private.source_erasure_thread_target OWNER TO sage;
ALTER TABLE memory_ingest_private.source_erasure_message_tombstone
  OWNER TO sage;
ALTER TABLE memory_ingest_private.source_erasure_thread_tombstone
  OWNER TO sage;
ALTER TABLE memory_ingest_private.source_erasure_receipt OWNER TO sage;
REVOKE ALL ON TABLE
  memory_ingest_private.source_erasure_operation,
  memory_ingest_private.source_erasure_target,
  memory_ingest_private.source_erasure_thread_target,
  memory_ingest_private.source_erasure_message_tombstone,
  memory_ingest_private.source_erasure_thread_tombstone,
  memory_ingest_private.source_erasure_receipt
FROM PUBLIC, memory_ingest_writer, memory_erasure_requester,
  governed_memory_worker;
ALTER TABLE memory_ingest_private.source_erasure_operation
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_operation
  FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_target
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_target
  FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_thread_target
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_thread_target
  FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_message_tombstone
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_message_tombstone
  FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_thread_tombstone
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_thread_tombstone
  FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_receipt
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.source_erasure_receipt
  FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_internal
  ON memory_ingest_private.source_erasure_operation
  TO sage USING (true) WITH CHECK (true);
CREATE POLICY owner_internal
  ON memory_ingest_private.source_erasure_target
  TO sage USING (true) WITH CHECK (true);
CREATE POLICY owner_internal
  ON memory_ingest_private.source_erasure_thread_target
  TO sage USING (true) WITH CHECK (true);
CREATE POLICY owner_internal
  ON memory_ingest_private.source_erasure_message_tombstone
  TO sage USING (true) WITH CHECK (true);
CREATE POLICY owner_internal
  ON memory_ingest_private.source_erasure_thread_tombstone
  TO sage USING (true) WITH CHECK (true);
CREATE POLICY owner_internal
  ON memory_ingest_private.source_erasure_receipt
  TO sage USING (true) WITH CHECK (true);

CREATE FUNCTION memory_ingest_private.enqueue_chat_log_message(
  p_message_id uuid,
  p_policy_sha256 text
)
RETURNS TABLE(outcome text, outbox_id uuid)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  source_row record;
  current_xid xid;
  existing memory_ingest_private.memory_ingest_outbox%ROWTYPE;
  new_outbox_id uuid;
  captured_at timestamptz;
  content_hash text;
  window_hash text;
  source_binding text;
BEGIN
  IF session_user <> 'brains_app'
     OR NOT pg_catalog.pg_has_role(
       session_user, 'memory_ingest_writer', 'MEMBER'
     ) THEN
    RAISE EXCEPTION 'authorized brains_app capture membership required'
      USING ERRCODE = '42501';
  END IF;
  actor := NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid;
  IF actor IS NULL OR COALESCE(
       pg_catalog.current_setting('app.auth_context_sha256', true), ''
     ) !~ '^[0-9a-f]{64}$'
     OR p_message_id IS NULL
     OR p_policy_sha256 IS NULL
     OR p_policy_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid memory ingest enqueue input'
      USING ERRCODE = '22023';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  -- xmin is a 32-bit xid while pg_current_xact_id() is epoch-aware xid8.
  current_xid := (
    (pg_catalog.pg_current_xact_id()::text::numeric % 4294967296)::text
  )::xid;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|memory_ingest|pilot_limit', 0
    )
  );

  SELECT source.*, source.xmin AS source_xmin INTO source_row
  FROM public.chat_log AS source
  WHERE source.id = p_message_id
    AND source.owner_user_id = actor
  FOR KEY SHARE;
  IF NOT FOUND
     OR source_row.thread_id IS NULL
     OR source_row.source IS DISTINCT FROM 'frontend/chat:user'
     OR source_row.text IS NULL
     OR source_row.created_at IS NULL
     OR source_row.source_xmin <> current_xid
     OR source_row.created_at <> captured_at
     OR EXISTS (
       SELECT 1
       FROM public.chat_attachments AS attachment
       WHERE attachment.owner_user_id = actor
         AND attachment.thread_id = source_row.thread_id
         AND attachment.message_id = p_message_id
     )
     OR normalize(source_row.text, NFC) <> source_row.text THEN
    RAISE EXCEPTION 'message is not an NFC current-transaction chat row'
      USING ERRCODE = '22023';
  END IF;
  content_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(source_row.text, 'UTF8')
  ), 'hex');
  window_hash := memory_ingest_private.ingest_window_sha256(
    actor, source_row.thread_id, p_message_id, p_message_id,
    p_message_id, content_hash
  );
  source_binding := memory_ingest_private.source_binding_sha256(
    actor, p_message_id, source_row.thread_id, p_message_id, p_message_id,
    0, window_hash, content_hash, p_policy_sha256, source_row.created_at
  );

  SELECT value.* INTO existing
  FROM memory_ingest_private.memory_ingest_outbox AS value
  WHERE value.owner_user_id = actor AND value.message_id = p_message_id;
  IF FOUND THEN
    IF existing.thread_id <> source_row.thread_id
       OR existing.exchange_id <> p_message_id
       OR existing.window_id <> p_message_id
       OR existing.window_ordinal <> 0
       OR existing.window_sha256 <> window_hash
       OR existing.content_sha256 <> content_hash
       OR existing.source_created_at <> source_row.created_at
       OR existing.policy_sha256 <> p_policy_sha256
       OR existing.source_binding_sha256 IS DISTINCT FROM source_binding THEN
      RAISE EXCEPTION 'memory ingest enqueue replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text, existing.outbox_id;
    RETURN;
  END IF;
  IF (
    SELECT pg_catalog.count(*)
    FROM memory_ingest_private.memory_ingest_outbox AS value
    WHERE value.owner_user_id = actor
      AND value.source_created_at >= captured_at - interval '24 hours'
      AND value.source_created_at <= captured_at
  ) >= 20 THEN
    RETURN QUERY SELECT 'pilot_limit_reached'::text, NULL::uuid;
    RETURN;
  END IF;
  new_outbox_id := pg_catalog.gen_random_uuid();
  INSERT INTO memory_ingest_private.memory_ingest_outbox(
    outbox_id, owner_user_id, operation_id, message_id, thread_id,
    exchange_id, window_id, window_ordinal, window_sha256, content_sha256,
    source_binding_sha256,
    ingest_after, source_created_at, policy_sha256,
    content_hash_expires_at, purge_after, available_at, created_at, updated_at
  ) VALUES (
    new_outbox_id, actor, p_message_id, p_message_id, source_row.thread_id,
    p_message_id, p_message_id, 0, window_hash,
    content_hash, source_binding, captured_at,
    source_row.created_at, p_policy_sha256,
    captured_at + interval '24 hours', captured_at + interval '7 days',
    captured_at, captured_at, captured_at
  );
  RETURN QUERY SELECT 'enqueued'::text, new_outbox_id;
END;
$function$;

CREATE FUNCTION memory_ingest_private.lease_memory_ingest(
  p_worker_id text,
  p_limit integer,
  p_lease_seconds integer
)
RETURNS TABLE(
  outbox_id uuid,
  owner_user_id uuid,
  message_id uuid,
  thread_id uuid,
  exchange_id uuid,
  window_id uuid,
  window_ordinal integer,
  window_sha256 text,
  content_sha256 text,
  source_binding_sha256 text,
  policy_sha256 text,
  source_created_at timestamptz,
  ingest_after timestamptz,
  context_review_count integer,
  eligibility_decision text,
  lease_token uuid,
  lease_expires_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate record;
  new_lease_token uuid;
  new_lease_expires_at timestamptz;
  captured_at timestamptz;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF pg_catalog.octet_length(COALESCE(p_worker_id, '')) NOT BETWEEN 1 AND 128
     OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
     OR p_lease_seconds IS NULL
     OR p_lease_seconds NOT BETWEEN 5 AND 300 THEN
    RAISE EXCEPTION 'invalid bridge lease input' USING ERRCODE = '22023';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  UPDATE memory_ingest_private.memory_ingest_outbox AS expired
  SET state = CASE
        WHEN expired.attempt_count >= expired.max_attempts THEN 'failed_terminal'
        ELSE 'retryable'
      END,
      available_at = pg_catalog.clock_timestamp(),
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL,
      last_error_code = 'lease_expired',
      content_sha256 = CASE WHEN expired.attempt_count >= expired.max_attempts
        THEN NULL ELSE expired.content_sha256 END,
      completed_at = CASE WHEN expired.attempt_count >= expired.max_attempts
        THEN captured_at ELSE NULL END,
      terminal_receipt_sha256 = CASE
        WHEN expired.attempt_count >= expired.max_attempts
        THEN memory_ingest_private.terminal_receipt_sha256(
          expired.source_binding_sha256, 'failed_terminal',
          expired.eligibility_decision,
          expired.context_review_count, NULL::uuid, NULL::uuid,
          'lease_expired', captured_at
        ) ELSE NULL::text END,
      updated_at = pg_catalog.clock_timestamp()
  WHERE expired.state = 'claimed'
    AND expired.lease_expires_at <= pg_catalog.clock_timestamp();

  FOR candidate IN
    SELECT value.*
    FROM memory_ingest_private.memory_ingest_outbox AS value
    WHERE value.state IN ('pending', 'retryable')
      AND value.available_at <= pg_catalog.clock_timestamp()
      AND value.content_hash_expires_at > pg_catalog.clock_timestamp()
      AND value.attempt_count < value.max_attempts
      AND NOT EXISTS (
        SELECT 1
        FROM public.chat_attachments AS attachment
        WHERE attachment.owner_user_id = value.owner_user_id
          AND attachment.thread_id = value.thread_id
          AND attachment.message_id = value.message_id
      )
    ORDER BY value.available_at, value.source_created_at, value.outbox_id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  LOOP
    new_lease_token := pg_catalog.gen_random_uuid();
    new_lease_expires_at := pg_catalog.clock_timestamp()
      + pg_catalog.make_interval(secs => p_lease_seconds);
    UPDATE memory_ingest_private.memory_ingest_outbox
    SET state = 'claimed', attempt_count = attempt_count + 1,
        lease_token = new_lease_token, claimed_by = p_worker_id,
        claimed_at = pg_catalog.clock_timestamp(),
        lease_expires_at = new_lease_expires_at,
        last_error_code = NULL, updated_at = pg_catalog.clock_timestamp()
    WHERE memory_ingest_outbox.outbox_id = candidate.outbox_id;
    RETURN QUERY SELECT candidate.outbox_id, candidate.owner_user_id,
      candidate.message_id, candidate.thread_id, candidate.exchange_id,
      candidate.window_id, candidate.window_ordinal, candidate.window_sha256,
      candidate.content_sha256, candidate.source_binding_sha256,
      candidate.policy_sha256,
      candidate.source_created_at, candidate.ingest_after,
      candidate.context_review_count,
      candidate.eligibility_decision,
      new_lease_token, new_lease_expires_at;
  END LOOP;
END;
$function$;

CREATE FUNCTION memory_ingest_private.read_leased_chat_log_message(
  p_outbox_id uuid,
  p_lease_token uuid
)
RETURNS TABLE(
  outbox_id uuid,
  owner_user_id uuid,
  message_id uuid,
  thread_id uuid,
  exchange_id uuid,
  window_id uuid,
  window_ordinal integer,
  window_sha256 text,
  content_sha256 text,
  source_binding_sha256 text,
  policy_sha256 text,
  source_created_at timestamptz,
  ingest_after timestamptz,
  context_review_count integer,
  eligibility_decision text,
  lease_token uuid,
  lease_expires_at timestamptz,
  role text,
  content text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  target memory_ingest_private.memory_ingest_outbox%ROWTYPE;
  source_row public.chat_log%ROWTYPE;
  observed_content_sha256 text;
  observed_window_sha256 text;
  observed_source_binding_sha256 text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_outbox_id IS NULL OR p_lease_token IS NULL THEN
    RAISE EXCEPTION 'invalid bridge lease read input' USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT target
  FROM memory_ingest_private.memory_ingest_outbox AS value
  WHERE value.outbox_id = p_outbox_id
  FOR UPDATE;
  IF target.state <> 'claimed'
     OR target.lease_token <> p_lease_token
     OR target.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'stale bridge lease' USING ERRCODE = '40001';
  END IF;

  SELECT source.* INTO STRICT source_row
  FROM public.chat_log AS source
  WHERE source.id = target.message_id
    AND source.owner_user_id = target.owner_user_id
    AND source.thread_id = target.thread_id
  FOR KEY SHARE;
  IF source_row.text IS NULL
     OR source_row.source IS DISTINCT FROM 'frontend/chat:user'
     OR source_row.created_at IS NULL
     OR source_row.created_at <> target.source_created_at
     OR source_row.created_at < target.ingest_after
     OR EXISTS (
       SELECT 1
       FROM public.chat_attachments AS attachment
       WHERE attachment.owner_user_id = target.owner_user_id
         AND attachment.thread_id = target.thread_id
         AND attachment.message_id = target.message_id
     )
     OR normalize(source_row.text, NFC) <> source_row.text
     OR target.exchange_id <> target.message_id
     OR target.window_id <> target.message_id
     OR target.window_ordinal <> 0 THEN
    RAISE EXCEPTION 'leased chat source contract drifted'
      USING ERRCODE = '23514';
  END IF;
  observed_content_sha256 := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(source_row.text, 'UTF8')
  ), 'hex');
  observed_window_sha256 := memory_ingest_private.ingest_window_sha256(
    target.owner_user_id, target.thread_id, target.exchange_id,
    target.window_id, target.message_id, observed_content_sha256
  );
  observed_source_binding_sha256 :=
    memory_ingest_private.source_binding_sha256(
      target.owner_user_id, target.message_id, target.thread_id,
      target.exchange_id, target.window_id, target.window_ordinal,
      observed_window_sha256, observed_content_sha256,
      target.policy_sha256, target.source_created_at
    );
  IF target.content_sha256 IS DISTINCT FROM observed_content_sha256
     OR target.window_sha256 <> observed_window_sha256
     OR target.source_binding_sha256 <> observed_source_binding_sha256 THEN
    RAISE EXCEPTION 'leased chat source binding drifted'
      USING ERRCODE = '23514';
  END IF;

  RETURN QUERY SELECT
    target.outbox_id, target.owner_user_id, target.message_id,
    target.thread_id, target.exchange_id, target.window_id,
    target.window_ordinal, target.window_sha256, target.content_sha256,
    target.source_binding_sha256, target.policy_sha256,
    target.source_created_at, target.ingest_after,
    target.context_review_count, target.eligibility_decision,
    target.lease_token, target.lease_expires_at,
    'user'::text, source_row.text;
END;
$function$;

CREATE FUNCTION memory_ingest_private.mark_memory_ingest_context_review(
  p_outbox_id uuid,
  p_lease_token uuid
)
RETURNS TABLE(outcome text, context_review_count integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  target memory_ingest_private.memory_ingest_outbox%ROWTYPE;
  captured_at timestamptz;
  receipt_hash text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_outbox_id IS NULL OR p_lease_token IS NULL THEN
    RAISE EXCEPTION 'invalid context-review mark input' USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT target
  FROM memory_ingest_private.memory_ingest_outbox AS value
  WHERE value.outbox_id = p_outbox_id
  FOR UPDATE;
  IF target.state <> 'claimed' OR target.lease_token <> p_lease_token
     OR target.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'stale bridge lease' USING ERRCODE = '40001';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  IF target.context_review_count = 1 THEN
    receipt_hash := memory_ingest_private.terminal_receipt_sha256(
      target.source_binding_sha256, 'skipped', 'review_context', 1,
      NULL::uuid, NULL::uuid, 'context_review_unresolved', captured_at
    );
    UPDATE memory_ingest_private.memory_ingest_outbox
    SET state = 'skipped', eligibility_decision = 'review_context',
        content_sha256 = NULL,
        completed_at = captured_at,
        terminal_receipt_sha256 = receipt_hash,
        lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
        lease_expires_at = NULL,
        last_error_code = 'context_review_unresolved',
        updated_at = captured_at
    WHERE memory_ingest_outbox.outbox_id = target.outbox_id
      AND memory_ingest_outbox.state = 'claimed'
      AND memory_ingest_outbox.lease_token = p_lease_token
      AND memory_ingest_outbox.context_review_count = 1;
    RETURN QUERY SELECT 'terminal_unresolved'::text, 1;
    RETURN;
  ELSIF target.context_review_count <> 0 THEN
    RAISE EXCEPTION 'invalid context review count' USING ERRCODE = '23514';
  END IF;
  UPDATE memory_ingest_private.memory_ingest_outbox
  SET context_review_count = 1, eligibility_decision = 'review_context',
      updated_at = captured_at
  WHERE memory_ingest_outbox.outbox_id = target.outbox_id
    AND memory_ingest_outbox.state = 'claimed'
    AND memory_ingest_outbox.lease_token = p_lease_token
    AND memory_ingest_outbox.context_review_count = 0;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'context review CAS failed' USING ERRCODE = '40001';
  END IF;
  RETURN QUERY SELECT 'marked'::text, 1;
END;
$function$;

CREATE FUNCTION memory_ingest_private.ack_memory_ingest(
  p_outbox_id uuid,
  p_lease_token uuid,
  p_decision text,
  p_successor_evidence_id uuid,
  p_successor_job_id uuid
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  target memory_ingest_private.memory_ingest_outbox%ROWTYPE;
  captured_at timestamptz;
  receipt_hash text;
  resulting_state text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_outbox_id IS NULL OR p_lease_token IS NULL OR p_decision IS NULL
     OR p_decision = 'review_context'
     OR p_decision NOT IN (
       'send_external', 'skip_zero_call', 'route_internal', 'block_local'
     ) OR (
       p_decision = 'send_external'
       AND (p_successor_evidence_id IS NULL OR p_successor_job_id IS NULL)
     ) OR (
       p_decision <> 'send_external'
       AND (p_successor_evidence_id IS NOT NULL OR p_successor_job_id IS NOT NULL)
     ) THEN
    RAISE EXCEPTION 'invalid bridge acknowledgment' USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT target
  FROM memory_ingest_private.memory_ingest_outbox AS value
  WHERE value.outbox_id = p_outbox_id
  FOR UPDATE;
  resulting_state := CASE WHEN p_decision = 'send_external'
    THEN 'completed' ELSE 'skipped' END;
  IF target.state IN ('completed', 'skipped') THEN
    receipt_hash := memory_ingest_private.terminal_receipt_sha256(
      target.source_binding_sha256, target.state,
      target.eligibility_decision, target.context_review_count,
      target.successor_evidence_id, target.successor_job_id,
      target.last_error_code, target.completed_at
    );
    IF target.state <> resulting_state
       OR target.eligibility_decision <> p_decision
       OR target.successor_evidence_id
            IS DISTINCT FROM p_successor_evidence_id
       OR target.successor_job_id IS DISTINCT FROM p_successor_job_id
       OR target.last_error_code IS NOT NULL
       OR target.terminal_receipt_sha256 <> receipt_hash THEN
      RAISE EXCEPTION 'bridge acknowledgment replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN 'replayed';
  END IF;
  IF target.state <> 'claimed' OR target.lease_token <> p_lease_token
     OR target.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'stale bridge lease' USING ERRCODE = '40001';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  receipt_hash := memory_ingest_private.terminal_receipt_sha256(
    target.source_binding_sha256, resulting_state, p_decision,
    target.context_review_count, p_successor_evidence_id,
    p_successor_job_id, NULL::text, captured_at
  );
  UPDATE memory_ingest_private.memory_ingest_outbox
  SET state = resulting_state,
      eligibility_decision = p_decision,
      successor_evidence_id = p_successor_evidence_id,
      successor_job_id = p_successor_job_id,
      content_sha256 = NULL, completed_at = captured_at,
      terminal_receipt_sha256 = receipt_hash,
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL, updated_at = captured_at
  WHERE memory_ingest_outbox.outbox_id = target.outbox_id;
  RETURN resulting_state;
END;
$function$;

CREATE FUNCTION memory_ingest_private.fail_memory_ingest(
  p_outbox_id uuid,
  p_lease_token uuid,
  p_failure_mode text,
  p_error_code text,
  p_retry_after_seconds integer
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  target memory_ingest_private.memory_ingest_outbox%ROWTYPE;
  resulting_state text;
  captured_at timestamptz;
  receipt_hash text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_outbox_id IS NULL OR p_lease_token IS NULL
     OR p_failure_mode IS NULL
     OR p_failure_mode NOT IN ('retryable', 'failed_terminal')
     OR p_error_code IS NULL
     OR pg_catalog.octet_length(COALESCE(p_error_code, '')) NOT BETWEEN 1 AND 128
     OR p_retry_after_seconds IS NULL
     OR p_retry_after_seconds NOT BETWEEN 0 AND 3600
     OR (p_failure_mode = 'failed_terminal' AND p_retry_after_seconds <> 0)
     OR (
       p_failure_mode = 'retryable'
       AND p_error_code NOT IN (
         'conversation_read_failed', 'successor_write_failed',
         'worker_transient_failure'
       )
     )
     OR (
       p_failure_mode = 'failed_terminal'
       AND p_error_code NOT IN (
         'bridge_contract_violation', 'eligibility_contract_violation',
         'source_binding_mismatch', 'successor_receipt_mismatch'
       )
     ) THEN
    RAISE EXCEPTION 'invalid bridge failure input' USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT target
  FROM memory_ingest_private.memory_ingest_outbox AS value
  WHERE value.outbox_id = p_outbox_id
  FOR UPDATE;
  IF target.state = 'failed_terminal' THEN
    receipt_hash := memory_ingest_private.terminal_receipt_sha256(
      target.source_binding_sha256, target.state,
      target.eligibility_decision, target.context_review_count,
      NULL::uuid, NULL::uuid, target.last_error_code, target.completed_at
    );
    IF NOT (
         (
           p_failure_mode = 'failed_terminal'
           AND p_error_code IN (
             'bridge_contract_violation', 'eligibility_contract_violation',
             'source_binding_mismatch', 'successor_receipt_mismatch'
           )
         )
         OR
         (
           p_failure_mode = 'retryable'
           AND target.attempt_count >= target.max_attempts
           AND p_retry_after_seconds = 0
           AND p_error_code IN (
             'conversation_read_failed', 'successor_write_failed',
             'worker_transient_failure'
           )
         )
       )
       OR target.last_error_code <> p_error_code
       OR target.terminal_receipt_sha256 <> receipt_hash THEN
      RAISE EXCEPTION 'bridge failure replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN 'replayed';
  END IF;
  IF target.state <> 'claimed' OR target.lease_token <> p_lease_token
     OR target.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'stale bridge lease' USING ERRCODE = '40001';
  END IF;
  IF p_failure_mode = 'retryable'
     AND target.attempt_count >= target.max_attempts
     AND p_retry_after_seconds <> 0 THEN
    RAISE EXCEPTION 'exhausted retry must use zero retry delay'
      USING ERRCODE = '22023';
  END IF;
  resulting_state := CASE
    WHEN p_failure_mode = 'retryable'
         AND target.attempt_count < target.max_attempts THEN 'retryable'
    ELSE 'failed_terminal' END;
  captured_at := pg_catalog.transaction_timestamp();
  IF resulting_state = 'failed_terminal' THEN
    receipt_hash := memory_ingest_private.terminal_receipt_sha256(
      target.source_binding_sha256, 'failed_terminal',
      target.eligibility_decision, target.context_review_count,
      NULL::uuid, NULL::uuid, p_error_code, captured_at
    );
  END IF;
  UPDATE memory_ingest_private.memory_ingest_outbox
  SET state = resulting_state,
      available_at = CASE WHEN resulting_state = 'retryable'
        THEN pg_catalog.clock_timestamp()
          + pg_catalog.make_interval(secs => p_retry_after_seconds)
        ELSE available_at END,
      content_sha256 = CASE WHEN resulting_state = 'failed_terminal'
        THEN NULL ELSE content_sha256 END,
      completed_at = CASE WHEN resulting_state = 'failed_terminal'
        THEN captured_at ELSE NULL END,
      terminal_receipt_sha256 = receipt_hash,
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL, last_error_code = p_error_code,
      updated_at = captured_at
  WHERE memory_ingest_outbox.outbox_id = target.outbox_id;
  RETURN resulting_state;
END;
$function$;

CREATE FUNCTION memory_ingest_private.expire_memory_ingest(p_limit integer)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  expired_count integer;
  captured_at timestamptz;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'invalid bridge expiry limit' USING ERRCODE = '22023';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  WITH candidates AS (
    SELECT value.outbox_id
    FROM memory_ingest_private.memory_ingest_outbox AS value
    WHERE value.state IN ('pending', 'retryable')
      AND value.content_hash_expires_at <= pg_catalog.clock_timestamp()
    ORDER BY value.content_hash_expires_at, value.outbox_id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  ), expired AS (
    UPDATE memory_ingest_private.memory_ingest_outbox AS value
    SET state = 'expired', content_sha256 = NULL,
        completed_at = captured_at,
        terminal_receipt_sha256 =
          memory_ingest_private.terminal_receipt_sha256(
            value.source_binding_sha256, 'expired',
            value.eligibility_decision, value.context_review_count,
            NULL::uuid, NULL::uuid,
            'content_hash_retention_expired', captured_at
          ),
        last_error_code = 'content_hash_retention_expired',
        updated_at = pg_catalog.clock_timestamp()
    FROM candidates
    WHERE value.outbox_id = candidates.outbox_id
    RETURNING 1
  )
  SELECT pg_catalog.count(*)::integer INTO expired_count FROM expired;
  RETURN expired_count;
END;
$function$;

CREATE FUNCTION memory_ingest_private.purge_terminal_memory_ingest(
  p_limit integer
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  purged_count integer;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'invalid bridge purge limit' USING ERRCODE = '22023';
  END IF;
  LOCK TABLE public.threads IN ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_log IN ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_attachments IN ROW EXCLUSIVE MODE;
  LOCK TABLE chat_integrity.assistant_transcript_attestation_v1
    IN ROW EXCLUSIVE MODE;
  IF pg_catalog.to_regclass('public.active_thread_selection') IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE public.active_thread_selection IN ROW EXCLUSIVE MODE';
  END IF;
  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE trusted_web.response_transcript_v1 IN ROW EXCLUSIVE MODE';
  END IF;
  LOCK TABLE memory_ingest_private.memory_ingest_outbox
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_operation
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_target
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_thread_target
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_message_tombstone
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_thread_tombstone
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_receipt
    IN ROW EXCLUSIVE MODE;
  PERFORM memory_ingest_private.assert_chat_deletion_catalog();
  WITH candidates AS (
    SELECT value.outbox_id
    FROM memory_ingest_private.memory_ingest_outbox AS value
    WHERE value.state IN (
      'completed', 'skipped', 'expired', 'failed_terminal'
    )
      AND value.purge_after <= pg_catalog.clock_timestamp()
    ORDER BY value.purge_after, value.outbox_id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  ), purged AS (
    DELETE FROM memory_ingest_private.memory_ingest_outbox AS value
    USING candidates
    WHERE value.outbox_id = candidates.outbox_id
    RETURNING 1
  )
  SELECT pg_catalog.count(*)::integer INTO purged_count FROM purged;
  RETURN purged_count;
END;
$function$;

CREATE FUNCTION memory_ingest_private.begin_source_erasure(
  p_operation_id uuid,
  p_selector_kind text,
  p_thread_id uuid,
  p_anchor_message_id uuid,
  p_recent_seconds integer,
  p_confirmation_sha256 text
)
RETURNS TABLE(
  outcome text,
  operation_id uuid,
  state text,
  target_count integer,
  selector_sha256 text,
  target_manifest_sha256 text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  requested_at timestamptz;
  from_inclusive timestamptz;
  anchor_created_at timestamptz;
  selector_hash text;
  target_manifest text;
  thread_target_manifest text;
  observed_attachment_count bigint;
  observed_count integer;
  observed_thread_count integer;
  existing memory_ingest_private.source_erasure_operation%ROWTYPE;
BEGIN
  IF session_user <> 'governed_memory_api'
     OR NOT pg_catalog.pg_has_role(
       session_user, 'memory_erasure_requester', 'MEMBER'
     ) THEN
    RAISE EXCEPTION 'authorized erasure requester membership required'
      USING ERRCODE = '42501';
  END IF;
  actor := NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid;
  IF actor IS NULL OR p_operation_id IS NULL
     OR COALESCE(
       pg_catalog.current_setting('app.auth_context_sha256', true), ''
     ) !~ '^[0-9a-f]{64}$'
     OR COALESCE(p_confirmation_sha256, '') !~ '^[0-9a-f]{64}$'
     OR p_confirmation_sha256 IS DISTINCT FROM
          memory_ingest_private.deletion_confirmation_sha256(
            p_operation_id, p_selector_kind, p_thread_id,
            p_anchor_message_id, p_recent_seconds
          )
     OR COALESCE(p_selector_kind, '') NOT IN (
       'thread', 'message_tail', 'recent', 'all_conversations'
     )
     OR (p_selector_kind = 'thread' AND (
       p_thread_id IS NULL OR p_anchor_message_id IS NOT NULL
       OR p_recent_seconds IS NOT NULL
     ))
     OR (p_selector_kind = 'message_tail' AND (
       p_thread_id IS NULL OR p_anchor_message_id IS NULL
       OR p_recent_seconds IS NOT NULL
     ))
     OR (p_selector_kind = 'recent' AND (
       p_thread_id IS NOT NULL OR p_anchor_message_id IS NOT NULL
       OR p_recent_seconds NOT IN (3600,86400,604800,2592000)
     ))
     OR (p_selector_kind = 'all_conversations' AND (
       p_thread_id IS NOT NULL OR p_anchor_message_id IS NOT NULL
       OR p_recent_seconds IS NOT NULL
     )) THEN
    RAISE EXCEPTION 'invalid source erasure request'
      USING ERRCODE = '22023';
  END IF;

  LOCK TABLE public.threads IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_log IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_attachments IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE chat_integrity.assistant_transcript_attestation_v1
    IN SHARE ROW EXCLUSIVE MODE;
  IF pg_catalog.to_regclass('public.active_thread_selection') IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE public.active_thread_selection '
      'IN SHARE ROW EXCLUSIVE MODE';
  END IF;
  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE trusted_web.response_transcript_v1 '
      'IN SHARE ROW EXCLUSIVE MODE';
  END IF;
  LOCK TABLE memory_ingest_private.memory_ingest_outbox
    IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_operation
    IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_target
    IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_thread_target
    IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_message_tombstone
    IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_thread_tombstone
    IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_receipt
    IN SHARE ROW EXCLUSIVE MODE;
  PERFORM memory_ingest_private.assert_chat_deletion_catalog();
  requested_at := pg_catalog.transaction_timestamp();
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(actor::text || '|chat_source_erasure', 0)
  );
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|chat_source_erasure|' || p_operation_id::text, 0
    )
  );

  SELECT value.* INTO existing
  FROM memory_ingest_private.source_erasure_operation AS value
  WHERE value.owner_user_id = actor
    AND value.operation_id = p_operation_id;
  IF FOUND THEN
    IF existing.selector_kind IS DISTINCT FROM p_selector_kind
       OR existing.selector_thread_id IS DISTINCT FROM p_thread_id
       OR existing.selector_anchor_message_id
            IS DISTINCT FROM p_anchor_message_id
       OR existing.selector_duration_seconds IS DISTINCT FROM p_recent_seconds
       OR existing.confirmation_sha256 IS DISTINCT FROM p_confirmation_sha256
    THEN
      RAISE EXCEPTION 'source erasure operation replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text, existing.operation_id,
      existing.state, existing.target_count, existing.selector_sha256,
      existing.target_manifest_sha256;
    RETURN;
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_operation AS active
    WHERE active.owner_user_id = actor
      AND active.state <> 'completed'
  ) THEN
    RAISE EXCEPTION 'owner source erasure already active'
      USING ERRCODE = '55000';
  END IF;

  IF p_selector_kind IN ('thread', 'message_tail') THEN
    PERFORM 1
    FROM public.threads AS thread
    WHERE thread.owner_user_id = actor
      AND thread.id = p_thread_id
      AND thread.created_at IS NOT NULL
      AND thread.created_at <= requested_at
    FOR KEY SHARE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'source erasure thread is absent or invalid'
        USING ERRCODE = 'P0002';
    END IF;
  END IF;
  IF p_selector_kind = 'thread' THEN
    IF EXISTS (
      SELECT 1
      FROM public.chat_log AS source
      WHERE source.owner_user_id = actor
        AND source.thread_id = p_thread_id
        AND (
          source.created_at IS NULL OR source.created_at > requested_at
        )
    ) THEN
      RAISE EXCEPTION 'source erasure thread has invalid chat time or lineage'
        USING ERRCODE = '23514';
    END IF;
  ELSIF p_selector_kind = 'all_conversations' AND EXISTS (
    SELECT 1
    FROM public.chat_log AS source
    WHERE source.owner_user_id = actor
      AND (
        source.thread_id IS NULL OR source.created_at IS NULL
        OR source.created_at > requested_at
      )
  ) THEN
    RAISE EXCEPTION 'all-conversation erasure has invalid chat time or lineage'
      USING ERRCODE = '23514';
  ELSIF p_selector_kind = 'all_conversations' AND EXISTS (
    SELECT 1
    FROM public.threads AS thread
    WHERE thread.owner_user_id = actor
      AND (
        thread.created_at IS NULL OR thread.created_at > requested_at
      )
  ) THEN
    RAISE EXCEPTION 'all-conversation erasure has invalid thread time'
      USING ERRCODE = '23514';
  ELSIF p_selector_kind = 'all_conversations' AND (
    SELECT pg_catalog.count(*)
    FROM public.threads AS thread
    WHERE thread.owner_user_id = actor
      AND thread.created_at <= requested_at
  ) > 100000 THEN
    RAISE EXCEPTION 'source erasure thread target limit exceeded'
      USING ERRCODE = '54000';
  END IF;

  IF p_selector_kind = 'message_tail' THEN
    SELECT source.created_at INTO anchor_created_at
    FROM public.chat_log AS source
    WHERE source.owner_user_id = actor
      AND source.thread_id = p_thread_id
      AND source.id = p_anchor_message_id
    FOR KEY SHARE;
    IF NOT FOUND OR anchor_created_at IS NULL THEN
      RAISE EXCEPTION 'source erasure anchor is absent'
        USING ERRCODE = 'P0002';
    END IF;
    IF EXISTS (
      SELECT 1
      FROM public.chat_log AS source
      WHERE source.owner_user_id = actor
        AND source.thread_id = p_thread_id
        AND source.created_at IS NULL
    ) THEN
      RAISE EXCEPTION 'message-tail erasure has invalid chat time'
        USING ERRCODE = '23514';
    END IF;
    from_inclusive := anchor_created_at;
  ELSIF p_selector_kind = 'recent' THEN
    IF EXISTS (
      SELECT 1
      FROM public.chat_log AS source
      WHERE source.owner_user_id = actor
        AND source.created_at IS NULL
    ) THEN
      RAISE EXCEPTION 'recent erasure has invalid chat time'
        USING ERRCODE = '23514';
    END IF;
    from_inclusive := requested_at
      - pg_catalog.make_interval(secs => p_recent_seconds);
  END IF;
  IF (
    p_selector_kind = 'message_tail' AND EXISTS (
      SELECT 1
      FROM public.chat_log AS source
      WHERE source.owner_user_id = actor
        AND source.thread_id = p_thread_id
        AND source.created_at > requested_at
        AND (source.created_at, source.id) >= (
          anchor_created_at, p_anchor_message_id
        )
    )
  ) OR (
    p_selector_kind = 'recent' AND EXISTS (
      SELECT 1
      FROM public.chat_log AS source
      WHERE source.owner_user_id = actor
        AND source.created_at > requested_at
    )
  ) THEN
    RAISE EXCEPTION 'source erasure selector has future-dated chat rows'
      USING ERRCODE = '23514';
  END IF;

  selector_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      'governed_memory.source_erasure_selector.v1' || E'\n'
        || memory_ingest_private.framed_utf8_field(
             'owner_user_id', actor::text
           )
        || memory_ingest_private.framed_utf8_field(
             'operation_id', p_operation_id::text
           )
        || memory_ingest_private.framed_utf8_field(
             'selector_kind', p_selector_kind
           )
        || memory_ingest_private.framed_utf8_field(
             'thread_id', p_thread_id::text
           )
        || memory_ingest_private.framed_utf8_field(
             'anchor_message_id', p_anchor_message_id::text
           )
        || memory_ingest_private.framed_utf8_field(
             'recent_seconds', p_recent_seconds::text
           )
        || memory_ingest_private.framed_utf8_field(
             'from_inclusive',
             memory_ingest_private.timestamp_utc_text(from_inclusive)
           )
        || memory_ingest_private.framed_utf8_field(
             'through_inclusive',
             memory_ingest_private.timestamp_utc_text(requested_at)
           ),
      'UTF8'
    )
  ), 'hex');

  INSERT INTO memory_ingest_private.source_erasure_operation(
    operation_id, owner_user_id, selector_kind, selector_thread_id,
    selector_anchor_message_id, selector_duration_seconds,
    selector_from_inclusive, selector_through_inclusive,
    selector_sha256, confirmation_sha256, target_count,
    target_manifest_sha256, thread_target_count,
    thread_target_manifest_sha256, state, created_at
  ) VALUES (
    p_operation_id, actor, p_selector_kind, p_thread_id,
    p_anchor_message_id, p_recent_seconds, from_inclusive, requested_at,
    selector_hash, p_confirmation_sha256, 0, pg_catalog.repeat('0', 64),
    0, pg_catalog.repeat('0', 64), 'fenced', requested_at
  );

  INSERT INTO memory_ingest_private.source_erasure_target(
    owner_user_id, operation_id, message_id, thread_id,
    source_created_at, target_sha256
  )
  SELECT actor, p_operation_id, source.id, source.thread_id,
    source.created_at,
    memory_ingest_private.source_erasure_target_sha256(
      actor, p_operation_id, source.id, source.thread_id, source.created_at
    )
  FROM public.chat_log AS source
  WHERE source.owner_user_id = actor
    AND source.created_at IS NOT NULL
    AND source.created_at <= requested_at
    AND (
      (p_selector_kind = 'thread' AND source.thread_id = p_thread_id)
      OR
      (p_selector_kind = 'message_tail'
        AND source.thread_id = p_thread_id
        AND (source.created_at, source.id) >= (
          anchor_created_at, p_anchor_message_id
        ))
      OR
      (p_selector_kind = 'recent' AND source.created_at >= from_inclusive)
      OR p_selector_kind = 'all_conversations'
    )
  ORDER BY source.created_at, source.id;

  INSERT INTO memory_ingest_private.source_erasure_thread_target(
    owner_user_id, operation_id, thread_id, source_created_at, target_sha256
  )
  SELECT actor, p_operation_id, thread.id, thread.created_at,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.source_erasure_thread_target.v1' || E'\n'
        || memory_ingest_private.framed_utf8_field(
             'owner_user_id', actor::text
           )
        || memory_ingest_private.framed_utf8_field(
             'operation_id', p_operation_id::text
           )
        || memory_ingest_private.framed_utf8_field(
             'thread_id', thread.id::text
           )
        || memory_ingest_private.framed_utf8_field(
             'source_created_at',
             memory_ingest_private.timestamp_utc_text(thread.created_at)
           ),
      'UTF8'
    )), 'hex')
  FROM public.threads AS thread
  WHERE thread.owner_user_id = actor
    AND thread.created_at IS NOT NULL
    AND thread.created_at <= requested_at
    AND (
      (p_selector_kind IN ('thread', 'message_tail')
        AND thread.id = p_thread_id)
      OR p_selector_kind = 'all_conversations'
      OR (p_selector_kind = 'recent' AND EXISTS (
        SELECT 1
        FROM memory_ingest_private.source_erasure_target AS target
        WHERE target.owner_user_id = actor
          AND target.operation_id = p_operation_id
          AND target.thread_id = thread.id
      ))
    )
  ORDER BY thread.created_at, thread.id;

  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    WHERE target.owner_user_id = actor
      AND target.operation_id = p_operation_id
      AND NOT EXISTS (
        SELECT 1
        FROM memory_ingest_private.source_erasure_thread_target
          AS thread_target
        WHERE thread_target.owner_user_id = target.owner_user_id
          AND thread_target.operation_id = target.operation_id
          AND thread_target.thread_id = target.thread_id
      )
  ) THEN
    RAISE EXCEPTION 'source erasure message/thread lineage differs'
      USING ERRCODE = '23514';
  END IF;

  SELECT pg_catalog.count(*)::integer,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.source_erasure_thread_target_manifest.v1' || E'\n'
        || COALESCE(pg_catalog.string_agg(
          target.target_sha256, E'\n'
          ORDER BY target.source_created_at, target.thread_id
        ), ''),
      'UTF8'
    )), 'hex')
  INTO observed_thread_count, thread_target_manifest
  FROM memory_ingest_private.source_erasure_thread_target AS target
  WHERE target.owner_user_id = actor
    AND target.operation_id = p_operation_id;
  IF observed_thread_count > 100000
     OR (
       p_selector_kind IN ('thread', 'message_tail')
       AND observed_thread_count <> 1
     ) THEN
    RAISE EXCEPTION 'source erasure thread target inventory differs'
      USING ERRCODE = '54000';
  END IF;

  IF (
    p_selector_kind IN ('thread', 'message_tail') AND EXISTS (
      SELECT 1
      FROM public.chat_log AS residual
      WHERE residual.thread_id = p_thread_id
        AND residual.owner_user_id IS DISTINCT FROM actor
    )
  ) OR (
    p_selector_kind IN ('message_tail', 'recent') AND EXISTS (
      SELECT 1
      FROM memory_ingest_private.source_erasure_target AS target_thread
      JOIN public.chat_log AS residual
        ON residual.thread_id = target_thread.thread_id
      WHERE target_thread.owner_user_id = actor
        AND target_thread.operation_id = p_operation_id
        AND residual.owner_user_id IS DISTINCT FROM actor
    )
  ) OR (
    p_selector_kind = 'all_conversations' AND EXISTS (
      SELECT 1
      FROM public.threads AS thread
      JOIN public.chat_log AS residual
        ON residual.thread_id = thread.id
      WHERE thread.owner_user_id = actor
        AND thread.created_at <= requested_at
        AND residual.owner_user_id IS DISTINCT FROM actor
    )
  ) THEN
    RAISE EXCEPTION 'source erasure candidate thread has mixed owner lineage'
      USING ERRCODE = '23514';
  END IF;

  SELECT pg_catalog.count(*)::integer,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.source_erasure_target_manifest.v1' || E'\n'
        || COALESCE(pg_catalog.string_agg(
          target.target_sha256, E'\n'
          ORDER BY target.source_created_at, target.message_id
        ), ''),
      'UTF8'
    )), 'hex')
  INTO observed_count, target_manifest
  FROM memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = actor
    AND target.operation_id = p_operation_id;
  IF observed_count > 100000 THEN
    RAISE EXCEPTION 'source erasure target limit exceeded'
      USING ERRCODE = '54000';
  END IF;

  IF p_selector_kind = 'thread' THEN
    SELECT pg_catalog.count(*)
    INTO observed_attachment_count
    FROM public.chat_attachments AS attachment
    WHERE attachment.owner_user_id = actor
      AND attachment.thread_id = p_thread_id;
  ELSIF p_selector_kind = 'all_conversations' THEN
    SELECT pg_catalog.count(*)
    INTO observed_attachment_count
    FROM public.chat_attachments AS attachment
    JOIN public.threads AS thread
      ON thread.id = attachment.thread_id
     AND thread.owner_user_id = attachment.owner_user_id
    WHERE thread.owner_user_id = actor
      AND thread.created_at <= requested_at;
  ELSE
    SELECT pg_catalog.count(*)
    INTO observed_attachment_count
    FROM public.chat_attachments AS attachment
    WHERE attachment.owner_user_id = actor
      AND (
        EXISTS (
          SELECT 1
          FROM memory_ingest_private.source_erasure_target AS target
          WHERE target.owner_user_id = actor
            AND target.operation_id = p_operation_id
            AND target.thread_id = attachment.thread_id
            AND target.message_id = attachment.message_id
        )
        OR EXISTS (
          SELECT 1
          FROM memory_ingest_private.source_erasure_target AS target_thread
          WHERE target_thread.owner_user_id = actor
            AND target_thread.operation_id = p_operation_id
            AND target_thread.thread_id = attachment.thread_id
            AND NOT EXISTS (
              SELECT 1
              FROM public.chat_log AS remaining
              WHERE remaining.owner_user_id = actor
                AND remaining.thread_id = target_thread.thread_id
                AND NOT EXISTS (
                  SELECT 1
                  FROM memory_ingest_private.source_erasure_target
                    AS remaining_target
                  WHERE remaining_target.owner_user_id = actor
                    AND remaining_target.operation_id = p_operation_id
                    AND remaining_target.thread_id = remaining.thread_id
                    AND remaining_target.message_id = remaining.id
                )
            )
        )
      );
  END IF;
  IF observed_attachment_count > 1000000 THEN
    RAISE EXCEPTION 'source erasure attachment target limit exceeded'
      USING ERRCODE = '54000';
  END IF;

  UPDATE memory_ingest_private.source_erasure_operation AS operation
  SET target_count = observed_count,
      target_manifest_sha256 = target_manifest,
      thread_target_count = observed_thread_count,
      thread_target_manifest_sha256 = thread_target_manifest
  WHERE operation.owner_user_id = actor
    AND operation.operation_id = p_operation_id;

  UPDATE memory_ingest_private.memory_ingest_outbox AS bridge
  SET state = 'erasure_cancelled', content_sha256 = NULL,
      completed_at = requested_at,
      terminal_receipt_sha256 =
        memory_ingest_private.terminal_receipt_sha256(
          bridge.source_binding_sha256, 'erasure_cancelled',
          bridge.eligibility_decision, bridge.context_review_count,
          NULL::uuid, NULL::uuid, 'source_erasure_fenced', requested_at
        ),
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL,
      last_error_code = 'source_erasure_fenced', updated_at = requested_at
  FROM memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = actor
    AND target.operation_id = p_operation_id
    AND bridge.owner_user_id = target.owner_user_id
    AND bridge.message_id = target.message_id
    AND bridge.state IN ('pending', 'claimed', 'retryable');

  RETURN QUERY SELECT 'fenced'::text, p_operation_id, 'fenced'::text,
    observed_count, selector_hash, target_manifest;
END;
$function$;

CREATE FUNCTION memory_ingest_private.read_source_erasure(
  p_operation_id uuid
)
RETURNS TABLE(
  operation_id uuid,
  selector_kind text,
  state text,
  target_count integer,
  selector_sha256 text,
  target_manifest_sha256 text,
  governed_receipt_sha256 text,
  last_error_code text,
  created_at timestamptz,
  completed_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'governed_memory_api'
     OR NOT pg_catalog.pg_has_role(
       session_user, 'memory_erasure_requester', 'MEMBER'
     ) THEN
    RAISE EXCEPTION 'authorized erasure requester membership required'
      USING ERRCODE = '42501';
  END IF;
  actor := NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid;
  IF actor IS NULL OR p_operation_id IS NULL
     OR COALESCE(
       pg_catalog.current_setting('app.auth_context_sha256', true), ''
     ) !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid source erasure status request'
      USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
  SELECT value.operation_id, value.selector_kind, value.state,
    value.target_count, value.selector_sha256,
    value.target_manifest_sha256, value.governed_receipt_sha256,
    value.last_error_code, value.created_at, value.completed_at
  FROM memory_ingest_private.source_erasure_operation AS value
  WHERE value.owner_user_id = actor
    AND value.operation_id = p_operation_id;
END;
$function$;

CREATE FUNCTION memory_ingest_private.lease_source_erasure(
  p_worker_id text,
  p_lease_seconds integer
)
RETURNS TABLE(
  operation_id uuid,
  owner_user_id uuid,
  selector_kind text,
  selector_sha256 text,
  target_count integer,
  target_manifest_sha256 text,
  state text,
  governed_receipt_sha256 text,
  lease_token uuid
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  target memory_ingest_private.source_erasure_operation%ROWTYPE;
  new_lease_token uuid;
BEGIN
  IF session_user <> 'governed_memory_worker'
     OR pg_catalog.octet_length(COALESCE(p_worker_id, '')) NOT BETWEEN 1 AND 128
     OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 5 AND 300 THEN
    RAISE EXCEPTION 'invalid source erasure lease request'
      USING ERRCODE = '22023';
  END IF;
  UPDATE memory_ingest_private.source_erasure_operation AS expired
  SET lease_token = NULL, leased_by = NULL, lease_expires_at = NULL,
      state = CASE WHEN expired.state IN (
          'governed_deleted', 'conversation_deleted_pending_ack'
        ) THEN expired.state ELSE 'retryable' END,
      last_error_code = CASE WHEN expired.state IN (
          'governed_deleted', 'conversation_deleted_pending_ack'
        ) THEN expired.last_error_code ELSE 'coordinator_lease_expired' END
  WHERE expired.lease_token IS NOT NULL
    AND expired.lease_expires_at <= pg_catalog.clock_timestamp();
  UPDATE memory_ingest_private.source_erasure_operation AS exhausted
  SET state = 'manual_review',
      last_error_code = 'coordinator_attempts_exhausted'
  WHERE exhausted.lease_token IS NULL
    AND exhausted.attempt_count >= 1000
    AND exhausted.state IN (
      'fenced', 'retryable', 'governed_deletion_pending', 'governed_deleted'
    );
  SELECT value.* INTO target
  FROM memory_ingest_private.source_erasure_operation AS value
    WHERE value.state IN (
      'fenced', 'retryable', 'governed_deletion_pending', 'governed_deleted',
      'conversation_deleted_pending_ack'
    )
    AND value.lease_token IS NULL
    AND (
      value.attempt_count < 1000
      OR value.state = 'conversation_deleted_pending_ack'
    )
  ORDER BY value.created_at, value.operation_id
  FOR UPDATE SKIP LOCKED
  LIMIT 1;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  new_lease_token := pg_catalog.gen_random_uuid();
  UPDATE memory_ingest_private.source_erasure_operation AS value
  SET lease_token = new_lease_token, leased_by = p_worker_id,
      lease_expires_at = pg_catalog.clock_timestamp()
        + pg_catalog.make_interval(secs => p_lease_seconds),
      attempt_count = CASE
        WHEN value.state = 'conversation_deleted_pending_ack'
             AND value.attempt_count >= 1000 THEN 1000
        ELSE value.attempt_count + 1
      END,
      state = CASE WHEN value.state IN ('fenced', 'retryable')
        THEN 'governed_deletion_pending' ELSE value.state END,
      last_error_code = NULL
  WHERE value.owner_user_id = target.owner_user_id
    AND value.operation_id = target.operation_id;
  RETURN QUERY SELECT target.operation_id, target.owner_user_id,
    target.selector_kind, target.selector_sha256, target.target_count,
    target.target_manifest_sha256,
    CASE WHEN target.state IN ('fenced', 'retryable')
      THEN 'governed_deletion_pending' ELSE target.state END,
    target.governed_receipt_sha256, new_lease_token;
END;
$function$;

CREATE FUNCTION memory_ingest_private.read_source_erasure_targets(
  p_operation_id uuid,
  p_lease_token uuid,
  p_after_created_at timestamptz,
  p_after_message_id uuid,
  p_limit integer
)
RETURNS TABLE(
  owner_user_id uuid,
  message_id uuid,
  thread_id uuid,
  source_created_at timestamptz,
  target_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  operation memory_ingest_private.source_erasure_operation%ROWTYPE;
BEGIN
  IF session_user <> 'governed_memory_worker'
     OR p_operation_id IS NULL OR p_lease_token IS NULL
     OR p_limit NOT BETWEEN 1 AND 500
     OR (p_after_created_at IS NULL) <> (p_after_message_id IS NULL) THEN
    RAISE EXCEPTION 'invalid source erasure target request'
      USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT operation
  FROM memory_ingest_private.source_erasure_operation AS value
  WHERE value.operation_id = p_operation_id;
  IF operation.lease_token <> p_lease_token
     OR operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'source erasure lease is stale'
      USING ERRCODE = '55000';
  END IF;
  RETURN QUERY
  SELECT target.owner_user_id, target.message_id, target.thread_id,
    target.source_created_at, target.target_sha256
  FROM memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id
    AND (
      p_after_created_at IS NULL
      OR (target.source_created_at, target.message_id)
           > (p_after_created_at, p_after_message_id)
    )
  ORDER BY target.source_created_at, target.message_id
  LIMIT p_limit;
END;
$function$;

CREATE FUNCTION memory_ingest_private.release_source_erasure_lease(
  p_operation_id uuid,
  p_lease_token uuid
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  IF session_user <> 'governed_memory_worker'
     OR p_operation_id IS NULL OR p_lease_token IS NULL THEN
    RAISE EXCEPTION 'invalid source erasure lease release'
      USING ERRCODE = '22023';
  END IF;
  UPDATE memory_ingest_private.source_erasure_operation AS value
  SET lease_token = NULL, leased_by = NULL, lease_expires_at = NULL
  WHERE value.operation_id = p_operation_id
    AND value.lease_token = p_lease_token;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'source erasure lease is stale'
      USING ERRCODE = '55000';
  END IF;
  RETURN 'released'::text;
END;
$function$;

CREATE FUNCTION memory_ingest_private.mark_source_erasure_governed_deleted(
  p_operation_id uuid,
  p_lease_token uuid,
  p_governed_receipt_sha256 text,
  p_expected_target_count integer,
  p_expected_target_manifest_sha256 text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  operation memory_ingest_private.source_erasure_operation%ROWTYPE;
BEGIN
  IF session_user <> 'governed_memory_worker'
     OR p_operation_id IS NULL OR p_lease_token IS NULL
     OR COALESCE(p_governed_receipt_sha256, '') !~ '^[0-9a-f]{64}$'
     OR p_expected_target_count NOT BETWEEN 0 AND 100000
     OR COALESCE(p_expected_target_manifest_sha256, '')
          !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid governed erasure receipt'
      USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT operation
  FROM memory_ingest_private.source_erasure_operation AS value
  WHERE value.operation_id = p_operation_id
  FOR UPDATE;
  IF operation.state = 'governed_deleted' THEN
    IF operation.governed_receipt_sha256 <> p_governed_receipt_sha256
       OR operation.target_count <> p_expected_target_count
       OR operation.target_manifest_sha256
            <> p_expected_target_manifest_sha256 THEN
      RAISE EXCEPTION 'governed erasure receipt replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN 'replayed'::text;
  END IF;
  IF operation.lease_token <> p_lease_token
     OR operation.lease_expires_at <= pg_catalog.clock_timestamp()
     OR operation.state <> 'governed_deletion_pending'
     OR operation.target_count <> p_expected_target_count
     OR operation.target_manifest_sha256 <> p_expected_target_manifest_sha256
  THEN
    RAISE EXCEPTION 'governed erasure receipt does not match fence'
      USING ERRCODE = '40001';
  END IF;
  UPDATE memory_ingest_private.source_erasure_operation AS value
  SET state = 'governed_deleted',
      governed_receipt_sha256 = p_governed_receipt_sha256,
      governed_completed_at = pg_catalog.transaction_timestamp(),
      lease_token = NULL, leased_by = NULL, lease_expires_at = NULL,
      last_error_code = NULL
  WHERE value.operation_id = p_operation_id;
  RETURN 'governed_deleted'::text;
END;
$function$;

CREATE FUNCTION memory_ingest_private.finalize_source_erasure(
  p_operation_id uuid,
  p_lease_token uuid,
  p_governed_receipt_sha256 text
)
RETURNS TABLE(
  outcome text,
  receipt_sha256 text,
  deleted_message_count integer,
  deleted_thread_count integer,
  deleted_attachment_count integer,
  deleted_bridge_row_count integer,
  message_tombstone_count integer,
  thread_tombstone_count integer,
  completed_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  operation memory_ingest_private.source_erasure_operation%ROWTYPE;
  stored memory_ingest_private.source_erasure_receipt%ROWTYPE;
  removed_messages integer := 0;
  removed_threads integer := 0;
  removed_attachments integer := 0;
  removed_bridge_rows integer := 0;
  removed_more integer := 0;
  message_tombstone_count integer := 0;
  thread_tombstone_count integer := 0;
  observed_message_tombstone_count integer := 0;
  observed_thread_tombstone_count integer := 0;
  observed_target_count integer := 0;
  observed_thread_target_count integer := 0;
  remaining_transcript boolean := false;
  finished_at timestamptz;
  observed_target_manifest text;
  observed_thread_target_manifest text;
  tombstone_manifest text;
  receipt_hash text;
  new_receipt_id uuid;
BEGIN
  IF session_user <> 'governed_memory_worker'
     OR p_operation_id IS NULL OR p_lease_token IS NULL
     OR COALESCE(p_governed_receipt_sha256, '') !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid source erasure finalization'
      USING ERRCODE = '22023';
  END IF;
  LOCK TABLE public.threads IN ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_log IN ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_attachments IN ROW EXCLUSIVE MODE;
  LOCK TABLE chat_integrity.assistant_transcript_attestation_v1
    IN ROW EXCLUSIVE MODE;
  IF pg_catalog.to_regclass('public.active_thread_selection') IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE public.active_thread_selection IN ROW EXCLUSIVE MODE';
  END IF;
  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE trusted_web.response_transcript_v1 IN ROW EXCLUSIVE MODE';
  END IF;
  LOCK TABLE memory_ingest_private.memory_ingest_outbox
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_operation
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_target
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_thread_target
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_message_tombstone
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_thread_tombstone
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_receipt
    IN ROW EXCLUSIVE MODE;
  PERFORM memory_ingest_private.assert_chat_deletion_catalog();
  SELECT value.* INTO STRICT operation
  FROM memory_ingest_private.source_erasure_operation AS value
  WHERE value.operation_id = p_operation_id
  FOR UPDATE;
  SELECT value.* INTO stored
  FROM memory_ingest_private.source_erasure_receipt AS value
  WHERE value.owner_user_id = operation.owner_user_id
    AND value.operation_id = operation.operation_id;
  IF FOUND THEN
    IF stored.governed_receipt_sha256 <> p_governed_receipt_sha256 THEN
      RAISE EXCEPTION 'source erasure finalization replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text, stored.receipt_sha256,
      stored.deleted_message_count, stored.deleted_thread_count,
      stored.deleted_attachment_count, stored.deleted_bridge_row_count,
      stored.message_tombstone_count, stored.thread_tombstone_count,
      stored.completed_at;
    RETURN;
  END IF;
  IF operation.state <> 'governed_deleted'
     OR operation.governed_receipt_sha256 <> p_governed_receipt_sha256
     OR operation.lease_token <> p_lease_token
     OR operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'governed deletion is not verified'
      USING ERRCODE = '40001';
  END IF;

  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      operation.owner_user_id::text || '|chat_source_erasure', 0
    )
  );
  SELECT pg_catalog.count(*)::integer,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.source_erasure_target_manifest.v1' || E'\n'
        || COALESCE(pg_catalog.string_agg(
          target.target_sha256, E'\n'
          ORDER BY target.source_created_at, target.message_id
        ), ''),
      'UTF8'
    )), 'hex')
  INTO observed_target_count, observed_target_manifest
  FROM memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id;
  SELECT pg_catalog.count(*)::integer,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.source_erasure_thread_target_manifest.v1' || E'\n'
        || COALESCE(pg_catalog.string_agg(
          target.target_sha256, E'\n'
          ORDER BY target.source_created_at, target.thread_id
        ), ''),
      'UTF8'
    )), 'hex')
  INTO observed_thread_target_count, observed_thread_target_manifest
  FROM memory_ingest_private.source_erasure_thread_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id;
  IF observed_target_count <> operation.target_count
     OR observed_target_manifest <> operation.target_manifest_sha256
     OR observed_thread_target_count <> operation.thread_target_count
     OR observed_thread_target_manifest <>
          operation.thread_target_manifest_sha256 THEN
    RAISE EXCEPTION 'conversation source target inventory drifted'
      USING ERRCODE = '40001';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN public.chat_log AS source
      ON source.id = target.message_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND (
        source.owner_user_id IS DISTINCT FROM target.owner_user_id
        OR source.thread_id IS DISTINCT FROM target.thread_id
        OR source.created_at IS DISTINCT FROM target.source_created_at
      )
  ) THEN
    RAISE EXCEPTION 'conversation source target lineage drifted'
      USING ERRCODE = '40001';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN memory_ingest_private.memory_ingest_outbox AS bridge
      ON bridge.message_id = target.message_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND bridge.owner_user_id IS DISTINCT FROM target.owner_user_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_target AS target
    JOIN public.chat_log AS residual
      ON residual.thread_id = target.thread_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND residual.owner_user_id IS DISTINCT FROM target.owner_user_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_target AS target
    JOIN public.threads AS thread
      ON thread.id = target.thread_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND (
        thread.owner_user_id IS DISTINCT FROM target.owner_user_id
        OR thread.created_at IS DISTINCT FROM target.source_created_at
      )
  ) THEN
    RAISE EXCEPTION 'conversation source owner/thread lineage drifted'
      USING ERRCODE = '40001';
  END IF;
  finished_at := pg_catalog.transaction_timestamp();
  INSERT INTO memory_ingest_private.source_erasure_message_tombstone(
    message_id, owner_user_id, operation_id, erased_at
  )
  SELECT target.message_id, target.owner_user_id, target.operation_id,
    finished_at
  FROM memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id
  ORDER BY target.message_id;
  GET DIAGNOSTICS message_tombstone_count = ROW_COUNT;
  IF message_tombstone_count <> operation.target_count THEN
    RAISE EXCEPTION 'conversation message tombstone count drifted'
      USING ERRCODE = '40001';
  END IF;
  DELETE FROM public.chat_attachments AS attachment
  USING memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id
    AND attachment.owner_user_id = target.owner_user_id
    AND attachment.thread_id = target.thread_id
    AND attachment.message_id = target.message_id;
  GET DIAGNOSTICS removed_attachments = ROW_COUNT;
  DELETE FROM public.chat_log AS source
  USING memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id
    AND source.owner_user_id = target.owner_user_id
    AND source.id = target.message_id;
  GET DIAGNOSTICS removed_messages = ROW_COUNT;
  IF removed_messages <> operation.target_count THEN
    RAISE EXCEPTION 'conversation message deletion count drifted'
      USING ERRCODE = '40001';
  END IF;

  INSERT INTO memory_ingest_private.source_erasure_thread_tombstone(
    thread_id, owner_user_id, operation_id, erased_at
  )
  SELECT target.thread_id, target.owner_user_id, target.operation_id,
    finished_at
  FROM memory_ingest_private.source_erasure_thread_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id
    AND NOT EXISTS (
      SELECT 1
      FROM public.chat_log AS remaining
      WHERE remaining.thread_id = target.thread_id
    )
  ORDER BY target.thread_id;
  GET DIAGNOSTICS thread_tombstone_count = ROW_COUNT;

  DELETE FROM public.chat_attachments AS attachment
  USING memory_ingest_private.source_erasure_thread_tombstone AS tombstone
  WHERE tombstone.owner_user_id = operation.owner_user_id
    AND tombstone.operation_id = operation.operation_id
    AND attachment.owner_user_id = operation.owner_user_id
    AND attachment.thread_id = tombstone.thread_id;
  GET DIAGNOSTICS removed_more = ROW_COUNT;
  removed_attachments := removed_attachments + removed_more;

  DELETE FROM public.threads AS thread
  USING memory_ingest_private.source_erasure_thread_tombstone AS tombstone
  WHERE tombstone.owner_user_id = operation.owner_user_id
    AND tombstone.operation_id = operation.operation_id
    AND thread.owner_user_id = operation.owner_user_id
    AND thread.id = tombstone.thread_id
    AND NOT EXISTS (
      SELECT 1
      FROM public.chat_log AS remaining
      WHERE remaining.thread_id = tombstone.thread_id
    );
  GET DIAGNOSTICS removed_threads = ROW_COUNT;
  IF removed_threads <> thread_tombstone_count THEN
    RAISE EXCEPTION 'conversation thread tombstone count drifted'
      USING ERRCODE = '40001';
  END IF;

  DELETE FROM memory_ingest_private.memory_ingest_outbox AS bridge
  USING memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id
    AND bridge.owner_user_id = target.owner_user_id
    AND bridge.message_id = target.message_id;
  GET DIAGNOSTICS removed_bridge_rows = ROW_COUNT;

  SELECT
    pg_catalog.count(*) FILTER (
      WHERE tombstone.identity_kind = 'message'
    )::integer,
    pg_catalog.count(*) FILTER (
      WHERE tombstone.identity_kind = 'thread'
    )::integer,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.conversation_suppression_manifest.v1' || E'\n'
        || COALESCE(pg_catalog.string_agg(
          memory_ingest_private.framed_utf8_field(
            'identity_kind', tombstone.identity_kind
          )
            || memory_ingest_private.framed_utf8_field(
                 'identity_id', tombstone.identity_id::text
               )
            || memory_ingest_private.framed_utf8_field(
                 'owner_user_id', tombstone.owner_user_id::text
               )
            || memory_ingest_private.framed_utf8_field(
                 'operation_id', tombstone.operation_id::text
               )
            || memory_ingest_private.framed_utf8_field(
                 'erased_at',
                 memory_ingest_private.timestamp_utc_text(
                   tombstone.erased_at
                 )
               ),
          '' ORDER BY tombstone.identity_kind, tombstone.identity_id
        ), ''),
      'UTF8'
    )), 'hex')
  INTO observed_message_tombstone_count,
    observed_thread_tombstone_count, tombstone_manifest
  FROM (
    SELECT 'message'::text AS identity_kind,
      value.message_id AS identity_id, value.owner_user_id,
      value.operation_id, value.erased_at
    FROM memory_ingest_private.source_erasure_message_tombstone AS value
    WHERE value.owner_user_id = operation.owner_user_id
      AND value.operation_id = operation.operation_id
    UNION ALL
    SELECT 'thread'::text AS identity_kind,
      value.thread_id AS identity_id, value.owner_user_id,
      value.operation_id, value.erased_at
    FROM memory_ingest_private.source_erasure_thread_tombstone AS value
    WHERE value.owner_user_id = operation.owner_user_id
      AND value.operation_id = operation.operation_id
  ) AS tombstone;
  IF observed_message_tombstone_count <> message_tombstone_count
     OR observed_thread_tombstone_count <> thread_tombstone_count THEN
    RAISE EXCEPTION 'conversation suppression tombstone inventory drifted'
      USING ERRCODE = '40001';
  END IF;

  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'SELECT EXISTS ('
      'SELECT 1 '
      'FROM trusted_web.response_transcript_v1 AS transcript '
      'JOIN memory_ingest_private.source_erasure_target AS target '
      'ON (target.message_id = transcript.user_chat_log_id '
      'OR target.message_id = transcript.assistant_chat_log_id) '
      'WHERE target.owner_user_id = $1 '
      'AND target.operation_id = $2'
      ')'
    INTO remaining_transcript
    USING operation.owner_user_id, operation.operation_id;
  END IF;
  IF remaining_transcript OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND NOT EXISTS (
        SELECT 1
        FROM memory_ingest_private.source_erasure_message_tombstone
          AS tombstone
        WHERE tombstone.message_id = target.message_id
          AND tombstone.owner_user_id = target.owner_user_id
          AND tombstone.operation_id = target.operation_id
          AND tombstone.erased_at = finished_at
      )
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_target AS target
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND NOT EXISTS (
        SELECT 1
        FROM public.chat_log AS remaining
        WHERE remaining.thread_id = target.thread_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM memory_ingest_private.source_erasure_thread_tombstone
          AS tombstone
        WHERE tombstone.thread_id = target.thread_id
          AND tombstone.owner_user_id = target.owner_user_id
          AND tombstone.operation_id = target.operation_id
          AND tombstone.erased_at = finished_at
      )
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN public.chat_log AS source
      ON source.id = target.message_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN public.chat_attachments AS attachment
      ON attachment.message_id = target.message_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN memory_ingest_private.memory_ingest_outbox AS bridge
      ON bridge.message_id = target.message_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_tombstone AS tombstone
    JOIN public.threads AS thread
      ON thread.id = tombstone.thread_id
    WHERE tombstone.owner_user_id = operation.owner_user_id
      AND tombstone.operation_id = operation.operation_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_tombstone AS tombstone
    JOIN public.chat_attachments AS attachment
      ON attachment.thread_id = tombstone.thread_id
    WHERE tombstone.owner_user_id = operation.owner_user_id
      AND tombstone.operation_id = operation.operation_id
  ) THEN
    RAISE EXCEPTION 'conversation source absence verification failed'
      USING ERRCODE = '40001';
  END IF;
  receipt_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.conversation_source_erasure_receipt.v2' || E'\n'
      || memory_ingest_private.framed_utf8_field(
           'owner_user_id', operation.owner_user_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'operation_id', operation.operation_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'selector_sha256', operation.selector_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'target_manifest_sha256', operation.target_manifest_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'target_count', operation.target_count::text
         )
      || memory_ingest_private.framed_utf8_field(
           'thread_target_manifest_sha256',
           operation.thread_target_manifest_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'thread_target_count', operation.thread_target_count::text
         )
      || memory_ingest_private.framed_utf8_field(
           'deleted_message_count', removed_messages::text
         )
      || memory_ingest_private.framed_utf8_field(
           'deleted_thread_count', removed_threads::text
         )
      || memory_ingest_private.framed_utf8_field(
           'deleted_attachment_count', removed_attachments::text
         )
      || memory_ingest_private.framed_utf8_field(
           'deleted_bridge_row_count', removed_bridge_rows::text
         )
      || memory_ingest_private.framed_utf8_field(
           'message_tombstone_count', message_tombstone_count::text
         )
      || memory_ingest_private.framed_utf8_field(
           'thread_tombstone_count', thread_tombstone_count::text
         )
      || memory_ingest_private.framed_utf8_field(
           'tombstone_manifest_sha256', tombstone_manifest
         )
      || memory_ingest_private.framed_utf8_field(
           'governed_receipt_sha256', p_governed_receipt_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'completed_at',
           memory_ingest_private.timestamp_utc_text(finished_at)
         ),
    'UTF8'
  )), 'hex');
  new_receipt_id := pg_catalog.gen_random_uuid();
  INSERT INTO memory_ingest_private.source_erasure_receipt(
    receipt_id, owner_user_id, operation_id, selector_sha256,
    target_manifest_sha256, target_count, thread_target_manifest_sha256,
    thread_target_count, deleted_message_count,
    deleted_thread_count, deleted_attachment_count,
    deleted_bridge_row_count, message_tombstone_count,
    thread_tombstone_count, tombstone_manifest_sha256,
    governed_receipt_sha256, receipt_sha256, completed_at
  ) VALUES (
    new_receipt_id, operation.owner_user_id, operation.operation_id,
    operation.selector_sha256, operation.target_manifest_sha256,
    operation.target_count, operation.thread_target_manifest_sha256,
    operation.thread_target_count, removed_messages, removed_threads,
    removed_attachments, removed_bridge_rows,
    message_tombstone_count, thread_tombstone_count, tombstone_manifest,
    p_governed_receipt_sha256, receipt_hash, finished_at
  );
  UPDATE memory_ingest_private.source_erasure_operation AS value
  SET state = 'conversation_deleted_pending_ack',
      completed_at = finished_at, last_error_code = NULL
  WHERE value.owner_user_id = operation.owner_user_id
    AND value.operation_id = operation.operation_id;
  RETURN QUERY SELECT 'conversation_deleted_pending_ack'::text, receipt_hash,
    removed_messages, removed_threads, removed_attachments,
    removed_bridge_rows, message_tombstone_count, thread_tombstone_count,
    finished_at;
END;
$function$;

CREATE FUNCTION memory_ingest_private.ack_source_erasure_completion(
  p_operation_id uuid,
  p_lease_token uuid,
  p_conversation_receipt_sha256 text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  operation memory_ingest_private.source_erasure_operation%ROWTYPE;
  stored memory_ingest_private.source_erasure_receipt%ROWTYPE;
  remaining_transcript boolean := false;
  observed_target_count integer := 0;
  observed_thread_target_count integer := 0;
  observed_message_tombstone_count integer := 0;
  observed_thread_tombstone_count integer := 0;
  removed_target_count integer := 0;
  removed_thread_target_count integer := 0;
  observed_target_manifest text;
  observed_thread_target_manifest text;
  observed_tombstone_manifest text;
BEGIN
  IF session_user <> 'governed_memory_worker'
     OR p_operation_id IS NULL OR p_lease_token IS NULL
     OR COALESCE(p_conversation_receipt_sha256, '')
          !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid source erasure completion acknowledgement'
      USING ERRCODE = '22023';
  END IF;
  LOCK TABLE public.threads IN ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_log IN ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_attachments IN ROW EXCLUSIVE MODE;
  LOCK TABLE chat_integrity.assistant_transcript_attestation_v1
    IN ROW EXCLUSIVE MODE;
  IF pg_catalog.to_regclass('public.active_thread_selection') IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE public.active_thread_selection IN ROW EXCLUSIVE MODE';
  END IF;
  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE trusted_web.response_transcript_v1 IN ROW EXCLUSIVE MODE';
  END IF;
  LOCK TABLE memory_ingest_private.memory_ingest_outbox
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_operation
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_target
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_thread_target
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_message_tombstone
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_thread_tombstone
    IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.source_erasure_receipt
    IN ROW EXCLUSIVE MODE;
  PERFORM memory_ingest_private.assert_chat_deletion_catalog();
  SELECT value.* INTO STRICT operation
  FROM memory_ingest_private.source_erasure_operation AS value
  WHERE value.operation_id = p_operation_id
  FOR UPDATE;
  SELECT value.* INTO STRICT stored
  FROM memory_ingest_private.source_erasure_receipt AS value
  WHERE value.owner_user_id = operation.owner_user_id
    AND value.operation_id = operation.operation_id;
  IF stored.receipt_sha256 <> p_conversation_receipt_sha256 THEN
    RAISE EXCEPTION 'source erasure completion receipt drifted'
      USING ERRCODE = '23514';
  END IF;
  IF operation.state = 'completed' THEN
    RETURN 'replayed'::text;
  END IF;
  IF operation.state <> 'conversation_deleted_pending_ack'
     OR operation.lease_token <> p_lease_token
     OR operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'source erasure completion lease is stale'
      USING ERRCODE = '55000';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      operation.owner_user_id::text || '|chat_source_erasure', 0
    )
  );
  SELECT pg_catalog.count(*)::integer,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.source_erasure_target_manifest.v1' || E'\n'
        || COALESCE(pg_catalog.string_agg(
          target.target_sha256, E'\n'
          ORDER BY target.source_created_at, target.message_id
        ), ''),
      'UTF8'
    )), 'hex')
  INTO observed_target_count, observed_target_manifest
  FROM memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id;
  SELECT pg_catalog.count(*)::integer,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.source_erasure_thread_target_manifest.v1' || E'\n'
        || COALESCE(pg_catalog.string_agg(
          target.target_sha256, E'\n'
          ORDER BY target.source_created_at, target.thread_id
        ), ''),
      'UTF8'
    )), 'hex')
  INTO observed_thread_target_count, observed_thread_target_manifest
  FROM memory_ingest_private.source_erasure_thread_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id;
  IF observed_target_count <> operation.target_count
     OR observed_target_manifest <> operation.target_manifest_sha256
     OR observed_target_manifest <> stored.target_manifest_sha256
     OR observed_thread_target_count <> operation.thread_target_count
     OR observed_thread_target_count <> stored.thread_target_count
     OR observed_thread_target_manifest <>
          operation.thread_target_manifest_sha256
     OR observed_thread_target_manifest <>
          stored.thread_target_manifest_sha256 THEN
    RAISE EXCEPTION 'pending-ack source erasure target inventory drifted'
      USING ERRCODE = '40001';
  END IF;

  SELECT
    pg_catalog.count(*) FILTER (
      WHERE tombstone.identity_kind = 'message'
    )::integer,
    pg_catalog.count(*) FILTER (
      WHERE tombstone.identity_kind = 'thread'
    )::integer,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.conversation_suppression_manifest.v1' || E'\n'
        || COALESCE(pg_catalog.string_agg(
          memory_ingest_private.framed_utf8_field(
            'identity_kind', tombstone.identity_kind
          )
            || memory_ingest_private.framed_utf8_field(
                 'identity_id', tombstone.identity_id::text
               )
            || memory_ingest_private.framed_utf8_field(
                 'owner_user_id', tombstone.owner_user_id::text
               )
            || memory_ingest_private.framed_utf8_field(
                 'operation_id', tombstone.operation_id::text
               )
            || memory_ingest_private.framed_utf8_field(
                 'erased_at',
                 memory_ingest_private.timestamp_utc_text(
                   tombstone.erased_at
                 )
               ),
          '' ORDER BY tombstone.identity_kind, tombstone.identity_id
        ), ''),
      'UTF8'
    )), 'hex')
  INTO observed_message_tombstone_count,
    observed_thread_tombstone_count, observed_tombstone_manifest
  FROM (
    SELECT 'message'::text AS identity_kind,
      value.message_id AS identity_id, value.owner_user_id,
      value.operation_id, value.erased_at
    FROM memory_ingest_private.source_erasure_message_tombstone AS value
    WHERE value.owner_user_id = operation.owner_user_id
      AND value.operation_id = operation.operation_id
    UNION ALL
    SELECT 'thread'::text AS identity_kind,
      value.thread_id AS identity_id, value.owner_user_id,
      value.operation_id, value.erased_at
    FROM memory_ingest_private.source_erasure_thread_tombstone AS value
    WHERE value.owner_user_id = operation.owner_user_id
      AND value.operation_id = operation.operation_id
  ) AS tombstone;
  IF observed_message_tombstone_count <> stored.message_tombstone_count
     OR observed_message_tombstone_count <> operation.target_count
     OR observed_thread_tombstone_count <> stored.thread_tombstone_count
     OR observed_thread_tombstone_count <> stored.deleted_thread_count
     OR observed_thread_tombstone_count > stored.thread_target_count
     OR observed_tombstone_manifest <> stored.tombstone_manifest_sha256 THEN
    RAISE EXCEPTION 'pending-ack suppression tombstone receipt drifted'
      USING ERRCODE = '40001';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND NOT EXISTS (
        SELECT 1
        FROM memory_ingest_private.source_erasure_message_tombstone
          AS tombstone
        WHERE tombstone.message_id = target.message_id
          AND tombstone.owner_user_id = target.owner_user_id
          AND tombstone.operation_id = target.operation_id
          AND tombstone.erased_at = stored.completed_at
      )
  ) THEN
    RAISE EXCEPTION 'pending-ack message tombstone binding drifted'
      USING ERRCODE = '40001';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_target AS target
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND NOT EXISTS (
        SELECT 1
        FROM public.chat_log AS remaining
        WHERE remaining.thread_id = target.thread_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM memory_ingest_private.source_erasure_thread_tombstone
          AS tombstone
        WHERE tombstone.thread_id = target.thread_id
          AND tombstone.owner_user_id = target.owner_user_id
          AND tombstone.operation_id = target.operation_id
          AND tombstone.erased_at = stored.completed_at
      )
  ) THEN
    RAISE EXCEPTION 'pending-ack thread tombstone binding drifted'
      USING ERRCODE = '40001';
  END IF;

  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'SELECT EXISTS ('
      'SELECT 1 '
      'FROM trusted_web.response_transcript_v1 AS transcript '
      'JOIN memory_ingest_private.source_erasure_target AS target '
      'ON (target.message_id = transcript.user_chat_log_id '
      'OR target.message_id = transcript.assistant_chat_log_id) '
      'WHERE target.owner_user_id = $1 '
      'AND target.operation_id = $2'
      ')'
    INTO remaining_transcript
    USING operation.owner_user_id, operation.operation_id;
  END IF;
  IF remaining_transcript OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN public.chat_log AS source
      ON source.id = target.message_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN public.chat_attachments AS attachment
      ON attachment.message_id = target.message_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN memory_ingest_private.memory_ingest_outbox AS bridge
      ON bridge.message_id = target.message_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_target AS target
    JOIN public.chat_log AS residual
      ON residual.thread_id = target.thread_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND residual.owner_user_id IS DISTINCT FROM target.owner_user_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_target AS target
    JOIN public.threads AS thread
      ON thread.id = target.thread_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
      AND (
        thread.owner_user_id IS DISTINCT FROM target.owner_user_id
        OR thread.created_at IS DISTINCT FROM target.source_created_at
      )
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_tombstone AS tombstone
    JOIN public.threads AS thread
      ON thread.id = tombstone.thread_id
    WHERE tombstone.owner_user_id = operation.owner_user_id
      AND tombstone.operation_id = operation.operation_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_tombstone AS tombstone
    JOIN public.chat_attachments AS attachment
      ON attachment.thread_id = tombstone.thread_id
    WHERE tombstone.owner_user_id = operation.owner_user_id
      AND tombstone.operation_id = operation.operation_id
  ) THEN
    RAISE EXCEPTION 'pending-ack conversation absence verification failed'
      USING ERRCODE = '40001';
  END IF;
  DELETE FROM memory_ingest_private.source_erasure_thread_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id;
  GET DIAGNOSTICS removed_thread_target_count = ROW_COUNT;
  IF removed_thread_target_count <> operation.thread_target_count THEN
    RAISE EXCEPTION 'pending-ack thread target purge drifted'
      USING ERRCODE = '40001';
  END IF;
  DELETE FROM memory_ingest_private.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id;
  GET DIAGNOSTICS removed_target_count = ROW_COUNT;
  IF removed_target_count <> operation.target_count THEN
    RAISE EXCEPTION 'pending-ack source erasure target purge drifted'
      USING ERRCODE = '40001';
  END IF;
  UPDATE memory_ingest_private.source_erasure_operation AS value
  SET state = 'completed', lease_token = NULL, leased_by = NULL,
      lease_expires_at = NULL, last_error_code = NULL
  WHERE value.owner_user_id = operation.owner_user_id
    AND value.operation_id = operation.operation_id;
  RETURN 'completed'::text;
END;
$function$;

CREATE FUNCTION memory_ingest_private.fail_source_erasure(
  p_operation_id uuid,
  p_lease_token uuid,
  p_failure_mode text,
  p_error_code text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  resulting_state text;
  operation memory_ingest_private.source_erasure_operation%ROWTYPE;
BEGIN
  IF session_user <> 'governed_memory_worker'
     OR p_operation_id IS NULL OR p_lease_token IS NULL
     OR p_failure_mode NOT IN ('retryable', 'manual_review')
     OR pg_catalog.octet_length(COALESCE(p_error_code, '')) NOT BETWEEN 1 AND 128
     OR p_error_code !~ '^[a-z][a-z0-9_]{0,127}$' THEN
    RAISE EXCEPTION 'invalid source erasure failure'
      USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT operation
  FROM memory_ingest_private.source_erasure_operation AS value
  WHERE value.operation_id = p_operation_id
  FOR UPDATE;
  IF operation.lease_token <> p_lease_token
     OR operation.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'source erasure lease is stale'
      USING ERRCODE = '55000';
  END IF;
  IF operation.state = 'conversation_deleted_pending_ack' THEN
    IF p_failure_mode <> 'retryable' THEN
      RAISE EXCEPTION 'deleted conversation may only remain pending ack'
        USING ERRCODE = '55000';
    END IF;
    resulting_state := 'conversation_deleted_pending_ack';
  ELSE
    resulting_state := p_failure_mode;
  END IF;
  UPDATE memory_ingest_private.source_erasure_operation AS value
  SET state = resulting_state, lease_token = NULL, leased_by = NULL,
      lease_expires_at = NULL, last_error_code = p_error_code
  WHERE value.operation_id = p_operation_id
    AND value.lease_token = p_lease_token;
  RETURN resulting_state;
END;
$function$;

CREATE FUNCTION memory_ingest_private.serialize_chat_source_erasure()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  fence_owner uuid;
BEGIN
  FOR fence_owner IN
    SELECT candidate.owner_id
    FROM (
      SELECT NEW.owner_user_id AS owner_id
      UNION
      SELECT target.owner_user_id
      FROM memory_ingest_private.source_erasure_target AS target
      JOIN memory_ingest_private.source_erasure_operation AS operation
        ON operation.owner_user_id = target.owner_user_id
       AND operation.operation_id = target.operation_id
      WHERE target.message_id = NEW.id
        AND operation.state <> 'completed'
      UNION
      SELECT target.owner_user_id
      FROM memory_ingest_private.source_erasure_thread_target AS target
      JOIN memory_ingest_private.source_erasure_operation AS operation
        ON operation.owner_user_id = target.owner_user_id
       AND operation.operation_id = target.operation_id
      WHERE target.thread_id = NEW.thread_id
        AND operation.state <> 'completed'
      UNION
      SELECT tombstone.owner_user_id
      FROM memory_ingest_private.source_erasure_message_tombstone
        AS tombstone
      WHERE tombstone.message_id = NEW.id
      UNION
      SELECT tombstone.owner_user_id
      FROM memory_ingest_private.source_erasure_thread_tombstone
        AS tombstone
      WHERE tombstone.thread_id = NEW.thread_id
    ) AS candidate
    WHERE candidate.owner_id IS NOT NULL
    ORDER BY candidate.owner_id::text
  LOOP
    PERFORM pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        fence_owner::text || '|chat_source_erasure', 0
      )
    );
  END LOOP;
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_message_tombstone AS tombstone
    WHERE tombstone.message_id = NEW.id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_tombstone AS tombstone
    WHERE tombstone.thread_id = NEW.thread_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_operation AS operation
    WHERE operation.state <> 'completed'
      AND (
        (operation.owner_user_id = NEW.owner_user_id
          AND (
            (operation.selector_kind = 'thread'
              AND NEW.thread_id = operation.selector_thread_id)
            OR
            (operation.selector_kind = 'message_tail'
              AND NEW.thread_id = operation.selector_thread_id
              AND (
                NEW.created_at IS NULL
                OR (
                  NEW.created_at >= operation.selector_from_inclusive
                  AND NEW.created_at <= operation.selector_through_inclusive
                )
              ))
            OR
            (operation.selector_kind = 'recent'
              AND (
                NEW.created_at IS NULL
                OR (
                  NEW.created_at >= operation.selector_from_inclusive
                  AND NEW.created_at <= operation.selector_through_inclusive
                )
              ))
            OR
            (operation.selector_kind = 'all_conversations'
              AND (
                NEW.thread_id IS NULL OR NEW.created_at IS NULL
                OR NEW.created_at <= operation.selector_through_inclusive
              ))
          ))
        OR
        EXISTS (
          SELECT 1
          FROM memory_ingest_private.source_erasure_target AS target
          WHERE target.owner_user_id = operation.owner_user_id
            AND target.operation_id = operation.operation_id
            AND target.message_id = NEW.id
        )
        OR
        (NEW.owner_user_id IS DISTINCT FROM operation.owner_user_id
          AND EXISTS (
            SELECT 1
            FROM memory_ingest_private.source_erasure_thread_target
              AS target
            WHERE target.owner_user_id = operation.owner_user_id
              AND target.operation_id = operation.operation_id
              AND target.thread_id = NEW.thread_id
          ))
      )
  ) THEN
    RAISE EXCEPTION 'chat source erasure fence is active'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$function$;

CREATE FUNCTION memory_ingest_private.serialize_thread_source_erasure()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  fence_owner uuid;
BEGIN
  IF TG_OP = 'INSERT' THEN
    FOR fence_owner IN
      SELECT candidate.owner_id
      FROM (
        SELECT NEW.owner_user_id AS owner_id
        UNION
        SELECT target.owner_user_id
        FROM memory_ingest_private.source_erasure_thread_target AS target
        JOIN memory_ingest_private.source_erasure_operation AS operation
          ON operation.owner_user_id = target.owner_user_id
         AND operation.operation_id = target.operation_id
        WHERE target.thread_id = NEW.id
          AND operation.state <> 'completed'
        UNION
        SELECT tombstone.owner_user_id
        FROM memory_ingest_private.source_erasure_thread_tombstone
          AS tombstone
        WHERE tombstone.thread_id = NEW.id
      ) AS candidate
      WHERE candidate.owner_id IS NOT NULL
      ORDER BY candidate.owner_id::text
    LOOP
      PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
          fence_owner::text || '|chat_source_erasure', 0
        )
      );
    END LOOP;
  ELSE
    FOR fence_owner IN
      SELECT candidate.owner_id
      FROM (
        SELECT NEW.owner_user_id AS owner_id
        UNION SELECT OLD.owner_user_id
        UNION
        SELECT target.owner_user_id
        FROM memory_ingest_private.source_erasure_thread_target AS target
        JOIN memory_ingest_private.source_erasure_operation AS operation
          ON operation.owner_user_id = target.owner_user_id
         AND operation.operation_id = target.operation_id
        WHERE target.thread_id IN (NEW.id, OLD.id)
          AND operation.state <> 'completed'
        UNION
        SELECT tombstone.owner_user_id
        FROM memory_ingest_private.source_erasure_thread_tombstone
          AS tombstone
        WHERE tombstone.thread_id IN (NEW.id, OLD.id)
      ) AS candidate
      WHERE candidate.owner_id IS NOT NULL
      ORDER BY candidate.owner_id::text
    LOOP
      PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
          fence_owner::text || '|chat_source_erasure', 0
        )
      );
    END LOOP;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_tombstone AS tombstone
    WHERE tombstone.thread_id = NEW.id
  ) OR (
    TG_OP = 'UPDATE' AND EXISTS (
      SELECT 1
      FROM memory_ingest_private.source_erasure_thread_tombstone AS tombstone
      WHERE tombstone.thread_id = OLD.id
    )
  ) THEN
    RAISE EXCEPTION 'erased chat thread identity may not be reused'
      USING ERRCODE = '55000';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_target AS target
    JOIN memory_ingest_private.source_erasure_operation AS operation
      ON operation.owner_user_id = target.owner_user_id
     AND operation.operation_id = target.operation_id
    WHERE operation.state <> 'completed'
      AND target.thread_id = NEW.id
  ) OR (
    TG_OP = 'UPDATE' AND EXISTS (
      SELECT 1
      FROM memory_ingest_private.source_erasure_thread_target AS target
      JOIN memory_ingest_private.source_erasure_operation AS operation
        ON operation.owner_user_id = target.owner_user_id
       AND operation.operation_id = target.operation_id
      WHERE operation.state <> 'completed'
        AND target.thread_id = OLD.id
    )
  ) THEN
    RAISE EXCEPTION 'thread source erasure fence is active'
      USING ERRCODE = '55000';
  END IF;

  IF NEW.owner_user_id IS NOT NULL AND EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_operation AS operation
    WHERE operation.owner_user_id = NEW.owner_user_id
      AND operation.state <> 'completed'
      AND (
        (operation.selector_kind = 'thread'
          AND NEW.id = operation.selector_thread_id)
        OR
        (operation.selector_kind = 'all_conversations'
          AND (
            NEW.created_at IS NULL
            OR NEW.created_at <= operation.selector_through_inclusive
          ))
      )
  ) THEN
    RAISE EXCEPTION 'thread source erasure fence is active'
      USING ERRCODE = '55000';
  END IF;
  IF TG_OP = 'UPDATE'
     AND OLD.owner_user_id IS NOT NULL
     AND EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_operation AS operation
    WHERE operation.owner_user_id = OLD.owner_user_id
      AND operation.state <> 'completed'
      AND (
        (operation.selector_kind = 'thread'
          AND OLD.id = operation.selector_thread_id)
        OR
        (operation.selector_kind = 'all_conversations'
          AND (
            OLD.created_at IS NULL
            OR OLD.created_at <= operation.selector_through_inclusive
          ))
      )
  ) THEN
    RAISE EXCEPTION 'thread source erasure fence is active'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$function$;

CREATE FUNCTION memory_ingest_private.serialize_attachment_source_erasure()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  fence_owner uuid;
BEGIN
  IF TG_OP = 'INSERT' THEN
    FOR fence_owner IN
      SELECT candidate.owner_id
      FROM (
        SELECT NEW.owner_user_id AS owner_id
        UNION
        SELECT target.owner_user_id
        FROM memory_ingest_private.source_erasure_target AS target
        JOIN memory_ingest_private.source_erasure_operation AS operation
          ON operation.owner_user_id = target.owner_user_id
         AND operation.operation_id = target.operation_id
        WHERE target.message_id = NEW.message_id
          AND operation.state <> 'completed'
        UNION
        SELECT target.owner_user_id
        FROM memory_ingest_private.source_erasure_thread_target AS target
        JOIN memory_ingest_private.source_erasure_operation AS operation
          ON operation.owner_user_id = target.owner_user_id
         AND operation.operation_id = target.operation_id
        WHERE target.thread_id = NEW.thread_id
          AND operation.state <> 'completed'
        UNION
        SELECT tombstone.owner_user_id
        FROM memory_ingest_private.source_erasure_message_tombstone
          AS tombstone
        WHERE tombstone.message_id = NEW.message_id
        UNION
        SELECT tombstone.owner_user_id
        FROM memory_ingest_private.source_erasure_thread_tombstone
          AS tombstone
        WHERE tombstone.thread_id = NEW.thread_id
      ) AS candidate
      WHERE candidate.owner_id IS NOT NULL
      ORDER BY candidate.owner_id::text
    LOOP
      PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
          fence_owner::text || '|chat_source_erasure', 0
        )
      );
    END LOOP;
  ELSE
    FOR fence_owner IN
      SELECT candidate.owner_id
      FROM (
        SELECT NEW.owner_user_id AS owner_id
        UNION SELECT OLD.owner_user_id
        UNION
        SELECT target.owner_user_id
        FROM memory_ingest_private.source_erasure_target AS target
        JOIN memory_ingest_private.source_erasure_operation AS operation
          ON operation.owner_user_id = target.owner_user_id
         AND operation.operation_id = target.operation_id
        WHERE target.message_id IN (NEW.message_id, OLD.message_id)
          AND operation.state <> 'completed'
        UNION
        SELECT target.owner_user_id
        FROM memory_ingest_private.source_erasure_thread_target AS target
        JOIN memory_ingest_private.source_erasure_operation AS operation
          ON operation.owner_user_id = target.owner_user_id
         AND operation.operation_id = target.operation_id
        WHERE target.thread_id IN (NEW.thread_id, OLD.thread_id)
          AND operation.state <> 'completed'
        UNION
        SELECT tombstone.owner_user_id
        FROM memory_ingest_private.source_erasure_message_tombstone
          AS tombstone
        WHERE tombstone.message_id IN (NEW.message_id, OLD.message_id)
        UNION
        SELECT tombstone.owner_user_id
        FROM memory_ingest_private.source_erasure_thread_tombstone
          AS tombstone
        WHERE tombstone.thread_id IN (NEW.thread_id, OLD.thread_id)
      ) AS candidate
      WHERE candidate.owner_id IS NOT NULL
      ORDER BY candidate.owner_id::text
    LOOP
      PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
          fence_owner::text || '|chat_source_erasure', 0
        )
      );
    END LOOP;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_message_tombstone AS tombstone
    WHERE tombstone.message_id = NEW.message_id
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_tombstone AS tombstone
    WHERE tombstone.thread_id = NEW.thread_id
  ) OR (
    TG_OP = 'UPDATE' AND (
      EXISTS (
        SELECT 1
        FROM memory_ingest_private.source_erasure_message_tombstone
          AS tombstone
        WHERE tombstone.message_id = OLD.message_id
      ) OR EXISTS (
        SELECT 1
        FROM memory_ingest_private.source_erasure_thread_tombstone
          AS tombstone
        WHERE tombstone.thread_id = OLD.thread_id
      )
    )
  ) THEN
    RAISE EXCEPTION 'erased chat attachment identity may not be reused'
      USING ERRCODE = '55000';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_operation AS operation
    WHERE operation.state <> 'completed'
      AND (
        EXISTS (
          SELECT 1
          FROM memory_ingest_private.source_erasure_target AS target
          WHERE target.owner_user_id = operation.owner_user_id
            AND target.operation_id = operation.operation_id
            AND target.message_id = NEW.message_id
        )
        OR EXISTS (
          SELECT 1
          FROM memory_ingest_private.source_erasure_thread_target AS target
          WHERE target.owner_user_id = operation.owner_user_id
            AND target.operation_id = operation.operation_id
            AND target.thread_id = NEW.thread_id
        )
        OR (TG_OP = 'UPDATE' AND (
          EXISTS (
            SELECT 1
            FROM memory_ingest_private.source_erasure_target AS target
            WHERE target.owner_user_id = operation.owner_user_id
              AND target.operation_id = operation.operation_id
              AND target.message_id = OLD.message_id
          ) OR EXISTS (
            SELECT 1
            FROM memory_ingest_private.source_erasure_thread_target AS target
            WHERE target.owner_user_id = operation.owner_user_id
              AND target.operation_id = operation.operation_id
              AND target.thread_id = OLD.thread_id
          )
        ))
      )
  ) THEN
    RAISE EXCEPTION 'attachment source erasure fence is active'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$function$;

CREATE FUNCTION
  memory_ingest_private.serialize_response_transcript_source_erasure()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  fence_owner uuid;
BEGIN
  IF TG_OP = 'INSERT' THEN
    FOR fence_owner IN
      SELECT candidate.owner_id
      FROM (
        SELECT NEW.owner_user_id AS owner_id
        UNION
        SELECT source.owner_user_id
        FROM public.chat_log AS source
        WHERE source.id IN (
          NEW.user_chat_log_id, NEW.assistant_chat_log_id
        )
        UNION
        SELECT target.owner_user_id
        FROM memory_ingest_private.source_erasure_target AS target
        WHERE target.message_id IN (
          NEW.user_chat_log_id, NEW.assistant_chat_log_id
        )
      ) AS candidate
      WHERE candidate.owner_id IS NOT NULL
      ORDER BY candidate.owner_id::text
    LOOP
      PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
          fence_owner::text || '|chat_source_erasure', 0
        )
      );
    END LOOP;
  ELSE
    FOR fence_owner IN
      SELECT candidate.owner_id
      FROM (
        SELECT NEW.owner_user_id AS owner_id
        UNION SELECT OLD.owner_user_id
        UNION
        SELECT source.owner_user_id
        FROM public.chat_log AS source
        WHERE source.id IN (
          NEW.user_chat_log_id, NEW.assistant_chat_log_id,
          OLD.user_chat_log_id, OLD.assistant_chat_log_id
        )
        UNION
        SELECT target.owner_user_id
        FROM memory_ingest_private.source_erasure_target AS target
        WHERE target.message_id IN (
          NEW.user_chat_log_id, NEW.assistant_chat_log_id,
          OLD.user_chat_log_id, OLD.assistant_chat_log_id
        )
      ) AS candidate
      WHERE candidate.owner_id IS NOT NULL
      ORDER BY candidate.owner_id::text
    LOOP
      PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
          fence_owner::text || '|chat_source_erasure', 0
        )
      );
    END LOOP;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN memory_ingest_private.source_erasure_operation AS operation
      ON operation.owner_user_id = target.owner_user_id
     AND operation.operation_id = target.operation_id
    WHERE operation.state <> 'completed'
      AND target.message_id IN (
        NEW.user_chat_log_id, NEW.assistant_chat_log_id
      )
  ) THEN
    RAISE EXCEPTION 'response transcript source erasure fence is active'
      USING ERRCODE = '55000';
  END IF;
  IF TG_OP = 'UPDATE' AND EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target AS target
    JOIN memory_ingest_private.source_erasure_operation AS operation
      ON operation.owner_user_id = target.owner_user_id
     AND operation.operation_id = target.operation_id
    WHERE operation.state <> 'completed'
      AND target.message_id IN (
        OLD.user_chat_log_id, OLD.assistant_chat_log_id
      )
  ) THEN
    RAISE EXCEPTION 'response transcript source erasure fence is active'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$function$;

CREATE FUNCTION memory_ingest_private.guard_source_erasure_receipt_immutable()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'source erasure receipt or suppression tombstone is immutable'
    USING ERRCODE = '55000';
END;
$function$;

CREATE TRIGGER chat_log_serialize_source_erasure
  BEFORE INSERT ON public.chat_log
  FOR EACH ROW EXECUTE FUNCTION
    memory_ingest_private.serialize_chat_source_erasure();
CREATE TRIGGER threads_serialize_source_erasure
  BEFORE INSERT OR UPDATE ON public.threads
  FOR EACH ROW EXECUTE FUNCTION
    memory_ingest_private.serialize_thread_source_erasure();
CREATE TRIGGER chat_attachments_serialize_source_erasure
  BEFORE INSERT OR UPDATE ON public.chat_attachments
  FOR EACH ROW EXECUTE FUNCTION
    memory_ingest_private.serialize_attachment_source_erasure();
DO $response_transcript_trigger$
BEGIN
  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'CREATE TRIGGER response_transcript_serialize_source_erasure '
      'BEFORE INSERT OR UPDATE '
      'ON trusted_web.response_transcript_v1 '
      'FOR EACH ROW EXECUTE FUNCTION '
      'memory_ingest_private.'
      'serialize_response_transcript_source_erasure()';
  END IF;
END;
$response_transcript_trigger$;
CREATE TRIGGER source_erasure_receipt_immutable
  BEFORE UPDATE OR DELETE
  ON memory_ingest_private.source_erasure_receipt
  FOR EACH ROW EXECUTE FUNCTION
    memory_ingest_private.guard_source_erasure_receipt_immutable();
CREATE TRIGGER source_erasure_message_tombstone_immutable
  BEFORE UPDATE OR DELETE
  ON memory_ingest_private.source_erasure_message_tombstone
  FOR EACH ROW EXECUTE FUNCTION
    memory_ingest_private.guard_source_erasure_receipt_immutable();
CREATE TRIGGER source_erasure_thread_tombstone_immutable
  BEFORE UPDATE OR DELETE
  ON memory_ingest_private.source_erasure_thread_tombstone
  FOR EACH ROW EXECUTE FUNCTION
    memory_ingest_private.guard_source_erasure_receipt_immutable();

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA memory_ingest_private
  FROM PUBLIC, memory_ingest_writer, memory_erasure_requester,
       governed_memory_worker;
GRANT EXECUTE ON FUNCTION memory_ingest_private.enqueue_chat_log_message(
  uuid,text
) TO memory_ingest_writer;
GRANT EXECUTE ON FUNCTION
  memory_ingest_private.begin_source_erasure(
    uuid,text,uuid,uuid,integer,text
  ),
  memory_ingest_private.read_source_erasure(uuid)
TO memory_erasure_requester;
GRANT EXECUTE ON FUNCTION
  memory_ingest_private.lease_memory_ingest(text,integer,integer),
  memory_ingest_private.read_leased_chat_log_message(uuid,uuid),
  memory_ingest_private.mark_memory_ingest_context_review(uuid,uuid),
  memory_ingest_private.ack_memory_ingest(uuid,uuid,text,uuid,uuid),
  memory_ingest_private.fail_memory_ingest(uuid,uuid,text,text,integer),
  memory_ingest_private.expire_memory_ingest(integer),
  memory_ingest_private.purge_terminal_memory_ingest(integer),
  memory_ingest_private.lease_source_erasure(text,integer),
  memory_ingest_private.read_source_erasure_targets(
    uuid,uuid,timestamptz,uuid,integer
  ),
  memory_ingest_private.release_source_erasure_lease(uuid,uuid),
  memory_ingest_private.mark_source_erasure_governed_deleted(
    uuid,uuid,text,integer,text
  ),
  memory_ingest_private.finalize_source_erasure(uuid,uuid,text),
  memory_ingest_private.ack_source_erasure_completion(uuid,uuid,text),
  memory_ingest_private.fail_source_erasure(uuid,uuid,text,text)
TO governed_memory_worker;

DO $postflight$
DECLARE
  expected_column record;
  expected_constraint record;
  forbidden_role text;
  forbidden_relation text;
  function_oid oid;
  function_signature text;
  object_definition text;
  observed_count integer;
  relation_oid oid;
  runtime_role text;
  should_execute boolean;
  should_security_definer boolean;
  privilege_name text;
BEGIN
  PERFORM memory_ingest_private.assert_chat_deletion_catalog();
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'memory_ingest_writer'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'memory_erasure_requester'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'governed_memory_api'
      AND rolcanlogin = (
        pg_catalog.current_setting(
          'governed_memory.inactive_installation'
        ) = 'off'
      )
      AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'governed_memory_worker'
      AND rolcanlogin = (
        pg_catalog.current_setting(
          'governed_memory.inactive_installation'
        ) = 'off'
      )
      AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'brains_app'
      AND rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND rolinherit
  ) OR EXISTS (
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
    RAISE EXCEPTION 'inactive bridge role or membership contract differs';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_namespace AS namespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(
        namespace.nspacl,
        pg_catalog.acldefault('n', namespace.nspowner)
      )
    ) AS acl
    WHERE namespace.nspname = 'memory_ingest_private'
      AND acl.grantee NOT IN (
        'sage'::regrole::oid,
        'memory_ingest_writer'::regrole::oid,
        'memory_erasure_requester'::regrole::oid,
        'governed_memory_worker'::regrole::oid
      )
  ) THEN
    RAISE EXCEPTION 'unexpected bridge schema ACL exists';
  END IF;
  FOREACH runtime_role IN ARRAY ARRAY[
    'memory_ingest_writer', 'memory_erasure_requester',
    'governed_memory_worker'
  ] LOOP
    IF NOT pg_catalog.has_schema_privilege(
      runtime_role, 'memory_ingest_private', 'USAGE'
    ) OR pg_catalog.has_schema_privilege(
      runtime_role, 'memory_ingest_private', 'CREATE'
    ) THEN
      RAISE EXCEPTION 'bridge schema privilege differs for %', runtime_role;
    END IF;
  END LOOP;

  SELECT pg_catalog.count(*)::integer INTO observed_count
  FROM pg_catalog.pg_class AS relation
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  WHERE namespace.nspname = 'memory_ingest_private'
    AND relation.relkind IN ('r', 'p');
  IF observed_count <> 7 THEN
    RAISE EXCEPTION 'bridge private table inventory differs';
  END IF;

  SELECT pg_catalog.count(*)::integer INTO observed_count
  FROM pg_catalog.pg_attribute AS attribute
  WHERE attribute.attrelid =
        'memory_ingest_private.source_erasure_receipt'::regclass
    AND attribute.attnum > 0
    AND NOT attribute.attisdropped;
  IF observed_count <> 18 THEN
    RAISE EXCEPTION 'source erasure receipt column inventory differs';
  END IF;
  FOR expected_column IN
    SELECT * FROM (VALUES
      ('receipt_id', 'uuid'),
      ('owner_user_id', 'uuid'),
      ('operation_id', 'uuid'),
      ('selector_sha256', 'text'),
      ('target_manifest_sha256', 'text'),
      ('target_count', 'integer'),
      ('thread_target_manifest_sha256', 'text'),
      ('thread_target_count', 'integer'),
      ('deleted_message_count', 'integer'),
      ('deleted_thread_count', 'integer'),
      ('deleted_attachment_count', 'integer'),
      ('deleted_bridge_row_count', 'integer'),
      ('message_tombstone_count', 'integer'),
      ('thread_tombstone_count', 'integer'),
      ('tombstone_manifest_sha256', 'text'),
      ('governed_receipt_sha256', 'text'),
      ('receipt_sha256', 'text'),
      ('completed_at', 'timestamp with time zone')
    ) AS expected(column_name, type_name)
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_attribute AS attribute
      WHERE attribute.attrelid =
            'memory_ingest_private.source_erasure_receipt'::regclass
        AND attribute.attname = expected_column.column_name
        AND attribute.atttypid = expected_column.type_name::regtype
        AND attribute.attnotnull
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
    ) THEN
      RAISE EXCEPTION 'source erasure receipt column differs: %',
        expected_column.column_name;
    END IF;
  END LOOP;

  SELECT pg_catalog.count(*)::integer INTO observed_count
  FROM pg_catalog.pg_attribute AS attribute
  WHERE attribute.attrelid =
        'memory_ingest_private.source_erasure_thread_target'::regclass
    AND attribute.attnum > 0
    AND NOT attribute.attisdropped;
  IF observed_count <> 5 OR EXISTS (
    SELECT 1
    FROM (VALUES
      ('owner_user_id', 'uuid'),
      ('operation_id', 'uuid'),
      ('thread_id', 'uuid'),
      ('source_created_at', 'timestamp with time zone'),
      ('target_sha256', 'text')
    ) AS expected(column_name, type_name)
    LEFT JOIN pg_catalog.pg_attribute AS attribute
      ON attribute.attrelid =
           'memory_ingest_private.source_erasure_thread_target'::regclass
     AND attribute.attname = expected.column_name
     AND attribute.atttypid = expected.type_name::regtype
     AND attribute.attnotnull
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    WHERE attribute.attnum IS NULL
  ) THEN
    RAISE EXCEPTION 'source erasure thread target columns differ';
  END IF;
  FOREACH forbidden_relation IN ARRAY ARRAY[
    'memory_ingest_private.source_erasure_message_tombstone',
    'memory_ingest_private.source_erasure_thread_tombstone'
  ] LOOP
    relation_oid := pg_catalog.to_regclass(forbidden_relation);
    SELECT pg_catalog.count(*)::integer INTO observed_count
    FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = relation_oid
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped;
    IF observed_count <> 4 OR EXISTS (
      SELECT 1
      FROM (VALUES
        (CASE WHEN forbidden_relation LIKE '%message%'
           THEN 'message_id' ELSE 'thread_id' END, 'uuid'),
        ('owner_user_id', 'uuid'),
        ('operation_id', 'uuid'),
        ('erased_at', 'timestamp with time zone')
      ) AS expected(column_name, type_name)
      LEFT JOIN pg_catalog.pg_attribute AS attribute
        ON attribute.attrelid = relation_oid
       AND attribute.attname = expected.column_name
       AND attribute.atttypid = expected.type_name::regtype
       AND attribute.attnotnull
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
      WHERE attribute.attnum IS NULL
    ) THEN
      RAISE EXCEPTION 'suppression tombstone columns differ: %',
        forbidden_relation;
    END IF;
  END LOOP;

  FOREACH forbidden_relation IN ARRAY ARRAY[
    'memory_ingest_private.memory_ingest_outbox',
    'memory_ingest_private.source_erasure_operation',
    'memory_ingest_private.source_erasure_target',
    'memory_ingest_private.source_erasure_thread_target',
    'memory_ingest_private.source_erasure_message_tombstone',
    'memory_ingest_private.source_erasure_thread_tombstone',
    'memory_ingest_private.source_erasure_receipt'
  ] LOOP
    relation_oid := pg_catalog.to_regclass(forbidden_relation);
    IF relation_oid IS NULL OR NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_class AS relation
      WHERE relation.oid = relation_oid
        AND relation.relkind = 'r'
        AND relation.relrowsecurity
        AND relation.relforcerowsecurity
        AND relation.relowner = 'sage'::regrole
    ) THEN
      RAISE EXCEPTION 'bridge table owner or forced RLS differs: %',
        forbidden_relation;
    END IF;
    SELECT pg_catalog.count(*)::integer INTO observed_count
    FROM pg_catalog.pg_policy AS policy
    WHERE policy.polrelid = relation_oid;
    IF observed_count <> 1 OR NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_policy AS policy
      WHERE policy.polrelid = relation_oid
        AND policy.polname = 'owner_internal'
        AND policy.polcmd = '*'
        AND policy.polpermissive
        AND policy.polroles = ARRAY['sage'::regrole::oid]::oid[]
        AND pg_catalog.pg_get_expr(
              policy.polqual, policy.polrelid, true
            ) = 'true'
        AND pg_catalog.pg_get_expr(
              policy.polwithcheck, policy.polrelid, true
            ) = 'true'
    ) THEN
      RAISE EXCEPTION 'bridge table RLS policy differs: %', forbidden_relation;
    END IF;
    IF EXISTS (
      SELECT 1
      FROM pg_catalog.pg_class AS relation
      CROSS JOIN LATERAL pg_catalog.aclexplode(
        COALESCE(
          relation.relacl,
          pg_catalog.acldefault('r', relation.relowner)
        )
      ) AS acl
      WHERE relation.oid = relation_oid
        AND acl.grantee <> 'sage'::regrole::oid
    ) THEN
      RAISE EXCEPTION 'direct bridge table ACL exists: %', forbidden_relation;
    END IF;
  END LOOP;

  FOR expected_constraint IN
    SELECT * FROM (VALUES
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_pkey', 'p'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_owner_id', 'u'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_operation_unique', 'u'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_message_unique', 'u'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_owner_nonzero', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_hashes', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_cutover', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_window', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_context_review', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_state', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_decision', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_attempts', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_lease_shape', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_terminal_shape', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_result_shape', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_error_size', 'c'),
      ('memory_ingest_private.memory_ingest_outbox',
       'memory_ingest_outbox_error_semantics', 'c'),
      ('memory_ingest_private.source_erasure_operation',
       'source_erasure_operation_pkey', 'p'),
      ('memory_ingest_private.source_erasure_operation',
       'source_erasure_operation_owner_id', 'u'),
      ('memory_ingest_private.source_erasure_operation',
       'source_erasure_operation_owner_nonzero', 'c'),
      ('memory_ingest_private.source_erasure_operation',
       'source_erasure_operation_selector', 'c'),
      ('memory_ingest_private.source_erasure_operation',
       'source_erasure_operation_hashes', 'c'),
      ('memory_ingest_private.source_erasure_operation',
       'source_erasure_operation_state', 'c'),
      ('memory_ingest_private.source_erasure_operation',
       'source_erasure_operation_counts', 'c'),
      ('memory_ingest_private.source_erasure_operation',
       'source_erasure_operation_lease', 'c'),
      ('memory_ingest_private.source_erasure_operation',
       'source_erasure_operation_completion', 'c'),
      ('memory_ingest_private.source_erasure_target',
       'source_erasure_target_pkey', 'p'),
      ('memory_ingest_private.source_erasure_target',
       'source_erasure_target_operation_fk', 'f'),
      ('memory_ingest_private.source_erasure_target',
       'source_erasure_target_hash', 'c'),
      ('memory_ingest_private.source_erasure_thread_target',
       'source_erasure_thread_target_pkey', 'p'),
      ('memory_ingest_private.source_erasure_thread_target',
       'source_erasure_thread_target_operation_fk', 'f'),
      ('memory_ingest_private.source_erasure_thread_target',
       'source_erasure_thread_target_hash', 'c'),
      ('memory_ingest_private.source_erasure_message_tombstone',
       'source_erasure_message_tombstone_pkey', 'p'),
      ('memory_ingest_private.source_erasure_message_tombstone',
       'source_erasure_message_tombstone_operation_fk', 'f'),
      ('memory_ingest_private.source_erasure_thread_tombstone',
       'source_erasure_thread_tombstone_pkey', 'p'),
      ('memory_ingest_private.source_erasure_thread_tombstone',
       'source_erasure_thread_tombstone_operation_fk', 'f'),
      ('memory_ingest_private.source_erasure_receipt',
       'source_erasure_receipt_pkey', 'p'),
      ('memory_ingest_private.source_erasure_receipt',
       'source_erasure_receipt_operation_unique', 'u'),
      ('memory_ingest_private.source_erasure_receipt',
       'source_erasure_receipt_hashes', 'c'),
      ('memory_ingest_private.source_erasure_receipt',
       'source_erasure_receipt_counts', 'c')
    ) AS expected(table_name, constraint_name, constraint_type)
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_constraint AS constraint_row
      WHERE constraint_row.conrelid = pg_catalog.to_regclass(
              expected_constraint.table_name
            )
        AND constraint_row.conname = expected_constraint.constraint_name
        AND constraint_row.contype = expected_constraint.constraint_type::"char"
        AND constraint_row.convalidated
    ) THEN
      RAISE EXCEPTION 'bridge constraint differs: %.%',
        expected_constraint.table_name,
        expected_constraint.constraint_name;
    END IF;
  END LOOP;
  SELECT pg_catalog.count(*)::integer INTO observed_count
  FROM pg_catalog.pg_constraint AS constraint_row
  WHERE constraint_row.conrelid IN (
    'memory_ingest_private.memory_ingest_outbox'::regclass,
    'memory_ingest_private.source_erasure_operation'::regclass,
    'memory_ingest_private.source_erasure_target'::regclass,
    'memory_ingest_private.source_erasure_thread_target'::regclass,
    'memory_ingest_private.source_erasure_message_tombstone'::regclass,
    'memory_ingest_private.source_erasure_thread_tombstone'::regclass,
    'memory_ingest_private.source_erasure_receipt'::regclass
  );
  IF observed_count <> 40 THEN
    RAISE EXCEPTION 'bridge constraint inventory differs';
  END IF;

  SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
  INTO object_definition
  FROM pg_catalog.pg_constraint AS constraint_row
  WHERE constraint_row.conrelid =
        'memory_ingest_private.source_erasure_operation'::regclass
    AND constraint_row.conname = 'source_erasure_operation_selector';
  IF object_definition IS NULL
     OR pg_catalog.strpos(object_definition, 'thread') = 0
     OR pg_catalog.strpos(object_definition, 'message_tail') = 0
     OR pg_catalog.strpos(object_definition, 'recent') = 0
     OR pg_catalog.strpos(object_definition, 'all_conversations') = 0 THEN
    RAISE EXCEPTION 'source erasure selector constraint is not chat-only';
  END IF;
  SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
  INTO object_definition
  FROM pg_catalog.pg_constraint AS constraint_row
  WHERE constraint_row.conrelid =
        'memory_ingest_private.memory_ingest_outbox'::regclass
    AND constraint_row.conname = 'memory_ingest_outbox_state';
  IF object_definition IS NULL
     OR pg_catalog.strpos(object_definition, 'erasure_cancelled') = 0 THEN
    RAISE EXCEPTION 'bridge erasure-cancelled state constraint differs';
  END IF;
  SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
  INTO object_definition
  FROM pg_catalog.pg_constraint AS constraint_row
  WHERE constraint_row.conrelid =
        'memory_ingest_private.source_erasure_operation'::regclass
    AND constraint_row.conname = 'source_erasure_operation_state';
  IF object_definition IS NULL OR pg_catalog.strpos(
       object_definition, 'conversation_deleted_pending_ack'
     ) = 0 THEN
    RAISE EXCEPTION 'source erasure pending-ack state constraint differs';
  END IF;
  SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
  INTO object_definition
  FROM pg_catalog.pg_constraint AS constraint_row
  WHERE constraint_row.conrelid =
        'memory_ingest_private.source_erasure_receipt'::regclass
    AND constraint_row.conname = 'source_erasure_receipt_counts';
  IF object_definition IS NULL
     OR pg_catalog.strpos(
          object_definition, 'deleted_message_count'
        ) = 0
     OR pg_catalog.strpos(
          object_definition, 'deleted_thread_count'
        ) = 0
     OR pg_catalog.strpos(
          object_definition, 'deleted_attachment_count'
        ) = 0
     OR pg_catalog.strpos(
          object_definition, 'deleted_bridge_row_count'
        ) = 0
     OR pg_catalog.strpos(
          object_definition, 'thread_target_count'
        ) = 0
     OR pg_catalog.strpos(
          object_definition, 'message_tombstone_count'
        ) = 0
     OR pg_catalog.strpos(
          object_definition, 'thread_tombstone_count'
        ) = 0 THEN
    RAISE EXCEPTION 'source erasure receipt count constraint differs';
  END IF;
  SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
  INTO object_definition
  FROM pg_catalog.pg_constraint AS constraint_row
  WHERE constraint_row.conrelid =
        'memory_ingest_private.source_erasure_receipt'::regclass
    AND constraint_row.conname = 'source_erasure_receipt_hashes';
  IF object_definition IS NULL
     OR pg_catalog.strpos(
          object_definition, 'thread_target_manifest_sha256'
        ) = 0
     OR pg_catalog.strpos(
          object_definition, 'tombstone_manifest_sha256'
        ) = 0 THEN
    RAISE EXCEPTION 'source erasure receipt hash constraint differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_target'::regclass
      AND constraint_row.conname = 'source_erasure_target_operation_fk'
      AND constraint_row.confrelid =
          'memory_ingest_private.source_erasure_operation'::regclass
      AND constraint_row.confdeltype = 'r'
      AND constraint_row.convalidated
  ) THEN
    RAISE EXCEPTION 'source erasure target ownership fence differs';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM (VALUES
      ('memory_ingest_private.source_erasure_thread_target'::regclass::oid,
       'source_erasure_thread_target_operation_fk'::text),
      ('memory_ingest_private.source_erasure_message_tombstone'::regclass::oid,
       'source_erasure_message_tombstone_operation_fk'::text),
      ('memory_ingest_private.source_erasure_thread_tombstone'::regclass::oid,
       'source_erasure_thread_tombstone_operation_fk'::text)
    ) AS expected(relation_oid, constraint_name)
    LEFT JOIN pg_catalog.pg_constraint AS constraint_row
      ON constraint_row.conrelid = expected.relation_oid
     AND constraint_row.conname = expected.constraint_name
     AND constraint_row.confrelid =
           'memory_ingest_private.source_erasure_operation'::regclass
     AND constraint_row.confdeltype = 'r'
     AND constraint_row.confupdtype = 'a'
     AND constraint_row.convalidated
     AND NOT constraint_row.condeferrable
     AND NOT constraint_row.condeferred
    WHERE constraint_row.oid IS NULL
  ) THEN
    RAISE EXCEPTION 'source erasure child ownership fence differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_message_tombstone'::regclass
      AND constraint_row.conname =
          'source_erasure_message_tombstone_pkey'
      AND pg_catalog.pg_get_constraintdef(constraint_row.oid, true) =
          'PRIMARY KEY (message_id)'
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_constraint AS constraint_row
    WHERE constraint_row.conrelid =
          'memory_ingest_private.source_erasure_thread_tombstone'::regclass
      AND constraint_row.conname =
          'source_erasure_thread_tombstone_pkey'
      AND pg_catalog.pg_get_constraintdef(constraint_row.oid, true) =
          'PRIMARY KEY (thread_id)'
  ) THEN
    RAISE EXCEPTION 'suppression tombstone global identity differs';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_index AS index_row
    WHERE index_row.indexrelid = pg_catalog.to_regclass(
            'memory_ingest_private.source_erasure_one_active_owner_idx'
          )
      AND index_row.indrelid =
          'memory_ingest_private.source_erasure_operation'::regclass
      AND index_row.indisunique
      AND index_row.indisvalid
      AND index_row.indisready
      AND index_row.indnkeyatts = 1
      AND index_row.indexprs IS NULL
      AND pg_catalog.pg_get_indexdef(
            index_row.indexrelid, 1, true
          ) = 'owner_user_id'
      AND pg_catalog.pg_get_expr(
            index_row.indpred, index_row.indrelid, true
          ) IN (
            'state <> ''completed''::text',
            '(state <> ''completed''::text)'
          )
  ) THEN
    RAISE EXCEPTION 'source erasure active-owner index differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_index AS index_row
    WHERE index_row.indexrelid = pg_catalog.to_regclass(
            'memory_ingest_private.source_erasure_target_page_idx'
          )
      AND index_row.indrelid =
          'memory_ingest_private.source_erasure_target'::regclass
      AND NOT index_row.indisunique
      AND index_row.indisvalid
      AND index_row.indisready
      AND index_row.indnkeyatts = 4
      AND index_row.indpred IS NULL
      AND index_row.indexprs IS NULL
      AND pg_catalog.pg_get_indexdef(
            index_row.indexrelid, 1, true
          ) = 'owner_user_id'
      AND pg_catalog.pg_get_indexdef(
            index_row.indexrelid, 2, true
          ) = 'operation_id'
      AND pg_catalog.pg_get_indexdef(
            index_row.indexrelid, 3, true
          ) = 'source_created_at'
      AND pg_catalog.pg_get_indexdef(
            index_row.indexrelid, 4, true
          ) = 'message_id'
  ) THEN
    RAISE EXCEPTION 'source erasure target page index differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_index AS index_row
    WHERE index_row.indexrelid = pg_catalog.to_regclass(
            'memory_ingest_private.source_erasure_thread_target_page_idx'
          )
      AND index_row.indrelid =
          'memory_ingest_private.source_erasure_thread_target'::regclass
      AND NOT index_row.indisunique
      AND index_row.indisvalid
      AND index_row.indisready
      AND index_row.indnkeyatts = 4
      AND index_row.indpred IS NULL
      AND index_row.indexprs IS NULL
      AND pg_catalog.pg_get_indexdef(
            index_row.indexrelid, 1, true
          ) = 'owner_user_id'
      AND pg_catalog.pg_get_indexdef(
            index_row.indexrelid, 2, true
          ) = 'operation_id'
      AND pg_catalog.pg_get_indexdef(
            index_row.indexrelid, 3, true
          ) = 'source_created_at'
      AND pg_catalog.pg_get_indexdef(
            index_row.indexrelid, 4, true
          ) = 'thread_id'
  ) THEN
    RAISE EXCEPTION 'source erasure thread target page index differs';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE trigger_row.tgrelid = 'public.chat_log'::regclass
      AND trigger_row.tgname = 'chat_log_serialize_source_erasure'
      AND trigger_row.tgenabled = 'O'
      AND NOT trigger_row.tgisinternal
      AND trigger_row.tgtype = 7
      AND trigger_row.tgfoid =
          'memory_ingest_private.serialize_chat_source_erasure()'::regprocedure
  ) THEN
    RAISE EXCEPTION 'chat source erasure serialization trigger differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE trigger_row.tgrelid = 'public.threads'::regclass
      AND trigger_row.tgname = 'threads_serialize_source_erasure'
      AND trigger_row.tgenabled = 'O'
      AND NOT trigger_row.tgisinternal
      AND trigger_row.tgtype = 23
      AND trigger_row.tgfoid =
          'memory_ingest_private.serialize_thread_source_erasure()'::regprocedure
  ) THEN
    RAISE EXCEPTION 'thread source erasure serialization trigger differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE trigger_row.tgrelid = 'public.chat_attachments'::regclass
      AND trigger_row.tgname = 'chat_attachments_serialize_source_erasure'
      AND trigger_row.tgenabled = 'O'
      AND NOT trigger_row.tgisinternal
      AND trigger_row.tgtype = 23
      AND trigger_row.tgfoid =
          'memory_ingest_private.serialize_attachment_source_erasure()'::regprocedure
  ) THEN
    RAISE EXCEPTION 'attachment source erasure serialization trigger differs';
  END IF;
  IF pg_catalog.to_regclass('trusted_web.response_transcript_v1') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1
       FROM pg_catalog.pg_trigger AS trigger_row
       WHERE trigger_row.tgrelid =
             pg_catalog.to_regclass('trusted_web.response_transcript_v1')
         AND trigger_row.tgname =
             'response_transcript_serialize_source_erasure'
         AND trigger_row.tgenabled = 'O'
         AND NOT trigger_row.tgisinternal
         AND trigger_row.tgtype = 23
         AND trigger_row.tgfoid =
             'memory_ingest_private.serialize_response_transcript_source_erasure()'::regprocedure
     ) THEN
    RAISE EXCEPTION 'response transcript serialization trigger differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE trigger_row.tgrelid =
          'memory_ingest_private.source_erasure_receipt'::regclass
      AND trigger_row.tgname = 'source_erasure_receipt_immutable'
      AND trigger_row.tgenabled = 'O'
      AND NOT trigger_row.tgisinternal
      AND trigger_row.tgtype = 27
      AND trigger_row.tgfoid =
          'memory_ingest_private.guard_source_erasure_receipt_immutable()'::regprocedure
  ) THEN
    RAISE EXCEPTION 'source erasure receipt immutability trigger differs';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE trigger_row.tgrelid =
          'memory_ingest_private.source_erasure_message_tombstone'::regclass
      AND trigger_row.tgname =
          'source_erasure_message_tombstone_immutable'
      AND trigger_row.tgenabled = 'O'
      AND NOT trigger_row.tgisinternal
      AND trigger_row.tgtype = 27
      AND trigger_row.tgfoid =
          'memory_ingest_private.guard_source_erasure_receipt_immutable()'::regprocedure
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger AS trigger_row
    WHERE trigger_row.tgrelid =
          'memory_ingest_private.source_erasure_thread_tombstone'::regclass
      AND trigger_row.tgname =
          'source_erasure_thread_tombstone_immutable'
      AND trigger_row.tgenabled = 'O'
      AND NOT trigger_row.tgisinternal
      AND trigger_row.tgtype = 27
      AND trigger_row.tgfoid =
          'memory_ingest_private.guard_source_erasure_receipt_immutable()'::regprocedure
  ) THEN
    RAISE EXCEPTION 'suppression tombstone immutability trigger differs';
  END IF;
  SELECT pg_catalog.count(*)::integer INTO observed_count
  FROM pg_catalog.pg_trigger AS trigger_row
  WHERE NOT trigger_row.tgisinternal
    AND (
      (trigger_row.tgrelid = 'public.chat_log'::regclass
        AND trigger_row.tgname = 'chat_log_serialize_source_erasure')
      OR
      (trigger_row.tgrelid = 'public.threads'::regclass
        AND trigger_row.tgname = 'threads_serialize_source_erasure')
      OR
      (trigger_row.tgrelid = 'public.chat_attachments'::regclass
        AND trigger_row.tgname = 'chat_attachments_serialize_source_erasure')
      OR
      (trigger_row.tgrelid =
          pg_catalog.to_regclass('trusted_web.response_transcript_v1')
        AND trigger_row.tgname =
          'response_transcript_serialize_source_erasure')
      OR
      (trigger_row.tgrelid =
          'memory_ingest_private.source_erasure_receipt'::regclass
        AND trigger_row.tgname = 'source_erasure_receipt_immutable')
      OR
      (trigger_row.tgrelid =
          'memory_ingest_private.source_erasure_message_tombstone'::regclass
        AND trigger_row.tgname =
          'source_erasure_message_tombstone_immutable')
      OR
      (trigger_row.tgrelid =
          'memory_ingest_private.source_erasure_thread_tombstone'::regclass
        AND trigger_row.tgname =
          'source_erasure_thread_tombstone_immutable')
    );
  IF observed_count <> 6 + (
       CASE WHEN pg_catalog.to_regclass(
         'trusted_web.response_transcript_v1'
       ) IS NULL THEN 0 ELSE 1 END
     ) THEN
    RAISE EXCEPTION 'source erasure trigger inventory differs';
  END IF;

  FOREACH runtime_role IN ARRAY ARRAY[
    'memory_ingest_writer', 'memory_erasure_requester',
    'governed_memory_worker'
  ] LOOP
    FOREACH forbidden_relation IN ARRAY ARRAY[
      'memory_ingest_private.memory_ingest_outbox',
      'memory_ingest_private.source_erasure_operation',
      'memory_ingest_private.source_erasure_target',
      'memory_ingest_private.source_erasure_thread_target',
      'memory_ingest_private.source_erasure_message_tombstone',
      'memory_ingest_private.source_erasure_thread_tombstone',
      'memory_ingest_private.source_erasure_receipt',
      'public.chat_log', 'public.threads', 'public.chat_attachments'
    ] LOOP
      FOREACH privilege_name IN ARRAY ARRAY[
        'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
        'REFERENCES', 'TRIGGER'
      ] LOOP
        IF pg_catalog.has_table_privilege(
          runtime_role, forbidden_relation, privilege_name
        ) THEN
          RAISE EXCEPTION 'bridge role % has direct authority on %',
            runtime_role, forbidden_relation;
        END IF;
      END LOOP;
    END LOOP;
  END LOOP;

  SELECT pg_catalog.count(*)::integer INTO observed_count
  FROM pg_catalog.pg_proc AS routine
  WHERE routine.pronamespace =
        pg_catalog.to_regnamespace('memory_ingest_private')
    AND routine.prokind = 'f';
  IF observed_count <> 30 THEN
    RAISE EXCEPTION 'bridge function inventory differs';
  END IF;

  FOREACH function_signature IN ARRAY ARRAY[
    'memory_ingest_private.framed_utf8_field(text,text)',
    'memory_ingest_private.deletion_confirmation_sha256(uuid,text,uuid,uuid,integer)',
    'memory_ingest_private.timestamp_utc_text(timestamptz)',
    'memory_ingest_private.assert_chat_deletion_catalog()',
    'memory_ingest_private.ingest_window_sha256(uuid,uuid,uuid,uuid,uuid,text)',
    'memory_ingest_private.source_binding_sha256(uuid,uuid,uuid,uuid,uuid,integer,text,text,text,timestamptz)',
    'memory_ingest_private.terminal_receipt_sha256(text,text,text,integer,uuid,uuid,text,timestamptz)',
    'memory_ingest_private.source_erasure_target_sha256(uuid,uuid,uuid,uuid,timestamptz)',
    'memory_ingest_private.enqueue_chat_log_message(uuid,text)',
    'memory_ingest_private.lease_memory_ingest(text,integer,integer)',
    'memory_ingest_private.read_leased_chat_log_message(uuid,uuid)',
    'memory_ingest_private.mark_memory_ingest_context_review(uuid,uuid)',
    'memory_ingest_private.ack_memory_ingest(uuid,uuid,text,uuid,uuid)',
    'memory_ingest_private.fail_memory_ingest(uuid,uuid,text,text,integer)',
    'memory_ingest_private.expire_memory_ingest(integer)',
    'memory_ingest_private.purge_terminal_memory_ingest(integer)',
    'memory_ingest_private.begin_source_erasure(uuid,text,uuid,uuid,integer,text)',
    'memory_ingest_private.read_source_erasure(uuid)',
    'memory_ingest_private.lease_source_erasure(text,integer)',
    'memory_ingest_private.read_source_erasure_targets(uuid,uuid,timestamptz,uuid,integer)',
    'memory_ingest_private.release_source_erasure_lease(uuid,uuid)',
    'memory_ingest_private.mark_source_erasure_governed_deleted(uuid,uuid,text,integer,text)',
    'memory_ingest_private.finalize_source_erasure(uuid,uuid,text)',
    'memory_ingest_private.ack_source_erasure_completion(uuid,uuid,text)',
    'memory_ingest_private.fail_source_erasure(uuid,uuid,text,text)',
    'memory_ingest_private.serialize_chat_source_erasure()',
    'memory_ingest_private.serialize_thread_source_erasure()',
    'memory_ingest_private.serialize_attachment_source_erasure()',
    'memory_ingest_private.serialize_response_transcript_source_erasure()',
    'memory_ingest_private.guard_source_erasure_receipt_immutable()'
  ] LOOP
    function_oid := pg_catalog.to_regprocedure(function_signature);
    should_security_definer := function_signature NOT IN (
      'memory_ingest_private.framed_utf8_field(text,text)',
      'memory_ingest_private.deletion_confirmation_sha256(uuid,text,uuid,uuid,integer)',
      'memory_ingest_private.timestamp_utc_text(timestamptz)',
      'memory_ingest_private.ingest_window_sha256(uuid,uuid,uuid,uuid,uuid,text)',
      'memory_ingest_private.source_binding_sha256(uuid,uuid,uuid,uuid,uuid,integer,text,text,text,timestamptz)',
      'memory_ingest_private.terminal_receipt_sha256(text,text,text,integer,uuid,uuid,text,timestamptz)',
      'memory_ingest_private.source_erasure_target_sha256(uuid,uuid,uuid,uuid,timestamptz)'
    );
    IF function_oid IS NULL OR NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_proc AS routine
      JOIN pg_catalog.pg_language AS language
        ON language.oid = routine.prolang
      WHERE routine.oid = function_oid
        AND routine.prokind = 'f'
        AND routine.proowner = 'sage'::regrole
        AND routine.prosecdef = should_security_definer
        AND routine.proconfig = ARRAY['search_path=pg_catalog']::text[]
        AND language.lanname = CASE WHEN should_security_definer
              THEN 'plpgsql' ELSE 'sql' END
    ) THEN
      RAISE EXCEPTION 'bridge function definition differs: %',
        function_signature;
    END IF;
    IF EXISTS (
      SELECT 1
      FROM pg_catalog.pg_proc AS routine
      CROSS JOIN LATERAL pg_catalog.aclexplode(
        COALESCE(
          routine.proacl,
          pg_catalog.acldefault('f', routine.proowner)
        )
      ) AS acl
      WHERE routine.oid = function_oid
        AND acl.privilege_type = 'EXECUTE'
        AND acl.grantee NOT IN (
          'sage'::regrole::oid,
          'memory_ingest_writer'::regrole::oid,
          'memory_erasure_requester'::regrole::oid,
          'governed_memory_worker'::regrole::oid
        )
    ) THEN
      RAISE EXCEPTION 'unexpected bridge function ACL exists: %',
        function_signature;
    END IF;
    FOREACH runtime_role IN ARRAY ARRAY[
      'brains_app', 'governed_memory_api', 'memory_ingest_writer',
      'memory_erasure_requester', 'governed_memory_worker'
    ] LOOP
      should_execute :=
        (runtime_role = 'memory_ingest_writer' AND function_signature =
          'memory_ingest_private.enqueue_chat_log_message(uuid,text)')
        OR
        (runtime_role = 'memory_erasure_requester' AND function_signature IN (
          'memory_ingest_private.begin_source_erasure(uuid,text,uuid,uuid,integer,text)',
          'memory_ingest_private.read_source_erasure(uuid)'
        ))
        OR
        (runtime_role = 'governed_memory_worker' AND function_signature IN (
          'memory_ingest_private.lease_memory_ingest(text,integer,integer)',
          'memory_ingest_private.read_leased_chat_log_message(uuid,uuid)',
          'memory_ingest_private.mark_memory_ingest_context_review(uuid,uuid)',
          'memory_ingest_private.ack_memory_ingest(uuid,uuid,text,uuid,uuid)',
          'memory_ingest_private.fail_memory_ingest(uuid,uuid,text,text,integer)',
          'memory_ingest_private.expire_memory_ingest(integer)',
          'memory_ingest_private.purge_terminal_memory_ingest(integer)',
          'memory_ingest_private.lease_source_erasure(text,integer)',
          'memory_ingest_private.read_source_erasure_targets(uuid,uuid,timestamptz,uuid,integer)',
          'memory_ingest_private.release_source_erasure_lease(uuid,uuid)',
          'memory_ingest_private.mark_source_erasure_governed_deleted(uuid,uuid,text,integer,text)',
          'memory_ingest_private.finalize_source_erasure(uuid,uuid,text)',
          'memory_ingest_private.ack_source_erasure_completion(uuid,uuid,text)',
          'memory_ingest_private.fail_source_erasure(uuid,uuid,text,text)'
        ));
      IF pg_catalog.has_function_privilege(
           runtime_role, function_oid, 'EXECUTE'
         ) IS DISTINCT FROM should_execute THEN
        RAISE EXCEPTION 'bridge function privilege differs: % %',
          runtime_role, function_signature;
      END IF;
    END LOOP;
  END LOOP;

  FOREACH forbidden_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF pg_catalog.to_regrole(forbidden_role) IS NOT NULL THEN
      IF pg_catalog.has_schema_privilege(
        forbidden_role, 'memory_ingest_private', 'USAGE'
      ) THEN
        RAISE EXCEPTION 'forbidden role % can use bridge schema', forbidden_role;
      END IF;
      IF pg_catalog.has_table_privilege(
        forbidden_role,
        'memory_ingest_private.memory_ingest_outbox', 'SELECT'
      ) OR pg_catalog.has_table_privilege(
        forbidden_role,
        'memory_ingest_private.memory_ingest_outbox', 'INSERT'
      ) OR pg_catalog.has_table_privilege(
        forbidden_role,
        'memory_ingest_private.memory_ingest_outbox', 'UPDATE'
      ) OR pg_catalog.has_table_privilege(
        forbidden_role,
        'memory_ingest_private.memory_ingest_outbox', 'DELETE'
      ) THEN
        RAISE EXCEPTION 'forbidden role % can access bridge outbox',
          forbidden_role;
      END IF;
      FOREACH forbidden_relation IN ARRAY ARRAY[
        'memory_ingest_private.memory_ingest_outbox',
        'memory_ingest_private.source_erasure_operation',
        'memory_ingest_private.source_erasure_target',
        'memory_ingest_private.source_erasure_thread_target',
        'memory_ingest_private.source_erasure_message_tombstone',
        'memory_ingest_private.source_erasure_thread_tombstone',
        'memory_ingest_private.source_erasure_receipt'
      ] LOOP
        FOREACH privilege_name IN ARRAY ARRAY[
          'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
          'REFERENCES', 'TRIGGER'
        ] LOOP
          IF pg_catalog.has_table_privilege(
            forbidden_role, forbidden_relation, privilege_name
          ) THEN
            RAISE EXCEPTION 'forbidden role % can access %',
              forbidden_role, forbidden_relation;
          END IF;
        END LOOP;
      END LOOP;
      FOREACH function_signature IN ARRAY ARRAY[
        'memory_ingest_private.enqueue_chat_log_message(uuid,text)',
        'memory_ingest_private.begin_source_erasure(uuid,text,uuid,uuid,integer,text)',
        'memory_ingest_private.read_source_erasure(uuid)',
        'memory_ingest_private.lease_source_erasure(text,integer)',
        'memory_ingest_private.read_source_erasure_targets(uuid,uuid,timestamptz,uuid,integer)',
        'memory_ingest_private.finalize_source_erasure(uuid,uuid,text)',
        'memory_ingest_private.ack_source_erasure_completion(uuid,uuid,text)'
      ] LOOP
        IF pg_catalog.has_function_privilege(
          forbidden_role, function_signature, 'EXECUTE'
        ) THEN
          RAISE EXCEPTION 'forbidden role % can execute %',
            forbidden_role, function_signature;
        END IF;
      END LOOP;
    END IF;
  END LOOP;
  IF EXISTS (
    SELECT 1 FROM pg_catalog.pg_trigger
    WHERE tgrelid = 'public.chat_log'::regclass
      AND tgname = 'chat_log_enqueue_memory_v1_consolidation'
      AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'legacy chat capture trigger remains after migration';
  END IF;
END;
$postflight$;
