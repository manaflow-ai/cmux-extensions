"""Tests of compat/compat.py's pure parts: the feed, folding findings, the diff and the summary."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import compat  # noqa: E402

FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle" version="2.0"
     xmlns:cmux="https://cmux.com/xml-namespaces/cmux"><channel>
  <item><sparkle:version>200</sparkle:version><sparkle:shortVersionString>1.0.0-nightly.200</sparkle:shortVersionString>
    <cmux:sourceCommit>""" + "b" * 40 + """</cmux:sourceCommit><pubDate>Wed</pubDate></item>
  <item><sparkle:version>300</sparkle:version><sparkle:shortVersionString>1.0.0-nightly.300</sparkle:shortVersionString>
    <pubDate>Thu</pubDate></item>
  <item><sparkle:version>100</sparkle:version><sparkle:shortVersionString>1.0.0-nightly.100</sparkle:shortVersionString>
    <cmux:sourceCommit>""" + "a" * 40 + """</cmux:sourceCommit></item>
</channel></rss>"""


def repo(rid, status, findings=()):
    return {"id": rid, "name": rid, "repo": f"o/{rid}", "kind": "setup", "status": status,
            "commit": "c" * 40, "findings": [dict(f) for f in findings]}


UNKNOWN = {"stages": ["settings"], "code": "unknown_setting", "key": "chat.density",
           "message": "the setting chat.density is not one this cmux has"}


class Feed(unittest.TestCase):
    def test_newest_first_and_commit(self):
        builds = compat.parse_feed(FEED)
        self.assertEqual([b["build"] for b in builds], ["300", "200", "100"])
        self.assertEqual(builds[0]["sha"], "")
        self.assertEqual(builds[1]["sha"], "b" * 40)

    def test_resolve_skips_a_build_without_a_commit(self):
        original = compat.fetch
        compat.fetch = lambda url, timeout=30: FEED.encode()
        try:
            self.assertEqual(compat.resolve("feed")["build"], "200")
            self.assertEqual(compat.resolve("feed", "a" * 40)["version"], "1.0.0-nightly.100")
            self.assertEqual(compat.resolve("feed", "d" * 40)["build"], "")
            with self.assertRaises(SystemExit):
                compat.resolve("feed", "nope")
        finally:
            compat.fetch = original


class Fold(unittest.TestCase):
    def test_install_refusal_joins_the_scan_finding(self):
        folded = compat.fold([
            {"stage": "settings", "code": "unknown_setting", "key": "chat.density",
             "message": "the setting chat.density is not one this cmux has; it is now chat.rows"},
            {"stage": "plan", "code": "profile", "key": None,
             "message": "the setting chat.density is not one this cmux has"},
            {"stage": "plan", "code": "setup", "message": "this setup is format 2"},
        ])
        self.assertEqual(len(folded), 2)
        self.assertEqual(folded[0]["stages"], ["settings", "plan"])
        self.assertIn("now chat.rows", folded[0]["message"])
        self.assertIsNone(folded[1]["key"])

    def test_key_from_message(self):
        self.assertEqual(compat.finding_key({"message": "the profile's value for a.b is not one"}), "a.b")


class Diff(unittest.TestCase):
    def test_first_report_raises_every_failure(self):
        changes = compat.diff(None, [repo("x", "fail", [UNKNOWN]), repo("y", "pass")])
        self.assertEqual(changes["broke"], ["x"])
        self.assertEqual(changes["added"], ["x", "y"])

    def test_broke_fixed_new_findings_removed(self):
        other = dict(UNKNOWN, key="sidebar.width", message="the setting sidebar.width is not one this cmux has")
        previous = {"repos": [repo("a", "pass"), repo("b", "fail", [UNKNOWN]), repo("c", "fail", [UNKNOWN]),
                              repo("gone", "pass")]}
        now = [repo("a", "fail", [UNKNOWN]), repo("b", "pass"), repo("c", "fail", [UNKNOWN, other]),
               repo("new", "fail", [UNKNOWN])]
        changes = compat.diff(previous, now)
        self.assertEqual(changes["broke"], ["a", "new"])
        self.assertEqual(changes["fixed"], ["b"])
        self.assertEqual(changes["new_findings"], {"c": ["sidebar.width"]})
        self.assertEqual(changes["removed"], ["gone"])

    def test_an_error_is_never_breakage(self):
        flake = {"stages": ["fetch"], "code": "clone_failed", "key": None, "message": "could not clone"}
        changes = compat.diff({"repos": [repo("a", "pass")]}, [repo("a", "error", [flake])])
        self.assertEqual((changes["broke"], changes["errors"]), ([], ["a"]))
        # Back to pass after an error is not "fixed": it never failed.
        self.assertEqual(compat.diff({"repos": [repo("a", "error", [flake])]}, [repo("a", "pass")])["fixed"], [])

    def test_signature_ignores_run_paths_and_keeps_the_code(self):
        one = {"code": "customize.io", "message": "could not write /tmp/compat-home-abc123/.config/x"}
        two = {"code": "customize.io", "message": "could not write /tmp/compat-home-zz9/.config/x"}
        self.assertEqual(compat.signature(one), compat.signature(two))
        refused = dict(UNKNOWN, code="value_refused")
        self.assertNotEqual(compat.signature(UNKNOWN), compat.signature(refused))


