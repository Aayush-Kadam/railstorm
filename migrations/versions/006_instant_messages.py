"""Durable instant-link delivery records; no external broker."""

from alembic import op

revision = "006"
down_revision = "005"


def upgrade():
    op.execute(
        "CREATE TABLE il_messages(run_id text NOT NULL REFERENCES runs(id), id text NOT NULL, data jsonb NOT NULL, PRIMARY KEY(run_id,id))"
    )


def downgrade():
    raise RuntimeError("Destructive downgrade disabled")
