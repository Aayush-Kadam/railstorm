"""Generate inspectable deterministic CB scenarios and their independent expectations."""

import json
from pathlib import Path

from railstorm.cb_simulation import CorrespondentScenario

out = Path("scenarios/correspondent")
out.mkdir(parents=True, exist_ok=True)
expected = {}
for i in range(1, 11):
    name = f"CB-{i:02d}"
    config = CorrespondentScenario(scenario_id=name).model_dump(mode="json")
    status, exposure, beneficiary, fees, posted = "SETTLED", {}, 9825, {"USD": 175}, 3

    def fault(kind, target, at=151, duration=5000):
        config["faults"] = [
            {"id": name + "-fault", "type": kind, "target": target, "at_ms": at, "duration_ms": duration}
        ]

    if i == 2:
        fault("PARTICIPANT_DOWN", "STERLING_UK", 0)
        status, beneficiary, fees, posted = "QUEUED", 0, {}, 0
    if i in (3, 4):
        fault("PARTICIPANT_DOWN" if i == 3 else "MESSAGE_DROP", "RHINE_EU")
        status, exposure, beneficiary, fees, posted = "PENDING_EXPOSURE", {"USD": 9900}, 0, {"USD": 100}, 1
    if i == 5:
        fault("DUPLICATE_DELIVERY", "RHINE_EU", 0, 500)
    if i == 6:
        for r in config["relationships"]:
            if r["id"] in ("SR_USD", "RM_USD"):
                r["funding_minor"] = 1
        status, exposure, beneficiary, fees, posted = "PENDING_EXPOSURE", {"USD": 9900}, 0, {"USD": 100}, 1
    if i == 7:
        config["relationships"][0]["cutoff_ms"] = 50
        config["deadline_ms"] = 200
        status, beneficiary, fees, posted = "EXPIRED", 0, {}, 0
    if i == 8:
        for r in config["relationships"]:
            r["variable_fee_bps"] = 10
        # Sequential floor: 100+10; 50+9; 25+9.
        beneficiary, fees = 9797, {"USD": 203}
    if i == 9:
        config["destination_currency"] = "SGD"
        fault("PARTICIPANT_DOWN", "MERIDIAN_SG", 151)
        # USD 9900 * 133 / 100 = SGD 13167; Rhine charges 50 before final outage.
        status, exposure, beneficiary, fees, posted = (
            "PENDING_EXPOSURE",
            {"SGD": 13117},
            0,
            {"USD": 100, "SGD": 50},
            3,
        )
    if i == 10:
        fault("PARTICIPANT_DOWN", "RHINE_EU", 151, 649)
    (out / (name + ".json")).write_text(json.dumps(config, indent=2), encoding="utf-8")
    expected[name] = {
        "economic_status": status,
        "open_exposure_minor": exposure,
        "beneficiary_minor": beneficiary,
        "fees_minor": fees,
        "posted_stages": posted,
        "financial_errors": 0,
        "replay_match": True,
        "route": ["ATLAS_US", "STERLING_UK", "RHINE_EU", "MERIDIAN_SG"],
        "initial_state": "Relationship funding and fee/window configuration in scenario JSON; bank GL mirrors funded settlement cash.",
        "postings": "Two balanced bank-domain journals per posted stage; each HOP debits receiving vostro, credits fees and payable/beneficiary, and mirrors the sending nostro. FX uses separate source/target currency journals.",
    }
(out / "expectations.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
