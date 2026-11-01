"""Verify copied exports against an externally retained count and chain head."""

from railstorm.domain import digest


def verify_export(events, expected_count, expected_head):
    previous = "0" * 64
    versions: dict[str, int] = {}
    for seq, event in enumerate(events, 1):
        aggregate = event["aggregate_id"]
        versions[aggregate] = versions.get(aggregate, 0) + 1
        value = {k: v for k, v in event.items() if k not in ("run_id", "event_hash")}
        if (
            event["seq"] != seq
            or event["previous_hash"] != previous
            or event["aggregate_version"] != versions[aggregate]
            or event["schema_version"] != 1
            or digest(value) != event["event_hash"]
        ):
            return {"match": False, "first_bad_sequence": seq}
        previous = event["event_hash"]
    return {"match": len(events) == expected_count and previous == expected_head, "first_bad_sequence": None}
