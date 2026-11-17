#!/usr/bin/env python3
"""Automated review of the cmux community registry.

Checks the four entry files (shape, unique ids, owner rule) and, for every entry
a pull request adds or changes, fetches what the entry points at:

- extensions: the newest release with `cmux-app.v2.json` and `cmux-extension.tar.gz`;
  the manifest against the vendored v2 schema, id, tag, scope reasons; the package's
  manifest equals the asset, the files it names are there, its script parses.
- themes: `cmux-theme.json` (`"cmuxTheme": 1`, id, version tag) and each theme file it names
  (default every `themes/*.json`): JSONC object, `colors`, size, at most 16, `use` names one.
- snippets: `cmux-snippet.json` (`"cmuxSnippet": 1`, a profile of settings and stylesheets), no
  actions, views or theme file; its stylesheets import nothing and load nothing remote.
- setups: `cmux-setup.json` (`"cmuxSetup": 1`, id); the reviewer sees its actions and extensions.

Writes a Markdown summary (the PR comment) and exits 1 when any check fails.

    python3 review/review.py --root . --base-dir BASE [--all] [--out comment.md]

BASE holds the base branch's copies of the entry files (a missing file is empty).
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

HERE = Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schemas"

KINDS = {
    "extensions.json": "extension",
    "themes.json": "theme",
    "snippets.json": "snippet",
    "setups.json": "setup",
}
RESERVED_OWNERS = {"cmux", "manaflow-ai"}
TRUSTED_OWNER = "manaflow-ai"

MANIFEST_ASSET = "cmux-app.v2.json"
PACKAGE_ASSET = "cmux-extension.tar.gz"
PACKAGE_MANIFESTS = ("cmux-app.v2.json", "cmux-app.json")
SETUP_FILE = "cmux-setup.json"
# Marker files of the no-code tiers (tiers.md): at the repo root or at the entry's `path`.
THEME_MARKER = "cmux-theme.json"
SNIPPET_MARKER = "cmux-snippet.json"
THEMES_MAX = 16
# What a snippet may carry: the stylesheets cmux loads per surface, and a keymap.
SNIPPET_FILES = (
    "agent-pane/theme.css",
    "markdown/theme.css",
    "diff/theme.css",
    "picker/theme.css",
    "fileViewer/theme.css",
    "keybindings.json",
)
SNIPPET_REFUSED_KEYS = ("actions", "automations", "extensions", "views", "customize")

MANIFEST_LIMIT = 64 * 1024
MAIN_LIMIT = 2 * 1024 * 1024
PACKAGE_LIMIT = 25 * 1024 * 1024
UNPACKED_LIMIT = 100 * 1024 * 1024
PACKAGE_MEMBERS_LIMIT = 4000
THEME_LIMIT = 256 * 1024
SNIPPET_LIMIT = 64 * 1024
SETUP_LIMIT = 256 * 1024
README_LIMIT = 512 * 1024
RELEASES_LOOKED_AT = 30
SCOPE_REASON_MIN = 10

# Characters that hide what code does (cmux-app-manifest hidden.rs): direction
# controls and marks, zero-width and invisible characters, tag and variation blocks.
HIDDEN = re.compile(
    "[‪-‮⁦-⁩‎‏؜​⁠-⁤"
    "\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)


class NotFound(Exception):
    pass


class FetchError(Exception):
    pass


class GitHub:
    """Reads public repositories through the GitHub REST API."""

    def __init__(self, token: str | None = None, api: str = "https://api.github.com"):
        self.token = token
        self.api = api

    def _open(self, url: str, accept: str, limit: int) -> bytes:
        headers = {"Accept": accept, "User-Agent": "cmux-extensions-review"}
        if self.token and urllib.parse.urlparse(url).hostname == "api.github.com":
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise NotFound(url) from None
            raise FetchError(f"{url}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise FetchError(f"{url}: {error}") from None
        if len(data) > limit:
            raise FetchError(f"{url} is larger than {limit // 1024} KiB")
        return data

    def json(self, path: str):
        return json.loads(self._open(self.api + path, "application/vnd.github+json", 8 * 1024 * 1024))

    def raw_file(self, owner: str, repo: str, path: str, ref: str, limit: int) -> bytes:
        quoted = urllib.parse.quote(path)
        url = f"{self.api}/repos/{owner}/{repo}/contents/{quoted}?ref={urllib.parse.quote(ref)}"
        return self._open(url, "application/vnd.github.raw", limit)

    def download(self, url: str, limit: int) -> bytes:
        return self._open(url, "application/octet-stream", limit)


@dataclass
class Result:
    file: str
    entry: dict
    change: str  # "added" | "changed" | "unchanged"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    glance: list[str] = field(default_factory=list)  # what a human reviewer looks at
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------- helpers


def strip_jsonc(text: str) -> str:
    """Drops // and /* */ comments and trailing commas outside strings."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def parse_jsonc(raw: bytes):
    text = raw.decode("utf-8-sig")
    return json.loads(strip_jsonc(text))


