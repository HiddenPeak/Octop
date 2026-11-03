-- The primary role now grants non-exclusive owner access. Existing identities,
-- sessions and trust records remain unchanged; only the singleton limit is removed.
DROP INDEX IF EXISTS idx_box_active_primary;
UPDATE _schema_version SET version = 63;
