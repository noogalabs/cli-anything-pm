"""Casualties for output-boundary secret redaction.

Every pm command emits through utils.output_json (the single shared stdout
path), and PropertyMeld responses can carry credentials that are not ours to
print: the work-orders comments payload nests the management company's OAuth
client secret two levels down under comment.agent.management. These tests pin
that (1) the redactor strips any key matching /secret|token|password|api_?key/i
recursively through dicts AND lists, (2) the real `pm work-orders comments`
command path renders it redacted and the raw value never reaches stdout,
(3) there is no environment, global, or per-command switch that turns
redaction off (output_json has no bypass parameter), and (4) api-keys rotate
delivers a once-shown secret only through non-displaying paths, refusing to
mint at all when it has no destination.

The sentinel is a synthetic string; no real credential appears in this file.
"""
import inspect
import io
import json
import os
import stat
import urllib.error
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_anything.propertymeld import http_backend
from cli_anything.propertymeld.cli import cli
from cli_anything.propertymeld.utils import (
    REDACTED,
    SENSITIVE_KEY_PATTERN,
    output_json,
    redact_sensitive,
    update_env_file,
)

# Synthetic, deliberately unusual so a plain substring search proves absence.
SENTINEL = "SENTINEL-oauth-secret-9f3c2b7a1d"


def comments_fixture():
    """The real shape: list -> comment -> agent -> management -> secret."""
    return [
        {
            "id": 1,
            "body": "Tech en route",
            "agent": {
                "id": 7,
                "name": "Coordinator",
                "management": {
                    "id": 42,
                    "name": "Example Management",
                    "oauth_client_secret": SENTINEL,
                },
            },
        },
        {"id": 2, "body": "Done", "agent": None},
    ]


# ── redact_sensitive: the mechanism ─────────────────────────────────────────

def test_secret_two_levels_down_under_list_element_is_redacted():
    out = redact_sensitive(comments_fixture())
    assert out[0]["agent"]["management"]["oauth_client_secret"] == REDACTED
    # Neighbours under the same parent survive untouched.
    assert out[0]["agent"]["management"]["name"] == "Example Management"
    assert out[0]["agent"]["name"] == "Coordinator"
    assert out[1]["agent"] is None
    assert SENTINEL not in json.dumps(out)


@pytest.mark.parametrize(
    "key",
    [
        "secret", "SECRET", "client_secret", "clientSecret", "oauth_client_secret",
        "token", "access_token", "accessToken", "refresh_token", "csrf_token",
        "password", "Password", "PASSWORD", "db_password",
        "api_key", "API_KEY", "apikey", "apiKey", "x-api-key",
    ],
)
def test_each_pattern_matches_case_insensitively_as_substring(key):
    assert redact_sensitive({key: SENTINEL}) == {key: REDACTED}


@pytest.mark.parametrize("key", ["id", "name", "status", "description", "email", "token_count_label_that_is_fine"])
def test_non_matching_keys_untouched_except_substring_hits(key):
    out = redact_sensitive({key: "value"})
    if SENSITIVE_KEY_PATTERN.search(key):
        # 'token_count...' contains 'token': documented over-redaction, stays redacted.
        assert out == {key: REDACTED}
    else:
        assert out == {key: "value"}


def test_whole_subtree_under_matching_key_is_dropped_not_descended():
    out = redact_sensitive({"token_config": {"nested": {"deep": "x"}, "list": [1, 2]}})
    assert out == {"token_config": REDACTED}


def test_recursion_through_tuples_and_empty_containers_and_leaves():
    assert redact_sensitive(({"password": "p"}, [])) == [{"password": REDACTED}, []]
    assert redact_sensitive({}) == {}
    assert redact_sensitive([]) == []
    assert redact_sensitive("plain") == "plain"
    assert redact_sensitive(42) == 42
    assert redact_sensitive(None) is None


def test_input_is_not_mutated():
    src = comments_fixture()
    redact_sensitive(src)
    assert src[0]["agent"]["management"]["oauth_client_secret"] == SENTINEL


def test_non_string_keys_are_left_alone():
    assert redact_sensitive({1: SENTINEL, None: SENTINEL}) == {1: SENTINEL, None: SENTINEL}


# ── output_json: the boundary ───────────────────────────────────────────────

def test_output_json_redacts_and_raw_value_never_reaches_stdout(capsys):
    output_json(comments_fixture())
    out = capsys.readouterr().out
    assert SENTINEL not in out
    assert REDACTED in out
    parsed = json.loads(out)
    assert parsed[0]["agent"]["management"]["oauth_client_secret"] == REDACTED


