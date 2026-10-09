"""Strict read pagination; this module never loads authentication or does I/O."""
import hashlib
import json
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit


class PaginationError(ValueError):
    """A fixed diagnostic code, never a server URL or response body."""

    def __init__(self, code):
        self.code = code
        super().__init__("read pagination failed: " + code)


PAGING_KEYS = frozenset({"cursor", "offset", "page", "limit"})


def _filters(query):
    return sorted((key, value) for key, value in parse_qsl(query, keep_blank_values=True) if key not in PAGING_KEYS)


def _path(raw, base, endpoint, filters):
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise PaginationError("invalid_next")
    try:
        origin = urlsplit(base)
        resource = urlunsplit((origin.scheme, origin.netloc, endpoint, "", ""))
        parsed = urlsplit(urljoin(resource, raw))
    except ValueError:
        raise PaginationError("invalid_next") from None
    if (parsed.scheme != origin.scheme or parsed.netloc != origin.netloc
            or parsed.username or parsed.password or parsed.fragment
            or parsed.path != endpoint):
        raise PaginationError("unsafe_next")
    if _filters(parsed.query) != filters:
        raise PaginationError("filter_scope_changed")
    prefix = origin.path
    return parsed.path[len(prefix):] + ("?" + parsed.query if parsed.query else "")


def collect(initial, fetch, *, base, require_complete=False, max_pages=50):
    """Collect a declared chain. Opaque arrays are supported but not certified.

    base ends in '/' and is the authenticated API prefix. fetch accepts a path
    relative to it. Missing count is legal only with an explicit terminal next.
    """
    if type(max_pages) is not int or max_pages < 1:
        raise PaginationError("invalid_page_budget")
    endpoint = urlsplit(urljoin(base, initial.lstrip("/"))).path
    current = initial.lstrip("/")
    filters = _filters(urlsplit(current).query)
    seen_paths, seen_pages, seen_ids = set(), set(), set()
    results, count, pages = [], None, 0
    basis = "unknown"
    while True:
        canonical = (urlsplit(current).path, tuple(sorted(parse_qsl(urlsplit(current).query, keep_blank_values=True))))
        if canonical in seen_paths:
            raise PaginationError("cycle")
        seen_paths.add(canonical)
        page = fetch(current)
        pages += 1
        if isinstance(page, list):
            if pages != 1:
                raise PaginationError("page_shape_changed")
            items, raw_next, terminal = page, None, False
            basis = "opaque_array"
        elif isinstance(page, dict) and isinstance(page.get("results"), list):
            items = page["results"]
            value = page.get("count")
            if value is not None:
                if type(value) is not int or value < 0:
                    raise PaginationError("invalid_count")
                if count is not None and value != count:
                    raise PaginationError("count_changed")
                count = value
            raw_next = page.get("next")
            if raw_next is not None and (not isinstance(raw_next, str) or not raw_next):
                raise PaginationError("invalid_next")
            terminal = "next" in page and raw_next is None
            basis = "declared_next_chain" if terminal else "count_verified" if count is not None else "undeclared_chain"
        else:
            raise PaginationError("invalid_page")
        if not all(isinstance(item, dict) for item in items):
            raise PaginationError("invalid_result_row")
        fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if items and fingerprint in seen_pages:
            raise PaginationError("duplicate_page")
        seen_pages.add(fingerprint)
        for item in items:
            if item.get("id") is not None:
                identity = str(item["id"])
                if identity in seen_ids:
                    raise PaginationError("duplicate_id")
                seen_ids.add(identity)
        results.extend(items)
        if count is not None and len(results) > count:
            raise PaginationError("count_overrun")
        if raw_next is None:
            if count is not None and len(results) != count:
                raise PaginationError("truncated_count")
            complete = terminal or count is not None
            if require_complete and not complete:
                raise PaginationError("completion_unproven")
            return dict(schema_version=1, results=results, count=count, next=None, complete=complete,
                        pages=pages, returned=len(results), basis=basis)
        # Validate before another GET (including before reaching the cap).
        current = _path(raw_next, base, endpoint, filters)
        if pages >= max_pages:
            raise PaginationError("page_cap")


def metadata(envelope):
    return {key: value for key, value in envelope.items() if key != "results"}
