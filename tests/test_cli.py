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
MOCK_VENDORS = [{"id": 10, "name": "Fixture Service"}]


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
            status_raw=None,
            assigned_to_tech=None,
            assigned_to_vendor=None,
            stuck_hours=None,
            created_since=None,
            status_not=None,
            no_tenant_linked=False,
            include_tech=False,
            limit=25,
        )

    def test_list_with_new_filter_flags(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_work_orders",
                   return_value=MOCK_WO_LIST) as mock_fn:
            result = runner.invoke(
                cli,
                [
                    "work-orders", "list",
                    "--assigned-to-tech", "5011",
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
            status_raw=None,
            assigned_to_tech=5011,
            assigned_to_vendor=99,
            stuck_hours=48.0,
            created_since="2026-05-18T00:00:00Z",
            status_not="COMPLETED",
            no_tenant_linked=True,
            include_tech=False,
            limit=25,
        )

    def test_list_with_status_raw_flag(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_work_orders",
                   return_value=MOCK_WO_LIST) as mock_fn:
            result = runner.invoke(
                cli,
                [
                    "work-orders", "list",
                    "--status-raw", "MAINTENANCE_COULD_NOT_COMPLETE",
                    "--assigned-to-tech", "5011",
                    "--created-since", "2026-05-18T00:00:00Z",
                    "--status-not", "COMPLETED",
                ],
            )
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(
            status=None,
            status_raw="MAINTENANCE_COULD_NOT_COMPLETE",
            assigned_to_tech=5011,
            assigned_to_vendor=None,
            stuck_hours=None,
            created_since="2026-05-18T00:00:00Z",
            status_not="COMPLETED",
            no_tenant_linked=False,
            include_tech=False,
            limit=25,
        )

    def test_list_rejects_status_and_status_raw_conflict(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_work_orders") as mock_fn:
            result = runner.invoke(
                cli,
                [
                    "work-orders", "list",
                    "--status", "open",
                    "--status-raw", "MAINTENANCE_COULD_NOT_COMPLETE",
                ],
            )
        assert result.exit_code != 0
        assert "--status and --status-raw cannot be combined" in result.output
        mock_fn.assert_not_called()

    def test_list_with_include_tech_flag(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_work_orders",
                   return_value=MOCK_WO_LIST) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "list", "--include-tech"])
        assert result.exit_code == 0
        mock_fn.assert_called_once()
        assert mock_fn.call_args.kwargs["include_tech"] is True

    def test_get_outputs_single_json(self, runner):
        with patch("cli_anything.propertymeld.api_backend.get_work_order",
                   return_value=MOCK_WO_LIST[0]):
            result = runner.invoke(cli, ["work-orders", "get", "1001"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 1001

    def test_get_with_include_tech_flag(self, runner):
        with patch("cli_anything.propertymeld.api_backend.get_work_order",
                   return_value=MOCK_WO_LIST[0]) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "get", "1001", "--include-tech"])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with("1001", include_tech=True)


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
        assert data[0]["name"] == "Fixture Service"

    def test_invite_passes_captured_args(self, runner):
        with patch(
            "cli_anything.propertymeld.http_backend.invite_vendor",
            return_value={"ok": True, "email": "alex+zztest@example.com"},
        ) as mock_fn:
            result = runner.invoke(cli, [
                "vendors", "invite",
                "--email", "alex+zztest@example.com",
                "--first-name", "Fixture Capture",
                "--last-name", "test last name ",
                "--company", "fixture business",
                "--line1", "123 test address example city ",
                "--postcode", "12345",
                "--phone", "2025550133",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        mock_fn.assert_called_once_with(
            email="alex+zztest@example.com",
            first_name="Fixture Capture",
            last_name="test last name ",
            company="fixture business",
            line1="123 test address example city ",
            postcode="12345",
            phone="2025550133",
            state="",
        )


class TestAgentsCLI:
    def test_agents_list_runs_and_emits_json(self, runner):
        mock_agents = [{"id": 1, "first_name": "Tech A", "last_name": "Example"}]
        with patch("cli_anything.propertymeld.http_backend.list_agents",
                   return_value=mock_agents):
            result = runner.invoke(cli, ["agents", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["first_name"] == "Tech A"

    def test_agents_search_filters_by_name(self, runner):
        mock_agents = [
            {"id": 1, "first_name": "Tech A", "last_name": "Example"},
            {"id": 2, "first_name": "Tech B", "last_name": "Lee"},
            {"id": 3, "first_name": "Tech C", "last_name": "Rossi"},
        ]
        with patch("cli_anything.propertymeld.http_backend.list_agents",
                   return_value=mock_agents):
            result = runner.invoke(cli, ["agents", "search", "tech a"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["first_name"] == "Tech A"

    def test_agents_get_returns_full_record(self, runner):
        mock_agent = {
            "id": 5012,
            "first_name": "Tech A",
            "last_name": "Example",
            "role": "MAINTENANCE",
            "contact": None,
            "user": {"email": "tech-d@example.com"},
        }
        with patch("cli_anything.propertymeld.http_backend.get_management_agent",
                   return_value=mock_agent) as mock_fn:
            result = runner.invoke(cli, ["agents", "get", "5012"])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(5012)
        data = json.loads(result.output)
        assert data["id"] == 5012
        assert data["first_name"] == "Tech A"
        # contact-None case (no contact record exists) surfaces as-is — operator
        # sees the PM data shape rather than silently missing the gap.
        assert data["contact"] is None
        assert data["user"]["email"] == "tech-d@example.com"

    def test_agents_get_requires_int_id(self, runner):
        # agent_id is typed int — click rejects non-integer arg with exit_code 2
        result = runner.invoke(cli, ["agents", "get", "not-an-int"])
        assert result.exit_code == 2
        assert "Invalid value" in result.output or "Error" in result.output


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
    {"id": 1, "first_name": "Fixture", "last_name": "Alpha", "email": "fixture.alpha@example.com"},
    {"id": 2, "first_name": "Fixture", "last_name": "Beta", "email": "fixture.beta@example.com"},
]


class TestWorkOrdersFilesCLI:
    def test_files_outputs_list(self, runner):
        with patch("cli_anything.propertymeld.http_backend.list_files",
                   return_value=MOCK_FILES) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "files", "90000001"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["filename"] == "before.jpg"
        mock_fn.assert_called_once_with("90000001")


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
            result = http_backend.list_files("90000001")
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
            result = http_backend.list_files("90000001")
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
            result = http_backend.list_files("90000001")
        assert len(result) == 1
        assert result[0]["uploader_role"] == "tenant"


class TestWorkOrdersWorkEntriesCLI:
    def test_work_entries_outputs_list(self, runner):
        mock_entries = [
            {"id": 1, "checkin": "2026-05-01T08:00:00Z", "checkout": "2026-05-01T10:30:00Z",
             "hours": 2.5, "agent_name": "Tech A", "description": "AC tune-up",
             "long_description": "Replaced filter, cleaned coils."},
        ]
        with patch("cli_anything.propertymeld.http_backend.list_work_entries",
                   return_value=mock_entries) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "work-entries", "list", "90000001"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["agent_name"] == "Tech A"
        mock_fn.assert_called_once_with("90000001")


class TestAssignTechCLI:
    def test_assign_tech_canonical_and_alias_stdout_match(self, runner):
        payload = {"ok": True, "agent_id": 5012, "matched_name": "Tech A"}
        with patch("cli_anything.propertymeld.http_backend.assign_tech",
                   return_value=payload) as mock_fn:
            canonical = runner.invoke(cli, ["work-orders", "assign-tech",
                                            "--work-order-id", "90000001",
                                            "--tech", "tech-d"])
            alias = runner.invoke(cli, ["assign-tech",
                                        "--work-order-id", "90000001",
                                        "--tech", "tech-d"])
        assert canonical.exit_code == 0
        assert alias.exit_code == 0
        assert canonical.stdout == alias.stdout
        assert "deprecated" not in canonical.stdout
        assert "deprecated" not in alias.stdout
        assert canonical.stderr == ""
        assert alias.stderr == "note: 'pm assign-tech' is deprecated; use 'pm work-orders assign-tech'\n"
        assert json.loads(canonical.stdout)["ok"] is True
        assert mock_fn.call_count == 2
        mock_fn.assert_any_call("90000001", "tech-d")


class TestAssignVendorCLI:
    def test_assign_vendor_passes_partial_name(self, runner):
        with patch("cli_anything.propertymeld.http_backend.assign_vendor_by_name",
                   return_value={"ok": True, "vendor_id": 10, "matched_name": "Fixture Service"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "assign-vendor",
                                         "--work-order-id", "90000001",
                                         "--vendor", "dyer"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        mock_fn.assert_called_once_with("90000001", "dyer", account_prefix="1")

    def test_assign_vendor_canonical_and_alias_stdout_match(self, runner):
        payload = {"ok": True, "vendor_id": 10, "matched_name": "Fixture Service"}
        with patch("cli_anything.propertymeld.http_backend.assign_vendor_by_name",
                   return_value=payload) as mock_fn:
            canonical = runner.invoke(cli, ["work-orders", "assign-vendor",
                                            "--work-order-id", "90000001",
                                            "--vendor", "dyer",
                                            "--account", "2"])
            alias = runner.invoke(cli, ["assign-vendor",
                                        "--work-order-id", "90000001",
                                        "--vendor", "dyer",
                                        "--account", "2"])
        assert canonical.exit_code == 0
        assert alias.exit_code == 0
        assert canonical.stdout == alias.stdout
        assert "deprecated" not in canonical.stdout
        assert "deprecated" not in alias.stdout
        assert canonical.stderr == ""
        assert alias.stderr == "note: 'pm assign-vendor' is deprecated; use 'pm work-orders assign-vendor'\n"
        assert json.loads(canonical.stdout)["ok"] is True
        assert mock_fn.call_count == 2
        mock_fn.assert_any_call("90000001", "dyer", account_prefix="2")


class TestWorkOrdersScheduleCLI:
    def test_schedule_passes_dtstart_and_hours(self, runner):
        with patch("cli_anything.propertymeld.http_backend.schedule_appointment",
                   return_value={"id": 4242, "scheduled_dtstart": "2026-04-27T14:00:00-04:00"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "schedule",
                                         "--meld-id", "90000001",
                                         "--dtstart", "2026-04-27T14:00:00-04:00",
                                         "--hours", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 4242
        mock_fn.assert_called_once_with("90000001",
                                        "2026-04-27T14:00:00-04:00",
                                        duration_hours=3.0)


class TestWorkOrdersForcePendingCompletionCLI:
    def test_force_pending_completion_accepts_positional_id(self, runner):
        with patch("cli_anything.propertymeld.http_backend.force_pending_completion",
                   return_value={"ok": True, "meld_id": 90000001,
                                 "status": "PENDING_COMPLETION"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "force-pending-completion",
                                         "90000001"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "PENDING_COMPLETION"
        mock_fn.assert_called_once_with("90000001")

    def test_force_pending_completion_accepts_meld_id_option(self, runner):
        with patch("cli_anything.propertymeld.http_backend.force_pending_completion",
                   return_value={"ok": True, "meld_id": 90000001,
                                 "status": "PENDING_COMPLETION"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "force-pending-completion",
                                         "--meld-id", "90000001"])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with("90000001")

    def test_force_pending_completion_rejects_conflicting_ids(self, runner):
        with patch("cli_anything.propertymeld.http_backend.force_pending_completion") as mock_fn:
            result = runner.invoke(cli, ["work-orders", "force-pending-completion",
                                         "90000001", "--meld-id", "90000002"])
        assert result.exit_code != 0
        assert "either positional meld_id or --meld-id" in result.output
        mock_fn.assert_not_called()


class TestWorkOrdersLifecycleCLI:
    def test_clone_supports_all_override_flags(self, runner):
        with patch("cli_anything.propertymeld.http_backend.clone_meld",
                   return_value={"ok": True, "new_meld_id": 777}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "clone",
                                         "--meld-id", "90000001",
                                         "--description", "Reset toilet",
                                         "--long-description", "Replace wax ring + punch list",
                                         "--no-tenant-presence-required",
                                         "--unit-id", "9000007",
                                         "--priority", "low"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["new_meld_id"] == 777
        mock_fn.assert_called_once_with(
            "90000001",
            brief_description="Reset toilet",
            description="Replace wax ring + punch list",
            tenant_presence_required=False,
            unit_id=9000007,
            priority="LOW",
            coordinator_id=None,
        )

    def test_clone_priority_medium_token_is_accepted(self, runner):
        """Regression: PM's real enum is MEDIUM (not MED). --priority medium
        must normalize to 'MEDIUM'; the old 'med' token is no longer a valid
        Click choice."""
        with patch("cli_anything.propertymeld.http_backend.clone_meld",
                   return_value={"ok": True, "new_meld_id": 778}) as mock_fn:
            ok = runner.invoke(cli, ["work-orders", "clone",
                                     "--meld-id", "90000001",
                                     "--priority", "medium"])
            bad = runner.invoke(cli, ["work-orders", "clone",
                                      "--meld-id", "90000001",
                                      "--priority", "med"])
        assert ok.exit_code == 0
        assert mock_fn.call_args.kwargs["priority"] == "MEDIUM"
        # 'med' is no longer a valid choice — Click rejects before the backend.
        assert bad.exit_code != 0
        assert "med" in bad.output.lower()

    def test_clone_coordinator_id_override_passes_through(self, runner):
        with patch("cli_anything.propertymeld.http_backend.clone_meld",
                   return_value={"ok": True, "new_meld_id": 779,
                                 "coordinator_id": 9999}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "clone",
                                         "--meld-id", "90000001",
                                         "--coordinator-id", "9999"])
        assert result.exit_code == 0
        assert mock_fn.call_args.kwargs["coordinator_id"] == 9999

    def test_set_coordinator_cmd_calls_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.set_coordinator",
                   return_value={"ok": True, "meld_id": 90000001,
                                 "coordinator_id": 5011}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "set-coordinator",
                                         "--meld-id", "90000001",
                                         "--user-id", "5011"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["coordinator_id"] == 5011
        mock_fn.assert_called_once_with("90000001", 5011)

    def test_merge_into_destination_legacy_flags(self, runner):
        """Backwards-compat path: --meld-id + --into are mapped to the captured
        web-UI shape (--destination + --source) under the hood."""
        with patch("cli_anything.propertymeld.http_backend.merge_meld",
                   return_value={"ok": True, "destination_meld_id": 90000002,
                                 "source_meld_ids": [90000001]}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "merge",
                                         "--meld-id", "90000001",
                                         "--into", "90000002"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["destination_meld_id"] == 90000002
        # Legacy --meld-id=source + --into=destination maps to
        # http_backend.merge_meld(destination_id, [source_id]).
        mock_fn.assert_called_once_with("90000002", ["90000001"])

    def test_merge_with_new_destination_source_flags(self, runner):
        """Native captured-shape path: --destination + --source(s)."""
        with patch("cli_anything.propertymeld.http_backend.merge_meld",
                   return_value={"ok": True, "destination_meld_id": 90000013,
                                 "source_meld_ids": [90000014, 90000015]}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "merge",
                                         "--destination", "90000013",
                                         "--source", "90000014",
                                         "--source", "90000015"])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with("90000013", ["90000014", "90000015"])

    def test_complete_with_notes_refuses_without_network_and_returns_context(self, runner):
        """The CLI must not imply persistence after manager completion was disabled."""
        fail_creds = lambda: pytest.fail("manager refusal must precede credentials")
        fail_network = lambda *a, **k: pytest.fail("manager refusal must precede network")
        with patch("cli_anything.propertymeld.http_backend._load_creds", fail_creds), \
             patch("cli_anything.propertymeld.http_backend._http_get", fail_network), \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token", fail_network), \
             patch("cli_anything.propertymeld.http_backend._http_patch", fail_network):
            result = runner.invoke(cli, [
                "work-orders", "complete",
                "--meld-id", "90000001",
                "--notes", "Replaced filter.",
            ])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] is None
        assert data["completion_notes"] == "Replaced filter."
        assert "manager-side completion is disabled" in data["error"]
        assert "in-house work through the tech-app checkout" in data["error"]
        assert "vendor work through the vendor-side path" in data["error"]
        assert "other manager work through the Property Meld web UI" in data["error"]

    def test_complete_help_says_notes_are_not_persisted(self, runner):
        result = runner.invoke(cli, ["work-orders", "complete", "--help"])
        assert result.exit_code == 0
        help_text = " ".join(result.output.split())
        assert "Refuse manager completion" in help_text
        assert "tech-app checkout for in-house work" in help_text
        assert "vendor-side completion for vendor work" in help_text
        assert "Property Meld web UI for other manager work" in help_text
        assert "not persisted" in help_text
        assert "Mark a meld complete" not in help_text

    def test_cancel_with_reason(self, runner):
        with patch("cli_anything.propertymeld.http_backend.cancel_meld",
                   return_value={"id": 1002, "status": "CANCELLED"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "cancel",
                                         "--meld-id", "90000001",
                                         "--reason", "Duplicate"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "CANCELLED"
        mock_fn.assert_called_once_with("90000001", reason="Duplicate")

    def test_cancel_requires_reason(self, runner):
        # PM rejects a reasonless cancel with HTTP 400
        # ("Cancellation reason must be provided"), verified live 2026-06-02.
        # The CLI must enforce --reason and never fire the no-reason request.
        with patch("cli_anything.propertymeld.http_backend.cancel_meld") as mock_fn:
            result = runner.invoke(cli, ["work-orders", "cancel",
                                         "--meld-id", "90000001"])
        assert result.exit_code != 0
        assert "--reason" in result.output
        mock_fn.assert_not_called()

    def test_cancel_rejects_empty_reason(self, runner):
        # required=True enforces PRESENCE, not non-emptiness: --reason ""
        # would otherwise fire a blank reason and hit the same 400.
        # The _require_nonempty callback must block it before any request.
        with patch("cli_anything.propertymeld.http_backend.cancel_meld") as mock_fn:
            result = runner.invoke(cli, ["work-orders", "cancel",
                                         "--meld-id", "90000001",
                                         "--reason", ""])
        assert result.exit_code != 0
        assert "reason cannot be empty" in result.output
        mock_fn.assert_not_called()

    def test_cancel_rejects_whitespace_reason(self, runner):
        with patch("cli_anything.propertymeld.http_backend.cancel_meld") as mock_fn:
            result = runner.invoke(cli, ["work-orders", "cancel",
                                         "--meld-id", "90000001",
                                         "--reason", "   "])
        assert result.exit_code != 0
        assert "reason cannot be empty" in result.output
        mock_fn.assert_not_called()


class TestTenantsCLI:
    def test_list_with_search(self, runner):
        with patch("cli_anything.propertymeld.http_backend.list_tenants",
                   return_value=MOCK_TENANTS) as mock_fn:
            result = runner.invoke(cli, ["tenants", "list", "--search", "alpha"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        mock_fn.assert_called_once_with(search="alpha", limit=100)

    def test_get_single_tenant(self, runner):
        with patch("cli_anything.propertymeld.http_backend.get_tenant",
                   return_value=MOCK_TENANTS[0]):
            result = runner.invoke(cli, ["tenants", "get", "1"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["first_name"] == "Fixture"

    def test_invite_passes_args_to_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.invite_tenant",
                   return_value={"ok": True, "tenant_id": 9000023, "invited": True}) as mock_fn:
            result = runner.invoke(cli, [
                "tenants", "invite",
                "--unit-id", "9000007",
                "--first-name", "Alex",
                "--last-name", "Example",
                "--email", "alex@example.com",
                "--cell", "2025550128",
                "--home", "2025550130",
                "--secondary-email", "alt@example.com",
                "--notes", "notes section",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["tenant_id"] == 9000023
        mock_fn.assert_called_once_with(
            unit_id=9000007,
            first_name="Alex",
            last_name="Example",
            email="alex@example.com",
            cell_phone="2025550128",
            home_phone="2025550130",
            secondary_email="alt@example.com",
            notes="notes section",
            should_invite=True,
        )

    def test_invite_no_invite_flag(self, runner):
        with patch("cli_anything.propertymeld.http_backend.invite_tenant",
                   return_value={"ok": True, "tenant_id": 9000023, "should_invite": False}) as mock_fn:
            result = runner.invoke(cli, [
                "tenants", "invite",
                "--unit-id", "9000007",
                "--first-name", "Alex",
                "--last-name", "Example",
                "--email", "alex@example.com",
                "--cell", "2025550128",
                "--no-invite",
            ])
        assert result.exit_code == 0, result.output
        assert mock_fn.call_args.kwargs["should_invite"] is False

    def test_invite_requires_core_fields(self, runner):
        result = runner.invoke(cli, ["tenants", "invite", "--unit-id", "9000007"])
        assert result.exit_code != 0
        assert "Missing option" in result.output


# Synthetic fixture covers the /api/tenants/ shape: flat email and phone at
# the top level, with no nested contact/user.
_TENANT_FIXTURE = [
    {"id": 9000016, "first_name": "Fixture", "last_name": "Alpha",
     "email": "resident.alpha@example.com", "phone": "(202) 555-0123"},
    {"id": 9000017, "first_name": "Fixture", "last_name": "Beta",
     "email": "resident.beta@example.com", "phone": "+1 (202) 555-0100"},
    {"id": 9000018, "first_name": "Fixture", "last_name": "Gamma",
     "email": "resident.gamma@example.com", "phone": "(202) 555-0122"},
    {"id": 9000019, "first_name": "Fixture", "last_name": "Delta",
     "email": "resident.delta@example.com", "phone": None},
]


class TestFilterTenants:
    """Direct unit tests on the search predicate (no HTTP layer involved)."""

    def test_single_word_lastname_match(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        out = _filter_tenants(_TENANT_FIXTURE, "Alpha")
        assert [t["id"] for t in out] == [9000016]

    def test_multi_word_name_match(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        out = _filter_tenants(_TENANT_FIXTURE, "Fixture Alpha")
        assert [t["id"] for t in out] == [9000016]

    def test_multi_word_name_match_case_insensitive(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        out = _filter_tenants(_TENANT_FIXTURE, "fixture beta")
        assert [t["id"] for t in out] == [9000017]

    def test_email_substring_match(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        out = _filter_tenants(_TENANT_FIXTURE, "resident.alpha")
        assert [t["id"] for t in out] == [9000016]

    def test_phone_formatted_needle_matches_formatted_stored(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        out = _filter_tenants(_TENANT_FIXTURE, "(202) 555-0123")
        assert [t["id"] for t in out] == [9000016]

    def test_phone_digits_only_needle_matches_formatted_stored(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        out = _filter_tenants(_TENANT_FIXTURE, "2025550123")
        assert [t["id"] for t in out] == [9000016]

    def test_phone_dashed_needle_matches_formatted_stored(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        out = _filter_tenants(_TENANT_FIXTURE, "202-555-0123")
        assert [t["id"] for t in out] == [9000016]

    def test_phone_partial_digits_above_floor_matches(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        # 7+ digit tail of the stored phone — should still hit.
        out = _filter_tenants(_TENANT_FIXTURE, "5550123")
        assert [t["id"] for t in out] == [9000016]

    def test_phone_below_digit_floor_does_not_match(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        # "913" is only 3 digits — below the 4-digit floor, so the phone
        # branch is skipped. "913" doesn't appear in any name/email, so
        # the result must be empty (NOT a spurious phone hit).
        out = _filter_tenants(_TENANT_FIXTURE, "913")
        assert out == []

    def test_empty_search_returns_all(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        out = _filter_tenants(_TENANT_FIXTURE, "")
        assert len(out) == len(_TENANT_FIXTURE)

    def test_no_match_returns_empty(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        out = _filter_tenants(_TENANT_FIXTURE, "ZzNoSuchPerson")
        assert out == []

    def test_phone_match_tolerates_country_code_and_punctuation(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        # Stored: "+1 (202) 555-0100" → digits "12025550100"
        # Needle digits "2025550100" should be a substring → match.
        out = _filter_tenants(_TENANT_FIXTURE, "(202) 555-0100")
        assert [t["id"] for t in out] == [9000017]

    def test_name_query_does_not_accidentally_phone_match(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        # "Gamma" has no digits → phone branch skipped → only name/email
        # matching applies. Verifies the no-digits short-circuit.
        out = _filter_tenants(_TENANT_FIXTURE, "Gamma")
        assert [t["id"] for t in out] == [9000018]

    def test_multi_word_match_tolerates_trailing_whitespace(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        # A trailing-space first_name must still match its normalized form.
        # The combined-name match must collapse
        # internal whitespace so "Fixture Beta" still hits.
        fixture = [
            {"id": 9000011, "first_name": "Fixture ", "last_name": "Beta",
             "email": "resident.beta@example.com", "phone": None},
        ]
        out = _filter_tenants(fixture, "Fixture Beta")
        assert [t["id"] for t in out] == [9000011]

    def test_phone_none_tenant_is_safe(self):
        from cli_anything.propertymeld.http_backend import _filter_tenants
        # Tenant 9000019 has phone=None. Should not crash, should not
        # match a phone needle.
        out = _filter_tenants(_TENANT_FIXTURE, "5550122")
        assert [t["id"] for t in out] == [9000018]


class TestListTenantsIntegratesFilter:
    """Smoke-tests that list_tenants delegates to _filter_tenants when search is set."""

    def test_search_path_paginates_and_filters(self):
        from cli_anything.propertymeld import http_backend
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value=""), \
             patch("cli_anything.propertymeld.http_backend._paginate_all",
                   return_value=list(_TENANT_FIXTURE)) as mock_paginate:
            out = http_backend.list_tenants(search="Fixture Beta", limit=10)
        mock_paginate.assert_called_once()
        assert [t["id"] for t in out] == [9000017]


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
                                         "--meld-id", "90000001",
                                         "--estimate-number", "INV-2026-001",
                                         "--amount", "1250.00"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["estimate_number"] == "INV-2026-001"
        # Positional + kwarg shape per cli wiring
        call = mock_fn.call_args
        assert call.args[0] == "90000001"
        assert call.args[1] == "INV-2026-001"
        assert call.args[2] == "1250.00"


class TestReceiptsCLI:
    def test_upload_receipt_passes_file_path(self, runner, tmp_path):
        receipt_file = tmp_path / "rcpt.pdf"
        receipt_file.write_bytes(b"%PDF-1.4 fake\n")
        with patch("cli_anything.propertymeld.http_backend.upload_receipt",
                   return_value=MOCK_RECEIPT) as mock_fn:
            result = runner.invoke(cli, ["receipts", "upload",
                                         "--meld-id", "90000001",
                                         "--file", str(receipt_file)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 9001
        mock_fn.assert_called_once()
        call = mock_fn.call_args
        assert call.args[0] == "90000001"
        assert call.args[1] == str(receipt_file)


class TestWorkOrdersScheduleVendorCLI:
    def test_schedule_vendor_passes_args(self, runner):
        with patch("cli_anything.propertymeld.http_backend.schedule_vendor_appointment",
                   return_value={"id": 5555, "scheduled_dtstart": "2026-05-06T14:00:00-04:00"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "schedule-vendor",
                                         "--meld-id", "90000001",
                                         "--vendor-id", "10",
                                         "--dtstart", "2026-05-06T14:00:00-04:00",
                                         "--hours", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 5555
        mock_fn.assert_called_once_with("90000001", "10",
                                        "2026-05-06T14:00:00-04:00",
                                        duration_hours=3.0)


# ──────────────────────────────────────────────────────────────────────────────
# Projects create / edit / detach-meld + work-orders update-notes (PR cli-wire)
# ──────────────────────────────────────────────────────────────────────────────


class TestProjectsCreateEditCLI:
    def test_create_passes_args_shape(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_project",
                   return_value={"ok": True, "project_id": 900008, "result": {"id": 900008}}) as mock_fn:
            result = runner.invoke(cli, [
                "projects", "create",
                "--name", "Test",
                "--project-type", "TURN",
                "--due-date", "2026-05-30T04:00:00.000Z",
                "--start-date", "2026-05-14T10:30:00Z",
                "--coordinator", "5011",
                "--unit-id", "9000007",
                "--unit-label", "123 Main St",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["project_id"] == 900008
        kw = mock_fn.call_args.kwargs
        assert kw["name"] == "Test"
        assert kw["project_type"] == "TURN"
        assert kw["coordinators"] == [5011]
        assert kw["unit"] == {"id": 9000007, "label": "123 Main St"}
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
                "--coordinator", "5011",
                "--unit-id", "9000007", "--unit-label", "L",
                "--prop-id", "9999",
            ])
        kw = mock_fn.call_args.kwargs
        assert kw["prop"] == {"id": 9999}

    def test_edit_passes_only_set_fields_plus_unit_dict(self, runner):
        with patch("cli_anything.propertymeld.http_backend.update_project",
                   return_value={"ok": True, "project_id": "900008", "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "projects", "edit", "900008",
                "--name", "Renamed",
                "--description", "new desc",
                "--unit-id", "9000007", "--unit-label", "123 Main",
            ])
        assert result.exit_code == 0
        kw = mock_fn.call_args.kwargs
        assert kw["project_id"] == "900008"
        assert kw["name"] == "Renamed"
        assert kw["description"] == "new desc"
        assert kw["unit"] == {"id": 9000007, "label": "123 Main"}
        # Unset fields should be None so the backend's fetch+merge can echo them.
        assert kw["project_type"] is None
        assert kw["due_date"] is None
        assert kw["start_date"] is None
        assert kw["coordinators"] is None


class TestProjectsDetachMeldCLI:
    def test_detach_meld_calls_patch_link_with_none(self, runner):
        with patch("cli_anything.propertymeld.http_backend.patch_meld_project_link",
                   return_value={"ok": True, "meld_id": 90000008, "project_id": None, "result": {}}) as mock_fn:
            result = runner.invoke(cli, ["projects", "detach-meld", "90000008"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["project_id"] is None
        call = mock_fn.call_args
        assert call.args[0] == "90000008"
        assert call.args[1] is None


class TestWorkOrdersUpdateNotesCLI:
    def test_update_notes_passes_text(self, runner):
        with patch("cli_anything.propertymeld.http_backend.update_meld_notes",
                   return_value={"ok": True, "meld_id": 90000008, "result": {"maintenance_notes": "hi"}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "update-notes", "90000008",
                "--maintenance", "hi",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["result"]["maintenance_notes"] == "hi"
        call = mock_fn.call_args
        assert call.args[0] == "90000008"
        assert call.args[1] == "hi"


class TestProjectsCreateMeldInCLI:
    """pm-dev projects create-meld-in — ergonomic --unit-id + --maintenance-id flags."""

    _COMMON_ARGS = [
        "projects", "create-meld-in", "900005",
        "--brief-description", "b",
        "--description", "d",
        "--work-category", "APPLIANCES",
        "--work-type", "TURN",
        "--due-date", "2026-05-16T00:00:00.000Z",
        "--work-location", "Kitchen",
    ]

    def test_id_flags_pass_stripped_objects(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project",
                   return_value={"ok": True, "meld_id": 99, "project_id": "900005", "result": {}}) as mock_fn:
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
                "--tenant-id", "9000014",
            ])
        assert result.exit_code == 0, result.output
        call = mock_fn.call_args
        assert call.kwargs["unit"] == {"id": 9000007}
        assert call.kwargs["maintenance"] == [{"id": 5011}]
        assert call.kwargs["tenants"] == [{"id": 9000014}]

    def test_multiple_maintenance_ids_repeatable(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project",
                   return_value={"ok": True, "meld_id": 99, "project_id": "900005", "result": {}}) as mock_fn:
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
                "--maintenance-id", "5013",
                "--tenant-id", "9000014",
                "--tenant-id", "9000015",
            ])
        assert result.exit_code == 0, result.output
        assert mock_fn.call_args.kwargs["maintenance"] == [{"id": 5011}, {"id": 5013}]
        assert mock_fn.call_args.kwargs["tenants"] == [{"id": 9000014}, {"id": 9000015}]

    def test_json_flags_still_work_for_power_users(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project",
                   return_value={"ok": True, "meld_id": 99, "project_id": "900005", "result": {}}) as mock_fn:
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-json", '{"id": 1, "display_address": {}, "prop": {}, "current_tenants": []}',
                "--maintenance-json", '[{"id": 9}]',
                "--tenants-json", '[{"id": 99, "contact": {}, "default_language": "en", "notification_settings": {}}]',
            ])
        assert result.exit_code == 0, result.output
        kwargs = mock_fn.call_args.kwargs
        assert kwargs["unit"]["id"] == 1
        assert kwargs["maintenance"] == [{"id": 9}]
        assert kwargs["tenants"][0]["id"] == 99

    def test_missing_both_unit_flags_errors(self, runner):
        result = runner.invoke(cli, self._COMMON_ARGS + [
            "--maintenance-id", "5011",
        ])
        assert result.exit_code != 0
        assert "--unit-id" in result.output and "--unit-json" in result.output

    def test_passing_both_unit_flags_errors(self, runner):
        result = runner.invoke(cli, self._COMMON_ARGS + [
            "--unit-id", "9000007",
            "--unit-json", '{"id": 1}',
            "--maintenance-id", "5011",
        ])
        assert result.exit_code != 0
        assert "--unit-id" in result.output and "--unit-json" in result.output

    def test_missing_both_maintenance_flags_errors(self, runner):
        result = runner.invoke(cli, self._COMMON_ARGS + [
            "--unit-id", "9000007",
        ])
        assert result.exit_code != 0
        assert "--maintenance-id" in result.output and "--maintenance-json" in result.output

    def test_tenants_optional_defaults_empty_list(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project",
                   return_value={"ok": True, "meld_id": 99, "project_id": "900005", "result": {}}) as mock_fn:
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
            ])
        assert result.exit_code == 0, result.output
        assert mock_fn.call_args.kwargs["tenants"] == []

    def test_passing_both_tenant_id_and_tenants_json_errors(self, runner):
        result = runner.invoke(cli, self._COMMON_ARGS + [
            "--unit-id", "9000007",
            "--maintenance-id", "5011",
            "--tenant-id", "9000014",
            "--tenants-json", '[{"id": 99}]',
        ])
        assert result.exit_code != 0
        assert "--tenant-id" in result.output and "--tenants-json" in result.output

    def test_empty_tenants_json_still_parses_and_errors(self, runner):
        result = runner.invoke(cli, self._COMMON_ARGS + [
            "--unit-id", "9000007",
            "--maintenance-id", "5011",
            "--tenants-json", "",
        ])
        assert result.exit_code != 0
        assert result.exception is not None
        assert "Expecting value" in str(result.exception)

    def test_requires_work_location(self, runner):
        # PM requires work_location (400 "Work Location is required.",
        # verified live 2026-06-02). _COMMON_ARGS-minus-work-location must
        # be rejected by Click before any backend call fires.
        args = [a for a in self._COMMON_ARGS if a not in ("--work-location", "Kitchen")]
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project") as mock_fn:
            result = runner.invoke(cli, args + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
            ])
        assert result.exit_code != 0
        assert "--work-location" in result.output
        mock_fn.assert_not_called()

    def test_rejects_empty_work_location(self, runner):
        # required=True allows --work-location "" through; the _require_nonempty
        # callback must block the blank value before the backend call fires.
        args = [a if a != "Kitchen" else "" for a in self._COMMON_ARGS]
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project") as mock_fn:
            result = runner.invoke(cli, args + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
            ])
        assert result.exit_code != 0
        assert "work-location cannot be empty" in result.output
        mock_fn.assert_not_called()

    def test_rejects_whitespace_work_location(self, runner):
        args = [a if a != "Kitchen" else "   " for a in self._COMMON_ARGS]
        with patch("cli_anything.propertymeld.http_backend.create_meld_in_project") as mock_fn:
            result = runner.invoke(cli, args + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
            ])
        assert result.exit_code != 0
        assert "work-location cannot be empty" in result.output
        mock_fn.assert_not_called()


class TestWorkOrdersCreateCLI:
    """pm work-orders create — standalone meld creation."""

    _COMMON_ARGS = [
        "work-orders", "create",
        "--brief-description", "b",
        "--description", "d",
        "--work-category", "APPLIANCES",
        "--work-type", "TURN",
        "--due-date", "2026-05-16T00:00:00.000Z",
        "--work-location", "Kitchen",
    ]

    def test_work_orders_create_runs_with_ergonomic_flags(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld",
                   return_value={"ok": True, "meld_id": 90000007, "result": {}}) as mock_fn:
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
                "--tenant-id", "9000014",
            ])
        assert result.exit_code == 0, result.output
        kwargs = mock_fn.call_args.kwargs
        assert kwargs["unit"] == {"id": 9000007}
        assert kwargs["maintenance"] == [{"id": 5011}]
        assert kwargs["tenants"] == [{"id": 9000014}]

    def test_work_orders_create_allows_unassigned_pending_assignment(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld",
                   return_value={
                       "ok": True,
                       "meld_id": 90000007,
                       "result": {
                           "id": 90000007,
                           "status": "PENDING_ASSIGNMENT",
                           "maintenance": [],
                       },
                   }) as mock_fn:
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-id", "9000007",
            ])
        assert result.exit_code == 0, result.output
        kwargs = mock_fn.call_args.kwargs
        assert kwargs["unit"] == {"id": 9000007}
        assert kwargs["maintenance"] == []
        data = json.loads(result.output)
        assert data["result"]["status"] == "PENDING_ASSIGNMENT"
        assert data["result"]["maintenance"] == []

    def test_work_orders_create_emits_meld_id(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_meld",
                   return_value={"ok": True, "meld_id": 90000007, "result": {"id": 90000007}}):
            result = runner.invoke(cli, self._COMMON_ARGS + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["meld_id"] == 90000007

    def test_requires_work_location(self, runner):
        # PM requires work_location (400 "Work Location is required.",
        # verified live 2026-06-02). Click must reject before create_meld fires.
        args = [a for a in self._COMMON_ARGS if a not in ("--work-location", "Kitchen")]
        with patch("cli_anything.propertymeld.http_backend.create_meld") as mock_fn:
            result = runner.invoke(cli, args + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
            ])
        assert result.exit_code != 0
        assert "--work-location" in result.output
        mock_fn.assert_not_called()

    def test_rejects_empty_work_location(self, runner):
        # required=True allows --work-location "" through; the _require_nonempty
        # callback must block the blank value before create_meld fires.
        args = [a if a != "Kitchen" else "" for a in self._COMMON_ARGS]
        with patch("cli_anything.propertymeld.http_backend.create_meld") as mock_fn:
            result = runner.invoke(cli, args + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
            ])
        assert result.exit_code != 0
        assert "work-location cannot be empty" in result.output
        mock_fn.assert_not_called()

    def test_rejects_whitespace_work_location(self, runner):
        args = [a if a != "Kitchen" else "   " for a in self._COMMON_ARGS]
        with patch("cli_anything.propertymeld.http_backend.create_meld") as mock_fn:
            result = runner.invoke(cli, args + [
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
            ])
        assert result.exit_code != 0
        assert "work-location cannot be empty" in result.output
        mock_fn.assert_not_called()


class TestWorkOrdersLinkTenantCLI:
    """pm work-orders link-tenant <meld_id> <tenant_id> — closes P1 #14."""

    def test_link_tenant_passes_meld_and_tenant_ids(self, runner):
        with patch("cli_anything.propertymeld.http_backend.link_tenant_to_meld",
                   return_value={"ok": True, "meld_id": "90000011", "tenant_id": 9000014,
                                 "linked": True, "tenant_count": 2, "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "link-tenant", "90000011", "9000014",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["linked"] is True
        assert data["tenant_id"] == 9000014
        mock_fn.assert_called_once_with("90000011", 9000014)

    def test_link_tenant_surfaces_already_linked(self, runner):
        with patch("cli_anything.propertymeld.http_backend.link_tenant_to_meld",
                   return_value={"ok": True, "meld_id": "90000011", "tenant_id": 9000014,
                                 "already_linked": True, "tenant_count": 1}):
            result = runner.invoke(cli, [
                "work-orders", "link-tenant", "90000011", "9000014",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["already_linked"] is True

    def test_link_tenant_requires_tenant_id_int(self, runner):
        # Click rejects non-int tenant_id at parse time
        result = runner.invoke(cli, [
            "work-orders", "link-tenant", "90000011", "not-a-number",
        ])
        assert result.exit_code != 0
        assert "Invalid value" in result.output or "not-a-number" in result.output


class TestP2GapCLICommands:
    def test_invoice_hold_calls_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.hold_meld_invoice",
                   return_value={"ok": True, "invoice_id": 9000012, "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "invoice-hold", "9000012",
                "--reason", "needs revision",
            ])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(9000012, reason="needs revision")

    def test_invoice_decline_calls_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.decline_meld_invoice",
                   return_value={"ok": True, "invoice_id": 9000012, "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "invoice-decline", "9000012",
                "--reason", "wrong job",
            ])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(9000012, reason="wrong job")

    def test_delete_file_calls_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.delete_meld_file",
                   return_value={"ok": True, "file_id": 90000020, "deleted": True}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "delete-file", "90000020", "--force"])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(90000020)

    def test_delete_file_without_force_in_no_tty_aborts(self, runner):
        with patch("cli_anything.propertymeld.http_backend.delete_meld_file") as mock_fn:
            result = runner.invoke(cli, ["work-orders", "delete-file", "90000020"])
        assert result.exit_code != 0
        assert "requires --force" in result.output
        mock_fn.assert_not_called()

    def test_delete_project_calls_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.delete_project",
                   return_value={"ok": True, "project_id": 900007, "deleted": True}) as mock_fn:
            result = runner.invoke(cli, ["projects", "delete", "900007", "--force"])
        assert result.exit_code == 0
        mock_fn.assert_called_once_with(900007)

    def test_delete_project_without_force_in_no_tty_aborts(self, runner):
        with patch("cli_anything.propertymeld.http_backend.delete_project") as mock_fn:
            result = runner.invoke(cli, ["projects", "delete", "900007"])
        assert result.exit_code != 0
        assert "requires --force" in result.output
        mock_fn.assert_not_called()


class TestUnitsEditNotesCLI:
    """pm units edit-notes <unit_id> --notes <text> — closes P3 #8 unit-level."""

    def test_passes_unit_id_and_notes_to_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.update_unit_notes",
                   return_value={"ok": True, "unit_id": 9000006,
                                 "maintenance_notes": "shut-off in basement", "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "units", "edit-notes", "9000006",
                "--notes", "shut-off in basement",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["maintenance_notes"] == "shut-off in basement"
        mock_fn.assert_called_once_with(9000006, "shut-off in basement")

    def test_requires_notes_flag(self, runner):
        result = runner.invoke(cli, ["units", "edit-notes", "9000006"])
        assert result.exit_code != 0
        assert "--notes" in result.output or "Missing option" in result.output

    def test_requires_int_unit_id(self, runner):
        result = runner.invoke(cli, [
            "units", "edit-notes", "not-a-number",
            "--notes", "x",
        ])
        assert result.exit_code != 0


class TestUnitsGetCLI:
    """pm units get <unit_id> exposes the existing direct read-only backend."""

    def test_returns_full_unit_without_rewriting_current_tenants(self, runner):
        unit = {
            "id": 9000006,
            "display_address": {"full_address": "redacted"},
            "prop": {"id": 9000002},
            "current_tenants": [
                {"id": 9000016, "is_active": True, "contact": {"cell_phone": "+12025550115"}},
            ],
        }
        with patch("cli_anything.propertymeld.http_backend.get_unit",
                   return_value=unit) as mock_fn:
            result = runner.invoke(cli, ["units", "get", "9000006", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == unit
        mock_fn.assert_called_once_with(9000006)

    def test_requires_integer_unit_id_before_backend_call(self, runner):
        with patch("cli_anything.propertymeld.http_backend.get_unit") as mock_fn:
            result = runner.invoke(cli, ["units", "get", "not-a-unit", "--json"])

        assert result.exit_code != 0
        mock_fn.assert_not_called()


class TestTenantsEditNotesCLI:
    """pm tenants edit-notes <tenant_id> --notes <text> — resident-level recallable notes."""

    def test_passes_tenant_id_and_notes_to_backend(self, runner):
        with patch("cli_anything.propertymeld.http_backend.update_tenant_notes",
                   return_value={"ok": True, "tenant_id": 9000016,
                                 "notes": "Access after 3pm",
                                 "result": {"notes": "Access after 3pm"}}) as mock_fn:
            result = runner.invoke(cli, [
                "tenants", "edit-notes", "9000016",
                "--notes", "Access after 3pm",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["notes"] == "Access after 3pm"
        assert data["tenant_id"] == 9000016
        mock_fn.assert_called_once_with(9000016, "Access after 3pm")

    def test_requires_notes_flag(self, runner):
        result = runner.invoke(cli, ["tenants", "edit-notes", "9000016"])
        assert result.exit_code != 0
        assert "--notes" in result.output or "Missing option" in result.output

    def test_requires_int_tenant_id(self, runner):
        result = runner.invoke(cli, [
            "tenants", "edit-notes", "not-a-number",
            "--notes", "x",
        ])
        assert result.exit_code != 0

    def test_accepts_empty_notes_to_clear(self, runner):
        """Clearing notes via --notes '' is a valid use (deliberate reset)."""
        with patch("cli_anything.propertymeld.http_backend.update_tenant_notes",
                   return_value={"ok": True, "tenant_id": 9000016,
                                 "notes": "", "result": {"notes": ""}}) as mock_fn:
            result = runner.invoke(cli, [
                "tenants", "edit-notes", "9000016",
                "--notes", "",
            ])
        assert result.exit_code == 0, result.output
        mock_fn.assert_called_once_with(9000016, "")


class TestTenantsEditContactCLI:
    """pm tenants edit-contact <tenant_id> — nested contact edit via full tenant PUT."""

    def test_passes_contact_fields_to_backend(self, runner):
        with patch(
            "cli_anything.propertymeld.http_backend.edit_tenant_contact",
            return_value={
                "ok": True,
                "tenant_id": 9000023,
                "contact": {
                    "primary_email": "primary@example.com",
                    "secondary_email": "secondary@example.com",
                    "cell_phone": "2025550110",
                    "home_phone": "2025550111",
                    "business_phone": "2025550112",
                },
                "updated_fields": [
                    "business_phone",
                    "cell_phone",
                    "home_phone",
                    "primary_email",
                    "secondary_email",
                ],
                "result": {},
            },
        ) as mock_fn:
            result = runner.invoke(cli, [
                "tenants", "edit-contact", "9000023",
                "--primary-email", "primary@example.com",
                "--secondary-email", "secondary@example.com",
                "--cell", "2025550110",
                "--home", "2025550111",
                "--business", "2025550112",
            ])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["tenant_id"] == 9000023
        assert data["contact"]["cell_phone"] == "2025550110"
        mock_fn.assert_called_once_with(
            9000023,
            primary_email="primary@example.com",
            secondary_email="secondary@example.com",
            cell_phone="2025550110",
            home_phone="2025550111",
            business_phone="2025550112",
        )

    def test_requires_at_least_one_contact_field(self, runner):
        result = runner.invoke(cli, ["tenants", "edit-contact", "9000023"])
        assert result.exit_code != 0
        assert "At least one" in result.output

    def test_requires_int_tenant_id(self, runner):
        result = runner.invoke(cli, [
            "tenants", "edit-contact", "not-a-number",
            "--cell", "2025550110",
        ])
        assert result.exit_code != 0

    def test_accepts_empty_string_to_clear_field(self, runner):
        with patch(
            "cli_anything.propertymeld.http_backend.edit_tenant_contact",
            return_value={
                "ok": True,
                "tenant_id": 9000023,
                "contact": {"secondary_email": ""},
                "updated_fields": ["secondary_email"],
                "result": {},
            },
        ) as mock_fn:
            result = runner.invoke(cli, [
                "tenants", "edit-contact", "9000023",
                "--secondary-email", "",
            ])

        assert result.exit_code == 0, result.output
        mock_fn.assert_called_once_with(
            9000023,
            primary_email=None,
            secondary_email="",
            cell_phone=None,
            home_phone=None,
            business_phone=None,
        )


class TestRequiredFreeTextNonEmpty:
    """Broad blank-bypass guard: `required=True` enforces PRESENCE only, so
    `--field ""` (or whitespace) sails past the required check and fires a
    blank free-text value to PM (the same 400 the field is meant to prevent).

    PR #40 closed 3 sites (cancel --reason, create --work-location x2). This
    suite proves the broad sweep that guards the remaining free-text required
    fields. Each case asserts the _require_nonempty callback BLOCKS the call
    before any backend request fires (BadParameter -> nonzero exit +
    assert_not_called). Empty AND whitespace are both blocked.

    Representative spread across commands + the invoice --reason proof case
    (cancel --reason was guarded in #40 but invoice hold/decline --reason —
    the SAME field name — was not; that inconsistency motivated this sweep).
    """

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_invoice_hold_rejects_blank_reason(self, runner, blank):
        # THE inconsistency proof: invoice --reason is the same field name as
        # the cancel --reason guarded in #40, yet was left unguarded.
        with patch("cli_anything.propertymeld.http_backend.hold_meld_invoice") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "invoice-hold", "9000012",
                "--reason", blank,
            ])
        assert result.exit_code != 0
        assert "reason cannot be empty" in result.output
        mock_fn.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_invoice_decline_rejects_blank_reason(self, runner, blank):
        with patch("cli_anything.propertymeld.http_backend.decline_meld_invoice") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "invoice-decline", "9000012",
                "--reason", blank,
            ])
        assert result.exit_code != 0
        assert "reason cannot be empty" in result.output
        mock_fn.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_send_message_rejects_blank_text(self, runner, blank):
        with patch("cli_anything.propertymeld.http_backend.send_message") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "send-message",
                "--meld-id", "90000001",
                "--text", blank,
            ])
        assert result.exit_code != 0
        assert "text cannot be empty" in result.output
        mock_fn.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_assign_tech_rejects_blank_tech(self, runner, blank):
        with patch("cli_anything.propertymeld.http_backend.assign_tech") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "assign-tech",
                "--work-order-id", "90000001",
                "--tech", blank,
            ])
        assert result.exit_code != 0
        assert "tech cannot be empty" in result.output
        mock_fn.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_vendor_invite_rejects_blank_email(self, runner, blank):
        with patch("cli_anything.propertymeld.http_backend.invite_vendor") as mock_fn:
            result = runner.invoke(cli, [
                "vendors", "invite",
                "--email", blank,
                "--first-name", "Fixture",
                "--last-name", "Test",
                "--company", "Test Co",
                "--line1", "123 Test St",
                "--postcode", "12345",
                "--phone", "2025550133",
            ])
        assert result.exit_code != 0
        assert "email cannot be empty" in result.output
        mock_fn.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_tenant_invite_rejects_blank_cell(self, runner, blank):
        with patch("cli_anything.propertymeld.http_backend.invite_tenant") as mock_fn:
            result = runner.invoke(cli, [
                "tenants", "invite",
                "--unit-id", "9000007",
                "--first-name", "Fixture",
                "--last-name", "Test",
                "--email", "zz@example.com",
                "--cell", blank,
            ])
        assert result.exit_code != 0
        assert "cell cannot be empty" in result.output
        mock_fn.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_project_create_rejects_blank_name(self, runner, blank):
        with patch("cli_anything.propertymeld.http_backend.create_project") as mock_fn:
            result = runner.invoke(cli, [
                "projects", "create",
                "--name", blank,
                "--project-type", "TURN",
                "--due-date", "2026-05-30T04:00:00.000Z",
                "--start-date", "2026-05-14T10:30:00Z",
                "--coordinator", "5011",
                "--unit-id", "9000007",
                "--unit-label", "123 Main St",
            ])
        assert result.exit_code != 0
        assert "name cannot be empty" in result.output
        mock_fn.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_estimate_create_rejects_blank_number(self, runner, blank):
        with patch("cli_anything.propertymeld.http_backend.create_estimate") as mock_fn:
            result = runner.invoke(cli, [
                "estimates", "create",
                "--meld-id", "90000001",
                "--estimate-number", blank,
                "--amount", "1250.00",
            ])
        assert result.exit_code != 0
        assert "estimate-number cannot be empty" in result.output
        mock_fn.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_work_order_create_rejects_blank_brief_description(self, runner, blank):
        with patch("cli_anything.propertymeld.http_backend.create_meld") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "create",
                "--brief-description", blank,
                "--description", "leak under sink",
                "--work-category", "PLUMBING",
                "--work-type", "REPAIR",
                "--due-date", "2026-05-16T02:52:41.393Z",
                "--work-location", "Kitchen",
                "--unit-id", "9000007",
                "--maintenance-id", "5011",
            ])
        assert result.exit_code != 0
        assert "brief-description cannot be empty" in result.output
        mock_fn.assert_not_called()
