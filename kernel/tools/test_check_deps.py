"""Negative tests for the production dependency gate (M2-01).

  python3 -m unittest discover -s kernel/tools -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_deps  # noqa: E402

ALLOW = json.loads((check_deps.ROOT / "kernel/deps-allowlist.json").read_text())
SHA2 = {"name": "sha2", "version": "0.10.8", "source": "registry", "checksum": ALLOW["packages"]["sha2"]["checksum"]}


def ok_set():
    pkgs = [dict(SHA2)] + [{"name": n, "version": e["version"], "source": "registry"}
                           for n, e in ALLOW["packages"].items() if n != "sha2"]
    lic = {(p["name"], p["version"]): "MIT OR Apache-2.0" for p in pkgs}
    return pkgs, lic


class Gate(unittest.TestCase):
    def test_reviewed_set_passes(self):
        pkgs, lic = ok_set()
        self.assertEqual(check_deps.check(pkgs, ALLOW, lic), [])

    def test_unlisted_forbidden_and_changed_packages_fail(self):
        for extra, needle in (({"name": "hmac", "version": "0.12.1", "source": "registry"}, "forbidden"),
                              ({"name": "leftpad", "version": "1.0.0", "source": "registry"}, "not on the allow-list")):
            pkgs, lic = ok_set()
            pkgs.append(extra)
            lic[(extra["name"], extra["version"])] = "MIT"
            with self.subTest(extra=extra["name"]):
                self.assertTrue(any(needle in x for x in check_deps.check(pkgs, ALLOW, lic)))
        pkgs, lic = ok_set()
        pkgs[0] = dict(SHA2, version="0.10.9")
        lic[("sha2", "0.10.9")] = "MIT OR Apache-2.0"
        self.assertTrue(any("approved 0.10.8" in x for x in check_deps.check(pkgs, ALLOW, lic)))
        pkgs, lic = ok_set()
        pkgs[0] = dict(SHA2, checksum="0" * 64)
        self.assertTrue(any("checksum" in x for x in check_deps.check(pkgs, ALLOW, lic)))

    def test_unapproved_licence_fails(self):
        pkgs, lic = ok_set()
        lic[("typenum", "1.20.1")] = "GPL-3.0"
        self.assertTrue(any("licence" in x for x in check_deps.check(pkgs, ALLOW, lic)))


class Features(unittest.TestCase):
    def test_bundled_sqlite_feature_is_refused(self):
        allow = {"forbidden_features": {"libsqlite3-sys": ["bundled"]}}
        meta = {"packages": [{"id": "x", "name": "libsqlite3-sys"}],
                "resolve": {"nodes": [{"id": "x", "features": ["bundled", "min_sqlite_version_3_14_0"]}]}}
        self.assertEqual(len(check_deps.forbidden_features(meta, allow)), 1)
        meta["resolve"]["nodes"][0]["features"] = []
        self.assertEqual(check_deps.forbidden_features(meta, allow), [])


if __name__ == "__main__":
    unittest.main()

