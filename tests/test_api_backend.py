"""Unit tests for api_backend — all API calls mocked."""
import json
import os
import sys
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

# Set dummy env vars before importing
os.environ.setdefault("PM_CLIENT_ID", "test-client-id")
os.environ.setdefault("PM_CLIENT_SECRET", "test-client-secret")

from cli_anything.propertymeld import api_backend
from cli_anything.propertymeld.markers import marker_kind
from cli_anything.propertymeld.utils import clear_token_cache


@pytest.fixture(autouse=True)
def reset_token_cache():
    clear_token_cache()
    yield
    clear_token_cache()


def make_response(data, status: int = 200):
    """Create a mock urllib response."""
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = json.dumps(data).encode()
    mock.status = status
    return mock


TOKEN_RESPONSE = {"access_token": "test-token-abc123", "token_type": "Bearer"}
WO_LIST_RESPONSE = {
    "count": 2,
    "results": [
        {"id": 1001, "status": "open", "description": "Leak in unit 2B"},
        {"id": 1002, "status": "open", "description": "HVAC not working"},
    ]
}
SINGLE_WO_RESPONSE = {"id": 1001, "status": "open", "description": "Leak in unit 2B"}
PROPERTIES_RESPONSE = {"count": 1, "results": [{"id": 5, "name": "123 Main St"}]}
VENDORS_RESPONSE = {"count": 1, "results": [{"id": 10, "name": "Example HVAC"}]}


