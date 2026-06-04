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
    "id": 90000014,
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
            hb.list_files("90000014")
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
            hb.list_files("90000014")
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
        result = hb.list_files("90000014")
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
            hb.inspect_meld("90000014")
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
            hb.inspect_meld("90000014")
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
            if url.endswith("/melds/90000014/") or "/melds/90000014/?" in url:
                return _FakeResp(json.dumps(MELD_DETAIL).encode())
            # All list endpoints (files, tenant-files, vendor-files,
            # work-entries, comments) return empty paginated pages.
            return _FakeResp(json.dumps({"count": 0, "next": None, "results": []}).encode())

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        result = hb.inspect_meld("90000014")
        assert result["meld"]["id"] == 90000014
        assert result["notes"]["completion_notes"] == "done"
        assert result["photos"]["manager"] == []

    def test_optional_path_401_recapture_fails_clean_exit(self, monkeypatch, capsys):
        """F11: a 401 on the OPTIONAL tenant/vendor-files path raises
        SessionExpired inside _http_get_optional_results. With inspect_meld now
        decorated by @with_recapture_retry, a failed recapture funnels to a
        clean SystemExit(1) + parseable stderr JSON, NOT a bare uncaught
        SessionExpired traceback."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            url = req.full_url
            # Optional tenant/vendor file endpoints 401 (auth failure).
            if "tenant-files" in url or "vendor-files" in url:
                raise _http_error(url, 401, b'{"detail": "auth"}')
            # Everything else (meld detail, manager files, work-entries,
            # comments) returns valid empty/detail JSON.
            if "/melds/90000014/" in url and "files" not in url and "work" not in url:
                return _FakeResp(json.dumps(MELD_DETAIL).encode())
            return _FakeResp(json.dumps({"count": 0, "next": None, "results": []}).encode())

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        # Avoid spawning the real Playwright recapture; force it to fail so the
        # decorator deterministically exits.
        monkeypatch.setattr(hb, "_attempt_recapture", lambda: False)

        with pytest.raises(SystemExit) as exc:
            hb.inspect_meld("90000014")
        assert exc.value.code == 1
        # Last stderr line must parse as JSON (structured envelope, no traceback).
        err = capsys.readouterr().err
        json.loads(err.strip().splitlines()[-1])

    def test_optional_path_401_then_recapture_succeeds_returns_dict(self, monkeypatch):
        """F11: a 401 on the optional path that clears after one recapture must
        re-run inspect_meld end-to-end and return the aggregated dict (empty
        tenant/vendor photos), proving the decorator retried successfully."""
        _patch_creds(monkeypatch)

        # The optional path 401s only on the FIRST attempt (before recapture);
        # after recapture clears the flag, the decorator's single re-invocation
        # of inspect_meld runs cleanly to completion.
        state = {"recaptured": False}

        def fake_urlopen(req, **kw):
            url = req.full_url
            if ("tenant-files" in url or "vendor-files" in url) and not state["recaptured"]:
                raise _http_error(url, 401, b'{"detail": "auth"}')
            if "/melds/90000014/" in url and "files" not in url and "work" not in url:
                return _FakeResp(json.dumps(MELD_DETAIL).encode())
            return _FakeResp(json.dumps({"count": 0, "next": None, "results": []}).encode())

        def fake_recapture():
            state["recaptured"] = True
            return True

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        # Recapture succeeds (no real Playwright); decorator re-invokes the call.
        monkeypatch.setattr(hb, "_attempt_recapture", fake_recapture)

        result = hb.inspect_meld("90000014")
        assert result["meld"]["id"] == 90000014
        assert result["photos"]["tenant"] == []
        assert result["photos"]["vendor"] == []


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
            "melds/90000014/tenant-files/?limit=100", "sessionid=fake", "tenant"
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
            "melds/90000014/vendor-files/?limit=100", "sessionid=fake", "vendor"
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
            "melds/90000014/tenant-files/?limit=100", "sessionid=fake", "tenant"
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
                "melds/90000014/vendor-files/?limit=100", "sessionid=fake", "vendor"
            )
        assert exc.value.code == 1

    # ── unified status->action rule: full matrix, BOTH transports ────────────
    #
    # The P2 unify: _OPTIONAL_UNAVAILABLE_STATUSES = {403, 404} is the SINGLE
    # rule applied to both catch branches. A status in the set downgrades; any
    # other recognized status is fatal — regardless of whether the status came
    # from a real HTTPError or was inferred from an HTML-200 interstitial body.

    @pytest.mark.parametrize("code", [403, 404])
    def test_optional_real_http_unavailable_downgrades(self, monkeypatch, code):
        """Real HTTPError 403/404 -> ([], note). NOTE: real-403 is a BEHAVIOR
        CHANGE (previously fatal); it now matches the HTML-200-403 case."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            raise _http_error(req.full_url, code, b'{"detail": "nope"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/90000014/tenant-files/?limit=100", "sessionid=fake", "tenant"
        )
        assert items == []
        assert note is not None
        assert str(code) in note
        assert "tenant" in note

    @pytest.mark.parametrize("code", [500, 429])
    def test_optional_real_http_recognized_non_unavailable_exits(self, monkeypatch, code):
        """Real HTTPError 500/429 (recognized, not in the unavailable set) stays
        fatal on the optional path."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            raise _http_error(req.full_url, code, b'{"detail": "boom"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb._http_get_optional_results(
                "melds/90000014/vendor-files/?limit=100", "sessionid=fake", "vendor"
            )
        assert exc.value.code == 1

    @pytest.mark.parametrize("code", [404, 403])
    def test_optional_html_200_unavailable_status_downgrades(self, monkeypatch, code):
        """HTML-200 interstitial inferring 404/403 -> ([], note)."""
        _patch_creds(monkeypatch)

        html = (
            f"<!DOCTYPE html><html><head><title>{code} Error</title></head>"
            f"<body><h1>{code} Error</h1></body></html>"
        ).encode()

        def fake_urlopen(req, **kw):
            return _FakeResp(html)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/90000014/tenant-files/?limit=100", "sessionid=fake", "tenant"
        )
        assert items == []
        assert note is not None
        assert str(code) in note

    @pytest.mark.parametrize("code", [500, 503])
    def test_optional_html_200_server_error_status_exits(self, monkeypatch, code):
        """THE P2 CORE: an HTML-200 page carrying a 5xx (proxy/app-server error
        page) must FAIL LOUD, not silently downgrade to an empty success."""
        _patch_creds(monkeypatch)

        html = (
            f"<!DOCTYPE html><html><head><title>{code} Server Error</title></head>"
            f"<body><h1>{code} Server Error</h1></body></html>"
        ).encode()

        def fake_urlopen(req, **kw):
            return _FakeResp(html)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb._http_get_optional_results(
                "melds/90000014/vendor-files/?limit=100", "sessionid=fake", "vendor"
            )
        assert exc.value.code == 1

    def test_optional_html_200_no_code_in_body_downgrades(self, monkeypatch):
        """HTML-200 interstitial with no parseable 4xx/5xx code (inferred None)
        downgrades as a forbidden-class (403) interstitial, not fatal."""
        _patch_creds(monkeypatch)

        bare_html = b"<html><body>Access denied. Please log in.</body></html>"

        def fake_urlopen(req, **kw):
            return _FakeResp(bare_html)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/90000014/vendor-files/?limit=100", "sessionid=fake", "vendor"
        )
        assert items == []
        assert note is not None
        assert "403" in note

    def test_optional_200_json_returns_items(self, monkeypatch):
        """A normal 200 JSON page on the optional path returns its results."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            return _FakeResp(json.dumps(FILES_PAGE).encode())

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/90000014/tenant-files/?limit=100", "sessionid=fake", "tenant"
        )
        assert note is None
        assert any(f.get("filename") == "before.jpg" for f in items)


