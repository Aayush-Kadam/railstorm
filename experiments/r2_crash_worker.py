"""Terminate a real worker before destination financial recovery begins."""

import os
import sys

from railstorm.rails.instant_link import InstantLink
from railstorm.store import Store

if __name__ == "__main__":
    # Exit before invoking the financial recovery action; durable pending state must survive.
    store = Store()
    rail = InstantLink(store)
    state = rail.inspect(sys.argv[1], "payment")["payment"]
    assert state["source_final"] and not state["destination_final"]
    os._exit(74)