class TestListWorkOrders:
    def test_returns_results_list(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(WO_LIST_RESPONSE),
            ]
            results = api_backend.list_work_orders()
        assert len(results) == 2
        assert results[0]["id"] == 1001

    def test_status_filter_passed_as_param(self):
        # 'open' fans out to all 3 PENDING_* states sent as repeated status=
        # params per Nexus DRF MultipleChoiceFilter shape (901d1f4).
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"results": []}),
            ]
            api_backend.list_work_orders(status="open")
        call_args = mock_open.call_args_list[1]
        url = call_args[0][0].full_url
        assert "status=PENDING_ASSIGNMENT" in url
        assert "status=PENDING_VENDOR" in url
        assert "status=PENDING_MORE_MANAGEMENT_AVAILABILITY" in url

    def test_work_orders_paginates_nexus_next_until_limit(self):
        first_page = {
            "count": 101,
            "next": "https://nexus.propertymeld.test/api/v2/meld/?cursor=abc&limit=100",
            "results": [{"id": i} for i in range(1000, 1100)],
        }
        second_page = {
            "count": 101,
            "next": None,
            "results": [{"id": 1100}],
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(first_page),
                make_response(second_page),
            ]
            results = api_backend.list_work_orders(limit=150)

        assert [r["id"] for r in results] == list(range(1000, 1101))
        urls = [c.args[0].full_url for c in mock_open.call_args_list]
        assert "limit=100" in urls[1]
        assert urls[2].endswith("/api/v2/meld/?cursor=abc&limit=100")

    def test_handles_flat_list_response(self):
        """Some endpoints return a flat list, not {results: [...]}."""
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response([{"id": 999}]),
            ]
            results = api_backend.list_work_orders()
        # The row survives; work_entries is now explicitly marked not-carried
        # rather than absent, so a consumer cannot read its absence as "none".
        assert len(results) == 1
        assert results[0]["id"] == 999
        assert marker_kind(results[0]["work_entries"]) == "not-carried"

    def test_vendor_filter_uses_cookie_rows_not_ignored_nexus_param(self):
        rich_results = [
            {"id": 1001, "vendor_assignment_requests": [
                {"vendor": {"id": 99, "name": "Example HVAC"}}
            ]},
            {"id": 1002, "vendor_assignment_requests": [
                {"vendor": {"id": 44, "name": "Other Vendor"}}
            ]},
            {"id": 1003, "vendor_assignment_requests": []},
            {"id": 1004},
        ]
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ) as mock_rich, patch("urllib.request.urlopen") as mock_open:
            results = api_backend.list_work_orders(assigned_to_vendor=99, limit=25)
        assert mock_open.call_count == 0
        mock_rich.assert_called_once_with(limit=100, status=None)
        assert [r["id"] for r in results] == [1001]

    def test_stuck_hours_filter_uses_updated_cookie_timestamp(self):
        rich_results = [
            {"id": 1001, "updated": "2000-01-01T00:00:00Z"},
            {"id": 1002, "updated": "2999-01-01T00:00:00Z"},
        ]
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ) as mock_rich, patch("urllib.request.urlopen") as mock_open:
            results = api_backend.list_work_orders(stuck_hours=1, limit=25)
        assert mock_open.call_count == 0
        mock_rich.assert_called_once_with(limit=100, status=None)
        assert [r["id"] for r in results] == [1001]

    def test_assigned_to_tech_fails_loud_until_tech_path_is_gated(self, capsys):
        with patch("urllib.request.urlopen") as mock_open, patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich"
        ) as mock_rich:
            with pytest.raises(SystemExit) as exc_info:
                api_backend.list_work_orders(assigned_to_tech=5011)
        assert exc_info.value.code == 2
        assert mock_open.call_count == 0
        assert mock_rich.call_count == 0
        captured = capsys.readouterr()
        assert "--assigned-to-tech is not supported" in captured.err

    def test_status_raw_filter_passed_as_raw_status_param(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"results": []}),
            ]
            api_backend.list_work_orders(
                status_raw="MAINTENANCE_COULD_NOT_COMPLETE",
                created_since="2026-05-18T00:00:00Z",
                status_not="COMPLETED",
            )
        url = mock_open.call_args_list[1][0][0].full_url
        assert "status=MAINTENANCE_COULD_NOT_COMPLETE" in url
        assert "created_since=2026-05-18T00%3A00%3A00Z" in url
        assert "status_not=COMPLETED" in url

    def test_status_and_status_raw_conflict_fails_loud(self, capsys):
        with patch("urllib.request.urlopen") as mock_open:
            with pytest.raises(SystemExit) as exc_info:
                api_backend.list_work_orders(
                    status="open",
                    status_raw="MAINTENANCE_COULD_NOT_COMPLETE",
                )
        assert exc_info.value.code == 2
        assert mock_open.call_count == 0
        captured = capsys.readouterr()
        assert "--status and --status-raw cannot be combined" in captured.err

    def test_no_tenant_linked_delegates_to_cookie_path_helper(self):
        # When no_tenant_linked=True, api_backend should bypass Nexus and call
        # http_backend.list_work_orders_rich which returns the tenants[] field.
        # Fixture covers three tenants-field shapes intentionally:
        #   * tenants=[]     → empty list, the canonical 'no tenant linked' case
        #   * tenants=[...]  → populated, must be filtered OUT
        #   * tenants=None   → null/missing field, treated as no-tenant-linked
        #                      so a malformed PM response can't silently slip a
        #                      real meld past the filter (truthy-check semantics
        #                      documented in api_backend.list_work_orders).
        rich_results = [
            {"id": 1001, "tenants": []},
            {"id": 1002, "tenants": [{"id": 5, "first_name": "Reece"}]},
            {"id": 1003, "tenants": None},
        ]
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ) as mock_rich, patch("urllib.request.urlopen") as mock_open:
            results = api_backend.list_work_orders(
                no_tenant_linked=True, status="open", limit=25
            )
        # rich helper called once with status + over-fetch limit
        assert mock_rich.call_count == 1
        # Nexus path NOT hit (no urllib request fired)
        assert mock_open.call_count == 0
        # Post-filter keeps only empty/null tenants entries
        assert [r["id"] for r in results] == [1001, 1003]

    def test_no_tenant_linked_respects_caller_limit_after_filter(self):
        rich_results = [{"id": i, "tenants": []} for i in range(50)]
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ):
            results = api_backend.list_work_orders(no_tenant_linked=True, limit=10)
        assert len(results) == 10
        assert [r["id"] for r in results] == list(range(10))

    @pytest.mark.parametrize("flag_kwarg,flag_name", [
        ("created_since", "--created-since"),
        ("status_raw", "--status-raw"),
        ("status_not", "--status-not"),
    ])
    def test_no_tenant_linked_rejects_incompatible_filter_combos(
        self, flag_kwarg, flag_name, capsys
    ):
        # Combining --no-tenant-linked with Nexus-only filters silently
        # returned the wrong meld set pre-fix (cookie-path delegation drops
        # those query params). Loud-fail per silent-failure-half-ships rule.
        kwargs = {
            "no_tenant_linked": True,
            flag_kwarg: (
                99
                if flag_kwarg not in ("created_since", "status_raw", "status_not")
                else "x"
            ),
        }
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich"
        ) as mock_rich:
            with pytest.raises(SystemExit) as exc_info:
                api_backend.list_work_orders(**kwargs)
        assert exc_info.value.code == 2
        # The rich-path delegation must NOT fire when the combo is rejected.
        assert mock_rich.call_count == 0
        captured = capsys.readouterr()
        assert flag_name in captured.err
        assert "cookie list path" in captured.err.lower()

    def test_no_tenant_linked_can_combine_with_vendor_and_stuck_filters(self):
        rich_results = [
            {
                "id": 1001,
                "tenants": [],
                "updated": "2000-01-01T00:00:00Z",
                "vendor_assignment_requests": [{"vendor": {"id": 99}}],
            },
            {
                "id": 1002,
                "tenants": [{"id": 5}],
                "updated": "2000-01-01T00:00:00Z",
                "vendor_assignment_requests": [{"vendor": {"id": 99}}],
            },
            {
                "id": 1003,
                "tenants": [],
                "updated": "2000-01-01T00:00:00Z",
                "vendor_assignment_requests": [{"vendor": {"id": 44}}],
            },
        ]
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ), patch("urllib.request.urlopen") as mock_open:
            results = api_backend.list_work_orders(
                no_tenant_linked=True,
                assigned_to_vendor=99,
                stuck_hours=1,
                limit=25,
            )
        assert mock_open.call_count == 0
        assert [r["id"] for r in results] == [1001]

    def test_stuck_hours_missing_updated_fails_loud(self, capsys):
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=[{"id": 1001}],
        ):
            with pytest.raises(SystemExit) as exc_info:
                api_backend.list_work_orders(stuck_hours=1)
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "Cannot honor --stuck-hours" in captured.err
        assert "updated timestamp" in captured.err

    def test_client_side_filter_warns_when_page_cap_may_truncate(self, capsys):
        rich_results = [
            {"id": i, "vendor_assignment_requests": [{"vendor": {"id": 99}}]}
            for i in range(100)
        ]
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ):
            results = api_backend.list_work_orders(assigned_to_vendor=99, limit=25)
        assert len(results) == 25
        captured = capsys.readouterr()
        assert "result may be incomplete" in captured.err

    def test_client_side_filter_warns_when_cap_hit_even_with_sparse_matches(self, capsys):
        rich_results = [
            {"id": i, "vendor_assignment_requests": [{"vendor": {"id": 99 if i == 0 else 44}}]}
            for i in range(100)
        ]
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ):
            results = api_backend.list_work_orders(assigned_to_vendor=99, limit=25)
        assert [r["id"] for r in results] == [0]
        captured = capsys.readouterr()
        assert "result may be incomplete" in captured.err

    def test_client_side_filter_warns_for_limit_above_default_page_cap(self, capsys):
        rich_results = [
            {"id": i, "vendor_assignment_requests": [{"vendor": {"id": 99}}]}
            for i in range(100)
        ]
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ):
            results = api_backend.list_work_orders(assigned_to_vendor=99, limit=200)
        assert len(results) == 100
        captured = capsys.readouterr()
        assert "result may be incomplete" in captured.err

    def test_client_side_filter_warns_for_limit_above_cap_with_sparse_matches(self, capsys):
        rich_results = [
            {"id": i, "vendor_assignment_requests": [{"vendor": {"id": 99 if i == 0 else 44}}]}
            for i in range(100)
        ]
        with patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ):
            results = api_backend.list_work_orders(assigned_to_vendor=99, limit=200)
        assert [r["id"] for r in results] == [0]
        captured = capsys.readouterr()
        assert "result may be incomplete" in captured.err

    def test_rich_filter_predicates_missing_fields_and_stuck_boundary(self):
        now = api_backend.datetime.fromisoformat("2026-06-09T12:00:00+00:00")
        assert api_backend._row_matches_vendor(
            {"vendor_assignment_requests": [{"vendor": {"id": 99}}]},
            99,
        )
        assert not api_backend._row_matches_vendor({"vendor_assignment_requests": []}, 99)
        assert not api_backend._row_matches_vendor({}, 99)
        assert api_backend._row_matches_stuck_hours(
            {"id": 1001, "updated": "2026-06-09T10:00:00Z"},
            2,
            now=now,
        )
        assert not api_backend._row_matches_stuck_hours(
            {"id": 1002, "updated": "2026-06-09T10:30:00Z"},
            2,
            now=now,
        )

    def test_include_tech_merges_cookie_assignment_fields(self):
        nexus_response = {
            "count": 2,
            "results": [
                {"id": 1001, "status": "PENDING_ASSIGNMENT"},
                {"id": 1002, "status": "PENDING_VENDOR"},
            ],
        }
        rich_results = [
            {
                "id": 1001,
                "in_house_servicers": [
                    {"id": 501, "agent": {"id": 5013, "first_name": "Tech C"}}
                ],
                "managementappointment": [{"id": 7001, "meld": 1001}],
                "vendor_assignment_requests": [],
                "vendorappointment": [],
            },
            {
                "id": 1002,
                "in_house_servicers": [],
                "managementappointment": [],
                "vendor_assignment_requests": [
                    {"id": 9001, "vendor": {"id": 44, "name": "Example HVAC"}}
                ],
                "vendorappointment": [{"id": 8001, "meld": 1002}],
            },
        ]
        with patch("urllib.request.urlopen") as mock_open, patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=rich_results,
        ) as mock_rich:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(nexus_response),
            ]
            results = api_backend.list_work_orders(include_tech=True, status="open")

        assert mock_rich.call_count == 1
        assert results[0]["in_house_servicers"][0]["agent"]["id"] == 5013
        assert results[0]["managementappointment"][0]["id"] == 7001
        assert "vendor_assignment_requests" not in results[0]
        assert "vendorappointment" not in results[0]

    def test_include_tech_empty_when_no_tech(self):
        with patch("urllib.request.urlopen") as mock_open, patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=[{"id": 1001}],
        ):
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"results": [{"id": 1001}]}),
            ]
            results = api_backend.list_work_orders(include_tech=True)

        assert results[0]["in_house_servicers"] == []
        assert results[0]["managementappointment"] == []
        assert "vendor_assignment_requests" not in results[0]
        assert "vendorappointment" not in results[0]

    def test_include_tech_warns_when_cookie_list_empty(self, capsys):
        with patch("urllib.request.urlopen") as mock_open, patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=[],
        ), patch(
            "cli_anything.propertymeld.http_backend.get_work_order_rich",
            return_value={},
        ):
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"results": [{"id": 1001}]}),
            ]
            results = api_backend.list_work_orders(include_tech=True)

        assert results[0]["in_house_servicers"] == []
        captured = capsys.readouterr()
        assert "Warning: --include-tech" in captured.err
        assert "cookie list returned no rows" in captured.err
        assert captured.out == ""

    def test_include_tech_warns_when_cookie_list_fails(self, capsys):
        with patch("urllib.request.urlopen") as mock_open, patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            side_effect=RuntimeError("cookie unavailable"),
        ), patch(
            "cli_anything.propertymeld.http_backend.get_work_order_rich",
        ) as mock_detail:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"results": [{"id": 1001}]}),
            ]
            results = api_backend.list_work_orders(include_tech=True)

        # CHANGED 2026-08-10: same rule as the detail path — a failed cookie
        # list fetch marks the assignment fields instead of emptying them.
        assert marker_kind(results[0]["in_house_servicers"]) == "fetch-failed"
        assert mock_detail.call_count == 0
        captured = capsys.readouterr()
        assert "cookie list fetch failed" in captured.err
        assert "cookie unavailable" in captured.err
        assert captured.out == ""

    def test_include_tech_preserves_existing_nexus_assignment_fields(self):
        nexus_managementappointment = [{"id": 7001, "in_house_servicers": [5013]}]
        with patch("urllib.request.urlopen") as mock_open, patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=[
                {
                    "id": 1001,
                    "in_house_servicers": [
                        {"id": 501, "agent": {"id": 5013, "first_name": "Tech C"}}
                    ],
                    "managementappointment": [{"id": 7001, "management_assignment": 9000020}],
                }
            ],
        ):
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"results": [{"id": 1001, "managementappointment": nexus_managementappointment}]}),
            ]
            results = api_backend.list_work_orders(include_tech=True)

        assert results[0]["in_house_servicers"][0]["agent"]["id"] == 5013
        assert results[0]["managementappointment"] == nexus_managementappointment

    def test_include_tech_detail_fallback_for_missing_list_item(self):
        with patch("urllib.request.urlopen") as mock_open, patch(
            "cli_anything.propertymeld.http_backend.list_work_orders_rich",
            return_value=[],
        ), patch(
            "cli_anything.propertymeld.http_backend.get_work_order_rich",
            return_value={
                "id": 1001,
                "in_house_servicers": [
                    {"id": 501, "agent": {"id": 5013, "first_name": "Tech C"}},
                    {"id": 502, "agent": {"id": 5022, "first_name": "Tech B"}},
                ],
                "managementappointment": [],
            },
        ) as mock_detail:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"results": [{"id": 1001}]}),
            ]
            results = api_backend.list_work_orders(include_tech=True)

        mock_detail.assert_called_once_with("1001")
        assert [a["agent"]["id"] for a in results[0]["in_house_servicers"]] == [5013, 5022]


