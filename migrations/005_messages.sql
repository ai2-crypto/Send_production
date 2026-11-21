-- Messages read from Zalo, kept so a page refresh does not forget who interacted with the OA.
-- Each load adds to what is kept, so someone who wrote long ago stays known even when Zalo's
-- recent list no longer shows them. message_json is the parsed message, not the raw response.
CREATE TABLE IF NOT EXISTS messages (
    msg_id TEXT PRIMARY KEY,
    zalo_user_id TEXT NOT NULL,
    direction INTEGER NOT NULL,
    sent_at_ms INTEGER NOT NULL,
    user_display_name TEXT,
    message_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_user ON messages (zalo_user_id, sent_at_ms);
