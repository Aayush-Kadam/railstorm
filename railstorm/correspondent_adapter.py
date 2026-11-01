"""Read-only bank-local preflight and thin execution bridge to CorrespondentRail."""

from railstorm.domain import MAX_MINOR, Currency
from railstorm.rails.correspondent import ChainCommand, CorrespondentRail, Relationship


class CorrespondentAdapter:
    def __init__(self, store):
        self.store = store
        self.rail = CorrespondentRail(store)

    @staticmethod
    def command(intent, deadline_ms=None, planning=False):
        limits = {str(intent.source_currency): intent.maximum_fee_minor}
        if intent.maximum_fees_by_currency is not None:
            limits.update({str(k): v for k, v in intent.maximum_fees_by_currency.items()})
        limits[str(intent.source_currency)] = min(
            intent.maximum_fee_minor, limits.get(str(intent.source_currency), intent.maximum_fee_minor)
        )
        return ChainCommand(
            idempotency_key=intent.idempotency_key,
            sender_bank_id=intent.source,
            receiver_bank_id=intent.destination,
            source_currency=intent.source_currency,
            destination_currency=intent.destination_currency,
            principal_minor=intent.amount_minor,
            deadline_ms=deadline_ms or min(86400000, intent.execute_until_ms - intent.created_ms),
            max_fees_minor={c: MAX_MINOR if planning else limits.get(str(c), 0) for c in Currency},
        )

    def evaluate(self, tx, intent, at_ms, down):
        rels = [
            Relationship.model_validate(r["config"])
            for r in tx.rows("SELECT config FROM cb_relationships WHERE run_id=:r ORDER BY id")
        ]
        path, out, fees = self.rail._plan(rels, self.command(intent, planning=True), at_ms)
        lookup = {r.id: r for r in rels}
        result = {
            "path": path,
            "beneficiary_minor": out,
            "fees_by_currency": fees,
            "fee_details": [],
            "liquidity": [],
            "reasons": [],
            "status": "FEASIBLE",
            "latency_ms": sum(s["delay_ms"] for s in path),
            "execution_adapter": True,
            "reservation_guarantee": "Observed only; reserve/consume at each hop under run mutex, not end-to-end reservation",
        }

        def reject(status, code, **detail):
            result["reasons"].append({"code": code, **detail})
            if result["status"] == "FEASIBLE":
                result["status"] = status

        limits = {str(intent.source_currency): intent.maximum_fee_minor}
        if intent.maximum_fees_by_currency is not None:
            limits.update({str(c): n for c, n in intent.maximum_fees_by_currency.items()})
        limits[str(intent.source_currency)] = min(
            intent.maximum_fee_minor, limits.get(str(intent.source_currency), intent.maximum_fee_minor)
        )
        for c, n in fees.items():
            if n and c not in limits:
                reject(
                    "QUOTE_UNAVAILABLE",
                    "FEE_REPORTING_CURRENCY_UNAVAILABLE",
                    currency=c,
                    fee_minor=n,
                    reporting_currency=str(intent.source_currency),
                )
            elif n > limits.get(c, 0):
                reject(
                    "STRUCTURALLY_INELIGIBLE",
                    "FEE_LIMIT",
                    currency=c,
                    required_minor=n,
                    maximum_minor=limits.get(c, 0),
                )
        for i, s in enumerate(path):
            if s["sender"] in down or s["receiver"] in down:
                reject(
                    "TEMPORARILY_UNAVAILABLE",
                    "INTERMEDIARY_DOWN",
                    bank=s["sender"] if s["sender"] in down else s["receiver"],
                )
            if s["type"] == "FX":
                q = s["quote"]
                if at_ms + sum(p["delay_ms"] for p in path[: i + 1]) >= q["expires_ms"]:
                    reject("QUOTE_UNAVAILABLE", "FX_QUOTE_EXPIRES_BEFORE_STAGE")
                # Inventory is the same funded destination vostro consumed by the next hop, not an extra pool.
                continue
            rel = lookup[s["relationship_id"]]
            day, clock = divmod(at_ms, 86400000)
            nominal = at_ms + sum(stage["delay_ms"] for stage in path[: i + 1])
            nominal_day, nominal_clock = divmod(nominal, 86400000)
            if nominal_day in rel.holidays or not rel.open_ms <= nominal_clock < rel.cutoff_ms:
                reject(
                    "TEMPORARILY_UNAVAILABLE",
                    "OPERATING_WINDOW_BEFORE_STAGE",
                    relationship_id=rel.id,
                    nominal_stage_ms=nominal,
                )
            if rel.status != "ACTIVE":
                reject("TEMPORARILY_UNAVAILABLE", "RELATIONSHIP_DOWN", relationship_id=rel.id)
            if day in rel.holidays or not rel.open_ms <= clock < rel.cutoff_ms:
                reject("TEMPORARILY_UNAVAILABLE", "OPERATING_WINDOW_CLOSED", relationship_id=rel.id)
            v = tx.position("VOSTRO:" + rel.id)
            n = tx.position("NOSTRO:" + rel.id)
            available = v["posted"] - v["reserved"] - v["buffer"]
            mirror_available = -n["posted"]
            required = s["input_minor"]
            item = {
                "bank": rel.respondent,
                "correspondent": rel.correspondent,
                "account": "VOSTRO:" + rel.id,
                "nostro_account": "NOSTRO:" + rel.id,
                "currency": str(rel.currency),
                "required_minor": required,
                "available_minor": available,
                "nostro_available_minor": mirror_available,
                "reserved_minor": v["reserved"],
                "buffer_minor": v["buffer"],
                "shortfall_minor": max(0, required - available),
                "semantics": "funded respondent deposit at specific correspondent; fees deducted from principal",
            }
            result["liquidity"].append(item)
            if n["posted"] + v["posted"] != 0:
                reject("RECONCILIATION_REQUIRED", "NOSTRO_VOSTRO_MISMATCH", account=item["account"])
            if available < required:
                reject("INSUFFICIENTLY_FUNDED", "LIQUIDITY_SHORTAGE", **item)
            if required > min(rel.maximum_minor, rel.exposure_limit_minor):
                reject("STRUCTURALLY_INELIGIBLE", "AMOUNT_OR_EXPOSURE_LIMIT", relationship_id=rel.id)
            exposure = tx.one(
                "SELECT COALESCE(sum(amount),0)::bigint n FROM cb_obligations WHERE run_id=:r AND debtor=:b AND currency=:c AND status='OPEN'",
                b=rel.correspondent,
                c=str(rel.currency),
            )["n"]
            item["open_exposure_minor"] = exposure
            if i < len(path) - 1 and exposure + s["output_minor"] > rel.exposure_limit_minor:
                reject(
                    "INSUFFICIENTLY_FUNDED",
                    "AGGREGATE_EXPOSURE_LIMIT",
                    bank=rel.correspondent,
                    currency=str(rel.currency),
                    available_minor=max(0, rel.exposure_limit_minor - exposure),
                    required_minor=s["output_minor"],
                )
            result["fee_details"].append(
                {
                    "hop": i,
                    "bank": rel.correspondent,
                    "recipient": f"GL:{rel.correspondent}:FEES:{rel.currency}",
                    "currency": str(rel.currency),
                    "amount_minor": s["fee_minor"],
                    "fixed_minor": rel.fixed_fee_minor,
                    "bps": rel.variable_fee_bps,
                    "rounding": "FLOOR_MINOR",
                    "charge_to": "BENEFICIARY",
                    "input_minor": required,
                    "output_minor": s["output_minor"],
                }
            )
        return result

    def execute(self, world, command, at_ms):
        self.rail.create(world, command, at_ms)
        return self.rail.advance(world, command.idempotency_key, at_ms)

    def inspect(self, world, payment):
        from railstorm.cb_simulation import chain_inspection

        with self.store.transaction(world) as tx:
            view = chain_inspection(tx, payment)
            fees = tx.rows(
                "SELECT a.currency,l.account_id,sum(l.delta)::bigint amount_minor FROM journal_lines l JOIN accounts a ON a.run_id=l.run_id AND a.id=l.account_id JOIN journal_entries j ON j.run_id=l.run_id AND j.id=l.entry_id WHERE l.run_id=:r AND j.payment_id=:p AND a.id LIKE 'GL:%:FEES:%' GROUP BY a.currency,l.account_id ORDER BY l.account_id",
                p=payment,
            )
            return {
                "chain": view,
                "realized_fee_details": fees,
                "realized_fees_by_currency": {
                    c: sum(r["amount_minor"] for r in fees if r["currency"] == c)
                    for c in sorted({r["currency"] for r in fees})
                },
                "logical_ms": tx.run["logical_time_ms"],
            }
