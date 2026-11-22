-- Audit logs are append-only. Block UPDATE/DELETE/TRUNCATE at the database level so that
-- no application code path (or compromised dashboard account) can rewrite history.
CREATE OR REPLACE FUNCTION activity_log_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'ActivityLog is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER activity_log_no_update_delete
  BEFORE UPDATE OR DELETE ON "ActivityLog"
  FOR EACH ROW EXECUTE FUNCTION activity_log_immutable();

CREATE TRIGGER activity_log_no_truncate
  BEFORE TRUNCATE ON "ActivityLog"
  FOR EACH STATEMENT EXECUTE FUNCTION activity_log_immutable();
