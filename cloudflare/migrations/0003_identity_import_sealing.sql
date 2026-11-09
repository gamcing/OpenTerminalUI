-- Seal staged identity rows before cutover; erase them only via their parent run.
ALTER TABLE cf_identity_import_runs ADD COLUMN sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0, 1));

CREATE TRIGGER cf_identity_import_runs_no_replace
BEFORE INSERT ON cf_identity_import_runs
WHEN EXISTS (SELECT 1 FROM cf_identity_import_runs WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'import run already exists');
END;

CREATE TRIGGER cf_identity_import_runs_immutable
BEFORE UPDATE ON cf_identity_import_runs
WHEN NOT (OLD.sealed = 0 AND NEW.sealed = 1
          AND NEW.id = OLD.id AND NEW.snapshot_sha256 = OLD.snapshot_sha256
          AND NEW.users_count = OLD.users_count AND NEW.refresh_tokens_count = OLD.refresh_tokens_count
          AND NEW.saved_views_count = OLD.saved_views_count AND NEW.created_at = OLD.created_at)
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
