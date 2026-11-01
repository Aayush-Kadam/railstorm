"""Operational creation time is excluded from deterministic financial hashes."""

from alembic import op

revision = "004"
down_revision = "003"


def upgrade():
    op.execute("ALTER TABLE runs ADD COLUMN created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled")
