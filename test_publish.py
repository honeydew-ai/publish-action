# Copyright 2026 Honeydew Data Inc.
# SPDX-License-Identifier: Apache-2.0

import email.message
import io
import json
import os
import re
import typing
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

import publish

POWERBI = publish.DESTINATIONS[0]
SIGMA = publish.DESTINATIONS[1]
TABLEAU = publish.DESTINATIONS[2]
THOUGHTSPOT = publish.DESTINATIONS[3]


def test_destination_keys_are_unique() -> None:
    keys = [destination.key for destination in publish.DESTINATIONS]
    assert keys == ["powerbi", "sigma", "tableau", "thoughtspot"]


# The GitHub Marketplace rejects the listing outright at 125 characters or more.
MARKETPLACE_DESCRIPTION_LIMIT = 125


def test_action_description_fits_the_marketplace_limit() -> None:
    """A description at the limit blocks publishing, and only fails in the UI."""
    action = Path(__file__).with_name("action.yml").read_text(encoding="utf-8")
    block = re.search(r"^description: >-\n((?:  .+\n)+)", action, re.MULTILINE)
    assert block is not None, "action.yml has no folded top-level description"
    # ">-" folds the continuation lines into one space-joined line, which is the
    # form the Marketplace measures.
    description = " ".join(line.strip() for line in block.group(1).splitlines())
    assert len(description) < MARKETPLACE_DESCRIPTION_LIMIT


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param("powerbi", POWERBI, id="powerbi"),
        pytest.param("sigma", SIGMA, id="sigma"),
        pytest.param("tableau", TABLEAU, id="tableau"),
        pytest.param("thoughtspot", THOUGHTSPOT, id="thoughtspot"),
    ],
)
def test_resolve_destination(target: str, expected: publish.Destination) -> None:
    assert publish.resolve_destination(target) == expected


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("", id="empty"),
        pytest.param("looker", id="unsupported_tool"),
        pytest.param("PowerBI", id="wrong_case"),
    ],
)
def test_resolve_destination_fails(target: str) -> None:
    with pytest.raises(SystemExit):
        publish.resolve_destination(target)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("sales", "sales", id="plain"),
        pytest.param("  sales  ", "sales", id="stripped"),
    ],
)
def test_require_env(value: str, expected: str) -> None:
    with mock.patch.dict(os.environ, {"HONEYDEW_WORKSPACE": value}, clear=True):
        assert publish.require_env("HONEYDEW_WORKSPACE") == expected


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="unset"),
        pytest.param({"HONEYDEW_WORKSPACE": ""}, id="empty"),
        pytest.param({"HONEYDEW_WORKSPACE": "   "}, id="whitespace_only"),
    ],
)
def test_require_env_fails(env: dict[str, str]) -> None:
    with mock.patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit):
        publish.require_env("HONEYDEW_WORKSPACE")


def test_the_git_ref_is_never_read() -> None:
    """What a workflow publishes is its own "paths:" filter's business, not ours.

    Inferring the workspace from the branch made the published target depend on
    which branch triggered the run, which is wrong in a repository holding many
    workspaces.
    """
    source = Path(__file__).with_name("publish.py").read_text(encoding="utf-8")
    assert "GITHUB_HEAD_REF" not in source
    assert "GITHUB_REF_NAME" not in source


def _collect(destination: publish.Destination, **env: str) -> dict[str, typing.Any]:
    with mock.patch.dict(os.environ, env, clear=True):
        return publish.collect_arguments(destination)


