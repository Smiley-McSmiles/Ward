-- Performance indexes for large histories: paginated loading, chat previews, outbox scans,
-- receipt updates, unread counts, date search, and cascade message_edits deletes.

CREATE INDEX IF NOT EXISTS messages_chat_time_id
    ON messages (chat_id, timestamp DESC, id DESC);

CREATE INDEX IF NOT EXISTS messages_chat_preview
    ON messages (chat_id, id DESC)
    WHERE retracted = 0 AND kind IN ('text', 'me', 'file');

CREATE INDEX IF NOT EXISTS messages_outbox
    ON messages (chat_id, timestamp, id)
    WHERE direction = 1 AND status = 0;

CREATE INDEX IF NOT EXISTS messages_msg_id_only
    ON messages (msg_id)
    WHERE direction = 1 AND msg_id IS NOT NULL AND msg_id != '';

CREATE INDEX IF NOT EXISTS messages_chat_unread
    ON messages (chat_id, id)
    WHERE direction = 0 AND retracted = 0 AND kind != 'system';

CREATE INDEX IF NOT EXISTS messages_timestamp
    ON messages (timestamp DESC, id DESC);

CREATE INDEX IF NOT EXISTS message_edits_message
    ON message_edits (message_id);
