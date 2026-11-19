"""Tests for review.py. Run: python3 -m unittest discover -s review/tests"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import review  # noqa: E402

API = "https://api.github.com"
DL = "https://github.com/dl"

MANIFEST = {
    "manifestVersion": 2,
    "id": "alice/focus",
    "name": "Focus",
    "version": "1.2.0",
    "description": "A focus timer.",
    "engines": {"cmux": ">=0.70.0"},
    "repository": "https://github.com/alice/cmux-focus",
    "runtime": {"main": "dist/main.js"},
    "files": ["dist/"],
    "scopes": {"storage:local": "Keeps the timer length you picked between launches."},
}


def tarball(files: dict[str, bytes], prefix: str = "", link: str | None = None) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(prefix + link)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return out.getvalue()


class FakeGitHub(review.GitHub):
    def __init__(self, responses: dict[str, bytes]):
        super().__init__(None)
        self.responses = responses

    def _open(self, url, accept, limit):
        if url not in self.responses:
            raise review.NotFound(url)
        data = self.responses[url]
        if len(data) > limit:
            raise review.FetchError(f"{url} is larger than {limit // 1024} KiB")
        return data


def j(value) -> bytes:
    return json.dumps(value).encode()


def extension_repo(manifest=MANIFEST, package=None, tag="v1.2.0", asset="cmux-app.v2.json", extra_assets=(), readme=b"# Focus\n![shot](s.png)\n"
                   b"[![Get it on cmux](https://cmux.com/extensions/badge.svg)](https://cmux.com/extensions/alice/cmux-focus)\n"):
    if package is None:
        package = tarball({asset: j(manifest), "dist/main.js": b"globalThis.x = 1;\n"})
    release = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            *({"name": n, "browser_download_url": f"{DL}/other"} for n in extra_assets),
            {"name": asset, "browser_download_url": f"{DL}/m"},
            {"name": "cmux-extension.tar.gz", "browser_download_url": f"{DL}/p"},
        ],
    }
    return {
        f"{API}/repos/alice/cmux-focus/releases?per_page=30": j([release]),
        f"{DL}/m": j(manifest),
        f"{DL}/p": package,
        f"{API}/repos/alice/cmux-focus/readme?ref={tag}": j({"download_url": f"{DL}/readme"}),
        f"{DL}/readme": readme,
    }


ENTRY = {
    "id": "alice/focus",
    "name": "Focus",
    "author": "Alice",
    "description": "A focus timer.",
    "repo": "alice/cmux-focus",
}


def run(entry, responses, kind="extensions.json"):
    return review.review_entry(FakeGitHub(responses), kind, entry, "added")


class ExtensionTests(unittest.TestCase):
    def test_good_extension_passes(self):
        result = run(ENTRY, extension_repo())
        self.assertEqual(result.errors, [])
        self.assertIn("manifest v2 schema", result.checked)
        self.assertIn("package manifest", result.checked)
        self.assertTrue(any("storage:local" in g for g in result.glance))

    def test_package_in_one_top_folder_passes(self):
        package = tarball({"cmux-app.v2.json": j(MANIFEST), "dist/main.js": b"1;\n"}, prefix="focus/")
        self.assertEqual(run(ENTRY, extension_repo(package=package)).errors, [])

    def test_manifest_asset_named_cmux_app_json_passes(self):
        result = run(ENTRY, extension_repo(asset="cmux-app.json", tag="1.2.0"))
        self.assertEqual(result.errors, [])

    def test_v2_asset_preferred_when_both(self):
        # The other asset's URL is not served: reading it would fail the review.
        responses = extension_repo(extra_assets=("cmux-app.json",))
        self.assertEqual(run(ENTRY, responses).errors, [])

    def test_no_release(self):
        responses = {f"{API}/repos/alice/cmux-focus/releases?per_page=30": j([])}
        self.assertIn("no release yet", run(ENTRY, responses).errors[0])

    def test_id_must_start_with_owner(self):
        result = run({**ENTRY, "id": "bob/focus"}, extension_repo())
        self.assertIn("must start with the repo owner", result.errors[0])

    def test_reserved_owner_refused_from_other_repos(self):
        result = run({**ENTRY, "id": "cmux/focus", "repo": "cmux/x"}, {})
        self.assertIn("reserved", " ".join(result.errors))

    def test_manifest_id_must_match(self):
        manifest = {**MANIFEST, "id": "alice/other"}
        result = run(ENTRY, extension_repo(manifest=manifest))
        self.assertTrue(any("differs from the entry id" in e for e in result.errors))

    def test_manifest_schema_errors(self):
        manifest = {k: v for k, v in MANIFEST.items() if k != "engines"}
        result = run(ENTRY, extension_repo(manifest=manifest))
        self.assertTrue(any("engines" in e for e in result.errors), result.errors)

    def test_tag_must_be_the_version(self):
        result = run(ENTRY, extension_repo(tag="v9.9.9"))
        self.assertTrue(any("is not the manifest version" in e for e in result.errors))

    def test_scope_without_a_real_reason(self):
        manifest = {**MANIFEST, "scopes": {"storage:local": "todo"}}
        result = run(ENTRY, extension_repo(manifest=manifest))
        self.assertTrue(any("give a real reason" in e for e in result.errors))

    def test_package_manifest_must_match_asset(self):
        package = tarball({"cmux-app.v2.json": j({**MANIFEST, "version": "1.1.0"}), "dist/main.js": b"1;\n"})
        result = run(ENTRY, extension_repo(package=package))
        self.assertTrue(any("differs from the release's" in e for e in result.errors))

    def test_missing_main_in_package(self):
        package = tarball({"cmux-app.v2.json": j(MANIFEST)})
        result = run(ENTRY, extension_repo(package=package))
        self.assertTrue(any("runtime.main" in e for e in result.errors))

    def test_symlink_in_package_refused(self):
        package = tarball({"cmux-app.v2.json": j(MANIFEST), "dist/main.js": b"1;\n"}, link="dist/evil")
        result = run(ENTRY, extension_repo(package=package))
        self.assertTrue(any("link or device" in e for e in result.errors))

    def test_script_that_does_not_parse(self):
        if not review.shutil.which("node"):
            self.skipTest("node not installed")
        package = tarball({"cmux-app.v2.json": j(MANIFEST), "dist/main.js": b"function (\n"})
        result = run(ENTRY, extension_repo(package=package))
        self.assertTrue(any("does not parse" in e for e in result.errors))

    def test_hidden_character_in_script(self):
        package = tarball({"cmux-app.v2.json": j(MANIFEST), "dist/main.js": "let a = 1; // ‮\n".encode()})
        result = run(ENTRY, extension_repo(package=package))
        self.assertTrue(any("U+202E" in e for e in result.errors))

    def test_readme_without_image_warns(self):
        result = run(ENTRY, extension_repo(readme=b"# Focus\n"))
        self.assertEqual(result.errors, [])
        self.assertTrue(any("no image" in w for w in result.warnings))

    def test_other_host_is_left_to_a_person(self):
        result = run({**ENTRY, "repo": "https://codeberg.org/alice/cmux-focus"}, {})
        self.assertEqual(result.errors, [])
        self.assertIn("by hand", result.warnings[0])


def raw(owner, repo, path, ref):
    return f"{API}/repos/{owner}/{repo}/contents/{review.urllib.parse.quote(path)}?ref={ref}"


README = {f"{DL}/r": b"![shot](s.png) https://cmux.com/extensions/badge.svg"}


def readme_at(owner, repo, ref):
    return {f"{API}/repos/{owner}/{repo}/readme?ref={ref}": j({"download_url": f"{DL}/r"}), **README}


class ThemeTests(unittest.TestCase):
    ENTRY = {**ENTRY, "id": "alice/dusk", "repo": "alice/dusk"}
    MARKER = {"cmuxTheme": 1, "id": "alice/dusk", "name": "Dusk", "description": "Warm.", "version": "1.0.0"}

    def repo(self, marker=None, themes=None, tag="1.0.0"):
        themes = {"dusk.json": b'// dusk\n{ "name": "Dusk", "colors": {"accent": "#f80"}, }\n'} if themes is None else themes
        responses = {
            f"{API}/repos/alice/dusk/releases/latest": j({"tag_name": tag}),
            raw("alice", "dusk", "cmux-theme.json", tag): j(marker or self.MARKER),
            f"{API}/repos/alice/dusk/contents/themes?ref={tag}": j([{"type": "file", "name": n} for n in themes]),
            **readme_at("alice", "dusk", tag),
        }
        for name, data in themes.items():
            responses[raw("alice", "dusk", f"themes/{name}", tag)] = data
        return responses

    def test_theme_repo_passes(self):
        result = run(self.ENTRY, self.repo(), "themes.json")
        self.assertEqual(result.errors, [])
        self.assertIn("themes: dusk", result.glance)

    def test_marker_needed(self):
        result = run(self.ENTRY, {**self.repo(), raw("alice", "dusk", "cmux-theme.json", "1.0.0"): j({"id": "alice/dusk"})}, "themes.json")
        self.assertTrue(any("cmuxTheme" in e for e in result.errors))

    def test_no_theme_files(self):
        result = run(self.ENTRY, self.repo(themes={}), "themes.json")
        self.assertTrue(any("no theme files" in e for e in result.errors))

    def test_bad_theme_file(self):
        result = run(self.ENTRY, self.repo(themes={"a.json": b'{"colors": "red"}'}), "themes.json")
        self.assertTrue(any("`colors` must be an object" in e for e in result.errors))

    def test_theme_file_cannot_pick_a_theme(self):
        result = run(self.ENTRY, self.repo(themes={"a.json": b'{"appearance": {"themeFile": "x"}}'}), "themes.json")
        self.assertTrue(any("themeFile" in e for e in result.errors))

    def test_use_must_name_a_file(self):
        result = run(self.ENTRY, self.repo(marker={**self.MARKER, "use": "night"}), "themes.json")
        self.assertTrue(any("`use` names `night`" in e for e in result.errors))

    def test_tag_must_be_version(self):
        result = run(self.ENTRY, self.repo(tag="2.0.0"), "themes.json")
        self.assertTrue(any("is not the version" in e for e in result.errors))

    def test_path_reads_marker_in_folder(self):
        entry = {**self.ENTRY, "path": "dusk"}
        responses = {
            f"{API}/repos/alice/dusk/releases/latest": j({"tag_name": "1.0.0"}),
            raw("alice", "dusk", "dusk/cmux-theme.json", "1.0.0"): j({**self.MARKER, "themes": ["themes/d.json"]}),
            raw("alice", "dusk", "dusk/themes/d.json", "1.0.0"): b'{"name": "D"}',
            **readme_at("alice", "dusk", "1.0.0"),
        }
        self.assertEqual(run(entry, responses, "themes.json").errors, [])

    def test_no_release_reads_default_branch(self):
        responses = self.repo(tag="main")
        del responses[f"{API}/repos/alice/dusk/releases/latest"]
        responses[f"{API}/repos/alice/dusk/tags?per_page=1"] = j([])
        responses[f"{API}/repos/alice/dusk"] = j({"default_branch": "main"})
        result = run(self.ENTRY, responses, "themes.json")
        self.assertEqual(result.errors, [])
        self.assertTrue(any("default branch" in c for c in result.checked))


class SnippetTests(unittest.TestCase):
    ENTRY = {**ENTRY, "id": "alice/compact", "repo": "alice/compact"}
    MARKER = {"cmuxSnippet": 1, "id": "alice/compact", "name": "Compact", "description": "Tight rows.",
              "cmuxProfile": 1, "settings": {"appearance.density": "compact"}}

    def repo(self, marker=None, files=None):
        responses = {
            f"{API}/repos/alice/compact/releases/latest": j({"tag_name": "v1.0.0"}),
            raw("alice", "compact", "cmux-snippet.json", "v1.0.0"): j(marker or self.MARKER),
            **readme_at("alice", "compact", "v1.0.0"),
        }
        for path, data in (files or {}).items():
            responses[raw("alice", "compact", path, "v1.0.0")] = data
        return responses

    def test_settings_snippet_passes(self):
        result = run(self.ENTRY, self.repo(), "snippets.json")
        self.assertEqual(result.errors, [])
        self.assertIn("settings: appearance.density", result.glance)

    def test_stylesheet_in_tree_is_checked(self):
        files = {"markdown/theme.css": b"h1 { background: url(https://x.example/a.png) }"}
        result = run(self.ENTRY, self.repo(files=files), "snippets.json")
        self.assertTrue(any("nothing from the network" in e for e in result.errors))

    def test_stylesheet_up_to_cmux_limit_passes(self):
        # An inlined font: as cmux reads it (MAX_SNIPPET_FILE_BYTES, 256 KiB).
        css = b"@font-face { font-family: F; src: url(data:font/woff2;base64," + b"A" * (180 * 1024) + b") }"
        result = run(self.ENTRY, self.repo(files={"agent-pane/theme.css": css}), "snippets.json")
        self.assertEqual(result.errors, [])
        too_big = b"a{}" + b" " * (256 * 1024)
        result = run(self.ENTRY, self.repo(files={"agent-pane/theme.css": too_big}), "snippets.json")
        self.assertTrue(any("larger than 256 KiB" in e for e in result.errors))

    def test_inline_stylesheet_import_refused(self):
        marker = {**self.MARKER, "files": {"diff/theme.css": "@import 'x.css';"}}
        result = run(self.ENTRY, self.repo(marker=marker), "snippets.json")
        self.assertTrue(any("@import" in e for e in result.errors))

    def test_actions_refused(self):
        marker = {**self.MARKER, "actions": {"x": {"type": "command", "command": "rm -rf ~"}}}
        result = run(self.ENTRY, self.repo(marker=marker), "snippets.json")
        self.assertTrue(any("no `actions`" in e for e in result.errors))

    def test_other_files_refused(self):
        marker = {**self.MARKER, "files": {"customize/main.js": "1"}}
        result = run(self.ENTRY, self.repo(marker=marker), "snippets.json")
        self.assertTrue(any("may carry only" in e for e in result.errors))

    def test_theme_setting_refused(self):
        marker = {**self.MARKER, "settings": {"appearance.themeFile": "dusk"}}
        result = run(self.ENTRY, self.repo(marker=marker), "snippets.json")
        self.assertTrue(any("that is a theme" in e for e in result.errors))

    def test_empty_snippet(self):
        marker = {k: v for k, v in self.MARKER.items() if k != "settings"}
        result = run(self.ENTRY, self.repo(marker=marker), "snippets.json")
        self.assertTrue(any("changes nothing" in e for e in result.errors))


class SetupTests(unittest.TestCase):
    ENTRY = {**ENTRY, "id": "alice/setup", "repo": "alice/setup"}

    def repo(self, setup):
        return {
            f"{API}/repos/alice/setup/releases/latest": j({"tag_name": "v1.0.0"}),
            raw("alice", "setup", "cmux-setup.json", "v1.0.0"): j(setup),
            **readme_at("alice", "setup", "v1.0.0"),
        }

    def test_setup_passes_and_lists_actions(self):
        setup = {"cmuxSetup": 1, "id": "alice/setup", "name": "Night", "description": "A setup.", "version": "1.0.0",
                 "cmuxProfile": 1, "actions": {"hi": {"type": "command", "command": "echo hi"}},
                 "extensions": ["github.com/alice/focus"]}
        result = run(self.ENTRY, self.repo(setup), "setups.json")
        self.assertEqual(result.errors, [])
        self.assertTrue(any("echo hi" in g for g in result.glance))
        self.assertTrue(any("github.com/alice/focus" in g for g in result.glance))

    def test_setup_needs_marker_and_id(self):
        result = run(self.ENTRY, self.repo({"id": "alice/other", "name": "x", "description": "y"}), "setups.json")
        self.assertTrue(any("cmuxSetup" in e for e in result.errors))
        self.assertTrue(any("differs from the entry id" in e for e in result.errors))


NIGHTLY = "a" * 40
OLDER = "b" * 40


def appcast(*items):
    body = "".join(
        f"<item><sparkle:version>{build}</sparkle:version>"
        f"<sparkle:shortVersionString>1.0.0-nightly.{build}</sparkle:shortVersionString>"
        f'<cmux:sourceCommit xmlns:cmux="https://cmux.com/xml-namespaces/cmux">{sha}</cmux:sourceCommit></item>'
        for build, sha in items
    )
    return f"<rss><channel>{body}</channel></rss>".encode()


ROWS = [
    {"key": "sidebar.density", "kind": "choice", "agent_settable": True, "validation": "portable",
     "choices": [{"value": "compact"}, {"value": "comfortable"}], "accepts": ["compact"], "refuses": ["tiny", 1]},
    {"key": "agentPane.display.width", "kind": "number", "agent_settable": True, "validation": "portable",
     "range": {"min": 480, "max": 1400, "step": 10}, "accepts": [480, 736], "refuses": [100, "760", True]},
    {"key": "appearance.themeFile", "kind": "text", "agent_settable": True, "validation": "portable",
     "accepts": ["studio"], "refuses": [1]},
    {"key": "sidebar.workspaceRow.icon", "kind": "toggle", "agent_settable": True, "validation": "portable",
     "accepts": [True, False], "refuses": ["true", 1]},
    {"key": "remote.ultra", "kind": "choice", "agent_settable": True, "validation": "portable",
     "choices": [{"value": "off"}, {"value": "auto"}, {"value": "always"}], "accepts": ["auto"], "refuses": [True]},
    {"key": "history.terminalCommands", "kind": "toggle", "agent_settable": False, "validation": "portable",
     "accepts": [True], "refuses": [1]},
]
RETIRED = {"retired": [{"key": "remote.ultraMode", "reason": "now remote.ultra", "replaced_by": "remote.ultra",
                        "values": [{"from": True, "to": "always"}]}]}
TOKENS = {"interface": "cmux.agentPane.style/1", "tokens": [
    {"name": "--cv-text", "kind": "color"},
    {"name": "--cv-old-tint", "kind": "color", "replacedBy": "--cv-user-bubble-tint"},
]}


def nightly(rows=ROWS, tokens=None, skips_unknown=False, sha=NIGHTLY):
    at = f"https://raw.githubusercontent.com/manaflow-ai/cmux/{sha}/"
    files = {
        review.NIGHTLY_APPCASTS[0]: appcast((4427140699701, sha), (4424316677601, OLDER)),
        at + review.SETTINGS_SCHEMA: j({"version": 1, "rows": rows}),
        at + review.RETIRED_KEYS: j(RETIRED),
    }
    if tokens is not None:
        files[at + review.STYLE_TOKENS] = j(tokens)
    if skips_unknown:
        files[at + review.PUBLISHED_KEYS] = b"sidebar.density\n"
    return files


class SetupOnNightlyTests(unittest.TestCase):
    ENTRY = {**ENTRY, "id": "alice/look", "repo": "alice/look"}

    def repo(self, settings, css=None):
        setup = {"cmuxSetup": 1, "id": "alice/look", "name": "Look", "description": "A look.", "version": "1.0.0",
                 "cmuxProfile": 1, "settings": settings}
        files = {
            f"{API}/repos/alice/look/releases/latest": j({"tag_name": "v1.0.0"}),
            raw("alice", "look", "cmux-setup.json", "v1.0.0"): j(setup),
            **readme_at("alice", "look", "v1.0.0"),
        }
        if css is not None:
            files[raw("alice", "look", "agent-pane/theme.css", "v1.0.0")] = css.encode()
        return files

    def check(self, settings, css=None, **target):
        return run(self.ENTRY, {**self.repo(settings, css), **nightly(**target)}, "setups.json")

    def test_newest_nightly_is_the_target(self):
        sha, version = review.nightly_commit(FakeGitHub(nightly()))
        self.assertEqual((sha, version), (NIGHTLY, "1.0.0-nightly.4427140699701"))

    def test_setup_the_nightly_takes_passes(self):
        result = self.check({"sidebar.density": "compact", "agentPane.display.width": 736,
                             "appearance.themeFile": "studio", "sidebar.workspaceRow.icon": False})
        self.assertEqual(result.errors, [])
        self.assertTrue(any("1.0.0-nightly.4427140699701" in c for c in result.checked))

    def test_unknown_key_refuses_until_the_nightly_skips_it(self):
        result = self.check({"sidebar.look": "claude"})
        self.assertTrue(any("`sidebar.look` is not one" in e and "refuses the whole setup" in e for e in result.errors))
        result = self.check({"sidebar.look": "claude"}, skips_unknown=True)
        self.assertEqual(result.errors, [])
        self.assertTrue(any("`sidebar.look`" in w and "leaves it out" in w for w in result.warnings))

    def test_renamed_key(self):
        result = self.check({"remote.ultraMode": True})
        self.assertTrue(any("renamed to `remote.ultra`" in e for e in result.errors))
        result = self.check({"remote.ultraMode": True}, skips_unknown=True)
        self.assertEqual(result.errors, [])
        self.assertTrue(any("set `remote.ultra`" in w for w in result.warnings))

    def test_values_the_row_refuses(self):
        result = self.check({"sidebar.density": "tiny", "agentPane.display.width": 2000,
                             "sidebar.workspaceRow.icon": "false", "appearance.themeFile": 3})
        self.assertEqual(len(result.errors), 4, result.errors)
        self.assertTrue(any("one of compact, comfortable" in e for e in result.errors))
        self.assertTrue(any("480 to 1400" in e for e in result.errors))
        self.assertEqual(self.check({"agentPane.display.width": True}).ok, False)

    def test_key_only_the_person_sets(self):
        result = self.check({"history.terminalCommands": True})
        self.assertTrue(any("only the person can change" in e for e in result.errors))

    def test_stylesheet_tokens(self):
        css = ":root {\n  --cv-text: #eee;\n  --cv-old-tint: #333;\n  /* --cv-commented: 1px; */\n  --cv-made-up: red;\n}\n"
        result = self.check({}, css=css, tokens=TOKENS)
        self.assertEqual(result.errors, [])
        self.assertTrue(any("`--cv-made-up`" in w and "paints nothing" in w for w in result.warnings))
        self.assertTrue(any("renamed to `--cv-user-bubble-tint`" in w for w in result.warnings))
        self.assertFalse(any("--cv-text`" in w or "--cv-commented" in w for w in result.warnings))
        result = self.check({}, css=css)
        self.assertTrue(any("publishes no" in w for w in result.warnings))

    def test_unreadable_nightly_is_a_warning_in_review(self):
        result = run(self.ENTRY, self.repo({"sidebar.look": "x"}), "setups.json")
        self.assertEqual(result.errors, [])
        self.assertTrue(any("could not read the cmux nightly" in w for w in result.warnings))

    def test_cmux_ref_overrides_the_nightly(self):
        files = {**self.repo({"sidebar.density": "compact"}), **nightly(sha=OLDER)}
        del files[review.NIGHTLY_APPCASTS[0]]
        gh = FakeGitHub(files)
        gh.cmux_ref = OLDER
        result = review.review_entry(gh, "setups.json", self.ENTRY, "added")
        self.assertEqual(result.errors, [])
        self.assertTrue(any(OLDER in c for c in result.checked))

    def test_checker_reads_the_schema_samples_as_cmux_does(self):
        gh = FakeGitHub(nightly())
        self.assertEqual(review.checker_drift(review.load_cmux_target(gh)), [])
        odd = [*ROWS, {"key": "x.new", "kind": "brand_new_kind", "agent_settable": True, "validation": "portable"},
               {"key": "x.width", "kind": "number", "agent_settable": True, "validation": "portable",
                "range": {"min": 0, "max": 10}, "accepts": [20]}]
        drift = review.checker_drift(review.load_cmux_target(FakeGitHub(nightly(rows=odd))))
        self.assertEqual(len(drift), 2, drift)

    def test_compat_run_over_setups_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in review.KINDS:
                (root / name).write_text(json.dumps([self.ENTRY] if name == "setups.json" else []))
            out = root / "report.md"
            files = {**self.repo({"sidebar.look": "claude"}), **nightly()}
            code = review.main(["--root", str(root), "--setups-compat", "--out", str(out), "--quiet"], gh=FakeGitHub(files))
            text = out.read_text()
            self.assertEqual(code, 1)
            self.assertIn("vs the cmux nightly: FAIL", text)
            self.assertIn(f"manaflow-ai/cmux@{NIGHTLY}", text)
            self.assertIn("### FAIL: `alice/look`", text)
            self.assertNotIn("README", text)
            files = {**self.repo({"sidebar.density": "compact"}), **nightly()}
            code = review.main(["--root", str(root), "--setups-compat", "--out", str(out), "--quiet"], gh=FakeGitHub(files))
            self.assertEqual(code, 0)
            self.assertIn("PASS", out.read_text())
            code = review.main(["--root", str(root), "--setups-compat", "--out", str(out), "--quiet"], gh=FakeGitHub(self.repo({})))
            self.assertEqual(code, 2)
            self.assertIn("NOT RUN", out.read_text())


class FileTests(unittest.TestCase):
    def write(self, root: Path, **files):
        for name in review.KINDS:
            (root / name).write_text(json.dumps(files.get(name.replace(".json", ""), [])))

    def test_shape_unique_ids_and_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = Path(tmp) / "root", Path(tmp) / "base"
            root.mkdir()
            base.mkdir()
            self.write(base, extensions=[ENTRY])
            self.write(root, extensions=[ENTRY], themes=[{**ENTRY, "repo": "alice/dusk"}, {"id": "Bad"}])
            out = Path(tmp) / "comment.md"
            code = review.main(["--root", str(root), "--base-dir", str(base), "--out", str(out), "--quiet"], gh=FakeGitHub({}))
            text = out.read_text()
            self.assertEqual(code, 1)
            self.assertIn("listed twice", text)
            self.assertIn("themes.json/1", text)
            self.assertNotIn("(added in extensions.json)", text)

    def test_unchanged_entries_are_not_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, extensions=[ENTRY])
            code = review.main(["--root", str(root), "--base-dir", str(root), "--quiet"], gh=FakeGitHub({}))
            self.assertEqual(code, 0)

    def test_added_entry_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = Path(tmp) / "root", Path(tmp) / "base"
            root.mkdir()
            base.mkdir()
            self.write(root, extensions=[ENTRY])
            code = review.main(["--root", str(root), "--base-dir", str(base), "--quiet"], gh=FakeGitHub(extension_repo()))
            self.assertEqual(code, 0)

    def test_repo_entry_files_follow_the_schema(self):
        root = Path(__file__).resolve().parents[2]
        entries, errors = review.load_entries(root)
        self.assertEqual(errors + review.file_checks(entries), [])

    def test_jsonc(self):
        self.assertEqual(review.parse_jsonc(b'{"a": "x // y", /* c */ "b": [1,],}'), {"a": "x // y", "b": [1]})

    def test_tag_forms(self):
        for tag in ("1.2.0", "v1.2.0", "focus@1.2.0", "focus/v1.2.0"):
            self.assertTrue(review.tag_matches(tag, "1.2.0"), tag)
        self.assertFalse(review.tag_matches("1.2.0-beta", "1.2.0"))
        self.assertFalse(review.tag_matches("11.2.0", "1.2.0"))


if __name__ == "__main__":
    unittest.main()
