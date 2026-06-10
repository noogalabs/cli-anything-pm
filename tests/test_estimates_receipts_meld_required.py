"""Contract tests for list_estimates + list_receipts meld-id-required guard.

Smoke matrix on 2026-05-19 surfaced that the previous unscoped CLI default
(`pm estimates list` / `pm receipts list` with no --meld-id) hit
/api/estimates/?limit=N and /api/receipts/?limit=N respectively — neither
of which PM exposes. Both returned HTTP 404. Fix: require meld_id on both
list functions + the CLI subcommands. Direct callers get a ValueError with
a clear message; CLI users get a click "Missing option '--meld-id'" error.

These tests pin:
1. ValueError raised when meld_id is None (no _http_get call attempted)
2. Happy-path meld-scoped paths hit the correct PM URL with correct params
3. _http_get NOT called on the no-meld error path
"""
import pytest

from cli_anything.propertymeld import http_backend as hb


def _patch_creds(monkeypatch):
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")


class TestListEstimatesMeldRequired:
    def test_raises_valueerror_when_meld_id_missing(self, monkeypatch):
        _patch_creds(monkeypatch)
        called = []

        def fake_http_get(path, cookie_hdr):
            called.append(path)
            return {}

        monkeypatch.setattr(hb, "_http_get", fake_http_get)

        with pytest.raises(ValueError) as exc:
            hb.list_estimates(meld_id=None)
        assert "unscoped" in str(exc.value).lower() or "meld_id" in str(exc.value)
        assert called == [], "no HTTP call should fire when meld_id is None"

    def test_meld_scoped_happy_path_hits_correct_url(self, monkeypatch):
        _patch_creds(monkeypatch)
        captured = {}

        def fake_http_get(path, cookie_hdr):
            captured["path"] = path
            return {"results": [{"id": 9001, "amount": "123.45"}]}

        monkeypatch.setattr(hb, "_http_get", fake_http_get)
        result = hb.list_estimates(meld_id="90000014", limit=50)
        assert captured["path"] == "estimates/meld/90000014/?limit=50"
        assert isinstance(result, list)
        assert result[0]["id"] == 9001

    def test_meld_scoped_with_status_filter_appends_query(self, monkeypatch):
        _patch_creds(monkeypatch)
        captured = {}

        def fake_http_get(path, cookie_hdr):
            captured["path"] = path
            return []

        monkeypatch.setattr(hb, "_http_get", fake_http_get)
        hb.list_estimates(meld_id="90000014", limit=25, status="issued")
        assert captured["path"] == "estimates/meld/90000014/?limit=25&status=issued"

    def test_meld_scoped_paginates_next_until_limit(self, monkeypatch):
        _patch_creds(monkeypatch)
        calls = []
        pages = {
            "estimates/meld/90000014/?limit=3": {
                "results": [{"id": 9001}, {"id": 9002}],
                "next": "https://app.propertymeld.test/3287/m/3287/api/estimates/meld/90000014/?cursor=abc&limit=100",
            },
            "estimates/meld/90000014/?cursor=abc&limit=100": {
                "results": [{"id": 9003}],
                "next": None,
            },
        }

        def fake_http_get(path, cookie_hdr):
            calls.append(path)
            return pages[path]

        monkeypatch.setattr(hb, "_http_get", fake_http_get)
        result = hb.list_estimates(meld_id="90000014", limit=3)

        assert [r["id"] for r in result] == [9001, 9002, 9003]
        assert calls == [
            "estimates/meld/90000014/?limit=3",
            "estimates/meld/90000014/?cursor=abc&limit=100",
        ]

    def test_meld_scoped_stops_fetching_once_limit_collected(self, monkeypatch):
        _patch_creds(monkeypatch)
        calls = []
        pages = {
            "estimates/meld/90000014/?limit=100": {
                "results": [{"id": i} for i in range(9000, 9100)],
                "next": "https://app.propertymeld.test/3287/m/3287/api/estimates/meld/90000014/?cursor=page2&limit=100",
            },
            "estimates/meld/90000014/?cursor=page2&limit=100": {
                "results": [{"id": i} for i in range(9100, 9200)],
                "next": "https://app.propertymeld.test/3287/m/3287/api/estimates/meld/90000014/?cursor=page3&limit=100",
            },
            "estimates/meld/90000014/?cursor=page3&limit=100": {
                "results": [{"id": i} for i in range(9200, 9300)],
                "next": None,
            },
        }

        def fake_http_get(path, cookie_hdr):
            calls.append(path)
            return pages[path]

        monkeypatch.setattr(hb, "_http_get", fake_http_get)
        result = hb.list_estimates(meld_id="90000014", limit=150)

        assert len(result) == 150
        assert [r["id"] for r in result[:2]] == [9000, 9001]
        assert calls == [
            "estimates/meld/90000014/?limit=100",
            "estimates/meld/90000014/?cursor=page2&limit=100",
        ]

    def test_meld_scoped_limit_zero_fetches_all(self, monkeypatch):
        _patch_creds(monkeypatch)
        calls = []
        pages = {
            "estimates/meld/90000014/?limit=100": {
                "results": [{"id": 1}],
                "next": "https://app.propertymeld.test/3287/m/3287/api/estimates/meld/90000014/?cursor=page2&limit=100",
            },
            "estimates/meld/90000014/?cursor=page2&limit=100": {
                "results": [{"id": 2}],
                "next": None,
            },
        }

        def fake_http_get(path, cookie_hdr):
            calls.append(path)
            return pages[path]

        monkeypatch.setattr(hb, "_http_get", fake_http_get)
        result = hb.list_estimates(meld_id="90000014", limit=0)

        assert [r["id"] for r in result] == [1, 2]
        assert calls == [
            "estimates/meld/90000014/?limit=100",
            "estimates/meld/90000014/?cursor=page2&limit=100",
        ]


