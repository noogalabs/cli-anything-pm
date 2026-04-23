"""Shared utilities: token cache, JSON output, error handling."""
import json
import os
import sys
from typing import Any

# Nexus API constants
TOKEN_URL = "https://app.propertymeld.com/api/v2/oauth/token/"
API_BASE = "https://app.propertymeld.com/api/v2"
MULTITENANT_ID = os.environ.get("PM_MULTITENANT_ID", "3287")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_token_cache: dict = {}


def get_token() -> str:
    """Fetch or return cached OAuth2 bearer token."""
    if _token_cache.get("token"):
        return _token_cache["token"]

    client_id = os.environ.get("PM_CLIENT_ID", "")
    client_secret = os.environ.get("PM_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print_error("PM_CLIENT_ID or PM_CLIENT_SECRET not set in environment.")
        sys.exit(2)

    import urllib.parse
    import urllib.request
    import ssl

    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
            "Accept": "application/json",
        }
    )
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=15) as resp:
        body = json.loads(resp.read())

    _token_cache["token"] = body["access_token"]
    return _token_cache["token"]


def output_json(data: Any) -> None:
    """Print data as JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


def print_error(message: str) -> None:
    """Print error to stderr in JSON format."""
    print(json.dumps({"error": message}), file=sys.stderr)


def clear_token_cache() -> None:
    """Clear cached token (for testing)."""
    _token_cache.clear()
