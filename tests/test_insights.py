"""Recorded-shape tests for the read-only PropertyMeld Insights client."""

import ast
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as parquet
import pytest
from click.testing import CliRunner

from cli_anything.propertymeld import api_backend, insights_backend
from cli_anything.propertymeld.cli import cli


def _parquet_bytes(rows):
    sink = pa.BufferOutputStream()
    parquet.write_table(pa.Table.from_pylist(rows), sink)
    return sink.getvalue().to_pybytes()


def _response(payload):
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.geturl.return_value = (
        "https://app.propertymeld.com/1000/m/1000/api/analytics/parquet/"
        "raw_meld_data.parquet"
    )
    response.read.return_value = payload
    return response


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    path = tmp_path / "property-meld.json"
    path.write_text(json.dumps({
        "cookies": [{
            "name": "sessionid",
            "value": "recorded-test-session",
            "domain": ".propertymeld.com",
        }]
    }))
    config = tmp_path / "propertymeld-config.json"
    config.write_text(json.dumps({
        "multitenant_id": "1000",
        "nexus_account_id": "2000",
        "credentials_path": str(path),
    }))
    monkeypatch.setenv("PROPERTYMELD_CONFIG", str(config))
    from cli_anything.propertymeld.config import propertymeld_config
    propertymeld_config.cache_clear()
    return path


RECORDED_MELDS = [
    {
        "meld_meld_id": 101,
        "meld_meld_created": "2026-08-01T10:00:00Z",
        "meld_meld_status": "COMPLETED",
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": 7001,
        "vendor_assigned_name": "  DBH   Construction ",
        "inhouse_servicer_name": None,
        "meld_meld_coordinator_id": 57163,
        "meld_meld_coordinator_name": "Coordinator",
        "meld_meld_assigned_to_accepted_for_vendor": 30.0,
        "meld_meld_accepted_to_scheduled_for_vendor": 60.0,
        "meld_meld_assigned_to_scheduled": 90.0,
        "meld_meld_assigned_to_completed": 180.0,
        "meld_meld_tenant_rating": 5,
        "invoice_amount": 250.0,
        "expenditure_amount": None,
        "total_worklog_hours": 2.5,
        "vendor_chat_response_seconds": 45,
        "meld_meld_brief_description": "resident text must never leave projection",
        "workentry_log": "private notes must never leave projection",
    },
    {
        "meld_meld_id": 102,
        "meld_meld_created": "2026-08-02T10:00:00Z",
        "meld_meld_status": "COMPLETED",
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": None,
        "vendor_assigned_name": "Unknown Vendor",
        "inhouse_servicer_name": None,
        "meld_meld_coordinator_id": 57163,
        "meld_meld_coordinator_name": "Coordinator",
        "meld_meld_assigned_to_accepted_for_vendor": None,
        "meld_meld_accepted_to_scheduled_for_vendor": None,
        "meld_meld_assigned_to_scheduled": None,
        "meld_meld_assigned_to_completed": 240.0,
        "meld_meld_tenant_rating": None,
        "invoice_amount": None,
        "expenditure_amount": None,
        "total_worklog_hours": None,
        "vendor_chat_response_seconds": None,
        "meld_meld_brief_description": "another private description",
        "workentry_log": None,
    },
    {
        "meld_meld_id": 103,
        "meld_meld_created": "2026-08-03T10:00:00Z",
        "meld_meld_status": "PENDING_ASSIGNMENT",
        "meld_meld_work_category": "PLUMBING",
        "meld_meld_project_id": None,
        "vendor_assigned_name": None,
        "inhouse_servicer_name": "Technician",
        "meld_meld_coordinator_id": 57163,
        "meld_meld_coordinator_name": "Coordinator",
        "meld_meld_assigned_to_accepted_for_vendor": None,
        "meld_meld_accepted_to_scheduled_for_vendor": None,
        "meld_meld_assigned_to_scheduled": None,
        "meld_meld_assigned_to_completed": None,
        "meld_meld_tenant_rating": None,
        "invoice_amount": None,
        "expenditure_amount": 75.0,
        "total_worklog_hours": 1.0,
        "vendor_chat_response_seconds": None,
        "meld_meld_brief_description": "private",
        "workentry_log": "private",
    },
]

