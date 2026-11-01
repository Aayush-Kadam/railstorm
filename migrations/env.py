import os

from alembic import context
from sqlalchemy import create_engine

engine = create_engine(os.environ["RAILSTORM_DATABASE_URL"])
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
