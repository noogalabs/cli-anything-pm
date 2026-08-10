"""Field markers that cannot be laundered into a confident empty answer.

WHY THIS EXISTS
---------------
`work_entries` arrived as ``null`` in list/detail payloads even when real entries
existed. Every consumer wrote the same idiom::

    (row.work_entries || []).length      # JS
    (row.get("work_entries") or [])      # Python

...which turns a MISSING FIELD into a confident ``0``. Reports then printed
"0 work entries, 0.00 total hours" as though those were measurements. Nothing
errored, nothing looked suspiciously empty — it rendered a number, and weeks of
sweeps were read as fact.

The same shape existed one field over: a failed cookie fetch set the assignment
fields to ``[]``, which reads as "nobody assigned" on the emergency-intake path.

THE PROPERTY THAT MATTERS
-------------------------
A marker is a non-empty ``dict``. Dicts are truthy in Python AND JavaScript, so
``x or []`` and ``x || []`` both return the MARKER rather than replacing it. The
laundering idiom cannot swallow it. A consumer either handles the marker or
produces something visibly wrong — never something quietly wrong.

``None`` and ``[]`` are laundered. A dict is not. That is the whole design.

Do NOT "fix" a marker by defaulting it to an empty list at the boundary; that
reintroduces exactly the bug this module exists to remove.
"""

from typing import Any

MARKER_KEY = "__pm_cli_marker__"

NOT_CARRIED = "not-carried"
FETCH_FAILED = "fetch-failed"


def not_carried(field: str, where: str, hint: str = "") -> dict:
    """This payload does not carry `field`. Its absence is NOT evidence of empty.

    Used where fetching the true value per row would be an N+1 against an
    endpoint measured at roughly a one-in-three timeout rate: a partially
    populated list is worse than a uniformly absent one, because some rows would
    be true and others silently short with no way to tell them apart.
    """
    return {
        MARKER_KEY: NOT_CARRIED,
        "field": field,
        "where": where,
        "meaning": f"{field} is not carried in this payload; absence is not evidence of empty",
        "hint": hint or f"fetch {field} from its dedicated endpoint if you need it",
    }


def fetch_failed(field: str, reason: str, detail: str = "") -> dict:
    """The true value was sought and could NOT be retrieved.

    Distinct from `not_carried`: we tried. Never collapse this to empty — a fix
    whose failure path writes `[]` rebuilds the original defect with an alibi,
    because the field would then look correctly populated-from-source.
    """
    return {
        MARKER_KEY: FETCH_FAILED,
        "field": field,
        "reason": reason,
        "detail": detail,
        "meaning": f"{field} could not be retrieved; this is NOT an empty result",
    }


def is_marker(value: Any) -> bool:
    """True when a value is any marker. Consumers branch on this before counting."""
    return isinstance(value, dict) and MARKER_KEY in value


def marker_kind(value: Any) -> str:
    """`not-carried`, `fetch-failed`, or `""` when the value is real data."""
    return value.get(MARKER_KEY, "") if is_marker(value) else ""