RECORDED_BENCHMARKS = [{
    "unit_count": "251-500",
    "priority": "NORMAL",
    "work_category": "TURNOVER",
    "region": "SOUTHEAST",
    "is_project": True,
    "completed_month": "2026-07-01T00:00:00Z",
    "sor_5th": 1.0,
    "sor_25th": 2.0,
    "sor_50th": 3.0,
    "sor_75th": 4.0,
    "sor_95th": 5.0,
    "res_sat_5th": 1.0,
    "res_sat_25th": 2.0,
    "res_sat_50th": 3.0,
    "res_sat_75th": 4.0,
    "res_sat_95th": 5.0,
    "invoice_spend_5th": 10.0,
    "invoice_spend_25th": 20.0,
    "invoice_spend_50th": 30.0,
    "invoice_spend_75th": 40.0,
    "invoice_spend_95th": 50.0,
    "expenditure_spend_5th": 11.0,
    "expenditure_spend_25th": 21.0,
    "expenditure_spend_50th": 31.0,
    "expenditure_spend_75th": 41.0,
    "expenditure_spend_95th": 51.0,
}]

RECORDED_NAN_MELDS = [
    {
        "meld_meld_id": 201,
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": 7001.0,
        "vendor_assigned_name": None,
    },
    {
        "meld_meld_id": 202,
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": float("nan"),
        "vendor_assigned_name": None,
    },
]

RECORDED_NAN_BENCHMARKS = [
    dict(RECORDED_BENCHMARKS[0], is_project=1.0, priority="NORMAL"),
    dict(RECORDED_BENCHMARKS[0], is_project=float("nan"), priority="HIGH"),
]


RECORDED_CLI_MELDS = [
    {
        "meld_meld_id": 301,
        "meld_meld_created": "2026-08-04T10:00:00Z",
        "meld_meld_status": "COMPLETED",
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": 8001.0,
        "vendor_assigned_name": "DBH Construction",
        "inhouse_servicer_name": "Primary Technician",
        "meld_meld_coordinator_id": 57163,
        "meld_meld_coordinator_name": "Coordinator One",
        "meld_meld_assigned_to_accepted_for_vendor": 31.0,
        "meld_meld_accepted_to_scheduled_for_vendor": 61.0,
        "meld_meld_assigned_to_scheduled": 91.0,
        "meld_meld_assigned_to_completed": 181.0,
        "meld_meld_tenant_rating": 5,
        "invoice_amount": 251.0,
        "expenditure_amount": 76.0,
        "total_worklog_hours": 2.5,
        "vendor_chat_response_seconds": 46,
    },
    {
        "meld_meld_id": 302,
        "meld_meld_created": "2026-08-05T10:00:00Z",
        "meld_meld_status": "COMPLETED",
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": float("nan"),
        "vendor_assigned_name": "Unknown Vendor",
        "inhouse_servicer_name": "Secondary Technician",
        "meld_meld_coordinator_id": 57164,
        "meld_meld_coordinator_name": "Coordinator Two",
        "meld_meld_assigned_to_accepted_for_vendor": 32.0,
        "meld_meld_accepted_to_scheduled_for_vendor": 62.0,
        "meld_meld_assigned_to_scheduled": 92.0,
        "meld_meld_assigned_to_completed": 182.0,
        "meld_meld_tenant_rating": 4,
        "invoice_amount": 252.0,
        "expenditure_amount": 77.0,
        "total_worklog_hours": 3.5,
        "vendor_chat_response_seconds": 47,
    },
    {
        "meld_meld_id": 303,
        "meld_meld_created": "2026-08-06T10:00:00Z",
        "meld_meld_status": "PENDING_ASSIGNMENT",
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": 8003.0,
        "vendor_assigned_name": "Same Vendor",
        "inhouse_servicer_name": "Third Technician",
        "meld_meld_coordinator_id": 57165,
        "meld_meld_coordinator_name": "Coordinator Three",
        "meld_meld_assigned_to_accepted_for_vendor": 33.0,
        "meld_meld_accepted_to_scheduled_for_vendor": 63.0,
        "meld_meld_assigned_to_scheduled": 93.0,
        "meld_meld_assigned_to_completed": 183.0,
        "meld_meld_tenant_rating": 3,
        "invoice_amount": 253.0,
        "expenditure_amount": 78.0,
        "total_worklog_hours": 4.5,
        "vendor_chat_response_seconds": 48,
    },
    {
        "meld_meld_id": 304,
        "meld_meld_created": "2026-08-07T10:00:00Z",
        "meld_meld_status": "IN_PROGRESS",
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": 8004.0,
        "vendor_assigned_name": None,
        "inhouse_servicer_name": "Fourth Technician",
        "meld_meld_coordinator_id": 57166,
        "meld_meld_coordinator_name": "Coordinator Four",
        "meld_meld_assigned_to_accepted_for_vendor": 34.0,
        "meld_meld_accepted_to_scheduled_for_vendor": 64.0,
        "meld_meld_assigned_to_scheduled": 94.0,
        "meld_meld_assigned_to_completed": 184.0,
        "meld_meld_tenant_rating": 2,
        "invoice_amount": 254.0,
        "expenditure_amount": 79.0,
        "total_worklog_hours": 5.5,
        "vendor_chat_response_seconds": 49,
    },
]

