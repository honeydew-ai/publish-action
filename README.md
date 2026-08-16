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

Add **one workflow per workspace** to the repository that stores your Honeydew metadata. The
`paths:` filter is the whole gate — GitHub runs the workflow only when the merged pull
request touched that workspace's directory, so nothing has to detect changes at run time and
no token is involved.

```yaml
# .github/workflows/publish-sales.yml
name: Publish Honeydew — sales

on:
  pull_request:
    types: [closed]
    branches: [main]
    paths: ['sales/**']          # ← the gate

permissions:
  contents: read

jobs:
  publish:
    if: github.event.pull_request.merged == true
    uses: ./.github/workflows/honeydew-publish.yml
    with:
      workspace: sales
      domain: sales_exec

      # One job per destination listed here. Keep only the ones you use.
      targets: '["powerbi", "sigma", "tableau", "thoughtspot"]'

      powerbi-model-name: Sales Exec
      powerbi-group-id: 3f2a1b4c-...

      sigma-connection-id: abc123
      sigma-folder-id: def456

      tableau-existing-datasource-id: 7a8b9c00-...

      thoughtspot-connection-name: honeydew
    secrets: inherit
```

The publish logic lives once in a reusable workflow that every per-workspace file calls, so
adding a workspace is one small file and nothing else:

```yaml
# .github/workflows/honeydew-publish.yml
on:
  workflow_call:
    inputs:
      workspace: {required: true, type: string}
      domain: {required: true, type: string}
      targets: {required: true, type: string}   # JSON array of destinations
      # ... every destination's inputs, all optional

jobs:
  publish:
    name: ${{ inputs.domain }} → ${{ matrix.target }}
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false           # one destination failing must not cancel the others
      matrix:
        target: ${{ fromJSON(inputs.targets) }}
    steps:
      - uses: honeydew-ai/publish-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
          workspace: ${{ inputs.workspace }}
          domain: ${{ inputs.domain }}
          target: ${{ matrix.target }}
          powerbi-model-name: ${{ inputs.powerbi-model-name }}
          powerbi-group-id: ${{ inputs.powerbi-group-id }}
          sigma-connection-id: ${{ inputs.sigma-connection-id }}
          sigma-folder-id: ${{ inputs.sigma-folder-id }}
          # ... and the Tableau and ThoughtSpot inputs
```

Both files, ready to copy, are in [`examples/`](examples). They list **all four destinations**
so every option is visible in one place — trim them: delete the destinations a workspace does
not publish to, both from `targets` and from the inputs below it. Inputs for destinations not
named in `targets` are ignored either way, so leaving them costs nothing but noise.

**What this gives you.** A merge touching only `finance/` never starts the `sales` workflow.
A merge touching both runs both. Each destination is its own job — its own pass or fail, its
own log, and its own **Re-run failed jobs** button — so a Sigma outage never hides or blocks
the Power BI publish.

<details>
<summary>One workflow for many workspaces</summary>

`paths:` is a workflow-level filter, so it cannot vary per matrix entry. If you would rather
have a single workflow covering every workspace, you have to work out which ones changed
yourself and gate the jobs on it — for example with
[`dorny/paths-filter`](https://github.com/dorny/paths-filter), or a `gh api` call over the
pull request's files. The action itself does no change detection; it publishes what you tell
it to.

Prefer the per-workspace files unless you have a reason not to: they need no extra
dependency, no token, and no API call.

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

## What gets published

The action publishes exactly what the step names: the `domain` of the `workspace`, on
`branch`. Nothing is inferred from the git branch the workflow runs on, and the action never
decides for itself whether to run — that is the workflow's `paths:` filter's job.

`branch` defaults to `prod`, because the common trigger is a merged pull request whose
content lands on `prod`. Set it explicitly to publish a development branch — for example, to
a staging BI workspace before merging.

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
| `workspace` | yes | | Honeydew workspace to publish from. |
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
HONEYDEW_DOMAIN=sales_exec \
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
