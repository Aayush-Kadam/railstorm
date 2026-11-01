"""PostgreSQL transaction boundary and canonical event store.

A run row is the mutex. Every command locks it before reading financial state,
so READ COMMITTED commands for a run serialize, including new-account/reservation
phantoms. Different runs remain independent. No messages leave a transaction.
"""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from sqlalchemy import create_engine, event, text

from railstorm import __version__
from railstorm.domain import TRANSITIONS, DomainError, canonical, digest
from railstorm.native_sql import NativeCommands

_active: ContextVar[tuple | None] = ContextVar("railstorm_transaction", default=None)


def _legacy_result_ready(conn, cursor, statement, parameters, context, many):
    """Preserve SQLAlchemy result/rowcount semantics for its public escape hatch."""
    if getattr(context, "native_command", False):
        return
    current = _active.get()
    if current is not None:
        transaction = current[1]
        if transaction.conn is conn:
            transaction.commands.sync()


setattr(_legacy_result_ready, "railstorm_pipeline_safe", True)


class Store:
    def __init__(self, url: str | None = None):
        self.engine = create_engine(url or os.environ["RAILSTORM_DATABASE_URL"], pool_size=8)
        if self.engine.dialect.name != "postgresql":
            raise ValueError("PostgreSQL required; no silent persistence substitute")
        event.listen(self.engine, "after_cursor_execute", _legacy_result_ready)

    @contextmanager
    def transaction(self, run_id: str, initialize_config: dict | None = None):
        current = _active.get()
        if current is not None:
            if initialize_config is not None:
                raise DomainError("NESTED_INITIALIZATION")
            owner, transaction = current
            if owner is not self or transaction.run_id != run_id:
                raise DomainError("CROSS_RUN_TRANSACTION")
            try:
                yield transaction
            except BaseException:
                transaction.rollback_only = True
                raise
            return
        with self.engine.begin() as conn:
            if initialize_config is not None:
                conn.execute(
                    text(
                        "INSERT INTO runs(id,config,fingerprint,engine_version) VALUES(:r,CAST(:c AS jsonb),:f,:v)"
                    ),
                    {
                        "r": run_id,
                        "c": canonical(initialize_config),
                        "f": digest(initialize_config),
                        "v": __version__,
                    },
                )
            run = (
                conn.execute(text("SELECT * FROM runs WHERE id=:r FOR UPDATE"), {"r": run_id})
                .mappings()
                .first()
            )
            if run is None:
                raise DomainError("RUN_NOT_FOUND")
            run_data = dict(run)
            run_data.pop("created_at", None)
            transaction = Transaction(conn, run_id, run_data)
            token = _active.set((self, transaction))
            original = (transaction.run["next_seq"], transaction.run["logical_time_ms"])
            try:
                with transaction.commands.scope():
                    yield transaction
                    if transaction.rollback_only:
                        raise DomainError("TRANSACTION_ROLLBACK_ONLY")
                    transaction.flush_events()
                    if original != (transaction.run["next_seq"], transaction.run["logical_time_ms"]):
                        transaction.execute(
                            "UPDATE runs SET next_seq=:s,logical_time_ms=:t WHERE id=:r",
                            s=transaction.run["next_seq"],
                            t=transaction.run["logical_time_ms"],
                        )
            finally:
                _active.reset(token)

    def create(self, run_id: str, config: dict):
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO runs(id,config,fingerprint,engine_version) VALUES(:r,CAST(:c AS jsonb),:f,:v)"
                ),
                {"r": run_id, "c": canonical(config), "f": digest(config), "v": __version__},
            )

    def events(self, run_id: str, after: int = 0, limit: int = 1000):
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT e.* FROM events e JOIN outbox o ON e.run_id=o.run_id AND e.seq=o.seq WHERE e.run_id=:r AND e.seq>:a ORDER BY e.seq LIMIT :n"
                    ),
                    {"r": run_id, "a": after, "n": limit},
                ).mappings()
            ]


