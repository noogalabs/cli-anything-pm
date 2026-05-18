"""Contract tests for the P2 CLI gap batch — gaps #3, #4, #5, #6.

Backends already shipped (PR #5, write-endpoint coverage bundle). This
batch is the CLI surface that wires them up:

- `pm invoices hold/decline <invoice_id> --reason ...`  (gap #3)
- `pm projects delete <project_id> --force`             (gap #4)
- `pm work-orders delete-file <file_id> --force`        (gap #5)
- `pm work-orders clone --long-description ...`         (gap #6)

Mirrors the test_cli_work_entries_crud.py mock-and-invoke pattern.
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


INVOICE_ID = 3863382
PROJECT_ID = 222964
FILE_ID = 20254356
MELD_ID = "12774440"


# ─────────────────────────────────────────────────────────────────────────────
# Gap #3 — invoices hold / decline
# ─────────────────────────────────────────────────────────────────────────────


class TestInvoicesHoldCLI:
    def test_hold_threads_reason_through(self, runner):
        with patch("cli_anything.propertymeld.http_backend.hold_meld_invoice",
                   return_value={"ok": True, "invoice_id": INVOICE_ID,
                                 "reason": "needs revision",
                                 "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "invoices", "hold", str(INVOICE_ID),
                "--reason", "needs revision",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        mock_fn.assert_called_once_with(INVOICE_ID, "needs revision")

    def test_hold_missing_reason_is_click_error(self, runner):
        with patch("cli_anything.propertymeld.http_backend.hold_meld_invoice") as mock_fn:
            result = runner.invoke(cli, [
                "invoices", "hold", str(INVOICE_ID),
            ])
        assert result.exit_code != 0
        assert "reason" in result.output.lower()
        mock_fn.assert_not_called()


class TestInvoicesDeclineCLI:
    def test_decline_threads_reason_through(self, runner):
        with patch("cli_anything.propertymeld.http_backend.decline_meld_invoice",
                   return_value={"ok": True, "invoice_id": INVOICE_ID,
                                 "reason": "wrong job",
                                 "result": {}}) as mock_fn:
            result = runner.invoke(cli, [
                "invoices", "decline", str(INVOICE_ID),
                "--reason", "wrong job",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        mock_fn.assert_called_once_with(INVOICE_ID, "wrong job")

    def test_decline_missing_reason_is_click_error(self, runner):
        with patch("cli_anything.propertymeld.http_backend.decline_meld_invoice") as mock_fn:
            result = runner.invoke(cli, [
                "invoices", "decline", str(INVOICE_ID),
            ])
        assert result.exit_code != 0
        assert "reason" in result.output.lower()
        mock_fn.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Gap #4 — projects delete
# ─────────────────────────────────────────────────────────────────────────────


class TestProjectsDeleteCLI:
    def test_delete_with_force_skips_prompt(self, runner):
        with patch("cli_anything.propertymeld.http_backend.delete_project",
                   return_value={"ok": True, "project_id": PROJECT_ID,
                                 "deleted": True}) as mock_fn:
            result = runner.invoke(cli, [
                "projects", "delete", str(PROJECT_ID), "--force",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        mock_fn.assert_called_once_with(PROJECT_ID)

    def test_delete_without_force_in_no_tty_aborts_clearly(self, runner):
        """No --force + no TTY → exit 2 with ok:false envelope, no backend
        call. Mirrors the work-entries delete guard (mitigates CI/cron
        callers forgetting --force)."""
        with patch("cli_anything.propertymeld.http_backend.delete_project") as mock_fn:
            result = runner.invoke(cli, [
                "projects", "delete", str(PROJECT_ID),
            ])
        assert result.exit_code != 0
        mock_fn.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Gap #5 — work-orders delete-file
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkOrdersDeleteFileCLI:
    def test_delete_file_with_force_skips_prompt(self, runner):
        with patch("cli_anything.propertymeld.http_backend.delete_meld_file",
                   return_value={"ok": True, "file_id": FILE_ID,
                                 "deleted": True}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "delete-file", str(FILE_ID), "--force",
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        mock_fn.assert_called_once_with(FILE_ID)

    def test_delete_file_without_force_in_no_tty_aborts_clearly(self, runner):
        with patch("cli_anything.propertymeld.http_backend.delete_meld_file") as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "delete-file", str(FILE_ID),
            ])
        assert result.exit_code != 0
        mock_fn.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Gap #6 — work-orders clone --long-description override
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkOrdersCloneLongDescriptionCLI:
    def test_clone_with_long_description_threads_through(self, runner):
        """--long-description routes to backend `description` kwarg so the
        long-form text is overridden rather than inherited from source.
        Regression guard against Blue's TMIITGJ inheritance bug."""
        with patch("cli_anything.propertymeld.http_backend.clone_meld",
                   return_value={"ok": True, "cloned_from": MELD_ID,
                                 "new_meld_id": 9999,
                                 "brief_description": "Reset toilet"}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "clone",
                "--meld-id", MELD_ID,
                "--description", "Reset toilet",
                "--long-description", "Reset toilet and punch list",
            ])
        assert result.exit_code == 0, result.output
        mock_fn.assert_called_once_with(
            MELD_ID,
            brief_description="Reset toilet",
            description="Reset toilet and punch list",
        )

    def test_clone_without_long_description_passes_none(self, runner):
        """No --long-description → backend gets description=None so the
        existing inherit-from-source behavior is preserved (back-compat)."""
        with patch("cli_anything.propertymeld.http_backend.clone_meld",
                   return_value={"ok": True, "cloned_from": MELD_ID,
                                 "new_meld_id": 9999,
                                 "brief_description": "Copy"}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "clone",
                "--meld-id", MELD_ID,
            ])
        assert result.exit_code == 0, result.output
        mock_fn.assert_called_once_with(
            MELD_ID,
            brief_description=None,
            description=None,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Backend regression — clone_meld description override path
# ─────────────────────────────────────────────────────────────────────────────


class TestCloneMeldBackendDescriptionOverride:
    def test_description_override_replaces_source_description(self):
        """clone_meld(meld_id, description='X') sends description='X' in the
        POST body even when the source meld has its own description value."""
        from cli_anything.propertymeld import http_backend

        captured_payload = {}

        def fake_get(path, cookie_hdr):
            return {
                "id": 12345,
                "brief_description": "Original brief",
                "description": "INHERITED long-form that we want to override",
                "work_category": "Maintenance",
                "work_location": "Kitchen",
                "priority": "MED",
                "unit": {"id": 9999},
            }

        def fake_post(path, payload, cookie_hdr, csrf_token):
            captured_payload.update(payload)
            return {"id": 67890, "reference_id": "TXYZNEW"}

        with patch("cli_anything.propertymeld.http_backend._validate_meld_id",
                   side_effect=lambda x: x), \
             patch("cli_anything.propertymeld.http_backend._load_creds",
                   return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header",
                   return_value=""), \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token",
                   return_value="csrf"), \
             patch("cli_anything.propertymeld.http_backend._http_get",
                   side_effect=fake_get), \
             patch("cli_anything.propertymeld.http_backend._http_post",
                   side_effect=fake_post):
            result = http_backend.clone_meld(
                "12345",
                brief_description="New brief",
                description="OVERRIDDEN long-form scope",
            )

        assert result["ok"] is True
        assert result["new_meld_id"] == 67890
        assert captured_payload["brief_description"] == "New brief"
        assert captured_payload["description"] == "OVERRIDDEN long-form scope"

    def test_description_none_falls_back_to_source(self):
        """clone_meld(meld_id, description=None) preserves the existing
        inherit-from-source behavior (back-compat)."""
        from cli_anything.propertymeld import http_backend

        captured_payload = {}

        def fake_get(path, cookie_hdr):
            return {
                "id": 12345,
                "brief_description": "Original brief",
                "description": "INHERITED long-form",
                "work_category": "Maintenance",
                "work_location": "Kitchen",
                "unit": {"id": 9999},
            }

        def fake_post(path, payload, cookie_hdr, csrf_token):
            captured_payload.update(payload)
            return {"id": 67890, "reference_id": "TXYZNEW"}

        with patch("cli_anything.propertymeld.http_backend._validate_meld_id",
                   side_effect=lambda x: x), \
             patch("cli_anything.propertymeld.http_backend._load_creds",
                   return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header",
                   return_value=""), \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token",
                   return_value="csrf"), \
             patch("cli_anything.propertymeld.http_backend._http_get",
                   side_effect=fake_get), \
             patch("cli_anything.propertymeld.http_backend._http_post",
                   side_effect=fake_post):
            http_backend.clone_meld("12345", brief_description="New brief")

        assert captured_payload["description"] == "INHERITED long-form"
