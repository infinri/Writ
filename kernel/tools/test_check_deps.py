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


class Licences(unittest.TestCase):
    def test_licence_approval_is_package_specific(self):
        allow = {"approved_licenses": ["MIT"], "forbidden": [], "packages": {
            "subtle": {"version": "2.6.1", "licence_approved": "BSD-3-Clause"}, "other": {"version": "1.0.0"}}}
        pkgs = [{"name": "subtle", "version": "2.6.1"}, {"name": "other", "version": "1.0.0"}]
        lic = {("subtle", "2.6.1"): "BSD-3-Clause", ("other", "1.0.0"): "BSD-3-Clause"}
        problems = check_deps.check(pkgs, allow, lic)
        self.assertEqual(problems, ["other 1.0.0: licence 'BSD-3-Clause' not approved"])


class Features(unittest.TestCase):
    def test_bundled_sqlite_feature_is_refused(self):
        allow = {"forbidden_features": {"libsqlite3-sys": ["bundled"]}}
        meta = {"packages": [{"id": "x", "name": "libsqlite3-sys"}],
                "resolve": {"nodes": [{"id": "x", "features": ["bundled", "min_sqlite_version_3_14_0"]}]}}
        self.assertEqual(len(check_deps.forbidden_features(meta, allow)), 1)
        meta["resolve"]["nodes"][0]["features"] = []
        self.assertEqual(check_deps.forbidden_features(meta, allow), [])


class Versions(unittest.TestCase):
    def test_versioned_keys_approve_each_version_exactly(self):
        allow = {"approved_licenses": ["MIT"], "forbidden": [], "packages": {
            "syn@2.0.1": {"version": "2.0.1"}, "syn@3.0.0": {"version": "3.0.0"}}}
        lic = {("syn", "2.0.1"): "MIT", ("syn", "3.0.0"): "MIT", ("syn", "3.0.1"): "MIT"}
        both = [{"name": "syn", "version": "2.0.1"}, {"name": "syn", "version": "3.0.0"}]
        self.assertEqual(check_deps.check(both, allow, lic), [])
        other = [{"name": "syn", "version": "2.0.1"}, {"name": "syn", "version": "3.0.1"}]
        problems = check_deps.check(other, allow, lic)
        self.assertTrue(any("not on the allow-list: syn 3.0.1" in x for x in problems))
        self.assertTrue(any("syn@3.0.0 is not in Cargo.lock" in x for x in problems))

    def test_dr5_licence_is_exact_to_package_and_version(self):
        allow = json.loads((check_deps.ROOT / "approval/deps-allowlist.json").read_text())
        expr = "(MIT OR Apache-2.0) AND Unicode-3.0"
        entry = allow["packages"]["unicode-ident"]
        self.assertEqual((entry["version"], entry["licence_approved"]), ("1.0.27", expr))
        self.assertEqual(allow["dev_packages"]["unicode-normalization"]["version"], "0.1.26")
        self.assertNotIn("unicode-normalization", allow["packages"])
        self.assertNotIn(expr, allow["approved_licenses"])
        bumped = [{"name": "unicode-ident", "version": "1.0.28"}]
        problems = check_deps.check(bumped, {**allow, "packages": {"unicode-ident": entry}}, {("unicode-ident", "1.0.28"): expr})
        self.assertTrue(any("approved 1.0.27" in x for x in problems))


