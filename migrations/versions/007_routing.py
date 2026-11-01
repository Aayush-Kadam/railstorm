"""Durable routing decisions, separate from settlement financial effects."""

from alembic import op

revision = "007"
down_revision = "006"


def upgrade():
    op.execute(
        "CREATE TABLE routing_records(run_id text NOT NULL REFERENCES runs(id), id text NOT NULL, data jsonb NOT NULL, PRIMARY KEY(run_id,id))"
    )

    op.execute(
        "CREATE UNIQUE INDEX routing_idempotency ON routing_records (run_id, ((data->'evaluation'->'intent'->>'idempotency_key')))"
    )


def downgrade():
    raise RuntimeError("Destructive downgrade disabled")
