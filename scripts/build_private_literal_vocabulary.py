#!/usr/bin/env python3
"""Build the CI private-literal vocabulary from authoritative local exports."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import zlib
from pathlib import Path


PROVENANCE_START = "<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_START -->"
PROVENANCE_END = "<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_END -->"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_vocabulary(agents, vendors, tenants, config, extras=()):
    values: set[str] = set()

    def add(value):
        if value is None:
            return
        text = str(value).strip()
        if text:
            values.add(text)

    def add_tenant(kind, value):
        if value is None:
            return
        text = str(value).strip()
        if text:
            values.add(f"tenant-{kind}:{text}")

    for agent in agents:
        for key in ("id", "first_name", "last_name", "management"):
            add(agent.get(key))
        user = agent.get("user") or {}
        for key in ("id", "first_name", "last_name"):
            add(user.get(key))

    for vendor in vendors:
        for key in ("id", "name"):
            add(vendor.get(key))

    for tenant in tenants:
        for key in ("first_name", "middle_name", "last_name"):
            add_tenant("name", tenant.get(key))
        user = tenant.get("user") or {}
        for key in ("first_name", "last_name"):
            add_tenant("name", user.get(key))
        add_tenant("email", user.get("email"))
        contact = tenant.get("contact") or {}
        for key in ("business_phone", "cell_phone", "fax", "home_phone"):
            add_tenant("phone", contact.get(key))
        for key in ("primary_email", "secondary_email", "tertiary_email"):
            add_tenant("email", contact.get(key))

    for key in ("multitenant_id", "nexus_account_id", "credentials_path"):
        add(config.get(key))
    for value in extras:
        add(value)

    return sorted(values, key=lambda value: (value.casefold(), value))


def serialize_vocabulary(vocabulary) -> str:
    """Return the byte-stable representation stored in the CI secret."""
    return json.dumps(vocabulary, separators=(",", ":"))


def encode_secret(vocabulary_json: str) -> str:
    """Losslessly encode the complete roster below GitHub's secret-size cap."""
    compressed = zlib.compress(vocabulary_json.encode("utf-8"), level=9)
    return "zlib64:" + base64.b64encode(compressed).decode("ascii")


def write_provenance(path: Path, *, agent_count: int, vendor_count: int,
                     tenant_count: int, vocabulary_json: str) -> None:
    """Replace the public, value-free provenance record in *path*."""
    digest = hashlib.sha256(vocabulary_json.encode("utf-8")).hexdigest()
    block = "\n".join((
        PROVENANCE_START,
        f"- Agent records: `{agent_count}`",
        f"- Vendor records: `{vendor_count}`",
        f"- Tenant records: `{tenant_count}`",
        f"- Vocabulary entries: `{len(json.loads(vocabulary_json))}`",
        f"- Vocabulary SHA-256: `{digest}`",
        PROVENANCE_END,
    ))
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(PROVENANCE_START) + r".*?" + re.escape(PROVENANCE_END),
        re.DOTALL,
    )
    if not pattern.search(text):
        raise ValueError(f"provenance markers missing from {path}")
    path.write_text(pattern.sub(block, text), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", type=Path, required=True)
    parser.add_argument("--vendors", type=Path, required=True)
    parser.add_argument("--tenants", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--extras", type=Path)
    parser.add_argument(
        "--provenance",
        type=Path,
        help="update the committed value-free source counts and vocabulary digest",
    )
    args = parser.parse_args()

    agents = _load(args.agents)
    vendors = _load(args.vendors)
    tenants = _load(args.tenants)
    extras = _load(args.extras) if args.extras else []
    vocabulary = build_vocabulary(
        agents, vendors, tenants, _load(args.config), extras
    )
    if not vocabulary:
        parser.error("authoritative sources produced an empty vocabulary")
    vocabulary_json = serialize_vocabulary(vocabulary)
    if args.provenance:
        write_provenance(
            args.provenance,
            agent_count=len(agents),
            vendor_count=len(vendors),
            tenant_count=len(tenants),
            vocabulary_json=vocabulary_json,
        )
    print(encode_secret(vocabulary_json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
