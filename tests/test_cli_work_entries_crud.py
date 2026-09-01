"""Contract tests for the `pm work-orders work-entries` sub-group CRUD surface.

P1 #1 gap closer — exercises Sub-agent A's CLI refactor that turned the
flat `work-entries` command into a sub-group with list/create/update/delete
children. Mirrors the `TestWorkOrdersLifecycleCLI` mock-and-invoke pattern
from `tests/test_cli.py` exactly: patch `http_backend.*` at the boundary,
invoke via `click.testing.CliRunner`, assert call kwargs + exit code +
JSON envelope shape.

Backend functions themselves are covered separately in
`tests/test_http_backend_top_level.py` (UPDATE/DELETE top-level path
shape) and `tests/test_http_backend_vendor_side.py` (nested CREATE).
"""
import json
import os
from unittest.mock import patch

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


# Blue's safe fixture from the architect doc: TKG5XYM = meld_id 90000009,
# tenant-less + already COMPLETED so it can't disrupt active work.
MELD_ID = "90000009"
ENTRY_ID = 9000010
AGENT_ID_SYNTHETIC = 5015


# ──────────────────────────────────────────────────────────────────────────────
# CREATE — nested POST /api/melds/{meld_id}/work-entries/
# ──────────────────────────────────────────────────────────────────────────────


class TestWorkEntriesCreateCLI:
    def test_create_minimal_required_fields(self, runner):
        """create with only --meld-id, --agent-id, --description passes
        through with optional fields defaulting to None / empty string."""
        with patch("cli_anything.propertymeld.http_backend.create_work_entry",
                   return_value={"ok": True, "meld_id": MELD_ID,
                                 "entry_id": ENTRY_ID,
                                 "result": {"id": ENTRY_ID}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "create",
                "--meld-id", MELD_ID,
                "--agent-id", str(AGENT_ID_SYNTHETIC),
                "--description", "test entry",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["entry_id"] == ENTRY_ID
        mock_fn.assert_called_once_with(
            MELD_ID,
            agent=AGENT_ID_SYNTHETIC,
            description="test entry",
            long_description="",
            checkin=None,
            checkout=None,
            hours=None,
        )

    def test_create_with_all_optional_fields(self, runner):
        """All optional fields (--hours, --checkin, --checkout,
        --long-description) thread through verbatim."""
        with patch("cli_anything.propertymeld.http_backend.create_work_entry",
                   return_value={"ok": True, "meld_id": MELD_ID,
                                 "entry_id": ENTRY_ID,
                                 "result": {"id": ENTRY_ID}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "create",
                "--meld-id", MELD_ID,
                "--agent-id", str(AGENT_ID_SYNTHETIC),
                "--description", "full body",
                "--long-description", "longer notes here",
                "--checkin", "2026-05-18T09:00:00-04:00",
                "--checkout", "2026-05-18T10:30:00-04:00",
                "--hours", "1.5",
            ])
        assert result.exit_code == 0, result.output
        mock_fn.assert_called_once_with(
            MELD_ID,
            agent=AGENT_ID_SYNTHETIC,
            description="full body",
            long_description="longer notes here",
            checkin="2026-05-18T09:00:00-04:00",
            checkout="2026-05-18T10:30:00-04:00",
            hours=1.5,
        )

    def test_create_missing_agent_id_errors(self, runner):
        """--agent-id is required; omitting it is a Click usage error
        (exit 2) before any backend call."""
        with patch("cli_anything.propertymeld.http_backend.create_work_entry") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "create",
                "--meld-id", MELD_ID,
                "--description", "missing agent",
            ])
        assert result.exit_code != 0
        # Click prints "Missing option '--agent-id'" to stderr/output
        assert "agent-id" in result.output.lower() or "agent" in result.output.lower()
        mock_fn.assert_not_called()

    def test_create_missing_description_errors(self, runner):
        """--description is required; omitting it is a Click usage error
        (exit 2) before any backend call."""
        with patch("cli_anything.propertymeld.http_backend.create_work_entry") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "create",
                "--meld-id", MELD_ID,
                "--agent-id", str(AGENT_ID_SYNTHETIC),
            ])
        assert result.exit_code != 0
        assert "description" in result.output.lower()
        mock_fn.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# UPDATE — top-level PATCH /api/melds/work-entries/{entry_id}/
# ──────────────────────────────────────────────────────────────────────────────


