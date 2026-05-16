"""Contract tests for top-level path endpoints (asymmetry rule guard).

Verifies the captured path shapes for:
- work-entries EDIT/DELETE   → top-level /melds/work-entries/{id}/
- manager files DELETE       → top-level /melds/files/{id}/
- project DELETE             → /projects/{id}/
- meld-invoices hold/decline → /meld-invoices/{id}/hold/ + /decline/

The asymmetry rule (feedback_pm_nested_create_top_level_edit) means
DELETE/EDIT paths must NOT be inferred from CREATE — these tests are the
mechanical guard.
"""
import json
import pytest

from cli_anything.propertymeld import http_backend as hb


class _FakeResp:
    def __init__(self, body: bytes = b'{"ok": true}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _capture_urlopen(monkeypatch, response_body: bytes = b'{"ok": true}'):
    """Patch urllib.request.urlopen and capture the request object."""
    captured: dict = {}

    def fake_urlopen(req, **kw):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["body"] = req.data
        return _FakeResp(body=response_body)

    monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
    return captured


def _patch_creds_csrf(monkeypatch):
    """Bypass credential loading + CSRF fetch — both off the network path here."""
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")
    monkeypatch.setattr(hb, "_get_csrf_token", lambda cookie_hdr: "csrf-fake")


class TestUpdateWorkEntry:
    def test_uses_top_level_path(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.update_work_entry(3177515, description="painted", hours=2.5)
        assert cap["method"] == "PATCH"
        # ASYMMETRY GUARD: top-level path, NOT /melds/{meld_id}/work-entries/{id}/
        assert "/melds/work-entries/3177515/" in cap["url"]
        body = json.loads(cap["body"])
        assert body["id"] == 3177515
        assert body["description"] == "painted"
        assert body["hours"] == 2.5
        # Fields not passed must not appear (partial PATCH)
        assert "checkin" not in body
        assert "checkout" not in body

    def test_only_id_when_no_fields(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.update_work_entry(42)
        body = json.loads(cap["body"])
        assert body == {"id": 42}


class TestDeleteWorkEntry:
    def test_uses_top_level_path_and_204(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, response_body=b"")
        result = hb.delete_work_entry(3177515)
        assert cap["method"] == "DELETE"
        # ASYMMETRY GUARD
        assert "/melds/work-entries/3177515/" in cap["url"]
        url_after_api = cap["url"].split("/api/", 1)[1]
        assert url_after_api == "melds/work-entries/3177515/"
        assert result == {"ok": True, "entry_id": 3177515, "deleted": True}


class TestDeleteMeldFile:
    def test_uses_top_level_path(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, response_body=b"")
        result = hb.delete_meld_file(20254356)
        assert cap["method"] == "DELETE"
        # ASYMMETRY GUARD: top-level, NOT /melds/{meld_id}/files/{id}/
        url_after_api = cap["url"].split("/api/", 1)[1]
        assert url_after_api == "melds/files/20254356/"
        assert result["deleted"] is True


class TestDeleteProject:
    def test_returns_204_envelope(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, response_body=b"")
        result = hb.delete_project(222964)
        assert cap["method"] == "DELETE"
        assert "/projects/222964/" in cap["url"]
        assert result == {"ok": True, "project_id": 222964, "deleted": True}


class TestHoldMeldInvoice:
    def test_uses_hold_subpath_with_reason(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.hold_meld_invoice(3863382, reason="needs revision")
        assert cap["method"] == "PATCH"
        assert "/meld-invoices/3863382/hold/" in cap["url"]
        body = json.loads(cap["body"])
        assert body == {"reason": "needs revision"}

    def test_empty_reason_raises(self):
        with pytest.raises(ValueError, match="reason is required"):
            hb.hold_meld_invoice(3863382, reason="")


class TestDeclineMeldInvoice:
    def test_uses_decline_subpath_with_reason(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.decline_meld_invoice(3863382, reason="wrong job")
        assert cap["method"] == "PATCH"
        assert "/meld-invoices/3863382/decline/" in cap["url"]
        body = json.loads(cap["body"])
        assert body == {"reason": "wrong job"}

    def test_empty_reason_raises(self):
        with pytest.raises(ValueError, match="reason is required"):
            hb.decline_meld_invoice(3863382, reason="")