@pytest.mark.parametrize(
    ("destination", "env", "expected"),
    [
        pytest.param(
            POWERBI,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_DOMAIN": "  sales  ",
                "HONEYDEW_POWERBI_MODEL_NAME": "Sales Exec",
                "HONEYDEW_POWERBI_GROUP_ID": "3f2a",
            },
            {
                "connector_name": "default",
                "domain": "sales",
                "model_name": "Sales Exec",
                "group_id": "3f2a",
            },
            id="values_are_stripped",
        ),
        pytest.param(
            SIGMA,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_DOMAIN": "sales",
                "HONEYDEW_SIGMA_CONNECTION_ID": "abc",
                "HONEYDEW_SIGMA_FOLDER_ID": "def",
                "HONEYDEW_SIGMA_TAGS": "v1, prod ,,",
            },
            {
                "connector_name": "default",
                "domain": "sales",
                "connection_id": "abc",
                "folder_id": "def",
                "tags": ["v1", "prod"],
            },
            id="sigma_tags_are_split",
        ),
        pytest.param(
            TABLEAU,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_DOMAIN": "sales",
                "HONEYDEW_TABLEAU_EXISTING_DATASOURCE_ID": "ds-1",
            },
            {
                "connector_name": "default",
                "domain": "sales",
                "existing_datasource_id": "ds-1",
            },
            id="tableau_update_by_id",
        ),
        pytest.param(
            TABLEAU,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_DOMAIN": "sales",
                "HONEYDEW_TABLEAU_DATASOURCE_NAME": "Sales",
                "HONEYDEW_TABLEAU_PROJECT_ID": "p-1",
            },
            {
                "connector_name": "default",
                "domain": "sales",
                "datasource_name": "Sales",
                "project_id": "p-1",
            },
            id="tableau_create_by_name",
        ),
        pytest.param(
            TABLEAU,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_DOMAIN": "sales",
                "HONEYDEW_TABLEAU_DATASOURCE_NAME": "Sales",
                "HONEYDEW_TABLEAU_PROJECT_ID": "p-1",
                "HONEYDEW_TABLEAU_AUTHENTICATION": "oauth",
            },
            {
                "connector_name": "default",
                "domain": "sales",
                "datasource_name": "Sales",
                "project_id": "p-1",
                "authentication": "OAUTH",
            },
            id="tableau_authentication_takes_the_api_spelling",
        ),
        pytest.param(
            THOUGHTSPOT,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_DOMAIN": "sales",
                "HONEYDEW_THOUGHTSPOT_CONNECTION_NAME": "hd",
            },
            {
                "connector_name": "default",
                "domain": "sales",
                "connection_name": "hd",
            },
            id="thoughtspot",
        ),
    ],
)
def test_collect_arguments(
    destination: publish.Destination,
    env: dict[str, str],
    expected: dict[str, typing.Any],
) -> None:
    assert _collect(destination, **env) == expected


@pytest.mark.parametrize(
    ("destination", "env"),
    [
        pytest.param(POWERBI, {}, id="powerbi_missing_everything"),
        pytest.param(
            POWERBI,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_POWERBI_MODEL_NAME": "Sales",
            },
            id="powerbi_missing_group_id",
        ),
        pytest.param(
            SIGMA,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_SIGMA_FOLDER_ID": "def",
            },
            id="sigma_missing_connection_id",
        ),
        pytest.param(
            THOUGHTSPOT,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_THOUGHTSPOT_CONNECTION_NAME": "hd",
            },
            id="thoughtspot_missing_required_domain",
        ),
        pytest.param(
            TABLEAU,
            {"HONEYDEW_CONNECTOR_NAME": "default"},
            id="tableau_neither_id_nor_name",
        ),
        pytest.param(
            TABLEAU,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_TABLEAU_DATASOURCE_NAME": "Sales",
            },
            id="tableau_name_without_project",
        ),
        pytest.param(
            TABLEAU,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_TABLEAU_EXISTING_DATASOURCE_ID": "ds-1",
                "HONEYDEW_TABLEAU_PROJECT_ID": "p-1",
            },
            id="tableau_id_and_project",
        ),
        pytest.param(
            TABLEAU,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_DOMAIN": "sales",
                "HONEYDEW_TABLEAU_DATASOURCE_NAME": "Sales",
                "HONEYDEW_TABLEAU_PROJECT_ID": "p-1",
                "HONEYDEW_TABLEAU_AUTHENTICATION": "sso",
            },
            id="tableau_unknown_authentication",
        ),
        pytest.param(
            TABLEAU,
            {
                "HONEYDEW_CONNECTOR_NAME": "default",
                "HONEYDEW_DOMAIN": "sales",
                "HONEYDEW_TABLEAU_EXISTING_DATASOURCE_ID": "ds-1",
                "HONEYDEW_TABLEAU_AUTHENTICATION": "OAUTH",
            },
            id="tableau_authentication_on_an_update",
        ),
    ],
)
def test_collect_arguments_fails(
    destination: publish.Destination,
    env: dict[str, str],
) -> None:
    with pytest.raises(SystemExit):
        _collect(destination, **env)


