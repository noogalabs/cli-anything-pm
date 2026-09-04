import importlib.util
import hashlib
import hmac
import json
import os
import re
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

SYNTHETIC_TENANT = "1000"


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
        f"https://app.propertymeld.com/{SYNTHETIC_TENANT}/m/{SYNTHETIC_TENANT}/api/melds/1/"
    )
    assert http_backend._build_url("melds/1/", side="vendor", vendor_id="7") == (
        f"https://app.propertymeld.com/{SYNTHETIC_TENANT}/v/7/api/melds/1/"
    )


def _private_vocab_module():
    script = Path(__file__).parents[1] / "scripts" / "build_private_literal_vocabulary.py"
    spec = importlib.util.spec_from_file_location("private_vocab", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tracked_private_digest_matches(repo: Path, digests: set[str], salt: bytes):
    module = _private_vocab_module()
    phone_candidate = re.compile(
        r"(?<![A-Za-z0-9])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?![A-Za-z0-9])"
    )
    email_candidate = re.compile(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    )
    numeric_candidate = re.compile(r"(?<![A-Za-z0-9])\d{1,11}(?![A-Za-z0-9])")
    name_field = re.compile(
        r"[\"'](?:first_name|middle_name|last_name|name)[\"']\s*:\s*[\"']([^\"']+)[\"']"
    )
    full_name_fields = re.compile(
        r"[\"']first_name[\"']\s*:\s*[\"']([^\"']+)[\"']"
        r".{0,300}?"
        r"[\"']last_name[\"']\s*:\s*[\"']([^\"']+)[\"']",
        re.DOTALL,
    )
    name_argv = re.compile(
        r"[\"']--(?:first|middle|last)-name[\"']\s*,\s*[\"']([^\"']+)[\"']"
    )

    def digest(token: str) -> str:
        return hmac.new(salt, token.encode("utf-8"), hashlib.sha256).hexdigest()

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
        # Global two-/three-word n-grams catch names leaked in prose while
        # avoiding false positives from common one-word vendor-name fragments.
        candidates = {token for token in module.name_ngrams(text) if " " in token}
        for first, last in full_name_fields.findall(text):
            candidates.update(module.name_ngrams(f"{first} {last}"))
        for regex in (name_field, name_argv):
            for value in regex.findall(text):
                candidates.update(module.name_ngrams(value))
        candidates.update(module.normalize_token(value) for value in email_candidate.findall(text))
        candidates.update(re.sub(r"\D", "", value) for value in phone_candidate.findall(text))
        candidates.update(numeric_candidate.findall(text))
        if any(digest(token) in digests for token in candidates if token):
            matches.append(str(path.relative_to(repo)))
    return matches


def _tracked_structural_private_matches(repo: Path):
    """Find private-data shapes that do not require a secret vocabulary.

    The numeric structural denominator is every standalone five- through
    eight-digit token, covering agent/coordinator, meld, unit, vendor, and
    tenant record IDs in this corpus. Six- through eight-digit fixture IDs must
    use the reserved 9-prefixed synthetic range. The only classified non-ID
    tokens are the command limit, synthetic postcode, and two CSS colours.
    Real IDs of every length are also checked against the authoritative
    roster/config vocabulary by ``_tracked_private_literal_matches``.
    """
    patterns = (
        re.compile(
            r"orgs[/]" + "asce" + r"ndops|Documents[/]" + "Asce" + "ndOps-Brain",
            re.IGNORECASE,
        ),
        re.compile(r"https?://[^\s\"']*propertymeld\.com/\d+/", re.IGNORECASE),
        re.compile("Asce" + "nd", re.IGNORECASE),
    )
    numeric_token = re.compile(r"(?<![A-Za-z0-9])\d{5,8}(?![A-Za-z0-9])")
    phone_candidate = re.compile(
        r"(?<![A-Za-z0-9])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?![A-Za-z0-9])"
    )
    allowed_non_id_tokens = {
        "123" + "45",  # intentionally synthetic postcode
        "100" + "00",  # Insights command limit
        "141" + "414", # CSS colour
        "161" + "616", # CSS colour
    }
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
        has_forbidden_numeric = any(
            token not in allowed_non_id_tokens
            and not (len(token) in (6, 7, 8) and token.startswith("9"))
            and not (len(token) == 7 and token.startswith("555" + "01"))
            for token in numeric_token.findall(text)
        )
        has_forbidden_phone = False
        for candidate in phone_candidate.findall(text):
            digits = re.sub(r"\D", "", candidate)
            national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
            if (
                len(national) != 10
                or national.startswith("423")
                or national[3:8] != ("555" + "01")
            ):
                has_forbidden_phone = True
                break
        if (
            any(pattern.search(text) for pattern in patterns)
            or has_forbidden_numeric
            or has_forbidden_phone
        ):
            matches.append(str(path.relative_to(repo)))
    return matches


def _is_fork_pull_request() -> bool:
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return False
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return False
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        pull = event["pull_request"]
        return pull["head"]["repo"]["full_name"] != pull["base"]["repo"]["full_name"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        # An unreadable event cannot prove this is a fork. Fail closed below.
        return False


def _tracked_private_export_paths(repo: Path) -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True
    ).stdout.split(b"\0")
    return sorted(
        relative.decode("utf-8")
        for relative in tracked
        if relative
        and (
            relative.decode("utf-8").startswith("private-exports/")
            or relative.decode("utf-8").endswith(".vocab-salt")
        )
    )


def test_private_tenant_and_org_digests_do_not_match_tracked_files():
    repo = Path(__file__).parents[1]
    raw_salt = os.environ.get("PROPERTYMELD_VOCAB_SALT")
    if not raw_salt:
        if os.environ.get("GITHUB_ACTIONS") == "true" and not _is_fork_pull_request():
            pytest.fail(
                "PROPERTYMELD_VOCAB_SALT is required in trusted GitHub Actions; "
                "the private-digest census cannot run without it"
            )
        pytest.skip(
            "PROPERTYMELD_VOCAB_SALT is absent locally or on a fork PR; "
            "private-digest census did not run"
        )
    module = _private_vocab_module()
    salt = module.decode_salt(raw_salt)
    digest_path = repo / "docs" / "private-literal-digests.json"
    digest_json = digest_path.read_text(encoding="utf-8")
    decoded = json.loads(digest_json)
    assert isinstance(decoded, list) and decoded
    assert all(re.fullmatch(r"[a-f0-9]{64}", item) for item in decoded)
    provenance = (repo / "docs" / "private-literal-secret-provenance.md").read_text(
        encoding="utf-8"
    )
    expected_digest = re.search(
        r"Digest-list SHA-256: `([a-f0-9]{64})`", provenance
    )
    assert expected_digest, "committed private-digest provenance hash is missing"
    assert hashlib.sha256(digest_json.encode("utf-8")).hexdigest() == expected_digest.group(1), (
        "committed private digest list is stale or partial"
    )
    assert _tracked_private_digest_matches(repo, set(decoded), salt) == []


def _assert_trusted_ci_salt_value_fails(monkeypatch, salt_value):
    if salt_value is None:
        monkeypatch.delenv("PROPERTYMELD_VOCAB_SALT", raising=False)
    else:
        monkeypatch.setenv("PROPERTYMELD_VOCAB_SALT", salt_value)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")

    try:
        test_private_tenant_and_org_digests_do_not_match_tracked_files()
    except BaseException as exc:
        assert isinstance(exc, pytest.fail.Exception)
        assert "PROPERTYMELD_VOCAB_SALT is required" in str(exc)
    else:
        pytest.fail("trusted CI missing-salt path did not fail")


def test_trusted_ci_missing_salt_fails_instead_of_skipping(monkeypatch):
    _assert_trusted_ci_salt_value_fails(monkeypatch, None)


def test_trusted_ci_blank_salt_fails_instead_of_skipping(monkeypatch):
    _assert_trusted_ci_salt_value_fails(monkeypatch, "")


def test_local_missing_salt_skips_by_name(monkeypatch):
    monkeypatch.delenv("PROPERTYMELD_VOCAB_SALT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    try:
        test_private_tenant_and_org_digests_do_not_match_tracked_files()
    except BaseException as exc:
        assert isinstance(exc, pytest.skip.Exception)
        assert "locally" in str(exc)
    else:
        pytest.fail("local missing-salt path did not skip")


def test_fork_pull_request_missing_salt_skips_by_name(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({
        "pull_request": {
            "head": {"repo": {"full_name": "contributor/fork"}},
            "base": {"repo": {"full_name": "noogalabs/cli-anything-pm"}},
        }
    }), encoding="utf-8")
    monkeypatch.delenv("PROPERTYMELD_VOCAB_SALT", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))

    try:
        test_private_tenant_and_org_digests_do_not_match_tracked_files()
    except BaseException as exc:
        assert isinstance(exc, pytest.skip.Exception)
        assert "fork PR" in str(exc)
    else:
        pytest.fail("fork PR missing-salt path did not skip")


def test_structural_private_data_shapes_are_absent_from_tracked_files():
    repo = Path(__file__).parents[1]
    assert _tracked_structural_private_matches(repo) == []


def test_private_export_inputs_are_never_tracked():
    repo = Path(__file__).parents[1]
    assert _tracked_private_export_paths(repo) == []


def _tracked_fixture(tmp_path: Path, text: str) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    fixture = tmp_path / "fixture.py"
    fixture.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "fixture.py"], cwd=tmp_path, check=True)
    return tmp_path