def test_output_json_ok_false_still_exits_1_after_redaction(capsys):
    # Regression guard: redaction must not mask the fail-loud envelope, and the
    # exit decision reads the original payload.
    with pytest.raises(SystemExit) as exc:
        output_json({"ok": False, "error": "denied", "token": SENTINEL})
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert SENTINEL not in out
    assert json.loads(out)["ok"] is False


def test_output_json_has_no_bypass_parameter():
    # Pins ruling B: no reveal path exists at all. Re-adding one must be a
    # deliberate, reviewed change that also changes this test.
    params = inspect.signature(output_json).parameters
    assert list(params) == ["data"], params
    with pytest.raises(TypeError):
        output_json({"x": 1}, reveal_sensitive=True)  # noqa: unexpected kwarg


@pytest.mark.parametrize(
    "env",
    [
        {"PM_REDACT": "0"}, {"PM_NO_REDACT": "1"}, {"PM_REDACT_SECRETS": "false"},
        {"CLI_ANYTHING_NO_REDACT": "1"}, {"PM_AGENT_MODE": "0"}, {"DEBUG": "1"},
    ],
)
def test_no_environment_switch_disables_redaction(capsys, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    output_json({"password": SENTINEL})
    assert SENTINEL not in capsys.readouterr().out


# ── the real leak path: pm work-orders comments ─────────────────────────────

@pytest.fixture
def runner():
    return CliRunner()


def test_cli_work_orders_comments_renders_redacted_on_real_path(runner):
    with patch.object(http_backend, "get_comments", return_value=comments_fixture()):
        result = runner.invoke(cli, ["work-orders", "comments", "900001"])
    assert result.exit_code == 0, result.output
    assert SENTINEL not in result.output
    assert REDACTED in result.output
    data = json.loads(result.output)
    assert data[0]["agent"]["management"]["oauth_client_secret"] == REDACTED
    assert data[0]["agent"]["management"]["name"] == "Example Management"


def test_cli_work_orders_get_and_list_also_redacted(runner):
    # The same boundary covers every command; spot-check two read paths whose
    # backends could carry the same class of key.
    payload = {"id": 900001, "status": "OPEN", "agent": {"management": {"api_key": SENTINEL}}}
    with patch("cli_anything.propertymeld.api_backend.get_work_order", return_value=payload):
        result = runner.invoke(cli, ["work-orders", "get", "900001"])
    assert result.exit_code == 0, result.output
    assert SENTINEL not in result.output

    with patch("cli_anything.propertymeld.api_backend.list_work_orders", return_value=[payload]):
        result = runner.invoke(cli, ["work-orders", "list"])
    assert result.exit_code == 0, result.output
    assert SENTINEL not in result.output


# ── api-keys rotate: never displays, non-displaying delivery only ───────────

ROTATE_RESULT = {
    "ok": True, "key_id": 5, "friendly_name": "test",
    "client_id": "cid-123", "client_secret": SENTINEL,
}


def test_rotate_refuses_before_minting_when_no_destination(runner):
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)) as mint:
        result = runner.invoke(cli, ["api-keys", "rotate"])
    assert result.exit_code == 1, result.output
    mint.assert_not_called()
    data = json.loads(result.output)
    assert data["ok"] is False
    assert "Nothing was minted" in data["error"]
    assert SENTINEL not in result.output


def test_rotate_update_env_writes_pair_mode_0600_and_never_prints_secret(runner, tmp_path):
    # Dedicated subdir: the autouse config fixture also writes into tmp_path,
    # and the exact-listing assertion below must see only what rotate wrote.
    env_dir = tmp_path / "envdir"
    env_dir.mkdir()
    env_path = env_dir / "blue.env"
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)):
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-env", str(env_path)])
    assert result.exit_code == 0, result.output
    assert SENTINEL not in result.output
    data = json.loads(result.output)
    assert data["client_secret"] == REDACTED
    assert data["client_id"] == "cid-123"
    assert data["env_update"]["keys_written"] == ["PM_CLIENT_ID", "PM_CLIENT_SECRET"]
    assert data["env_update"]["mode"] == "0600"
    assert SENTINEL not in json.dumps(data["env_update"])
    content = env_path.read_text()
    assert "PM_CLIENT_ID=cid-123\n" in content
    assert f"PM_CLIENT_SECRET={SENTINEL}\n" in content
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    # Atomic: no temp sibling left behind in the destination directory.
    assert [p.name for p in env_dir.iterdir()] == ["blue.env"]


def test_rotate_update_env_refuses_before_minting_when_dir_missing(runner, tmp_path):
    missing = tmp_path / "nope" / "blue.env"
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)) as mint:
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-env", str(missing)])
    assert result.exit_code == 1, result.output
    mint.assert_not_called()
    assert "Nothing was minted" in json.loads(result.output)["error"]


