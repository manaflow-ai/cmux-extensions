#!/usr/bin/env python3
"""Customization compat: every published setup in this registry, checked against a cmux NEXT nightly.

For one nightly (the newest in the feed, or the one --sha names) it:

1. resolves the nightly's commit from the feed (`<cmux:sourceCommit>` in appcast-arm64.xml);
2. gets a checker for exactly that commit: a published Linux `cmux` with `cmux setup check`
   when the nightly ships one (or --cmux names one), else a small harness
   (compat/harness) built from that commit's cmux-customize and cmux-config crates, which
   calls what `cmux setup install` calls (read_dir, plan_setup, install_setup) and also
   lists every setting the schema refuses;
3. clones each registry repo that holds a `cmux-setup.json` (setups.json, and the
   extensions.json entries that turn out to be setups) and checks it in an empty HOME;
4. writes a JSON report and a Markdown summary, with the diff against --previous
   (the last nightly's report): new breakage first.

    python3 compat/compat.py resolve [--sha SHA]
    python3 compat/compat.py run --work DIR --out-json r.json --out-md r.md [--previous p.json]

The repos are read, never run. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY = HERE.parent

FEED = "https://files-next.cmux.com/nightly-next/appcast-arm64.xml"
CMUX_REPO = "manaflow-ai/cmux"
SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
CMUX_NS = "https://cmux.com/xml-namespaces/cmux"
SETUP_FILE = "cmux-setup.json"
# The crates the harness depends on; compat grows the sparse checkout from them.
HARNESS_CRATES = ("cmux-customize", "cmux-config")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# A finding's setting, when its message names one.
KEY_RE = re.compile(r"(?:the setting|value for) ([A-Za-z0-9_.\-]+)")
SCHEMA = 1


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
        timeout: int = 1800, check: bool = True) -> subprocess.CompletedProcess:
    log("+ " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {result.returncode}: {result.stderr.strip()[-2000:]}")
    return result


def fetch(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "cmux-registry-compat"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# --- the nightly -------------------------------------------------------------------------


def parse_feed(xml_text: str) -> list[dict]:
    """The feed's builds, newest first: build number, version, commit, date."""
    root = ET.fromstring(xml_text)
    builds = []
    for item in root.iter("item"):
        commit = (item.findtext(f"{{{CMUX_NS}}}sourceCommit") or "").strip().lower()
        builds.append({
            "build": (item.findtext(f"{{{SPARKLE}}}version") or "").strip(),
            "version": (item.findtext(f"{{{SPARKLE}}}shortVersionString") or "").strip(),
            "sha": commit if SHA_RE.match(commit) else "",
            "published": (item.findtext("pubDate") or "").strip(),
        })
    builds.sort(key=lambda b: int(b["build"]) if b["build"].isdigit() else 0, reverse=True)
    return builds


def resolve(feed_url: str, sha: str = "") -> dict:
    """The nightly to check: the newest feed build with a commit, or the build of `sha`."""
    builds = parse_feed(fetch(feed_url).decode("utf-8"))
    if sha:
        sha = sha.strip().lower()
        if not SHA_RE.match(sha):
            raise SystemExit(f"--sha must be 40 hex digits, not {sha!r}")
        for build in builds:
            if build["sha"] == sha:
                return {**build, "feed": feed_url}
        # A commit that is not (or no longer) in the feed: checked as given.
        return {"build": "", "version": "", "sha": sha, "published": "", "feed": feed_url}
    for build in builds:
        if build["sha"]:
            return {**build, "feed": feed_url}
    raise SystemExit(f"no build in {feed_url} names its commit")


# --- cmux source at the nightly -----------------------------------------------------------


def workspace_paths(cmux_tui: Path) -> dict[str, str]:
    """`name = { path = "crates/x" }` entries of cmux-tui's [workspace.dependencies]."""
    text = (cmux_tui / "Cargo.toml").read_text()
    section = text.split("[workspace.dependencies]", 1)[-1]
    found = {}
    for match in re.finditer(r'^([A-Za-z0-9_-]+)\s*=\s*\{[^}\n]*path\s*=\s*"([^"]+)"', section, re.M):
        found[match.group(1)] = match.group(2)
    return found


