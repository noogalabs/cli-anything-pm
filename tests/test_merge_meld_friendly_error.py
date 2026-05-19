"""Contract tests for merge_meld friendly-error path (gap-bucket P1 #10, 2026-05-18).

PM API rejects merge when the destination is not in PENDING_ASSIGNMENT (a
tech/vendor has been assigned). PM web UI bypasses this constraint via a
different code path we haven't reverse-engineered yet. Until then, the CLI
translates the 400 "Destination Meld not found" response into a structured
{ok:false, error, message, ...} payload so callers can act on it instead of
the bare 400 + sys.exit(1) that _http_post would otherwise produce.

These tests pin the new error envelope shape + verify the happy path still
returns the expected {ok:true} structure.
"""
import io
import json
import urllib.error
import pytest

from cli_anything.propertymeld import http_backend as hb


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _patch_creds_csrf(monkeypatch):
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")
    monkeypatch.setattr(hb, "_get_csrf_token", lambda cookie_hdr: "csrf-fake")


class TestMergeMeldHappyPath:
    def test_returns_ok_with_merged_meld_id_on_success(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)

        def fake_urlopen(req, **kw):
            assert req.get_method() == "POST"
            assert "/melds/90000014/merge/" in req.full_url
            body = json.loads(req.data)
            # _validate_meld_id coerces to int, so payload carries int meld id.
            assert body["meld"] == 12701109
            return _FakeResp(body=b'{"id": 90000014, "status": "MANAGER_CANCELED"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        result = hb.merge_meld("90000014", "12701109")
        assert result["ok"] is True
        assert result["merged_meld_id"] == 90000014
        assert result["into_meld_id"] == 12701109
        assert result["result"]["status"] == "MANAGER_CANCELED"


class TestMergeMeldDestinationAssigned:
    """Gap-bucket P1 #10: friendly error when destination is not PENDING_ASSIGNMENT."""

    def _raise_destination_400(self, monkeypatch):
        def fake_urlopen(req, **kw):
            body = b'{"detail": "Destination Meld not found"}'
            err = urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(body)
            )
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

    def test_returns_structured_error_on_destination_400(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        self._raise_destination_400(monkeypatch)

        result = hb.merge_meld("90000014", "12701109")
        assert result["ok"] is False
        assert result["error"] == "destination_not_pending_assignment"
        assert "12701109" in result["message"]
        assert "PENDING_ASSIGNMENT" in result["message"]
        assert result["destination_meld_id"] == 12701109
        assert result["source_meld_id"] == 90000014
        # raw_body included so callers can inspect the original PM response.
        assert "Destination Meld not found" in result["raw_body"]

    def test_message_includes_workaround_suggestions(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        self._raise_destination_400(monkeypatch)

        result = hb.merge_meld("90000014", "12701109")
        msg = result["message"]
        # Each of the three documented workarounds should be in the message
        # so the AI / human caller sees the recovery options inline.
        assert "unassign" in msg.lower()
        assert "scope comment" in msg.lower() or "separate" in msg.lower()
        assert "web ui" in msg.lower()

    def test_does_not_exit_process_on_destination_400(self, monkeypatch):
        """Sanity check: the destination-400 path returns a dict, NOT sys.exit."""
        _patch_creds_csrf(monkeypatch)
        self._raise_destination_400(monkeypatch)

        # If the function called sys.exit, pytest would raise SystemExit and
        # this assert would not be reached.
        result = hb.merge_meld("90000014", "12701109")
        assert isinstance(result, dict)
        assert result["ok"] is False


class TestMergeMeldOtherErrors:
    """Non-destination 400s and other 4xx/5xx still take the standard exit
    path so we don't silently swallow real failures.
    """

    def test_500_falls_through_to_sys_exit(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)

        def fake_urlopen(req, **kw):
            body = b'{"detail": "Internal server error"}'
            err = urllib.error.HTTPError(
                req.full_url, 500, "Internal Server Error", {}, io.BytesIO(body)
            )
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit):
            hb.merge_meld("90000014", "12701109")

    def test_400_without_destination_phrase_falls_through(self, monkeypatch):
        """A different 400 (e.g. validation failure) should NOT be misclassified
        as the destination-assigned case. It should sys.exit like normal."""
        _patch_creds_csrf(monkeypatch)

        def fake_urlopen(req, **kw):
            body = b'{"detail": "Source meld not found"}'
            err = urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(body)
            )
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit):
            hb.merge_meld("90000014", "12701109")