@pytest.mark.parametrize(
    "destination",
    [pytest.param(d, id=d.key) for d in publish.DESTINATIONS],
)
def test_domain_is_required_for_every_destination(
    destination: publish.Destination,
) -> None:
    required = {a.input_name for a in destination.arguments if a.required}
    assert "domain" in required


POWERBI_MUTATION = """mutation publish($connector_name: String!, $domain: String!, \
$model_name: String!, $group_id: String!) {
  sync_powerbi_datasource(connector_name: $connector_name, domain: $domain, \
model_name: $model_name, group_id: $group_id) {
    semantic_model_url
    refresh_error
  }
}"""

SIGMA_MUTATION = """mutation publish($connector_name: String!, $connection_id: String!, \
$folder_id: String!, $tags: [String!]!) {
  sync_sigma_datasource(connector_name: $connector_name, connection_id: $connection_id, \
folder_id: $folder_id, tags: $tags) {
    data_model_url
    data_model_id
    tag_error
  }
}"""

TABLEAU_MUTATION = """mutation publish($connector_name: String!, \
$existing_datasource_id: String!) {
  sync_tableau_datasource(connector_name: $connector_name, \
existing_datasource_id: $existing_datasource_id) {
    datasource_url
  }
}"""

TABLEAU_OAUTH_MUTATION = """mutation publish($connector_name: String!, \
$domain: String!, $datasource_name: String!, $project_id: String!, \
$authentication: TableauAuthentication!) {
  sync_tableau_datasource(connector_name: $connector_name, domain: $domain, \
datasource_name: $datasource_name, project_id: $project_id, \
authentication: $authentication) {
    datasource_url
  }
}"""


@pytest.mark.parametrize(
    ("destination", "values", "expected"),
    [
        pytest.param(
            POWERBI,
            {
                "connector_name": "default",
                "domain": "sales",
                "model_name": "Sales Exec",
                "group_id": "3f2a",
            },
            POWERBI_MUTATION,
            id="powerbi",
        ),
        pytest.param(
            SIGMA,
            {
                "connector_name": "default",
                "connection_id": "abc",
                "folder_id": "def",
                "tags": ["v1"],
            },
            SIGMA_MUTATION,
            id="sigma_omits_unset_arguments_and_types_the_list",
        ),
        pytest.param(
            TABLEAU,
            {"connector_name": "default", "existing_datasource_id": "ds-1"},
            TABLEAU_MUTATION,
            id="tableau_single_result_field",
        ),
        pytest.param(
            TABLEAU,
            {
                "connector_name": "default",
                "domain": "sales",
                "datasource_name": "Sales",
                "project_id": "p-1",
                "authentication": "OAUTH",
            },
            TABLEAU_OAUTH_MUTATION,
            id="tableau_declares_the_authentication_enum_type",
        ),
    ],
)
def test_build_mutation(
    destination: publish.Destination,
    values: dict[str, typing.Any],
    expected: str,
) -> None:
    assert publish.build_mutation(destination, values) == expected


@pytest.mark.parametrize(
    ("destination", "response", "expected"),
    [
        pytest.param(
            POWERBI,
            {"semantic_model_url": "https://powerbi/1", "refresh_error": None},
            publish.PublishResult(
                url="https://powerbi/1",
                object_id="",
                warnings=(),
            ),
            id="powerbi_clean",
        ),
        pytest.param(
            POWERBI,
            {"semantic_model_url": "https://powerbi/1", "refresh_error": "no capacity"},
            publish.PublishResult(
                url="https://powerbi/1",
                object_id="",
                warnings=("refresh_error: no capacity",),
            ),
            id="powerbi_refresh_failed_after_publish",
        ),
        pytest.param(
            SIGMA,
            {
                "data_model_url": "https://sigma/1",
                "data_model_id": "dm-1",
                "tag_error": "unknown tag",
            },
            publish.PublishResult(
                url="https://sigma/1",
                object_id="dm-1",
                warnings=("tag_error: unknown tag",),
            ),
            id="sigma_reports_created_id",
        ),
        pytest.param(
            TABLEAU,
            {"datasource_url": "https://tableau/1"},
            publish.PublishResult(
                url="https://tableau/1",
                object_id="",
                warnings=(),
            ),
            id="tableau",
        ),
    ],
)
def test_publish_maps_the_response(
    destination: publish.Destination,
    response: dict[str, typing.Any],
    expected: publish.PublishResult,
) -> None:
    client = mock.Mock()
    client.gql.return_value = {destination.mutation: response}
    result = publish.publish(
        client,
        destination,
        workspace="sales",
        branch="prod",
        values={"connector_name": "default"},
    )
    assert result == expected