RECORDED_CLI_BENCHMARKS = [
    dict(RECORDED_BENCHMARKS[0], is_project=1.0),
    {
        **RECORDED_BENCHMARKS[0],
        "unit_count": "501-1000",
        "priority": "HIGH",
        "work_category": "PLUMBING",
        "region": "MIDWEST",
        "is_project": float("nan"),
        "completed_month": "2026-08-01T00:00:00Z",
        "sor_5th": 6.0,
        "sor_25th": 7.0,
        "sor_50th": 8.0,
        "sor_75th": 9.0,
        "sor_95th": 10.0,
        "res_sat_5th": 6.0,
        "res_sat_25th": 7.0,
        "res_sat_50th": 8.0,
        "res_sat_75th": 9.0,
        "res_sat_95th": 10.0,
        "invoice_spend_5th": 60.0,
        "invoice_spend_25th": 70.0,
        "invoice_spend_50th": 80.0,
        "invoice_spend_75th": 90.0,
        "invoice_spend_95th": 100.0,
        "expenditure_spend_5th": 61.0,
        "expenditure_spend_25th": 71.0,
        "expenditure_spend_50th": 81.0,
        "expenditure_spend_75th": 91.0,
        "expenditure_spend_95th": 101.0,
    },
]


# Fleet lesson 007: a schema criterion over three commands is checked by
# enumerating every consumer-readable member for each command by name.
CLI_OUTPUT_SCHEMA = {
    "melds": {
        "top_level": (
            "dataset", "source", "columns", "matched_count", "returned_count",
            "project_missing_count", "vendor_resolution", "rows",
        ),
        "row": (
            "meld_meld_id", "meld_meld_created", "meld_meld_status",
            "meld_meld_work_category", "meld_meld_project_id",
            "vendor_assigned_name", "inhouse_servicer_name",
            "meld_meld_coordinator_id", "meld_meld_coordinator_name",
            "meld_meld_assigned_to_accepted_for_vendor",
            "meld_meld_accepted_to_scheduled_for_vendor",
            "meld_meld_assigned_to_scheduled", "meld_meld_assigned_to_completed",
            "meld_meld_tenant_rating", "invoice_amount", "expenditure_amount",
            "total_worklog_hours", "vendor_chat_response_seconds",
            "vendor_resolution",
        ),
        "vendor_summary": ("counts", "unresolved_names", "ambiguous_names"),
        "vendor_counts": (
            "resolved", "unresolved", "ambiguous", "not_applicable",
        ),
        "row_vendor_resolution": (
            "status", "source_name", "vendor_id", "vendor_name", "matches",
        ),
        "vendor_match": ("vendor_id", "vendor_name"),
    },
    "turnovers": {
        "top_level": (
            "dataset", "source", "columns", "matched_count", "returned_count",
            "project_missing_count", "vendor_resolution", "rows",
        ),
        "row": (
            "meld_meld_id", "meld_meld_created", "meld_meld_status",
            "meld_meld_work_category", "meld_meld_project_id",
            "vendor_assigned_name", "inhouse_servicer_name",
            "meld_meld_coordinator_id", "meld_meld_coordinator_name",
            "meld_meld_assigned_to_accepted_for_vendor",
            "meld_meld_accepted_to_scheduled_for_vendor",
            "meld_meld_assigned_to_scheduled", "meld_meld_assigned_to_completed",
            "meld_meld_tenant_rating", "invoice_amount", "expenditure_amount",
            "total_worklog_hours", "vendor_chat_response_seconds",
            "vendor_resolution",
        ),
        "vendor_summary": ("counts", "unresolved_names", "ambiguous_names"),
        "vendor_counts": (
            "resolved", "unresolved", "ambiguous", "not_applicable",
        ),
        "row_vendor_resolution": (
            "status", "source_name", "vendor_id", "vendor_name", "matches",
        ),
        "vendor_match": ("vendor_id", "vendor_name"),
    },
    "benchmarks": {
        "top_level": (
            "dataset", "source", "columns", "matched_count", "returned_count",
            "project_missing_count", "rows",
        ),
        "row": (
            "unit_count", "priority", "work_category", "region", "is_project",
            "completed_month", "sor_5th", "sor_25th", "sor_50th", "sor_75th",
            "sor_95th", "res_sat_5th", "res_sat_25th", "res_sat_50th",
            "res_sat_75th", "res_sat_95th", "invoice_spend_5th",
            "invoice_spend_25th", "invoice_spend_50th", "invoice_spend_75th",
            "invoice_spend_95th", "expenditure_spend_5th",
            "expenditure_spend_25th", "expenditure_spend_50th",
            "expenditure_spend_75th", "expenditure_spend_95th",
        ),
    },
}