class TestListReceiptsMeldRequired:
    def test_raises_valueerror_when_meld_id_missing(self, monkeypatch):
        _patch_creds(monkeypatch)
        called = []

        def fake_http_get(path, cookie_hdr):
            called.append(path)
            return []

        monkeypatch.setattr(hb, "_http_get", fake_http_get)

        with pytest.raises(ValueError) as exc:
            hb.list_receipts(meld_id=None)
        assert "unscoped" in str(exc.value).lower() or "meld_id" in str(exc.value)
        assert called == [], "no HTTP call should fire when meld_id is None"

    def test_meld_scoped_happy_path_hits_correct_url(self, monkeypatch):
        _patch_creds(monkeypatch)
        captured = {}

        def fake_http_get(path, cookie_hdr):
            captured["path"] = path
            return [{"id": 5001, "amount": "67.89"}]

        monkeypatch.setattr(hb, "_http_get", fake_http_get)
        result = hb.list_receipts(meld_id="90000014", limit=10)
        assert captured["path"] == "melds/90000014/receipts/"
        assert isinstance(result, list)
        assert result[0]["id"] == 5001

    def test_results_unwrapping_handles_both_envelope_and_bare_list(self, monkeypatch):
        _patch_creds(monkeypatch)

        # Envelope form
        monkeypatch.setattr(hb, "_http_get", lambda p, h: {"results": [{"id": 1}, {"id": 2}]})
        assert hb.list_receipts(meld_id="100") == [{"id": 1}, {"id": 2}]

        # Bare list form
        monkeypatch.setattr(hb, "_http_get", lambda p, h: [{"id": 3}])
        assert hb.list_receipts(meld_id="100") == [{"id": 3}]


class TestUpdateEstimateNoOp:
    def test_update_estimate_no_fields_fails_before_http(self, monkeypatch):
        called = []
        monkeypatch.setattr(hb, "_load_creds", lambda: called.append("_load_creds"))

        result = hb.update_estimate("9001")

        assert result["ok"] is False
        assert "no fields" in result["error"].lower()
        assert called == []


class TestEstimatesListCliRequiresMeld:
    def test_estimates_list_cli_errors_without_meld_id(self):
        from click.testing import CliRunner
        from cli_anything.propertymeld.cli import estimates

        runner = CliRunner()
        result = runner.invoke(estimates, ["list"])
        assert result.exit_code != 0
        assert "Missing option" in result.output and "--meld-id" in result.output


class TestReceiptsListCliRequiresMeld:
    def test_receipts_list_cli_errors_without_meld_id(self):
        from click.testing import CliRunner
        from cli_anything.propertymeld.cli import receipts

        runner = CliRunner()
        result = runner.invoke(receipts, ["list"])
        assert result.exit_code != 0
        assert "Missing option" in result.output and "--meld-id" in result.output