def crate_deps(manifest: Path) -> set[str]:
    """Names a crate's [dependencies] (and target dependencies) take from the workspace."""
    names = set()
    in_deps = False
    for line in manifest.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_deps = stripped.endswith("dependencies]") and "dev-dependencies" not in stripped
            continue
        if in_deps:
            match = re.match(r"([A-Za-z0-9_-]+)(?:\.workspace\s*=\s*true|\s*=\s*\{[^}]*workspace\s*=\s*true)", stripped)
            if match:
                names.add(match.group(1))
    return names


INCLUDE_RE = re.compile(r'include_(?:str|bytes)!\(\s*(?:concat!\(\s*)?"([^"]+)"')


def needed_dirs(repo: Path, crate_dirs: set[str]) -> set[str]:
    """Repo-relative folders the crates' non-test sources include files from."""
    dirs = set()
    for crate in crate_dirs:
        for source in (repo / crate).rglob("*.rs"):
            if "tests" in source.relative_to(repo / crate).parts or source.name == "tests.rs":
                continue
            for match in INCLUDE_RE.finditer(source.read_text(errors="replace")):
                target = os.path.normpath(os.path.join(source.parent, match.group(1)))
                folder = os.path.dirname(target) if not match.group(1).endswith("/") else target
                rel = os.path.relpath(folder, repo)
                if not rel.startswith(".."):
                    dirs.add(rel)
    return dirs


def checkout_source(sha: str, dest: Path, remote: str) -> Path:
    """A sparse, blobless checkout of `sha` holding just what the harness builds from."""
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q"], cwd=dest)
        run(["git", "remote", "add", "origin", remote], cwd=dest)
    run(["git", "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", sha], cwd=dest, timeout=900)
    run(["git", "sparse-checkout", "init", "--cone"], cwd=dest)
    wanted = {f"cmux-tui/crates/{name}" for name in HARNESS_CRATES}
    first = True
    while True:
        run(["git", "sparse-checkout", "set", *sorted(wanted)], cwd=dest)
        if first:
            run(["git", "checkout", "-q", "--detach", sha], cwd=dest, timeout=900)
            first = False
        paths = workspace_paths(dest / "cmux-tui")
        crates = {d for d in wanted if d.startswith("cmux-tui/crates/") and (dest / d / "Cargo.toml").exists()}
        grown = set(wanted)
        for crate in crates:
            for dep in crate_deps(dest / crate / "Cargo.toml"):
                if dep in paths:
                    grown.add(os.path.normpath(f"cmux-tui/{paths[dep]}"))
        grown |= needed_dirs(dest, crates)
        if grown == wanted:
            return dest
        wanted = grown


def toolchain(cmux_tui: Path) -> str:
    match = re.search(r'channel\s*=\s*"([^"]+)"', (cmux_tui / "rust-toolchain.toml").read_text())
    return match.group(1) if match else "stable"


def build_harness(source: Path, work: Path, target_dir: Path) -> tuple[Path, str]:
    """compat/harness built against `source`'s crates, with cmux-tui's lock and toolchain."""
    cmux_tui = source / "cmux-tui"
    channel = toolchain(cmux_tui)
    crate = work / "harness"
    if crate.exists():
        shutil.rmtree(crate)
    shutil.copytree(HERE / "harness", crate)
    edition = re.search(r'^edition\s*=\s*"(\d+)"', (cmux_tui / "Cargo.toml").read_text(), re.M)
    deps = "\n".join(
        f'{name} = {{ path = "{(cmux_tui / "crates" / name).as_posix()}" }}' for name in HARNESS_CRATES)
    serde = re.search(r'^serde_json\s*=\s*(.+)$', (cmux_tui / "Cargo.toml").read_text(), re.M)
    serde_spec = serde.group(1) if serde else '"1"'
    edition_name = edition.group(1) if edition else "2024"
    (crate / "Cargo.toml").write_text(
        "[package]\nname = \"cmux-compat-check\"\nversion = \"0.0.0\"\n"
        f"edition = \"{edition_name}\"\npublish = false\n\n"
        f"[dependencies]\n{deps}\nserde_json = {serde_spec}\n\n"
        "[workspace]\n")
    # The nightly's own lock: every crate the harness pulls in is the version cmux ships with.
    shutil.copy(cmux_tui / "Cargo.lock", crate / "Cargo.lock")
    shutil.copy(cmux_tui / "rust-toolchain.toml", crate / "rust-toolchain.toml")
    if shutil.which("rustup"):
        run(["rustup", "toolchain", "install", channel, "--profile", "minimal", "--no-self-update"],
            timeout=900)
    env = {**os.environ, "CARGO_TARGET_DIR": str(target_dir), "RUSTUP_TOOLCHAIN": channel}
    run(["cargo", "build", "--release", "--quiet"], cwd=crate, env=env, timeout=3600)
    return target_dir / "release" / "cmux-compat-check", channel