def test_sourced_resident_name_is_visible_to_private_literal_census(tmp_path):
    repo = _tracked_fixture(
        tmp_path,
        'resident = {"first_name": "' + "Resi" + "dent" + '", "last_name": "' + "Le" + "ak" + '"}\n',
    )

    module = _private_vocab_module()
    salt = bytes(range(32))
    digests = set(
        module.digest_vocabulary(module.name_ngrams("Resi" + "dent Le" + "ak"), salt)
    )

    assert _tracked_private_digest_matches(repo, digests, salt) == ["fixture.py"]


def test_bare_org_token_is_visible_to_structural_private_literal_census(tmp_path):
    repo = _tracked_fixture(tmp_path, 'account_match = "' + "Asce" + "nd" + '"\n')

    assert _tracked_structural_private_matches(repo) == ["fixture.py"]


def test_tracked_private_export_path_is_visible_to_custody_census(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    export = tmp_path / "private-exports" / "tenants.json"
    export.parent.mkdir()
    export.write_text("[]\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", str(export.relative_to(tmp_path))], cwd=tmp_path, check=True)

    assert _tracked_private_export_paths(tmp_path) == ["private-exports/tenants.json"]


def test_tracked_vocab_salt_file_is_visible_to_custody_census(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    salt_file = tmp_path / "operator.vocab-salt"
    salt_file.write_text("not-a-real-salt\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", salt_file.name], cwd=tmp_path, check=True)

    assert _tracked_private_export_paths(tmp_path) == ["operator.vocab-salt"]


