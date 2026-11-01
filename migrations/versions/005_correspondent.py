"""Bank-local general ledgers, directed deposit relationships and chain records."""

from alembic import op

revision = "005"
down_revision = "004"


def upgrade():
    op.execute("""
    ALTER TABLE accounts DROP CONSTRAINT accounts_kind_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_kind_check CHECK(kind IN ('CLAIM','OPENING_OFFSET','ASSET','EQUITY'));
    CREATE OR REPLACE FUNCTION validate_position() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE k text;
    BEGIN
      SELECT kind INTO k FROM accounts WHERE run_id=NEW.run_id AND id=NEW.account_id;
      IF k='CLAIM' AND NEW.posted<NEW.reserved+NEW.buffer THEN RAISE EXCEPTION 'INSUFFICIENT_FUNDS'; END IF;
      IF k='ASSET' AND (NEW.posted>0 OR NEW.reserved<>0 OR NEW.buffer<>0) THEN RAISE EXCEPTION 'INVALID_ASSET_POSITION'; END IF;
      IF abs(NEW.posted::numeric)>9000000000000000 THEN RAISE EXCEPTION 'OVERFLOW'; END IF;
      RETURN NEW;
    END; $$;
    CREATE TABLE cb_relationships(run_id text REFERENCES runs(id),id text,respondent text NOT NULL,
      correspondent text NOT NULL,currency text NOT NULL,nostro_id text NOT NULL,vostro_id text NOT NULL,
      config jsonb NOT NULL,PRIMARY KEY(run_id,id),FOREIGN KEY(run_id,nostro_id) REFERENCES accounts(run_id,id),
      FOREIGN KEY(run_id,vostro_id) REFERENCES accounts(run_id,id));
    CREATE TABLE cb_chains(run_id text,payment_id text,reference text NOT NULL,instruction jsonb NOT NULL,
      PRIMARY KEY(run_id,payment_id),UNIQUE(run_id,reference),FOREIGN KEY(run_id,payment_id) REFERENCES payments(run_id,id));
    CREATE TABLE cb_hops(run_id text,payment_id text,hop_index integer,id text,idempotency_key text NOT NULL,data jsonb NOT NULL,
      PRIMARY KEY(run_id,id),UNIQUE(run_id,payment_id,hop_index),UNIQUE(run_id,idempotency_key),
      FOREIGN KEY(run_id,payment_id) REFERENCES cb_chains(run_id,payment_id));
    CREATE TABLE cb_obligations(run_id text,payment_id text,id text,debtor text NOT NULL,account_id text NOT NULL,
      currency text NOT NULL,amount bigint NOT NULL CHECK(amount>0),status text NOT NULL CHECK(status IN ('OPEN','DISCHARGED','RETURNED')),
      opened_ms bigint NOT NULL,closed_ms bigint,PRIMARY KEY(run_id,id),FOREIGN KEY(run_id,payment_id) REFERENCES cb_chains(run_id,payment_id),
      FOREIGN KEY(run_id,account_id) REFERENCES accounts(run_id,id));
    CREATE INDEX cb_obligations_open ON cb_obligations(run_id,status,payment_id);
    CREATE INDEX cb_hops_payment ON cb_hops(run_id,payment_id,hop_index);
    CREATE INDEX expired_reservations ON reservations(run_id,expires_ms,payment_id) WHERE status='ACTIVE';
    CREATE TRIGGER cb_rel_immutable BEFORE UPDATE OR DELETE ON cb_relationships FOR EACH ROW EXECUTE FUNCTION immutable_record();
    CREATE TRIGGER cb_chain_immutable BEFORE UPDATE OR DELETE ON cb_chains FOR EACH ROW EXECUTE FUNCTION immutable_record();
    """)


def downgrade():
    raise RuntimeError("Destructive downgrade disabled")