def test_publish_does_not_retry() -> None:
    """A retried create would publish the same model twice."""
    client = mock.Mock()
    client.gql.return_value = {TABLEAU.mutation: {"datasource_url": "https://x"}}
    publish.publish(
        client,
        TABLEAU,
        workspace="sales",
        branch="prod",
        values={"connector_name": "default"},
    )
    assert client.gql.call_args.kwargs["retries"] == 0


def test_publish_fails_on_null_result() -> None:
    client = mock.Mock()
    client.gql.return_value = {TABLEAU.mutation: None}
    with pytest.raises(SystemExit):
        publish.publish(
            client,
            TABLEAU,
            workspace="sales",
            branch="prod",
            values={"connector_name": "default"},
        )


def test_write_outputs(tmp_path: Path) -> None:
    output = tmp_path / "output"
    result = publish.PublishResult(
        url="https://sigma/1",
        object_id="dm-1",
        warnings=("tag_error: line one\nline two",),
    )
    with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}, clear=True):
        publish.write_outputs(result)
    assert output.read_text(encoding="utf-8") == (
        "url=https://sigma/1\nid=dm-1\nwarning=tag_error: line one line two\n"
    )


def test_write_step_summary_reports_the_id_to_reuse(tmp_path: Path) -> None:
    summary = tmp_path / "summary"
    result = publish.PublishResult(
        url="https://sigma/1",
        object_id="dm-1",
        warnings=(),
    )
    with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}, clear=True):
        publish.write_step_summary(
            SIGMA,
            result,
            workspace="sales",
            branch="prod",
            domain="sales_exec",
        )
    contents = summary.read_text(encoding="utf-8")
    assert contents == (
        "## Honeydew publish\n"
        "\n"
        "| Destination | Workspace | Branch | Domain | Result |\n"
        "|---|---|---|---|---|\n"
        "| Sigma | sales | prod | sales_exec | ✅ published |\n"
        "\n"
        "[Open in Sigma](https://sigma/1)\n"
        "\n"
        "`data_model_id`: `dm-1` — pass this back as the update id on the next run "
        "so it updates this object instead of creating another one.\n"
    )


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        pytest.param("", True, True, id="unset_keeps_default_true"),
        pytest.param("", False, False, id="unset_keeps_default_false"),
        pytest.param("false", True, False, id="false"),
        pytest.param("FALSE", True, False, id="false_uppercase"),
        pytest.param("0", True, False, id="zero"),
        pytest.param("no", True, False, id="no"),
        pytest.param("true", False, True, id="true"),
    ],
)
def test_is_enabled(value: str, default: bool, expected: bool) -> None:
    with mock.patch.dict(os.environ, {"HONEYDEW_FLAG": value}, clear=True):
        assert publish.is_enabled("HONEYDEW_FLAG", default=default) is expected


@pytest.mark.parametrize(
    ("attempt", "retry_after", "expected"),
    [
        pytest.param(0, None, 1.0, id="first_wait_is_the_minimum"),
        pytest.param(1, None, 2.0, id="doubles"),
        pytest.param(2, None, 4.0, id="doubles_again"),
        pytest.param(4, None, 16.0, id="still_below_the_cap"),
        pytest.param(10, None, 30.0, id="capped_at_max"),
        pytest.param(0, "7", 7.0, id="retry_after_wins"),
        pytest.param(0, " 7 ", 7.0, id="retry_after_is_stripped"),
        pytest.param(0, "900", 60.0, id="retry_after_is_capped"),
        pytest.param(3, "-1", 8.0, id="negative_retry_after_ignored"),
        pytest.param(3, "NaN", 8.0, id="nan_falls_back_to_backoff"),
        pytest.param(3, "nan", 8.0, id="lowercase_nan_falls_back_to_backoff"),
        pytest.param(3, "inf", 8.0, id="infinity_ignored"),
        pytest.param(3, "-inf", 8.0, id="negative_infinity_ignored"),
        pytest.param(
            3,
            "Wed, 21 Oct 2026 07:28:00 GMT",
            8.0,
            id="http_date_form_falls_back_to_backoff",
        ),
    ],
)
def test_retry_delay(attempt: int, retry_after: str | None, expected: float) -> None:
    assert publish.retry_delay(attempt, retry_after) == expected