class Advisories(unittest.TestCase):
    EXC = {"rsa@0.10.0-rc.19": {"ids": ["RUSTSEC-2023-0071"], "scope": "DEVELOPMENT-ONLY", "decision": "D-041"}}

    def test_recorded_exception_is_visible_not_failing_in_development(self):
        problems, notes = check_deps.advisories({("rsa", "0.10.0-rc.19"): ["RUSTSEC-2023-0071"]}, self.EXC, False)
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 1)

    def test_release_mode_new_advisory_and_other_version_fail(self):
        hit = {("rsa", "0.10.0-rc.19"): ["RUSTSEC-2023-0071"]}
        self.assertEqual(len(check_deps.advisories(hit, self.EXC, True)[0]), 1)
        self.assertEqual(len(check_deps.advisories({("rsa", "0.10.0-rc.19"): ["RUSTSEC-2023-0071", "RUSTSEC-2099-0001"]},
                                                   self.EXC, False)[0]), 1)
        self.assertEqual(len(check_deps.advisories({("rsa", "0.10.0-rc.20"): ["RUSTSEC-2023-0071"]}, self.EXC, False)[0]), 1)


class ThroughMembers(unittest.TestCase):
    def test_forbidden_crate_through_a_workspace_member_fails(self):
        allows = {"writ-approval": {"forbidden": [], "forbidden_anywhere": ["rusqlite"]}, "writ-ledger-sqlite": {"forbidden": ["hmac"]}}
        full = {"writ-approval": {"writ-ledger-sqlite", "rusqlite", "serde"}, "writ-ledger-sqlite": {"rusqlite", "hmac"}}
        self.assertEqual(check_deps.forbidden_through_members(full, allows),
                         ["writ-approval: forbidden crate rusqlite reached (directly or through a workspace member)"])
        full["writ-approval"].discard("rusqlite")
        self.assertEqual(check_deps.forbidden_through_members(full, allows), [])

    # M5a-11 (J-0 feature-unification note): source-level rules replace the forbidden features
    # that workspace feature unification would otherwise trip.
    def test_broker_source_may_use_rustix_time_only(self):
        ok = ("use rustix::time::{clock_gettime_dynamic, DynamicClockId};\n"
              "let t = rustix :: time::clock_gettime_dynamic(x); // rustix::process in a comment\n"
              "/* rustix::fs::open */ let s = \"rustix::fs\";\n")
        self.assertEqual(check_deps.rustix_time_only(ok), [])
        for bad in ("use rustix::fs::open;", "rustix::process::getpid()", "use rustix::{time, fs};",
                    "use rustix;", "use rustix::*;", "extern crate rustix as r;", "use ::rustix::net::x;"):
            with self.subTest(bad=bad):
                self.assertNotEqual(check_deps.rustix_time_only(bad), [])

    def test_approval_source_never_uses_tokio_signal(self):
        ok = ("use tokio::net::TcpListener; // tokio::signal is not used\n"
              "let r = tokio::runtime::Builder::new_current_thread(); let s = \"tokio::signal\";\n"
              "use tokio::{net, time};\n")
        self.assertEqual(check_deps.no_tokio_signal(ok), [])
        for bad in ("tokio::signal::ctrl_c()", "use tokio::signal::unix::{signal, SignalKind};",
                    "use tokio::{net, signal};", "use tokio::{\n  signal::unix,\n};", "use tokio::*;",
                    "use ::tokio :: signal;", "use tokio as t;"):
            with self.subTest(bad=bad):
                self.assertNotEqual(check_deps.no_tokio_signal(bad), [])

    def test_manifest_features_are_checked_per_member(self):
        good = '[dependencies]\nrustix = { version = "=1.1.6", default-features = false, features = ["time"] }\n'
        wide = '[dependencies]\nrustix = { version = "=1.1.6", default-features = false, features = ["time", "fs"] }\n'
        default = '[dependencies]\nrustix = { version = "=1.1.6", features = ["time"] }\n'
        self.assertEqual(check_deps.manifest_features(good, "rustix", {"time"}), [])
        self.assertNotEqual(check_deps.manifest_features(wide, "rustix", {"time"}), [])
        self.assertNotEqual(check_deps.manifest_features(default, "rustix", {"time"}), [])

    def test_this_repository_passes_the_source_rules(self):
        self.assertEqual(check_deps.source_rules(check_deps.ROOT), [])


if __name__ == "__main__":
    unittest.main()

