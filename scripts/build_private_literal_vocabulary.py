#!/usr/bin/env python3
"""Build the CI private-literal vocabulary from authoritative local exports."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


PROVENANCE_START = "<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_START -->"
PROVENANCE_END = "<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_END -->"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_vocabulary(agents, vendors, config, extras=()):
    values: set[str] = set()

    def add(value):
        if value is None:
            return
        text = str(value).strip()
        if text:
            values.add(text)

    for agent in agents:
        for key in ("id", "first_name", "last_name", "management"):
            add(agent.get(key))
        user = agent.get("user") or {}
        for key in ("id", "first_name", "last_name"):
            add(user.get(key))

    for vendor in vendors:
        for key in ("id", "name"):
            add(vendor.get(key))

    for key in ("multitenant_id", "nexus_account_id", "credentials_path"):
        add(config.get(key))
    for value in extras:
        add(value)

    return sorted(values, key=lambda value: (value.casefold(), value))


def serialize_vocabulary(vocabulary) -> str:
    """Return the byte-stable representation stored in the CI secret."""
    return json.dumps(vocabulary, separators=(",", ":"))


def write_provenance(path: Path, *, agent_count: int, vendor_count: int,
                     vocabulary_json: str) -> None:
    """Replace the public, value-free provenance record in *path*."""
    digest = hashlib.sha256(vocabulary_json.encode("utf-8")).hexdigest()
    block = "\n".join((
        PROVENANCE_START,
        f"- Agent records: `{agent_count}`",
        f"- Vendor records: `{vendor_count}`",
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
    extras = _load(args.extras) if args.extras else []
    vocabulary = build_vocabulary(
        agents, vendors, _load(args.config), extras
    )
    if not vocabulary:
        parser.error("authoritative sources produced an empty vocabulary")
    vocabulary_json = serialize_vocabulary(vocabulary)
    if args.provenance:
        write_provenance(
            args.provenance,
            agent_count=len(agents),
            vendor_count=len(vendors),
            vocabulary_json=vocabulary_json,
        )
    print(vocabulary_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