def running_version(nightly: dict, source: Path) -> str:
    if nightly.get("version"):
        return nightly["version"]
    match = re.search(r'\[workspace\.package\][^\[]*?version\s*=\s*"([^"]+)"',
                      (source / "cmux-tui" / "Cargo.toml").read_text(), re.S)
    return match.group(1) if match else "0.0.0"


# --- a published CLI, when there is one ---------------------------------------------------


def published_cli(nightly: dict, work: Path) -> Path | None:
    """A Linux `cmux` attached to the nightly's release for this build, if the train ships one."""
    if not nightly.get("build"):
        return None
    arch = {"x86_64": "(?:amd64|x64|x86_64)", "aarch64": "arm64"}.get(os.uname().machine, "none")
    pattern = re.compile(rf"^cmux-(?:cli-)?linux-{arch}-{nightly['build']}$")
    try:
        release = json.loads(fetch(f"https://api.github.com/repos/{CMUX_REPO}/releases/tags/nightly-next"))
    except (urllib.error.URLError, ValueError) as error:
        log(f"could not read the nightly release ({error}); building the harness")
        return None
    for asset in release.get("assets", []):
        if pattern.match(asset.get("name", "")):
            binary = work / "cmux"
            binary.write_bytes(fetch(asset["browser_download_url"], timeout=300))
            binary.chmod(0o755)
            return binary
    return None


def has_setup_check(cmux: Path) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="compat-home-") as home:
            result = subprocess.run([str(cmux), "setup", "check", "--help"], capture_output=True,
                                    text=True, timeout=60, env=isolated_env(Path(home)))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


# --- checking one repo --------------------------------------------------------------------


