CREATE TABLE runs (
 id text PRIMARY KEY, config jsonb NOT NULL, fingerprint text NOT NULL,
 engine_version text NOT NULL, next_seq bigint NOT NULL DEFAULT 0,
 logical_time_ms bigint NOT NULL DEFAULT 0 CHECK(logical_time_ms >= 0),
 status text NOT NULL DEFAULT 'READY'
);
CREATE TABLE accounts (
 run_id text REFERENCES runs(id), id text, owner text NOT NULL,
 currency text NOT NULL CHECK(currency IN ('USD','EUR','SGD','INR')),
 issuer text NOT NULL, kind text NOT NULL CHECK(kind IN ('CLAIM','OPENING_OFFSET')),
 PRIMARY KEY(run_id,id), UNIQUE(run_id,id,currency,issuer)
);
CREATE TABLE positions (
 run_id text, account_id text, posted bigint NOT NULL DEFAULT 0,
 reserved bigint NOT NULL DEFAULT 0 CHECK(reserved>=0), buffer bigint NOT NULL DEFAULT 0 CHECK(buffer>=0),
 PRIMARY KEY(run_id,account_id), FOREIGN KEY(run_id,account_id) REFERENCES accounts(run_id,id)
);
CREATE TABLE payments (
 run_id text REFERENCES runs(id), id text, fingerprint text NOT NULL,
 command jsonb NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(run_id,id)
);
CREATE TABLE quotes (
 run_id text, id text, payment_id text NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(run_id,id), FOREIGN KEY(run_id,payment_id) REFERENCES payments(run_id,id)
);
CREATE TABLE reservations (
 run_id text, id text, payment_id text NOT NULL, account_id text NOT NULL,
 amount bigint NOT NULL CHECK(amount>0), expires_ms bigint NOT NULL,
 status text NOT NULL CHECK(status IN ('ACTIVE','CONSUMED','RELEASED')),
 PRIMARY KEY(run_id,id), FOREIGN KEY(run_id,payment_id) REFERENCES payments(run_id,id),
 FOREIGN KEY(run_id,account_id) REFERENCES accounts(run_id,id)
);
CREATE TABLE decisions (
 run_id text, payment_id text, decision text NOT NULL CHECK(decision IN ('PREPARED','COMMIT','ABORT')),
 PRIMARY KEY(run_id,payment_id), FOREIGN KEY(run_id,payment_id) REFERENCES payments(run_id,id)
);
CREATE TABLE journal_entries (
 run_id text REFERENCES runs(id), id text, payment_id text, currency text NOT NULL,
 issuer text NOT NULL, PRIMARY KEY(run_id,id), UNIQUE(run_id,id,currency,issuer),
 FOREIGN KEY(run_id,payment_id) REFERENCES payments(run_id,id)
);
CREATE TABLE journal_lines (
 run_id text, entry_id text, line_no integer, account_id text NOT NULL,
 currency text NOT NULL, issuer text NOT NULL, delta bigint NOT NULL CHECK(delta<>0),
 PRIMARY KEY(run_id,entry_id,line_no),
 FOREIGN KEY(run_id,entry_id,currency,issuer) REFERENCES journal_entries(run_id,id,currency,issuer),
 FOREIGN KEY(run_id,account_id,currency,issuer) REFERENCES accounts(run_id,id,currency,issuer)
);
CREATE TABLE events (
 run_id text REFERENCES runs(id), seq bigint, event_id text NOT NULL,
 logical_time_ms bigint NOT NULL, event_type text NOT NULL, aggregate_id text NOT NULL,
 aggregate_version bigint NOT NULL, causation_id text NOT NULL, correlation_id text NOT NULL,
 schema_version integer NOT NULL DEFAULT 1, payload jsonb NOT NULL,
 previous_hash text NOT NULL, event_hash text NOT NULL,
 PRIMARY KEY(run_id,seq), UNIQUE(run_id,event_id)
);
CREATE TABLE outbox (
 run_id text, seq bigint, PRIMARY KEY(run_id,seq),
 FOREIGN KEY(run_id,seq) REFERENCES events(run_id,seq)
);
CREATE INDEX events_aggregate ON events(run_id,aggregate_id,seq);
CREATE INDEX journal_payment ON journal_entries(run_id,payment_id);
CREATE INDEX reservations_payment ON reservations(run_id,payment_id);

CREATE FUNCTION immutable_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'APPEND_ONLY'; END; $$;
CREATE TRIGGER events_immutable BEFORE UPDATE OR DELETE ON events FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER entries_immutable BEFORE UPDATE OR DELETE ON journal_entries FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER lines_immutable BEFORE UPDATE OR DELETE ON journal_lines FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER accounts_immutable BEFORE UPDATE OR DELETE ON accounts FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER quotes_immutable BEFORE UPDATE OR DELETE ON quotes FOR EACH ROW EXECUTE FUNCTION immutable_record();

CREATE FUNCTION balanced_entry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n bigint; total numeric;
BEGIN
 SELECT count(*),coalesce(sum(delta),0) INTO n,total FROM journal_lines
 WHERE run_id=NEW.run_id AND entry_id=NEW.id;
 IF n<2 OR total<>0 THEN RAISE EXCEPTION 'UNBALANCED_JOURNAL'; END IF;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER journal_balanced AFTER INSERT ON journal_entries
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION balanced_entry();

CREATE FUNCTION validate_position() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (SELECT kind FROM accounts WHERE run_id=NEW.run_id AND id=NEW.account_id)='CLAIM'
 AND NEW.posted < NEW.reserved + NEW.buffer THEN RAISE EXCEPTION 'INSUFFICIENT_FUNDS'; END IF;
 IF abs(NEW.posted::numeric)>9000000000000000 THEN RAISE EXCEPTION 'OVERFLOW'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER position_safe BEFORE INSERT OR UPDATE ON positions FOR EACH ROW EXECUTE FUNCTION validate_position();

CREATE FUNCTION reservation_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status<>'ACTIVE' OR NEW.status NOT IN ('CONSUMED','RELEASED')
 OR (NEW.run_id,NEW.id,NEW.payment_id,NEW.account_id,NEW.amount,NEW.expires_ms)
 IS DISTINCT FROM (OLD.run_id,OLD.id,OLD.payment_id,OLD.account_id,OLD.amount,OLD.expires_ms)
 THEN RAISE EXCEPTION 'ILLEGAL_RESERVATION_TRANSITION'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER reservation_state BEFORE UPDATE ON reservations FOR EACH ROW EXECUTE FUNCTION reservation_transition();
