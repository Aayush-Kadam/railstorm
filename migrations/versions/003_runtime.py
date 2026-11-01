"""Persistent virtual-event queue cursor, operational faults and scenario registry."""

from alembic import op

revision = "003"
down_revision = "002"


def upgrade():
    op.execute("""
    CREATE TABLE scenarios(id text PRIMARY KEY, config jsonb NOT NULL, fingerprint text NOT NULL);
    CREATE TABLE runtime(run_id text PRIMARY KEY REFERENCES runs(id), cursor integer NOT NULL DEFAULT 0,
      queue jsonb NOT NULL, paused boolean NOT NULL DEFAULT true, error text);
    CREATE TABLE faults(run_id text REFERENCES runs(id), id text, data jsonb NOT NULL, active boolean NOT NULL,
      frozen_minor bigint NOT NULL DEFAULT 0, PRIMARY KEY(run_id,id));
    CREATE TRIGGER scenarios_immutable BEFORE UPDATE OR DELETE ON scenarios FOR EACH ROW EXECUTE FUNCTION immutable_record();
    """)


def downgrade():
    raise RuntimeError("Destructive downgrade disabled")
