-- Experimental private storage for immutable, versioned SQLite images.
-- These tables are not an application database until commit/recovery integration exists.
CREATE TABLE IF NOT EXISTS cf_sqlite_images (
    id TEXT PRIMARY KEY NOT NULL,
    database_name TEXT NOT NULL,
    parent_revision INTEGER NOT NULL CHECK (parent_revision >= 0),
    byte_count INTEGER NOT NULL CHECK (byte_count > 0),
    chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0, 1))
);
CREATE TABLE IF NOT EXISTS cf_sqlite_image_chunks (
    image_id TEXT NOT NULL REFERENCES cf_sqlite_images(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    data_base64 TEXT NOT NULL,
    PRIMARY KEY (image_id, ordinal)
);
CREATE TABLE IF NOT EXISTS cf_sqlite_heads (
    database_name TEXT PRIMARY KEY NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    image_id TEXT NOT NULL REFERENCES cf_sqlite_images(id)
);
CREATE TRIGGER IF NOT EXISTS cf_sqlite_images_no_replace
BEFORE INSERT ON cf_sqlite_images
WHEN EXISTS (SELECT 1 FROM cf_sqlite_images WHERE id=NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'image ID already exists');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_images_immutable
BEFORE UPDATE ON cf_sqlite_images
WHEN NOT (OLD.sealed=0 AND NEW.sealed=1 AND NEW.id=OLD.id AND NEW.database_name=OLD.database_name
    AND NEW.parent_revision=OLD.parent_revision AND NEW.byte_count=OLD.byte_count
    AND NEW.chunk_count=OLD.chunk_count AND NEW.sha256=OLD.sha256)
BEGIN
    SELECT RAISE(ABORT, 'image is immutable');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_images_no_delete_sealed
BEFORE DELETE ON cf_sqlite_images WHEN OLD.sealed=1
BEGIN
    SELECT RAISE(ABORT, 'sealed image cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_chunks_insert_open
BEFORE INSERT ON cf_sqlite_image_chunks
WHEN NOT EXISTS (SELECT 1 FROM cf_sqlite_images WHERE id=NEW.image_id AND sealed=0)
    OR EXISTS (SELECT 1 FROM cf_sqlite_image_chunks WHERE image_id=NEW.image_id AND ordinal=NEW.ordinal)
BEGIN
    SELECT RAISE(ABORT, 'image is not open');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_chunks_no_update
BEFORE UPDATE ON cf_sqlite_image_chunks
BEGIN
    SELECT RAISE(ABORT, 'image chunks are immutable');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_chunks_no_delete
BEFORE DELETE ON cf_sqlite_image_chunks
WHEN EXISTS (SELECT 1 FROM cf_sqlite_images WHERE id=OLD.image_id)
BEGIN
    SELECT RAISE(ABORT, 'delete the parent image');
END;
