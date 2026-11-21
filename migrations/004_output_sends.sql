-- One row per message part sent (or attempted) with an employee's daily output.
-- A row is written as 'sending' before Zalo is called, so a crash leaves a trace that the
-- part may have been delivered and it is not sent again blindly.
CREATE TABLE IF NOT EXISTS output_sends (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    employee_code TEXT NOT NULL,
    zalo_user_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    part INTEGER NOT NULL,
    parts INTEGER NOT NULL,
    status TEXT NOT NULL,
    zalo_message_id TEXT,
    error_code INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS output_sends_day ON output_sends (day, employee_code);
