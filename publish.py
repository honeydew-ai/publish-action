# Copyright 2026 Honeydew Data Inc.
# SPDX-License-Identifier: Apache-2.0

"""Publish a Honeydew domain to a BI tool via the Honeydew GraphQL API.

Entry point of the honeydew-ai/publish-action GitHub Action.
Uses only the Python standard library, so it runs on any GitHub runner
without installing dependencies.

One run publishes one (workspace, domain, BI tool) target, so a caller fans out
over a job matrix and gets a separate pass, fail and re-run per combination.
The target is published only when its workspace changed in the merged pull
request; otherwise the run reports "skipped" and succeeds.

Every destination is one entry in DESTINATIONS: the mutation to call, the
arguments it takes, and the result fields to read back. The rest of this file
is destination-agnostic, so adding a destination is a new entry plus its
inputs in action.yml.
"""

import base64
import dataclasses
import enum
import json
import os
import sys
import time
import typing
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

MAIN_BRANCH = "prod"
PUBLIC_API_PATH = "/api/public/v1/graphql"
# Publishing builds the model in the destination tool and, for Power BI, refreshes
# it — far slower than a read, so this is well above the validate action's timeout.
REQUEST_TIMEOUT_SECONDS = 900
RETRIES = 5
RETRIED_HTTP_CODES = (429, 502, 503, 504)
# Exponential backoff bounded to a maximum single wait, matching the Honeydew
# server's own connectors (tenacity wait_exponential(multiplier=1, min=1, max=30)).
BACKOFF_MULTIPLIER_SECONDS = 1.0
BACKOFF_MIN_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
# A server-supplied Retry-After wins over the backoff, but is not trusted unbounded.
MAX_RETRY_AFTER_SECONDS = 60.0

GITHUB_API_ROOT = "https://api.github.com"
CHANGED_FILES_PER_PAGE = 100
# GitHub caps the pull request files listing at 3000 entries.
CHANGED_FILES_MAX_PAGES = 30

WORKFLOW_COMMAND_ESCAPES = str.maketrans({"%": "%25", "\r": "%0D", "\n": "%0A"})


class ArgKind(enum.StrEnum):
    """GraphQL type of a mutation argument, as declared in the mutation header."""

    STRING = "String"
    STRING_LIST = "[String!]"


ArgValue = str | list[str]


class ApiError(Exception):
    """The Honeydew API rejected a call or could not be reached."""


@dataclasses.dataclass(frozen=True)
class Argument:
    """One argument of a sync mutation, and the action input that fills it."""

    api_name: str
    input_name: str
    required: bool = False
    kind: ArgKind = ArgKind.STRING

    @property
    def env_name(self) -> str:
        return "HONEYDEW_" + self.input_name.upper().replace("-", "_")


@dataclasses.dataclass(frozen=True)
class Destination:
    """A place a domain can be published to, and how to address it.

    ``url_field``, ``id_field`` and ``warning_fields`` map the destination's own
    response type onto the action's outputs. A warning field holds the error of a
    step that runs *after* the publish succeeded (refreshing a Power BI model,
    tagging a Sigma version), so it never means the publish itself failed.
    """

    key: str
    label: str
    mutation: str
    arguments: tuple[Argument, ...]
    url_field: str
    id_field: str | None = None
    warning_fields: tuple[str, ...] = ()
    extra_check: typing.Callable[[dict[str, ArgValue]], str] | None = None


def _check_tableau_arguments(values: dict[str, ArgValue]) -> str:
    """Tableau updates by id and creates by name+project; anything else is an error."""
    updating = "existing_datasource_id" in values
    creating = "datasource_name" in values and "project_id" in values
    if updating and ("datasource_name" in values or "project_id" in values):
        return (
            "Set either 'tableau-existing-datasource-id' to update an existing data "
            "source, or both 'tableau-datasource-name' and 'tableau-project-id' to "
            "create one — not both."
        )
    if not updating and not creating:
        return (
            "Set 'tableau-existing-datasource-id' to update an existing data source, "
            "or both 'tableau-datasource-name' and 'tableau-project-id' to create one."
        )
    return ""


