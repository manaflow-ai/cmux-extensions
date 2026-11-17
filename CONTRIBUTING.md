# Contributing

Adding an item is one pull request that adds one entry. The steps, the entry shape and what the
review checks are in the [README](README.md#submit-your-extension).

## One pull request, one item

- Add your entry at the end of the file for its kind. Order does not matter.
- Change nothing else. A pull request that also edits other entries, the review script or the
  workflows is closed.
- Use your own repo: the entry's `id` and `repo` start with your GitHub user or org.

## Before you open it

- The release is public and has both assets (extensions), or the file is on the default branch
  or a tag (themes, snippets, setups).
- Every scope in `scopes` and `optionalScopes` has a reason a person can judge on the consent card:
  what the extension does with it, in one sentence. Ask only for what you use.
- The README says what the item does and shows at least one screenshot.
- Run the review yourself:

  ```sh
  python3 -m venv .venv && .venv/bin/pip install -r review/requirements.txt
  .venv/bin/python review/review.py --all
  ```

## After it merges

Your item shows on cmux.com and in the App Store's Community section within the hour. New
releases reach people without another pull request. Edit your entry only to change its name,
description or location; remove it by pull request.

## Changing the review itself

The validator is [review/review.py](review/review.py) with tests in [review/tests](review/tests):
`python3 -m unittest discover -s review/tests`. The manifest schemas in
[schemas/vendor](schemas/vendor) are copies from the cmux app
(`cmux-tui/crates/cmux-app-host/schema/v2/`); update them together with the app.
