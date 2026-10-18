"""Shared test setup: every test gets a private WARD_HOME, and `run(coro)` runs a coroutine on Ward's GLib loop."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# libraries install.sh fetched with pip (WARD_VENDOR), as the installed launcher would find them
if os.environ.get("WARD_VENDOR") and os.path.isdir(os.environ["WARD_VENDOR"]):
    sys.path.append(os.environ["WARD_VENDOR"])

os.environ.setdefault("WARD_LOG", "warning")
os.environ["GSETTINGS_BACKEND"] = "memory"


@pytest.fixture(autouse=True)
def ward_home(tmp_path, monkeypatch):
    home = tmp_path / "ward-home"
    home.mkdir()
    monkeypatch.setenv("WARD_HOME", str(home))
    # never touch the developer's real keyring or GnuPG home
    monkeypatch.setenv("WARD_SECRETS", "memory")
    yield home


@pytest.fixture
def loop():
    from ward import aio

    lp = aio.install()
    yield lp


@pytest.fixture
def run(loop):
    def _run(coro, timeout=30):
        import asyncio

        return loop.run_until_complete(asyncio.wait_for(coro, timeout))

    return _run
