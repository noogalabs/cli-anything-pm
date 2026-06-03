"""Wire-boundary regression tests for list_files + inspect_meld error handling.

Both functions read PM cookie-session endpoints and historically assumed a
200 JSON body. Two crash classes were possible:

1. A forbidden/not-found resource served as an HTTP error (403/404/5xx).
   _http_get already converts non-401 HTTPErrors via normalize_http_error +
   sys.exit(1), so these must surface as a clean SystemExit(1) with a
   structured stderr envelope — never a bare traceback and never a silent
   empty-list-on-403.

2. PM serving a permission-denied / forbidden interstitial as HTTP **200**
   with an HTML body. `json.loads()` on that HTML raised an UNCAUGHT
   json.JSONDecodeError -> full traceback crash. The fix surfaces a clean
   error (status 403 inferred from the body, or a generic non-JSON envelope)
   and exits 1 instead of crashing.

Mirrors the wire-boundary mocking style of tests/test_api_backend.py and
tests/test_merge_meld_friendly_error.py (monkeypatch urllib.request.urlopen
with a fake 200 response or a raised urllib.error.HTTPError).
"""
import io
import json
import urllib.error

import pytest

from cli_anything.propertymeld import http_backend as hb


class _FakeResp:
    """Minimal context-manager stand-in for a urllib 200 response."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _io_body(b: bytes):
    return io.BytesIO(b)


def _patch_creds(monkeypatch):
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")


def _http_error(url: str, code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "err", {}, _io_body(body))


# HTML body PM serves on a forbidden interstitial — note this can come back
# with an HTTP 200 status, which is the uncaught-JSONDecodeError crash case.
FORBIDDEN_HTML = (
    b"<!DOCTYPE html><html><head><title>403 Forbidden</title></head>"
    b"<body><h1>403 Forbidden</h1><p>You do not have permission.</p></body></html>"
)

# A valid single-page files list (DRF paginated shape, no next page).
FILES_PAGE = {
    "count": 1,
    "next": None,
    "results": [
        {"id": 1, "filename": "before.jpg", "signed_url": "https://x/before.jpg"},
    ],
}

MELD_DETAIL = {
    "id": 12701108,
    "completion_notes": "done",
    "maintenance_notes": "n/a",
}


# ── list_files ──────────────────────────────────────────────────────────────


class TestListFilesErrorHandling:
    @pytest.mark.parametrize("code", [403, 404, 500, 502, 503])
    def test_http_error_exits_clean_no_traceback(self, monkeypatch, capsys, code):
        """403/404/5xx HTTPError -> clean SystemExit(1) + structured stderr,
        never a bare traceback, never a silent empty list."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            raise _http_error(req.full_url, code, b'{"detail": "nope"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb.list_files("12701108")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        payload = json.loads(err.strip().splitlines()[-1])
        assert payload["status_code"] == code

    def test_html_403_served_as_http_200_does_not_crash(self, monkeypatch, capsys):
        """PM forbidden page returned with HTTP 200 + HTML body. Must NOT raise
        an uncaught json.JSONDecodeError; must surface a clean error + exit 1."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            return _FakeResp(FORBIDDEN_HTML)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb.list_files("12701108")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        payload = json.loads(err.strip().splitlines()[-1])
        # body-derived 403 when the HTML names it, else a generic non-JSON error
        assert payload.get("status_code") in (403, None)
        assert "error" in payload

    def test_200_json_still_works(self, monkeypatch):
        """A normal 200 JSON page list still returns merged, role-tagged items."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            # Only the manager files endpoint has data; tenant/vendor empty.
            if "/files/" in req.full_url and "tenant" not in req.full_url and "vendor" not in req.full_url:
                return _FakeResp(json.dumps(FILES_PAGE).encode())
            return _FakeResp(json.dumps({"count": 0, "next": None, "results": []}).encode())

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        result = hb.list_files("12701108")
        assert isinstance(result, list)
        assert any(f.get("filename") == "before.jpg" for f in result)
        assert all(f.get("uploader_role") in ("manager", "tenant", "vendor") for f in result)


# ── inspect_meld ─────────────────────────────────────────────────────────────


