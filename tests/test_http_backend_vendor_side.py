"""Contract tests for nested CREATE + vendor-side endpoints.

Verifies:
- create_work_entry hits NESTED path /melds/{meld_id}/work-entries/
- vendor_accept_assignment / vendor_set_schedule hit /v/{vendor_id}/api/...
- vendor_create_invoice / vendor_submit_invoice hit the vendor surface
- Payload shapes match captured request bodies

Asymmetry rule guard: CREATE is nested, EDIT/DELETE is top-level (other test file).
Surface rule guard: vendor flows route via /v/, never /m/.
"""
import json
import pytest

from cli_anything.propertymeld import http_backend as hb


class _FakeResp:
    def __init__(self, body: bytes = b'{"id": 999}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _capture_urlopen(monkeypatch, response_body: bytes = b'{"id": 999}'):
    captured: dict = {}

    def fake_urlopen(req, **kw):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["body"] = req.data
        return _FakeResp(body=response_body)

    monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
    return captured


def _patch_creds_csrf(monkeypatch):
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")
    monkeypatch.setattr(hb, "_get_csrf_token", lambda cookie_hdr: "csrf-fake")


class TestCreateWorkEntry:
    def test_uses_nested_path(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, response_body=b'{"id": 3177515}')
        result = hb.create_work_entry(
            12720246, agent=57163, description="painted",
            long_description="I painted", hours=0.13,
            checkin="2026-05-16T02:52:00.000Z", checkout="2026-05-16T03:00:00.000Z",
        )
        assert cap["method"] == "POST"
        # ASYMMETRY GUARD: NESTED path under meld
        assert "/melds/12720246/work-entries/" in cap["url"]
        body = json.loads(cap["body"])
        assert body["agent"] == 57163
        assert body["description"] == "painted"
        assert body["meld"] == 12720246
        assert body["hours"] == 0.13
        assert result["entry_id"] == 3177515

    def test_omits_optional_fields(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.create_work_entry(42, agent=1, description="x")
        body = json.loads(cap["body"])
        assert "checkin" not in body
        assert "checkout" not in body
        assert "hours" not in body
        # Defaults still present
        assert body["long_description"] == ""
        assert body["meld"] == 42


class TestVendorAcceptAssignment:
    def test_routes_to_vendor_surface(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.vendor_accept_assignment("91159", 8559205)
        assert cap["method"] == "PATCH"
        # SURFACE GUARD: vendor side
        assert "/v/91159/" in cap["url"]
        assert "/m/" not in cap["url"]
        assert "/assignments/8559205/accept/" in cap["url"]
        body = json.loads(cap["body"])
        assert body == {}


class TestVendorSetSchedule:
    def test_normalizes_tuple_segments(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.vendor_set_schedule(
            "91159", 8559205,
            new_segments=[("2026-05-17T14:00:00.000Z", "2026-05-17T16:00:00.000Z")],
        )
        assert "/v/91159/" in cap["url"]
        assert "/assignments/8559205/segments/" in cap["url"]
        body = json.loads(cap["body"])
        assert body["segments_to_keep"] == []
        assert body["new_segments"][0]["event"]["dtstart"] == "2026-05-17T14:00:00.000Z"
        assert body["new_segments"][0]["event"]["dtend"] == "2026-05-17T16:00:00.000Z"
        assert body["new_segments"][0]["event"]["type"] == "default"
        assert body["new_segments"][0]["event"]["_cid"] == "event_0"
        assert body["appointments_required"] == 1

    def test_accepts_fully_formed_segments(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        seg = {"event": {"dtstart": "a", "dtend": "b", "type": "default", "_cid": "event_3"}}
        hb.vendor_set_schedule("91159", 8559205, new_segments=[seg])
        body = json.loads(cap["body"])
        assert body["new_segments"] == [seg]

    def test_rejects_bad_segment(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        _capture_urlopen(monkeypatch)
        with pytest.raises(ValueError, match="Unsupported segment shape"):
            hb.vendor_set_schedule("91159", 8559205, new_segments=["bad"])


class TestVendorCreateInvoice:
    def test_routes_to_vendor_surface_and_normalizes(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, response_body=b'{"id": 3863382}')
        result = hb.vendor_create_invoice(
            "91159", 12791157,
            line_items=[
                {"quantity": 1, "unit_price": 125.00, "description": "first"},
                {"quantity": 1, "unit_price": "250.00", "description": "second"},
            ],
        )
        assert cap["method"] == "POST"
        assert "/v/91159/" in cap["url"]
        assert cap["url"].endswith("/meld-invoices/")
        body = json.loads(cap["body"])
        assert body["meld"] == 12791157
        # unit_price always serialized as string per PM contract
        assert body["invoice_line_items"][0]["unit_price"] == "125.0"
        assert body["invoice_line_items"][1]["unit_price"] == "250.00"
        assert body["invoice_line_items"][0]["_cid"] == "line_item_0"
        assert body["invoice_line_items"][1]["_cid"] == "line_item_1"
        assert result["invoice_id"] == 3863382

    def test_empty_line_items_raises(self):
        with pytest.raises(ValueError, match="at least one entry"):
            hb.vendor_create_invoice("91159", 12791157, line_items=[])


class TestVendorSubmitInvoice:
    def test_sends_submit_flag(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.vendor_submit_invoice("91159", 3863382)
        assert cap["method"] == "PATCH"
        assert "/v/91159/" in cap["url"]
        assert "/meld-invoices/3863382/" in cap["url"]
        # No /hold/ or /decline/ — base submit endpoint
        assert "/hold/" not in cap["url"]
        assert "/decline/" not in cap["url"]
        body = json.loads(cap["body"])
        assert body == {"submit_to_manager": True}
