"""Prevent appending journal lines after their original committing transaction."""

from alembic import op

revision = "002"
down_revision = "001"


def upgrade():
    op.execute("""CREATE FUNCTION journal_line_window() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (SELECT xmin::text::bigint FROM journal_entries WHERE run_id=NEW.run_id AND id=NEW.entry_id)
      <> (pg_current_xact_id()::text::bigint % 4294967296) THEN
        RAISE EXCEPTION 'JOURNAL_ALREADY_SEALED';
      END IF;
      RETURN NEW;
    END; $$;
    CREATE TRIGGER lines_sealed BEFORE INSERT ON journal_lines FOR EACH ROW EXECUTE FUNCTION journal_line_window();
    CREATE FUNCTION decision_monotonic() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.decision<>'PREPARED' OR NEW.decision NOT IN ('COMMIT','ABORT') THEN
        RAISE EXCEPTION 'ILLEGAL_DECISION_TRANSITION';
      END IF;
      RETURN NEW;
    END; $$;
    CREATE TRIGGER decision_state BEFORE UPDATE ON decisions FOR EACH ROW EXECUTE FUNCTION decision_monotonic();
    CREATE TRIGGER reservations_no_delete BEFORE DELETE ON reservations FOR EACH ROW EXECUTE FUNCTION immutable_record();
    CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions FOR EACH ROW EXECUTE FUNCTION immutable_record();
    """)


def downgrade():
    raise RuntimeError("Destructive downgrade disabled")