def isolated_env(home: Path) -> dict:
    """An empty HOME and nothing of the runner's own cmux, config or tokens."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
            "CMUX_COMPAT_ISOLATED": "1"}


def finding_key(finding: dict) -> str | None:
    if finding.get("key"):
        return finding["key"]
    match = KEY_RE.search(finding.get("message", ""))
    return match.group(1) if match else None


def fold(findings: list[dict]) -> list[dict]:
    """One finding per problem: an install refusal of a setting the scan already named joins it."""
    out: list[dict] = []
    for raw in findings:
        finding = {"stages": [raw.get("stage", "check")], "code": raw.get("code") or "",
                   "key": finding_key(raw), "message": raw.get("message", "")}
        same = next((f for f in out if finding["key"] and f["key"] == finding["key"]), None)
        if same is None:
            same = next((f for f in out if f["message"] == finding["message"]), None)
        if same is not None:
            same["stages"] += [s for s in finding["stages"] if s not in same["stages"]]
        else:
            out.append(finding)
    return out


def check_harness(binary: Path, running: str, owner: str, folder: Path, home: Path) -> dict:
    result = subprocess.run([str(binary), running, owner, str(folder)], capture_output=True, text=True,
                            timeout=300, env=isolated_env(home))
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"ok": False, "error": True, "findings": [{
            "stage": "check", "code": "checker_crashed",
            "message": f"the check exited {result.returncode}: {result.stderr.strip()[-500:]}"}]}


def check_cli(cmux: Path, folder: Path, home: Path) -> dict:
    """`cmux setup check DIR --json`, read leniently: its findings under any of the usual names."""
    result = subprocess.run([str(cmux), "setup", "check", str(folder), "--json"], capture_output=True,
                            text=True, timeout=300, env=isolated_env(home))
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = {"findings": [{"stage": "check", "code": "checker_output",
                              "message": (result.stderr or result.stdout).strip()[-500:]}]}
    raw = next((data[k] for k in ("findings", "problems", "errors") if isinstance(data.get(k), list)), [])
    findings = [f if isinstance(f, dict) else {"message": str(f)} for f in raw]
    for f in findings:
        f.setdefault("stage", "check")
        f["message"] = f.get("message") or f.get("text") or json.dumps(f)
    if result.returncode != 0 and not findings:
        findings = [{"stage": "check", "code": data.get("code", "refused"),
                     "message": data.get("message") or data.get("error") or result.stderr.strip()[-500:]}]
    return {"ok": result.returncode == 0 and not findings, "id": data.get("id"), "findings": findings}


def entries(root: Path) -> list[dict]:
    """Every registry entry that may be a setup: setups.json, then extensions.json."""
    out = []
    for file, kind in (("setups.json", "setup"), ("extensions.json", "extension")):
        path = root / file
        if not path.exists():
            continue
        for entry in json.loads(path.read_text()):
            if isinstance(entry, dict) and isinstance(entry.get("repo"), str) and "/" in entry["repo"]:
                out.append({"id": entry.get("id") or entry["repo"], "name": entry.get("name", ""),
                            "repo": entry["repo"], "path": entry.get("path", ""), "kind": kind})
    return out


def check_entry(entry: dict, checker: dict, running: str, work: Path) -> dict | None:
    repo = entry["repo"]
    dest = work / "repos" / repo.replace("/", "__")
    base = {"id": entry["id"], "name": entry["name"], "repo": repo, "kind": entry["kind"]}
    if dest.exists():
        shutil.rmtree(dest)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    cloned = subprocess.run(["git", "clone", "-q", "--depth", "1", f"https://github.com/{repo}.git", str(dest)],
                            capture_output=True, text=True, timeout=300, env=env)
    if cloned.returncode != 0:
        if entry["kind"] != "setup":
            return None
        return {**base, "status": "error", "commit": "", "findings": [{
            "stages": ["fetch"], "code": "clone_failed", "key": None,
            "message": f"could not clone https://github.com/{repo}: {cloned.stderr.strip()[-300:]}"}]}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True).stdout.strip()
    folder = dest / entry["path"] if entry["path"] else dest
    if entry["kind"] != "setup" and not (folder / SETUP_FILE).exists():
        return None  # An extension package, not a setup: the review checks those.
    with tempfile.TemporaryDirectory(prefix="compat-home-") as home:
        if checker["mode"] == "cli":
            result = check_cli(Path(checker["binary"]), folder, Path(home))
        else:
            result = check_harness(Path(checker["binary"]), running, repo.split("/")[0], folder, Path(home))
    findings = fold(result.get("findings", []))
    status = "error" if result.get("error") else ("pass" if result.get("ok") and not findings else "fail")
    return {**base, "status": status, "commit": commit, "setup_id": result.get("id"),
            "version": result.get("version"), "findings": findings}


# --- report -------------------------------------------------------------------------------


def signature(finding: dict) -> str:
    return finding.get("key") or finding.get("message", "")


def diff(previous: dict | None, repos: list[dict]) -> dict:
    """What changed since the last nightly's report: broke, new findings, fixed, added, removed."""
    before = {r["id"]: r for r in (previous or {}).get("repos", [])}
    now = {r["id"]: r for r in repos}
    out = {"broke": [], "new_findings": {}, "fixed": [], "still_failing": [], "added": [], "removed": []}
    for rid, repo in now.items():
        old = before.get(rid)
        failing = repo["status"] != "pass"
        if old is None:
            out["added"].append(rid)
            if failing and previous is not None:
                out["broke"].append(rid)
            continue
        was_failing = old["status"] != "pass"
        if failing and not was_failing:
            out["broke"].append(rid)
        elif not failing and was_failing:
            out["fixed"].append(rid)
        elif failing:
            seen = {signature(f) for f in old.get("findings", [])}
            fresh = [signature(f) for f in repo["findings"] if signature(f) not in seen]
            if fresh:
                out["new_findings"][rid] = fresh
            out["still_failing"].append(rid)
    out["removed"] = sorted(set(before) - set(now))
    return out