class Render(unittest.TestCase):
    def test_new_breakage_comes_first(self):
        previous = {"schema": 1, "nightly": {"sha": "a" * 40, "build": "100", "version": "v100"},
                    "repos": [repo("a", "pass"), repo("b", "pass")]}
        report = compat.build_report({"sha": "b" * 40, "build": "200", "version": "v200"},
                                     {"mode": "harness", "description": "the harness"},
                                     [repo("a", "fail", [UNKNOWN]), repo("b", "pass")], previous)
        text = compat.render(report)
        self.assertLess(text.index("### New breakage"), text.index("### Every published setup"))
        self.assertIn("`chat.density`: `the setting chat.density is not one this cmux has` (settings)", text)
        self.assertIn("1 of 2 pass", text)
        self.assertIn("v100", text)


class Source(unittest.TestCase):
    def test_deps_and_includes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tui = root / "cmux-tui"
            (tui / "crates/a/src").mkdir(parents=True)
            (tui / "Cargo.toml").write_text(
                '[workspace.dependencies]\nb = { path = "crates/b" }\nserde = "1"\n')
            (tui / "crates/a/Cargo.toml").write_text(
                "[dependencies]\nb.workspace = true\nserde.workspace = true\n"
                'own = { path = "../own" }\n'
                "[dependencies.d]\nworkspace = true\n"
                "[target.'cfg(unix)'.dependencies]\ne = { workspace = true }\n"
                "[dev-dependencies]\nc.workspace = true\n")
            (tui / "crates/a/src/lib.rs").write_text(
                'const S: &str = include_str!("../../../../schemas/settings/s.json");\n'
                'const T: &str = include_str!(concat!("../../b/interfaces/", "x", "/1.json"));\n'
                'const U: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/data/u.json"));\n')
            self.assertEqual(compat.workspace_paths(tui), {"b": "crates/b"})
            self.assertEqual(compat.crate_deps(tui / "crates/a/Cargo.toml"),
                             ({"b", "serde", "d", "e"}, {"../own"}))
            self.assertEqual(compat.needed_dirs(root, {"cmux-tui/crates/a"}),
                             {"schemas/settings", "cmux-tui/crates/b/interfaces", "cmux-tui/crates/a/data"})


class Untrusted(unittest.TestCase):
    def test_feed_text_is_validated(self):
        feed = FEED.replace("<sparkle:version>200</sparkle:version>",
                            "<sparkle:version>200\nalready_checked=true</sparkle:version>")
        forged = [b for b in compat.parse_feed(feed) if b["sha"] == "b" * 40]
        self.assertEqual([b["build"] for b in forged], [""])

    def test_outputs_refuse_a_newline(self):
        with tempfile.NamedTemporaryFile("r") as out:
            compat.write_outputs(out.name, {"sha": "a" * 40, "n": 2})
            self.assertEqual(out.read(), f"sha={'a' * 40}\nn=2\n")
            with self.assertRaises(SystemExit):
                compat.write_outputs(out.name, {"build": "1\nx=y"})

    def test_messages_are_inert(self):
        self.assertEqual(compat.code_span("hi @someone\n`x`"), "`hi @someone 'x'`")


if __name__ == "__main__":
    unittest.main()
