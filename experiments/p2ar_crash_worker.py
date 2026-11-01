"""Disposable worker: exit after a durable recovery delivery, before recovery completion event."""

import os
import sys

from railstorm.rails.instant_link import InstantLink
from railstorm.store import Store


class CrashAfterDestinationCommit(InstantLink):
    def deliver(self, run, payment, stage, at_ms, available=True, lose_ack=False):
        result = super().deliver(run, payment, stage, at_ms, available=available, lose_ack=True)
        if stage == "DESTINATION":
            os._exit(73)
        return result


if __name__ == "__main__":
    CrashAfterDestinationCommit(Store()).recover(sys.argv[1], sys.argv[2], 40)
    raise RuntimeError("INJECTED_PROCESS_EXIT_DID_NOT_HAPPEN")