def commit_link(sha: str) -> str:
    return f"[`{sha[:12]}`](https://github.com/{CMUX_REPO}/commit/{sha})" if sha else "(none)"


def render(report: dict) -> str:
    nightly, changes, repos = report["nightly"], report["diff"], report["repos"]
    by_id = {r["id"]: r for r in repos}
    lines = ["<!-- cmux-customization-compat -->", "## Customization compat",
             "",
             f"Nightly {nightly.get('version') or '(not in the feed)'} at {commit_link(nightly['sha'])}"
             f", checked with {report['checker']['description']}."]
    previous = report.get("previous")
    if previous:
        lines.append(f"Compared with the previous report: nightly {previous.get('version') or ''} at "
                     f"{commit_link(previous.get('sha', ''))}.")
    else:
        lines.append("No previous report to compare with: this is the first.")
    lines.append("")

    def findings_block(repo: dict) -> list[str]:
        out = []
        for f in repo["findings"]:
            key = f"`{f['key']}`: " if f.get("key") else ""
            out.append(f"  - {key}{f['message']} ({', '.join(f['stages'])})")
        return out

    if changes["broke"] or changes["new_findings"]:
        lines += ["### New breakage", ""]
        for rid in changes["broke"]:
            lines.append(f"- **{rid}** ([{by_id[rid]['repo']}](https://github.com/{by_id[rid]['repo']})) now fails:")
            lines += findings_block(by_id[rid])
        for rid, fresh in changes["new_findings"].items():
            lines.append(f"- **{rid}** fails in a new way: " + ", ".join(f"`{s}`" for s in fresh))
        lines.append("")
    if changes["fixed"]:
        lines += ["### Fixed since the previous nightly", ""] + [f"- {rid}" for rid in changes["fixed"]] + [""]

    passed = sum(r["status"] == "pass" for r in repos)
    lines += [f"### Every published setup ({passed} of {len(repos)} pass)", "",
              "| Setup | Repo | Commit | Result |", "| --- | --- | --- | --- |"]
    for r in repos:
        commit = f"[`{r['commit'][:7]}`](https://github.com/{r['repo']}/commit/{r['commit']})" if r["commit"] else ""
        result = {"pass": "pass", "fail": f"fail ({len(r['findings'])})", "error": "error"}[r["status"]]
        lines.append(f"| {r['id']} | [{r['repo']}](https://github.com/{r['repo']}) | {commit} | {result} |")
    lines.append("")
    failing = [r for r in repos if r["status"] != "pass"]
    if failing:
        lines += ["### Findings", ""]
        for r in failing:
            lines.append(f"- **{r['id']}**")
            lines += findings_block(r)
        lines.append("")
    if changes["removed"]:
        lines += ["Left the registry since the previous report: " + ", ".join(changes["removed"]), ""]
    if report["checker"].get("note"):
        lines += [f"<sub>{report['checker']['note']}</sub>", ""]
    return "\n".join(lines)


def build_report(nightly: dict, checker: dict, repos: list[dict], previous: dict | None) -> dict:
    prev = None
    if previous:
        prev = {k: previous.get("nightly", {}).get(k, "") for k in ("sha", "build", "version")}
    return {
        "schema": SCHEMA,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nightly": nightly,
        "checker": checker,
        "previous": prev,
        "repos": repos,
        "diff": diff(previous, repos),
    }


def load_previous(path: str | None) -> dict | None:
    if not path or not Path(path).is_file():
        return None
    try:
        data = json.loads(Path(path).read_text())
    except ValueError:
        log(f"ignoring {path}: not JSON")
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


