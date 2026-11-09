-- Experimental user-scoped watchlists for the D1 identity migration.
CREATE TABLE IF NOT EXISTS watchlists (
    id VARCHAR(36) PRIMARY KEY NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    name VARCHAR(128) NOT NULL,
    symbols_json JSON NOT NULL CHECK (json_valid(symbols_json) AND json_type(symbols_json) = 'array'),
    column_config_json JSON NOT NULL CHECK (json_valid(column_config_json) AND json_type(column_config_json) = 'object'),
    created_at DATETIME NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_watchlists_user_id ON watchlists (user_id);
CREATE INDEX IF NOT EXISTS ix_watchlists_name ON watchlists (name);
