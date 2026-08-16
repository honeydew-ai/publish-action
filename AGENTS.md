# Honeydew Publish Action — Development Guidelines

This repository contains a GitHub Action that publishes
[Honeydew](https://honeydew.ai) semantic-layer workspace branches to BI tools via
the [Honeydew GraphQL API](https://honeydew.ai/docs/integration/graphql-api).

Its sibling, [validate-workspace-action](https://github.com/honeydew-ai/validate-workspace-action),
validates a workspace before merge. The two repos deliberately duplicate
`HoneydewClient` rather than share it — both must stay dependency-free, so a
shared package would mean a release-coupled submodule for ~150 lines. Fix
client-layer bugs in both.

## Repository Structure

- **`action.yml`** — the composite action definition (inputs, env wiring, outputs)
- **`publish.py`** — the action's entire logic; runs with `python3` on the runner
- **`test_publish.py`** — pytest tests for `publish.py`
- **`.github/workflows/ci.yml`** — CI (tests, formatting, typing)

## Adding a destination

Every destination is one `Destination` entry in `DESTINATIONS` in `publish.py`:
the mutation to call, its arguments, and the result fields to read back. The rest
of the file is destination-agnostic, so a new BI tool — or a data catalog such as
Atlan (`sync_atlan`) — needs no new branching.

1. Add the `Destination` entry, mapping each mutation argument to an `Argument`
   with its action input name.
2. Add those inputs to `action.yml`, prefixed with the destination key
   (`atlan-connection-qualified-name`), and pass them through in the `env:` block.
3. Add a row to the README's per-destination input table and to the
   "Updating versus duplicating" table.
4. Add parametrized cases to the `collect_arguments`, `build_mutation` and
   `publish` tests.

Keep destination-specific logic in the descriptor, not in `if target == ...`
branches. Argument combinations a single `required` flag cannot express go in an
`extra_check` (see `_check_tableau_arguments`).

Some destinations do not fit the shared inputs: `sync_atlan` takes no `domain` at
all, and `sync_quicksight_datasource` names its connector argument `name` rather
than `connector_name`. The descriptor already carries the API-side name per
argument, so both are expressible — but a destination with no `domain` needs its
own handling of the `domain` input rather than silently ignoring it.

## Hard Constraints

- **`publish.py` must use only the Python standard library.** The action runs
  directly on customer runners without a `pip install` step — never add runtime
  dependencies.
- **No checkout requirement.** The action must keep working without
  `actions/checkout`; everything it needs comes from environment variables and
  the Honeydew API.
- **Never put untrusted GitHub event data into shell commands.** Inputs flow
  into Python via `env:` only.
- **Never retry a publish.** `sync_*` mutations are not idempotent when they
  create; a retry after a timeout can publish the same model twice. `publish()`
  passes `retries=0`, and only idempotent calls (`reset_workspace`, queries) use
  the default retries.

## Python Guidelines

- Target Python 3.12+ syntax on runners; type-check with mypy (strict) at 3.12.
- Use modern type syntax: `X | None`, `list[str]`, `dict[str, int]` — import
  from `typing` only for advanced types (`typing.Any`, `typing.NoReturn`,
  `typing.cast`).
- Formatting and linting are enforced by pre-commit (black, ruff with ALL rules
  including import sorting — see `ruff.toml` — yamllint, mypy, and more).
  Lines stay under 120 characters (the ruff limit; black wraps sooner).
- Use keyword-only arguments (`*` separator) for functions with multiple parameters.
- Module-level constants use ALL_CAPS.
- Use the walrus operator for assign-then-check patterns.
- Write self-explanatory code; comment only what the code cannot say.

## Testing

- Tests use pytest. Run with `pytest -v`.
- Parametrize similar tests with `@pytest.mark.parametrize`, attaching ids via
  `pytest.param(..., id="...")` — never copy-paste test bodies.
- Assert the full output with `==` (no partial `in`/`len` checks). Mutation
  documents are asserted as exact strings, so a change to `build_mutation` shows
  up as a readable diff.

## Checks

Run before committing (CI runs the same):

```bash
pre-commit run --all-files
pytest -v
```

Install the git hook once with `pre-commit install`.

## Releases

Customers pin the action as `honeydew-ai/publish-action@v1`.
After merging changes, create/move the `v1` major tag to the release commit.
