-- Private, temporary staging for offline identity/saved-view imports.
-- Rows can contain password hashes and refresh token IDs, so restrict D1 access.
CREATE TABLE IF NOT EXISTS cf_identity_import_runs (
    id TEXT PRIMARY KEY NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    users_count INTEGER NOT NULL CHECK (users_count >= 0),
    refresh_tokens_count INTEGER NOT NULL CHECK (refresh_tokens_count >= 0),
    saved_views_count INTEGER NOT NULL CHECK (saved_views_count >= 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS cf_identity_import_rows (
    run_id TEXT NOT NULL,
    table_name TEXT NOT NULL CHECK (table_name IN ('users', 'refresh_tokens', 'saved_views')),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    row_json TEXT NOT NULL CHECK (json_valid(row_json) AND json_type(row_json) = 'array'),
    PRIMARY KEY (run_id, table_name, ordinal),
    FOREIGN KEY (run_id) REFERENCES cf_identity_import_runs (id) ON DELETE CASCADE
);