CLI_VENDOR_ROSTER = [
    {"id": 91159, "name": "DBH Construction"},
    {"id": 92001, "name": "Same Vendor"},
    {"id": 92002, "name": " same   vendor "},
]

RECORDED_MINIMAL_MELDS = [{
    "meld_meld_id": 401,
    "meld_meld_work_category": "TURNOVER",
    "vendor_assigned_name": "DBH Construction",
}]

RECORDED_MINIMAL_BENCHMARKS = [{
    "unit_count": "251-500",
    "priority": "NORMAL",
    "work_category": "TURNOVER",
    "region": "SOUTHEAST",
    "is_project": True,
    "completed_month": "2026-08-01T00:00:00Z",
}]

REQUIRED_CLI_INPUT_ROSTER = (
    ("melds", "meld_meld_id"),
    ("melds", "meld_meld_work_category"),
    ("melds", "vendor_assigned_name"),
    ("turnovers", "meld_meld_id"),
    ("turnovers", "meld_meld_work_category"),
    ("turnovers", "vendor_assigned_name"),
    ("benchmarks", "unit_count"),
    ("benchmarks", "priority"),
    ("benchmarks", "work_category"),
    ("benchmarks", "region"),
    ("benchmarks", "is_project"),
    ("benchmarks", "completed_month"),
)


def _expected_cli_meld_rows():
    return [
        {
            **RECORDED_CLI_MELDS[0],
            "vendor_resolution": {
                "status": "resolved",
                "source_name": "DBH Construction",
                "vendor_id": 91159,
                "vendor_name": "DBH Construction",
            },
        },
        {
            **RECORDED_CLI_MELDS[1],
            "meld_meld_project_id": None,
            "vendor_resolution": {
                "status": "unresolved",
                "source_name": "Unknown Vendor",
                "vendor_id": None,
                "vendor_name": None,
            },
        },
        {
            **RECORDED_CLI_MELDS[2],
            "vendor_resolution": {
                "status": "ambiguous",
                "source_name": "Same Vendor",
                "vendor_id": None,
                "vendor_name": None,
                "matches": [
                    {"vendor_id": 92001, "vendor_name": "Same Vendor"},
                    {"vendor_id": 92002, "vendor_name": "same   vendor"},
                ],
            },
        },
        {
            **RECORDED_CLI_MELDS[3],
            "vendor_resolution": {
                "status": "not_applicable",
                "source_name": None,
                "vendor_id": None,
                "vendor_name": None,
            },
        },
    ]


def _expected_cli_payload(command):
    if command in {"melds", "turnovers"}:
        return {
            "dataset": command,
            "source": "analytics/parquet/raw_meld_data.parquet",
            "columns": list(CLI_OUTPUT_SCHEMA[command]["row"]),
            "matched_count": 4,
            "returned_count": 4,
            "project_missing_count": 1,
            "vendor_resolution": {
                "counts": {
                    "ambiguous": 1,
                    "not_applicable": 1,
                    "resolved": 1,
                    "unresolved": 1,
                },
                "unresolved_names": ["Unknown Vendor"],
                "ambiguous_names": ["Same Vendor"],
            },
            "rows": _expected_cli_meld_rows(),
        }
    return {
        "dataset": "benchmarks",
        "source": "analytics/parquet/benchmarks.parquet",
        "columns": list(CLI_OUTPUT_SCHEMA["benchmarks"]["row"]),
        "matched_count": 2,
        "returned_count": 2,
        "project_missing_count": 1,
        "rows": [
            dict(RECORDED_CLI_BENCHMARKS[0]),
            {**RECORDED_CLI_BENCHMARKS[1], "is_project": None},
        ],
    }


def _assert_cli_row_schema(command, output):
    schema = CLI_OUTPUT_SCHEMA[command]
    assert set(output) == set(schema["top_level"])
    assert output["columns"] == list(schema["row"])
    assert set().union(*(set(row) for row in output["rows"])) == set(schema["row"])


