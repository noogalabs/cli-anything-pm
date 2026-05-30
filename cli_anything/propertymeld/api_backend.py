"""
Property Meld Nexus API backend.
Uses OAuth2 client credentials (PM_CLIENT_ID, PM_CLIENT_SECRET).
All reads go through this backend. Writes are NOT supported by the API (use browser_backend).

Endpoint notes:
  - Work orders: GET /api/v2/meld/ (singular, NOT /melds/)
  - Properties: GET /api/v2/property/
  - Vendors: GET /api/v2/vendor/
  - X-Multitenant-Id header required on all requests.
"""
import json
import ssl
import sys
import urllib.request
from typing import Any, Optional

from .http_backend import _validate_meld_id
from .utils import API_BASE, MULTITENANT_ID, UA, get_token, print_error


def _api_get(path: str, params: Optional[dict] = None) -> Any:
    """Make authenticated GET request to Nexus API."""
    import urllib.parse

    token = get_token()
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Multitenant-Id": MULTITENANT_ID,
            "User-Agent": UA,
            "Accept": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Surface the response body so DRF validation errors like
        # `{"status":["Select a valid choice. open is not one of the available choices."]}`
        # actually reach the operator. Previously the body was discarded and
        # callers saw only "API error 400: Bad Request", which masked the
        # exact lowercase-vs-UPPER_CASE_SNAKE_CASE enum bug fixed in 901d1f4.
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        from .utils import normalize_http_error
        try:
            detail = normalize_http_error(e.code, body)
        except Exception:
            detail = {"error": f"API error {e.code}: {e.reason}", "status_code": e.code}
        print(json.dumps(detail), file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print_error(f"Network error: {e.reason}")
        sys.exit(1)


def list_work_orders(
    status: Optional[str] = None,
    assigned_to_tech: Optional[int] = None,
    assigned_to_vendor: Optional[int] = None,
    stuck_hours: Optional[float] = None,
    created_since: Optional[str] = None,
    status_not: Optional[str] = None,
    no_tenant_linked: bool = False,
    include_tech: bool = False,
    limit: int = 25,
) -> list:
    """List work orders, optionally filtered by status.

    PM Nexus accepts UPPER_CASE_SNAKE_CASE values for the `status` filter and
    rejects anything else (HTTP 400 "Select a valid choice"). The valid set
    observed via Nexus introspection on tenant 3287:

        PENDING_ASSIGNMENT
        PENDING_VENDOR
        PENDING_MORE_MANAGEMENT_AVAILABILITY
        COMPLETED
        MANAGER_CANCELED

    The CLI exposes friendlier slugs ("open", "pending", "completed",
    "canceled"). "open" maps to ALL three PENDING_* states sent as repeated
    `status=` query params, which Nexus interprets as a logical OR.

    --no-tenant-linked routing: PM's Nexus server-side filter uses the wrong
    predicate (`has_registered_tenant=False` instead of `len(tenants)==0`),
    returning false positives for melds whose tenant simply hasn't registered
    for the PM portal. Nexus list response also OMITS the tenants[] field so
    we can't post-filter client-side here. When this flag is set we delegate
    to cookie-path `/api/melds/` via http_backend.list_work_orders_rich (which
    returns the tenants[] field) and filter on `not r.get("tenants")`. Remove
    the delegation once PM fixes the server-side predicate.
    """
    # Delegate to cookie-path when we need the tenants[] field that Nexus omits.
    # The cookie-path /api/melds/ list endpoint does NOT honor the Nexus
    # query filter set (assigned_to_*, stuck_hours, created_since, status_not).
    # Silently dropping those filters when --no-tenant-linked is combined with
    # them returns wrong-meld sets to the caller — a silent-failure-half-ship
    # per locked rule (feedback_no_silent_failure_half_ships, 2026-05-23).
    # Reject the combo loudly until v1.1 wires client-side filtering on the
    # cookie response. AM-sweep case (--no-tenant-linked + status alone) ships
    # clean; combo callers get a clear error and choose to drop the conflict
    # or wait for the full-feature fix.
    #
    # Over-fetch heuristic: max(limit*4, 100). Sized for typical AM-sweep where
    # truly-empty melds are ~10-20% of the open list. For atypical workloads
    # (e.g. limit=500 + low FP ratio) the post-filter can silently truncate
    # without surfacing 'result may be incomplete'. Switch to _paginate_all in
    # http_backend.list_work_orders_rich if a real consumer needs accurate
    # full-corpus filtering past the 100-per-page cap.
    #
    # `tenants` field can be a list, None, or missing — truthy-check `not
    # r.get("tenants")` treats all three as 'no tenant linked' so a malformed
    # response can't silently slip a real meld past the filter.
    if no_tenant_linked:
        incompatible = {
            "--assigned-to-tech": assigned_to_tech,
            "--assigned-to-vendor": assigned_to_vendor,
            "--stuck-hours": stuck_hours,
            "--created-since": created_since,
            "--status-not": status_not,
        }
        set_flags = [name for name, val in incompatible.items() if val is not None]
        if set_flags:
            print_error(
                "--no-tenant-linked cannot be combined with "
                f"{', '.join(set_flags)} yet — the cookie-path delegation "
                "drops Nexus query params. Drop the conflicting flag(s) or "
                "use --no-tenant-linked alone (status + limit still apply)."
            )
            sys.exit(2)

        from . import http_backend
        rich = http_backend.list_work_orders_rich(limit=max(limit * 4, 100), status=status)
        filtered = [r for r in rich if not r.get("tenants")]
        return filtered[:limit]

    results = _list_work_orders_nexus(
        status=status,
        assigned_to_tech=assigned_to_tech,
        assigned_to_vendor=assigned_to_vendor,
        stuck_hours=stuck_hours,
        created_since=created_since,
        status_not=status_not,
        limit=limit,
    )
    if include_tech:
        from . import http_backend
        detail_fetcher = http_backend.get_work_order_rich
        try:
            rich = http_backend.list_work_orders_rich(limit=max(limit, 100), status=status)
        except (Exception, SystemExit) as exc:
            _warn_include_tech_unavailable(f"cookie list fetch failed: {exc}")
            rich = []
            detail_fetcher = lambda _meld_id: {}
        else:
            if results and not rich:
                _warn_include_tech_unavailable("cookie list returned no rows")
        _merge_assignment_fields(results, rich, detail_fetcher)
    return results


def _list_work_orders_nexus(
    status: Optional[str] = None,
    assigned_to_tech: Optional[int] = None,
    assigned_to_vendor: Optional[int] = None,
    stuck_hours: Optional[float] = None,
    created_since: Optional[str] = None,
    status_not: Optional[str] = None,
    limit: int = 25,
) -> list:
    """List work orders through Nexus only."""
    params: list[tuple[str, str]] = [("limit", str(limit))]
    if status:
        slug_to_states = {
            "open": [
                "PENDING_ASSIGNMENT",
                "PENDING_VENDOR",
                "PENDING_MORE_MANAGEMENT_AVAILABILITY",
            ],
            "pending": ["PENDING_VENDOR"],
            "completed": ["COMPLETED"],
            "canceled": ["MANAGER_CANCELED"],
        }
        states = slug_to_states.get(status.lower(), [status])
        for s in states:
            params.append(("status", s))
    if assigned_to_tech is not None:
        params.append(("assigned_to_tech", str(assigned_to_tech)))
    if assigned_to_vendor is not None:
        params.append(("assigned_to_vendor", str(assigned_to_vendor)))
    if stuck_hours is not None:
        params.append(("stuck_hours", str(stuck_hours)))
    if created_since:
        params.append(("created_since", created_since))
    if status_not:
        params.append(("status_not", status_not))

    data = _api_get("/meld/", params)
    results = data.get("results", data) if isinstance(data, dict) else data
    return results


_ASSIGNMENT_FIELDS = (
    "in_house_servicers",
    "managementappointment",
)


def _merge_assignment_fields(
    base_results: list[dict],
    rich_results: list[dict],
    detail_fetcher,
) -> None:
    """Merge cookie-path assignment fields into Nexus-shaped work orders."""
    rich_by_id = {str(r.get("id")): r for r in rich_results if isinstance(r, dict)}
    for item in base_results:
        meld_id = str(item.get("id"))
        rich = rich_by_id.get(meld_id)
        if rich is None and meld_id and meld_id != "None":
            try:
                rich = detail_fetcher(meld_id)
            except (Exception, SystemExit) as exc:
                _warn_include_tech_unavailable(
                    f"cookie detail fetch failed for meld {meld_id}: {exc}"
                )
                rich = {}
        for field in _ASSIGNMENT_FIELDS:
            if item.get(field):
                continue
            item[field] = (rich or {}).get(field) or []


def _warn_include_tech_unavailable(reason: str) -> None:
    print(
        "Warning: --include-tech could not verify cookie-path in-house tech "
        f"fields ({reason}); empty in_house_servicers may mean unavailable "
        "cookie data, not no tech assigned.",
        file=sys.stderr,
    )


def get_work_order(meld_id: str, include_tech: bool = False) -> dict:
    """Get a single work order by ID."""
    meld_id = str(_validate_meld_id(meld_id))
    result = _api_get(f"/meld/{meld_id}/")
    if include_tech:
        from . import http_backend
        try:
            rich = http_backend.get_work_order_rich(meld_id)
        except (Exception, SystemExit) as exc:
            _warn_include_tech_unavailable(
                f"cookie detail fetch failed for meld {meld_id}: {exc}"
            )
            rich = {}
        for field in _ASSIGNMENT_FIELDS:
            result[field] = rich.get(field) or []
    return result


def list_properties(limit: int = 100) -> list:
    """List properties up to `limit`, walking DRF `next` link pagination.

    Previous behavior returned only the first page (`limit` capped at the
    server-side page size, ~100). When the underlying record set was larger
    than 100, the help text "List all properties" silently lied. Now: walk
    `next` until we have `limit` items or the chain ends.
    """
    return _paginate_until("/property/", limit)


def list_vendors(limit: int = 100) -> list:
    """List vendors up to `limit`. See list_properties for pagination notes."""
    return _paginate_until("/vendor/", limit)


def _paginate_until(path: str, limit: int) -> list:
    """Walk DRF `next` links until `limit` items collected or chain exhausted."""
    page_size = max(1, min(limit, 100))
    next_path: Optional[str] = path
    params: Optional[dict] = {"limit": page_size}
    results: list = []
    while next_path and len(results) < limit:
        data = _api_get(next_path, params)
        page_items = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(page_items, list):
            return page_items if isinstance(page_items, list) else []
        results.extend(page_items)
        if not isinstance(data, dict):
            break
        raw_next = data.get("next")
        if not raw_next:
            break
        if "/api/v2" in raw_next:
            next_path = raw_next.split("/api/v2", 1)[1]
            params = None  # the `next` URL already has limit + cursor baked in
        else:
            break
    return results[:limit]


def probe() -> dict:
    """Health check — verify API is reachable and credentials work."""
    try:
        token = get_token()
        return {"ok": True, "token_prefix": token[:8] + "..."}
    except SystemExit:
        return {"ok": False, "error": "Authentication failed"}
