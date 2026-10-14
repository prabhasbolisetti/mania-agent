-- Run in the Supabase SQL editor after checking that existing non-NULL IDs are unique.
-- The partial index permits any number of NULL IDs (outbound messages / old records).
CREATE UNIQUE INDEX IF NOT EXISTS messages_whatsapp_message_id_unique
ON public.messages (whatsapp_message_id)
WHERE whatsapp_message_id IS NOT NULL;
