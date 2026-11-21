-- After a failed refresh, the automatic refresh waits until this time (UTC ISO 8601), so a
-- wrong setting does not make every page load call Zalo again. NULL after a success.
ALTER TABLE oauth_tokens ADD COLUMN retry_after TEXT;