def test_backoff_schedule_outlasts_a_brief_api_restart() -> None:
    """The five retries wait 1+2+4+8+16 = 31s in total, on top of the requests."""
    waits = [publish.retry_delay(attempt, None) for attempt in range(publish.RETRIES)]
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0]


ENDPOINT = "https://api.example.com/api/public/v1/graphql"


def _client() -> publish.HoneydewClient:
    return publish.HoneydewClient.from_api_key(
        base_url="https://api.example.com",
        api_key="key",
        api_secret="secret",
    )


def _response(body: bytes) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=ENDPOINT,
        code=code,
        msg="",
        hdrs=email.message.Message(),
        fp=io.BytesIO(b"server detail"),
    )


def test_gql_sends_variables_and_context_headers() -> None:
    with mock.patch(
        "urllib.request.urlopen",
        return_value=_response(b'{"data": {"ok": true}}'),
    ) as urlopen:
        assert _client().gql(
            "mutation publish($a: String!) {}",
            variables={"a": "b"},
            workspace="sales",
            branch="prod",
        ) == {"ok": True}
    request = urlopen.call_args.args[0]
    assert request.full_url == ENDPOINT
    assert json.loads(request.data) == {
        "query": "mutation publish($a: String!) {}",
        "variables": {"a": "b"},
    }
    assert request.get_header("Authorization") == "Basic a2V5OnNlY3JldA=="
    assert request.get_header("X-honeydew-workspace") == "sales"
    assert request.get_header("X-honeydew-branch") == "prod"
    assert request.get_header("X-honeydew-client") == "publish-action"


def test_gql_omits_variables_when_empty() -> None:
    with mock.patch(
        "urllib.request.urlopen",
        return_value=_response(b'{"data": {"ok": true}}'),
    ) as urlopen:
        _client().gql("mutation { reset_workspace }")
    assert json.loads(urlopen.call_args.args[0].data) == {
        "query": "mutation { reset_workspace }",
    }


def test_gql_fails_on_graphql_errors() -> None:
    with mock.patch(
        "urllib.request.urlopen",
        return_value=_response(b'{"errors": [{"message": "no such domain"}]}'),
    ), pytest.raises(SystemExit):
        _client().gql("query {}")


@pytest.mark.parametrize(
    ("retries", "expected_calls"),
    [
        pytest.param(0, 1, id="publish_does_not_retry"),
        pytest.param(3, 4, id="idempotent_calls_retry"),
    ],
)
def test_retry_count_is_per_request(retries: int, expected_calls: int) -> None:
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=_http_error(503),
    ) as urlopen, mock.patch("time.sleep"), pytest.raises(SystemExit):
        _client().gql("query {}", retries=retries)
    assert urlopen.call_count == expected_calls


def test_retry_after_header_drives_the_sleep() -> None:
    headers = email.message.Message()
    headers["Retry-After"] = "12"
    rate_limited = urllib.error.HTTPError(
        url=ENDPOINT,
        code=429,
        msg="",
        hdrs=headers,
        fp=io.BytesIO(b"slow down"),
    )
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=[rate_limited, _response(b'{"data": {"ok": true}}')],
    ), mock.patch("time.sleep") as sleep:
        assert _client().gql("query {}") == {"ok": True}
    assert sleep.call_args_list == [mock.call(12.0)]


def test_unauthorized_is_not_retried() -> None:
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=_http_error(401),
    ) as urlopen, pytest.raises(SystemExit):
        _client().gql("query {}")
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    "base_url",
    [
        pytest.param("api.honeydew.cloud", id="no_scheme"),
        pytest.param("ftp://api.honeydew.cloud", id="wrong_scheme"),
    ],
)
def test_invalid_base_url_fails(base_url: str) -> None:
    with pytest.raises(SystemExit):
        publish.HoneydewClient.from_api_key(
            base_url=base_url,
            api_key="key",
            api_secret="secret",
        )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param("plain", "plain", id="plain"),
        pytest.param("a\nb", "a%0Ab", id="newline"),
        pytest.param("100%", "100%25", id="percent"),
        pytest.param("a\r\n::add-mask::x", "a%0D%0A::add-mask::x", id="forged_command"),
    ],
)
def test_escape_workflow_command(message: str, expected: str) -> None:
    assert publish.escape_workflow_command(message) == expected
