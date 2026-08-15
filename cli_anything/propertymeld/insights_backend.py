"""Read-only Property Meld Insights parquet client.

This module intentionally exposes only three fixed analytics datasets and only
constructs explicit HTTP GET requests.  It does not import the cookie backend,
whose surface also contains write helpers.  Vendor identity enrichment uses the
Nexus API backend, which is GET-only.
"""

from __future__ import annotations

import json
import math
import os
import ssl
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Iterable, Optional

from . import api_backend


class InsightsError(RuntimeError):
    """A fail-closed Insights fetch, schema, or parquet decoding error."""


_ENDPOINTS = MappingProxyType({
    "melds": "analytics/parquet/raw_meld_data.parquet",
    "turnovers": "analytics/parquet/raw_meld_data.parquet",
    "benchmarks": "analytics/parquet/benchmarks.parquet",
})

_MELD_COLUMNS = (
    "meld_meld_id",
    "meld_meld_created",
    "meld_meld_status",
    "meld_meld_work_category",
    "meld_meld_project_id",
    "vendor_assigned_name",
    "inhouse_servicer_name",
    "meld_meld_coordinator_id",
    "meld_meld_coordinator_name",
    "meld_meld_assigned_to_accepted_for_vendor",
    "meld_meld_accepted_to_scheduled_for_vendor",
    "meld_meld_assigned_to_scheduled",
    "meld_meld_assigned_to_completed",
    "meld_meld_tenant_rating",
    "invoice_amount",
    "expenditure_amount",
    "total_worklog_hours",
    "vendor_chat_response_seconds",
)
_MELD_REQUIRED = frozenset({
    "meld_meld_id",
    "meld_meld_work_category",
    "vendor_assigned_name",
})

_BENCHMARK_COLUMNS = (
    "unit_count",
    "priority",
    "work_category",
    "region",
    "is_project",
    "completed_month",
    "sor_5th",
    "sor_25th",
    "sor_50th",
    "sor_75th",
    "sor_95th",
    "res_sat_5th",
    "res_sat_25th",
    "res_sat_50th",
    "res_sat_75th",
    "res_sat_95th",
    "invoice_spend_5th",
    "invoice_spend_25th",
    "invoice_spend_50th",
    "invoice_spend_75th",
    "invoice_spend_95th",
    "expenditure_spend_5th",
    "expenditure_spend_25th",
    "expenditure_spend_50th",
    "expenditure_spend_75th",
    "expenditure_spend_95th",
)
_BENCHMARK_REQUIRED = frozenset({
    "unit_count",
    "priority",
    "work_category",
    "region",
    "is_project",
    "completed_month",
})

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_SSL_CONTEXT = ssl.create_default_context()


def _credentials_path() -> str:
    return os.environ.get(
        "PM_CREDS_PATH",
        os.path.expanduser("~/.claude/credentials/property-meld.json"),
    )


def _load_cookie_header() -> str:
    path = _credentials_path()
    try:
        with open(path, encoding="utf-8") as handle:
            credentials = json.load(handle)
    except FileNotFoundError as exc:
        raise InsightsError(f"Credentials file not found: {path}") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise InsightsError(f"Cannot read PropertyMeld credentials: {exc}") from exc

    cookies = credentials.get("cookies") if isinstance(credentials, dict) else None
    if not isinstance(cookies, list):
        raise InsightsError("PropertyMeld credentials contain no cookies list")
    parts = [
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if isinstance(cookie, dict)
        and "propertymeld.com" in str(cookie.get("domain", ""))
        and cookie.get("name")
        and cookie.get("value")
    ]
    if not parts:
        raise InsightsError("PropertyMeld credentials contain no usable session cookie")
    return "; ".join(parts)


def _dataset_url(dataset: str) -> str:
    try:
        endpoint = _ENDPOINTS[dataset]
    except KeyError as exc:
        raise InsightsError(f"Unsupported Insights dataset: {dataset}") from exc
    multitenant = os.environ.get("PM_MULTITENANT_ID", "3287")
    if not multitenant.isdigit():
        raise InsightsError("PM_MULTITENANT_ID must be numeric")
    return (
        f"https://app.propertymeld.com/{multitenant}/m/{multitenant}/api/"
        f"{endpoint}"
    )


