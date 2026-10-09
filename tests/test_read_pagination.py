"""Synthetic read completeness controls. Never load a real PM session."""
from urllib.parse import parse_qs, urlsplit

import pytest

from cli_anything.propertymeld import api_backend as api, http_backend as hb
from cli_anything.propertymeld.pagination import PaginationError, collect

BASE = "https://synthetic.invalid/9000001/m/9000001/api/"
MELD = "900001"


@pytest.fixture(autouse=True)
def no_wire(monkeypatch):
    monkeypatch.setattr(hb.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("unexpected network"))
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda _: "fake-session")
    monkeypatch.setattr(hb, "_build_url", lambda path: BASE + path)


def page(items, count=None, next=None):
    return dict(results=items, count=count, next=next)


def test_three_pages_provenance_and_missing_count():
    calls = []
    pages = {
        "comments/?limit=100": page([{"id": 1}], next="?cursor=second"),
        "comments/?cursor=second": page([{"id": 2}], next=BASE + "comments/?cursor=third"),
        "comments/?cursor=third": page([{"id": 3}]),
    }
    result = collect("comments/?limit=100", lambda path: calls.append(path) or pages[path], base=BASE, require_complete=True)
    assert len(calls) == 3 and [row["id"] for row in result["results"]] == [1, 2, 3]
    assert result["count"] is None and result["complete"] and result["pages"] == 3
    assert result["basis"] == "declared_next_chain"


@pytest.mark.parametrize("next,code", [
    ("https://outside.invalid/1000/m/1000/api/comments/?cursor=secret", "unsafe_next"),
    ("//outside.invalid/1000/m/1000/api/comments/", "unsafe_next"),
    (BASE + "melds/?cursor=second", "unsafe_next"),
    ("https://user:password@synthetic.invalid/9000001/m/9000001/api/comments/", "unsafe_next"),
    (BASE + "comments/#fragment", "unsafe_next"),
    ("https://[bad/", "invalid_next"),
    (7, "invalid_next"), (False, "invalid_next"), ("", "invalid_next"),
])
def test_bad_next_refuses_before_another_fetch(next, code):
    calls = []
    with pytest.raises(PaginationError, match=code):
        collect("comments/", lambda path: calls.append(path) or page([{"id": 1}], next=next), base=BASE, require_complete=True)
    assert calls == ["comments/"]


@pytest.mark.parametrize("initial,next", [
    ("comments/?meld=900001&limit=100", "?meld=900002&cursor=b"),
    ("comments/?meld=900001&limit=100", "?cursor=b"),
    ("melds/?status=A&status=B", "?status=A&cursor=b"),
    ("melds/?status=A&status=B", "?status=C&status=B&cursor=b"),
    ("melds/?status=A&status=B", "?cursor=b"),
    ("melds/?created_since=2026-01-01&status_not=C", "?created_since=2026-01-01&cursor=b"),
    ("melds/", "?new_filter=unexpected&cursor=b"),
])
def test_next_filter_scope_bound_before_get(initial, next):
    calls = []
    with pytest.raises(PaginationError, match="filter_scope_changed"):
        collect(initial, lambda path: calls.append(path) or page([{"id": 1}], next=next), base=BASE, require_complete=True)
    assert calls == [initial]


def test_repeated_filters_can_reorder_but_not_change():
    iterator = iter([page([{"id": 1}], 2, "?status=B&status=A&cursor=b&limit=1"), page([{"id": 2}], 2)])
    result = collect("melds/?status=A&status=B&limit=100", lambda _: next(iterator), base=BASE, require_complete=True)
    assert result["complete"] and result["returned"] == 2


@pytest.mark.parametrize("pages,code", [
    ([page([{"id": 1}], 2, "?cursor=b"), page([{"id": 2}], 3)], "count_changed"),
    ([page([{"id": 1}], 2)], "truncated_count"),
    ([page([{"id": 1}, {"id": 2}], 1)], "count_overrun"),
    ([page([], True)], "invalid_count"),
    ([page(["not-a-row"])], "invalid_result_row"),
    ([{"error": "denied"}], "invalid_page"),
    ([page([{"id": 1}], 2, "?cursor=b"), page([{"id": 1}], 2)], "duplicate_page"),
    ([page([{"id": 1, "text": "a"}], 2, "?cursor=b"), page([{"id": 1, "text": "b"}], 2)], "duplicate_id"),
])
def test_invalid_chain_never_returns_partial(pages, code):
    iterator = iter(pages)
    with pytest.raises(PaginationError, match=code):
        collect("comments/", lambda _: next(iterator), base=BASE, require_complete=True)


def test_cap_and_canonical_cycle():
    calls = []
    with pytest.raises(PaginationError, match="page_cap"):
        collect("comments/", lambda path: calls.append(path) or page([{"id": len(calls)}], next="?cursor=" + str(len(calls))),
                base=BASE, require_complete=True, max_pages=2)
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(PaginationError, match="cycle"):
        collect("comments/?a=1&b=2", lambda path: calls.append(path) or page([{"id": 1}], next="?b=2&a=1"), base=BASE, require_complete=True)
    assert len(calls) == 1