class TestStatusFromHtmlBody:
    """Unit tests for _status_from_html_body status-context anchoring (Codex P2).

    The prior naked `\\b([45]\\d{2})\\b` scan honored ANY standalone 4xx/5xx
    number — incidental CSS/markup (`font-weight: 500`, `width="500"`, a footer
    support code). With inferred-5xx now FATAL on the optional path, that caused
    BOTH wrongful exits on benign interstitials AND, worse, silent downgrades of
    a real 5xx that happened to contain a stray "403"/"404". A code is now only
    honored when it sits in a real status context (reason phrase / heading /
    HTTP|Error|Status label); otherwise None (safe downgrade)."""

    def test_css_number_plus_real_heading_picks_heading(self):
        """`font-weight:500` noise + `<h1>403 Forbidden</h1>` -> 403, not 500."""
        html = (
            "<style>h1{font-weight:500}</style>"
            "<h1>403 Forbidden</h1>"
        )
        assert hb._status_from_html_body(html) == 403

    def test_incidental_number_no_status_context_returns_none(self):
        """`width="500"` only on a login interstitial with NO status heading or
        reason phrase -> None (the P2 core: no bare fallback scan)."""
        html = (
            '<html><body><img width="500">'
            "<form>Please log in to continue</form></body></html>"
        )
        assert hb._status_from_html_body(html) is None

    def test_title_server_error_returns_500(self):
        """A real `<title>500 Internal Server Error</title>` -> 500."""
        assert (
            hb._status_from_html_body("<title>500 Internal Server Error</title>")
            == 500
        )

    def test_worst_case_real_5xx_with_stray_403_infers_5xx(self):
        """WORST CASE (Aussie): a real 5xx page whose body ALSO contains a stray
        '403'/'404' (footer copy / CSS) must infer the 5xx from the heading, NOT
        the stray lower code — otherwise a server error is silently downgraded."""
        html_title = (
            '<style>.box{width:403px}</style>'
            "<title>500 Internal Server Error</title>"
            "<footer>support code 404</footer>"
        )
        assert hb._status_from_html_body(html_title) == 500

        html_heading = (
            "<h1>500 Internal Server Error</h1>"
            '<div style="margin:404px">contact support: 403</div>'
        )
        assert hb._status_from_html_body(html_heading) == 500

    def test_reason_phrase_503_returns_503(self):
        assert (
            hb._status_from_html_body("<p>503 Service Unavailable</p>") == 503
        )

    def test_http_label_502_returns_502(self):
        assert hb._status_from_html_body("<p>HTTP 502 Bad Gateway</p>") == 502

    def test_title_404_returns_404(self):
        assert hb._status_from_html_body("<title>404 Not Found</title>") == 404

    def test_error_label_returns_404(self):
        assert hb._status_from_html_body("Error 404") == 404

    def test_status_label_returns_500(self):
        assert hb._status_from_html_body("Status: 500") == 500

    def test_phrase_then_code_order(self):
        """Phrase-then-code order ('Forbidden 403') is honored too."""
        assert hb._status_from_html_body("Forbidden 403") == 403

    def test_empty_and_none_safe(self):
        assert hb._status_from_html_body("") is None
        assert hb._status_from_html_body(None) is None

    def test_numeric_heading_not_masked_by_later_lower_reason_phrase(self):
        """P2 CORE: a numeric-only 5xx heading ('<h1>500</h1>', NO reason phrase)
        followed by a LATER, lower-code '403 Forbidden' reason phrase must infer
        the heading's 500 (earliest position), NOT the later 403. The prior
        tier-ordered scan returned the reason-phrase 403 first and silently
        masked the server error."""
        html = "<h1>500</h1><footer>403 Forbidden</footer>"
        assert hb._status_from_html_body(html) == 500

    def test_numeric_title_not_masked_by_later_reason_phrase(self):
        """Same masking guard for a numeric-only <title>: earliest position
        (the 502 title) wins over a later '404 Not Found' phrase."""
        html = "<title>502</title><div>404 Not Found</div>"
        assert hb._status_from_html_body(html) == 502

    def test_exact_tie_title_reason_phrase_same_code(self):
        """An exact-position tie ('<title>403 Forbidden</title>' matches both
        the reason-phrase and the heading at ~same spot) is deterministic and
        does NOT change the code — both yield 403."""
        assert hb._status_from_html_body("<title>403 Forbidden</title>") == 403