CONNECTOR = Argument("connector_name", "connector-name", required=True)
# Required for every destination, though the API accepts publishing a whole workspace:
# a BI model built from an ungoverned full workspace is rarely what anyone wants.
DOMAIN = Argument("domain", "domain", required=True)

DESTINATIONS: tuple[Destination, ...] = (
    Destination(
        key="powerbi",
        label="Power BI",
        mutation="sync_powerbi_datasource",
        arguments=(
            CONNECTOR,
            DOMAIN,
            Argument("model_name", "powerbi-model-name", required=True),
            Argument("group_id", "powerbi-group-id", required=True),
        ),
        url_field="semantic_model_url",
        warning_fields=("refresh_error",),
    ),
    Destination(
        key="sigma",
        label="Sigma",
        mutation="sync_sigma_datasource",
        arguments=(
            CONNECTOR,
            DOMAIN,
            Argument("connection_id", "sigma-connection-id", required=True),
            Argument("folder_id", "sigma-folder-id", required=True),
            Argument("model_name", "sigma-model-name"),
            Argument("existing_data_model_id", "sigma-existing-data-model-id"),
            Argument("tags", "sigma-tags", kind=ArgKind.STRING_LIST),
        ),
        url_field="data_model_url",
        id_field="data_model_id",
        warning_fields=("tag_error",),
    ),
    Destination(
        key="tableau",
        label="Tableau",
        mutation="sync_tableau_datasource",
        arguments=(
            CONNECTOR,
            DOMAIN,
            Argument("datasource_name", "tableau-datasource-name"),
            Argument("project_id", "tableau-project-id"),
            Argument("existing_datasource_id", "tableau-existing-datasource-id"),
        ),
        url_field="datasource_url",
        extra_check=_check_tableau_arguments,
    ),
    Destination(
        key="thoughtspot",
        label="ThoughtSpot",
        mutation="sync_thoughtspot_datasource",
        arguments=(
            CONNECTOR,
            DOMAIN,
            Argument("connection_name", "thoughtspot-connection-name", required=True),
            Argument("table_name", "thoughtspot-table-name"),
        ),
        url_field="table_url",
        id_field="table_guid",
    ),
)


def print_error(message: str) -> None:
    print(f"::error::{escape_workflow_command(message)}")


def print_warning(message: str) -> None:
    print(f"::warning::{escape_workflow_command(message)}")


def print_notice(message: str) -> None:
    print(f"::notice::{escape_workflow_command(message)}")


def escape_workflow_command(message: str) -> str:
    """Percent-encode the characters that terminate or forge a workflow command.

    GitHub has no library for this and no alternative encoding: a command ends at
    the first newline, so unescaped API-provided text could forge a second command
    such as ``::add-mask::``. The Actions spec defines exactly these three
    replacements. A translation table applies them in one pass, so — unlike chained
    ``str.replace`` calls — the percent signs introduced here cannot be re-escaped
    by a later step.
    """
    return message.translate(WORKFLOW_COMMAND_ESCAPES)


def fail(message: str) -> typing.NoReturn:
    print_error(message)
    sys.exit(1)


def resolve_destination(target: str) -> Destination:
    if not target:
        fail(f"Missing required input: target. Expected one of: {_destination_keys()}.")
    for destination in DESTINATIONS:
        if destination.key == target:
            return destination
    fail(f"Unknown target '{target}'. Expected one of: {_destination_keys()}.")


def _destination_keys() -> str:
    return ", ".join(destination.key for destination in DESTINATIONS)