class TestGetWorkOrder:
    def test_returns_single_work_order(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(SINGLE_WO_RESPONSE),
            ]
            result = api_backend.get_work_order("1001")
        assert result["id"] == 1001

    def test_url_contains_meld_id(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(SINGLE_WO_RESPONSE),
            ]
            api_backend.get_work_order("1001")
        url = mock_open.call_args_list[1][0][0].full_url
        assert "/meld/1001/" in url

    def test_rejects_short_code(self):
        with pytest.raises(ValueError) as exc:
            api_backend.get_work_order("T5LKWTDB")
        assert "integer PK" in str(exc.value)

    def test_include_tech_merges_cookie_assignment_fields(self):
        rich = {
            "in_house_servicers": [
                {"id": 9000027, "agent": {"id": 5013, "first_name": "tech c"}}
            ],
            "managementappointment": [
                {
                    "id": 9000021,
                    "meld": 90000017,
                    "management_assignment": {
                        "in_house_servicers": [
                            {"first_name": "tech c", "last_name": "example"}
                        ]
                    },
                }
            ],
        }
        with patch("urllib.request.urlopen") as mock_open, patch(
            "cli_anything.propertymeld.http_backend.get_work_order_rich",
            return_value=rich,
        ) as mock_rich:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"id": 90000017, "status": "PENDING_ASSIGNMENT"}),
            ]
            result = api_backend.get_work_order("90000017", include_tech=True)

        mock_rich.assert_called_once_with("90000017")
        assert result["in_house_servicers"][0]["agent"]["id"] == 5013
        assert result["managementappointment"][0]["management_assignment"]["in_house_servicers"][0]["last_name"] == "example"

    def test_include_tech_warns_when_cookie_detail_fails(self, capsys):
        with patch("urllib.request.urlopen") as mock_open, patch(
            "cli_anything.propertymeld.http_backend.get_work_order_rich",
            side_effect=RuntimeError("cookie unavailable"),
        ):
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"id": 90000017, "status": "PENDING_ASSIGNMENT"}),
            ]
            result = api_backend.get_work_order("90000017", include_tech=True)

        # CHANGED 2026-08-10: a failed cookie fetch must NOT produce []. These
        # fields answer "who is assigned", and an empty list reads as "nobody
        # assigned" on the emergency-intake path. The old assertion encoded that
        # ambiguity — the warning text beside it even described the problem.
        assert marker_kind(result["in_house_servicers"]) == "fetch-failed"
        assert (result["in_house_servicers"] or []) is not []
        captured = capsys.readouterr()
        assert "cookie detail fetch failed for meld 90000017" in captured.err
        assert "cookie unavailable" in captured.err
        assert captured.out == ""


class TestListProperties:
    def test_returns_property_list(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(PROPERTIES_RESPONSE),
            ]
            results = api_backend.list_properties()
        assert results[0]["name"] == "123 Main St"


class TestListVendors:
    def test_returns_vendor_list(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(VENDORS_RESPONSE),
            ]
            results = api_backend.list_vendors()
        assert results[0]["name"] == "Example HVAC"


