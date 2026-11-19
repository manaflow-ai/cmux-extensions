# cmux community extensions

This repository is the list of community extensions, themes, snippets and setups for
[cmux](https://cmux.com). Everything listed here shows on [cmux.com/extensions](https://cmux.com/extensions)
and in the Community section of the cmux App Store, and installs in one click.

Each item lives in its author's own git repository. This repository only lists it: an item is
listed once its entry is on `main` here.

| File | What it lists | Where the item lives |
|---|---|---|
| [`extensions.json`](extensions.json) | Plugins: an app manifest v2 (`cmux-app.json` or `cmux-app.v2.json`) and its code, run in the sandboxed app host with the scopes it declares | The repo's releases |
| [`themes.json`](themes.json) | Themes: cmux theme files (`themes/NAME.json`, the format `cmux theme check` reads), no code | `cmux-theme.json` and `themes/*.json` in the repo |
| [`snippets.json`](snippets.json) | Snippets: a few settings and/or one surface stylesheet, no code | `cmux-snippet.json` in the repo |
| [`setups.json`](setups.json) | Setups: a whole customization (theme, keymap, sidebar, actions, extensions) in one install | `cmux-setup.json` in the repo |

## Submit your extension

1. Make the repo. `cmux ext new NAME --repo OWNER/REPO` writes a ready one, or start from the
   template [manaflow-ai/cmux-extension-sample](https://github.com/manaflow-ai/cmux-extension-sample).
   `cmux ext publish` checks it is ready and prints the release command.
2. Publish a release. Its tag is the manifest `version`, with or without a `v` (`1.2.0` or `v1.2.0`) and it has two assets:
   - `cmux-app.json` (or `cmux-app.v2.json`; v2 wins when a release has both): the manifest
   - `cmux-extension.tar.gz`: the built package (the manifest and every file it lists), made by `cmux ext pack`

   Themes, snippets and setups need no assets: the review reads the marker file (`cmux-theme.json`,
   `cmux-snippet.json`, `cmux-setup.json`) at the newest release's tag, else the newest tag, else the
   default branch. `cmux setup export` writes a setup repo.
3. Fork this repository and add one entry to the end of the right file:

   ```json
   {
     "id": "your-name/focus-timer",
     "name": "Focus Timer",
     "author": "Your Name",
     "description": "A sidebar timer for focus sessions.",
     "repo": "your-name/cmux-focus-timer"
   }
   ```

   - `id`: lowercase `OWNER/NAME`, starting with your GitHub user or org (lowercased). For an extension it
     is the manifest `id`. `cmux/` and `manaflow-ai/` are reserved.
   - `repo`: `OWNER/REPO` on GitHub, or `https://HOST/OWNER/REPO` on another git host.
   - `path` (optional): the folder holding the manifest or marker file when it is not the repo root
     (several items in one repo).
   - `description`: one sentence, at most 250 characters.
   The shape is [schemas/registry-entries.schema.json](schemas/registry-entries.schema.json).
4. Open a pull request. Fill in the checklist. The automated review comments within a few minutes.

To update an item, publish a new release (or push to the file): cmux picks up new releases on its own.
Edit your entry only to change its name, description or location.

You can also add the topic `cmux-extension` (or `cmux-setup`) to your repo: cmux.com lists such repos
as candidates and links them to a prefilled pull request here.

## What the automated review checks

[`review/review.py`](review/review.py) runs on every pull request ([`Review`](.github/workflows/review.yml)),
then again with `main`'s copy, which posts one summary comment and the `registry-review` status
([`Post review`](.github/workflows/post-review.yml)).

All files:
- each file is a JSON array of entries with the right shape; ids are unique across all four files
- the `id` starts with the repo owner, lowercased; `cmux/` and `manaflow-ai/` only from `manaflow-ai`
- a repo (and path) is listed once per file

Each added or changed entry, fetched from its repo:
- extension: the newest release with both assets; the manifest asset passes the manifest v2 schema
  ([vendored](schemas/vendor/) from the cmux app); its `id` is the entry's; the tag is the manifest
  version; `repository` is the repo; every scope and optional scope has a reason (a sentence, not
  `todo`); `cmux-extension.tar.gz` unpacks safely (no links, no `..`, size bounds), its manifest
  equals the release's, the files the manifest names are in it and listed in `files`, the script has
  no hidden characters and parses (`node --check`, never run), the catalog fragment passes its schema
- theme: `cmux-theme.json` with `"cmuxTheme": 1` and the entry's `id`; each theme file it names (default
  every `themes/*.json`, at most 16, each 256 KiB or less) parses (comments allowed) as an object, has an
  object `colors` if any, and does not set `appearance.themeFile`; `use` names one of them
- snippet: `cmux-snippet.json` with `"cmuxSnippet": 1` and the entry's `id`; it changes something;
  no `actions`, `automations`, `extensions` or views, no `appearance.themeFile`; it carries only the
  surface stylesheets (`agent-pane`, `markdown`, `diff`, `picker`, `fileViewer` `theme.css`) and
  `keybindings.json`; stylesheets have no `@import` and no remote `url(...)`
- setup: `cmux-setup.json` with `"cmuxSetup": 1`, the entry's `id`, a name and a description; against
  the newest cmux nightly (the commit its appcast names), each setting is a key that nightly has (a
  renamed key names its new one), one an agent may set, with a value its row accepts, as cmux
  checks at install; the custom properties `agent-pane/theme.css` sets are `cmux.agentPane.style/1`
  tokens (a warning: an unknown one paints nothing). A key the nightly lacks is an error while its
  install refuses the whole setup for it, and a warning once it leaves the key out instead
- themes, snippets and setups: a `version` in the marker matches the newest tag
- a README (it is the listing's page); a warning when it shows no image or no badge
- items on hosts other than GitHub: a person checks them by hand

## What a reviewer glances at

The comment lists, for each entry, what a person should look at before merging:
- the scopes and their reasons: does the extension need them for what it says it does?
- the actions a setup adds and the commands they run, and the extensions it installs
- the README: does it say what the item does, with a screenshot?
- the name and description: clear, not misleading, not taken

A merge into `main` is the approval. A listed item's new releases are not reviewed again here: cmux's
consent card shows every scope on install, and asks again when an update adds one.

## Get it on cmux badge

Put this in your README (replace `OWNER/REPO`):

```markdown
[![Get it on cmux](https://cmux.com/extensions/badge.svg)](https://cmux.com/extensions/OWNER/REPO)
```

For a setup, link to `https://cmux.com/setups/OWNER/REPO`.

## Links

- Browse: [cmux.com/extensions](https://cmux.com/extensions)
- Build an extension: [extension dev loop](https://github.com/manaflow-ai/cmux/blob/feat-cmux-next/docs/cmux-next/extensions/dev-loop.md),
  [App Store and manifest v2](https://github.com/manaflow-ai/cmux/blob/feat-cmux-next/docs/app-store-usage.md)
- Contributing here: [CONTRIBUTING.md](CONTRIBUTING.md)

## Removal

An item is removed when it breaks cmux, misleads people, asks for scopes it does not need, or its repo
goes away. Authors can remove their own entry by pull request.