def _assert_complete_cli_schema(command, output):
    _assert_cli_row_schema(command, output)
    schema = CLI_OUTPUT_SCHEMA[command]
    if command == "benchmarks":
        return
    summary = output["vendor_resolution"]
    assert set(summary) == set(schema["vendor_summary"])
    assert set(summary["counts"]) == set(schema["vendor_counts"])
    resolutions = [row["vendor_resolution"] for row in output["rows"]]
    assert set().union(*(set(resolution) for resolution in resolutions)) == set(
        schema["row_vendor_resolution"]
    )
    matches = [
        match
        for resolution in resolutions
        for match in resolution.get("matches", [])
    ]
    assert matches
    assert set().union(*(set(match) for match in matches)) == set(
        schema["vendor_match"]
    )


def _invoke_recorded_cli(command, rows, *, roster=(), extra_args=()):
    response = _response(_parquet_bytes(rows))
    if command == "benchmarks":
        response.geturl.return_value = (
            "https://app.propertymeld.com/1000/m/1000/api/analytics/parquet/"
            "benchmarks.parquet"
        )
    with patch("urllib.request.urlopen", return_value=response), patch(
        "cli_anything.propertymeld.api_backend.list_vendors",
        return_value=list(roster),
    ):
        result = CliRunner().invoke(
            cli,
            ["insights", command, *extra_args, "--limit", "10"],
        )
    assert result.exit_code == 0
    return json.loads(result.output)


def test_melds_uses_exact_get_and_resolves_without_leaking_free_text(credentials):
    payload = _parquet_bytes(RECORDED_MELDS)
    roster = [{"id": 91159, "name": "DBH Construction"}]
    with patch("urllib.request.urlopen", return_value=_response(payload)) as opener, patch(
        "cli_anything.propertymeld.api_backend.list_vendors", return_value=roster
    ) as list_vendors:
        result = insights_backend.get_melds(limit=10)

    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url == (
        "https://app.propertymeld.com/1000/m/1000/api/analytics/parquet/"
        "raw_meld_data.parquet"
    )
    list_vendors.assert_called_once_with(limit=None)
    assert result["matched_count"] == 3
    assert result["returned_count"] == 3
    assert len(result["rows"]) == 3
    assert result["rows"][0]["vendor_resolution"] == {
        "status": "resolved",
        "source_name": "  DBH   Construction ",
        "vendor_id": 91159,
        "vendor_name": "DBH Construction",
    }
    assert result["rows"][1]["vendor_resolution"]["status"] == "unresolved"
    assert result["rows"][2]["vendor_resolution"]["status"] == "not_applicable"
    assert result["vendor_resolution"]["unresolved_names"] == ["Unknown Vendor"]
    for row in result["rows"]:
        assert "meld_meld_brief_description" not in row
        assert "workentry_log" not in row


def test_turnovers_filter_preserves_unresolved_rows(credentials):
    payload = _parquet_bytes(RECORDED_MELDS)
    with patch("urllib.request.urlopen", return_value=_response(payload)), patch(
        "cli_anything.propertymeld.api_backend.list_vendors", return_value=[]
    ):
        result = insights_backend.get_melds(limit=10, turnovers_only=True)

    assert result["dataset"] == "turnovers"
    assert [row["meld_meld_id"] for row in result["rows"]] == [101, 102]
    assert all(row["vendor_resolution"]["status"] == "unresolved" for row in result["rows"])


def test_meld_project_filter_treats_nan_as_non_project(credentials):
    payload = _parquet_bytes(RECORDED_NAN_MELDS)
    with patch("urllib.request.urlopen", return_value=_response(payload)), patch(
        "cli_anything.propertymeld.api_backend.list_vendors", return_value=[]
    ):
        projects = insights_backend.get_melds(limit=10, project=True)
        non_projects = insights_backend.get_melds(limit=10, project=False)

    assert [row["meld_meld_id"] for row in projects["rows"]] == [201]
    assert [row["meld_meld_id"] for row in non_projects["rows"]] == [202]
    assert non_projects["rows"][0]["meld_meld_project_id"] is None
    assert projects["project_missing_count"] == 1
    assert non_projects["project_missing_count"] == 1


@pytest.mark.parametrize("command", ["melds", "turnovers", "benchmarks"])
@pytest.mark.parametrize("flags", [[], ["--project"], ["--non-project"]], ids=[
    "no-flag",
    "project",
    "non-project",
])
def test_cli_preserves_missing_project_disclosure_for_every_mode(
    credentials, command, flags
):
    if command == "benchmarks":
        response = _response(_parquet_bytes(RECORDED_NAN_BENCHMARKS))
        response.geturl.return_value = (
            "https://app.propertymeld.com/1000/m/1000/api/analytics/parquet/"
            "benchmarks.parquet"
        )
    else:
        response = _response(_parquet_bytes(RECORDED_NAN_MELDS))
    with patch("urllib.request.urlopen", return_value=response), patch(
        "cli_anything.propertymeld.api_backend.list_vendors", return_value=[]
    ):
        result = CliRunner().invoke(
            cli,
            ["insights", command, *flags, "--limit", "10"],
        )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert output["project_missing_count"] == 1


