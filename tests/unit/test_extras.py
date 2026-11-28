"""Unit tests for ward.extras OMEMO installation and abi3 xeddsa fallback."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ward import extras


class ExtrasInstallTests(unittest.TestCase):
    def test_pkg_name_and_extras(self):
        self.assertEqual(extras._pkg_name("twomemo[xml]>=1.0"), "twomemo")
        self.assertEqual(extras._pkg_name("markdown-it-py>=2"), "markdown-it-py")
        self.assertEqual(extras._pkg_name("XEdDSA>=1.0"), "xeddsa")
        self.assertEqual(extras._pkg_extras("twomemo[xml]>=1.0"), {"xml"})
        self.assertEqual(extras._pkg_extras("omemo>=1.0"), set())

    def test_install_uses_only_binary_xeddsa_first(self):
        calls: list[list[str]] = []

        def fake_run(args, on_line=None):
            calls.append(args)
            return 0, []

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(extras, "_run_pip", side_effect=fake_run), \
                 patch.object(extras, "activate"), \
                 patch.object(extras, "check"):
                extras.install([*extras.OMEMO, *extras.LEGACY_OMEMO], target=tmp)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["--target", tmp, "--only-binary", "xeddsa"])

    def test_install_falls_back_to_abi3_xeddsa_wheel(self):
        calls: list[list[str]] = []

        def fake_run(args, on_line=None):
            calls.append(args)
            if len(calls) == 1:
                return 1, ["ERROR: No matching distribution found for xeddsa>=1.0"]
            return 0, []

        fake_dists = [
            SimpleNamespace(
                metadata={"Name": "omemo"},
                requires=["x3dh (>=1.0)", "xeddsa (>=1.0)", "typing-extensions (>=4.3.0)"],
            ),
            SimpleNamespace(
                metadata={"Name": "twomemo"},
                requires=["omemo (>=1.0)", "protobuf (>=3.20.3) ; extra == 'xml'", "pytest ; extra == 'dev'"],
            ),
            SimpleNamespace(
                metadata={"Name": "xeddsa"},
                requires=["cffi (>=1.14.5)"],
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(extras, "_run_pip", side_effect=fake_run), \
                 patch.object(extras.importlib.metadata, "distributions", return_value=fake_dists), \
                 patch.object(extras, "activate"), \
                 patch.object(extras, "check"):
                extras.install(["linkify-it-py", *extras.OMEMO], target=tmp, verify=False)

        self.assertEqual(len(calls), 4)
        # 1) Initial --only-binary xeddsa attempt
        self.assertIn("--only-binary", calls[0])
        # 2) Prebuilt cp313 abi3 wheel for xeddsa only
        self.assertEqual(
            calls[1],
            ["--target", tmp, "--only-binary=:all:", "--python-version", "3.13", "--no-deps", "xeddsa>=1.0"],
        )
        # 3) Pure-Python OMEMO packages with --no-deps
        self.assertEqual(
            calls[2],
            ["--target", tmp, "--no-deps", "omemo>=1.0", "twomemo[xml]>=1.0", "x3dh>=1.0", "doubleratchet>=1.0"],
        )
        # 4) Native dependencies + non-OMEMO packages, excluding xeddsa/omemo and unselected extras
        self.assertEqual(
            calls[3],
            ["--target", tmp, "linkify-it-py", "typing-extensions>=4.3.0", "protobuf>=3.20.3", "cffi>=1.14.5"],
        )

    def test_install_raises_when_all_attempts_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(extras, "_run_pip", return_value=(1, ["boom"])):
                with self.assertRaises(extras.InstallError):
                    extras.install(extras.OMEMO, target=tmp, verify=False)


if __name__ == "__main__":
    unittest.main()
