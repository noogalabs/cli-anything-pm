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


def list_work_orders(status: Optional[str] = None, limit: int = 25) -> list:
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
    """
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

    data = _api_get("/meld/", params)
    results = data.get("results", data) if isinstance(data, dict) else data
    return results


def get_work_order(meld_id: str) -> dict:
    """Get a single work order by ID."""
    meld_id = _validate_meld_id(meld_id)
    return _api_get(f"/meld/{meld_id}/")


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
