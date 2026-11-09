-- Pins have no expiry: a reader interrupted at any point must stay safe.
CREATE TABLE IF NOT EXISTS cf_sqlite_image_pins (
    id TEXT PRIMARY KEY NOT NULL,
    database_name TEXT NOT NULL,
    image_id TEXT NOT NULL REFERENCES cf_sqlite_images(id),
    revision INTEGER NOT NULL CHECK (revision > 0)
);
CREATE INDEX IF NOT EXISTS cf_sqlite_pins_image ON cf_sqlite_image_pins(image_id);
CREATE INDEX IF NOT EXISTS cf_sqlite_image_gc ON cf_sqlite_images(database_name, sealed, parent_revision);
CREATE INDEX IF NOT EXISTS cf_sqlite_chunk_ref ON cf_sqlite_image_chunks(data_base64, image_id);
DROP TRIGGER IF EXISTS cf_sqlite_images_no_delete_sealed;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_images_protect_live
BEFORE DELETE ON cf_sqlite_images WHEN OLD.sealed=1 AND (
    EXISTS (SELECT 1 FROM cf_sqlite_heads WHERE image_id=OLD.id) OR
    EXISTS (SELECT 1 FROM cf_sqlite_image_pins WHERE image_id=OLD.id)
)
BEGIN
    SELECT RAISE(ABORT, 'head or pinned image cannot be deleted');
END;
DROP TRIGGER IF EXISTS cf_sqlite_pool_no_delete;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_pool_protect_referenced
BEFORE DELETE ON cf_sqlite_chunk_pool WHEN EXISTS (
    SELECT 1 FROM cf_sqlite_image_chunks c JOIN cf_sqlite_images i ON i.id=c.image_id
    WHERE i.database_name=OLD.database_name AND c.data_base64='@' || OLD.sha256
)
BEGIN
    SELECT RAISE(ABORT, 'referenced chunk cannot be deleted');
END;

-- Keep a small immutable publication receipt after collecting image bytes.
-- This preserves the ability to resolve an unknown publish outcome later.
CREATE TABLE IF NOT EXISTS cf_sqlite_publication_receipts (
    image_id TEXT PRIMARY KEY NOT NULL,
    database_name TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64)
);
CREATE TRIGGER IF NOT EXISTS cf_sqlite_archive_publication
BEFORE DELETE ON cf_sqlite_images WHEN OLD.sealed=1
BEGIN
    INSERT INTO cf_sqlite_publication_receipts(image_id,database_name,revision,sha256)
    VALUES (OLD.id,OLD.database_name,OLD.parent_revision+1,OLD.sha256);
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_images_no_receipt_reuse
BEFORE INSERT ON cf_sqlite_images
WHEN EXISTS (SELECT 1 FROM cf_sqlite_publication_receipts WHERE image_id=NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'published image ID already used');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_receipts_no_update
BEFORE UPDATE ON cf_sqlite_publication_receipts
BEGIN
    SELECT RAISE(ABORT, 'publication receipts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_receipts_no_delete
BEFORE DELETE ON cf_sqlite_publication_receipts
BEGIN
    SELECT RAISE(ABORT, 'publication receipts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS cf_sqlite_receipts_no_replace
BEFORE INSERT ON cf_sqlite_publication_receipts
WHEN EXISTS (SELECT 1 FROM cf_sqlite_publication_receipts WHERE image_id=NEW.image_id)
BEGIN
    SELECT RAISE(ABORT, 'publication receipt already exists');
END;