class TestWorkEntriesUpdateCLI:
    def test_update_single_field_patches_only_that_field(self, runner):
        """Only --description set: backend receives description='patched'
        and every other kwarg as None (partial-PATCH contract)."""
        with patch("cli_anything.propertymeld.http_backend.update_work_entry",
                   return_value={"ok": True, "entry_id": ENTRY_ID,
                                 "result": {"id": ENTRY_ID,
                                            "description": "patched"}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "update", str(ENTRY_ID),
                "--description", "patched",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        mock_fn.assert_called_once_with(
            ENTRY_ID,
            description="patched",
            long_description=None,
            checkin=None,
            checkout=None,
            hours=None,
            agent=None,
        )

    def test_update_no_fields_errors_with_envelope(self, runner):
        """CLI guard: zero update fields → exit 1 + ok:false envelope,
        no backend call (avoid burning a CSRF round-trip on a no-op).

        Exit code is 1 (the unified ok:false failure code from output_json),
        not the former bespoke 2 — both non-zero; click's own arg-parse
        UsageErrors remain at exit 2."""
        with patch("cli_anything.propertymeld.http_backend.update_work_entry") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "update", str(ENTRY_ID),
            ])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "no fields" in data["error"].lower()
        mock_fn.assert_not_called()

    def test_update_multiple_fields_threads_all_through(self, runner):
        """Multi-field PATCH: all provided values reach the backend,
        unset fields stay None."""
        with patch("cli_anything.propertymeld.http_backend.update_work_entry",
                   return_value={"ok": True, "entry_id": ENTRY_ID,
                                 "result": {"id": ENTRY_ID}}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "update", str(ENTRY_ID),
                "--description", "new desc",
                "--hours", "2.25",
                "--agent-id", str(AGENT_ID_SYNTHETIC),
            ])
        assert result.exit_code == 0, result.output
        mock_fn.assert_called_once_with(
            ENTRY_ID,
            description="new desc",
            long_description=None,
            checkin=None,
            checkout=None,
            hours=2.25,
            agent=AGENT_ID_SYNTHETIC,
        )


# ──────────────────────────────────────────────────────────────────────────────
# DELETE — top-level DELETE /api/melds/work-entries/{entry_id}/
# ──────────────────────────────────────────────────────────────────────────────


class TestWorkEntriesDeleteCLI:
    def test_delete_with_force_skips_prompt(self, runner):
        """--force bypasses click.confirm and goes straight to backend."""
        with patch("cli_anything.propertymeld.http_backend.delete_work_entry",
                   return_value={"ok": True, "entry_id": ENTRY_ID}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "delete", str(ENTRY_ID),
                "--force",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        mock_fn.assert_called_once_with(ENTRY_ID)

    def test_delete_without_force_in_no_tty_aborts_clearly(self, runner):
        """No --force + no TTY (CliRunner gives no stdin tty by default) →
        exit 2 with a clear ok:false envelope rather than hanging or
        running a destructive call. Mitigates R3 from the architect doc
        (CI/cron callers forgetting --force)."""
        with patch("cli_anything.propertymeld.http_backend.delete_work_entry") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "delete", str(ENTRY_ID),
            ])
        assert result.exit_code != 0
        # Either the no-TTY guard fired (preferred) or click.confirm aborted.
        # In both cases backend MUST NOT have been called.
        mock_fn.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# LIST — regression guard for the breaking sub-group rename
# ──────────────────────────────────────────────────────────────────────────────


class TestWorkEntriesListRegressionCLI:
    def test_list_still_works_after_group_refactor(self, runner):
        """Sub-agent A moved `work-entries` from a flat command (positional
        meld_id) to a sub-group with `list` as a child command. This
        regression test locks the new invocation shape and ensures the
        backend call is unchanged."""
        with patch("cli_anything.propertymeld.http_backend.list_work_entries",
                   return_value=[{"id": ENTRY_ID,
                                  "description": "existing entry"}]) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", "list", MELD_ID,
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["id"] == ENTRY_ID
        mock_fn.assert_called_once_with(MELD_ID)

    def test_legacy_flat_invocation_still_routes_to_list(self, runner):
        """Back-compat for the pre-refactor shape `work-entries <meld_id>`
        (no explicit `list` subcommand). Codex review of PR #7 flagged
        that callers using the legacy form would break with "No such
        command"; the _WorkEntriesGroup class routes them to `list`."""
        with patch("cli_anything.propertymeld.http_backend.list_work_entries",
                   return_value=[{"id": ENTRY_ID,
                                  "description": "legacy shape"}]) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "work-entries", MELD_ID,
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data[0]["id"] == ENTRY_ID
        mock_fn.assert_called_once_with(MELD_ID)
