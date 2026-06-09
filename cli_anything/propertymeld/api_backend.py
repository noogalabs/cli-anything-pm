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
from datetime import datetime, timezone
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
    status_raw: Optional[str] = None,
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
        MAINTENANCE_COULD_NOT_COMPLETE (raw-only via status_raw)

    The CLI exposes friendlier slugs ("open", "pending", "completed",
    "canceled"). "open" maps to ALL three PENDING_* states sent as repeated
    `status=` query params, which Nexus interprets as a logical OR.
    `status_raw` bypasses that slug map and sends one raw PM status directly.

    --no-tenant-linked routing: PM's Nexus server-side filter uses the wrong
    predicate (`has_registered_tenant=False` instead of `len(tenants)==0`),
    returning false positives for melds whose tenant simply hasn't registered
    for the PM portal. Nexus list response also OMITS the tenants[] field so
    we can't post-filter client-side here. When this flag is set we delegate
    to cookie-path `/api/melds/` via http_backend.list_work_orders_rich (which
    returns the tenants[] field) and filter on `not r.get("tenants")`. Remove
    the delegation once PM fixes the server-side predicate.
    """
    if status and status_raw:
        print_error("--status and --status-raw cannot be combined")
        sys.exit(2)

    if assigned_to_tech is not None:
        print_error(
            "--assigned-to-tech is not supported on `work-orders list` yet: "
            "PM Nexus ignores the list param and Nexus list rows do not expose "
            "assigned_technicians. Use --include-tech with an unfiltered list "
            "or wait for the gated detail-fetch/server-param follow-up."
        )
        sys.exit(2)

    needs_cookie_filter = (
        no_tenant_linked
        or assigned_to_vendor is not None
        or stuck_hours is not None
    )
    if needs_cookie_filter:
        incompatible = {
            "--created-since": created_since,
            "--status-raw": status_raw,
            "--status-not": status_not,
        }
        set_flags = [name for name, val in incompatible.items() if val is not None]
        if set_flags:
            print_error(
                f"{', '.join(set_flags)} cannot be combined with "
                "--assigned-to-vendor, --stuck-hours, or --no-tenant-linked yet: "
                "those filters use the cookie list path, which does not honor "
                "created_since/status_not. Refusing instead of returning a "
                "partially filtered list."
            )
            sys.exit(2)

        from . import http_backend
        fetch_limit = max(limit * 4, 100)
        rich = http_backend.list_work_orders_rich(limit=fetch_limit, status=status)
        filtered = _filter_work_orders_rich(
            rich,
            assigned_to_vendor=assigned_to_vendor,
            stuck_hours=stuck_hours,
            no_tenant_linked=no_tenant_linked,
        )
        if len(rich) >= fetch_limit:
            print(
                "Warning: result may be incomplete; cookie list page cap was "
                "reached before client-side filters exhausted all matches.",
                file=sys.stderr,
            )
        return filtered[:limit]

    results = _list_work_orders_nexus(
        status=status,
        status_raw=status_raw,
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


def _filter_work_orders_rich(
    rows: list[dict],
    *,
    assigned_to_vendor: Optional[int] = None,
    stuck_hours: Optional[float] = None,
    no_tenant_linked: bool = False,
) -> list[dict]:
    now = datetime.now(timezone.utc)
    filtered: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if no_tenant_linked and row.get("tenants"):
            continue
        if assigned_to_vendor is not None and not _row_matches_vendor(row, assigned_to_vendor):
            continue
        if stuck_hours is not None and not _row_matches_stuck_hours(row, stuck_hours, now=now):
            continue
        filtered.append(row)
    return filtered


def _row_matches_vendor(row: dict, vendor_id: int) -> bool:
    requests = row.get("vendor_assignment_requests") or []
    if not isinstance(requests, list):
        return False
    for request in requests:
        if not isinstance(request, dict):
            continue
        vendor = request.get("vendor") or {}
        if isinstance(vendor, dict) and str(vendor.get("id")) == str(vendor_id):
            return True
    return False


def _parse_pm_datetime(value: Any) -> datetime:
    if not value:
        raise ValueError("missing timestamp")
    if not isinstance(value, str):
        raise ValueError(f"timestamp is not a string: {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_matches_stuck_hours(row: dict, stuck_hours: float, *, now: datetime) -> bool:
    """Return true when a meld has had no activity for at least stuck_hours.

    Blue's 2026-06-09 probe confirmed cookie list rows expose `updated` and
    the legacy Nexus `stuck_hours` param is ignored. We therefore define this
    filter as "no activity in N hours" using the row's `updated` timestamp.
    """
    try:
        updated = _parse_pm_datetime(row.get("updated"))
    except ValueError as exc:
        print_error(
            "Cannot honor --stuck-hours: cookie row is missing a valid updated "
            f"timestamp for meld {row.get('id')}: {exc}"
        )
        sys.exit(2)
    age_hours = (now.astimezone(timezone.utc) - updated).total_seconds() / 3600
    return age_hours >= stuck_hours


def _list_work_orders_nexus(
    status: Optional[str] = None,
    status_raw: Optional[str] = None,
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
    if status_raw:
        params.append(("status", status_raw))
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
