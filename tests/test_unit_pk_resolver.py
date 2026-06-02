"""Unit-PK resolver tests.

Fixtures use the REAL shape captured live from the cookie/manager auth path on
2026-06-02 (GET /api/properties/{id}/ embeds units[] with integer `id` PKs;
units[].unit is the human label; list endpoints ignore server-side filters so
matching is client-side). Address/name values are GENERIC placeholders only.
"""
import pytest

from cli_anything.propertymeld import http_backend as hb


# ── real captured shape, generic data ────────────────────────────────────────

def _unit(uid, label, **extra):
    base = {
        "id": uid,
        "unit": label,
        "apartment": extra.get("apartment", ""),
        "building": extra.get("building"),
        "floor": extra.get("floor"),
        "suite": extra.get("suite", ""),
        "room": extra.get("room", ""),
        "department": extra.get("department", ""),
        "unit_address": extra.get("unit_address", 9000000 + uid),
        "prop": {"id": extra.get("prop_id", 5000)},
    }
    return base


# Single-unit property (street-address label), mirrors prop 1006-style record.
PROP_SINGLE = {
    "id": 5000,
    "property_name": "123 Main St",
    "line_1": "123 Main St",
    "line_2": "",
    "units": [_unit(7001, "123 Main St", prop_id=5000)],
}

# Multi-unit property: "Unit A" / "Unit B" (mirrors the 1098 N Hawthorne shape).
PROP_MULTI = {
    "id": 5100,
    "property_name": "456 Oak Ave",
    "line_1": "456 Oak Ave",
    "line_2": "",
    "units": [
        _unit(7101, "Unit A", prop_id=5100),
        _unit(7102, "Unit B", prop_id=5100),
        _unit(7103, "Apt 12", apartment="12", prop_id=5100),
    ],
}


@pytest.fixture
def patch_backend(monkeypatch):
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")

    props = {"5000": PROP_SINGLE, "5100": PROP_MULTI}

    def fake_get_no_exit(path, cookie_hdr, **kw):
        # properties/{id}/
        if path.startswith("properties/") and path.rstrip("/").split("/")[-1].isdigit():
            pid = path.rstrip("/").split("/")[-1]
            return props.get(pid, {"error": "HTTP 404", "status_code": 404})
        return {"error": "unexpected path", "path": path}

    def fake_get(path, cookie_hdr, **kw):
        return fake_get_no_exit(path, cookie_hdr, **kw)

    def fake_paginate_all(path, cookie_hdr, **kw):
        # property roster for name-based resolution
        if path.startswith("properties/"):
            return [PROP_SINGLE, PROP_MULTI]
        return []

    monkeypatch.setattr(hb, "_http_get_no_exit", fake_get_no_exit)
    monkeypatch.setattr(hb, "_http_get", fake_get)
    monkeypatch.setattr(hb, "_paginate_all", fake_paginate_all)
    return monkeypatch


# ── normalize_unit_label (pure) ──────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Apt 12", "12"),
    ("Unit 12", "12"),
    ("#12", "12"),
    ("  12 ", "12"),
    ("no. 12", "12"),
    ("Number 7", "7"),
    ("Unit A", "a"),
    ("Suite 200", "200"),
    ("unit", "unit"),            # bare designator stays
    ("", ""),
    (None, ""),
])
def test_normalize_unit_label(raw, expected):
    assert hb.normalize_unit_label(raw) == expected


# ── resolve_unit_pk: confident single match ──────────────────────────────────

def test_single_unit_property_resolves_regardless_of_label(patch_backend):
    res = hb.resolve_unit_pk(5000, "anything at all")
    assert res["unit_id"] == 7001
    assert res["property_id"] == 5000
    assert res["note"] == "single-unit property"


def test_exact_label_match_returns_pk(patch_backend):
    res = hb.resolve_unit_pk(5100, "Unit A")
    assert res["unit_id"] == 7101
    assert res["matched_on"] == "Unit A"


def test_messy_apt_label_normalizes_to_match(patch_backend):
    # "Apt 12" / "Unit 12" / "#12" / "12" all hit the apartment="12" unit.
    for q in ("Apt 12", "unit 12", "#12", "12"):
        res = hb.resolve_unit_pk(5100, q)
        assert res["unit_id"] == 7103, q


# ── resolve_unit_pk: not-found offers backstop, never guesses ─────────────────

def test_no_match_returns_backstop_not_a_guess(patch_backend):
    res = hb.resolve_unit_pk(5100, "Unit Z")
    assert "unit_id" not in res
    assert res["not_found"] == "Unit Z"
    assert res["property_id"] == 5100
    ids = sorted(u["id"] for u in res["units"])
    assert ids == [7101, 7102, 7103]


# ── resolve_unit_pk: ambiguity surfaces a disambiguation list ─────────────────

def test_ambiguous_match_lists_candidates(patch_backend, monkeypatch):
    # Two units sharing the same normalized label -> ambiguous, no commit.
    dup = {
        "id": 5200,
        "property_name": "789 Pine Rd",
        "line_1": "789 Pine Rd",
        "units": [hb_unit("Unit A", 7201), hb_unit("unit a", 7202)],
    }

    def fake_get_no_exit(path, cookie_hdr, **kw):
        return dup

    monkeypatch.setattr(hb, "_http_get_no_exit", fake_get_no_exit)
    res = hb.resolve_unit_pk(5200, "Unit A")
    assert "unit_id" not in res
    assert sorted(u["id"] for u in res["ambiguous"]) == [7201, 7202]


def hb_unit(label, uid):
    return {"id": uid, "unit": label, "apartment": "", "building": None,
            "suite": "", "room": "", "department": ""}


# ── property resolution by name (client-side substring) ──────────────────────

def test_property_by_name_substring(patch_backend):
    res = hb.resolve_unit_pk("Oak Ave", "Unit A")
    assert res["unit_id"] == 7101
    assert res["property_id"] == 5100


def test_property_name_ambiguous(patch_backend, monkeypatch):
    # substring matching both rostered properties
    monkeypatch.setattr(hb, "_paginate_all",
                        lambda path, ck, **kw: [PROP_SINGLE, PROP_MULTI])
    res = hb.resolve_unit_pk("St", "x")  # "123 Main St" line_1 contains "St"; "456 Oak Ave" no
    # only PROP_SINGLE matches "St" -> resolves to its single unit
    assert res["unit_id"] == 7001


def test_property_not_found(patch_backend):
    res = hb.resolve_unit_pk("Nonexistent Place", "Unit A")
    assert "error" in res
    assert "not found" in res["error"]


# ── list_units_by_property backstop ──────────────────────────────────────────

def test_list_by_property_reports_count(patch_backend):
    res = hb.list_units_by_property(5100)
    assert res["property_id"] == 5100
    assert res["count"] == 3
    assert sorted(u["id"] for u in res["units"]) == [7101, 7102, 7103]


def test_list_by_property_not_found(patch_backend):
    res = hb.list_units_by_property(9999)
    assert "error" in res


# ── get_unit_by_address ──────────────────────────────────────────────────────

def test_get_by_address_single_match_fetches_full_unit(patch_backend, monkeypatch):
    monkeypatch.setattr(hb, "get_unit",
                        lambda uid: {"id": uid, "unit": "Unit A", "maintenance_notes": "x"})
    res = hb.get_unit_by_address(5100, "Unit A")
    assert res["id"] == 7101
    assert res["_resolved"]["matched_on"] == "Unit A"


def test_get_by_address_no_match_returns_disambiguation(patch_backend):
    res = hb.get_unit_by_address(5100, "Unit Z")
    assert "not_found" in res
