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
        print_error(f"API error {e.code}: {e.reason}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print_error(f"Network error: {e.reason}")
        sys.exit(1)


def list_work_orders(status: Optional[str] = None, limit: int = 25) -> list:
    """List work orders, optionally filtered by status."""
    params: dict = {"limit": limit}
    if status:
        status_map = {
            "open": "open",
            "pending": "pending_completion",
            "completed": "completed",
            "canceled": "canceled",
        }
        params["status"] = status_map.get(status.lower(), status)

    data = _api_get("/meld/", params)
    results = data.get("results", data) if isinstance(data, dict) else data
    return results


def get_work_order(meld_id: str) -> dict:
    """Get a single work order by ID."""
    return _api_get(f"/meld/{meld_id}/")


def list_properties(limit: int = 100) -> list:
    """List all properties."""
    data = _api_get("/property/", {"limit": limit})
    results = data.get("results", data) if isinstance(data, dict) else data
    return results


def list_vendors(limit: int = 100) -> list:
    """List all vendors."""
    data = _api_get("/vendor/", {"limit": limit})
    results = data.get("results", data) if isinstance(data, dict) else data
    return results


def probe() -> dict:
    """Health check — verify API is reachable and credentials work."""
    try:
        token = get_token()
        return {"ok": True, "token_prefix": token[:8] + "..."}
    except SystemExit:
        return {"ok": False, "error": "Authentication failed"}
