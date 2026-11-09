-- Offline staging upgrade; unfinished import runs must be resolved first.
SELECT CASE WHEN EXISTS(SELECT 1 FROM cf_identity_import_runs) OR EXISTS(SELECT 1 FROM cf_identity_import_rows)
    THEN json('unfinished identity import exists') ELSE 1 END;
DROP TRIGGER cf_identity_import_runs_immutable;
DROP TRIGGER cf_identity_import_rows_no_update;
DROP TRIGGER cf_identity_import_rows_no_delete;
DROP TRIGGER cf_identity_import_rows_only_unsealed;
ALTER TABLE cf_identity_import_runs ADD COLUMN manifest_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(manifest_json) AND json_type(manifest_json) = 'object');
ALTER TABLE cf_identity_import_rows RENAME TO cf_identity_import_rows_old;
CREATE TABLE cf_identity_import_rows (
    run_id TEXT NOT NULL,
    table_name TEXT NOT NULL CHECK (length(table_name) BETWEEN 1 AND 64),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    row_json TEXT NOT NULL CHECK (json_valid(row_json) AND json_type(row_json) = 'array'),
    PRIMARY KEY (run_id, table_name, ordinal),
    FOREIGN KEY (run_id) REFERENCES cf_identity_import_runs (id) ON DELETE CASCADE
);
DROP TABLE cf_identity_import_rows_old;
CREATE TRIGGER cf_identity_import_runs_immutable
BEFORE UPDATE ON cf_identity_import_runs
WHEN NOT (OLD.sealed = 0 AND NEW.sealed = 1
          AND NEW.id = OLD.id AND NEW.snapshot_sha256 = OLD.snapshot_sha256
          AND NEW.users_count = OLD.users_count AND NEW.refresh_tokens_count = OLD.refresh_tokens_count
          AND NEW.saved_views_count = OLD.saved_views_count AND NEW.api_keys_count = OLD.api_keys_count
          AND NEW.manifest_json = OLD.manifest_json AND NEW.created_at = OLD.created_at)
BEGIN
    SELECT RAISE(ABORT, 'import run is immutable');
END;
CREATE TRIGGER cf_identity_import_rows_no_update
BEFORE UPDATE ON cf_identity_import_rows
BEGIN
    SELECT RAISE(ABORT, 'staged rows are immutable');
END;
CREATE TRIGGER cf_identity_import_rows_no_delete
BEFORE DELETE ON cf_identity_import_rows
WHEN EXISTS (SELECT 1 FROM cf_identity_import_runs WHERE id = OLD.run_id)
BEGIN
    SELECT RAISE(ABORT, 'remove the parent import run');
END;
CREATE TRIGGER cf_identity_import_rows_only_unsealed
BEFORE INSERT ON cf_identity_import_rows
WHEN NOT EXISTS (SELECT 1 FROM cf_identity_import_runs WHERE id = NEW.run_id AND sealed = 0)
     OR EXISTS (SELECT 1 FROM cf_identity_import_rows
                WHERE run_id = NEW.run_id AND table_name = NEW.table_name AND ordinal = NEW.ordinal)
BEGIN
    SELECT RAISE(ABORT, 'import run is not open');
END;