def test_common_english_first_name_is_not_a_digest_subject(tmp_path):
    repo = _tracked_fixture(tmp_path, 'message = "mark this item green"\n')
    module = _private_vocab_module()
    salt = bytes(range(32))
    # The authoritative roster may contain Mark Green. The full identity is
    # sensitive; either common word in ordinary prose is not.
    digests = set(module.digest_vocabulary(module.name_ngrams("Mark Green"), salt))

    assert _tracked_private_digest_matches(repo, digests, salt) == []


def test_name_vocabulary_excludes_bare_common_words():
    module = _private_vocab_module()

    assert module.name_ngrams("Mark Green") == {"mark green"}


def test_non_reserved_phone_is_visible_to_structural_census(tmp_path):
    repo = _tracked_fixture(tmp_path, 'phone = "' + "4" + "23-555-0199" + '"\n')

    assert _tracked_structural_private_matches(repo) == ["fixture.py"]


def test_private_vocabulary_is_derived_from_complete_roster_shapes():
    module = _private_vocab_module()

    vocabulary = module.build_vocabulary(
        [{
            "id": 501,
            "first_name": "Tech",
            "last_name": "Example",
            "management": 1000,
            "user": {"id": 601, "first_name": "Operator", "last_name": "Example"},
        }],
        [{"id": 701, "name": "Vendor Example"}],
        [{
            "first_name": "SyntheticFirst",
            "middle_name": "SyntheticMiddle",
            "last_name": "SyntheticLast",
            "user": {
                "first_name": "SyntheticFirst",
                "last_name": "SyntheticLast",
                "email": "synthetic.person@example.com",
            },
            "contact": {
                "cell_phone": "2025550101",
                "primary_email": "fixture.contact@example.com",
            },
        }],
        {
            "multitenant_id": "1000",
            "nexus_account_id": "2000",
            "credentials_path": "/private/example/session.json",
        },
        ["Example Property Management", "orgs/example"],
    )

    assert set(vocabulary) == {
        "501", "601", "701", "1000", "2000",
        "tech example", "operator example", "vendor example",
        "syntheticfirst syntheticmiddle", "syntheticmiddle syntheticlast",
        "syntheticfirst syntheticmiddle syntheticlast",
        "syntheticfirst syntheticlast",
        "synthetic.person@example.com",
        "fixture.contact@example.com", "2025550101",
        "/private/example/session.json", "example property management", "orgs/example",
    }


def test_private_vocabulary_provenance_records_counts_and_digest(tmp_path):
    module = _private_vocab_module()
    doc = tmp_path / "provenance.md"
    doc.write_text(
        f"before\n{module.PROVENANCE_START}\nold\n{module.PROVENANCE_END}\nafter\n",
        encoding="utf-8",
    )
    digest_json = module.serialize_digests(["a" * 64, "b" * 64])

    module.write_provenance(
        doc, agent_count=10, vendor_count=17, tenant_count=648,
        digest_json=digest_json
    )

    text = doc.read_text(encoding="utf-8")
    assert "Agent records: `10`" in text
    assert "Vendor records: `17`" in text
    assert "Tenant records: `648`" in text
    assert "HMAC digests: `2`" in text
    assert hashlib.sha256(digest_json.encode()).hexdigest() in text


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
    salt = bytes(range(32))
    digest = hmac.new(salt, synthetic_id.encode(), hashlib.sha256).hexdigest()
    assert _tracked_private_digest_matches(repo, {digest}, salt) == []


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
