"""Contract tests for upload_meld_file - presign + S3 + commit flow.

Verifies the 3-step S3-presign upload flow that replaced the broken multipart
binary impl (regression 2026-05-24 — PM started rejecting multipart with
`{"file": ["Not a valid string."], "filename": ["This field is required."]}`).

The flow mirrors what PM's web UI does:
  1. GET  /api/{role}/files/generate-policy/?filename=&content_type= → policy
  2. POST policy.url multipart with policy.fields + file bytes → S3 201
  3. POST /api/melds/{id}/{commit_endpoint}/ JSON {file, filename, meld_id} → 201

Test scaffolding mirrors tests/test_http_backend_top_level.py.
"""
import io
import json
import os
import tempfile

import pytest

from cli_anything.propertymeld import http_backend as hb


class _FakeResp:
    def __init__(self, body: bytes = b'{"id": 999}', status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _patch_creds_csrf(monkeypatch):
    """Bypass credential loading + CSRF fetch — both off the network path here."""
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")
    monkeypatch.setattr(hb, "_get_csrf_token", lambda cookie_hdr: "csrf-fake")


def _capture_urlopen(monkeypatch, response_bodies: list):
    """Patch urllib.request.urlopen, capture each call in order.

    response_bodies: list of bytes; one per expected urlopen call. Each call
    consumes the next entry.
    """
    captured: list = []
    bodies = list(response_bodies)

    def fake_urlopen(req, **kw):
        captured.append({
            "method": req.get_method(),
            "url": req.full_url,
            "body": req.data,
            "headers": dict(req.header_items()),
        })
        return _FakeResp(body=bodies.pop(0))

    monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
    return captured


@pytest.fixture
def tiny_jpeg():
    """Provide a tiny binary file path for upload tests."""
    fd, path = tempfile.mkstemp(suffix=".jpg")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01")  # JPEG magic + minimal
        yield path
    finally:
        os.unlink(path)


# Presign response shape captured live from PM 2026-05-25:
_PRESIGN_BODY = json.dumps({
    "url": "https://property-meld-app.s3-accelerate.amazonaws.com/",
    "fields": {
        "acl": "public-read",
        "success_action_status": "201",
        "key": "media/meld/files/2026-05-25 18:50:16.900010+00:00-test.jpg",
        "AWSAccessKeyId": "AKIA5QE6ARYYVZKQYUIT",
        "policy": "eyJleHBpcmF0aW9uIjogIjIwMjYtMDUtMjVUMTk6NTA6MTZaIn0=",
        "signature": "I9pxdFT94RThSSuMUV5bsv4wSas=",
    },
}).encode()

_COMMIT_BODY = json.dumps({
    "id": 90000021,
    "meld": 90000016,
    "file": "meld/files/2026-05-25 18:50:43.900009+00:00-test.jpg",
    "filename": "test.jpg",
    "filenotes": [],
}).encode()


class TestUploadMeldFileManager:
    def test_success_path_routes_through_presign_s3_commit(self, monkeypatch, tiny_jpeg):
        """Manager upload: 3 urlopen calls in order (presign, S3 POST, commit)."""
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, [_PRESIGN_BODY, b"<xml/>", _COMMIT_BODY])

        result = hb.upload_meld_file("90000016", tiny_jpeg, "manager")

        assert result["ok"] is True
        assert result["file_id"] == 90000021
        assert result["uploader_role"] == "manager"

        # Three calls: presign GET, S3 POST, commit POST
        assert len(cap) == 3

        # Step 1: presign — GET to manager presign endpoint with filename/content_type query
        assert cap[0]["method"] == "GET"
        assert "/api/melds/files/generate-policy/" in cap[0]["url"]
        assert "filename=" in cap[0]["url"]
        assert "content_type=image%2Fjpeg" in cap[0]["url"]

        # Step 2: S3 POST — to the S3 URL with multipart body containing the fields
        assert cap[1]["method"] == "POST"
        assert cap[1]["url"].startswith("https://property-meld-app.s3-accelerate.amazonaws.com/")
        # All policy fields must be present in the multipart body
        body2 = cap[1]["body"]
        for field in ("AWSAccessKeyId", "policy", "signature", "acl", "success_action_status", "key"):
            assert field.encode() in body2, f"missing policy field {field}"

        # Step 3: commit POST — JSON body to /api/melds/{id}/files/
        assert cap[2]["method"] == "POST"
        assert "/api/melds/90000016/files/" in cap[2]["url"]
        commit_payload = json.loads(cap[2]["body"])
        assert commit_payload["file"] == "media/meld/files/2026-05-25 18:50:16.900010+00:00-test.jpg"
        assert commit_payload["filename"].endswith(".jpg")
        assert commit_payload["meld_id"] == 90000016

    def test_description_included_when_provided(self, monkeypatch, tiny_jpeg):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, [_PRESIGN_BODY, b"<xml/>", _COMMIT_BODY])

        hb.upload_meld_file("90000016", tiny_jpeg, "manager", description="from regression test")

        commit_payload = json.loads(cap[2]["body"])
        assert commit_payload["description"] == "from regression test"

    def test_description_omitted_when_empty(self, monkeypatch, tiny_jpeg):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, [_PRESIGN_BODY, b"<xml/>", _COMMIT_BODY])

        hb.upload_meld_file("90000016", tiny_jpeg, "manager")  # default description=""

        commit_payload = json.loads(cap[2]["body"])
        assert "description" not in commit_payload


