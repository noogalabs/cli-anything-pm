"""Fail-closed, per-install Property Meld routing and credential custody."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CONFIG_ENV = "PROPERTYMELD_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.claude/credentials/propertymeld-config.json").expanduser()


class PropertyMeldConfigError(RuntimeError):
    """The local Property Meld configuration is absent or unsafe."""


@dataclass(frozen=True)
class PropertyMeldConfig:
    multitenant_id: str
    nexus_account_id: str
    credentials_path: Path

    @property
    def manager_base_url(self) -> str:
        return (
            f"https://app.propertymeld.com/{self.multitenant_id}"
            f"/m/{self.multitenant_id}"
        )


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV, str(DEFAULT_CONFIG_PATH))).expanduser()


def load_propertymeld_config(path: Path | None = None) -> PropertyMeldConfig:
    source = path or config_path()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PropertyMeldConfigError(
            f"Property Meld config not found: {source}. "
            "Copy config/propertymeld.example.json and set PROPERTYMELD_CONFIG."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PropertyMeldConfigError(
            f"Property Meld config is unreadable: {source}"
        ) from exc

    multitenant_id = raw.get("multitenant_id") if isinstance(raw, dict) else None
    nexus_account_id = raw.get("nexus_account_id") if isinstance(raw, dict) else None
    credentials_path = raw.get("credentials_path") if isinstance(raw, dict) else None
    if not isinstance(multitenant_id, (str, int)) or not str(multitenant_id).isdigit():
        raise PropertyMeldConfigError(
            "Property Meld multitenant_id must be numeric in the local config"
        )
    if not isinstance(credentials_path, str) or not credentials_path.strip():
        raise PropertyMeldConfigError(
            "Property Meld credentials_path must be a non-empty local path"
        )
    if not isinstance(nexus_account_id, (str, int)) or not str(nexus_account_id).isdigit():
        raise PropertyMeldConfigError(
            "Property Meld nexus_account_id must be numeric in the local config"
        )
    return PropertyMeldConfig(
        multitenant_id=str(multitenant_id),
        nexus_account_id=str(nexus_account_id),
        credentials_path=Path(credentials_path).expanduser(),
    )


@lru_cache(maxsize=1)
def propertymeld_config() -> PropertyMeldConfig:
    """Resolve installation routing on first action, never during import/help."""
    return load_propertymeld_config()


def require_propertymeld_config() -> PropertyMeldConfig:
    """Fail an action cleanly while keeping import/help config-independent."""
    try:
        return propertymeld_config()
    except PropertyMeldConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