def _fetch_parquet_bytes(dataset: str) -> bytes:
    """Fetch one allowlisted dataset with a literal GET request."""
    url = _dataset_url(dataset)
    request = urllib.request.Request(
        url,
        headers={
            "Cookie": _load_cookie_header(),
            "Accept": "application/vnd.apache.parquet, application/octet-stream",
            "User-Agent": _UA,
            "Referer": f"https://app.propertymeld.com/{os.environ.get('PM_MULTITENANT_ID', '3287')}/m/{os.environ.get('PM_MULTITENANT_ID', '3287')}/insights/",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            request,
            context=_SSL_CONTEXT,
            timeout=60,
        ) as response:
            final_url = response.geturl()
            if "/login" in final_url:
                raise InsightsError("PropertyMeld session redirected to login")
            payload = response.read()
    except InsightsError:
        raise
    except urllib.error.HTTPError as exc:
        raise InsightsError(f"Insights GET failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise InsightsError(f"Insights GET failed: {exc.reason}") from exc
    except OSError as exc:
        raise InsightsError(f"Insights GET failed: {exc}") from exc

    if len(payload) < 8 or payload[:4] != b"PAR1" or payload[-4:] != b"PAR1":
        raise InsightsError("Insights endpoint did not return a valid parquet payload")
    return payload


def _read_parquet_rows(
    payload: bytes,
    *,
    allowed_columns: Iterable[str],
    required_columns: frozenset[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as parquet
    except ImportError as exc:  # pragma: no cover - dependency packaging guard
        raise InsightsError(
            "pyarrow is required for Insights commands; reinstall cli-anything-pm"
        ) from exc

    try:
        source = pa.BufferReader(payload)
        available = set(parquet.read_schema(source).names)
        missing = sorted(required_columns - available)
        if missing:
            raise InsightsError(
                "Insights parquet schema is missing required columns: "
                + ", ".join(missing)
            )
        selected = [column for column in allowed_columns if column in available]
        source.seek(0)
        table = parquet.read_table(source, columns=selected)
    except InsightsError:
        raise
    except Exception as exc:
        raise InsightsError(f"Cannot decode Insights parquet: {exc}") from exc
    return table.to_pylist(), selected


def _normalize_vendor_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _vendor_name(vendor: dict[str, Any]) -> Optional[str]:
    for key in ("name", "company_name", "company"):
        value = vendor.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_vendor_names(
    rows: Iterable[dict[str, Any]],
    vendors: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach stable Nexus vendor IDs without dropping unresolved rows."""
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for vendor in vendors:
        if not isinstance(vendor, dict):
            continue
        name = _vendor_name(vendor)
        key = _normalize_vendor_name(name)
        if not key or vendor.get("id") is None:
            continue
        candidate = {"vendor_id": vendor["id"], "vendor_name": name}
        bucket = index.setdefault(key, {})
        bucket[str(vendor["id"])] = candidate

    enriched: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    unresolved: set[str] = set()
    ambiguous: set[str] = set()
    for original in rows:
        row = dict(original)
        source_name = row.get("vendor_assigned_name")
        key = _normalize_vendor_name(source_name)
        matches = sorted(
            index.get(key, {}).values(),
            key=lambda match: (str(match["vendor_id"]), str(match["vendor_name"])),
        )
        if not key:
            resolution = {
                "status": "not_applicable",
                "source_name": source_name,
                "vendor_id": None,
                "vendor_name": None,
            }
        elif not matches:
            resolution = {
                "status": "unresolved",
                "source_name": source_name,
                "vendor_id": None,
                "vendor_name": None,
            }
            unresolved.add(str(source_name))
        elif len(matches) == 1:
            resolution = {
                "status": "resolved",
                "source_name": source_name,
                **matches[0],
            }
        else:
            resolution = {
                "status": "ambiguous",
                "source_name": source_name,
                "vendor_id": None,
                "vendor_name": None,
                "matches": matches,
            }
            ambiguous.add(str(source_name))
        counts[resolution["status"]] += 1
        row["vendor_resolution"] = resolution
        enriched.append(row)

    summary = {
        "counts": dict(sorted(counts.items())),
        "unresolved_names": sorted(unresolved, key=str.casefold),
        "ambiguous_names": sorted(ambiguous, key=str.casefold),
    }
    return enriched, summary


def _project_matches(value: Any, project: Optional[bool]) -> bool:
    if project is None:
        return True
    is_project = bool(value)
    return is_project is project


def _string_matches(value: Any, expected: Optional[str]) -> bool:
    if expected is None:
        return True
    return str(value or "").casefold() == expected.casefold()


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(nested) for nested in value]
    return value


def _sanitize_nonfinite(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        cleaned.append({key: _json_safe_value(value) for key, value in row.items()})
    return cleaned


def get_melds(
    *,
    limit: int = 100,
    work_category: Optional[str] = None,
    project: Optional[bool] = None,
    turnovers_only: bool = False,
) -> dict[str, Any]:
    dataset = "turnovers" if turnovers_only else "melds"
    payload = _fetch_parquet_bytes(dataset)
    rows, columns = _read_parquet_rows(
        payload,
        allowed_columns=_MELD_COLUMNS,
        required_columns=_MELD_REQUIRED,
    )
    category = "TURNOVER" if turnovers_only else work_category
    filtered = [
        row
        for row in rows
        if _string_matches(row.get("meld_meld_work_category"), category)
        and _project_matches(row.get("meld_meld_project_id"), project)
    ]
    returned = _sanitize_nonfinite(filtered[:limit])
    vendors = api_backend.list_vendors(limit=None)
    enriched, resolution = resolve_vendor_names(returned, vendors)
    return {
        "dataset": dataset,
        "source": _ENDPOINTS[dataset],
        "columns": columns + ["vendor_resolution"],
        "matched_count": len(filtered),
        "returned_count": len(enriched),
        "vendor_resolution": resolution,
        "rows": enriched,
    }


def get_benchmarks(
    *,
    limit: int = 100,
    work_category: Optional[str] = None,
    priority: Optional[str] = None,
    region: Optional[str] = None,
    project: Optional[bool] = None,
) -> dict[str, Any]:
    payload = _fetch_parquet_bytes("benchmarks")
    rows, columns = _read_parquet_rows(
        payload,
        allowed_columns=_BENCHMARK_COLUMNS,
        required_columns=_BENCHMARK_REQUIRED,
    )
    filtered = [
        row
        for row in rows
        if _string_matches(row.get("work_category"), work_category)
        and _string_matches(row.get("priority"), priority)
        and _string_matches(row.get("region"), region)
        and (project is None or bool(row.get("is_project")) is project)
    ]
    returned = _sanitize_nonfinite(filtered[:limit])
    return {
        "dataset": "benchmarks",
        "source": _ENDPOINTS["benchmarks"],
        "columns": columns,
        "matched_count": len(filtered),
        "returned_count": len(returned),
        "rows": returned,
    }