@pytest.mark.parametrize("body", [[{"id": 1}], {"results": [{"id": 1}]}])
def test_opaque_payload_legacy_rows_are_not_complete(body):
    result = collect("comments/", lambda _: body, base=BASE)
    assert result["results"] == [{"id": 1}] and result["complete"] is False and result["count"] is None
    with pytest.raises(PaginationError, match="completion_unproven"):
        collect("comments/", lambda _: body, base=BASE, require_complete=True)


def test_count_can_prove_terminal_payload_without_next():
    result = collect("comments/", lambda _: {"results": [{"id": 1}], "count": 1}, base=BASE, require_complete=True)
    assert result["complete"] and result["basis"] == "count_verified"


@pytest.mark.parametrize("function,resource", [(hb.get_comments, "comments/"), (hb.list_work_entries, "melds/900001/work-entries/")])
def test_child_reads_exhaust_past_100_and_preserve_legacy_list(monkeypatch, function, resource):
    calls = []
    def fetch(path, cookie):
        calls.append(path)
        if "cursor" not in path:
            query = "?meld=900001&cursor=b" if resource == "comments/" else "?cursor=b"
            return page([{"id": value} for value in range(100)], 101, BASE + resource + query)
        return page([{"id": 100}], 101)
    monkeypatch.setattr(hb, "_http_get", fetch)
    assert len(function(MELD)) == 101
    calls.clear()
    result = function(MELD, complete=True)
    assert result["count"] == result["returned"] == 101 and result["pages"] == 2 and len(calls) == 2


def test_files_ids_are_scoped_by_role(monkeypatch):
    def fetch(path, cookie):
        # The same primary key exists in three independent source tables.
        return page([{"id": 1, "filename": "synthetic.jpg"}], 1)
    monkeypatch.setattr(hb, "_http_get", fetch)
    result = hb.list_files(MELD, complete=True)
    assert result["count"] == result["returned"] == 3
    assert {(row["uploader_role"], row["id"]) for row in result["results"]} == {("manager", 1), ("tenant", 1), ("vendor", 1)}
    assert set(result["resources"]) == {"manager", "tenant", "vendor"}


def test_files_partial_source_fails_whole_collection(monkeypatch):
    def fetch(path, cookie):
        return page([], 1 if "tenant-files" in path else 0)
    monkeypatch.setattr(hb, "_http_get", fetch)
    with pytest.raises(PaginationError, match="truncated_count"):
        hb.list_files(MELD, complete=True)


def test_complete_list_ignores_limit_and_preserves_repeated_status(monkeypatch):
    calls = []
    def fetch(path, params=None):
        calls.append(path)
        if "cursor" not in path:
            query = parse_qs(urlsplit(path).query)
            assert query["limit"] == ["100"]
            assert query["status"] == ["PENDING_ASSIGNMENT", "PENDING_VENDOR", "PENDING_MORE_MANAGEMENT_AVAILABILITY"]
            return page([{"id": 1}], 2, api.API_BASE + "/meld/?cursor=b&status=PENDING_ASSIGNMENT&status=PENDING_VENDOR&status=PENDING_MORE_MANAGEMENT_AVAILABILITY")
        return page([{"id": 2}], 2)
    monkeypatch.setattr(api, "_api_get", fetch)
    result = api.list_work_orders(status="open", limit=1, complete=True)
    assert len(calls) == 2 and result["returned"] == result["count"] == 2 and result["scope"] == "meld_list_only"


def test_client_filter_runs_after_exhausting_cookie_pages(monkeypatch):
    calls = []
    def fetch(path, cookie):
        calls.append(path)
        if "cursor" not in path:
            return page([{"id": 1, "tenants": [{"id": 10}]}], 2, BASE + "melds/?cursor=b")
        return page([{"id": 2, "tenants": []}], 2)
    monkeypatch.setattr(hb, "_http_get", fetch)
    result = api.list_work_orders(no_tenant_linked=True, limit=1, complete=True)
    assert len(calls) == 2 and [row["id"] for row in result["results"]] == [2]
    assert result["source_count"] == result["source_returned"] == 2 and result["count"] == 1


@pytest.mark.parametrize("options,row,code", [
    ({"no_tenant_linked": True}, {"id": 1}, "tenant_filter_data_missing"),
    ({"assigned_to_vendor": 1}, {"id": 1}, "vendor_filter_data_missing"),
])
def test_missing_client_filter_denominator_refuses(monkeypatch, options, row, code):
    monkeypatch.setattr(hb, "_http_get", lambda *a: page([row], 1))
    with pytest.raises(PaginationError, match=code):
        api.list_work_orders(complete=True, **options)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_bad_stuck_hours_refuses_before_http(monkeypatch, value):
    monkeypatch.setattr(hb, "_http_get", lambda *a: pytest.fail("must refuse before GET"))
    with pytest.raises(PaginationError, match="invalid_stuck_hours"):
        api.list_work_orders(complete=True, stuck_hours=value)


