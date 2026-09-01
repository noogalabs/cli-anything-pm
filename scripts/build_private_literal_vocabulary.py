#!/usr/bin/env python3
"""Build the CI private-literal vocabulary from authoritative local exports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", type=Path, required=True)
    parser.add_argument("--vendors", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--extras", type=Path)
    args = parser.parse_args()

    extras = _load(args.extras) if args.extras else []
    vocabulary = build_vocabulary(
        _load(args.agents), _load(args.vendors), _load(args.config), extras
    )
    if not vocabulary:
        parser.error("authoritative sources produced an empty vocabulary")
    print(json.dumps(vocabulary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
