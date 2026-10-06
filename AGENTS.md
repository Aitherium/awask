# awask for agents

Read this if you are an agent (or a human) editing this package. Short on
purpose: the commands, the traps that cost a session, and where the rest lives.
Nothing here is read at runtime — it is for you.

## What this is

PyPI distribution **`awask`** (version in `pyproject.toml`), import package
`awask`, Python >= 3.10. Your agent asks you a question — and acts on your
answer. A question is a card with recipes; the answer resumes the work.

This repository is a **synced mirror** of the AitherOS monorepo (lane
`.github/workflows/sync-awask.yml`). Hand edits made here are overwritten on
the next sync — change the source and let the lane publish.

## Build, test, verify

```bash
python -m pytest tests -q        # the suite: 97 tests, green at v0.1.0
pip install -e .                 # editable install for developing against it
```

The suite was run from a source checkout with no prior install. The publish
lane (`publish-brick.yml`) additionally builds the wheel, installs it and
imports it — a tree that tests green can still ship a broken wheel.

## Rules that keep this useful

- **The pause lifecycle is the product.** `test_stop_steer_drain.py` pins the
  three ways a running ask can end — stopped, steered, answered-and-drained.
  A new exit path lands with its case, or an agent sits waiting forever on a
  question nobody can answer.
- **A question is a card with recipes, not prose.** `test_card_recipes.py`
  pins the shapes; a question the UI cannot render is a question that never
  gets answered, which reads from the outside as the agent ignoring you.
- **Every verb the CLI advertises exists.** `test_cli_store_verbs_exist.py`
  is the anti-ghost rule — help text that names a command the code does not
  have is this family's recurring defect shape.
- **The registry drives the public surface.** This repo's README header,
  `llms.txt` and `aither-manifest.json` are generated from the ecosystem
  registry (one yaml in the AitherOS monorepo) and rewritten on every sync.
  Change the registry; do not hand-edit the generated blocks.

## Read next

- `llms.txt` — the install/use card written for an agent to execute
- `README.md` — the human front door
- `docs/` — the generated docs site source
