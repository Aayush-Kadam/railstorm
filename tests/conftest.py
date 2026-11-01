import os
import uuid

import pytest

from railstorm.ledger import Ledger
from railstorm.store import Store


@pytest.fixture
def store():
    if not os.environ.get("RAILSTORM_DATABASE_URL"):
        pytest.skip("Real disposable PostgreSQL required: set RAILSTORM_DATABASE_URL")
    instance = Store()
    yield instance
    instance.engine.dispose()


@pytest.fixture
def world(store):
    run = "test-" + uuid.uuid4().hex
    ledger = Ledger(store)
    ledger.initialize(run)
    return ledger, run