class Transaction:
    def __init__(self, conn, run_id: str, run: dict):
        self.conn = conn
        self.run_id = run_id
        self.run = run
        self.last_hash: str | None = None
        self.versions: dict[str, int] = {}
        self._active_fault_rows: list[dict] | None = None
        self.event_rows: list[dict] = []
        self._payment_records: dict[str, dict | None] = {}
        self.rollback_only = False
        self.commands = NativeCommands(conn, self.mark_failed)

    def mark_failed(self):
        self.rollback_only = True

    def active_faults(self):
        """Run-locked transaction-local view; fault commands invalidate this cache."""
        if self._active_fault_rows is None:
            self._active_fault_rows = self.rows(
                "SELECT data FROM faults WHERE run_id=:r AND active=true ORDER BY id"
            )
        return self._active_fault_rows

    def invalidate_faults(self):
        self._active_fault_rows = None

    def execute(self, sql: str, **params):
        # Public in-transaction event reads retain read-your-writes semantics.
        if (
            self.event_rows
            and sql.lstrip().upper().startswith("SELECT")
            and ("FROM events" in sql or "JOIN events" in sql or "FROM outbox" in sql)
        ):
            self.flush_events()
        result = self.commands.execute(sql, {"r": self.run_id, **params})
        if (
            sql.lstrip()
            .upper()
            .startswith(("UPDATE PAYMENTS", "INSERT INTO PAYMENTS", "DELETE FROM PAYMENTS"))
        ):
            if "p" in params:
                self._payment_records.pop(params["p"], None)
            else:
                self._payment_records.clear()
        return result

    def flush_events(self):
        """Persist every frozen event and outbox row before the existing commit.

        Parameterized executemany preserves row identities and hashes. A failure
        at either insert propagates to the enclosing financial transaction.
        """
        if not self.event_rows:
            return
        self.commands.execute(
            """INSERT INTO events VALUES(:r,:seq,:event_id,:logical_time_ms,:event_type,
          :aggregate_id,:aggregate_version,:causation_id,:correlation_id,:schema_version,
          CAST(:payload AS jsonb),:previous_hash,:event_hash)""",
            self.event_rows,
        )
        self.commands.execute(
            "INSERT INTO outbox VALUES(:r,:s)",
            [{"r": self.run_id, "s": row["seq"]} for row in self.event_rows],
        )
        self.event_rows.clear()

    def rows(self, sql: str, **params):
        return [dict(x) for x in self.execute(sql, **params).mappings()]

    def one(self, sql: str, **params):
        rows = self.rows(sql, **params)
        return rows[0] if rows else None

    def event(self, kind: str, aggregate: str, payload: dict):
        seq = self.run["next_seq"] + 1
        self.run["next_seq"] = seq
        if self.last_hash is None:
            prior = self.one("SELECT event_hash FROM events WHERE run_id=:r AND seq=:s", s=seq - 1)
            self.last_hash = prior["event_hash"] if prior else "0" * 64
        previous = self.last_hash
        if aggregate not in self.versions:
            # A first-seen aggregate has no buffered records; later versions are
            # already tracked locally. Avoid flushing unrelated pending events.
            self.versions[aggregate] = self.commands.execute(
                "SELECT count(*) AS n FROM events WHERE run_id=:r AND aggregate_id=:a",
                {"r": self.run_id, "a": aggregate},
            ).scalar_one()
        self.versions[aggregate] += 1
        version = self.versions[aggregate]
        value = {
            "seq": seq,
            "event_id": f"e{seq:09d}",
            "logical_time_ms": self.run["logical_time_ms"],
            "event_type": kind,
            "aggregate_id": aggregate,
            "aggregate_version": version,
            "causation_id": aggregate,
            "correlation_id": aggregate,
            "schema_version": 1,
            "payload": payload,
            "previous_hash": previous,
        }
        event_hash = digest(value)
        self.event_rows.append(
            {"r": self.run_id, **value, "payload": canonical(payload), "event_hash": event_hash}
        )
        self.last_hash = event_hash

    def time(self, at_ms: int):
        if at_ms < self.run["logical_time_ms"]:
            raise DomainError("TIME_REVERSAL")
        self.run["logical_time_ms"] = at_ms

    def position(self, account: str):
        row = self.one("SELECT * FROM positions WHERE run_id=:r AND account_id=:a", a=account)
        if row is None:
            raise DomainError("ACCOUNT_NOT_FOUND")
        return row

    def account(self, account: str, owner: str, currency: str, kind: str = "CLAIM", issuer: str = "SHARED"):
        data = {"id": account, "owner": owner, "currency": currency, "issuer": issuer, "kind": kind}
        self.execute("INSERT INTO accounts VALUES(:r,:id,:owner,:currency,:issuer,:kind)", **data)
        self.execute("INSERT INTO positions(run_id,account_id) VALUES(:r,:a)", a=account)
        self.event("ACCOUNT_OPENED", account, data)

    def journal(
        self,
        identity: str,
        payment: str | None,
        currency: str,
        lines: list[tuple[str, int]],
        issuer: str = "SHARED",
    ):
        if sum(delta for _, delta in lines) != 0 or len(lines) < 2:
            raise DomainError("UNBALANCED_JOURNAL")
        self.execute(
            "INSERT INTO journal_entries VALUES(:r,:id,:p,:c,:i)",
            id=identity,
            p=payment,
            c=currency,
            i=issuer,
        )
        parameters = []
        for index, (account, delta) in enumerate(lines):
            if type(delta) is not int or delta == 0:
                raise DomainError("INVALID_POSTING")
            parameters.append(
                {
                    "r": self.run_id,
                    "j": identity,
                    "n": index,
                    "a": account,
                    "c": currency,
                    "i": issuer,
                    "d": delta,
                }
            )
        # Bounded posting-group batches retain every line, projection update and
        # database constraint under the same financial transaction and run lock.
        self.commands.execute("INSERT INTO journal_lines VALUES(:r,:j,:n,:a,:c,:i,:d)", parameters)
        self.commands.execute(
            "UPDATE positions SET posted=posted+:d WHERE run_id=:r AND account_id=:a", parameters
        )
        self.event(
            "JOURNAL_POSTED",
            payment or identity,
            {
                "id": identity,
                "payment_id": payment,
                "currency": currency,
                "issuer": issuer,
                "lines": [{"account_id": a, "delta": d} for a, d in lines],
            },
        )

    def payment_record(self, identity: str):
        if identity not in self._payment_records:
            self._payment_records[identity] = self.one(
                "SELECT * FROM payments WHERE run_id=:r AND id=:p", p=identity
            )
        # Callers can edit a proposed transition without modifying the old
        # authoritative value used to validate that transition.
        return deepcopy(self._payment_records[identity])

    def payment(self, identity: str):
        row = self.payment_record(identity)
        return row["data"] if row else None

    def create_payment(self, identity: str, fingerprint: str, command: dict, state: dict):
        self.execute(
            "INSERT INTO payments VALUES(:r,:p,:f,CAST(:c AS jsonb),CAST(:d AS jsonb))",
            p=identity,
            f=fingerprint,
            c=canonical(command),
            d=canonical(state),
        )
        self._payment_records[identity] = deepcopy(
            {
                "run_id": self.run_id,
                "id": identity,
                "fingerprint": fingerprint,
                "command": command,
                "data": state,
            }
        )

    def payment_state(self, identity: str, data: dict):
        record = self.payment_record(identity)
        old = record["data"] if record else None
        transitions = TRANSITIONS
        if old and old.get("rail") == "CORRESPONDENT":
            from railstorm.rails.correspondent import CB_TRANSITIONS

            transitions = CB_TRANSITIONS
        if old and data["economic_status"] not in transitions.get(old["economic_status"], set()):
            raise DomainError("ILLEGAL_TRANSITION")
        if old and old["economic_status"] == "SETTLED":
            if {k: v for k, v in old.items() if k != "ack_status"} != {
                k: v for k, v in data.items() if k != "ack_status"
            }:
                raise DomainError("FINAL_SETTLEMENT_IMMUTABLE")
        self.execute(
            "UPDATE payments SET data=CAST(:d AS jsonb) WHERE run_id=:r AND id=:p",
            p=identity,
            d=canonical(data),
        )
        if record is not None:
            record["data"] = deepcopy(data)
            self._payment_records[identity] = record
        self.event("PAYMENT_CHANGED", identity, {"id": identity, **data})

    def reserve(self, identity: str, payment: str, account: str, amount: int, expiry: int):
        # Both writes already share the run mutex and transaction. The CTE
        # combines their communication without changing triggers or predicates.
        self.execute(
            "WITH projection AS (UPDATE positions SET reserved=reserved+:n "
            "WHERE run_id=:r AND account_id=:a RETURNING account_id) "
            "INSERT INTO reservations SELECT :r,:id,:p,:a,:n,:e,'ACTIVE' "
            "FROM (SELECT count(*) FROM projection) dependency",
            id=identity,
            p=payment,
            a=account,
            n=amount,
            e=expiry,
        )
        self.event(
            "RESERVATION_CHANGED",
            payment,
            {
                "id": identity,
                "payment_id": payment,
                "account_id": account,
                "amount": amount,
                "expires_ms": expiry,
                "status": "ACTIVE",
            },
        )

    def resolve(self, payment: str, status: str, expected: dict | None = None):
        rows = self.rows(
            "SELECT * FROM reservations WHERE run_id=:r AND payment_id=:p AND status='ACTIVE' ORDER BY id",
            p=payment,
        )
        if status == "CONSUMED":
            observed = {row["id"]: (row["account_id"], row["amount"], row["expires_ms"]) for row in rows}
            if expected is None or observed != expected:
                raise DomainError("RESERVATION_MISMATCH")
        for row in rows:
            self.execute(
                "WITH projection AS (UPDATE positions SET reserved=reserved-:n "
                "WHERE run_id=:r AND account_id=:a RETURNING account_id) "
                "UPDATE reservations SET status=:s WHERE run_id=:r AND id=:id "
                "AND (SELECT count(*) FROM projection)>=0",
                s=status,
                id=row["id"],
                a=row["account_id"],
                n=row["amount"],
            )
            self.event(
                "RESERVATION_CHANGED",
                payment,
                {k: v for k, v in {**row, "status": status}.items() if k != "run_id"},
            )

    def decision(self, payment: str, decision: str):
        old = self.one("SELECT decision FROM decisions WHERE run_id=:r AND payment_id=:p", p=payment)
        if old and old["decision"] != "PREPARED":
            raise DomainError("ILLEGAL_DECISION_TRANSITION")
        self.execute(
            """INSERT INTO decisions VALUES(:r,:p,:d)
        ON CONFLICT(run_id,payment_id) DO UPDATE SET decision=excluded.decision""",
            p=payment,
            d=decision,
        )
        self.event("DECISION_CHANGED", payment, {"payment_id": payment, "decision": decision})
