"""CLI subprocess tests — verify commands produce valid JSON output."""
import json
import subprocess
import sys
import os
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from cli_anything.propertymeld.cli import cli
from cli_anything.propertymeld.utils import clear_token_cache

os.environ.setdefault("PM_CLIENT_ID", "test-id")
os.environ.setdefault("PM_CLIENT_SECRET", "test-secret")


@pytest.fixture(autouse=True)
def reset_cache():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def runner():
    return CliRunner()


MOCK_WO_LIST = [{"id": 1001, "status": "open", "description": "Test WO"}]
MOCK_PROPERTIES = [{"id": 5, "name": "123 Main St"}]
MOCK_VENDORS = [{"id": 10, "name": "Dyer HVAC"}]


class TestWorkOrdersCLI:
    def test_list_outputs_json(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_work_orders",
                   return_value=MOCK_WO_LIST):
            result = runner.invoke(cli, ["work-orders", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["id"] == 1001

    def test_list_with_status_flag(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_work_orders",
                   return_value=MOCK_WO_LIST) as mock_fn:
            runner.invoke(cli, ["work-orders", "list", "--status", "open"])
        mock_fn.assert_called_once_with(
            status="open",
            assigned_to_tech=None,
            assigned_to_vendor=None,
            stuck_hours=None,
            created_since=None,
            status_not=None,
            no_tenant_linked=False,
            limit=25,
        )

    def test_list_with_new_filter_flags(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_work_orders",
                   return_value=MOCK_WO_LIST) as mock_fn:
            result = runner.invoke(
                cli,
                [
                    "work-orders", "list",
                    "--assigned-to-tech", "90025",
                    "--assigned-to-vendor", "99",
                    "--stuck-hours", "48",
                    "--created-since", "2026-05-18T00:00:00Z",
                    "--status-not", "COMPLETED",
                    "--no-tenant-linked",
                ],
            )
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(
            status=None,
            assigned_to_tech=90025,
            assigned_to_vendor=99,
            stuck_hours=48.0,
            created_since="2026-05-18T00:00:00Z",
            status_not="COMPLETED",
            no_tenant_linked=True,
            limit=25,
        )

    def test_get_outputs_single_json(self, runner):
        with patch("cli_anything.propertymeld.api_backend.get_work_order",
                   return_value=MOCK_WO_LIST[0]):
            result = runner.invoke(cli, ["work-orders", "get", "1001"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 1001


class TestPropertiesCLI:
    def test_list_outputs_json(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_properties",
                   return_value=MOCK_PROPERTIES):
            result = runner.invoke(cli, ["properties", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["name"] == "123 Main St"


class TestVendorsCLI:
    def test_list_outputs_json(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_vendors",
                   return_value=MOCK_VENDORS):
            result = runner.invoke(cli, ["vendors", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["name"] == "Dyer HVAC"


class TestProbeCLI:
    def test_probe_outputs_ok(self, runner):
        with patch("cli_anything.propertymeld.api_backend.probe",
                   return_value={"ok": True, "token_prefix": "test-tok..."}):
            result = runner.invoke(cli, ["probe"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True


# ──────────────────────────────────────────────────────────────────────────────
# Coverage for unpushed feats: work-orders files / assign-vendor /
# work-orders schedule / work-orders merge|complete|cancel / tenants list|get
# ──────────────────────────────────────────────────────────────────────────────


MOCK_FILES = [
    {"id": 9001, "filename": "before.jpg", "signed_url": "https://example/before.jpg"},
    {"id": 9002, "filename": "after.jpg", "signed_url": "https://example/after.jpg"},
]
MOCK_TENANTS = [
    {"id": 1, "first_name": "Fixture", "last_name": "Doe", "email": "fixture.alpha@example.com"},
    {"id": 2, "first_name": "Resident", "last_name": "Person035", "email": "fixture.beta@example.com"},
]


class TestWorkOrdersFilesCLI:
    def test_files_outputs_list(self, runner):
        with patch("cli_anything.propertymeld.http_backend.list_files",
                   return_value=MOCK_FILES) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "files", "90000014"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["filename"] == "before.jpg"
        mock_fn.assert_called_once_with("90000014")


class TestListFilesMergesAllSources:
    """list_files() hits manager + tenant + vendor endpoints and tags uploader_role."""

    def _stub_http_get(self, manager_items, tenant_items, vendor_items):
        def _side(path, cookie_hdr):
            if "tenant-files" in path:
                return {"results": tenant_items}
            if "vendor-files" in path:
                return {"results": vendor_items}
            return {"results": manager_items}
        return _side

    def test_merges_three_sources_with_uploader_role(self):
        from cli_anything.propertymeld import http_backend
        side = self._stub_http_get(
            [{"id": 1, "filename": "mgr.pdf"}],
            [{"id": 2, "filename": "tenant.jpg"}],
            [{"id": 3, "filename": "vendor.png"}],
        )
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value=""), \
             patch("cli_anything.propertymeld.http_backend._http_get", side_effect=side):
            result = http_backend.list_files("90000014")
        assert len(result) == 3
        roles = {f["uploader_role"] for f in result}
        assert roles == {"manager", "tenant", "vendor"}
        mgr = next(f for f in result if f["uploader_role"] == "manager")
        assert mgr["filename"] == "mgr.pdf"

    def test_merges_when_some_endpoints_empty(self):
        from cli_anything.propertymeld import http_backend
        side = self._stub_http_get(
            [{"id": 1, "filename": "mgr-only.pdf"}],
            [],
            [],
        )
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value=""), \
             patch("cli_anything.propertymeld.http_backend._http_get", side_effect=side):
            result = http_backend.list_files("90000014")
        assert len(result) == 1
        assert result[0]["uploader_role"] == "manager"

    def test_handles_flat_list_response(self):
        from cli_anything.propertymeld import http_backend
        def side(path, cookie_hdr):
            if "tenant-files" in path:
                return [{"id": 9, "filename": "flat.jpg"}]
            if "vendor-files" in path:
                return []
            return {"results": []}
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value=""), \
             patch("cli_anything.propertymeld.http_backend._http_get", side_effect=side):
            result = http_backend.list_files("90000014")
        assert len(result) == 1
        assert result[0]["uploader_role"] == "tenant"


class TestWorkOrdersWorkEntriesCLI:
    def test_work_entries_outputs_list(self, runner):
        mock_entries = [
            {"id": 1, "checkin": "2026-05-01T08:00:00Z", "checkout": "2026-05-01T10:30:00Z",
             "hours": 2.5, "agent_name": "Person019", "description": "AC tune-up",
             "long_description": "Replaced filter, cleaned coils."},
        ]
        with patch("cli_anything.propertymeld.http_backend.list_work_entries",
                   return_value=mock_entries) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "work-entries", "90000014"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["agent_name"] == "Person019"
        mock_fn.assert_called_once_with("90000014")


class TestAssignVendorCLI:
    def test_assign_vendor_passes_partial_name(self, runner):
        with patch("cli_anything.propertymeld.http_backend.assign_vendor_by_name",
                   return_value={"ok": True, "vendor_id": 10, "matched_name": "Dyer HVAC"}) as mock_fn:
            result = runner.invoke(cli, ["assign-vendor",
                                         "--work-order-id", "90000014",
                                         "--vendor", "dyer"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        mock_fn.assert_called_once_with("90000014", "dyer", account_prefix="1")


class TestWorkOrdersScheduleCLI:
    def test_schedule_passes_dtstart_and_hours(self, runner):
        with patch("cli_anything.propertymeld.http_backend.schedule_appointment",
                   return_value={"id": 4242, "scheduled_dtstart": "2026-04-27T14:00:00-04:00"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "schedule",
                                         "--meld-id", "90000014",
                                         "--dtstart", "2026-04-27T14:00:00-04:00",
                                         "--hours", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 4242
        mock_fn.assert_called_once_with("90000014",
                                        "2026-04-27T14:00:00-04:00",
                                        duration_hours=3.0)


class TestWorkOrdersLifecycleCLI:
    def test_clone_supports_all_override_flags(self, runner):
        with patch("cli_anything.propertymeld.http_backend.clone_meld",
                   return_value={"ok": True, "new_meld_id": 777}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "clone",
                                         "--meld-id", "90000014",
                                         "--description", "Reset toilet",
                                         "--long-description", "Replace wax ring + punch list",
                                         "--no-tenant-presence-required",
                                         "--unit-id", "9000005",
                                         "--priority", "low"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["new_meld_id"] == 777
        mock_fn.assert_called_once_with(
            "90000014",
            brief_description="Reset toilet",
            description="Replace wax ring + punch list",
            tenant_presence_required=False,
            unit_id=9000005,
            priority="LOW",
        )

    def test_merge_into_destination(self, runner):
        with patch("cli_anything.propertymeld.http_backend.merge_meld",
                   return_value={"ok": True, "source": "90000014", "destination": "12701109"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "merge",
                                         "--meld-id", "90000014",
                                         "--into", "12701109"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["destination"] == "12701109"
        mock_fn.assert_called_once_with("90000014", "12701109")

    def test_complete_with_notes(self, runner):
        with patch("cli_anything.propertymeld.http_backend.complete_meld",
                   return_value={"id": 1001, "status": "COMPLETE"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "complete",
                                         "--meld-id", "90000014",
                                         "--notes", "Replaced filter."])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "COMPLETE"
        mock_fn.assert_called_once_with("90000014", completion_notes="Replaced filter.")

    def test_cancel_with_reason(self, runner):
        with patch("cli_anything.propertymeld.http_backend.cancel_meld",
                   return_value={"id": 1002, "status": "CANCELLED"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "cancel",
                                         "--meld-id", "90000014",
                                         "--reason", "Duplicate"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "CANCELLED"
        mock_fn.assert_called_once_with("90000014", reason="Duplicate")


class TestTenantsCLI:
    def test_list_with_search(self, runner):
        with patch("cli_anything.propertymeld.http_backend.list_tenants",
                   return_value=MOCK_TENANTS) as mock_fn:
            result = runner.invoke(cli, ["tenants", "list", "--search", "doe"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        mock_fn.assert_called_once_with(search="doe", limit=100)

    def test_get_single_tenant(self, runner):
        with patch("cli_anything.propertymeld.http_backend.get_tenant",
                   return_value=MOCK_TENANTS[0]):
            result = runner.invoke(cli, ["tenants", "get", "1"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["first_name"] == "Fixture"


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2 backport coverage: projects / estimates / receipts / vendor-invites /
# work-orders schedule-vendor
# ──────────────────────────────────────────────────────────────────────────────


MOCK_PROJECT = {"id": 7001, "name": "Q2 Renovations", "description": "Bldg-A common-area refresh"}
MOCK_ESTIMATE = {"id": 8001, "estimate_number": "INV-2026-001", "amount": "1250.00", "status": "draft"}
MOCK_RECEIPT = {"id": 9001, "filename": "home-depot-2026-04-29.pdf", "linked_estimate_id": 8001}


class TestProjectsCLI:
    """projects create/update/delete dropped per Item 3 spike — list + get only."""

    def test_list_outputs_json(self, runner):
        with patch("cli_anything.propertymeld.http_backend.list_projects",
                   return_value=[MOCK_PROJECT]) as mock_fn:
            result = runner.invoke(cli, ["projects", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["id"] == 7001
        mock_fn.assert_called_once()

    def test_get_outputs_single_json(self, runner):
        with patch("cli_anything.propertymeld.http_backend.get_project",
                   return_value=MOCK_PROJECT):
            result = runner.invoke(cli, ["projects", "get", "7001"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 7001


class TestEstimatesCLI:
    def test_create_estimate_passes_args(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_estimate",
                   return_value=MOCK_ESTIMATE) as mock_fn:
            result = runner.invoke(cli, ["estimates", "create",
                                         "--meld-id", "90000014",
                                         "--estimate-number", "INV-2026-001",
                                         "--amount", "1250.00"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["estimate_number"] == "INV-2026-001"
        # Positional + kwarg shape per cli wiring
        call = mock_fn.call_args
        assert call.args[0] == "90000014"
        assert call.args[1] == "INV-2026-001"
        assert call.args[2] == "1250.00"


class TestReceiptsCLI:
    def test_upload_receipt_passes_file_path(self, runner, tmp_path):
        receipt_file = tmp_path / "rcpt.pdf"
        receipt_file.write_bytes(b"%PDF-1.4 fake\n")
        with patch("cli_anything.propertymeld.http_backend.upload_receipt",
                   return_value=MOCK_RECEIPT) as mock_fn:
            result = runner.invoke(cli, ["receipts", "upload",
                                         "--meld-id", "90000014",
                                         "--file", str(receipt_file)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 9001
        mock_fn.assert_called_once()
        call = mock_fn.call_args
        assert call.args[0] == "90000014"
        assert call.args[1] == str(receipt_file)


class TestWorkOrdersScheduleVendorCLI:
    def test_schedule_vendor_passes_args(self, runner):
        with patch("cli_anything.propertymeld.http_backend.schedule_vendor_appointment",
                   return_value={"id": 5555, "scheduled_dtstart": "2026-05-06T14:00:00-04:00"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "schedule-vendor",
                                         "--meld-id", "90000014",
                                         "--vendor-id", "10",
                                         "--dtstart", "2026-05-06T14:00:00-04:00",
                                         "--hours", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 5555
        mock_fn.assert_called_once_with("90000014", "10",
                                        "2026-05-06T14:00:00-04:00",
                                        duration_hours=3.0)


# ──────────────────────────────────────────────────────────────────────────────
# Projects create / edit / detach-meld + work-orders update-notes (PR cli-wire)
# ──────────────────────────────────────────────────────────────────────────────


class TestProjectsCreatePerson003CLI:
    def test_create_passes_args_shape(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_project",
                   return_value={"ok": True, "project_id": 222969, "result": {"id": 222969}}) as mock_fn:
            result = runner.invoke(cli, [
                "projects", "create",
                "--name", "Test",
                "--project-type", "TURN",
                "--due-date", "2026-05-30T04:00:00.000Z",
                "--start-date", "2026-05-14T10:30:00Z",
                "--coordinator", "90025",
                "--unit-id", "9000005",
                "--unit-label", "123 Main St",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["project_id"] == 222969
        kw = mock_fn.call_args.kwargs
        assert kw["name"] == "Test"
        assert kw["project_type"] == "TURN"
        assert kw["coordinators"] == [90025]
        assert kw["unit"] == {"id": 9000005, "label": "123 Main St"}
        assert kw["meld_location"] == "Unit"
        assert kw["prop"] is None

    def test_create_with_prop_id_builds_prop_dict(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_project",
                   return_value={"ok": True, "project_id": 1, "result": {}}) as mock_fn:
            runner.invoke(cli, [
                "projects", "create",
                "--name", "P", "--project-type", "TURN",
                "--due-date", "2026-05-30T04:00:00.000Z",
                "--start-date", "2026-05-14T10:30:00Z",
                "--coordinator", "90025",
                "--unit-id", "9000005", "--unit-label", "L",
                "--prop-id", "9999",
            ])
        kw = mock_fn.call_args.kwargs
        assert kw["prop"] == {"id": 9999}

    def test_edit_passes_only_set_fields_plus_unit_dict(self, runner):
        with patch("cli_anything.propertymeld.http_backend.update_project",
                   return_value={"ok": True, "project_id": "222969", "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "projects", "edit", "222969",
                "--name", "Renamed",
                "--description", "new desc",
                "--unit-id", "9000005", "--unit-label", "123 Main",
            ])
        assert result.exit_code == 0
        kw = mock_fn.call_args.kwargs
        assert kw["project_id"] == "222969"
        assert kw["name"] == "Renamed"
        assert kw["description"] == "new desc"
        assert kw["unit"] == {"id": 9000005, "label": "123 Main"}
        # Unset fields should be None so the backend's fetch+merge can echo them.
        assert kw["project_type"] is None
        assert kw["due_date"] is None
        assert kw["start_date"] is None
        assert kw["coordinators"] is None


class TestProjectsDetachMeldCLI:
    def test_detach_meld_calls_patch_link_with_none(self, runner):
        with patch("cli_anything.propertymeld.http_backend.patch_meld_project_link",
                   return_value={"ok": True, "meld_id": 90000002, "project_id": None, "result": {}}) as mock_fn:
            result = runner.invoke(cli, ["projects", "detach-meld", "90000002"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["project_id"] is None
        call = mock_fn.call_args
        assert call.args[0] == "90000002"
        assert call.args[1] is None


class TestWorkOrdersUpdateNotesCLI:
    def test_update_notes_passes_text(self, runner):
        with patch("cli_anything.propertymeld.http_backend.update_meld_notes",
                   return_value={"ok": True, "meld_id": 90000002, "result": {"maintenance_notes": "hi"}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "update-notes", "90000002",
                "--maintenance", "hi",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["result"]["maintenance_notes"] == "hi"
        call = mock_fn.call_args
        assert call.args[0] == "90000002"
        assert call.args[1] == "hi"


class TestProjectsCreateMeldInCLI:
    """pm-dev projects create-meld-in — ergonomic --unit-id + --maintenance-id flags."""

    _COMMON_ARGS = [
        "projects", "create-meld-in", "222959",
        "--brief-description", "b",
        "--description", "d",
        "--work-category", "APPLIANCES",
        "--work-type", "TURN",
        "--due-date", "2026-05-16T00:00:00.000Z",
    ]

    def test_id_flags_pass_stripped_objects(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project",
                   return_value={"ok": True, "meld_id": 99, "project_id": "222959", "result": {}}) as mock_fn:
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-id", "9000005",
                "--maintenance-id", "90025",
            ])
        assert result.exit_code == 0, result.output
        call = mock_fn.call_args
        assert call.kwargs["unit"] == {"id": 9000005}
        assert call.kwargs["maintenance"] == [{"id": 90025}]

    def test_multiple_maintenance_ids_repeatable(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project",
                   return_value={"ok": True, "meld_id": 99, "project_id": "222959", "result": {}}) as mock_fn:
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-id", "9000005",
                "--maintenance-id", "90025",
                "--maintenance-id", "90028",
            ])
        assert result.exit_code == 0, result.output
        assert mock_fn.call_args.kwargs["maintenance"] == [{"id": 90025}, {"id": 90028}]

    def test_json_flags_still_work_for_power_users(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project",
                   return_value={"ok": True, "meld_id": 99, "project_id": "222959", "result": {}}) as mock_fn:
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-json", '{"id": 1, "display_address": {}, "prop": {}, "current_tenants": []}',
                "--maintenance-json", '[{"id": 9}]',
            ])
        assert result.exit_code == 0, result.output
        kwargs = mock_fn.call_args.kwargs
        assert kwargs["unit"]["id"] == 1
        assert kwargs["maintenance"] == [{"id": 9}]

    def test_missing_both_unit_flags_errors(self, runner):
        result = runner.invoke(cli, self._COMMON_ARGS + [
            "--maintenance-id", "90025",
        ])
        assert result.exit_code != 0
        assert "--unit-id" in result.output and "--unit-json" in result.output

    def test_passing_both_unit_flags_errors(self, runner):
        result = runner.invoke(cli, self._COMMON_ARGS + [
            "--unit-id", "9000005",
            "--unit-json", '{"id": 1}',
            "--maintenance-id", "90025",
        ])
        assert result.exit_code != 0
        assert "--unit-id" in result.output and "--unit-json" in result.output

    def test_missing_both_maintenance_flags_errors(self, runner):
        result = runner.invoke(cli, self._COMMON_ARGS + [
            "--unit-id", "9000005",
        ])
        assert result.exit_code != 0
        assert "--maintenance-id" in result.output and "--maintenance-json" in result.output


class TestWorkOrdersLinkTenantCLI:
    """pm work-orders link-tenant <meld_id> <tenant_id> — closes P1 #14."""

    def test_link_tenant_passes_meld_and_tenant_ids(self, runner):
        with patch("cli_anything.propertymeld.http_backend.link_tenant_to_meld",
                   return_value={"ok": True, "meld_id": "90000003", "tenant_id": 9000009,
                                 "linked": True, "tenant_count": 2, "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "link-tenant", "90000003", "9000009",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["linked"] is True
        assert data["tenant_id"] == 9000009
        mock_fn.assert_called_once_with("90000003", 9000009)

    def test_link_tenant_surfaces_already_linked(self, runner):
        with patch("cli_anything.propertymeld.http_backend.link_tenant_to_meld",
                   return_value={"ok": True, "meld_id": "90000003", "tenant_id": 9000009,
                                 "already_linked": True, "tenant_count": 1}):
            result = runner.invoke(cli, [
                "work-orders", "link-tenant", "90000003", "9000009",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["already_linked"] is True

    def test_link_tenant_requires_tenant_id_int(self, runner):
        # Click rejects non-int tenant_id at parse time
        result = runner.invoke(cli, [
            "work-orders", "link-tenant", "90000003", "not-a-number",
        ])
        assert result.exit_code != 0
        assert "Invalid value" in result.output or "not-a-number" in result.output


class TestP2GapCLICommands:
    def test_invoice_hold_calls_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.hold_meld_invoice",
                   return_value={"ok": True, "invoice_id": 3863382, "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "invoice-hold", "3863382",
                "--reason", "needs revision",
            ])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(3863382, reason="needs revision")

    def test_invoice_decline_calls_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.decline_meld_invoice",
                   return_value={"ok": True, "invoice_id": 3863382, "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "invoice-decline", "3863382",
                "--reason", "wrong job",
            ])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(3863382, reason="wrong job")

    def test_delete_file_calls_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.delete_meld_file",
                   return_value={"ok": True, "file_id": 20254356, "deleted": True}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "delete-file", "20254356"])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(20254356)

    def test_delete_project_calls_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.delete_project",
                   return_value={"ok": True, "project_id": 222964, "deleted": True}) as mock_fn:
            result = runner.invoke(cli, ["projects", "delete", "222964"])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(222964)


class TestUnitsPerson003NotesCLI:
    """pm units edit-notes <unit_id> --notes <text> — closes P3 #8 unit-level."""

    def test_passes_unit_id_and_notes_to_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.update_unit_notes",
                   return_value={"ok": True, "unit_id": 1754419,
                                 "maintenance_notes": "shut-off in basement", "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "units", "edit-notes", "1754419",
                "--notes", "shut-off in basement",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["maintenance_notes"] == "shut-off in basement"
        mock_fn.assert_called_once_with(1754419, "shut-off in basement")

    def test_requires_notes_flag(self, runner):
        result = runner.invoke(cli, ["units", "edit-notes", "1754419"])
        assert result.exit_code != 0
        assert "--notes" in result.output or "Missing option" in result.output

    def test_requires_int_unit_id(self, runner):
        result = runner.invoke(cli, [
            "units", "edit-notes", "not-a-number",
            "--notes", "x",
        ])
        assert result.exit_code != 0
