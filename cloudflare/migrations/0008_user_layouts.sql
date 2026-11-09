-- Do not change the import manifest while an older staged import is pending.
SELECT CASE WHEN EXISTS(SELECT 1 FROM cf_identity_import_runs) OR EXISTS(SELECT 1 FROM cf_identity_import_rows)
    THEN json('unfinished identity import exists') ELSE 1 END;
CREATE TABLE IF NOT EXISTS user_layouts (
    id VARCHAR(36) PRIMARY KEY NOT NULL,
    user_key VARCHAR(64) NOT NULL UNIQUE,
    layouts_json JSON NOT NULL CHECK (json_valid(layouts_json) AND json_type(layouts_json) = 'array'),
    updated_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_user_layouts_updated_at ON user_layouts (updated_at);
