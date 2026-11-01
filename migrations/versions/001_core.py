"""Initial ledger, immutable journal and safety constraints."""

from pathlib import Path

from alembic import op

revision = "001"
down_revision = None


def upgrade():
    sql = (Path(__file__).parents[1] / "001_core.sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a disposable database instead.")
