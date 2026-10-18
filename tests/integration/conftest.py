import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import ProsodyServer  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def prosody():
    if not ProsodyServer.available():
        pytest.skip("prosody is not installed")
    srv = ProsodyServer(loglevel=os.environ.get("WARD_PROSODY_LOG", "info"))
    srv.start()
    yield srv
    if os.environ.get("WARD_KEEP_PROSODY"):
        print("prosody kept in", srv.dir)
        return
    srv.stop()


@pytest.fixture
def trust_test_ca(prosody, monkeypatch):
    monkeypatch.setenv("WARD_EXTRA_CA", prosody.cert)
    return prosody
