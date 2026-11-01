"""Index append-only market versions without changing quote selection."""

from alembic import op

revision = "008"
down_revision = "007"


def upgrade():
    op.execute(
        "CREATE INDEX market_quote_lookup ON events(run_id,(payload->>'pair'),seq DESC) "
        "WHERE event_type='MARKET_QUOTE'"
    )


def downgrade():
    op.execute("DROP INDEX market_quote_lookup")
