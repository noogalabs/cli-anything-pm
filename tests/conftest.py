import json

import pytest

from cli_anything.propertymeld.config import propertymeld_config


@pytest.fixture(autouse=True)
def synthetic_propertymeld_config(tmp_path, monkeypatch):
    """Keep every test independent from an operator-owned local config."""
    credentials = tmp_path / "propertymeld-session.json"
    config = tmp_path / "propertymeld-config.json"
    config.write_text(
        json.dumps(
            {
                "multitenant_id": "1000",
                "nexus_account_id": "2000",
                "credentials_path": str(credentials),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYMELD_CONFIG", str(config))
    propertymeld_config.cache_clear()
    yield
    propertymeld_config.cache_clear()