class TestUploadMeldFileRoleRouting:
    def test_tenant_role_uses_tenant_presign_and_commit(self, monkeypatch, tiny_jpeg):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, [_PRESIGN_BODY, b"<xml/>", _COMMIT_BODY])

        hb.upload_meld_file("90000016", tiny_jpeg, "tenant")

        # Presign: tenant-side presign URL
        assert "/api/tenants/files/generate-policy/" in cap[0]["url"]
        # Commit: tenant-files endpoint on the meld
        assert "/api/melds/90000016/tenant-files/" in cap[2]["url"]

    def test_vendor_role_uses_vendor_presign_and_commit(self, monkeypatch, tiny_jpeg):
        _patch_creds_csrf(monkeypatch)
        cap = _capture_urlopen(monkeypatch, [_PRESIGN_BODY, b"<xml/>", _COMMIT_BODY])

        hb.upload_meld_file("90000016", tiny_jpeg, "vendor")

        assert "/api/vendors/files/generate-policy/" in cap[0]["url"]
        assert "/api/melds/90000016/vendor-files/" in cap[2]["url"]

    def test_unknown_role_returns_error_dict(self, monkeypatch, tiny_jpeg):
        _patch_creds_csrf(monkeypatch)
        # No urlopen calls expected — should fail before any HTTP
        _capture_urlopen(monkeypatch, [])

        result = hb.upload_meld_file("90000016", tiny_jpeg, "lawyer")

        assert result["ok"] is False
        assert "Unknown uploader_role" in result["error"]


class TestUploadMeldFileErrors:
    def test_missing_file_returns_error_dict(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        _capture_urlopen(monkeypatch, [])

        result = hb.upload_meld_file("90000016", "/tmp/does-not-exist.jpg", "manager")

        assert result["ok"] is False
        assert "File not found" in result["error"]

    def test_presign_http_error_surfaces_pm_body_verbatim(self, monkeypatch, tiny_jpeg):
        """HTTPError on presign step → {ok: False} with normalized error from PM body."""
        _patch_creds_csrf(monkeypatch)
        import urllib.error

        def fake_urlopen(req, **kw):
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                {},
                io.BytesIO(b'{"filename": ["This field is required."]}'),
            )

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        # Bypass the recapture-retry decorator: SessionExpired is only raised on 401.
        result = hb.upload_meld_file("90000016", tiny_jpeg, "manager")

        assert result["ok"] is False
        assert result["uploader_role"] == "manager"

    def test_short_code_meld_id_raises_actionable_error(self, monkeypatch, tiny_jpeg):
        """PM short code (T78HZIFB) must fail at SDK boundary, not as PM 404."""
        _patch_creds_csrf(monkeypatch)
        _capture_urlopen(monkeypatch, [])

        with pytest.raises(ValueError, match="must be the integer PK"):
            hb.upload_meld_file("T78HZIFB", tiny_jpeg, "manager")