def collect_arguments(destination: Destination) -> dict[str, ArgValue]:
    """Read this destination's mutation arguments from the environment."""
    values: dict[str, ArgValue] = {}
    missing: list[str] = []
    for argument in destination.arguments:
        raw = os.environ.get(argument.env_name, "").strip()
        if not raw:
            if argument.required:
                missing.append(argument.input_name)
            continue
        if argument.kind is ArgKind.STRING_LIST:
            if items := [part.strip() for part in raw.split(",") if part.strip()]:
                values[argument.api_name] = items
        else:
            values[argument.api_name] = raw
    if missing:
        fail(
            f"Publishing to {destination.label} requires the "
            f"{_quoted_list(missing)} input(s).",
        )
    if destination.extra_check and (error := destination.extra_check(values)):
        fail(error)
    return values


def _quoted_list(names: list[str]) -> str:
    return ", ".join(f"'{name}'" for name in names)


def workspace_changed(
    workspace: str,
    *,
    token: str,
    repository: str,
    event_path: str,
) -> bool:
    """Whether the merged pull request touched anything under ``workspace/``.

    Returns ``True`` when it cannot tell — no token, or not a pull request event.
    Publishing on "cannot tell" is deliberate: skipping instead would silently do
    nothing on a manual run, which is the more damaging way to be wrong.
    """
    if not token:
        print(
            "No 'github-token' supplied: publishing without checking whether the "
            "workspace changed.",
        )
        return True
    if (number := _pull_request_number(event_path)) is None:
        print(
            "Not a pull request event: publishing without checking whether the "
            "workspace changed.",
        )
        return True
    if not repository:
        print("GITHUB_REPOSITORY is unset: cannot check whether the workspace changed.")
        return True
    paths = _fetch_changed_paths(token=token, repository=repository, number=number)
    changed = {path.split("/")[0] for path in paths if "/" in path}
    print(
        f"Pull request #{number} changed {len(paths)} file(s) across "
        f"{len(changed)} workspace(s): {', '.join(sorted(changed)) or 'none'}",
    )
    return workspace in changed


def _pull_request_number(event_path: str) -> int | None:
    if not event_path or not Path(event_path).exists():
        return None
    with Path(event_path).open(encoding="utf-8") as event_file:
        event = json.load(event_file)
    number = (event.get("pull_request") or {}).get("number")
    return int(number) if isinstance(number, int) else None


def _fetch_changed_paths(*, token: str, repository: str, number: int) -> set[str]:
    paths: set[str] = set()
    for page in range(1, CHANGED_FILES_MAX_PAGES + 1):
        url = (
            f"{GITHUB_API_ROOT}/repos/{repository}/pulls/{number}/files"
            f"?per_page={CHANGED_FILES_PER_PAGE}&page={page}"
        )
        batch = _github_get(url, token)
        paths.update(str(entry["filename"]) for entry in batch)
        if len(batch) < CHANGED_FILES_PER_PAGE:
            return paths
    print_warning(
        f"Pull request #{number} lists more files than this action reads "
        f"({CHANGED_FILES_MAX_PAGES * CHANGED_FILES_PER_PAGE}); a changed workspace "
        "may be missed and its publish skipped.",
    )
    return paths


