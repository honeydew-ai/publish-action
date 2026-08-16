# Honeydew Publish Action

[![GitHub Marketplace](https://img.shields.io/badge/Marketplace-Honeydew%20Publish-blue?logo=github)](https://github.com/marketplace/actions/honeydew-publish)

A GitHub Action that publishes a [Honeydew](https://honeydew.ai) semantic-layer workspace
branch to a BI tool — **Power BI**, **Sigma**, **Tableau** or **ThoughtSpot** — so merging a
pull request updates the models your analysts use.

The action calls the [Honeydew GraphQL API](https://honeydew.ai/docs/integration/graphql-api)
directly. It has no dependencies and does not require checking out the repository.

For validating a workspace *before* it merges, see
[validate-workspace-action](https://github.com/honeydew-ai/validate-workspace-action).

## Usage

Publish to Power BI whenever a pull request is merged into the default branch:

```yaml
# .github/workflows/honeydew-publish.yml
name: Publish Honeydew Workspace

on:
  pull_request:
    types: [closed]
    branches: [main]

jobs:
  publish:
    if: github.event.pull_request.merged == true
    runs-on: ubuntu-latest
    steps:
      - uses: honeydew-ai/publish-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
          target: powerbi
          domain: sales_exec
          powerbi-model-name: Sales Exec
          powerbi-group-id: ${{ vars.POWERBI_GROUP_ID }}
```

The workspace is detected from the merged branch name, and the Honeydew branch defaults to
`prod` — the branch a merge produces.

### Publishing two domains to two destinations

Each step publishes one domain to one destination, so publishing `sales_exec` to Power BI and
`sales_ops` to Tableau is two steps. Writing them out separately is the clearest form when
the destinations need different inputs:

```yaml
# .github/workflows/publish-honeydew.yml
name: Publish Honeydew Workspace

on:
  pull_request:
    types: [closed]
    branches: [main]

permissions:
  contents: read

jobs:
  publish:
    if: github.event.pull_request.merged == true
    runs-on: ubuntu-latest
    steps:
      - name: Publish sales_exec to Power BI
        uses: honeydew-ai/publish-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
          target: powerbi
          domain: sales_exec
          powerbi-model-name: Sales Exec
          powerbi-group-id: ${{ vars.POWERBI_GROUP_ID }}

      - name: Publish sales_ops to Tableau
        uses: honeydew-ai/publish-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
          target: tableau
          domain: sales_ops
          # Updates the existing data source. On the first run, swap this for
          # tableau-datasource-name + tableau-project-id to create it.
          tableau-existing-datasource-id: ${{ vars.TABLEAU_SALES_OPS_ID }}
```

Steps run in order, and a failure stops the ones after it. To publish them independently —
each with its own pass or fail in the GitHub UI, and running in parallel — use a matrix
instead:

```yaml
jobs:
  publish:
    if: github.event.pull_request.merged == true
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        include:
          - target: powerbi
            domain: sales_exec
          - target: tableau
            domain: sales_ops
    steps:
      - uses: honeydew-ai/publish-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
          target: ${{ matrix.target }}
          domain: ${{ matrix.domain }}
          powerbi-model-name: Sales Exec
          powerbi-group-id: ${{ vars.POWERBI_GROUP_ID }}
          tableau-existing-datasource-id: ${{ vars.TABLEAU_SALES_OPS_ID }}
```

Inputs for other destinations are ignored, so one step definition can serve the whole matrix.
The trade-off is that every destination's inputs share one block, which gets unwieldy once
they differ much — prefer separate steps in that case.

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

## How the workspace is detected

Honeydew names development git branches `<workspace>/<branch>` — for example, branch
`q3-fixes` of workspace `sales` lives on the git branch `sales/q3-fixes`. The action reads
the workspace from that name (`github.head_ref` on a pull request, otherwise the current
ref), or from the explicit `workspace` input.

Only the **workspace** is detected. The Honeydew branch to publish comes from the `branch`
input and defaults to `prod`, because the common trigger is a merged pull request whose
content lands on `prod`. Set `branch` explicitly to publish a development branch — for
example, to a staging BI workspace before merging.

Before publishing, the action reloads the workspace from git (`reset_workspace`) so the
published model reflects the latest commit. The reload runs in the API key's own session and
does not affect anyone editing the workspace in Honeydew Studio. Set `reload: 'false'` to
skip it.

## Updating versus duplicating

Publishing on every merge must *update* the object rather than create another one. How that
works depends on the destination:

| Destination | Repeat publishes |
|---|---|
| **Power BI** | Updates the model with the same `powerbi-model-name` in the workspace. |
| **ThoughtSpot** | Updates the table with the same `thoughtspot-table-name`. |
| **Tableau** | Pass `tableau-existing-datasource-id` to update. `tableau-datasource-name` + `tableau-project-id` **create** a new data source. |
| **Sigma** | Pass `sigma-existing-data-model-id` to update. Without it, a new data model is **created** every run. |

For Tableau and Sigma, run the action once to create the object, take the id from the job
summary (or the `id` output), and store it as a repository variable:

```yaml
      - uses: honeydew-ai/publish-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
          target: sigma
          domain: sales_ops
          sigma-connection-id: ${{ vars.SIGMA_CONNECTION_ID }}
          sigma-folder-id: ${{ vars.SIGMA_FOLDER_ID }}
          sigma-existing-data-model-id: ${{ vars.SIGMA_DATA_MODEL_ID }}
```

List the ids of objects that already exist with the
[GraphQL API](https://honeydew.ai/docs/integration/graphql-api#publish-to-bi-tools):
`tableau_honeydew_datasources` for Tableau, and `powerbi_workspaces`, `sigma_folders`,
`sigma_connections`, `tableau_projects` and `thoughtspot_connections` for the ids the inputs
below take.

## Inputs

### Common

| Input | Required | Default | Description |
|---|---|---|---|
| `api-key` | yes | | Honeydew API key name. |
| `api-secret` | yes | | Honeydew API key secret. |
| `target` | yes | | `powerbi`, `sigma`, `tableau` or `thoughtspot`. |
| `base-url` | no | `https://api.honeydew.cloud` | Honeydew API base URL. Only set this if your organization uses a custom hostname (see **Settings > API** in the Honeydew UI). |
| `workspace` | no | auto-detected | Honeydew workspace to publish. |
| `branch` | no | `prod` | Honeydew branch to publish. |
| `domain` | yes | | Domain to publish. |
| `connector-name` | no | `default` | Name of the connector configured in Honeydew for the target tool. |
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
| `url` | Link to the published object in the target tool. |
| `id` | ID of the published object, for the destinations that report one (Sigma data model, ThoughtSpot table). |
| `warning` | Errors reported by steps that ran after a successful publish — refreshing the Power BI model, applying Sigma version tags. Empty when there were none. |

A warning never means the publish failed. Set `fail-on-warning: 'true'` to fail the step
anyway.

## Running locally

For development and testing, the script can run outside GitHub Actions and authenticate with
a user bearer token instead of an API key:

```bash
HONEYDEW_BASE_URL=http://localhost:5000 \
HONEYDEW_TOKEN="<your token>" \
HONEYDEW_TARGET=powerbi \
HONEYDEW_WORKSPACE=sales \
HONEYDEW_BRANCH=prod \
HONEYDEW_CONNECTOR_NAME=default \
HONEYDEW_POWERBI_MODEL_NAME="Sales Exec" \
HONEYDEW_POWERBI_GROUP_ID=<group id> \
python3 publish.py
```

`HONEYDEW_TOKEN` takes precedence over `HONEYDEW_API_KEY` / `HONEYDEW_API_SECRET`. Every
input maps to `HONEYDEW_` plus its upper-cased name with dashes as underscores.

## Output

- A summary table is written to the job summary, with a link to the published object and the
  id to reuse on the next run.
- Warnings are reported as GitHub warning annotations, and errors as error annotations.
- The action fails (non-zero exit) if publishing fails.

## License

Copyright 2026 Honeydew Data Inc.

[Apache License 2.0](LICENSE)
