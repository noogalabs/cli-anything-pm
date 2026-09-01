import json
import os
import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cli_anything.propertymeld import http_backend
from cli_anything.propertymeld.cli import cli, command_index
from cli_anything.propertymeld.config import (
    PropertyMeldConfigError,
    load_propertymeld_config,
    propertymeld_config,
)


def _leaf_commands(root: click.Group):
    leaves = []

    def visit(command, path):
        if isinstance(command, click.Group):
            for name, child in sorted(command.commands.items()):
                visit(child, path + (name,))
            return
        leaves.append((path, command))

    visit(root, ())
    return leaves


def test_every_leaf_has_help_and_explicit_json_contract():
    runner = CliRunner()
    leaves = _leaf_commands(cli)

    assert leaves
    for path, command in leaves:
        option_names = {
            option
            for param in command.params
            if isinstance(param, click.Option)
            for option in param.opts
        }
        assert "--json" in option_names, " ".join(path)
        result = runner.invoke(cli, [*path, "--help"])
        assert result.exit_code == 0, (" ".join(path), result.output)
        assert "Usage:" in result.output


def test_runtime_index_matches_independently_derived_click_population():
    independently_derived = [" ".join(path) for path, _ in _leaf_commands(cli)]
    catalog = command_index()

    assert [entry["command"] for entry in catalog["commands"]] == independently_derived
    assert all(entry["help"] for entry in catalog["commands"])

    result = CliRunner().invoke(cli, ["index", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == catalog


@pytest.mark.parametrize(
    ("args", "target"),
    (
        (("probe", "--json"), "cli_anything.propertymeld.api_backend.probe"),
        (("insights", "melds", "--json"), "cli_anything.propertymeld.insights_backend.get_melds"),
        (("insights", "turnovers", "--json"), "cli_anything.propertymeld.insights_backend.get_melds"),
        (("insights", "benchmarks", "--json"), "cli_anything.propertymeld.insights_backend.get_benchmarks"),
    ),
)
def test_new_json_contracts_emit_parseable_json(monkeypatch, args, target):
    """The four added flags promise JSON behavior, not merely option presence."""
    monkeypatch.setattr(target, lambda **_kwargs: {"status": "synthetic"})

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"status": "synthetic"}


def test_missing_config_refuses_action_with_setup_guidance(monkeypatch):
    monkeypatch.setenv("PROPERTYMELD_CONFIG", "/definitely/missing/propertymeld.json")
    propertymeld_config.cache_clear()

    result = CliRunner().invoke(cli, ["work-orders", "comments", "1", "--json"])

    assert result.exit_code == 2
    assert "Property Meld config not found" in result.output
    assert "config/propertymeld.example.json" in result.output


def test_dummy_config_drives_real_manager_and_vendor_routing():
    assert http_backend._build_url("melds/1/") == (
        "https://app.propertymeld.com/1000/m/1000/api/melds/1/"
    )
    assert http_backend._build_url("melds/1/", side="vendor", vendor_id="7") == (
        "https://app.propertymeld.com/1000/v/7/api/melds/1/"
    )


def _tracked_private_literal_matches(repo: Path, needles: tuple[str, ...]):
    matches = []
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for relative in tracked:
        if not relative:
            continue
        path = repo / relative.decode("utf-8")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if any(needle in text for needle in needles):
            matches.append(str(path.relative_to(repo)))
    return matches


def test_private_tenant_and_org_literals_are_absent_from_tracked_files():
    repo = Path(__file__).parents[1]
    raw = os.environ.get("PROPERTYMELD_PRIVATE_LITERALS")
    if not raw:
        pytest.skip(
            "PROPERTYMELD_PRIVATE_LITERALS is absent; private-literal census is armed in CI via repository secret"
        )
    decoded = json.loads(raw)
    assert isinstance(decoded, list)
    private_literals = tuple(decoded)
    assert private_literals and all(isinstance(item, str) and item for item in private_literals)
    assert _tracked_private_literal_matches(repo, private_literals) == []


def test_supported_untracked_config_is_outside_source_census(tmp_path):
    repo = Path(__file__).parents[1]
    config = tmp_path / "propertymeld.json"
    synthetic_id = str(900_000 + (abs(hash(str(tmp_path))) % 99_999))
    config.write_text(json.dumps({
        "multitenant_id": synthetic_id,
        "nexus_account_id": "9001",
        "credentials_path": str(tmp_path / "session.json"),
    }))

    assert load_propertymeld_config(config).multitenant_id == synthetic_id
    assert _tracked_private_literal_matches(repo, (synthetic_id,)) == []


def test_malformed_config_fails_closed_by_field(tmp_path):
    config = tmp_path / "bad.json"
    config.write_text(json.dumps({
        "multitenant_id": "not-a-number",
        "nexus_account_id": "2000",
        "credentials_path": str(tmp_path / "session.json"),
    }))

    try:
        load_propertymeld_config(config)
    except PropertyMeldConfigError as exc:
        assert "multitenant_id must be numeric" in str(exc)
    else:
        raise AssertionError("malformed config was accepted")
