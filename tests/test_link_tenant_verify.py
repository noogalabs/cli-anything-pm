"""HTTP-boundary contract tests for link_tenant_to_meld verify-and-fail-loud.

These drive the REAL bug an operator reported: link_tenant_to_meld PATCHed the
base meld detail with a merged `tenants` array, PM returned HTTP 2xx, and the
function returned linked:true computed from the LOCAL merge — but an immediate
re-GET showed tenants=[]. The base meld `tenants` relation is read-only/derived,
so the write target must be PM's dedicated meld-tenants endpoint.

The fix PUTs a full relation-object echo to /api/melds/{id}/tenants/ and
re-GETs the relation, asserting the tenant id actually persisted; if not it
RAISES (fail-loud).

The tests mock urllib.request.urlopen at the wire boundary (NOT a mid-level
helper), mirroring tests/test_schedule_appointment.py. link_tenant_to_meld
calls _http_get(melds/{id}/tenants/) TWICE (initial read + verify re-GET); the
fake urlopen sequences the two relation-GET responses by call order.
"""
import json

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


# Initial relation GET: no tenants yet. Extra meld fields are required by the
# live OPTIONS PUT serializer and must be preserved in the full-object echo.
_MELD_NO_TENANTS = json.dumps(
    {
        "id": 12937555,
        "updated": "2026-06-21T00:00:00Z",
        "tenants": [],
        "brief_description": "Leaky faucet",
        "work_location": "Kitchen",
        "work_category": "plumbing",
        "work_type": "repair",
        "priority": "normal",
    }
).encode()

# Tenant GET: the fully-hydrated tenant object link_tenant_to_meld appends.
_TENANT = json.dumps({"id": 4242, "first_name": "Fixture", "last_name": "Doe"}).encode()

# OPTIONS response mirrors the live route: writable contract is actions.PUT.
_OPTIONS = json.dumps({
    "actions": {
        "PUT": {
            "id": {"read_only": True},
            "updated": {"read_only": True},
            "tenants": {"read_only": False, "required": True},
            "brief_description": {"read_only": False, "required": True},
            "work_location": {"read_only": False, "required": True},
            "work_category": {"read_only": False, "required": True},
            "work_type": {"read_only": False},
            "priority": {"read_only": False},
        }
    }
}).encode()

# PUT response — 2xx body (PM echoes the meld; content irrelevant to the fix).
_PUT_2XX = json.dumps({"id": 12937555, "tenants": []}).encode()


def _wire(monkeypatch, *, verify_meld_body):
    """Install a urlopen that sequences responses:
      1st GET melds/12937555/tenants/  -> initial body (_MELD_NO_TENANTS)
      GET tenants/4242/                -> _TENANT
      OPTIONS melds/12937555/tenants/  -> _OPTIONS
      PUT melds/12937555/tenants/      -> _PUT_2XX
      2nd GET melds/12937555/tenants/  -> verify_meld_body (persisted or not)
    """
    calls = []
    meld_get_count = {"n": 0}

    def fake_urlopen(req, **kw):
        method = req.get_method()
        url = req.full_url
        calls.append({"method": method, "url": url, "body": req.data})
        if url.endswith("/tenants/4242/"):
            return _FakeResp(_TENANT)
        if url.endswith("/melds/12937555/tenants/"):
            if method == "OPTIONS":
                return _FakeResp(_OPTIONS)
            if method == "PUT":
                return _FakeResp(_PUT_2XX)
            # GET: first call is the initial read, second is the verify re-GET.
            meld_get_count["n"] += 1
            if meld_get_count["n"] == 1:
                return _FakeResp(_MELD_NO_TENANTS)
            return _FakeResp(verify_meld_body)
        return _FakeResp(b"{}")

    monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
    return calls


class TestLinkTenantVerify:
    def test_persisted_returns_linked_with_count(self, monkeypatch):
        """Verify re-GET shows the tenant in tenants -> linked:true, and
        tenant_count is computed from the PERSISTED tenants (==1), not the
        local merge."""
        _patch_creds_csrf(monkeypatch)
        verify_body = json.dumps(
            {"id": 12937555, "tenants": [{"id": 4242}]}
        ).encode()
        calls = _wire(monkeypatch, verify_meld_body=verify_body)

        result = hb.link_tenant_to_meld("12937555", 4242)

        assert result["ok"] is True
        assert result["linked"] is True
        assert result["tenant_id"] == 4242
        assert result["tenant_count"] == 1
        # The fix re-GETs the relation after the PUT: two relation GETs total.
        meld_gets = [
            c for c in calls
            if c["method"] == "GET"
            and c["url"].endswith("/melds/12937555/tenants/")
        ]
        assert len(meld_gets) == 2
        put_calls = [
            c for c in calls
            if c["method"] == "PUT"
            and c["url"].endswith("/melds/12937555/tenants/")
        ]
        assert len(put_calls) == 1
        put_payload = json.loads(put_calls[0]["body"])
        assert put_payload["brief_description"] == "Leaky faucet"
        assert put_payload["work_location"] == "Kitchen"
        assert put_payload["work_category"] == "plumbing"
        assert put_payload["work_type"] == "repair"
        assert "id" not in put_payload
        assert "updated" not in put_payload
        assert put_payload["tenants"] == [{"id": 4242, "first_name": "Fixture", "last_name": "Doe"}]

    def test_not_persisted_raises_fail_loud(self, monkeypatch):
        """THE regression: PUT returns 2xx but the verify re-GET still shows
        tenants=[] -> the link did not persist -> RAISE RuntimeError. Must NOT
        return a linked:true dict."""
        _patch_creds_csrf(monkeypatch)
        verify_body = json.dumps({"id": 12937555, "tenants": []}).encode()
        _wire(monkeypatch, verify_meld_body=verify_body)

        with pytest.raises(RuntimeError) as exc:
            hb.link_tenant_to_meld("12937555", 4242)

        msg = str(exc.value).lower()
        assert "dedicated" in msg
        assert "relation is unchanged" in msg
        assert "did not persist" in msg
