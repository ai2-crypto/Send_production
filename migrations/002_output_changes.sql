-- The last version of each report seen, to tell what changed when the file is edited.
CREATE TABLE IF NOT EXISTS output_snapshots (
    source_key TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_digest TEXT NOT NULL,
    outputs_digest TEXT NOT NULL,
    outputs_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per changed output line, kept until the operator marks it handled.
CREATE TABLE IF NOT EXISTS output_changes (
    id INTEGER PRIMARY KEY,
    detected_at TEXT NOT NULL,
    source_key TEXT NOT NULL,
    file_name TEXT NOT NULL,
    day TEXT NOT NULL,
    employee_code TEXT NOT NULL,
    employee_name TEXT NOT NULL,
    line TEXT NOT NULL,
    unit TEXT,
    old_quantity REAL,
    new_quantity REAL,
    handled_at TEXT
);

CREATE INDEX IF NOT EXISTS output_changes_open ON output_changes (handled_at, day);
