"""Run with python -m railstorm.cli; outputs are computed from PostgreSQL."""

import argparse
import json
from pathlib import Path

from railstorm.audit import reconcile, replay_check, snapshot
from railstorm.domain import PaymentCommand
from railstorm.ledger import Ledger
from railstorm.store import Store


def main():
    parser = argparse.ArgumentParser(description="RAILSTORM synthetic settlement laboratory")
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("initial", "golden"):
        p = commands.add_parser(name)
        p.add_argument("run_id")
        p.add_argument("--drop-ack", action="store_true")
    for name in ("replay", "reconcile", "export"):
        p = commands.add_parser(name)
        p.add_argument("run_id")
    p = commands.add_parser("run")
    p.add_argument("scenario")
    p.add_argument("run_id")
    p = commands.add_parser("compare")
    p.add_argument("scenario")
    p.add_argument("prefix")
    p = commands.add_parser("recover-reservations")
    p.add_argument("run_id")
    p.add_argument("at_ms", type=int)
    p = commands.add_parser("chain")
    p.add_argument("run_id")
    p.add_argument("payment_id")
    p = commands.add_parser("correspondent")
    p.add_argument("scenario")
    p.add_argument("run_id")
    args = parser.parse_args()
    store = Store()
    ledger = Ledger(store)
    if args.operation in ("initial", "golden"):
        ledger.initialize(args.run_id)
        fields = (
            {}
            if args.operation == "golden"
            else {"destination_currency": "USD", "receiver_bank_id": "STERLING_UK"}
        )
        cmd = PaymentCommand.model_validate({"idempotency_key": f"{args.operation}-0001", **fields})
        first = ledger.submit(args.run_id, cmd, drop_ack=args.drop_ack)
        retry = ledger.submit(args.run_id, cmd, drop_ack=args.drop_ack)
        with store.transaction(args.run_id) as tx:
            state = snapshot(tx)
        result = {
            "payment": first,
            "duplicate_returned_same_result": retry == first,
            "positions_minor": {a: p for a, p in state["positions"].items() if p["posted"]},
            "journals": state["journals"],
            "verification": reconcile(store, args.run_id),
        }
    elif args.operation == "replay":
        result = replay_check(store, args.run_id)
    elif args.operation == "reconcile":
        result = reconcile(store, args.run_id)
    elif args.operation == "export":
        with store.transaction(args.run_id) as tx:
            result = {
                "config": tx.run,
                "state": snapshot(tx),
                "events": tx.rows("SELECT * FROM events WHERE run_id=:r ORDER BY seq"),
            }
    elif args.operation == "recover-reservations":
        from railstorm.recovery import recover_reservations

        result = recover_reservations(store, args.run_id, args.at_ms)
    elif args.operation == "chain":
        from railstorm.cb_simulation import chain_inspection

        with store.transaction(args.run_id) as tx:
            result = chain_inspection(tx, args.payment_id)
    elif args.operation == "correspondent":
        from railstorm.cb_simulation import CorrespondentScenario, create_correspondent_run
        from railstorm.simulation import metrics, step

        cb_scenario = CorrespondentScenario.model_validate_json(Path(args.scenario).read_text())
        create_correspondent_run(store, args.run_id, cb_scenario)
        while not step(store, args.run_id, 50)["done"]:
            pass
        result = {"metrics": metrics(store, args.run_id), "verification": reconcile(store, args.run_id)}
    else:
        from railstorm.simulation import Scenario, compare, run_scenario

        scenario = Scenario.model_validate(json.loads(Path(args.scenario).read_text()))
        result = (
            run_scenario(store, args.run_id, scenario)
            if args.operation == "run"
            else compare(store, args.prefix, scenario)
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
