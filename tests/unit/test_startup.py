"""Startup: work that can happen later or elsewhere must not make the window wait.

Measured before these changes: probing every optional library for `ward --check` took 200 ms even though only the
required ones decide whether Ward can start; importing the OMEMO libraries took 170 ms on the main thread; and every
account's TLS context loaded the CA store twice.  The tray's properties were the worst: each of the fourteen
`GetAll` answers packed both icon pixmaps, wrapping every byte in a Python object, which froze the main loop for
about 1.6 s right after the window was drawn.
"""

import os
import ssl
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_missing_required_does_not_probe_optional_features(monkeypatch):
    from ward import deps

    def boom(*_a, **_k):
        raise AssertionError("an optional feature was probed on the startup path")

    for name in ("features", "omemo_available", "legacy_omemo_available", "have"):
        monkeypatch.setattr(deps, name, boom)
    assert deps.missing_required() == []


def test_renderer_choice(monkeypatch):
    from ward import main

    env = dict(os.environ)
    monkeypatch.setattr(os, "environ", env)
    env.pop("GSK_RENDERER", None)
    env.pop("GDK_DISABLE", None)

    monkeypatch.setenv("WARD_RENDERER", "opengl")
    main._choose_renderer()
    assert env["GSK_RENDERER"] == "gl"          # "ngl" was renamed in GTK 4.18 and warns
    assert "vulkan" in env["GDK_DISABLE"].split(",")

    env.pop("GSK_RENDERER", None)
    env.pop("GDK_DISABLE", None)
    monkeypatch.setenv("WARD_RENDERER", "software")
    main._choose_renderer()
    assert env["GSK_RENDERER"] == "cairo"
    disabled = env["GDK_DISABLE"].split(",")
    assert "gl" in disabled and "vulkan" in disabled

    env["GDK_DISABLE"] = "all"          # values after "all" would be exceptions to it
    main._software_only("gl")
    assert env["GDK_DISABLE"] == "all"


def test_tls_contexts_are_shared():
    from ward.core.session import base_ssl_context

    assert base_ssl_context() is base_ssl_context(), "loading the CA store again for every account"
    assert base_ssl_context(fresh=True) is not base_ssl_context(fresh=True)
    unverified = base_ssl_context(verify=False)
    assert unverified.check_hostname is False and unverified.verify_mode == ssl.CERT_NONE


def _python(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([ROOT, os.environ.get("PYTHONPATH", "")]))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)


def test_omemo_xml_schemas_are_loaded_when_the_manager_starts():
    from ward import deps

    if not deps.omemo_available():
        pytest.skip("OMEMO libraries not installed")
    code = (
        "import sys\n"
        "import ward.extras as e\n"
        "e.activate()\n"
        "import ward.core.omemo.service as service\n"
        "print('imported', 'twomemo.etree' in sys.modules)\n"
        "service._load_etree()\n"
        "print('loaded', 'twomemo.etree' in sys.modules)\n"
    )
    out = _python(code)
    assert out.returncode == 0, out.stderr
    # twomemo.etree compiles three XML schemas on import: that is done by _load_etree in the worker pool, when the
    # first OMEMO manager starts, not by importing the service at startup
    assert out.stdout.split() == ["imported", "False", "loaded", "True"], out.stdout


def test_core_creates_omemo_after_the_preload(run):
    from ward import deps
    from ward.core.core import Core
    from ward.core.storage.db import Database

    core = Core(db=Database(":memory:"), connect=False)
    assert core.omemo is None, "the libraries are imported while the window is built"
    seen = []
    core.connect("omemo-ready", lambda *_: seen.append(core.omemo))
    run(core.load_omemo())
    assert seen and seen[0] is core.omemo
    if deps.omemo_available():
        assert core.omemo is not None and not core.omemo_error
    else:
        assert core.omemo is None and core.omemo_error