class TestInspectMeldErrorHandling:
    @pytest.mark.parametrize("code", [403, 404, 500, 502, 503])
    def test_http_error_on_detail_exits_clean_no_traceback(self, monkeypatch, capsys, code):
        """403/404/5xx on the meld detail fetch -> clean SystemExit(1)."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            raise _http_error(req.full_url, code, b'{"detail": "nope"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb.inspect_meld("12701108")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        payload = json.loads(err.strip().splitlines()[-1])
        assert payload["status_code"] == code

    def test_html_403_served_as_http_200_does_not_crash(self, monkeypatch, capsys):
        """Forbidden HTML page at HTTP 200 on the detail fetch must not crash."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            return _FakeResp(FORBIDDEN_HTML)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb.inspect_meld("12701108")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        payload = json.loads(err.strip().splitlines()[-1])
        assert payload.get("status_code") in (403, None)
        assert "error" in payload

    def test_200_json_still_works(self, monkeypatch):
        """A normal 200 detail + empty photo/notes endpoints aggregates cleanly."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            url = req.full_url
            if url.endswith("/melds/12701108/") or "/melds/12701108/?" in url:
                return _FakeResp(json.dumps(MELD_DETAIL).encode())
            # All list endpoints (files, tenant-files, vendor-files,
            # work-entries, comments) return empty paginated pages.
            return _FakeResp(json.dumps({"count": 0, "next": None, "results": []}).encode())

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        result = hb.inspect_meld("12701108")
        assert result["meld"]["id"] == 12701108
        assert result["notes"]["completion_notes"] == "done"
        assert result["photos"]["manager"] == []


# ── OPTIONAL path (_http_get_optional_results) ───────────────────────────────
#
# The OPTIONAL read path is NON-FATAL BY DESIGN: tenant/vendor file endpoints
# may legitimately be forbidden on a manager session. PM serves that as either
# a real HTTP-404 OR an HTML-200 permission interstitial. Both carry the SAME
# "unavailable" semantic and MUST downgrade to ([], note) — never sys.exit(1).
# This is the REQUIRED-vs-OPTIONAL split: _http_get (required) exits on an
# HTML-200; _http_get_optional_results (optional) downgrades.


class TestOptionalResultsDowngrade:
    def test_optional_html_200_interstitial_downgrades_not_exit(self, monkeypatch):
        """HTML-200 forbidden interstitial on the OPTIONAL path -> ([], note)
        with the inferred status, NOT a SystemExit. This is the P2 regression:
        the prior fix routed this through the fatal _parse_json_body_or_exit."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            return _FakeResp(FORBIDDEN_HTML)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/12701108/tenant-files/?limit=100", "sessionid=fake", "tenant"
        )
        assert items == []
        assert note is not None
        # status inferred from the HTML body (403 named in FORBIDDEN_HTML)
        assert "403" in note
        assert "tenant" in note

    def test_optional_html_200_no_status_in_body_defaults_403(self, monkeypatch):
        """HTML-200 interstitial with no parseable 4xx/5xx code in the body
        still downgrades (default 403), not sys.exit."""
        _patch_creds(monkeypatch)

        bare_html = b"<html><body>Access denied. Please log in.</body></html>"

        def fake_urlopen(req, **kw):
            return _FakeResp(bare_html)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/12701108/vendor-files/?limit=100", "sessionid=fake", "vendor"
        )
        assert items == []
        assert note is not None
        assert "403" in note

    def test_optional_real_http_404_still_downgrades(self, monkeypatch):
        """REGRESSION GUARD: the original HTTP-404 downgrade must still work."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            raise _http_error(req.full_url, 404, b'{"detail": "Not found"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/12701108/tenant-files/?limit=100", "sessionid=fake", "tenant"
        )
        assert items == []
        assert note is not None
        assert "404" in note

    def test_optional_non_404_http_error_still_exits(self, monkeypatch):
        """A non-404 transport HTTPError (e.g. 500) is still fatal on the
        optional path — only the 'unavailable' semantics (404 / HTML-200
        interstitial) downgrade."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            raise _http_error(req.full_url, 500, b"<html>500 boom</html>")

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb._http_get_optional_results(
                "melds/12701108/vendor-files/?limit=100", "sessionid=fake", "vendor"
            )
        assert exc.value.code == 1

    def test_optional_200_json_returns_items(self, monkeypatch):
        """A normal 200 JSON page on the optional path returns its results."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            return _FakeResp(json.dumps(FILES_PAGE).encode())

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/12701108/tenant-files/?limit=100", "sessionid=fake", "tenant"
        )
        assert note is None
        assert any(f.get("filename") == "before.jpg" for f in items)


class TestRequiredVsOptionalSplit:
    """The load-bearing asymmetry: same HTML-200 interstitial, two outcomes."""

    def test_inspect_meld_optional_photo_html_200_degrades_to_no_photos(self, monkeypatch):
        """inspect_meld: meld detail + manager files are valid JSON, but the
        OPTIONAL tenant/vendor photo endpoints return an HTML-200 interstitial.
        inspect_meld must SUCCEED with empty tenant/vendor photos + notes, NOT
        sys.exit(1). This is the end-to-end proof of the P2 fix."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            url = req.full_url
            if url.endswith("/melds/12701108/") or "/melds/12701108/?" in url:
                return _FakeResp(json.dumps(MELD_DETAIL).encode())
            if "tenant-files" in url or "vendor-files" in url:
                # OPTIONAL path: forbidden interstitial served as HTTP 200.
                return _FakeResp(FORBIDDEN_HTML)
            # manager files + work-entries + comments: empty pages.
            return _FakeResp(json.dumps({"count": 0, "next": None, "results": []}).encode())

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        result = hb.inspect_meld("12701108")
        assert result["meld"]["id"] == 12701108
        assert result["photos"]["tenant"] == []
        assert result["photos"]["vendor"] == []

    def test_required_path_html_200_still_exits(self, monkeypatch):
        """The REQUIRED path (_http_get, exercised via list_files' manager
        files fetch) on an HTML-200 interstitial STILL sys.exit(1) — its
        fatality is unchanged by this fix."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            return _FakeResp(FORBIDDEN_HTML)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb.list_files("12701108")
        assert exc.value.code == 1