class TestStatusContextThroughOptionalPath:
    """Verify the anchored inference end-to-end through _http_get_optional_results:
    None/{403,404} downgrade; recognized 5xx/4xx-other fail loud."""

    def test_incidental_500_login_interstitial_downgrades(self, monkeypatch):
        """`width="500"` login interstitial, no status heading -> None inferred
        -> DOWNGRADE (NOT a wrongful sys.exit on a benign page). The P2 core."""
        _patch_creds(monkeypatch)

        html = (
            b'<html><body><img width="500">'
            b"<form>Please log in to continue</form></body></html>"
        )

        def fake_urlopen(req, **kw):
            return _FakeResp(html)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/90000014/tenant-files/?limit=100", "sessionid=fake", "tenant"
        )
        assert items == []
        assert note is not None
        assert "403" in note  # None -> forbidden-class downgrade

    def test_css_noise_plus_403_heading_downgrades(self, monkeypatch):
        """`font-weight:500` + `<h1>403 Forbidden</h1>` -> 403 -> DOWNGRADE."""
        _patch_creds(monkeypatch)

        html = (
            b"<style>h1{font-weight:500}</style>"
            b"<h1>403 Forbidden</h1>"
        )

        def fake_urlopen(req, **kw):
            return _FakeResp(html)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        items, note = hb._http_get_optional_results(
            "melds/90000014/tenant-files/?limit=100", "sessionid=fake", "tenant"
        )
        assert items == []
        assert note is not None
        assert "403" in note

    def test_worst_case_real_5xx_with_stray_403_exits(self, monkeypatch):
        """WORST CASE end-to-end: a real 5xx page with a stray '403' in footer
        copy must FAIL LOUD (SystemExit), NOT silently downgrade as a 4xx."""
        _patch_creds(monkeypatch)

        html = (
            b'<style>.box{width:403px}</style>'
            b"<title>500 Internal Server Error</title>"
            b"<footer>support code 404</footer>"
        )

        def fake_urlopen(req, **kw):
            return _FakeResp(html)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb._http_get_optional_results(
                "melds/90000014/vendor-files/?limit=100", "sessionid=fake", "vendor"
            )
        assert exc.value.code == 1

    def test_numeric_heading_with_later_403_exits_not_downgrade(self, monkeypatch):
        """P2 CORE end-to-end: a numeric-only '<h1>500</h1>' heading followed by
        a LATER '403 Forbidden' in footer copy must FAIL LOUD (SystemExit), NOT
        silently downgrade as a 4xx. Earliest-position selection picks the 500."""
        _patch_creds(monkeypatch)

        html = b"<h1>500</h1><footer>403 Forbidden</footer>"

        def fake_urlopen(req, **kw):
            return _FakeResp(html)

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb._http_get_optional_results(
                "melds/90000014/vendor-files/?limit=100", "sessionid=fake", "vendor"
            )
        assert exc.value.code == 1

    def test_real_title_5xx_exits(self, monkeypatch):
        """A real `<title>500 Internal Server Error</title>` -> 500 -> FATAL."""
        _patch_creds(monkeypatch)

        def fake_urlopen(req, **kw):
            return _FakeResp(b"<title>500 Internal Server Error</title>")

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(SystemExit) as exc:
            hb._http_get_optional_results(
                "melds/90000014/vendor-files/?limit=100", "sessionid=fake", "vendor"
            )
        assert exc.value.code == 1


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
            if url.endswith("/melds/90000014/") or "/melds/90000014/?" in url:
                return _FakeResp(json.dumps(MELD_DETAIL).encode())
            if "tenant-files" in url or "vendor-files" in url:
                # OPTIONAL path: forbidden interstitial served as HTTP 200.
                return _FakeResp(FORBIDDEN_HTML)
            # manager files + work-entries + comments: empty pages.
            return _FakeResp(json.dumps({"count": 0, "next": None, "results": []}).encode())

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        result = hb.inspect_meld("90000014")
        assert result["meld"]["id"] == 90000014
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
            hb.list_files("90000014")
        assert exc.value.code == 1
