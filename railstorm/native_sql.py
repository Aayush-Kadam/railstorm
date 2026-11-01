"""Prepared psycopg commands inside the existing SQLAlchemy-owned transaction.

No financial state is cached here. SQLAlchemy still owns pooling, the run mutex,
and commit/rollback. Pipeline exit must succeed before that owner may commit.
"""

from contextlib import contextmanager
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

import psycopg
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
from sqlalchemy.exc import DBAPIError, MultipleResultsFound, NoResultFound

_DIALECT = PGDialect_psycopg()


@lru_cache(maxsize=512)
def prepared_text(statement: str) -> str:
    # Use the supported dialect compiler, not regex parameter substitution.
    # Literal %, casts, repeated binds and quoted values retain their meaning.
    return str(text(statement).compile(dialect=_DIALECT))


class NativeResult:
    def __init__(self, commands, cursor, statement):
        self.commands = commands
        self.cursor = cursor
        self.statement = statement

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.all())

    def all(self):
        return self.commands.call(self.cursor.fetchall, self.statement)

    def fetchall(self):
        return self.all()

    def first(self):
        return self.commands.call(self.cursor.fetchone, self.statement)

    def fetchone(self):
        return self.first()

    def scalar(self):
        row = self.first()
        return next(iter(row.values())) if row is not None else None

    def scalar_one(self):
        rows = self.all()
        if not rows:
            raise NoResultFound()
        if len(rows) != 1:
            raise MultipleResultsFound()
        return next(iter(rows[0].values()))

    @property
    def rowcount(self):
        self.commands.sync()
        return self.cursor.rowcount


class NativeCommands:
    def __init__(self, connection, on_error):
        self.connection = connection
        self.driver = connection.connection.driver_connection
        if not isinstance(self.driver, psycopg.Connection):
            raise ValueError("Native financial commands require the pinned psycopg3 driver")
        self.pipeline: Any = None
        self.on_error = on_error

    def call(self, operation, statement="native command"):
        try:
            return operation()
        except psycopg.Error as error:
            self.fail(error, statement)

    def fail(self, error, statement):
        self.on_error()
        disconnected = self.driver.closed or self.connection.dialect.is_disconnect(error, self.driver, None)
        if disconnected:
            self.connection.invalidate(error)
        raise DBAPIError.instance(
            statement,
            None,
            error,
            psycopg.Error,
            connection_invalidated=disconnected,
            dialect=self.connection.dialect,
        ) from error

    @contextmanager
    def scope(self):
        try:
            with self.driver.pipeline() as pipeline:
                self.pipeline = pipeline
                yield self
        except psycopg.Error as error:
            self.fail(error, "pipeline completion")
        finally:
            self.pipeline = None

    def sync(self):
        if self.pipeline is not None:
            self.call(self.pipeline.sync, "pipeline synchronization")

    def execute(self, statement, parameters):
        sql = prepared_text(statement)
        cursor = self.driver.cursor(row_factory=dict_row)
        many = isinstance(parameters, list)
        context = SimpleNamespace(
            native_command=True,
            dialect=self.connection.dialect,
            compiled=None,
            execution_options=self.connection.get_execution_options(),
            statement=sql,
            parameters=parameters,
            executemany=many,
        )
        # Retain existing fault-injection and monitoring hooks. Unknown external
        # after-hooks get synchronized cursor metadata; explicitly pipeline-safe
        # observers may inspect timing/context without forcing every write roundtrip.
        try:
            for before in self.connection.dispatch.before_cursor_execute:
                sql, parameters = before(self.connection, cursor, sql, parameters, context, many)
            if many:
                self.call(lambda: cursor.executemany(sql, parameters), statement)
            else:
                self.call(lambda: cursor.execute(sql, parameters, prepare=True), statement)
            after_hooks = list(self.connection.dispatch.after_cursor_execute)
            if any(not getattr(after, "railstorm_pipeline_safe", False) for after in after_hooks):
                self.sync()
            for after in after_hooks:
                after(self.connection, cursor, sql, parameters, context, many)
        except BaseException:
            self.on_error()
            raise
        return NativeResult(self, cursor, statement)
