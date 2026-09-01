#!/usr/bin/env python3
"""Build a value-free HMAC census from authoritative private exports."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from pathlib import Path


PROVENANCE_START = "<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_START -->"
PROVENANCE_END = "<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_END -->"
SALT_ENV = "PROPERTYMELD_VOCAB_SALT"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_token(value) -> str:
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def name_ngrams(value) -> set[str]:
    words = re.findall(r"[\w]+(?:[-'][\w]+)?", normalize_token(value))
    return {
        " ".join(words[start:start + width])
        # Bare first/last names collide with ordinary English in public prose.
        # Full-name pairs/triples preserve specificity without losing real
        # resident/staff/vendor identities.
        for width in range(2, 4)
        for start in range(0, len(words) - width + 1)
    }


def build_vocabulary(agents, vendors, tenants, config, extras=()):
    values: set[str] = set()

    def add(value):
        if value is not None and (token := normalize_token(value)):
            values.add(token)

    def add_name(value):
        if value is not None:
            values.update(name_ngrams(value))

    def add_phone(value):
        if value is not None and (digits := re.sub(r"\D", "", str(value))):
            values.add(digits)

    for agent in agents:
        for key in ("id", "management"):
            add(agent.get(key))
        add_name(" ".join(
            str(agent.get(key) or "") for key in ("first_name", "last_name")
        ))
        user = agent.get("user") or {}
        add(user.get("id"))
        add_name(" ".join(
            str(user.get(key) or "") for key in ("first_name", "last_name")
        ))

    for vendor in vendors:
        add(vendor.get("id"))
        add_name(vendor.get("name"))

    for tenant in tenants:
        add_name(" ".join(
            str(tenant.get(key) or "")
            for key in ("first_name", "middle_name", "last_name")
        ))
        user = tenant.get("user") or {}
        add_name(" ".join(
            str(user.get(key) or "") for key in ("first_name", "last_name")
        ))
        add(user.get("email"))
        contact = tenant.get("contact") or {}
        for key in ("business_phone", "cell_phone", "fax", "home_phone"):
            add_phone(contact.get(key))
        for key in ("primary_email", "secondary_email", "tertiary_email"):
            add(contact.get(key))

    for key in ("multitenant_id", "nexus_account_id", "credentials_path"):
        add(config.get(key))
    for value in extras:
        add(value)

    return sorted(values)


def decode_salt(raw: str) -> bytes:
    try:
        salt = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError(f"{SALT_ENV} must be hexadecimal") from exc
    if len(salt) != 32:
        raise ValueError(f"{SALT_ENV} must encode exactly 32 bytes")
    return salt


def digest_vocabulary(vocabulary, salt: bytes) -> list[str]:
    return sorted(
        hmac.new(salt, token.encode("utf-8"), hashlib.sha256).hexdigest()
        for token in vocabulary
    )


def serialize_digests(digests) -> str:
    return json.dumps(digests, separators=(",", ":")) + "\n"


def write_provenance(path: Path, *, agent_count: int, vendor_count: int,
                     tenant_count: int, digest_json: str) -> None:
    digest = hashlib.sha256(digest_json.encode("utf-8")).hexdigest()
    block = "\n".join((
        PROVENANCE_START,
        f"- Agent records: `{agent_count}`",
        f"- Vendor records: `{vendor_count}`",
        f"- Tenant records: `{tenant_count}`",
        f"- HMAC digests: `{len(json.loads(digest_json))}`",
        f"- Digest-list SHA-256: `{digest}`",
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
    parser.add_argument("--digests", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()

    raw_salt = os.environ.get(SALT_ENV)
    if not raw_salt:
        parser.error(f"{SALT_ENV} is required")
    try:
        salt = decode_salt(raw_salt)
    except ValueError as exc:
        parser.error(str(exc))

    agents = _load(args.agents)
    vendors = _load(args.vendors)
    tenants = _load(args.tenants)
    extras = _load(args.extras) if args.extras else []
    vocabulary = build_vocabulary(agents, vendors, tenants, _load(args.config), extras)
    if not vocabulary:
        parser.error("authoritative sources produced an empty vocabulary")
    digest_json = serialize_digests(digest_vocabulary(vocabulary, salt))
    args.digests.write_text(digest_json, encoding="utf-8")
    write_provenance(
        args.provenance,
        agent_count=len(agents),
        vendor_count=len(vendors),
        tenant_count=len(tenants),
        digest_json=digest_json,
    )
    print(
        json.dumps({
            "agents": len(agents),
            "vendors": len(vendors),
            "tenants": len(tenants),
            "digests": len(json.loads(digest_json)),
        })
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