def parse_repo(repo: str) -> tuple[str, str, str]:
    """`OWNER/REPO` or `https://HOST/OWNER/REPO` -> (host, owner, repo)."""
    if repo.startswith("https://"):
        parts = repo[len("https://") :].split("/")
        host, owner, name = parts[0].lower(), parts[1], parts[2]
    else:
        host, (owner, name) = "github.com", repo.split("/")
    if name.endswith(".git"):
        name = name[:-4]
    return host, owner, name


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def schema_errors(schema: dict, value) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    found = []
    for error in sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path)):
        where = "/" + "/".join(str(p) for p in error.absolute_path)
        found.append(f"{where}: {error.message}")
    return found


def tag_matches(tag: str, version: str) -> bool:
    """`1.2.0`, `v1.2.0`, or for a repo with several items `NAME@1.2.0` / `NAME/v1.2.0`."""
    return re.fullmatch(r"(?:.+[@/])?v?" + re.escape(version), tag) is not None


def hidden_characters(text: str) -> list[str]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in HIDDEN.finditer(line):
            found.append(f"line {number}: U+{ord(match.group()):04X}")
    return found


# ---------------------------------------------------------------- whole files


def load_entries(root: Path) -> tuple[dict[str, list], list[str]]:
    entries, errors = {}, []
    schema = load_schema("registry-entries.schema.json")
    for name in KINDS:
        path = root / name
        if not path.exists():
            errors.append(f"{name} is missing")
            entries[name] = []
            continue
        try:
            value = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            errors.append(f"{name} is not valid JSON: {error}")
            entries[name] = []
            continue
        for problem in schema_errors(schema, value):
            errors.append(f"{name}{problem}")
        entries[name] = [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []
    return entries, errors


def load_base(base_dir: Path | None) -> dict[str, list]:
    base = {}
    for name in KINDS:
        path = base_dir / name if base_dir else None
        try:
            value = json.loads(path.read_text()) if path and path.exists() else []
        except json.JSONDecodeError:
            value = []
        base[name] = [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []
    return base


def file_checks(entries: dict[str, list]) -> list[str]:
    errors = []
    seen_ids: dict[str, str] = {}
    for name, items in entries.items():
        seen_sources: set[tuple] = set()
        for entry in items:
            entry_id = str(entry.get("id", ""))
            if entry_id in seen_ids:
                errors.append(f"id `{entry_id}` is listed twice ({seen_ids[entry_id]} and {name})")
            seen_ids.setdefault(entry_id, name)
            repo = entry.get("repo")
            if isinstance(repo, str):
                try:
                    host, owner, rname = parse_repo(repo)
                except (ValueError, IndexError):
                    continue
                source = (host, owner.lower(), rname.lower(), entry.get("path", ""))
                if source in seen_sources:
                    errors.append(f"{name}: `{repo}` {entry.get('path', '')} is listed twice")
                seen_sources.add(source)
    return errors


def owner_errors(entry: dict) -> list[str]:
    try:
        _, owner, _ = parse_repo(entry["repo"])
    except (KeyError, ValueError, IndexError):
        return ["`repo` is not OWNER/REPO or https://HOST/OWNER/REPO"]
    entry_id = entry.get("id", "")
    id_owner = entry_id.split("/", 1)[0]
    errors = []
    if id_owner != owner.lower():
        errors.append(f"id `{entry_id}` must start with the repo owner, lowercased: `{owner.lower()}/`")
    if id_owner in RESERVED_OWNERS and owner.lower() != TRUSTED_OWNER:
        errors.append(f"ids under `{id_owner}/` are reserved")
    return errors


def diff(entries: dict[str, list], base: dict[str, list]) -> tuple[list[tuple[str, dict, str]], list[tuple[str, dict]]]:
    changed, removed = [], []
    for name in KINDS:
        before = {e.get("id"): e for e in base[name]}
        now_ids = set()
        for entry in entries[name]:
            now_ids.add(entry.get("id"))
            old = before.get(entry.get("id"))
            state = "added" if old is None else ("unchanged" if old == entry else "changed")
            changed.append((name, entry, state))
        removed += [(name, e) for i, e in before.items() if i not in now_ids]
    return changed, removed


# ---------------------------------------------------------------- per kind


def newest_release_with(gh: GitHub, owner: str, repo: str, wanted: tuple[str, ...]):
    releases = gh.json(f"/repos/{owner}/{repo}/releases?per_page={RELEASES_LOOKED_AT}")
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        names = {a.get("name") for a in release.get("assets", [])}
        if all(w in names for w in wanted):
            return release, releases
    return None, releases


def asset_url(release: dict, name: str) -> str:
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            return asset["browser_download_url"]
    raise NotFound(name)


def newest_ref(gh: GitHub, owner: str, repo: str) -> tuple[str, str]:
    """The latest release's tag, else the newest tag, else the default branch."""
    try:
        release = gh.json(f"/repos/{owner}/{repo}/releases/latest")
        return release["tag_name"], f"release {release['tag_name']}"
    except NotFound:
        pass
    tags = gh.json(f"/repos/{owner}/{repo}/tags?per_page=1")
    if tags:
        return tags[0]["name"], f"tag {tags[0]['name']}"
    info = gh.json(f"/repos/{owner}/{repo}")
    return info["default_branch"], f"default branch {info['default_branch']} (no release or tag yet)"


def check_readme(gh: GitHub, owner: str, repo: str, ref: str, result: Result) -> None:
    try:
        readme = gh.json(f"/repos/{owner}/{repo}/readme?ref={urllib.parse.quote(ref)}")
        text = gh.download(readme["download_url"], README_LIMIT).decode("utf-8", "replace")
    except NotFound:
        result.errors.append("the repo has no README (it is the listing's page)")
        return
    if not re.search(r"!\[[^\]]*\]\(|<img\s", text):
        result.warnings.append("README shows no image; listings with a screenshot get installed more")
    if "cmux.com/extensions/badge.svg" not in text:
        result.warnings.append("README has no \"Get it on cmux\" badge (see this repo's README)")
    result.checked.append("README")


def safe_members(tar: tarfile.TarFile, result: Result) -> dict[str, tarfile.TarInfo] | None:
    members: dict[str, tarfile.TarInfo] = {}
    total = 0
    for count, member in enumerate(tar):
        if count >= PACKAGE_MEMBERS_LIMIT:
            result.errors.append(f"{PACKAGE_ASSET} has more than {PACKAGE_MEMBERS_LIMIT} entries")
            return None
        name = member.name
        while name.startswith("./"):
            name = name[2:]
        parts = name.split("/")
        if name.startswith("/") or ".." in parts:
            result.errors.append(f"{PACKAGE_ASSET}: `{member.name}` leaves the package")
            return None
        if not (member.isfile() or member.isdir()):
            result.errors.append(f"{PACKAGE_ASSET}: `{member.name}` is a link or device; only files and folders")
            return None
        total += member.size
        if total > UNPACKED_LIMIT:
            result.errors.append(f"{PACKAGE_ASSET} unpacks to more than {UNPACKED_LIMIT // 1024 // 1024} MiB")
            return None
        if name:
            members[name.rstrip("/")] = member
    return members


def package_root(members: dict[str, tarfile.TarInfo]) -> str | None:
    for manifest in PACKAGE_MANIFESTS:
        if manifest in members:
            return ""
    tops = {name.split("/", 1)[0] for name in members}
    if len(tops) == 1:
        top = tops.pop()
        for manifest in PACKAGE_MANIFESTS:
            if f"{top}/{manifest}" in members:
                return top + "/"
    return None


def referenced_paths(manifest: dict) -> list[tuple[str, str]]:
    refs = []
    runtime = manifest.get("runtime") or {}
    if isinstance(runtime.get("main"), str):
        refs.append(("runtime.main", runtime["main"]))
    if isinstance((runtime.get("web") or {}).get("root"), str):
        refs.append(("runtime.web.root", runtime["web"]["root"]))
    for key in ("catalog", "strings", "icon"):
        if isinstance(manifest.get(key), str):
            refs.append((key, manifest[key]))
    sidebar_icon = ((manifest.get("presentation") or {}).get("sidebarItem") or {}).get("icon")
    if isinstance(sidebar_icon, str):
        refs.append(("presentation.sidebarItem.icon", sidebar_icon))
    for name, implementation in (manifest.get("implements") or {}).items():
        if isinstance(implementation, dict) and isinstance(implementation.get("web"), str):
            refs.append((f"implements.{name}.web", implementation["web"]))
    for i, notice in enumerate(manifest.get("notices") or []):
        if isinstance(notice, dict) and isinstance(notice.get("path"), str):
            refs.append((f"notices[{i}].path", notice["path"]))
    return refs


def script_parses(source: bytes) -> str | None:
    """`node --check` parses the script without running it; None when it parses or node is absent."""
    node = shutil.which("node")
    if not node:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "main.js"
        path.write_bytes(source)
        run = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    if run.returncode == 0:
        return None
    lines = [line for line in run.stderr.splitlines() if line.strip()]
    return "; ".join(lines[:4]) or "node --check failed"


def check_scopes(manifest: dict, result: Result) -> None:
    for key in ("scopes", "optionalScopes"):
        for scope, reason in (manifest.get(key) or {}).items():
            text = reason.strip() if isinstance(reason, str) else ""
            if len(text) < SCOPE_REASON_MIN or " " not in text or text.lower() in (scope.lower(), "todo", "tbd"):
                result.errors.append(f"{key} `{scope}`: give a real reason (a sentence the consent card shows), not `{reason}`")
            else:
                result.glance.append(f"{key} `{scope}`: {text}")
    if not manifest.get("scopes") and not manifest.get("optionalScopes"):
        result.glance.append("asks for no scopes")


def check_extension(gh: GitHub, entry: dict, result: Result) -> None:
    host, owner, repo = parse_repo(entry["repo"])
    if host != "github.com":
        result.warnings.append(f"releases on {host} are not checked automatically; a reviewer checks them by hand")
        return
    release, releases = newest_release_with(gh, owner, repo, (MANIFEST_ASSET, PACKAGE_ASSET))
    if release is None:
        if not releases:
            result.errors.append(f"no release yet: publish one with the assets `{MANIFEST_ASSET}` and `{PACKAGE_ASSET}` (`cmux ext publish` prints the command)")
        else:
            result.errors.append(f"no release among the newest {RELEASES_LOOKED_AT} has both `{MANIFEST_ASSET}` and `{PACKAGE_ASSET}`")
        return
    tag = release["tag_name"]
    result.checked.append(f"release {tag}")

    try:
        manifest_raw = gh.download(asset_url(release, MANIFEST_ASSET), MANIFEST_LIMIT)
        manifest = json.loads(manifest_raw)
    except FetchError as error:
        result.errors.append(f"`{MANIFEST_ASSET}`: {error}")
        return
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        result.errors.append(f"`{MANIFEST_ASSET}` is not valid JSON: {error}")
        return
    if not isinstance(manifest, dict):
        result.errors.append(f"`{MANIFEST_ASSET}` is not a JSON object")
        return
    problems = schema_errors(load_schema("vendor/cmux-app.v2.schema.json"), manifest)
    result.errors += [f"`{MANIFEST_ASSET}` {p}" for p in problems[:20]]
    for problem in hidden_characters(manifest_raw.decode("utf-8", "replace")):
        result.errors.append(f"`{MANIFEST_ASSET}` carries a hidden character ({problem})")
    result.checked.append("manifest v2 schema")

    if manifest.get("id") != entry["id"]:
        result.errors.append(f"manifest id `{manifest.get('id')}` differs from the entry id `{entry['id']}`")
    version = manifest.get("version")
    if isinstance(version, str) and not tag_matches(tag, version):
        result.errors.append(f"release tag `{tag}` is not the manifest version `{version}` (use `{version}` or `v{version}`)")
    repository = manifest.get("repository")
    expected = f"https://github.com/{owner}/{repo}"
    if repository is None:
        result.warnings.append(f"manifest has no `repository`; set it to `{expected}`")
    elif str(repository).lower() != expected.lower():
        result.errors.append(f"manifest `repository` is `{repository}`, not `{expected}`")
    if manifest.get("name") != entry.get("name"):
        result.warnings.append(f"entry name `{entry.get('name')}` differs from the manifest name `{manifest.get('name')}`")
    check_scopes(manifest, result)
    engines = (manifest.get("engines") or {}).get("cmux")
    if engines:
        result.glance.append(f"requires cmux {engines}")

    check_package(gh, release, manifest, result)
    check_readme(gh, owner, repo, tag, result)


def check_package(gh: GitHub, release: dict, manifest: dict, result: Result) -> None:
    try:
        raw = gh.download(asset_url(release, PACKAGE_ASSET), PACKAGE_LIMIT)
    except FetchError as error:
        result.errors.append(f"`{PACKAGE_ASSET}`: {error}")
        return
    try:
        tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        members = safe_members(tar, result)
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as error:
        result.errors.append(f"`{PACKAGE_ASSET}` is not a gzip tar: {error}")
        return
    if members is None:
        return
    root = package_root(members)
    if root is None:
        result.errors.append(f"`{PACKAGE_ASSET}` has no `{PACKAGE_MANIFESTS[0]}` (or `{PACKAGE_MANIFESTS[1]}`) at its top")
        return

    def read(path: str, limit: int) -> bytes:
        member = members[root + path]
        if member.size > limit:
            raise FetchError(f"`{path}` is larger than {limit // 1024} KiB")
        handle = tar.extractfile(member)
        return handle.read() if handle else b""

    inner_name = next(m for m in PACKAGE_MANIFESTS if root + m in members)
    try:
        inner = json.loads(read(inner_name, MANIFEST_LIMIT))
    except (json.JSONDecodeError, UnicodeDecodeError, FetchError) as error:
        result.errors.append(f"`{PACKAGE_ASSET}`: `{inner_name}` does not parse: {error}")
        return
    if inner != manifest:
        result.errors.append(f"the package's `{inner_name}` differs from the release's `{MANIFEST_ASSET}`")
    result.checked.append("package manifest")

    files = [f.rstrip("/") for f in manifest.get("files") or [] if isinstance(f, str)]
    for where, path in referenced_paths(manifest):
        path = path.rstrip("/")
        present = root + path in members or any(name.startswith(root + path + "/") for name in members)
        if not present:
            result.errors.append(f"`{where}`: `{path}` is not in `{PACKAGE_ASSET}`")
        elif files and not any(path == f or path.startswith(f + "/") for f in files):
            result.errors.append(f"`{where}`: `{path}` is not listed in `files`")
    for path in files:
        if root + path not in members and not any(n.startswith(root + path + "/") for n in members):
            result.errors.append(f"`files` lists `{path}`, which is not in `{PACKAGE_ASSET}`")

    main = (manifest.get("runtime") or {}).get("main")
    if isinstance(main, str) and root + main in members:
        try:
            source = read(main, MAIN_LIMIT)
        except FetchError as error:
            result.errors.append(f"runtime.main: {error}")
            return
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError:
            result.errors.append(f"runtime.main `{main}` is not UTF-8")
            return
        for problem in hidden_characters(text)[:5]:
            result.errors.append(f"runtime.main `{main}` carries a hidden character ({problem})")
        failure = script_parses(source)
        if failure:
            result.errors.append(f"runtime.main `{main}` does not parse: {failure}")
        else:
            result.checked.append("script parses")

    catalog = manifest.get("catalog")
    if isinstance(catalog, str) and root + catalog in members:
        try:
            fragment = json.loads(read(catalog, MANIFEST_LIMIT * 4))
        except (json.JSONDecodeError, UnicodeDecodeError, FetchError) as error:
            result.errors.append(f"catalog `{catalog}` does not parse: {error}")
            return
        problems = schema_errors(load_schema("vendor/cmux-app-catalog.schema.json"), fragment)
        result.errors += [f"catalog `{catalog}` {p}" for p in problems[:10]]


@dataclass
class Marked:
    """A theme, snippet or setup repo: its marker file, read at the newest release, tag or branch."""

    owner: str
    repo: str
    ref: str
    folder: str  # "" or "sub/dir/"
    marker: str
    data: dict
    raw: bytes

    def read(self, gh: GitHub, path: str, limit: int) -> bytes:
        return gh.raw_file(self.owner, self.repo, self.folder + path, self.ref, limit)


def read_marker(gh: GitHub, entry: dict, marker: str, key: str, limit: int, result: Result) -> Marked | None:
    host, owner, repo = parse_repo(entry["repo"])
    if host != "github.com":
        result.warnings.append(f"repos on {host} are not checked automatically; a reviewer checks them by hand")
        return None
    ref, where = newest_ref(gh, owner, repo)
    result.checked.append(where)
    folder = entry["path"].strip("/") + "/" if entry.get("path") else ""
    path = folder + marker
    try:
        raw = gh.raw_file(owner, repo, path, ref, limit)
    except NotFound:
        result.errors.append(f"`{path}` is not in {entry['repo']} at {ref}")
        return None
    try:
        data = parse_jsonc(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        result.errors.append(f"`{path}` does not parse as JSON (comments allowed): {error}")
        return None
    if not isinstance(data, dict):
        result.errors.append(f"`{path}` is not a JSON object")
        return None
    if data.get(key) != 1:
        result.errors.append(f"`{path}` needs `\"{key}\": 1`")
    if data.get("id") != entry["id"]:
        result.errors.append(f"`{path}` id `{data.get('id')}` differs from the entry id `{entry['id']}`")
    for field_name in ("name", "description"):
        if not isinstance(data.get(field_name), str) or not data[field_name].strip():
            result.errors.append(f"`{path}` has no `{field_name}`")
    version = data.get("version")
    if isinstance(version, str) and where.startswith(("release", "tag")) and not tag_matches(ref, version):
        result.errors.append(f"the newest tag `{ref}` is not the version `{version}` in `{path}`")
    if "cmuxProfile" in data and data["cmuxProfile"] != 1:
        result.errors.append(f"`{path}`: `cmuxProfile` must be 1")
    for problem in hidden_characters(raw.decode("utf-8", "replace"))[:5]:
        result.errors.append(f"`{path}` carries a hidden character ({problem})")
    result.checked.append(marker)
    return Marked(owner, repo, ref, folder, marker, data, raw)


def list_folder(gh: GitHub, marked: Marked, folder: str) -> list[str]:
    try:
        items = gh.json(
            f"/repos/{marked.owner}/{marked.repo}/contents/{urllib.parse.quote(marked.folder + folder)}"
            f"?ref={urllib.parse.quote(marked.ref)}"
        )
    except NotFound:
        return []
    return [f"{folder}/{i['name']}" for i in items if isinstance(i, dict) and i.get("type") == "file"]


def check_theme_file(path: str, raw: bytes, result: Result) -> None:
    try:
        theme = parse_jsonc(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        result.errors.append(f"`{path}` does not parse as JSON (comments allowed): {error}")
        return
    if not isinstance(theme, dict):
        result.errors.append(f"`{path}` is not a JSON object")
        return
    if "colors" in theme and not isinstance(theme["colors"], dict):
        result.errors.append(f"`{path}`: `colors` must be an object")
    if "ghostty" in theme and not isinstance(theme["ghostty"], (dict, str)):
        result.errors.append(f"`{path}`: `ghostty` must be an object or a string")
    appearance = theme.get("appearance") if isinstance(theme.get("appearance"), dict) else {}
    if "appearance.themeFile" in theme or "themeFile" in appearance:
        result.errors.append(f"`{path}`: a theme file cannot set `appearance.themeFile`")
    for problem in hidden_characters(raw.decode("utf-8", "replace"))[:5]:
        result.errors.append(f"`{path}` carries a hidden character ({problem})")


def check_theme(gh: GitHub, entry: dict, result: Result) -> None:
    marked = read_marker(gh, entry, THEME_MARKER, "cmuxTheme", MANIFEST_LIMIT, result)
    if marked is None:
        return
    listed = marked.data.get("themes")
    if listed is not None and not (isinstance(listed, list) and all(isinstance(p, str) for p in listed)):
        result.errors.append(f"`{THEME_MARKER}`: `themes` must be a list of paths")
        return
    paths = listed if listed is not None else list_folder(gh, marked, "themes")
    paths = [p for p in paths if p.endswith(".json") and not p.split("/")[-1].startswith(".")]
    if not paths:
        result.errors.append("no theme files: put them at `themes/NAME.json` or list them in `themes`")
        return
    if len(paths) > THEMES_MAX:
        result.errors.append(f"{len(paths)} theme files; at most {THEMES_MAX}")
        return
    names = []
    for path in paths:
        if not re.fullmatch(r"themes/[A-Za-z0-9._ -]+\.json", path):
            result.errors.append(f"`{path}`: theme files sit at `themes/NAME.json`")
            continue
        try:
            raw = marked.read(gh, path, THEME_LIMIT)
        except NotFound:
            result.errors.append(f"`{path}` is listed but not in the repo at {marked.ref}")
            continue
        except FetchError as error:
            result.errors.append(str(error))
            continue
        check_theme_file(path, raw, result)
        names.append(path.split("/")[-1][: -len(".json")])
    use = marked.data.get("use")
    if use is not None and use not in names:
        result.errors.append(f"`use` names `{use}`, which is not one of the theme files ({', '.join(names)})")
    result.checked.append(f"{len(names)} theme file(s)")
    result.glance.append(f"themes: {', '.join(names)}")
    check_readme(gh, marked.owner, marked.repo, marked.ref, result)


def check_css(path: str, text: str, result: Result) -> None:
    if re.search(r"@import\b", text, re.I):
        result.errors.append(f"`{path}`: a snippet stylesheet is one file; no `@import`")
    if re.search(r"url\(\s*['\"]?\s*(?:[a-z][a-z0-9+.-]*:)?//", text, re.I):
        result.errors.append(f"`{path}`: a snippet loads nothing from the network; no remote `url(...)`")
    if re.search(r"expression\s*\(|javascript:|behavior\s*:", text, re.I):
        result.errors.append(f"`{path}`: a snippet runs no code")


def check_snippet(gh: GitHub, entry: dict, result: Result) -> None:
    marked = read_marker(gh, entry, SNIPPET_MARKER, "cmuxSnippet", SNIPPET_LIMIT, result)
    if marked is None:
        return
    data = marked.data
    for key in SNIPPET_REFUSED_KEYS:
        if key in data:
            result.errors.append(f"`{SNIPPET_MARKER}`: a snippet has no `{key}` (that is a setup or an extension)")
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        result.errors.append(f"`{SNIPPET_MARKER}`: `settings` must be an object of dotted keys")
        settings = {}
    if "appearance.themeFile" in settings:
        result.errors.append("a snippet cannot set `appearance.themeFile`: that is a theme")
    inline = data.get("files") or {}
    if not isinstance(inline, dict):
        result.errors.append(f"`{SNIPPET_MARKER}`: `files` must map logical paths to text")
        inline = {}
    for path, content in inline.items():
        if path not in SNIPPET_FILES:
            result.errors.append(f"`files` carries `{path}`; a snippet may carry only {', '.join(SNIPPET_FILES)}")
        elif not isinstance(content, str):
            result.errors.append(f"`files.{path}` must be text")
    carried = {p: c for p, c in inline.items() if p in SNIPPET_FILES and isinstance(c, str)}
    for path in SNIPPET_FILES:
        if path in carried:
            continue
        try:
            carried[path] = marked.read(gh, path, SNIPPET_LIMIT).decode("utf-8-sig")
        except NotFound:
            continue
        except FetchError as error:
            result.errors.append(str(error))
        except UnicodeDecodeError:
            result.errors.append(f"`{path}` is not UTF-8")
    for path, text in carried.items():
        for problem in hidden_characters(text)[:5]:
            result.errors.append(f"`{path}` carries a hidden character ({problem})")
        if path.endswith(".css"):
            check_css(path, text, result)
        else:
            try:
                parse_jsonc(text.encode())
            except json.JSONDecodeError as error:
                result.errors.append(f"`{path}` does not parse: {error}")
    if not settings and not carried:
        result.errors.append("the snippet changes nothing: give it `settings` or a stylesheet")
    result.checked.append("snippet profile")
    if settings:
        result.glance.append(f"settings: {', '.join(sorted(settings)[:20])}")
    if carried:
        result.glance.append(f"files: {', '.join(sorted(carried))}")
    check_readme(gh, marked.owner, marked.repo, marked.ref, result)


def check_setup(gh: GitHub, entry: dict, result: Result) -> None:
    marked = read_marker(gh, entry, SETUP_FILE, "cmuxSetup", SETUP_LIMIT, result)
    if marked is None:
        return
    setup = marked.data
    for name, action in (setup.get("actions") or {}).items():
        if isinstance(action, dict) and action.get("command"):
            result.glance.append(f"action `{name}` runs `{action['command']}`")
    for ref in setup.get("extensions") or []:
        result.glance.append(f"installs extension `{ref}`")
    if setup.get("settings"):
        result.glance.append(f"settings: {', '.join(sorted(setup['settings'])[:20])}")
    check_readme(gh, marked.owner, marked.repo, marked.ref, result)




CHECKERS = {
    "extension": check_extension,
    "theme": check_theme,
    "snippet": check_snippet,
    "setup": check_setup,
}


def review_entry(gh: GitHub, name: str, entry: dict, change: str) -> Result:
    result = Result(name, entry, change)
    entry_schema = load_schema("registry-entries.schema.json")["$defs"]["entry"]
    if schema_errors(entry_schema, entry):
        result.errors.append("the entry's shape is wrong (see Entry files above); nothing fetched")
        return result
    result.errors += owner_errors(entry)
    if result.errors:
        return result
    try:
        CHECKERS[KINDS[name]](gh, entry, result)
    except NotFound as error:
        result.errors.append(f"not found: {error} (is the repo public?)")
    except FetchError as error:
        result.errors.append(f"could not fetch: {error}")
    return result


# ---------------------------------------------------------------- report


def render(file_errors: list[str], results: list[Result], removed: list[tuple[str, dict]]) -> str:
    failed = bool(file_errors) or any(not r.ok for r in results)
    lines = ["<!-- cmux-registry-review -->", "## Registry review: " + ("FAIL" if failed else "PASS"), ""]
    if file_errors:
        lines.append("### Entry files")
        lines += [f"- FAIL: {e}" for e in file_errors]
        lines.append("")
    if not results and not removed:
        lines.append("No entry added or changed.")
    for r in results:
        verdict = "PASS" if r.ok else "FAIL"
        lines.append(f"### {verdict}: `{r.entry.get('id')}` ({r.change} in {r.file})")
        lines.append(f"Repo: {r.entry.get('repo')}" + (f", path `{r.entry['path']}`" if r.entry.get("path") else ""))
        if r.checked:
            lines.append(f"Checked: {', '.join(r.checked)}")
        lines += [f"- FAIL: {e}" for e in r.errors]
        lines += [f"- WARN: {w}" for w in r.warnings]
        if r.glance:
            lines.append("")
            lines.append("For the reviewer:")
            lines += [f"- {g}" for g in r.glance]
        lines.append("")
    for name, entry in removed:
        lines.append(f"- Removed from {name}: `{entry.get('id')}`")
    lines.append("")
    lines.append("A maintainer still glances at the README, screenshots and scope reasons before merging.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, gh: GitHub | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=HERE.parent)
    parser.add_argument("--base-dir", type=Path, help="base branch copies of the entry files")
    parser.add_argument("--all", action="store_true", help="fetch and check every entry, not only added or changed ones")
    parser.add_argument("--out", type=Path, help="write the Markdown summary here")
    parser.add_argument("--quiet", action="store_true", help="do not print the summary")
    args = parser.parse_args(argv)

    gh = gh or GitHub(os.environ.get("GITHUB_TOKEN"))
    entries, file_errors = load_entries(args.root)
    file_errors += file_checks(entries)
    changes, removed = diff(entries, load_base(args.base_dir))
    results = [
        review_entry(gh, name, entry, change)
        for name, entry, change in changes
        if args.all or change != "unchanged"
    ]
    report = render(file_errors, results, removed)
    if args.out:
        args.out.write_text(report)
    if not args.quiet:
        sys.stdout.write(report)
    return 1 if file_errors or any(not r.ok for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
