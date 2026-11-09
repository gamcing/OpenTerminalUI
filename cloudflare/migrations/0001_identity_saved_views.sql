-- Experimental initial D1 schema for the identity and saved-view migration.
-- Keep aligned with Alembic revisions 0001_initial and 0011_saved_views.
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(36) PRIMARY KEY NOT NULL,
    email VARCHAR(320) NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    role VARCHAR(6) NOT NULL DEFAULT 'VIEWER',
    created_at DATETIME NOT NULL,
    last_login DATETIME
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email);
CREATE INDEX IF NOT EXISTS ix_users_role ON users (role);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id VARCHAR(36) PRIMARY KEY NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    jti VARCHAR(64) NOT NULL,
    expires_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    revoked_at DATETIME,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_user_id ON refresh_tokens (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_refresh_tokens_jti ON refresh_tokens (jti);
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_expires_at ON refresh_tokens (expires_at);
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_revoked_at ON refresh_tokens (revoked_at);

CREATE TABLE IF NOT EXISTS saved_views (
    id VARCHAR(36) PRIMARY KEY NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    name VARCHAR(160) NOT NULL,
    scope VARCHAR(80) NOT NULL,
    page VARCHAR(240) NOT NULL,
    payload_json JSON NOT NULL DEFAULT '{}',
    description TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saved_views_user_id ON saved_views (user_id);
CREATE INDEX IF NOT EXISTS ix_saved_views_name ON saved_views (name);
CREATE INDEX IF NOT EXISTS ix_saved_views_scope ON saved_views (scope);
CREATE INDEX IF NOT EXISTS ix_saved_views_page ON saved_views (page);
CREATE INDEX IF NOT EXISTS ix_saved_views_created_at ON saved_views (created_at);
CREATE INDEX IF NOT EXISTS ix_saved_views_updated_at ON saved_views (updated_at);
