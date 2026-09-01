"""Contract tests for merge_meld captured-shape rewrite (2026-05-19).

Earlier today's tests (test_returns_structured_error_on_destination_400, etc.)
were locked against a buggy CLI shape that PM was rejecting because the URL
semantic was flipped (CLI put source in URL; PM expected destination) and the
body field name was wrong (CLI sent {"meld": dest}; PM expected
{"destination_id", "source_ids": [...]}).

Capture doc:
    orgs/example/docs/pm-create-meld-in-and-merge-endpoint-capture-2026-05-19.md

These tests pin the captured-shape behavior:
- URL is /api/melds/{destination_id}/merge/
- Body is {"destination_id": int, "source_ids": [int, ...]}
- Multi-source single-call shape supported (source_ids is array)
- Legacy positional args (meld_id=, into_meld_id=) supported via compat shim
  but emit no friendly-error envelope on 400 (the prior envelope was a
  symptom-handler for our own buggy payload, not a real PM constraint)
"""
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


class TestMergeCapturedShape:
    def test_single_source_uses_destination_in_url_and_body(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        captured = {}

        def fake_urlopen(req, **kw):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            captured["method"] = req.get_method()
            return _FakeResp(body=b'{"message": "Melds merged successfully"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        result = hb.merge_meld(destination_id="90000013", source_ids=["90000014"])

        assert "/melds/90000013/merge/" in captured["url"], "URL must use destination id"
        assert captured["method"] == "POST"
        assert captured["body"] == {"destination_id": 90000013, "source_ids": [90000014]}
        assert result["ok"] is True
        assert result["destination_meld_id"] == 90000013
        assert result["source_meld_ids"] == [90000014]
        assert result["result"]["message"] == "Melds merged successfully"

    def test_multi_source_packs_into_source_ids_array(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        captured = {}

        def fake_urlopen(req, **kw):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return _FakeResp(body=b'{"message": "Melds merged successfully"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        result = hb.merge_meld(
            destination_id="90000013",
            source_ids=["90000014", "90000015"],
        )

        assert captured["body"]["destination_id"] == 90000013
        assert captured["body"]["source_ids"] == [90000014, 90000015]
        assert result["source_meld_ids"] == [90000014, 90000015]

    def test_single_source_passed_as_scalar_is_coerced_to_list(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        captured = {}

        def fake_urlopen(req, **kw):
            captured["body"] = json.loads(req.data)
            return _FakeResp(body=b'{"message": "ok"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        hb.merge_meld(destination_id="90000013", source_ids="90000014")
        assert captured["body"]["source_ids"] == [90000014]

    def test_empty_source_ids_raises(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        called = []

        def fake_urlopen(req, **kw):
            called.append(req.full_url)
            return _FakeResp(body=b"{}")

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(ValueError):
            hb.merge_meld(destination_id="90000013", source_ids=[])
        assert called == [], "no HTTP call should fire on empty source_ids"

    def test_legacy_kwargs_meld_id_and_into_meld_id_still_work(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        captured = {}

        def fake_urlopen(req, **kw):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return _FakeResp(body=b'{"message": "ok"}')

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        # Legacy call: merge_meld(meld_id=SOURCE, into_meld_id=DEST). Compat shim
        # should swap so URL/body use destination correctly.
        hb.merge_meld(
            destination_id="ignored-placeholder",
            source_ids="ignored-placeholder",
            meld_id="90000014",
            into_meld_id="90000013",
        )
        assert "/melds/90000013/merge/" in captured["url"]
        assert captured["body"]["destination_id"] == 90000013
        assert captured["body"]["source_ids"] == [90000014]

    def test_400_response_no_longer_translated_to_friendly_envelope(self, monkeypatch):
        """The old 'destination_not_pending_assignment' envelope was a symptom-
        handler for our own buggy payload. With the captured shape the 400 path
        falls through to normalize_http_error + sys.exit(1) — real failures
        should surface, not be papered over."""
        _patch_creds_csrf(monkeypatch)

        def fake_urlopen(req, **kw):
            err = urllib.error.HTTPError(
                req.full_url, 400,
                "Bad Request", {},
                io_body(b'{"detail": "some real bad request"}'),
            )
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(SystemExit):
            hb.merge_meld(destination_id="90000013", source_ids=["90000014"])


def io_body(b):
    import io
    return io.BytesIO(b)