class TestProbe:
    def test_returns_ok_with_token(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = make_response(TOKEN_RESPONSE)
            result = api_backend.probe()
        assert result["ok"] is True
        assert "test-tok" in result["token_prefix"]


# ──────────────────────────────────────────────────────────────────────────────
# int-PK guard — Phase A backport
# ──────────────────────────────────────────────────────────────────────────────


from cli_anything.propertymeld import http_backend


class TestValidateMeldIdGuard:
    def test_int_passthrough(self):
        assert http_backend._validate_meld_id(90000001) == 90000001
        assert http_backend._validate_meld_id("90000001") == 90000001

    def test_rejects_short_code(self):
        with pytest.raises(ValueError) as exc:
            http_backend._validate_meld_id("T5LKWTDB")
        assert "T5LKWTDB" in str(exc.value)
        assert "integer PK" in str(exc.value)


class TestListAgents:
    def test_list_agents_uses_paginate_all(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value="cookie"), \
             patch("cli_anything.propertymeld.http_backend._paginate_all",
                   return_value=[{"id": 5012}, {"id": 5013}, {"id": 5014}]) as paginate_mock:
            result = http_backend.list_agents()
        paginate_mock.assert_called_once_with("agents/?limit=100", "cookie")
        assert isinstance(result, list)
        assert len(result) == 3

    def test_list_agents_returns_paginated_items(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value="cookie"), \
             patch("cli_anything.propertymeld.http_backend._paginate_all",
                   return_value=[{"id": 5012}]):
            result = http_backend.list_agents()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 5012

    def test_list_agents_returns_empty_list_on_empty(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value="cookie"), \
             patch("cli_anything.propertymeld.http_backend._paginate_all", return_value=[]):
            result = http_backend.list_agents()
        assert result == []


class TestRecaptureRetry:
    def _session_expired(self):
        return http_backend.SessionExpired(
            urllib.error.HTTPError(
                url="https://app.propertymeld.com/test",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=BytesIO(b""),
            )
        )

    def test_retries_once_after_recapture(self):
        calls = {"count": 0}

        @http_backend.with_recapture_retry
        def flaky():
            calls["count"] += 1
            if calls["count"] == 1:
                raise self._session_expired()
            return {"ok": True}

        with patch("cli_anything.propertymeld.http_backend._attempt_recapture", return_value=True) as recapture:
            assert flaky() == {"ok": True}
        recapture.assert_called_once_with()
        assert calls["count"] == 2

    def test_raises_exit_when_recapture_fails(self):
        @http_backend.with_recapture_retry
        def flaky():
            raise self._session_expired()

        with patch("cli_anything.propertymeld.http_backend._attempt_recapture", return_value=False):
            with pytest.raises(SystemExit) as exc:
                flaky()
        assert exc.value.code == 1


class TestScheduleVendorAppointment:
    """Fixtures mirror the LIVE PM payload captured 2026-05-13 (2nd session).

    The captured manager-UI vendor-schedule request is:
      PATCH /api/assignments/{assignment_request_id}/segments/
      {
        "mark_scheduled": true,
        "segments_to_keep": [],
        "new_segments": [],
        "multiple_segments_to_book": [{"event": {"dtstart": ..., "dtend": ...}}]
      }

    The id targeted is the vendor_assignment_request.id (NOT
    vendorappointment.id). The earlier PR-#1 mocks used a fake field
    `vendorassignment` which never appears in real responses.
    """

    HAPPY_MELD = {
        "id": 90000001,
        "status": "PENDING_TENANT_AVAILABILITY",
        "vendor_assignment_requests": [
            {
                "id": 8000,
                "vendor": {"id": 42, "name": "Example HVAC"},
                "accepted": "2026-05-13T12:59:15.615119Z",
                "rejected": None,
                "canceled": None,
                "meld": 90000001,
            },
        ],
        "vendorappointment": [
            {"id": 7000, "meld": 90000001, "assignment_request": 8000, "availability_segment": None},
        ],
    }

    def test_happy_path_patches_segments_endpoint(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = self.HAPPY_MELD
            mp.return_value = {"appointments_required": None}

            # No _emit_meld_state_change patch here: that name was a dead call
            # that raised NameError AFTER the booking PATCH, masking success and
            # driving a re-run double-book. Running the real path (no mock for it)
            # guards against the NameError regressing.
            result = http_backend.schedule_vendor_appointment(
                "90000001", "42", "2026-05-20T14:00:00-04:00", duration_hours=2.0
            )

            assert result["ok"] is True
            assert result["meld_id"] == 90000001
            assert result["assignment_request_id"] == 8000
            assert result["appointment_id"] == 7000
            assert result["dtstart"] == "2026-05-20T14:00:00-04:00"
            # dtend is computed dtstart + duration_hours.
            assert result["dtend"].startswith("2026-05-20T16:00:00")
            mp.assert_called_once()
            path, payload, _, _ = mp.call_args[0]
            assert path == "assignments/8000/segments/"
            assert payload["mark_scheduled"] is True
            assert payload["segments_to_keep"] == []
            assert payload["new_segments"] == []
            assert len(payload["multiple_segments_to_book"]) == 1
            ev = payload["multiple_segments_to_book"][0]["event"]
            assert ev["dtstart"] == "2026-05-20T14:00:00-04:00"
            assert ev["dtend"].startswith("2026-05-20T16:00:00")

    def test_no_double_book_and_no_nameerror_on_emit_removal(self):
        """Regression: the dead `_emit_meld_state_change` call ran AFTER the
        booking PATCH and raised NameError (the @with_recapture_retry decorator
        only catches SessionExpired, so it propagated and crashed the CLI). The
        booking was already created server-side, so the operator re-ran the
        command -> a SECOND booking. This test runs the real code path with NO
        mock for the emit symbol and asserts:
          (a) no NameError (or any exception) escapes,
          (b) the booking endpoint is PATCHed EXACTLY ONCE (no double-book),
          (c) the success return is intact.
        Pre-fix, this test fails with NameError raised from the function.
        """
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = self.HAPPY_MELD
            mp.return_value = {"appointments_required": None}

            # Must not raise NameError (or anything). Symbol intentionally NOT mocked.
            try:
                result = http_backend.schedule_vendor_appointment(
                    "90000001", "42", "2026-05-20T14:00:00-04:00", duration_hours=2.0
                )
            except NameError as exc:  # pragma: no cover - explicit failure path
                raise AssertionError(
                    f"schedule_vendor_appointment raised NameError (dead _emit call): {exc}"
                )

            # Exactly ONE booking PATCH — proves no re-POST / double-book.
            mp.assert_called_once()
            assert mp.call_args[0][0] == "assignments/8000/segments/"

            # Success return intact.
            assert result["ok"] is True
            assert result["assignment_request_id"] == 8000
            assert result["appointment_id"] == 7000

    def test_returns_error_when_no_vendor_appointment(self):
        meld = {
            "id": 90000001,
            "status": "PENDING_ASSIGNMENT",
            "vendor_assignment_requests": [],
            "vendorappointment": [],
        }
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = meld

            result = http_backend.schedule_vendor_appointment(
                "90000001", "42", "2026-05-20T14:00:00-04:00"
            )
            assert result["ok"] is False
            assert "No vendor appointment" in result["error"]

    def test_no_match_fails_loud(self):
        """F4: an unmatched vendor_id must fail loud, NOT silently book the
        first appointment (a DIFFERENT vendor) and report success."""
        meld = {
            "id": 90000001,
            "status": "PENDING_TENANT_AVAILABILITY",
            "vendor_assignment_requests": [
                {"id": 8000, "vendor": {"id": 10, "name": "First HVAC"}, "accepted": "2026-05-13T10:00:00Z", "rejected": None, "canceled": None},
                {"id": 8001, "vendor": {"id": 42, "name": "Example HVAC"}, "accepted": "2026-05-13T11:00:00Z", "rejected": None, "canceled": None},
            ],
            "vendorappointment": [
                {"id": 7000, "meld": 90000001, "assignment_request": 8000},
                {"id": 7001, "meld": 90000001, "assignment_request": 8001},
            ],
        }
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = meld
            mp.return_value = {}

            # vendor_id 999 matches no appointment — must fail loud, no PATCH.
            result = http_backend.schedule_vendor_appointment(
                "90000001", "999", "2026-05-20T14:00:00-04:00"
            )
            assert result["ok"] is False
            assert "999" in result["error"]
            mp.assert_not_called()

    def test_skips_rejected_request(self):
        """F4: a rejected vendor request must NOT be silently rebooked onto a
        different vendor's appointment via the old first-appointment fallback."""
        meld = {
            "id": 90000001,
            "status": "PENDING_ASSIGNMENT",
            "vendor_assignment_requests": [
                {"id": 8000, "vendor": {"id": 10, "name": "First HVAC"}, "accepted": "2026-05-13T10:00:00Z", "rejected": None, "canceled": None},
                {"id": 8001, "vendor": {"id": 42, "name": "Example HVAC"}, "accepted": None, "rejected": "2026-05-13T11:00:00Z", "canceled": None},
            ],
            "vendorappointment": [
                {"id": 7000, "meld": 90000001, "assignment_request": 8000},
                {"id": 7001, "meld": 90000001, "assignment_request": 8001},
            ],
        }
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = meld
            mp.return_value = {}

            # vendor 42's request is rejected — must fail loud, NOT rebook vendor 10.
            result = http_backend.schedule_vendor_appointment(
                "90000001", "42", "2026-05-20T14:00:00-04:00"
            )
            assert result["ok"] is False
            assert "42" in result["error"]
            mp.assert_not_called()

    def test_refuses_when_appointment_already_booked(self):
        """F5: a second schedule on an already-booked vendor appointment must
        refuse rather than wipe the existing availability_segment via the
        replace-all (segments_to_keep:[]) PATCH."""
        booked = {
            "id": 90000001,
            "status": "PENDING_TENANT_AVAILABILITY",
            "vendor_assignment_requests": [
                {"id": 8000, "vendor": {"id": 42, "name": "Example HVAC"},
                 "accepted": "2026-05-13T12:59:15Z", "rejected": None, "canceled": None},
            ],
            "vendorappointment": [
                {"id": 7000, "meld": 90000001, "assignment_request": 8000,
                 "availability_segment": {"id": 555, "event": {"dtstart": "2026-05-19T09:00:00-04:00"}}},
            ],
        }
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = booked

            result = http_backend.schedule_vendor_appointment(
                "90000001", "42", "2026-05-20T14:00:00-04:00", duration_hours=2.0
            )
            assert result["ok"] is False
            assert "reschedule" in result["error"].lower()
            mp.assert_not_called()  # the destructive replace-all PATCH must NOT fire


# ──────────────────────────────────────────────────────────────────────────────
# Project↔meld operations (pm-capture 2026-05-13 — PR #3)
# ──────────────────────────────────────────────────────────────────────────────


class TestAddMeldsToProject:
    """PUT /api/projects/{id}/add-melds/ — verified shape from pm-capture."""

    def _patched(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_put"),
        )

    def test_happy_path_single_meld(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_put_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, mock_put_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 900005, "melds": [{"id": 90000005, "project": 900005}]}

            result = http_backend.add_melds_to_project("900005", [90000005])

            assert result["ok"] is True
            assert result["project_id"] == "900005"
            path, payload, _, _ = mp.call_args[0]
            assert path == "projects/900005/add-melds/"
            assert payload == {"melds": [{"project": "900005", "id": 90000005}]}

    def test_multi_meld(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_put_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, mock_put_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 900005, "melds": []}

            http_backend.add_melds_to_project("900005", [90000005, 90000006])

            _, payload, _, _ = mp.call_args[0]
            assert payload["melds"] == [
                {"project": "900005", "id": 90000005},
                {"project": "900005", "id": 90000006},
            ]

    def test_empty_meld_list_short_circuits(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_put_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, mock_put_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"

            result = http_backend.add_melds_to_project("900005", [])

            assert result["ok"] is False
            assert "no meld_ids" in result["error"]
            mp.assert_not_called()


_FULL_UNIT_FIXTURE = {
    "id": 9000007,
    "display_address": {"id": 9000003, "line_1": "123 Main St", "city": "Chattanooga"},
    "building": None,
    "floor": None,
    "prop": {"id": 9000003, "line_1": "123 Main St"},
    "current_tenants": [{"id": 9000013, "first_name": "Demo", "last_name": "Resident"}],
}

_FULL_AGENT_FIXTURE = {
    "id": 5011,
    "type": "ManagementAgent",
    "composite_id": "2-5011",
    "first_name": "Alex",
    "last_name": "Example",
    "title": "COORDINATOR",
    "department": "MAINTENANCE",
    "selected_property_groups": [2937],
    "denormalized_property_groups": [2937],
    "property_groups": [2937],
}

_FULL_TENANT_FIXTURE = {
    "id": 99,
    "type": "Tenant",
    "composite_id": "3-99",
    "first_name": "Regina",
    "last_name": "Moses",
    "prompt_for_mobile": False,
    "contact": {"email": "regina@example.com", "phone": "4235550100"},
    "default_language": "en",
    "notification_settings": {"sms": True, "email": True},
}


_TENANT_INVITE_UNIT_FIXTURE = {
    "id": 9000007,
    "prop_groups": [],
    "is_active": True,
    "unit": "",
    "suite": "",
    "apartment": "",
    "room": "",
    "department": "",
    "display_address": {
        "id": 9000003,
        "line_1": "123 Main St",
        "city": "Chattanooga",
        "county_province": "TN",
        "postcode": "12345",
    },
    "prop": {
        "id": 9000003,
        "line_1": "123 Main St",
        "property_name": "",
        "postcode": "12345",
        "city": "Chattanooga",
        "county_province": "TN",
    },
}


class TestTenantInvite:
    """POST /api/tenants/ create-with-invite from W2/W3 #19 HAR capture."""

    def _patch_io(self):
        return (
            patch("cli_anything.propertymeld.http_backend.get_unit"),
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_post_no_exit"),
        )

    def test_hydrates_unit_and_posts_captured_payload_shape(self):
        gu_p, mc_p, mch_p, mcs_p, mp_p = self._patch_io()
        with gu_p as gu, mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp:
            gu.return_value = dict(_TENANT_INVITE_UNIT_FIXTURE)
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {
                "id": 9000023,
                "contact": {"id": 9000025, "cell_phone": "(678) 923-5467"},
                "invited": True,
                "last_invite": {"id": 90000019, "email": "alex@example.com"},
                "notes": "notes section",
            }

            result = http_backend.invite_tenant(
                unit_id="9000007",
                first_name="Alex",
                last_name="Example",
                email="alex@example.com",
                cell_phone="6789235467",
                home_phone="6789873214",
                secondary_email="alt@example.com",
                notes="notes section",
            )

            assert result["ok"] is True
            assert result["tenant_id"] == 9000023
            assert result["contact_id"] == 9000025
            gu.assert_called_once_with(9000007)
            path, payload, _, _ = mp.call_args[0]
            assert path == "tenants/"
            assert payload == {
                "contact": {
                    "primary_email": "alex@example.com",
                    "cell_phone": "6789235467",
                    "secondary_email": "alt@example.com",
                    "home_phone": "6789873214",
                },
                "units": [_TENANT_INVITE_UNIT_FIXTURE],
                "first_name": "Alex",
                "last_name": "Example",
                "notes": "notes section",
                "should_invite": True,
            }

    def test_no_invite_and_optional_fields_omitted(self):
        gu_p, mc_p, mch_p, mcs_p, mp_p = self._patch_io()
        with gu_p as gu, mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp:
            gu.return_value = dict(_TENANT_INVITE_UNIT_FIXTURE)
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 9000023, "contact": {}, "invited": False}

            result = http_backend.invite_tenant(
                9000007, "Alex", "Example", "alex@example.com", "6789235467",
                should_invite=False,
            )

            assert result["should_invite"] is False
            _, payload, _, _ = mp.call_args[0]
            assert payload["should_invite"] is False
            assert payload["notes"] == ""
            assert payload["contact"] == {
                "primary_email": "alex@example.com",
                "secondary_email": "alex@example.com",
                "cell_phone": "6789235467",
                "home_phone": "",
            }

    def test_malformed_phone_400_surfaces_clearly(self):
        gu_p, mc_p, mch_p, mcs_p, mp_p = self._patch_io()
        with gu_p as gu, mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp:
            gu.return_value = dict(_TENANT_INVITE_UNIT_FIXTURE)
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {
                "contact": {"cell_phone": ["Supplied phone number is invalid"]},
                "error": "HTTP 400",
                "status_code": 400,
            }

            result = http_backend.invite_tenant(
                9000007, "Alex", "Example", "alex@example.com", "6781235467"
            )

            assert result["ok"] is False
            assert result["error"] == "malformed cell phone"
            assert result["status_code"] == 400
            assert result["cell_phone_errors"] == ["Supplied phone number is invalid"]

    def test_raises_when_unit_hydration_returns_non_dict(self):
        with patch("cli_anything.propertymeld.http_backend.get_unit", return_value="bad"):
            with pytest.raises(RuntimeError, match="non-dict"):
                http_backend.invite_tenant(
                    9000007, "Alex", "Example", "alex@example.com", "6789235467"
                )


class TestCreateMeldInProject:
    """POST /api/projects/{id}/list-create-meld/ — PM requires fully hydrated objects.

    Stripped {"id": N} inputs auto-hydrate via GET /units/{id}/ + GET /agents/{id}/
    before the POST. Full objects pass through. Partial objects raise pre-wire.
    """

    def _patch_io(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_post"),
            patch("cli_anything.propertymeld.http_backend.get_unit"),
            patch("cli_anything.propertymeld.http_backend.get_management_agent"),
            patch("cli_anything.propertymeld.http_backend.get_tenant"),
        )

    def test_stripped_inputs_auto_hydrate_via_get(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 90000007, "brief_description": "test"}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = _FULL_AGENT_FIXTURE
            mgt.return_value = _FULL_TENANT_FIXTURE

            result = http_backend.create_meld_in_project(
                project_id="900005",
                brief_description="test",
                description="test",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T02:52:41.393Z",
                unit={"id": 9000007},
                maintenance=[{"id": 5011, "type": "ManagementAgent"}],
                tenants=[{"id": 99}],
                work_location="ffff",
                notify_owner=False,
                notify_tenants=True,
            )

            assert result["ok"] is True
            assert result["meld_id"] == 90000007
            assert result["project_id"] == "900005"
            mgu.assert_called_once_with(9000007)
            mga.assert_called_once_with(5011)
            mgt.assert_called_once_with(99)
            path, payload, _, _ = mp.call_args[0]
            assert path == "projects/900005/list-create-meld/"
            assert payload["project"] == "900005"
            assert payload["notify_owners_string"] == "false"
            assert payload["notify_tenants_string"] == "true"
            assert payload["unit"] == _FULL_UNIT_FIXTURE
            assert payload["maintenance"] == [_FULL_AGENT_FIXTURE]
            assert payload["tenants"] == [_FULL_TENANT_FIXTURE]

    def test_full_objects_pass_through_unchanged(self):
        """Power-user path: pre-hydrated objects skip the GET round-trip."""
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}

            http_backend.create_meld_in_project(
                project_id="900005",
                brief_description="b",
                description="d",
                work_category="INTERIOR",
                work_type="PREVENTIVE_MAINTENANCE",
                due_date="2026-05-16T00:00:00.000Z",
                unit=_FULL_UNIT_FIXTURE,
                maintenance=[_FULL_AGENT_FIXTURE],
                tenants=[_FULL_TENANT_FIXTURE],
            )

            mgu.assert_not_called()
            mga.assert_not_called()
            mgt.assert_not_called()
            _, payload, _, _ = mp.call_args[0]
            assert payload["unit"] == _FULL_UNIT_FIXTURE
            assert payload["maintenance"] == [_FULL_AGENT_FIXTURE]
            assert payload["tenants"] == [_FULL_TENANT_FIXTURE]

    def test_maintenance_as_single_dict_gets_wrapped_and_hydrated(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = _FULL_AGENT_FIXTURE
            mgt.return_value = _FULL_TENANT_FIXTURE

            http_backend.create_meld_in_project(
                project_id="900005",
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit={"id": 1},
                maintenance={"id": 5011},
                tenants=[{"id": 99}],
            )

            _, payload, _, _ = mp.call_args[0]
            assert payload["maintenance"] == [_FULL_AGENT_FIXTURE]
            assert payload["tenants"] == [_FULL_TENANT_FIXTURE]

    def test_partial_unit_raises_with_missing_keys(self):
        with pytest.raises(ValueError, match="display_address"):
            http_backend.create_meld_in_project(
                project_id="900005",
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit={"id": 1, "some_other_key": "x"},
                maintenance=[_FULL_AGENT_FIXTURE],
            )

    def test_partial_maintenance_raises_with_missing_keys(self):
        with pytest.raises(ValueError, match="selected_property_groups"):
            http_backend.create_meld_in_project(
                project_id="900005",
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit=_FULL_UNIT_FIXTURE,
                maintenance=[{"id": 5011, "first_name": "Alex"}],
            )

    def test_partial_tenant_raises_with_missing_keys(self):
        with pytest.raises(ValueError, match="contact"):
            http_backend.create_meld_in_project(
                project_id="900005",
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit=_FULL_UNIT_FIXTURE,
                maintenance=[_FULL_AGENT_FIXTURE],
                tenants=[{"id": 99, "first_name": "Regina"}],
            )

    def test_empty_tenants_does_not_call_get(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = _FULL_AGENT_FIXTURE

            http_backend.create_meld_in_project(
                project_id="900005",
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit={"id": 1},
                maintenance=[{"id": 5011}],
            )

            mgt.assert_not_called()
            _, payload, _, _ = mp.call_args[0]
            assert payload["tenants"] == []


class TestCreateMeld:
    """POST /api/melds/ — standalone work-order creation with hydrated nested objects."""

    def _patch_io(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_post"),
            patch("cli_anything.propertymeld.http_backend.get_unit"),
            patch("cli_anything.propertymeld.http_backend.list_all_maintenance"),
            patch("cli_anything.propertymeld.http_backend.get_tenant"),
        )

    def test_create_meld_with_stripped_ids_auto_hydrates(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 90000007, "brief_description": "test"}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = [{**_FULL_AGENT_FIXTURE, "composite_id": "ManagementAgent-5011", "type": "ManagementAgent"}]
            mgt.return_value = _FULL_TENANT_FIXTURE

            result = http_backend.create_meld(
                brief_description="test",
                description="test",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T02:52:41.393Z",
                unit={"id": 9000007},
                maintenance=[{"id": 5011, "type": "ManagementAgent"}],
                tenants=[{"id": 99}],
                work_location="inside",
            )

            assert result["ok"] is True
            assert result["meld_id"] == 90000007
            mgu.assert_called_once_with(9000007)
            mga.assert_called_once_with(registered_only=False)
            mgt.assert_called_once_with(99)
            path, payload, _, _ = mp.call_args[0]
            assert path == "melds/"
            assert payload["unit"] == _FULL_UNIT_FIXTURE
            assert payload["maintenance"][0]["id"] == 5011
            assert payload["maintenance"][0]["composite_id"] == "ManagementAgent-5011"
            assert payload["maintenance"][0]["type"] == "ManagementAgent"
            assert payload["tenants"] == [_FULL_TENANT_FIXTURE]

    def test_create_meld_payload_uses_notify_owners_plural(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = [{**_FULL_AGENT_FIXTURE, "composite_id": "ManagementAgent-5011", "type": "ManagementAgent"}]

            http_backend.create_meld(
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit={"id": 1},
                maintenance=[{"id": 5011}],
                notify_owners=True,
            )
            _, payload, _, _ = mp.call_args[0]
            assert payload["notify_owners"] is True
            assert payload["notify_owners_string"] == "true"
            assert "notify_owner" not in payload

    def test_create_meld_allows_empty_maintenance_payload(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 90000007, "status": "PENDING_ASSIGNMENT", "maintenance": []}
            mgu.return_value = _FULL_UNIT_FIXTURE

            result = http_backend.create_meld(
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit={"id": 1},
                maintenance=[],
                work_location="inside",
            )

            assert result["ok"] is True
            assert result["result"]["status"] == "PENDING_ASSIGNMENT"
            mga.assert_not_called()
            _, payload, _, _ = mp.call_args[0]
            assert payload["maintenance"] == []

    def test_create_meld_omits_project_field(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = [{**_FULL_AGENT_FIXTURE, "composite_id": "ManagementAgent-5011", "type": "ManagementAgent"}]

            http_backend.create_meld(
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit={"id": 1},
                maintenance=[{"id": 5011}],
            )
            _, payload, _, _ = mp.call_args[0]
            assert "project" not in payload

    def test_create_meld_full_objects_pass_through_unchanged(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}
            mga.return_value = []

            http_backend.create_meld(
                brief_description="b",
                description="d",
                work_category="INTERIOR",
                work_type="PREVENTIVE_MAINTENANCE",
                due_date="2026-05-16T00:00:00.000Z",
                unit=_FULL_UNIT_FIXTURE,
                maintenance=[{**_FULL_AGENT_FIXTURE, "composite_id": "ManagementAgent-5011", "type": "ManagementAgent"}],
                tenants=[_FULL_TENANT_FIXTURE],
            )

            mgu.assert_not_called()
            mga.assert_not_called()
            mgt.assert_not_called()
            _, payload, _, _ = mp.call_args[0]
            assert payload["unit"] == _FULL_UNIT_FIXTURE
            assert payload["maintenance"][0]["id"] == _FULL_AGENT_FIXTURE["id"]
            assert payload["maintenance"][0]["composite_id"] == "ManagementAgent-5011"
            assert payload["maintenance"][0]["type"] == "ManagementAgent"
            assert payload["tenants"] == [_FULL_TENANT_FIXTURE]

    def test_create_meld_partial_unit_raises(self):
        with pytest.raises(ValueError, match="display_address"):
            http_backend.create_meld(
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit={"id": 1, "some_other_key": "x"},
                maintenance=[_FULL_AGENT_FIXTURE],
            )

    def test_create_meld_full_maintenance_without_composite_id_raises(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = []
            bad = dict(_FULL_AGENT_FIXTURE)
            bad.pop("composite_id", None)
            with pytest.raises(ValueError, match="composite_id \\+ type"):
                http_backend.create_meld(
                    brief_description="b",
                    description="d",
                    work_category="APPLIANCES",
                    work_type="TURN",
                    due_date="2026-05-16T00:00:00.000Z",
                    unit=_FULL_UNIT_FIXTURE,
                    maintenance=[bad],
                )
            mp.assert_not_called()

    def test_create_meld_unknown_maintenance_id_raises_clear_error(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = [{**_FULL_AGENT_FIXTURE, "id": 5011, "composite_id": "ManagementAgent-5011", "type": "ManagementAgent"}]

            with pytest.raises(ValueError, match="maintenance id 999 not found"):
                http_backend.create_meld(
                    brief_description="b",
                    description="d",
                    work_category="APPLIANCES",
                    work_type="TURN",
                    due_date="2026-05-16T00:00:00.000Z",
                    unit={"id": 1},
                    maintenance=[{"id": 999}],
                )

    def test_create_meld_stripped_maintenance_with_string_id_matches(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p, mgt_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga, mgt_p as mgt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 90000007, "brief_description": "test"}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = [{**_FULL_AGENT_FIXTURE, "id": 5011, "composite_id": "ManagementAgent-5011", "type": "ManagementAgent"}]
            mgt.return_value = _FULL_TENANT_FIXTURE

            result = http_backend.create_meld(
                brief_description="test",
                description="test",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T02:52:41.393Z",
                unit={"id": 9000007},
                maintenance=[{"id": "5011"}],
                tenants=[{"id": 99}],
                work_location="inside",
            )

            assert result["ok"] is True
            path, payload, _, _ = mp.call_args[0]
            assert path == "melds/"
            assert payload["maintenance"][0]["id"] == 5011


class TestUnitAndAgentHydration:
    """GET /api/units/{id}/ and GET /api/agents/{id}/ — hydration helpers."""

    def test_get_unit_hits_correct_path(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mg.return_value = _FULL_UNIT_FIXTURE
            result = http_backend.get_unit(9000007)
            assert result == _FULL_UNIT_FIXTURE
            path, _ = mg.call_args[0]
            assert path == "units/9000007/"

    def test_get_management_agent_hits_correct_path(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mg.return_value = _FULL_AGENT_FIXTURE
            result = http_backend.get_management_agent(5011)
            assert result == _FULL_AGENT_FIXTURE
            path, _ = mg.call_args[0]
            assert path == "agents/5011/"


class TestPatchMeldProjectLink:
    """PATCH /api/melds/{id}/ — full-payload echo (delta 400s, verified live 2026-05-29)."""

    # Current meld returned by the GET that feeds the full-echo payload.
    _CURRENT_MELD = {
        "id": 90000005,
        "brief_description": "b",
        "work_location": "loc",
        "work_category": "APPLIANCES",
        "work_type": "TURN",
        "priority": "MEDIUM",
        "project": None,
    }
    _ECHO_FIELDS = ("brief_description", "work_location", "work_category", "work_type", "priority")

    def _patched(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_get"),
            patch("cli_anything.propertymeld.http_backend._http_patch"),
        )

    def test_attach(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_get_p, mock_patch_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, \
             mock_get_p as mg, mock_patch_p as mpt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = dict(self._CURRENT_MELD)
            mpt.return_value = {"id": 90000005, "project": 900005}

            result = http_backend.patch_meld_project_link("90000005", 900005)

            assert result["ok"] is True
            assert result["meld_id"] == 90000005
            assert result["project_id"] == 900005
            path, payload, _, _ = mpt.call_args[0]
            assert path == "melds/90000005/"
            # Full-echo: every required field present (delta would 400 in prod),
            # plus the project being set. A regression to a delta payload fails here.
            for field in self._ECHO_FIELDS:
                assert field in payload, f"full-echo payload missing {field}"
            assert payload["project"] == 900005
            assert "id" not in payload

    def test_detach_sends_null(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_get_p, mock_patch_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, \
             mock_get_p as mg, mock_patch_p as mpt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = dict(self._CURRENT_MELD)
            mpt.return_value = {"id": 90000005, "project": None}

            result = http_backend.patch_meld_project_link("90000005", None)

            assert result["ok"] is True
            assert result["project_id"] is None
            _, payload, _, _ = mpt.call_args[0]
            for field in self._ECHO_FIELDS:
                assert field in payload, f"full-echo payload missing {field}"
            assert payload["project"] is None
            assert "id" not in payload


# ──────────────────────────────────────────────────────────────────────────────
# Top-level project create/edit + meld notes (pm-capture 2nd session 2026-05-13)
# ──────────────────────────────────────────────────────────────────────────────


class TestCreateProjectLiveShape:
    """POST /api/projects/ — verified from 2nd pm-capture (2026-05-13 02:58Z).

    Captured payload was:
      {name, project_type, description, due_date, start_date,
       coordinators:[int], meld_location:"Unit", prop:null,
       unit:{id:int, label:str}}
    """

    def test_happy_path_mirrors_capture(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_post") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 900006, "name": "vendor assigning"}

            result = http_backend.create_project(
                name="vendor assigning",
                project_type="TURN",
                due_date="2026-05-22T04:00:00.000Z",
                start_date="2026-05-13T23:59:59-04:00",
                coordinators=[5011],
                unit={"id": 9000007, "label": "123 Main St, Chattanooga, TN, 12345"},
            )

            assert result["ok"] is True
            assert result["project_id"] == 900006
            path, payload, _, _ = mp.call_args[0]
            assert path == "projects/"
            assert payload["name"] == "vendor assigning"
            assert payload["project_type"] == "TURN"
            assert payload["coordinators"] == [5011]
            assert payload["meld_location"] == "Unit"
            assert payload["prop"] is None
            assert payload["unit"] == {"id": 9000007, "label": "123 Main St, Chattanooga, TN, 12345"}
            assert payload["description"] == ""


class TestUpdateProjectLiveShape:
    """PATCH /api/projects/{id}/ — full-payload-echo verified live 2026-05-14.

    PM rejects partial PATCH on projects (HTTP 400 "field is required" for
    every omitted required field). update_project fetches the current
    project first, overlays caller-set fields, and sends the FULL merged
    payload. The mocked _http_get returns the current state.
    """

    CURRENT_PROJECT = {
        "id": 900005,
        "name": "original",
        "project_type": "TURN",
        "description": "old description",
        "due_date": "2026-05-30T04:00:00Z",
        "start_date": "2026-05-14T03:00:00Z",
        "coordinators": [{"id": 5011, "first_name": "Alex"}],
        "meld_location": "Unit",
        "prop": None,
        "unit": {"id": 9000007, "label": "123 Main St"},
    }

    def test_happy_path_merges_caller_fields_with_full_echo(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = dict(self.CURRENT_PROJECT)
            mp.return_value = {"id": 900005, "name": "renamed"}

            result = http_backend.update_project(
                project_id="900005",
                name="renamed",
                description="new description",
            )

            assert result["ok"] is True
            assert result["project_id"] == "900005"
            mg.assert_called_once()
            mp.assert_called_once()
            path, payload, _, _ = mp.call_args[0]
            assert path == "projects/900005/"
            # Full payload echo — required fields all present, caller fields override.
            assert payload["name"] == "renamed"
            assert payload["description"] == "new description"
            assert payload["project_type"] == "TURN"
            assert payload["coordinators"] == [5011]  # coordinator dict flattened to id
            assert payload["unit"] == {"id": 9000007, "label": "123 Main St"}
            assert payload["meld_location"] == "Unit"
            assert payload["due_date"] == "2026-05-30T04:00:00Z"
            assert payload["start_date"] == "2026-05-14T03:00:00Z"

    def test_passing_no_overrides_still_sends_full_echo(self):
        """Calling update_project with no overrides is a "ping with echo" — PM accepts."""
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = dict(self.CURRENT_PROJECT)
            mp.return_value = {"id": 900005}

            result = http_backend.update_project(project_id="900005")

            assert result["ok"] is True
            mp.assert_called_once()
            _, payload, _, _ = mp.call_args[0]
            # No overrides — echo back the current state verbatim.
            assert payload["name"] == "original"
            assert payload["project_type"] == "TURN"


class TestUpdateMeldNotes:
    """PATCH /api/v2/melds/{id}/notes/ — verified from 2nd pm-capture."""

    def test_happy_path(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 90000004, "maintenance_notes": "test"}

            result = http_backend.update_meld_notes("90000004", "test")

            assert result["ok"] is True
            assert result["meld_id"] == 90000004
            path, payload, _, _ = mp.call_args[0]
            assert path == "v2/melds/90000004/notes/"
            assert payload == {"maintenance_notes": "test"}


class TestCloneMeldOverrides:
    def _patch_clone_io(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_get"),
            patch("cli_anything.propertymeld.http_backend._http_post"),
        )

    def _source(self):
        return {
            "brief_description": "Old title",
            "description": "old long-form",
            "work_category": "PLUMBING",
            "work_location": "Bathroom",
            "priority": "EMERGENCY",
            "tenant_presence_required": True,
            "unit": {"id": 9999},
        }

    def test_long_description_override(self):
        mc_p, mch_p, mcs_p, mget_p, mp_p = self._patch_clone_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mget_p as mget, mp_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mget.return_value = self._source()
            mp.return_value = {"id": 555, "reference_id": "TABCDE"}

            http_backend.clone_meld("90000008", description="new long-form")

            _, payload, _, _ = mp.call_args[0]
            assert payload["description"] == "new long-form"

    def test_no_tenant_presence_required_override(self):
        mc_p, mch_p, mcs_p, mget_p, mp_p = self._patch_clone_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mget_p as mget, mp_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mget.return_value = self._source()
            mp.return_value = {"id": 555, "reference_id": "TABCDE"}

            http_backend.clone_meld("90000008", tenant_presence_required=False)

            _, payload, _, _ = mp.call_args[0]
            assert payload["tenant_presence_required"] is False

    def test_unit_id_override(self):
        mc_p, mch_p, mcs_p, mget_p, mp_p = self._patch_clone_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mget_p as mget, mp_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mget.return_value = self._source()
            mp.return_value = {"id": 555, "reference_id": "TABCDE"}

            http_backend.clone_meld("90000008", unit_id=9000007)

            _, payload, _, _ = mp.call_args[0]
            assert payload["unit"] == {"id": 9000007}

    def test_priority_override(self):
        mc_p, mch_p, mcs_p, mget_p, mp_p = self._patch_clone_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mget_p as mget, mp_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mget.return_value = self._source()
            mp.return_value = {"id": 555, "reference_id": "TABCDE"}

            http_backend.clone_meld("90000008", priority="LOW")

            _, payload, _, _ = mp.call_args[0]
            assert payload["priority"] == "LOW"


_TENANT_FIXTURE = {
    "id": 9000014,
    "user": {"id": 9001, "first_name": "Regina", "last_name": "Moses", "email": "regina@example.com"},
    "contact": {"id": 4001, "home_phone": "+14235550199"},
    "is_active": True,
    "management": 1000,
}


class TestLinkTenantToMeld:
    """PATCH /api/melds/{id}/ with merged tenants array — closes P1 #14."""

    def _patches(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_get"),
            patch("cli_anything.propertymeld.http_backend._http_patch"),
        )

    def _stub_creds(self, mc, mch, mcs):
        mc.return_value = {"cookie": "x"}
        mch.return_value = "Cookie: session=xyz"
        mcs.return_value = "csrf"

    def test_appends_to_existing_tenants_array(self):
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            existing = {"id": 9999, "first_name": "Other", "last_name": "Tenant"}
            mg.side_effect = [
                {"id": "90000011", "tenants": [existing]},  # GET meld
                _TENANT_FIXTURE,                              # GET tenant for hydration
                # verify re-GET (post-PATCH persistence check): tenant now present
                {"id": "90000011", "tenants": [existing, _TENANT_FIXTURE]},
            ]
            mp.return_value = {"id": "90000011", "tenants": [existing, _TENANT_FIXTURE]}

            result = http_backend.link_tenant_to_meld("90000011", 9000014)

            assert result["ok"] is True
            assert result["linked"] is True
            assert result["tenant_id"] == 9000014
            assert result["tenant_count"] == 2
            path, payload, _, _ = mp.call_args[0]
            assert path == "melds/90000011/"
            # Full-echo PATCH: required meld fields must be present (delta 400s in
            # prod, verified live 2026-05-29). A regression to a delta payload
            # (just {"id", "tenants"}) fails these assertions.
            for field in ("brief_description", "work_location", "work_category",
                          "work_type", "priority"):
                assert field in payload, f"full-echo payload missing {field}"
            assert "id" not in payload
            assert len(payload["tenants"]) == 2
            assert _TENANT_FIXTURE in payload["tenants"]
            assert existing in payload["tenants"]

    def test_idempotent_already_linked_short_circuits_patch(self):
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            mg.return_value = {"id": "90000011", "tenants": [_TENANT_FIXTURE]}

            result = http_backend.link_tenant_to_meld("90000011", 9000014)

            assert result["ok"] is True
            assert result.get("already_linked") is True
            assert result.get("linked") is None or result.get("linked") is False
            assert result["tenant_count"] == 1
            mp.assert_not_called()

    def test_idempotent_short_circuits_on_string_tenant_id_from_pm(self):
        """Regression: PM may return an already-linked tenant's id as a STRING.

        The idempotent check must str()-normalize too; otherwise an
        already-linked tenant fails the int-vs-str membership test, skips the
        short-circuit, and falls through to the no-dedup merge — DUPLICATING the
        tenant on the meld. This test puts a STRING id in the existing tenants
        and asserts already_linked with NO PATCH. It fails against the old
        int-only check (which would fire the PATCH).
        """
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            mg.return_value = {
                "id": "90000011", "tenants": [{"id": "9000014"}]
            }

            result = http_backend.link_tenant_to_meld("90000011", 9000014)

            assert result["ok"] is True
            assert result.get("already_linked") is True
            assert result["tenant_count"] == 1
            mp.assert_not_called()

    def test_hits_correct_paths_get_meld_then_get_tenant_then_patch(self):
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            mg.side_effect = [
                {"id": "90000011", "tenants": []},
                _TENANT_FIXTURE,
                # verify re-GET (post-PATCH persistence check) re-hits the meld
                {"id": "90000011", "tenants": [_TENANT_FIXTURE]},
            ]
            mp.return_value = {"id": "90000011", "tenants": [_TENANT_FIXTURE]}

            http_backend.link_tenant_to_meld("90000011", 9000014)

            get_paths = [c.args[0] for c in mg.call_args_list]
            assert get_paths == [
                "melds/90000011/", "tenants/9000014/", "melds/90000011/"
            ]
            patch_path = mp.call_args[0][0]
            assert patch_path == "melds/90000011/"

    def test_handles_missing_tenants_field_on_meld(self):
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            mg.side_effect = [
                {"id": "90000011"},  # no tenants key at all
                _TENANT_FIXTURE,
                # verify re-GET (post-PATCH persistence check): tenant present
                {"id": "90000011", "tenants": [_TENANT_FIXTURE]},
            ]
            mp.return_value = {"id": "90000011", "tenants": [_TENANT_FIXTURE]}

            result = http_backend.link_tenant_to_meld("90000011", 9000014)

            assert result["linked"] is True
            assert result["tenant_count"] == 1
            _, payload, _, _ = mp.call_args[0]
            assert payload["tenants"] == [_TENANT_FIXTURE]

    def test_verify_accepts_string_tenant_id_from_pm(self):
        """Regression: PM may return the persisted tenant id as a STRING.

        The verify block must str()-normalize both sides; otherwise a genuine
        success raises "did NOT persist" on the str-vs-int mismatch. This test
        returns the verify-GET id as a STRING and asserts SUCCESS — it FAILS
        against the old int-only membership check, so green here proves the
        normalization is exercised (not illusory).
        """
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            mg.side_effect = [
                {"id": "90000011", "tenants": []},         # GET meld
                _TENANT_FIXTURE,                            # GET tenant hydration
                # verify re-GET: PM returns the tenant id as a STRING
                {"id": "90000011", "tenants": [{"id": "9000014"}]},
            ]
            mp.return_value = {"id": "90000011", "tenants": [_TENANT_FIXTURE]}

            result = http_backend.link_tenant_to_meld("90000011", 9000014)

            assert result["ok"] is True
            assert result["linked"] is True
            assert result["tenant_id"] == 9000014


class TestGetTenant:
    """GET /api/tenants/{id}/ — tenant hydration helper."""

    def test_get_tenant_hits_correct_path(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mg.return_value = _TENANT_FIXTURE
            result = http_backend.get_tenant(9000014)
            assert result == _TENANT_FIXTURE
            assert mg.call_args[0][0] == "tenants/9000014/"


class TestUpdateUnitNotes:
    """PATCH /api/units/{id}/ with {maintenance_notes: text} — closes P3 #8 (unit-level)."""

    def test_patches_correct_path_with_notes_body(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 9000006, "maintenance_notes": "Water shut-off in basement"}

            result = http_backend.update_unit_notes(9000006, "Water shut-off in basement")

            assert result["ok"] is True
            assert result["unit_id"] == 9000006
            assert result["maintenance_notes"] == "Water shut-off in basement"
            path, payload, _, _ = mp.call_args[0]
            assert path == "units/9000006/"
            assert payload == {"maintenance_notes": "Water shut-off in basement"}

    def test_coerces_string_unit_id_to_int(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"maintenance_notes": "x"}

            result = http_backend.update_unit_notes("9000006", "x")
            assert result["unit_id"] == 9000006  # int, not str
            path, _, _, _ = mp.call_args[0]
            assert path == "units/9000006/"

    def test_empty_notes_clears_field(self):
        """Per pm-capture 2026-05-14T03:07:13, PATCH with empty string clears the field."""
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"maintenance_notes": ""}

            http_backend.update_unit_notes(9000006, "")
            _, payload, _, _ = mp.call_args[0]
            assert payload == {"maintenance_notes": ""}


_TENANT_NOTES_FIXTURE = {
    "id": 9000016,
    "first_name": "Angelica",
    "last_name": "Acevedo",
    "middle_name": "",
    "notes": "",
    "is_active": True,
    "management": 1000,
    "user": {"id": 9000008, "email": "a@example.com"},
    "contact": {"id": 9000024, "cell_phone": "(706) 913-7178"},
    "leases": [{"unit_id": 9000005}],
}


_TENANT_CONTACT_FIXTURE = {
    "id": 9000023,
    "user": {
        "id": 9000001,
        "email": "alex@example.com",
        "first_name": "Alex",
        "last_name": "Example",
        "last_active_at": "2026-05-31T03:00:11.803163Z",
        "last_active_channel": "DIGITAL",
        "last_login": "2026-05-29T11:42:27.648293Z",
    },
    "contact": {
        "id": 9000025,
        "home_phone": "(678) 987-3214",
        "cell_phone": "(678) 923-5467",
        "business_phone": "",
        "created": "2026-05-31T02:59:07.878312Z",
        "create_by": {"org_type": "m", "persona_id": 5011},
        "updated": "2026-05-31T02:59:07.878360Z",
        "update_by": {"org_type": "m", "persona_id": 5011},
        "tenant_objs": [1000],
        "home_phone_ext": "",
        "cell_phone_ext": "",
        "business_phone_ext": "",
        "primary_email": "alex@example.com",
        "secondary_email": "alex@example.com",
        "tertiary_email": "",
    },
    "invited": True,
    "last_invite": {
        "created": "2026-05-31T02:59:07.939013Z",
        "email": "alex@example.com",
        "id": 90000019,
    },
    "created": "2026-05-31T02:59:07.891911Z",
    "create_by": {"org_type": "m", "persona_id": 5011},
    "updated": "2026-05-31T02:59:10.803742Z",
    "update_by": {"org_type": "m", "persona_id": 5011},
    "is_active": True,
    "first_name": "Alex",
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


class TestUpdateTenantNotes:
    """PATCH /api/tenants/{id}/ with full body, mutating only `notes` — closes
    P3 #8 (resident-level). Verified shape from pm-tenant-notes-endpoint-capture-2026-05-18.
    """

    def _wire(self, mc, mch, mcs, mg, mp, *, get_returns, patch_returns):
        mc.return_value = {"cookie": "x"}
        mch.return_value = "Cookie: session=xyz"
        mcs.return_value = "csrf"
        mg.return_value = get_returns
        mp.return_value = patch_returns

    def test_get_then_patch_full_body_with_notes_mutated(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            self._wire(
                mc, mch, mcs, mg, mp,
                get_returns=dict(_TENANT_NOTES_FIXTURE),
                patch_returns={**_TENANT_NOTES_FIXTURE, "notes": "Access after 3pm"},
            )

            result = http_backend.update_tenant_notes(9000016, "Access after 3pm")

            assert result["ok"] is True
            assert result["tenant_id"] == 9000016
            assert result["notes"] == "Access after 3pm"
            # GET path verified
            assert mg.call_args[0][0] == "tenants/9000016/"
            # PATCH path + full-body echo verified
            patch_path, patch_payload, _, _ = mp.call_args[0]
            assert patch_path == "tenants/9000016/"
            # Full body sent — not a thin patch
            assert patch_payload["first_name"] == "Angelica"
            assert patch_payload["last_name"] == "Acevedo"
            assert patch_payload["id"] == 9000016
            assert patch_payload["contact"]["cell_phone"] == "(706) 913-7178"
            # Notes was mutated
            assert patch_payload["notes"] == "Access after 3pm"

    def test_coerces_string_tenant_id_to_int(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            self._wire(
                mc, mch, mcs, mg, mp,
                get_returns=dict(_TENANT_NOTES_FIXTURE),
                patch_returns={**_TENANT_NOTES_FIXTURE, "notes": "x"},
            )
            result = http_backend.update_tenant_notes("9000016", "x")
            assert result["tenant_id"] == 9000016  # int, not str
            assert mg.call_args[0][0] == "tenants/9000016/"
            assert mp.call_args[0][0] == "tenants/9000016/"

    def test_empty_notes_clears_field(self):
        """Empty string is a valid value — represents a deliberate clear."""
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            self._wire(
                mc, mch, mcs, mg, mp,
                get_returns={**_TENANT_NOTES_FIXTURE, "notes": "old value"},
                patch_returns={**_TENANT_NOTES_FIXTURE, "notes": ""},
            )
            http_backend.update_tenant_notes(9000016, "")
            _, payload, _, _ = mp.call_args[0]
            assert payload["notes"] == ""

    def test_preserves_other_fields_from_get(self):
        """The whole point of GET-mutate-PATCH is that fields we didn't touch
        survive — validators run on the full payload, so dropping fields is
        what made thin-patch return 400 in the live capture."""
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            self._wire(
                mc, mch, mcs, mg, mp,
                get_returns=dict(_TENANT_NOTES_FIXTURE),
                patch_returns={**_TENANT_NOTES_FIXTURE, "notes": "n"},
            )
            http_backend.update_tenant_notes(9000016, "n")
            _, payload, _, _ = mp.call_args[0]
            # Every non-notes key from the GET response must be in the PATCH body
            for key in _TENANT_NOTES_FIXTURE:
                if key == "notes":
                    continue
                assert key in payload, f"PATCH body dropped {key!r} — thin-patch will 400"

    def test_raises_when_get_returns_non_dict(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = "not a dict — unexpected api response"
            import pytest
            with pytest.raises(RuntimeError, match="non-dict"):
                http_backend.update_tenant_notes(9000016, "x")
            # We must not have attempted a PATCH if GET was malformed
            assert mp.call_count == 0


class TestUpdateTenantContact:
    """PUT /api/tenants/{id}/ with full body, mutating nested contact fields.

    Covers NEW-2 / tenant-PUT-contact-edit-200 from the 2026-05-31 HAR capture.
    """

    def _wire(self, mc, mch, mcs, mg, mp, *, get_returns, put_returns):
        mc.return_value = {"cookie": "x"}
        mch.return_value = "Cookie: session=xyz"
        mcs.return_value = "csrf"
        mg.return_value = get_returns
        mp.return_value = put_returns

    def test_get_then_put_full_body_with_contact_fields_mutated(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_put") as mp, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mpatch:
            get_returns = json.loads(json.dumps(_TENANT_CONTACT_FIXTURE))
            put_returns = json.loads(json.dumps(_TENANT_CONTACT_FIXTURE))
            put_returns["contact"]["cell_phone"] = "(678) 923-7654"
            put_returns["contact"]["home_phone"] = "(678) 987-4123"
            put_returns["contact"]["business_phone"] = "(423) 654-1234"
            self._wire(mc, mch, mcs, mg, mp, get_returns=get_returns, put_returns=put_returns)

            result = http_backend.edit_tenant_contact(
                9000023,
                cell_phone="(678) 923-7654",
                home_phone=" (678) 987-4123",
                business_phone="4236541234",
            )

            assert result["ok"] is True
            assert result["tenant_id"] == 9000023
            assert result["contact"]["business_phone"] == "(423) 654-1234"
            assert result["updated_fields"] == ["business_phone", "cell_phone", "home_phone"]
            assert mg.call_args[0][0] == "tenants/9000023/"
            put_path, put_payload, _, _ = mp.call_args[0]
            assert put_path == "tenants/9000023/"
            assert mpatch.call_count == 0
            assert put_payload["id"] == 9000023
            assert put_payload["first_name"] == "Alex"
            assert put_payload["notes"] == "notes section"
            assert put_payload["user"]["email"] == "alex@example.com"
            assert put_payload["contact"]["cell_phone"] == "(678) 923-7654"
            assert put_payload["contact"]["home_phone"] == " (678) 987-4123"
            assert put_payload["contact"]["business_phone"] == "4236541234"
            assert put_payload["contact"]["primary_email"] == "alex@example.com"

    def test_mutates_email_fields_and_preserves_other_tenant_keys(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_put") as mp:
            get_returns = json.loads(json.dumps(_TENANT_CONTACT_FIXTURE))
            put_returns = json.loads(json.dumps(_TENANT_CONTACT_FIXTURE))
            put_returns["contact"]["primary_email"] = "primary@example.com"
            put_returns["contact"]["secondary_email"] = ""
            self._wire(mc, mch, mcs, mg, mp, get_returns=get_returns, put_returns=put_returns)

            http_backend.edit_tenant_contact(
                "9000023",
                primary_email="primary@example.com",
                secondary_email="",
            )

            assert mg.call_args[0][0] == "tenants/9000023/"
            assert mp.call_args[0][0] == "tenants/9000023/"
            _, payload, _, _ = mp.call_args[0]
            for key in _TENANT_CONTACT_FIXTURE:
                assert key in payload, f"PUT body dropped {key!r}"
            assert payload["contact"]["primary_email"] == "primary@example.com"
            assert payload["contact"]["secondary_email"] == ""
            assert payload["contact"]["tertiary_email"] == ""
            assert payload["contact"]["cell_phone"] == "(678) 923-5467"

    def test_requires_at_least_one_field(self):
        with pytest.raises(ValueError, match="at least one"):
            http_backend.edit_tenant_contact(9000023)

    def test_raises_when_contact_missing(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_put") as mp:
            get_returns = json.loads(json.dumps(_TENANT_CONTACT_FIXTURE))
            get_returns["contact"] = None
            self._wire(mc, mch, mcs, mg, mp, get_returns=get_returns, put_returns={})
            with pytest.raises(RuntimeError, match="contact object"):
                http_backend.edit_tenant_contact(9000023, cell_phone="4235550100")
            assert mp.call_count == 0
