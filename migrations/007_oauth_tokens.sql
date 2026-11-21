-- The OA token pair, kept up to date by the app: every refresh returns a new refresh token and
-- voids the old one, so the pair can only live somewhere the app can write. Both tokens are
-- encrypted with TOKEN_ENCRYPTION_KEY. A single OA is used, so there is one row named 'oa'.
CREATE TABLE IF NOT EXISTS oauth_tokens (
    name TEXT PRIMARY KEY CHECK (name = 'oa'),
    access_token_enc TEXT NOT NULL,
    refresh_token_enc TEXT NOT NULL,
    -- UTC ISO 8601.
    access_expires_at TEXT NOT NULL,
    refresh_expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- While set and in the future, one caller is refreshing; others wait and read the result.
    refresh_lease_until TEXT,
    -- Why the last refresh failed, shown to the operator; NULL after a success.
    last_error TEXT,
    -- 1 when Zalo refused the refresh token, so only new tokens from API Explorer can help.
    needs_reauthorization INTEGER NOT NULL DEFAULT 0
);
