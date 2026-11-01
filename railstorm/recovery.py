"""Bounded, explicit virtual-time recovery of abandoned shared-book holds."""

from railstorm.domain import DomainError
from railstorm.ledger import Ledger
from railstorm.store import Store


def recover_reservations(store: Store, run_id: str, at_ms: int, limit: int = 100):
    if type(at_ms) is not int or at_ms < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise DomainError("RECOVERY_LIMIT")
    with store.transaction(run_id) as tx:
        tx.time(at_ms)
        identities = tx.rows(
            """SELECT DISTINCT payment_id FROM reservations WHERE run_id=:r
          AND status='ACTIVE' AND expires_ms<=:t ORDER BY payment_id LIMIT :n""",
            t=at_ms,
            n=limit,
        )
    actions = []
    for row in identities:
        identity = row["payment_id"]
        try:
            with store.transaction(run_id) as tx:
                payment = tx.payment(identity)
                decision = tx.one(
                    "SELECT decision FROM decisions WHERE run_id=:r AND payment_id=:p", p=identity
                )
                if decision and decision["decision"] == "COMMIT":
                    actions.append(
                        {
                            "payment_id": identity,
                            "action": "BLOCKED_COMMIT",
                            "severity": "ERROR",
                            "reason": "Preserve holds; investigate/complete durable decision, never timeout-abort",
                        }
                    )
                elif (
                    payment
                    and payment["rail"] == "ATOMIC_PVP"
                    and payment["economic_status"] == "PREPARED"
                    and decision
                    and decision["decision"] == "PREPARED"
                ):
                    result = Ledger(store).settle(run_id, identity, at_ms)
                    actions.append(
                        {"payment_id": identity, "action": result["economic_status"], "severity": "INFO"}
                    )
                elif (
                    payment
                    and payment["economic_status"] in ("EXPIRED", "ABORTED")
                    and decision
                    and decision["decision"] == "ABORT"
                ):
                    tx.time(at_ms)
                    tx.resolve(identity, "RELEASED")
                    actions.append({"payment_id": identity, "action": "RELEASE_ABORTED", "severity": "INFO"})
                else:
                    actions.append(
                        {"payment_id": identity, "action": "BLOCKED_OWNER_STATE", "severity": "ERROR"}
                    )
        except DomainError as exc:
            actions.append(
                {
                    "payment_id": identity,
                    "action": "BLOCKED_VALIDATION",
                    "severity": "ERROR",
                    "reason": exc.code,
                }
            )
    return {
        "at_ms": at_ms,
        "limit": limit,
        "examined": len(identities),
        "actions": actions,
        "errors": sum(a["severity"] == "ERROR" for a in actions),
        "scope": "ATOMIC_PVP; COMMIT is never timeout-aborted",
    }
