"""HTTP-boundary contract tests for link_tenant_to_meld verify-and-fail-loud.

These drive the REAL bug an operator reported: link_tenant_to_meld PATCHed the
meld with a merged `tenants` array, PM returned HTTP 2xx, and the function
returned linked:true computed from the LOCAL merge — but an immediate re-GET
showed tenants=[]. The meld's `tenants` relation is very likely read-only /
derived, so the PATCH was silently ignored. Success was illusory.

The fix re-GETs the meld after the PATCH and asserts the tenant id actually
persisted; if not it RAISES (fail-loud), pointing to the web-UI workaround.

The tests mock urllib.request.urlopen at the wire boundary (NOT a mid-level
helper), mirroring tests/test_schedule_appointment.py. link_tenant_to_meld
calls _http_get(melds/{id}/) TWICE (initial read + verify re-GET); the fake
urlopen sequences the two meld-GET responses by call order.
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


# Initial meld GET: no tenants yet + the required full-echo fields the PATCH
# payload reads back (brief_description/work_location/work_category/work_type/
# priority).
_MELD_NO_TENANTS = json.dumps(
    {
        "id": 90000018,
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

# PATCH response — 2xx body (PM echoes the meld; content irrelevant to the fix).
_PATCH_2XX = json.dumps({"id": 90000018, "tenants": []}).encode()


def _wire(monkeypatch, *, verify_meld_body):
    """Install a urlopen that sequences responses:
      1st GET melds/90000018/  -> initial body (_MELD_NO_TENANTS)
      GET tenants/4242/        -> _TENANT
      PATCH melds/90000018/    -> _PATCH_2XX
      2nd GET melds/90000018/  -> verify_meld_body (persisted or not)
    """
    calls = []
    meld_get_count = {"n": 0}

    def fake_urlopen(req, **kw):
        method = req.get_method()
        url = req.full_url
        calls.append({"method": method, "url": url, "body": req.data})
        if url.endswith("/tenants/4242/"):
            return _FakeResp(_TENANT)
        if url.endswith("/melds/90000018/"):
            if method == "PATCH":
                return _FakeResp(_PATCH_2XX)
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
            {"id": 90000018, "tenants": [{"id": 4242}]}
        ).encode()
        calls = _wire(monkeypatch, verify_meld_body=verify_body)

        result = hb.link_tenant_to_meld("90000018", 4242)

        assert result["ok"] is True
        assert result["linked"] is True
        assert result["tenant_id"] == 4242
        assert result["tenant_count"] == 1
        # The fix re-GETs the meld after the PATCH: two meld GETs total.
        meld_gets = [
            c for c in calls
            if c["method"] == "GET" and c["url"].endswith("/melds/90000018/")
        ]
        assert len(meld_gets) == 2

    def test_not_persisted_raises_fail_loud(self, monkeypatch):
        """THE regression: PATCH returns 2xx but the verify re-GET still shows
        tenants=[] -> the link did not persist -> RAISE RuntimeError pointing
        to the web-UI workaround. Must NOT return a linked:true dict."""
        _patch_creds_csrf(monkeypatch)
        verify_body = json.dumps({"id": 90000018, "tenants": []}).encode()
        _wire(monkeypatch, verify_meld_body=verify_body)

        with pytest.raises(RuntimeError) as exc:
            hb.link_tenant_to_meld("90000018", 4242)

        msg = str(exc.value).lower()
        assert "web ui" in msg or "web-ui" in msg
        assert "did not persist" in msg