def write_outputs(path: str | None, values: dict) -> None:
    if path:
        with open(path, "a") as out:
            for key, value in values.items():
                out.write(f"{key}={value}\n")


# --- commands -----------------------------------------------------------------------------


def cmd_resolve(args) -> int:
    nightly = resolve(args.feed, args.sha or "")
    previous = load_previous(args.previous)
    same = bool(previous) and previous["nightly"].get("sha") == nightly["sha"]
    print(json.dumps(nightly))
    write_outputs(args.github_output, {"sha": nightly["sha"], "build": nightly["build"],
                                       "already_checked": str(same).lower()})
    return 0


def cmd_run(args) -> int:
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    nightly = resolve(args.feed, args.sha or "")
    log(f"nightly {nightly['version'] or '?'} build {nightly['build'] or '?'} at {nightly['sha']}")
    source = checkout_source(nightly["sha"], work / "cmux", args.cmux_remote)
    running = running_version(nightly, source)

    cli = Path(args.cmux).resolve() if args.cmux else published_cli(nightly, work)
    if cli and has_setup_check(cli):
        checker = {"mode": "cli", "binary": str(cli),
                   "description": f"`cmux setup check` ({'given' if args.cmux else 'published for this build'})"}
    else:
        note = ("No Linux cmux with `cmux setup check` is published for this nightly, so the harness "
                "built from its source checked each setup the way `cmux setup install` does.")
        if cli:
            note = "The cmux given has no `cmux setup check`; " + note[0].lower() + note[1:]
        binary, channel = build_harness(source, work, Path(args.target_dir or work / "target").resolve())
        checker = {"mode": "harness", "binary": str(binary), "toolchain": channel,
                   "description": "the install check built from this nightly's source "
                                  "(cmux-customize read_dir, plan_setup, install_setup)",
                   "note": note}
    checker["running_version"] = running

    repos = []
    for entry in entries(Path(args.registry)):
        log(f"checking {entry['repo']}")
        result = check_entry(entry, checker, running, work)
        if result is not None:
            repos.append(result)
    report = build_report(nightly, {k: v for k, v in checker.items() if k != "binary"},
                          repos, load_previous(args.previous))
    Path(args.out_json).write_text(json.dumps(report, indent=2) + "\n")
    Path(args.out_md).write_text(render(report) + "\n")
    changes = report["diff"]
    new = bool(changes["broke"] or changes["new_findings"])
    write_outputs(args.github_output, {
        "sha": nightly["sha"], "new_breakage": str(new).lower(),
        "failing": sum(r["status"] != "pass" for r in repos)})
    log(f"{sum(r['status'] == 'pass' for r in repos)} of {len(repos)} pass; new breakage: {new}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--feed", default=os.environ.get("CMUX_COMPAT_FEED", FEED))
    sub = parser.add_subparsers(dest="command", required=True)
    resolve_p = sub.add_parser("resolve", help="print the nightly to check")
    resolve_p.add_argument("--sha", help="a nightly commit (default: the newest feed build)")
    resolve_p.add_argument("--previous", help="the previous report: already_checked when it is this nightly")
    resolve_p.add_argument("--github-output")
    run_p = sub.add_parser("run", help="check every published setup and write the report")
    run_p.add_argument("--sha", help="a nightly commit (default: the newest feed build)")
    run_p.add_argument("--work", required=True, help="a scratch folder")
    run_p.add_argument("--registry", default=str(REGISTRY))
    run_p.add_argument("--previous", help="the previous nightly's report JSON")
    run_p.add_argument("--cmux", help="a cmux binary to use when it has `cmux setup check`")
    run_p.add_argument("--cmux-remote", default=f"https://github.com/{CMUX_REPO}.git")
    run_p.add_argument("--target-dir", help="cargo's target folder (cached between runs)")
    run_p.add_argument("--out-json", required=True)
    run_p.add_argument("--out-md", required=True)
    run_p.add_argument("--github-output")
    args = parser.parse_args(argv)
    return cmd_resolve(args) if args.command == "resolve" else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
