# Honeydew Publish Action

[![GitHub Marketplace](https://img.shields.io/badge/Marketplace-Honeydew%20Publish-blue?logo=github)](https://github.com/marketplace/actions/honeydew-publish)

A GitHub Action that publishes a [Honeydew](https://honeydew.ai) domain to a BI tool —
**Power BI**, **Sigma**, **Tableau** or **ThoughtSpot** — so merging a pull request updates
the models your analysts use.

One run publishes one **(workspace, domain, BI tool)** combination, and only when **that
workspace changed** in the merged pull request. Fan out over a job matrix and every
combination gets its own line in the Actions UI: its own pass or fail, its own log, and its
own **Re-run failed jobs** button.

The action calls the [Honeydew GraphQL API](https://honeydew.ai/docs/integration/graphql-api)
directly. It has no dependencies and does not check out the repository.

For validating a workspace *before* it merges, see
[validate-workspace-action](https://github.com/honeydew-ai/validate-workspace-action).

## Usage

```yaml
# .github/workflows/honeydew-publish.yml
name: Publish Honeydew Domains

on:
  pull_request:
    types: [closed]
    branches: [main]

permissions:
  contents: read
  pull-requests: read   # lets the action see whether the workspace changed

jobs:
  publish:
    if: github.event.pull_request.merged == true
    runs-on: ubuntu-latest
    # One job per combination, named so the Actions UI shows what deployed.
    name: ${{ matrix.workspace }}/${{ matrix.domain }} → ${{ matrix.target }}
    strategy:
      fail-fast: false        # one destination failing must not cancel the others
      matrix:
        include:
          - workspace: sales
            domain: sales_exec
            target: powerbi
            model_name: Sales Exec
            group_id: 3f2a1b4c-...

          - workspace: sales
            domain: sales_exec
            target: sigma
            connection_id: abc123
            folder_id: def456

          - workspace: finance
            domain: finance_core
            target: thoughtspot
            connection_name: honeydew
    steps:
      - uses: honeydew-ai/publish-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
          github-token: ${{ github.token }}
          workspace: ${{ matrix.workspace }}
          domain: ${{ matrix.domain }}
          target: ${{ matrix.target }}
          powerbi-model-name: ${{ matrix.model_name }}
          powerbi-group-id: ${{ matrix.group_id }}
          sigma-connection-id: ${{ matrix.connection_id }}
          sigma-folder-id: ${{ matrix.folder_id }}
          thoughtspot-connection-name: ${{ matrix.connection_name }}
```

Inputs belonging to a different `target` are ignored, so one step definition serves the whole
matrix. A matrix entry that omits a key leaves it empty, which is the same as not setting it.

On a merge touching only `finance/`, the two `sales` jobs report **skipped** and pass, and the
`finance` job publishes. Each job's summary says which it was.

<details>
<summary>Separate steps instead of a matrix</summary>

If you prefer one job with several steps, that works too — but a failing step stops the ones
after it, and re-running retries all of them. The matrix is what buys per-combination
re-runs.

```yaml
    steps:
      - name: Publish sales_exec to Power BI
        uses: honeydew-ai/publish-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
          github-token: ${{ github.token }}
          workspace: sales
          domain: sales_exec
          target: powerbi
          powerbi-model-name: Sales Exec
          powerbi-group-id: 3f2a1b4c-...
```

</details>

### Pinning a version

`@v1` tracks the latest v1 release and receives patches automatically. For reproducible
builds — or if you prefer to review each update yourself — pin to a full release tag or
commit SHA instead:

```yaml
      - uses: honeydew-ai/publish-action@v1.0.0   # exact release
      # or
      - uses: honeydew-ai/publish-action@<commit-sha>
```

## Prerequisites

1. **Public GraphQL API enabled** — the Honeydew public API is not enabled by default.
   Contact [support@honeydew.ai](mailto:support@honeydew.ai) to enable it for your organization.
2. **The destination connector configured in Honeydew** — set it up from the user settings
   menu under **Power BI** / **Sigma** / **Tableau** / **ThoughtSpot** → **Settings**. A
   connector configured this way is named `default`, which is this action's default.
3. **API key** — create an API key and secret in Honeydew
   (see [API Keys](https://honeydew.ai/docs/access-control/api-keys)).
   Publishing requires the **Editor** role.
4. **GitHub secrets** — store the key and secret as repository secrets
   (`HONEYDEW_API_KEY` and `HONEYDEW_API_SECRET` in the examples above).

## Publishing only what changed

Pass `github-token: ${{ github.token }}` and grant `pull-requests: read`. The action reads
the merged pull request's changed files and publishes only if a file under the `workspace/`
directory changed.

| Situation | What happens |
|---|---|
| The `workspace` directory changed | Published |
| It did not change | `status: skipped`, and the job **passes** |
| No `github-token`, or not a pull request event | Published |

That last row is deliberate: "cannot tell" publishes rather than skips, because silently
doing nothing on a `workflow_dispatch` run is the more damaging way to be wrong. If you want
the gate enforced, always pass the token.

Before publishing, the action reloads the workspace from git (`reset_workspace`) so the
published model reflects the merged commit. The reload runs in the API key's own session and
does not affect anyone editing the workspace in Honeydew Studio. Set `reload: 'false'` to
skip it.

## Updating versus duplicating

Publishing on every merge must *update* the object rather than create another one. How that
works depends on the destination:

| Destination | Repeat publishes |
|---|---|
| **Power BI** | Updates the model with the same `powerbi-model-name` in the workspace. |
| **ThoughtSpot** | Updates the table with the same `thoughtspot-table-name`. |
| **Tableau** | Pass `tableau-existing-datasource-id` to update. A name + project ID **create** a new data source. |
| **Sigma** | Pass `sigma-existing-data-model-id` to update. Without it, a new data model is **created** every run. |

For Tableau and Sigma, run the action once to create the object, take the ID from the job
summary (or the `id` output), and store it as a repository variable:

```yaml
          sigma-existing-data-model-id: ${{ vars.SIGMA_SALES_EXEC_MODEL_ID }}
```

List the IDs of objects that already exist with the
[GraphQL API](https://honeydew.ai/docs/integration/graphql-api#publish-to-bi-tools):
`powerbi_workspaces`, `sigma_connections`, `sigma_folders`, `tableau_projects`,
`tableau_honeydew_datasources` and `thoughtspot_connections`.

## Inputs

### Common

| Input | Required | Default | Description |
|---|---|---|---|
| `api-key` | yes | | Honeydew API key name. |
| `api-secret` | yes | | Honeydew API key secret. |
| `workspace` | yes | | Honeydew workspace, and the directory whose changes gate this publish. |
| `domain` | yes | | Domain to publish. |
| `target` | yes | | `powerbi`, `sigma`, `tableau` or `thoughtspot`. |
| `github-token` | no | | Token used to check whether the workspace changed. Omit to always publish. |
| `branch` | no | `prod` | Honeydew branch to publish. |
| `connector-name` | no | `default` | Connector configured in Honeydew for the target tool. |
| `base-url` | no | `https://api.honeydew.cloud` | Honeydew API base URL. Set this only if your organization uses a custom hostname. |
| `reload` | no | `true` | Reload the workspace from git before publishing. |
| `fail-on-warning` | no | `false` | Fail the step when the publish succeeded but a follow-up step reported an error. |

### Per destination

| Input | Destination | Required | Description |
|---|---|---|---|
| `powerbi-model-name` | Power BI | yes | Name of the semantic model to create or update. |
| `powerbi-group-id` | Power BI | yes | ID of the Power BI workspace to publish into. |
| `sigma-connection-id` | Sigma | yes | ID of the Sigma connection to the data warehouse. |
| `sigma-folder-id` | Sigma | yes | ID of the Sigma folder to publish into. |
| `sigma-model-name` | Sigma | no | Name of the data model. Defaults to the domain's display name. |
| `sigma-existing-data-model-id` | Sigma | no | ID of the data model to update. Omit to create a new one. |
| `sigma-tags` | Sigma | no | Comma-separated version tags to apply. |
| `tableau-datasource-name` | Tableau | no | Name of the data source to create. Requires `tableau-project-id`. |
| `tableau-project-id` | Tableau | no | ID of the project to create the data source in. |
| `tableau-existing-datasource-id` | Tableau | no | ID of the data source to update. |
| `thoughtspot-connection-name` | ThoughtSpot | yes | Name of the Honeydew connection in ThoughtSpot. |
| `thoughtspot-table-name` | ThoughtSpot | no | Name of the table. Defaults to the domain's display name. |

Tableau requires **either** `tableau-existing-datasource-id`, **or** both
`tableau-datasource-name` and `tableau-project-id` — the action fails if you set neither or
mix the two.

## Outputs

| Output | Description |
|---|---|
| `status` | `published`, or `skipped` when the workspace did not change. |
| `url` | Link to the published object in the target tool. |
| `id` | ID of the published object, for the destinations that report one (Sigma data model, ThoughtSpot table). |
| `warning` | Errors reported by steps that ran after a successful publish — refreshing the Power BI model, applying Sigma version tags. Empty when there were none. |

A warning means the publish itself succeeded. Set `fail-on-warning: 'true'` to fail the step
anyway.

## Running locally

For development and testing, the script can run outside GitHub Actions and authenticate with
a user bearer token instead of an API key:

```bash
HONEYDEW_BASE_URL=http://localhost:5000 \
HONEYDEW_TOKEN="<your token>" \
HONEYDEW_WORKSPACE=sales \
HONEYDEW_DOMAIN=sales_exec \
HONEYDEW_TARGET=powerbi \
HONEYDEW_CONNECTOR_NAME=default \
HONEYDEW_POWERBI_MODEL_NAME="Sales Exec" \
HONEYDEW_POWERBI_GROUP_ID=<group id> \
python3 publish.py
```

`HONEYDEW_TOKEN` takes precedence over `HONEYDEW_API_KEY` / `HONEYDEW_API_SECRET`. Every
input maps to `HONEYDEW_` plus its upper-cased name with dashes as underscores. With no
`HONEYDEW_GITHUB_TOKEN`, the publish always runs.

## Output

- A summary table is written to the job summary, with a link to the published object and the
  ID to reuse on the next run.
- A skip is reported as a notice annotation; warnings and errors as warning and error
  annotations.
- The action fails (non-zero exit) if publishing fails. A skip exits zero.

## License

Copyright 2026 Honeydew Data Inc.

[Apache License 2.0](LICENSE)
