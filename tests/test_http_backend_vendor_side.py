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
import io
import pytest
import urllib.error

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
        cap = _capture_urlopen(monkeypatch, response_body=b'{"id": 9000010}')
        result = hb.create_work_entry(
            90000003, agent=9036, description="painted",
            long_description="I painted", hours=0.13,
            checkin="2026-05-16T02:52:00.000Z", checkout="2026-05-16T03:00:00.000Z",
        )
        assert cap["method"] == "POST"
        # ASYMMETRY GUARD: NESTED path under meld
        assert "/melds/90000003/work-entries/" in cap["url"]
        body = json.loads(cap["body"])
        assert body["agent"] == 9036
        assert body["description"] == "painted"
        assert body["meld"] == 90000003
        assert body["hours"] == 0.13
        assert result["entry_id"] == 9000010

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
        hb.vendor_accept_assignment("6011", 9000028)
        assert cap["method"] == "PATCH"
        # SURFACE GUARD: vendor side
        assert "/v/6011/" in cap["url"]
        assert "/m/" not in cap["url"]
        assert "/assignments/9000028/accept/" in cap["url"]
        body = json.loads(cap["body"])
        assert body == {}


class TestVendorSetSchedule:
    def test_normalizes_tuple_segments(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.vendor_set_schedule(
            "6011", 9000028,
            new_segments=[("2026-05-17T14:00:00.000Z", "2026-05-17T16:00:00.000Z")],
            segments_to_keep=[],
        )
        assert "/v/6011/" in cap["url"]
        assert "/assignments/9000028/segments/" in cap["url"]
        body = json.loads(cap["body"])
        assert body["segments_to_keep"] == []
        assert body["new_segments"][0]["event"]["dtstart"] == "2026-05-17T14:00:00.000Z"
        assert body["new_segments"][0]["event"]["dtend"] == "2026-05-17T16:00:00.000Z"
        assert body["new_segments"][0]["event"]["type"] == "default"
        assert body["new_segments"][0]["event"]["_cid"] == "event_0"
        assert body["appointments_required"] == 1

    def test_requires_explicit_segments_to_keep(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        _capture_urlopen(monkeypatch)
        with pytest.raises(ValueError, match="segments_to_keep is required"):
            hb.vendor_set_schedule(
                "6011", 9000028,
                new_segments=[("2026-05-17T14:00:00.000Z", "2026-05-17T16:00:00.000Z")],
            )

    def test_accepts_fully_formed_segments(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        seg = {"event": {"dtstart": "a", "dtend": "b", "type": "default", "_cid": "event_3"}}
        hb.vendor_set_schedule("6011", 9000028, new_segments=[seg], segments_to_keep=[123])
        body = json.loads(cap["body"])
        assert body["segments_to_keep"] == [123]
        assert body["new_segments"] == [seg]

    def test_rejects_bad_segment(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        _capture_urlopen(monkeypatch)
        with pytest.raises(ValueError, match="Unsupported segment shape"):
            hb.vendor_set_schedule("6011", 9000028, new_segments=["bad"], segments_to_keep=[])


class TestVendorCreateInvoice:
    def test_routes_to_vendor_surface_and_normalizes(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, response_body=b'{"id": 9000012}')
        result = hb.vendor_create_invoice(
            "6011", 90000010,
            line_items=[
                {"quantity": 1, "unit_price": 125.00, "description": "first"},
                {"quantity": 1, "unit_price": "250.00", "description": "second"},
            ],
        )
        assert cap["method"] == "POST"
        assert "/v/6011/" in cap["url"]
        assert cap["url"].endswith("/meld-invoices/")
        body = json.loads(cap["body"])
        assert body["meld"] == 90000010
        # unit_price always serialized as string per PM contract
        assert body["invoice_line_items"][0]["unit_price"] == "125.0"
        assert body["invoice_line_items"][1]["unit_price"] == "250.00"
        assert body["invoice_line_items"][0]["_cid"] == "line_item_0"
        assert body["invoice_line_items"][1]["_cid"] == "line_item_1"
        assert result["invoice_id"] == 9000012

    def test_empty_line_items_raises(self):
        with pytest.raises(ValueError, match="at least one entry"):
            hb.vendor_create_invoice("6011", 90000010, line_items=[])


class TestVendorIdRequired:
    """Every vendor-side public function rejects empty/None vendor_id before
    coercing to string (otherwise /v/None/ would route silently)."""

    def test_accept_assignment_rejects_none(self):
        with pytest.raises(ValueError, match="vendor_id is required"):
            hb.vendor_accept_assignment(None, 9000028)

    def test_accept_assignment_rejects_empty(self):
        with pytest.raises(ValueError, match="vendor_id is required"):
            hb.vendor_accept_assignment("", 9000028)

    def test_set_schedule_rejects_none(self):
        with pytest.raises(ValueError, match="vendor_id is required"):
            hb.vendor_set_schedule(None, 9000028, new_segments=[])

    def test_create_invoice_rejects_none(self):
        with pytest.raises(ValueError, match="vendor_id is required"):
            hb.vendor_create_invoice(None, 90000010, line_items=[{"unit_price": 1}])

    def test_submit_invoice_rejects_none(self):
        with pytest.raises(ValueError, match="vendor_id is required"):
            hb.vendor_submit_invoice(None, 9000012)


class TestVendorSubmitInvoice:
    def test_sends_submit_flag(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch)
        hb.vendor_submit_invoice("6011", 9000012)
        assert cap["method"] == "PATCH"
        assert "/v/6011/" in cap["url"]
        assert "/meld-invoices/9000012/" in cap["url"]
        # No /hold/ or /decline/ — base submit endpoint
        assert "/hold/" not in cap["url"]
        assert "/decline/" not in cap["url"]
        body = json.loads(cap["body"])
        assert body == {"submit_to_manager": True}


class TestVendorPerson004:
    def test_posts_captured_payload_to_manager_invite_endpoint(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, response_body=b"")
        result = hb.invite_vendor(
            email="alex+zztest@example.com",
            first_name="Fixture Capture",
            last_name="test last name ",
            company="fixture business",
            line1="123 test address example city ",
            postcode="12345",
            phone="2025550133",
        )
        assert cap["method"] == "POST"
        assert "/m/" in cap["url"]
        assert "/vendors/invite/" in cap["url"]
        body = json.loads(cap["body"])
        assert body == {
            "email": "alex+zztest@example.com",
            "first_name": "Fixture Capture",
            "last_name": "test last name ",
            "name": "fixture business",
            "line_1": "123 test address example city ",
            "state": "",
            "postcode": "12345",
            "phone": "2025550133",
        }
        assert result["ok"] is True
        assert result["email"] == "alex+zztest@example.com"

    def test_duplicate_email_400_returns_friendly_not_silent_success(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        err = urllib.error.HTTPError(
            url="https://x/api/vendors/invite/",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=io.BytesIO(b""),
        )

        def boom(req, **kw):
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", boom)
        result = hb.invite_vendor(
            email="alex@example.com",
            first_name="Fixture Capture",
            last_name="test last name ",
            company="fixture business",
            line1="123 test address example city ",
            postcode="12345",
            phone="12025550107",
        )
        assert result["ok"] is False
        assert result["already_exists"] is True
        assert result["already_invited"] is True
        assert result["email"] == "alex@example.com"
        assert result["detail"]["status_code"] == 400


class TestTenantPerson004:
    def test_posts_captured_payload_to_manager_tenants_endpoint(self, monkeypatch):
        unit = {"id": 9000025, "name": "Unit A", "property": {"id": 1000}}
        monkeypatch.setattr(hb, "get_unit", lambda unit_id: unit)
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(
            monkeypatch,
            response_body=b'{"id":9000026,"contact":{"id":9000025},"invited":true}',
        )

        result = hb.invite_tenant(
            unit_id=9000025,
            first_name="Person038",
            last_name="Example",
            email="alex@example.com",
            cell_phone="2025550128",
            notes="notes section",
        )

        assert cap["method"] == "POST"
        assert "/m/" in cap["url"]
        assert "/tenants/" in cap["url"]
        body = json.loads(cap["body"])
        assert body == {
            "contact": {
                "primary_email": "alex@example.com",
                "secondary_email": "alex@example.com",
                "cell_phone": "2025550128",
                "home_phone": "",
            },
            "units": [unit],
            "first_name": "Person038",
            "last_name": "Example",
            "notes": "notes section",
            "should_invite": True,
        }
        assert result["ok"] is True
        assert result["tenant_id"] == 9000026
        assert result["contact_id"] == 9000025

    def test_no_invite_sends_false(self, monkeypatch):
        unit = {"id": 9000025, "name": "Unit A"}
        monkeypatch.setattr(hb, "get_unit", lambda unit_id: unit)
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(
            monkeypatch,
            response_body=b'{"id":9000026,"contact":{"id":9000025},"invited":false}',
        )

        result = hb.invite_tenant(
            9000025,
            "Person038",
            "Example",
            "alex@example.com",
            "2025550128",
            should_invite=False,
        )

        body = json.loads(cap["body"])
        assert body["should_invite"] is False
        assert result["should_invite"] is False

    def test_phone_invalid_400_returns_friendly_validation(self, monkeypatch):
        unit = {"id": 9000025, "name": "Unit A"}
        monkeypatch.setattr(hb, "get_unit", lambda unit_id: unit)
        _patch_creds_csrf(monkeypatch)
        err = urllib.error.HTTPError(
            url="https://x/api/tenants/",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=io.BytesIO(
                b'{"contact":{"cell_phone":["Supplied phone number is invalid"]}}'
            ),
        )

        def boom(req, **kw):
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", boom)
        result = hb.invite_tenant(
            9000025,
            "Person038",
            "Example",
            "alex@example.com",
            "bad-phone",
        )

        assert result["ok"] is False
        assert result["error"] == "malformed cell phone"
        assert result["cell_phone_errors"] == ["Supplied phone number is invalid"]
        assert result["status_code"] == 400


def _tenant_contact_fixture():
    return {
        "id": 9000026,
        "user": {
            "id": 9000001,
            "email": "alex@example.com",
            "first_name": "Person038",
            "last_name": "Example",
        },
        "contact": {
            "id": 9000025,
            "home_phone": "old home",
            "cell_phone": "(202) 555-0128",
            "business_phone": "",
            "primary_email": "alex@example.com",
            "secondary_email": "alex@example.com",
            "tertiary_email": "",
            "tenant_objs": [1000],
        },
        "invited": True,
        "last_invite": {"id": 90000019, "email": "alex@example.com"},
        "first_name": "Person038",
        "middle_name": "",
        "last_name": "Example",
        "notes": "notes section",
        "prompt_for_mobile": True,
        "default_language": "",
        "address": None,
        "management": 1000,
        "leases": [],
        "links": [],
    }


class TestTenantPerson001Person003:
    def test_get_then_puts_full_tenant_object_with_only_contact_updates(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        original = _tenant_contact_fixture()
        updated = json.loads(json.dumps(original))
        updated["contact"]["cell_phone"] = "(202) 555-0129"
        calls = []

        def fake_urlopen(req, **kw):
            calls.append({
                "method": req.get_method(),
                "url": req.full_url,
                "body": req.data,
            })
            if req.get_method() == "GET":
                return _FakeResp(json.dumps(original).encode())
            return _FakeResp(json.dumps(updated).encode())

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        result = hb.edit_tenant_contact(9000026, cell_phone="(202) 555-0129")

        assert [call["method"] for call in calls] == ["GET", "PUT"]
        assert calls[0]["url"].endswith("/tenants/9000026/")
        assert calls[1]["url"].endswith("/tenants/9000026/")
        put_body = json.loads(calls[1]["body"])
        expected = json.loads(json.dumps(original))
        expected["contact"]["cell_phone"] = "(202) 555-0129"
        assert put_body == expected
        assert put_body["contact"]["home_phone"] == "old home"
        assert put_body["contact"]["secondary_email"] == "alex@example.com"
        assert put_body["notes"] == "notes section"
        assert result["ok"] is True
        assert result["tenant_id"] == 9000026

    def test_put_4xx_surfaces_loudly(self, monkeypatch, capsys):
        _patch_creds_csrf(monkeypatch)
        original = _tenant_contact_fixture()

        def fake_urlopen(req, **kw):
            if req.get_method() == "GET":
                return _FakeResp(json.dumps(original).encode())
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=403,
                msg="Forbidden",
                hdrs={},
                fp=io.BytesIO(b'{"detail":"Forbidden"}'),
            )

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb.edit_tenant_contact(9000026, cell_phone="2025550110")

        assert exc.value.code == 1
        stderr = capsys.readouterr().err
        assert '"status_code": 403' in stderr
        assert "Forbidden" in stderr
