"""Negative-control tests: pm-CLI commands must exit NON-ZERO (code 1) when the
backend returns a failure envelope ({"ok": False, ...}).

Companion to the reliability fix in utils.output_json(): a write command whose
backend returns {"ok": False} now exits 1 (was 0 — the bug). These tests assert
the failure exit so a regression back to exit-0 is caught.

Scaffolding mirrors tests/test_cli.py: CliRunner, patch the backend fn on
cli_anything.propertymeld.http_backend.<fn>, invoke `cli`. The backend fn is
mocked to return {"ok": False, ...}, so NO network call ever fires. For the two
upload commands the file_path option/arg is a click.Path(exists=True); a real
tmp file is supplied so Click's own validation passes (exit 2 otherwise) and the
mocked backend's {"ok": False} drives the exit-1 path.

The two upload commands also call _normalize_meld_id() before the backend.
resolve_meld_id() short-circuits on a digit-only meld_id (returns it verbatim
with no HTTP), so a numeric meld_id keeps these tests off the network.
"""
import json
import os

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


class TestExitNonZeroOnBackendFailure:
    def test_tenants_invite_exits_1_on_failure_envelope(self, runner):
        from unittest.mock import patch
        with patch("cli_anything.propertymeld.http_backend.invite_tenant",
                   return_value={"ok": False, "error": "tenant invite failed"}) as mock_fn:
            result = runner.invoke(cli, [
                "tenants", "invite",
                "--unit-id", "1870266",
                "--first-name", "Alex",
                "--last-name", "Example",
                "--email", "alex@example.com",
                "--cell", "6789235467",
            ])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False
        mock_fn.assert_called_once()

    def test_vendors_invite_exits_1_on_failure_envelope(self, runner):
        from unittest.mock import patch
        with patch("cli_anything.propertymeld.http_backend.invite_vendor",
                   return_value={"ok": False, "error": "vendor invite failed"}) as mock_fn:
            result = runner.invoke(cli, [
                "vendors", "invite",
                "--email", "alex+zztest@example.com",
                "--first-name", "ZZ",
                "--last-name", "Test",
                "--company", "Test Co",
                "--line1", "123 Test St",
                "--postcode", "37421",
                "--phone", "6784567891",
            ])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False
        mock_fn.assert_called_once()

    def test_receipts_upload_exits_1_on_failure_envelope(self, runner, tmp_path):
        from unittest.mock import patch
        # --file is click.Path(exists=True): supply a real file so Click passes
        # (exit 2 otherwise); the mocked backend then drives the exit-1 path.
        receipt_file = tmp_path / "rcpt.pdf"
        receipt_file.write_bytes(b"%PDF-1.4 fake\n")
        with patch("cli_anything.propertymeld.http_backend.upload_receipt",
                   return_value={"ok": False, "error": "File not found"}) as mock_fn:
            result = runner.invoke(cli, [
                "receipts", "upload",
                "--meld-id", "12701108",
                "--file", str(receipt_file),
            ])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False
        mock_fn.assert_called_once()

    def test_work_orders_upload_file_exits_1_on_failure_envelope(self, runner, tmp_path):
        from unittest.mock import patch
        # file_path is a positional click.Path(exists=True): supply a real file.
        upload_file = tmp_path / "photo.jpg"
        upload_file.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01")
        with patch("cli_anything.propertymeld.http_backend.upload_meld_file",
                   return_value={"ok": False, "error": "Unknown uploader_role"}) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "upload-file",
                "12701108", str(upload_file),
                "--as", "manager",
            ])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False
        mock_fn.assert_called_once()