@pytest.mark.parametrize("command", ["melds", "turnovers", "benchmarks"])
def test_cli_preserves_every_named_output_member_from_recorded_parquet(
    credentials, command
):
    if command == "benchmarks":
        response = _response(_parquet_bytes(RECORDED_CLI_BENCHMARKS))
        response.geturl.return_value = (
            "https://app.propertymeld.com/1000/m/1000/api/analytics/parquet/"
            "benchmarks.parquet"
        )
    else:
        response = _response(_parquet_bytes(RECORDED_CLI_MELDS))
    with patch("urllib.request.urlopen", return_value=response), patch(
        "cli_anything.propertymeld.api_backend.list_vendors",
        return_value=CLI_VENDOR_ROSTER,
    ):
        result = CliRunner().invoke(
            cli,
            ["insights", command, "--limit", "10"],
        )

    assert result.exit_code == 0
    output = json.loads(result.output)
    _assert_complete_cli_schema(command, output)
    assert output == _expected_cli_payload(command)


@pytest.mark.parametrize("command", ["melds", "turnovers", "benchmarks"])
def test_cli_schema_is_invariant_for_minimal_supported_parquet(
    credentials, command
):
    if command == "benchmarks":
        rows = RECORDED_MINIMAL_BENCHMARKS
        required = {
            "unit_count", "priority", "work_category", "region", "is_project",
            "completed_month",
        }
        roster = ()
    else:
        rows = RECORDED_MINIMAL_MELDS
        required = {
            "meld_meld_id", "meld_meld_work_category", "vendor_assigned_name",
            "vendor_resolution",
        }
        roster = CLI_VENDOR_ROSTER[:1]

    output = _invoke_recorded_cli(command, rows, roster=roster)
    _assert_cli_row_schema(command, output)
    row = output["rows"][0]
    optional = set(CLI_OUTPUT_SCHEMA[command]["row"]) - required
    assert optional
    assert {field: row[field] for field in optional} == {
        field: None for field in optional
    }


@pytest.mark.parametrize(
    "command,missing_member",
    REQUIRED_CLI_INPUT_ROSTER,
    ids=[
        f"{command}-missing-{member}"
        for command, member in REQUIRED_CLI_INPUT_ROSTER
    ],
)
def test_cli_fails_closed_and_names_each_missing_required_member(
    credentials, command, missing_member
):
    source = (
        RECORDED_MINIMAL_BENCHMARKS[0]
        if command == "benchmarks"
        else RECORDED_MINIMAL_MELDS[0]
    )
    row = dict(source)
    del row[missing_member]
    response = _response(_parquet_bytes([row]))
    if command == "benchmarks":
        response.geturl.return_value = (
            "https://app.propertymeld.com/1000/m/1000/api/analytics/parquet/"
            "benchmarks.parquet"
        )
    with patch("urllib.request.urlopen", return_value=response), patch(
        "cli_anything.propertymeld.api_backend.list_vendors",
        return_value=CLI_VENDOR_ROSTER[:1],
    ):
        result = CliRunner().invoke(
            cli,
            ["insights", command, "--limit", "10"],
        )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "error": (
            "Insights parquet schema is missing required columns: "
            f"{missing_member}"
        )
    }


@pytest.mark.parametrize("command", ["melds", "turnovers"])
def test_cli_vendor_counts_zero_fill_resolved_only_result(credentials, command):
    output = _invoke_recorded_cli(
        command,
        RECORDED_MINIMAL_MELDS,
        roster=CLI_VENDOR_ROSTER[:1],
    )

    assert output["vendor_resolution"] == {
        "counts": {
            "resolved": 1,
            "unresolved": 0,
            "ambiguous": 0,
            "not_applicable": 0,
        },
        "unresolved_names": [],
        "ambiguous_names": [],
    }
    assert output["rows"][0]["vendor_resolution"]["status"] == "resolved"


@pytest.mark.parametrize("command", ["melds", "turnovers"])
def test_cli_vendor_counts_zero_fill_unresolved_only_result(credentials, command):
    rows = [dict(RECORDED_MINIMAL_MELDS[0], vendor_assigned_name="Unknown Vendor")]
    output = _invoke_recorded_cli(command, rows)

    assert output["vendor_resolution"] == {
        "counts": {
            "resolved": 0,
            "unresolved": 1,
            "ambiguous": 0,
            "not_applicable": 0,
        },
        "unresolved_names": ["Unknown Vendor"],
        "ambiguous_names": [],
    }
    assert output["rows"][0]["vendor_resolution"]["status"] == "unresolved"


