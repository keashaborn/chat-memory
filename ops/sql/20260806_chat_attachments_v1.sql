BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS threads_id_owner_user_id_chat_attachments_uq
    ON public.threads(id,owner_user_id);

CREATE UNIQUE INDEX IF NOT EXISTS chat_log_id_owner_thread_chat_attachments_uq
    ON public.chat_log(id,owner_user_id,thread_id);

CREATE TABLE public.chat_attachments (
    id uuid PRIMARY KEY,
    owner_user_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    message_id uuid,
    filename text NOT NULL,
    media_type text NOT NULL,
    content text,
    content_sha256 text NOT NULL,
    byte_size integer NOT NULL,
    status text NOT NULL DEFAULT 'ready',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CONSTRAINT chat_attachments_filename_ck
        CHECK (
            length(filename) BETWEEN 1 AND 160
            AND filename !~ E'[/\\\\\\x00\\r\\n]'
        ),
    CONSTRAINT chat_attachments_media_type_ck
        CHECK (media_type IN ('text/plain','text/markdown')),
    CONSTRAINT chat_attachments_sha256_ck
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chat_attachments_byte_size_ck
        CHECK (byte_size BETWEEN 1 AND 49152),
    CONSTRAINT chat_attachments_status_ck
        CHECK (status IN ('ready','error','deleted')),
    CONSTRAINT chat_attachments_content_state_ck
        CHECK (
            (status IN ('ready','error') AND deleted_at IS NULL AND content IS NOT NULL
             AND octet_length(content)=byte_size)
            OR
            (status='deleted' AND deleted_at IS NOT NULL AND content IS NULL)
        ),
    CONSTRAINT chat_attachments_thread_owner_fk
        FOREIGN KEY(thread_id,owner_user_id)
        REFERENCES public.threads(id,owner_user_id)
        ON DELETE CASCADE,
    CONSTRAINT chat_attachments_message_owner_thread_fk
        FOREIGN KEY(message_id,owner_user_id,thread_id)
        REFERENCES public.chat_log(id,owner_user_id,thread_id)
        ON DELETE CASCADE
);

CREATE INDEX chat_attachments_owner_thread_created_idx
    ON public.chat_attachments(owner_user_id,thread_id,created_at,id);

CREATE INDEX chat_attachments_owner_message_idx
    ON public.chat_attachments(owner_user_id,message_id)
    WHERE message_id IS NOT NULL;

ALTER TABLE public.chat_attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_attachments FORCE ROW LEVEL SECURITY;

CREATE POLICY chat_attachments_owner_isolation
    ON public.chat_attachments
    AS PERMISSIVE
    FOR ALL
    TO PUBLIC
    USING (owner_user_id=memory.current_actor_user_id())
    WITH CHECK (owner_user_id=memory.current_actor_user_id());

DO $owner$
DECLARE
    target_owner name;
BEGIN
    SELECT tableowner INTO target_owner
    FROM pg_catalog.pg_tables
    WHERE schemaname='public' AND tablename='chat_log';
    IF target_owner IS NULL THEN
        RAISE EXCEPTION 'chat_log owner unavailable';
    END IF;
    EXECUTE format('ALTER TABLE public.chat_attachments OWNER TO %I',target_owner);
END
$owner$;

COMMIT;
