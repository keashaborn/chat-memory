ALTER TABLE public.chat_attachments
  DROP CONSTRAINT chat_attachments_byte_size_ck;
ALTER TABLE public.chat_attachments
  ADD CONSTRAINT chat_attachments_byte_size_ck
  CHECK (byte_size >= 1 AND byte_size <= 73728);