@pytest.mark.parametrize("command", ["melds", "turnovers"])
def test_cli_vendor_counts_zero_fill_empty_result(credentials, command):
    if command == "melds":
        rows = RECORDED_MINIMAL_MELDS
        extra_args = ("--work-category", "PLUMBING")
    else:
        rows = [
            dict(RECORDED_MINIMAL_MELDS[0], meld_meld_work_category="PLUMBING")
        ]
        extra_args = ()
    output = _invoke_recorded_cli(
        command,
        rows,
        roster=CLI_VENDOR_ROSTER[:1],
        extra_args=extra_args,
    )

    assert output["columns"] == list(CLI_OUTPUT_SCHEMA[command]["row"])
    assert output["matched_count"] == 0
    assert output["returned_count"] == 0
    assert output["rows"] == []
    assert output["vendor_resolution"] == {
        "counts": {
            "resolved": 0,
            "unresolved": 0,
            "ambiguous": 0,
            "not_applicable": 0,
        },
        "unresolved_names": [],
        "ambiguous_names": [],
    }


def test_default_backends_disclose_recorded_nan_for_all_datasets():
    meld_payload = _parquet_bytes(RECORDED_NAN_MELDS)
    benchmark_payload = _parquet_bytes(RECORDED_NAN_BENCHMARKS)
    with patch.object(
        insights_backend,
        "_fetch_parquet_bytes",
        side_effect=[meld_payload, meld_payload, benchmark_payload],
    ), patch(
        "cli_anything.propertymeld.api_backend.list_vendors", return_value=[]
    ):
        results = {
            "melds": insights_backend.get_melds(limit=10),
            "turnovers": insights_backend.get_melds(limit=10, turnovers_only=True),
            "benchmarks": insights_backend.get_benchmarks(limit=10),
        }

    assert {
        dataset: result["project_missing_count"]
        for dataset, result in results.items()
    } == {"melds": 1, "turnovers": 1, "benchmarks": 1}


def test_vendor_name_collision_is_ambiguous_and_row_is_not_dropped():
    rows = [{"meld_meld_id": 1, "vendor_assigned_name": "Same Vendor"}]
    vendors = [
        {"id": 1, "name": "Same Vendor"},
        {"id": 2, "name": " same   vendor "},
    ]
    enriched, summary = insights_backend.resolve_vendor_names(rows, vendors)
    assert len(enriched) == 1
    resolution = enriched[0]["vendor_resolution"]
    assert resolution["status"] == "ambiguous"
    assert [match["vendor_id"] for match in resolution["matches"]] == [1, 2]
    assert summary["ambiguous_names"] == ["Same Vendor"]


def test_duplicate_roster_rows_with_same_id_are_not_false_ambiguous():
    rows = [{"meld_meld_id": 1, "vendor_assigned_name": "Same Vendor"}]
    vendors = [
        {"id": 1, "name": "Same Vendor"},
        {"id": 1, "name": " same   vendor "},
    ]
    enriched, summary = insights_backend.resolve_vendor_names(rows, vendors)
    assert enriched[0]["vendor_resolution"]["status"] == "resolved"
    assert enriched[0]["vendor_resolution"]["vendor_id"] == 1
    assert summary["ambiguous_names"] == []


def test_vendor_roster_unbounded_mode_exhausts_nexus_pagination():
    first_page = {
        "next": "https://app.propertymeld.com/api/v2/vendor/?cursor=second",
        "results": [{"id": 1, "name": "First"}],
    }
    second_page = {
        "next": None,
        "results": [{"id": 2, "name": "Second"}],
    }
    with patch.object(api_backend, "_api_get", side_effect=[first_page, second_page]) as getter:
        rows = api_backend.list_vendors(limit=None)
    assert rows == [{"id": 1, "name": "First"}, {"id": 2, "name": "Second"}]
    assert getter.call_count == 2


def test_vendor_roster_transport_is_get_without_body():
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.read.return_value = json.dumps({
        "next": None,
        "results": [{"id": 1, "name": "Recorded Vendor"}],
    }).encode()
    with patch.object(api_backend, "get_token", return_value="recorded-token"), patch(
        "urllib.request.urlopen", return_value=response
    ) as opener:
        rows = api_backend.list_vendors(limit=None)

    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url.endswith("/api/v2/vendor/?limit=100")
    assert rows == [{"id": 1, "name": "Recorded Vendor"}]