def test_rotate_has_no_show_secret_flag(runner):
    result = runner.invoke(cli, ["api-keys", "rotate", "--show-secret"])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_update_env_file_replaces_in_place_preserves_others_and_export_form(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "BOT_TOKEN=keepme\nexport PM_CLIENT_ID=old-id\nCHAT_ID=1\nPM_CLIENT_SECRET=old-secret\n"
    )
    env_path.chmod(0o644)
    res = update_env_file(str(env_path), {"PM_CLIENT_ID": "new-id", "PM_CLIENT_SECRET": SENTINEL})
    assert res["keys_written"] == ["PM_CLIENT_ID", "PM_CLIENT_SECRET"]
    assert SENTINEL not in json.dumps(res)
    lines = env_path.read_text().splitlines()
    assert lines == ["BOT_TOKEN=keepme", "PM_CLIENT_ID=new-id", "CHAT_ID=1", f"PM_CLIENT_SECRET={SENTINEL}"]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_update_env_file_appends_missing_keys_and_creates_file(tmp_path):
    env_path = tmp_path / "fresh.env"
    update_env_file(str(env_path), {"PM_CLIENT_SECRET": SENTINEL})
    assert env_path.read_text() == f"PM_CLIENT_SECRET={SENTINEL}\n"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_update_env_file_rejects_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        update_env_file(str(tmp_path / "missing" / "x.env"), {"PM_CLIENT_SECRET": "v"})


# ── stderr error path + known over-redaction, pinned ────────────────────────

def _http_error(code, payload):
    """A urllib HTTPError whose body is the given JSON payload."""
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError("https://app.propertymeld.com/x", code, "err", {}, io.BytesIO(body))


def test_normalize_http_error_redacts_at_the_source():
    # Second-seat finding: seventeen call sites consume this dict and ten print
    # it straight to stderr without output_json. Redacting inside the
    # normalizer covers every one of them, present and future.
    from cli_anything.propertymeld.utils import normalize_http_error
    err = normalize_http_error(403, json.dumps({"detail": "forbidden", "client_secret": SENTINEL}))
    assert err["client_secret"] == REDACTED
    assert err["status_code"] == 403
    assert err["detail"] == "forbidden"
    assert SENTINEL not in json.dumps(err)


def test_real_http_backend_error_path_never_prints_secret_to_stderr(capsys):
    # Drives the REAL emission site (http_backend._http_get: HTTPError ->
    # print(json.dumps(normalize_http_error(...)), file=sys.stderr) -> exit 1).
    # A reimplemented helper call would stay green with the fix reverted; this
    # will not.
    err = _http_error(403, {"detail": "forbidden", "management": {"oauth_client_secret": SENTINEL}})
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit) as exc:
            http_backend._http_get("/api/x", "sessionid=abc")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert SENTINEL not in captured.err
    assert SENTINEL not in captured.out
    assert REDACTED in captured.err
    assert json.loads(captured.err.strip().splitlines()[-1])["status_code"] == 403


def test_real_api_backend_nexus_error_path_never_prints_secret_to_stderr(capsys):
    # The Nexus read path (api_backend._api_get, used by work-orders list/get)
    # has its own HTTPError handler that prints normalize_http_error(...) to
    # stderr. get_token is stubbed so the only urlopen call is the GET under test.
    from cli_anything.propertymeld import api_backend
    err = _http_error(400, {"status": ["bad choice"], "access_token": SENTINEL})
    with patch.object(api_backend, "get_token", return_value="stub-token"), \
         patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit) as exc:
            api_backend._api_get("/melds/")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert SENTINEL not in captured.err
    assert SENTINEL not in captured.out
    assert REDACTED in captured.err


def test_real_http_backend_no_exit_variant_returns_redacted_error():
    # The `return normalize_http_error(...)` sites feed the dict back into a
    # result that reaches output_json; prove the dict is already clean there.
    err = _http_error(422, {"error": "bad", "api_key": SENTINEL})
    with patch("urllib.request.urlopen", side_effect=err):
        result = http_backend._http_get_no_exit("/api/x", "sessionid=abc")
    assert result["api_key"] == REDACTED
    assert SENTINEL not in json.dumps(result)


def test_probe_token_prefix_is_over_redacted_by_design(capsys):
    # Known, accepted over-redaction: `pm probe` emits a deliberately safe
    # truncated token_prefix whose KEY matches /token/. Pinned so that adding
    # an allowlist later is a deliberate, reviewed change rather than drift.
    output_json({"ok": True, "token_prefix": "abcdefgh..."})
    assert json.loads(capsys.readouterr().out)["token_prefix"] == REDACTED
