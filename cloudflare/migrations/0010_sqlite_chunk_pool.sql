-- Experimental immutable, content-addressed chunks. Existing inline chunks remain readable.
CREATE TABLE IF NOT EXISTS cf_sqlite_chunk_pool (
    database_name TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    data_base64 TEXT NOT NULL,
    PRIMARY KEY (database_name, sha256)
);
CREATE TRIGGER IF NOT EXISTS cf_sqlite_pool_no_changed_replace
BEFORE INSERT ON cf_sqlite_chunk_pool
WHEN EXISTS (SELECT 1 FROM cf_sqlite_chunk_pool
             WHERE database_name=NEW.database_name AND sha256=NEW.sha256 AND data_base64<>NEW.data_base64)
BEGIN
    SELECT RAISE(ABORT, 'chunk digest already has different content');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_pool_no_update
BEFORE UPDATE ON cf_sqlite_chunk_pool
BEGIN
    SELECT RAISE(ABORT, 'pooled chunks are immutable');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_pool_no_delete
BEFORE DELETE ON cf_sqlite_chunk_pool
BEGIN
    SELECT RAISE(ABORT, 'pooled chunks cannot be deleted');
END;