def test_cookie_complete_keeps_incompatible_filters_refused(monkeypatch):
    monkeypatch.setattr(hb, "_http_get", lambda *a: pytest.fail("must refuse before GET"))
    with pytest.raises(PaginationError, match="unsupported_filter_combination"):
        api.list_work_orders(complete=True, no_tenant_linked=True, created_since="2026-01-01")


def install_detail(monkeypatch):
    detail = {"id": int(MELD), "maintenance_notes": "synthetic maintenance", "completion_notes": None}
    monkeypatch.setattr(api, "_api_get", lambda *_: dict(detail))
    def fetch(path, cookie):
        if path == "melds/900001/":
            return dict(detail)
        return page([{"id": 1, "description": "synthetic note"}], 1)
    monkeypatch.setattr(hb, "_http_get", fetch)


def test_parent_get_individually_certifies_resources(monkeypatch):
    install_detail(monkeypatch)
    result = api.get_work_order_complete(MELD)
    assert result["result"]["id"] == int(MELD) and result["complete"] is True
    assert set(result["resources"]) == {"meld", "notes", "comments", "files", "work_entries"}
    assert result["resources"]["files"]["count"] == 3
    assert result["resources"]["comments"]["count"] == result["resources"]["work_entries"]["count"] == 1
    assert all(value["resource"]["meld_id"] == int(MELD) for value in result["resources"].values())
    assert result["resources"]["files"]["resources"]["tenant"]["resource"]["role"] == "tenant"


def test_parent_child_failure_is_not_a_marker_success(monkeypatch):
    install_detail(monkeypatch)
    monkeypatch.setattr(hb, "list_work_entries", lambda *a, **kw: (_ for _ in ()).throw(PaginationError("truncated_count")))
    with pytest.raises(PaginationError, match="truncated_count"):
        api.get_work_order_complete(MELD)


def test_notes_use_existing_resources_only(monkeypatch):
    install_detail(monkeypatch)
    result = hb.get_notes(MELD, complete=True)
    assert set(result["resources"]) == {"notes", "comments", "work_entries"}
    assert result["notes"] == {"maintenance_notes": "synthetic maintenance", "completion_notes": None}


def test_missing_note_fields_cannot_be_certified(monkeypatch):
    monkeypatch.setattr(hb, "_http_get", lambda *a: {"id": int(MELD)})
    with pytest.raises(PaginationError, match="notes_fields_unavailable"):
        hb.get_notes(MELD, complete=True)


def test_no_notes_requery_when_parent_already_has_notes(monkeypatch):
    install_detail(monkeypatch)
    original = hb._http_get
    calls = []
    monkeypatch.setattr(hb, "_http_get", lambda path, cookie: calls.append(path) or original(path, cookie))
    api.get_work_order_complete(MELD)
    assert calls.count("melds/900001/") == 1
    assert sum(path.startswith("comments/") for path in calls) == 1
    assert sum("work-entries/" in path for path in calls) == 1


@pytest.mark.parametrize("read", [
    lambda: hb.get_comments(MELD, complete=True),
    lambda: hb.list_files(MELD, complete=True),
    lambda: hb.list_work_entries(MELD, complete=True),
    lambda: hb.get_notes(MELD, complete=True),
    lambda: hb.get_work_order_rich(MELD, complete=True),
    lambda: api.get_work_order_complete(MELD),
    lambda: api.list_work_orders(no_tenant_linked=True, complete=True),
    lambda: api.list_work_orders(include_tech=True, complete=True),
])
def test_complete_session_expiry_never_launches_helper(monkeypatch, read):
    monkeypatch.setattr(api, "_api_get", lambda *a: {"id": int(MELD)} if urlsplit(a[0]).path == f"/meld/{MELD}/" else page([{"id": int(MELD)}], 1))
    calls = []
    def expired(*args):
        calls.append(args)
        raise hb.SessionExpired(hb.urllib.error.HTTPError("https://synthetic.invalid/", 401, "expired", {}, None))
    monkeypatch.setattr(hb, "_http_get", expired)
    monkeypatch.setattr(hb, "_attempt_recapture", lambda: pytest.fail("complete read must not recapture"))
    monkeypatch.setattr(hb.subprocess, "run", lambda *a, **kw: pytest.fail("complete read must not launch a helper"))
    with pytest.raises(PaginationError, match="session_expired"):
        read()
    assert len(calls) == 1


def test_legacy_comments_preserve_existing_recapture_behavior(monkeypatch):
    calls = []
    def fetch(*args):
        calls.append(args)
        if len(calls) == 1:
            raise hb.SessionExpired(hb.urllib.error.HTTPError("https://synthetic.invalid/", 401, "expired", {}, None))
        return page([{"id": 1}], 1)
    recaptures = []
    monkeypatch.setattr(hb, "_http_get", fetch)
    monkeypatch.setattr(hb, "_attempt_recapture", lambda: recaptures.append(True) or True)
    assert hb.get_comments(MELD) == [{"id": 1}]
    assert len(calls) == 2 and recaptures == [True]
