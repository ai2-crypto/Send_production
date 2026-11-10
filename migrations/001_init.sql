CREATE TABLE IF NOT EXISTS employee_zalo_links (
    employee_code TEXT PRIMARY KEY,
    employee_name TEXT NOT NULL,
    zalo_user_id TEXT NOT NULL UNIQUE,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at TEXT NOT NULL
);