def test_benchmarks_exact_endpoint_and_recorded_shape(credentials):
    payload = _parquet_bytes(RECORDED_BENCHMARKS)
    response = _response(payload)
    response.geturl.return_value = (
        "https://app.propertymeld.com/1000/m/1000/api/analytics/parquet/"
        "benchmarks.parquet"
    )
    with patch("urllib.request.urlopen", return_value=response) as opener:
        result = insights_backend.get_benchmarks(
            limit=10,
            work_category="turnover",
            priority="normal",
            region="southeast",
            project=True,
        )
    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.full_url.endswith("/api/analytics/parquet/benchmarks.parquet")
    assert result["returned_count"] == 1
    assert result["rows"][0]["sor_50th"] == 3.0


def test_benchmark_project_filter_treats_nan_as_non_project(credentials):
    payload = _parquet_bytes(RECORDED_NAN_BENCHMARKS)
    response = _response(payload)
    response.geturl.return_value = (
        "https://app.propertymeld.com/1000/m/1000/api/analytics/parquet/"
        "benchmarks.parquet"
    )
    with patch("urllib.request.urlopen", return_value=response):
        projects = insights_backend.get_benchmarks(limit=10, project=True)
        non_projects = insights_backend.get_benchmarks(limit=10, project=False)

    assert [row["priority"] for row in projects["rows"]] == ["NORMAL"]
    assert [row["priority"] for row in non_projects["rows"]] == ["HIGH"]
    assert non_projects["rows"][0]["is_project"] is None
    assert projects["project_missing_count"] == 1
    assert non_projects["project_missing_count"] == 1


def test_unknown_dataset_refuses_before_credentials_or_network():
    with patch("urllib.request.urlopen") as opener:
        with pytest.raises(insights_backend.InsightsError, match="Unsupported"):
            insights_backend._fetch_parquet_bytes("arbitrary-path")
    opener.assert_not_called()


def test_non_parquet_login_or_html_response_fails_closed(credentials):
    with patch("urllib.request.urlopen", return_value=_response(b"<html>login</html>")):
        with pytest.raises(insights_backend.InsightsError, match="valid parquet"):
            insights_backend._fetch_parquet_bytes("melds")


def test_http_401_is_clean_failure_without_recapture(credentials):
    error = __import__("urllib.error").error.HTTPError(
        "https://app.propertymeld.com/insights",
        401,
        "Unauthorized",
        {},
        io.BytesIO(b"unauthorized"),
    )
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(insights_backend.InsightsError, match="HTTP 401"):
            insights_backend._fetch_parquet_bytes("melds")


def test_missing_required_recorded_column_fails_closed():
    payload = _parquet_bytes([{"meld_meld_id": 1}])
    with pytest.raises(insights_backend.InsightsError, match="missing required columns"):
        insights_backend._read_parquet_rows(
            payload,
            allowed_columns=insights_backend._MELD_COLUMNS,
            required_columns=insights_backend._MELD_REQUIRED,
        )


def test_source_has_only_literal_get_request_construction():
    source = Path(insights_backend.__file__).read_text()
    tree = ast.parse(source)
    request_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Request"
    ]
    assert len(request_calls) == 1
    methods = [
        keyword.value
        for keyword in request_calls[0].keywords
        if keyword.arg == "method"
    ]
    assert len(methods) == 1
    assert isinstance(methods[0], ast.Constant)
    assert methods[0].value == "GET"
    assert "with_recapture_retry" not in source
    assert "_attempt_recapture" not in source
    assert set(cli.commands["insights"].commands) == {
        "melds",
        "turnovers",
        "benchmarks",
    }


@pytest.mark.parametrize("command,dataset", [
    (["insights", "melds", "--limit", "2"], "melds"),
    (["insights", "turnovers", "--limit", "2"], "turnovers"),
    (["insights", "benchmarks", "--limit", "2"], "benchmarks"),
])
def test_cli_subcommands_emit_clean_json(command, dataset):
    expected = {"dataset": dataset, "returned_count": 0, "rows": []}
    target = (
        "cli_anything.propertymeld.insights_backend.get_benchmarks"
        if dataset == "benchmarks"
        else "cli_anything.propertymeld.insights_backend.get_melds"
    )
    with patch(target, return_value=expected):
        result = CliRunner().invoke(cli, command)
    assert result.exit_code == 0
    assert json.loads(result.output) == expected


def test_cli_backend_failure_is_json_and_nonzero():
    with patch(
        "cli_anything.propertymeld.insights_backend.get_melds",
        side_effect=insights_backend.InsightsError("recorded schema drift"),
    ):
        result = CliRunner().invoke(cli, ["insights", "melds"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"error": "recorded schema drift"}