def _github_get(url: str, token: str) -> list[dict[str, typing.Any]]:
    request = urllib.request.Request(  # noqa: S310  # suspicious-url-open-usage
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(  # noqa: S310  # suspicious-url-open-usage
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            body = json.loads(response.read())
            return typing.cast("list[dict[str, typing.Any]]", body)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        fail(
            f"Cannot read the pull request's changed files (HTTP {error.code}): "
            f"{detail}. The token needs 'pull-requests: read' permission.",
        )
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        fail(f"Cannot read the pull request's changed files: {error}")


class HoneydewClient:
    def __init__(self, *, base_url: str, authorization: str) -> None:
        if not base_url.startswith(("https://", "http://")):
            fail(f"Invalid base-url '{base_url}': must start with https:// or http://")
        self._endpoint = base_url.rstrip("/") + PUBLIC_API_PATH
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": authorization,
            "X-Honeydew-Client": "publish-action",
        }

    @classmethod
    def from_api_key(
        cls,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
    ) -> "HoneydewClient":
        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        return cls(base_url=base_url, authorization=f"Basic {token}")

    @classmethod
    def from_token(cls, *, base_url: str, token: str) -> "HoneydewClient":
        """Authenticate with a user bearer token — for local testing of this action."""
        return cls(base_url=base_url, authorization=f"Bearer {token}")

    def gql(
        self,
        query: str,
        *,
        variables: dict[str, ArgValue] | None = None,
        workspace: str | None = None,
        branch: str | None = None,
        retries: int = RETRIES,
    ) -> dict[str, typing.Any]:
        """Run a GraphQL document, raising ApiError on anything the API rejects.

        ``retries`` defaults to retrying transient failures, which is safe only for
        idempotent operations. Publishing passes 0 — see ``publish``.
        """
        payload = self._request(
            query,
            variables=variables,
            workspace=workspace,
            branch=branch,
            retries=retries,
        )
        if errors := payload.get("errors"):
            raise ApiError(_error_messages(errors))
        if (data := payload.get("data")) is None:
            raise ApiError(f"response has no data: {json.dumps(payload)[:500]}")
        return typing.cast("dict[str, typing.Any]", data)

    def _request(
        self,
        query: str,
        *,
        variables: dict[str, ArgValue] | None,
        workspace: str | None,
        branch: str | None,
        retries: int,
    ) -> dict[str, typing.Any]:
        headers = dict(self._headers)
        if workspace:
            headers["X-Honeydew-Workspace"] = workspace
        if branch:
            headers["X-Honeydew-Branch"] = branch
        document: dict[str, typing.Any] = {"query": query}
        if variables:
            document["variables"] = variables
        return self._post_with_retries(json.dumps(document).encode(), headers, retries)

    def _post_with_retries(
        self,
        body: bytes,
        headers: dict[str, str],
        retries: int,
    ) -> dict[str, typing.Any]:
        for attempt in range(retries + 1):
            request = urllib.request.Request(  # noqa: S310  # suspicious-url-open-usage
                self._endpoint,
                data=body,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(  # noqa: S310  # suspicious-url-open-usage
                    request,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                ) as response:
                    raw = response.read()
            except urllib.error.HTTPError as error:
                if error.code in RETRIED_HTTP_CODES and attempt < retries:
                    time.sleep(retry_delay(attempt, error.headers.get("Retry-After")))
                    continue
                detail = error.read().decode(errors="replace")[:500]
                if error.code == HTTPStatus.UNAUTHORIZED:
                    fail(
                        "Honeydew API authentication failed (HTTP 401). Check the "
                        "api-key and api-secret inputs, and make sure the public "
                        "GraphQL API is enabled for your organization.",
                    )
                raise ApiError(f"HTTP {error.code}: {detail}") from error
            except (TimeoutError, urllib.error.URLError) as error:
                if attempt < retries:
                    time.sleep(retry_delay(attempt, None))
                    continue
                reason = getattr(error, "reason", error)
                raise ApiError(f"cannot reach {self._endpoint}: {reason}") from error
            try:
                return typing.cast("dict[str, typing.Any]", json.loads(raw))
            except json.JSONDecodeError as error:
                raise ApiError(
                    f"non-JSON response: {raw.decode(errors='replace')[:500]}",
                ) from error
        raise AssertionError


def retry_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before the next attempt: Retry-After if usable, else backoff."""
    if (honored := _parse_retry_after(retry_after)) is not None:
        return honored
    # 2.0** rather than 2**: int**int is Any to mypy, since a negative exponent floats.
    backoff = BACKOFF_MULTIPLIER_SECONDS * 2.0**attempt
    return min(BACKOFF_MAX_SECONDS, max(BACKOFF_MIN_SECONDS, backoff))


def _parse_retry_after(retry_after: str | None) -> float | None:
    """Read Retry-After as delay-seconds, capped, ignoring the HTTP-date form.

    Trusting an HTTP-date would mean trusting the server's clock against the
    runner's, so that form falls back to exponential backoff instead.
    """
    if retry_after is None:
        return None
    try:
        seconds = float(retry_after.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _error_messages(errors: list[dict[str, typing.Any]]) -> str:
    return "; ".join(error.get("message", json.dumps(error)) for error in errors)


def build_mutation(destination: Destination, values: dict[str, ArgValue]) -> str:
    """Build a mutation document naming only the arguments actually supplied.

    Omitting an unset argument entirely, rather than passing null, keeps "not
    provided" distinct from "explicitly cleared" for the API — which is what
    Tableau's create-versus-update rules key off.
    """
    kinds = {
        argument.api_name: argument.kind
        for argument in destination.arguments
        if argument.api_name in values
    }
    declarations = ", ".join(f"${name}: {kind}!" for name, kind in kinds.items())
    call_arguments = ", ".join(f"{name}: ${name}" for name in kinds)
    result_fields = "\n    ".join(_result_fields(destination))
    return (
        f"mutation publish({declarations}) {{\n"
        f"  {destination.mutation}({call_arguments}) {{\n"
        f"    {result_fields}\n"
        f"  }}\n"
        f"}}"
    )


def _result_fields(destination: Destination) -> list[str]:
    fields = [destination.url_field]
    if destination.id_field:
        fields.append(destination.id_field)
    fields.extend(destination.warning_fields)
    return fields


class Status(enum.StrEnum):
    PUBLISHED = "published"
    SKIPPED = "skipped"


@dataclasses.dataclass(frozen=True)
class Target:
    """The one (workspace, domain, BI tool) combination this run publishes."""

    destination: Destination
    workspace: str
    branch: str
    domain: str

    @property
    def label(self) -> str:
        return f"{self.workspace}/{self.domain} \u2192 {self.destination.label}"


@dataclasses.dataclass(frozen=True)
class PublishResult:
    url: str
    object_id: str
    warnings: tuple[str, ...]


def publish(
    client: HoneydewClient,
    target: Target,
    values: dict[str, ArgValue],
) -> PublishResult:
    destination = target.destination
    mutation = build_mutation(destination, values)
    print(f"Publishing {target.label} (branch '{target.branch}')...")
    # No retries: creating a data source is not idempotent, so a retry after a
    # timeout could publish the same model twice.
    data = client.gql(
        mutation,
        variables=values,
        workspace=target.workspace,
        branch=target.branch,
        retries=0,
    )
    if (result := data.get(destination.mutation)) is None:
        fail(
            f"Publishing to {destination.label} returned no result. The destination "
            "may have rejected the request — check the Honeydew connector settings.",
        )
    return PublishResult(
        url=str(result.get(destination.url_field) or ""),
        object_id=(
            str(result.get(destination.id_field) or "") if destination.id_field else ""
        ),
        warnings=tuple(
            f"{field}: {result[field]}"
            for field in destination.warning_fields
            if result.get(field)
        ),
    )


def reload_workspace(client: HoneydewClient, *, workspace: str, branch: str) -> None:
    print(f"Reloading workspace '{workspace}' branch '{branch}' from git...")
    client.gql("mutation { reset_workspace }", workspace=workspace, branch=branch)


def write_outputs(status: Status, result: PublishResult | None) -> None:
    if not (output_path := os.environ.get("GITHUB_OUTPUT")):
        return
    # Newlines would be read as further output lines; the API-provided warning text
    # is the only value here that can contain them.
    warning = " ".join(result.warnings) if result else ""
    values = {
        "status": str(status),
        "url": result.url if result else "",
        "id": result.object_id if result else "",
        "warning": warning.replace("\n", " ").replace("\r", " "),
    }
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.writelines(f"{name}={value}\n" for name, value in values.items())


def write_step_summary(
    target: Target,
    status: Status,
    result: PublishResult | None,
) -> None:
    if not (summary_path := os.environ.get("GITHUB_STEP_SUMMARY")):
        return
    destination = target.destination
    if status is Status.SKIPPED:
        outcome = "⏭️ skipped — workspace unchanged"
    elif result and result.warnings:
        outcome = "⚠️ published with warnings"
    else:
        outcome = "✅ published"
    row = (
        f"| {target.workspace} | {target.branch} | {target.domain} "
        f"| {destination.label} | {outcome} |"
    )
    lines = [
        "## Honeydew publish",
        "",
        "| Workspace | Branch | Domain | Destination | Result |",
        "|---|---|---|---|---|",
        row,
        "",
    ]
    if result and result.url:
        lines.append(f"[Open in {destination.label}]({result.url})")
    if result and result.object_id and destination.id_field:
        reuse_hint = (
            f"`{destination.id_field}`: `{result.object_id}` — pass this back as the "
            "update id on the next run so it updates this object instead of creating "
            "another one."
        )
        lines.extend(["", reuse_hint])
    if result:
        lines.extend(f"- ⚠️ {warning}" for warning in result.warnings)
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def require_env(name: str) -> str:
    if not (value := os.environ.get(name, "").strip()):
        input_name = name.removeprefix("HONEYDEW_").lower().replace("_", "-")
        fail(f"Missing required input: {input_name}")
    return value


def is_enabled(name: str, *, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in {"false", "0", "no"}


def _build_client(base_url: str) -> HoneydewClient:
    if token := os.environ.get("HONEYDEW_TOKEN", "").strip():
        return HoneydewClient.from_token(base_url=base_url, token=token)
    return HoneydewClient.from_api_key(
        base_url=base_url,
        api_key=require_env("HONEYDEW_API_KEY"),
        api_secret=require_env("HONEYDEW_API_SECRET"),
    )


def _skip(target: Target) -> None:
    print_notice(f"Skipped {target.label}: workspace unchanged in this pull request.")
    write_outputs(Status.SKIPPED, None)
    write_step_summary(target, Status.SKIPPED, None)


def main() -> None:
    base_url = (
        os.environ.get("HONEYDEW_BASE_URL", "").strip() or "https://api.honeydew.cloud"
    )
    destination = resolve_destination(os.environ.get("HONEYDEW_TARGET", "").strip())
    workspace = require_env("HONEYDEW_WORKSPACE")
    branch = os.environ.get("HONEYDEW_BRANCH", "").strip() or MAIN_BRANCH
    values = collect_arguments(destination)
    target = Target(
        destination=destination,
        workspace=workspace,
        branch=branch,
        domain=str(values.get("domain") or ""),
    )

    if not workspace_changed(
        workspace,
        token=os.environ.get("HONEYDEW_GITHUB_TOKEN", "").strip(),
        repository=os.environ.get("GITHUB_REPOSITORY", "").strip(),
        event_path=os.environ.get("GITHUB_EVENT_PATH", ""),
    ):
        _skip(target)
        return

    client = _build_client(base_url)
    try:
        if is_enabled("HONEYDEW_RELOAD", default=True):
            reload_workspace(client, workspace=workspace, branch=branch)
        result = publish(client, target, values)
    except ApiError as error:
        fail(f"Publishing to {destination.label} failed: {error}")

    write_outputs(Status.PUBLISHED, result)
    write_step_summary(target, Status.PUBLISHED, result)
    for warning in result.warnings:
        print_warning(f"[{destination.label}] {warning}")
    print(f"Published to {destination.label}: {result.url or '(no link returned)'}")
    if result.object_id and destination.id_field:
        print(f"{destination.id_field}: {result.object_id}")
    if result.warnings and is_enabled("HONEYDEW_FAIL_ON_WARNING", default=False):
        fail(f"Publishing to {destination.label} reported warnings.")


if __name__ == "__main__":
    main()
