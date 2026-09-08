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
import time
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
    scrub_sensitive_text,
    scrub_sensitive_text_narrow,
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
    env_delivery = [d for d in data["deliveries"] if d["path"] == "env"]
    assert len(env_delivery) == 1
    assert env_delivery[0]["status"] == "ok"
    assert env_delivery[0]["vars"] == ["PM_CLIENT_ID", "PM_CLIENT_SECRET"]
    assert env_delivery[0]["mode"] == "0600"
    assert SENTINEL not in json.dumps(data["deliveries"])
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
    # F2: a replaced line keeps its export prefix; a plain line stays plain.
    assert lines == ["BOT_TOKEN=keepme", "export PM_CLIENT_ID=new-id", "CHAT_ID=1", f"PM_CLIENT_SECRET={SENTINEL}"]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_update_env_file_appends_missing_keys_and_creates_file(tmp_path):
    env_path = tmp_path / "fresh.env"
    update_env_file(str(env_path), {"PM_CLIENT_SECRET": SENTINEL})
    assert env_path.read_text() == f"PM_CLIENT_SECRET={SENTINEL}\n"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


# ── F1: credential SHAPES inside free-text error excerpts ───────────────────

CANARY = "CANARY0notARealSecret9f3c2b7a1d4e5f6a7b8c"  # labelled synthetic, high-entropy shape


@pytest.mark.parametrize(
    "text,must_keep",
    [
        (f"Forbidden. Authorization: Bearer {CANARY} try again", ["Forbidden", "Authorization: Bearer", "try again"]),
        (f"proxy error basic {CANARY} upstream", ["proxy error", "basic", "upstream"]),
        (f"bad request client_secret={CANARY}&next=1", ["bad request", "client_secret=", "next=1"]),
        (f'{{"error": "x", "api_key": "{CANARY}"}}', ['"error": "x"', '"api_key": "']),
        (f"token: {CANARY} expired", ["token: ", "expired"]),
        (f"see {CANARY} in the log", ["see", "in the log"]),
    ],
)
def test_scrub_sensitive_text_removes_value_shapes_but_keeps_surroundings(text, must_keep):
    out = scrub_sensitive_text(text)
    assert CANARY not in out
    assert REDACTED in out
    for piece in must_keep:
        assert piece in out, (piece, out)


def test_scrub_sensitive_text_leaves_ordinary_prose_alone():
    prose = "HTTP 502 Bad Gateway: the upstream server is temporarily unavailable, retry in 30 seconds"
    assert scrub_sensitive_text(prose) == prose
    assert scrub_sensitive_text("") == ""


def test_real_http_backend_html_error_body_canary_never_reaches_stderr(capsys):
    # The F1 path: a NON-JSON (HTML) error page echoing an Authorization header.
    # Key-match cannot see inside a string; the excerpt scrubber must.
    html = f"<html><body><h1>403 Forbidden</h1><p>Authorization: Bearer {CANARY}</p><p>Contact support</p></body></html>"
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 403, "err", {}, io.BytesIO(html.encode()))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    captured = capsys.readouterr()
    assert CANARY not in captured.err
    assert CANARY not in captured.out
    assert REDACTED in captured.err
    # CONTROL: the excerpt still carries its surrounding text.
    assert "403 Forbidden" in captured.err
    assert "Contact support" in captured.err


def test_real_http_backend_plaintext_error_body_canary_never_reaches_stderr(capsys):
    # The `detail` path: non-JSON, non-HTML plaintext body with key=value.
    body = f"upstream rejected: client_secret={CANARY} please retry later"
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 502, "err", {}, io.BytesIO(body.encode()))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    captured = capsys.readouterr()
    assert CANARY not in captured.err
    assert REDACTED in captured.err
    assert "upstream rejected" in captured.err
    assert "please retry later" in captured.err


def test_excerpt_is_scrubbed_before_truncation_so_no_head_leaks():
    # A value that STRADDLES the 200-char cap: a truncate-first implementation
    # would keep the head of the credential inside the excerpt. Scrubbing first
    # replaces the whole value, so no prefix of it can survive the cut. The
    # canary is positioned to start before char 200 and end after it; with the
    # scrubber neutered this test goes red (verified by mutation).
    from cli_anything.propertymeld.utils import normalize_http_error
    prefix = "<html>" + ("x " * 78) + "Authorization: Bearer "
    start = len(" ".join(prefix.split()))
    assert 150 < start < 200, start  # canary begins inside the excerpt window
    html = f"{prefix}{CANARY}</html>"
    excerpt = normalize_http_error(403, html)["body_excerpt"]
    assert len(excerpt) <= 200
    assert CANARY[:16] not in excerpt  # the head that truncate-first would leak
    assert CANARY not in excerpt
    assert REDACTED in excerpt or excerpt.endswith("Bearer") or "Bearer" in excerpt


# ── F3: newline injection refused before the file is touched ────────────────

@pytest.mark.parametrize("bad", ["v\nINJECTED=1", "v\rINJECTED=1", "v\r\n"])
def test_update_env_file_refuses_newline_in_value_and_leaves_file_untouched(tmp_path, bad):
    env_path = tmp_path / ".env"
    original = "BOT_TOKEN=keepme\nPM_CLIENT_SECRET=old\n"
    env_path.write_text(original)
    with pytest.raises(ValueError):
        update_env_file(str(env_path), {"PM_CLIENT_SECRET": bad})
    assert env_path.read_text() == original
    assert [p.name for p in tmp_path.iterdir() if p.name != "propertymeld-config.json"] == [".env"]


def test_update_env_file_refuses_newline_in_key(tmp_path):
    with pytest.raises(ValueError):
        update_env_file(str(tmp_path / ".env"), {"PM_CLIENT_SECRET\nX": "v"})


def test_rotate_delivery_failure_exits_nonzero_and_never_prints_secret(runner, tmp_path):
    # Minted but undelivered must never exit 0.
    env_dir = tmp_path / "envdir"; env_dir.mkdir()
    env_path = env_dir / "blue.env"
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)), \
         patch("cli_anything.propertymeld.cli.update_env_file", side_effect=ValueError("simulated write refusal")):
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-env", str(env_path)])
    assert result.exit_code == 1, result.output
    assert SENTINEL not in result.output
    data = json.loads(result.output)
    assert data["ok"] is False
    assert "delivery path failed" in data["error"]
    env_delivery = [d for d in data["deliveries"] if d["path"] == "env"]
    assert env_delivery[0]["status"] == "error"


# ── Codex C1: delivery status must SURVIVE redaction ─────────────────────────

class _Proc:
    def __init__(self, rc, stderr="", stdout=""):
        self.returncode = rc
        self.stderr = stderr
        self.stdout = stdout


def test_c1_railway_delivery_status_survives_redaction_ok(runner):
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)), \
         patch("subprocess.run", return_value=_Proc(0)):
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-railway"])
    assert result.exit_code == 0, result.output
    assert SENTINEL not in result.output
    data = json.loads(result.output)
    rail = [d for d in data["deliveries"] if d["path"] == "railway"]
    # One entry for the PAIR (U1): status readable, no rollback on success.
    assert len(rail) == 1 and rail[0]["vars"] == ["PM_CLIENT_ID", "PM_CLIENT_SECRET"]
    assert rail[0]["status"] == "ok" and "rollback" not in rail[0]
    assert "railway_updates" not in data
    assert REDACTED not in json.dumps(data["deliveries"])


def test_c1_railway_delivery_failure_readable_and_exits_1(runner):
    # The combined set fails: the pair entry reads error, exit 1.
    def fake_run(cmd, **kw):
        if "--json" in cmd:
            return _Proc(0, stdout=json.dumps({"PM_CLIENT_ID": "old-id", "PM_CLIENT_SECRET": "old-secret"}))
        return _Proc(1, "set failed")
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)), \
         patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-railway"])
    assert result.exit_code == 1, result.output
    assert SENTINEL not in result.output
    data = json.loads(result.output)
    assert data["ok"] is False
    rail = [d for d in data["deliveries"] if d["path"] == "railway"][0]
    assert rail["status"] == "error" and "set failed" in rail["detail"]


# ── Codex C2: JSON ARRAY error bodies stay structured and get redacted ───────

def test_c2_array_error_body_is_redacted_structurally():
    from cli_anything.propertymeld.utils import normalize_http_error
    err = normalize_http_error(400, json.dumps([{"client_secret": CANARY, "field": "x"}, {"ok": 1}]))
    assert isinstance(err["detail"], list)
    assert err["detail"][0]["client_secret"] == REDACTED
    assert err["detail"][0]["field"] == "x"
    assert CANARY not in json.dumps(err)


def test_c2_real_http_backend_array_error_body_canary_never_reaches_stderr(capsys):
    body = json.dumps([{"client_secret": CANARY, "message": "denied"}]).encode()
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 400, "err", {}, io.BytesIO(body))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    captured = capsys.readouterr()
    assert CANARY not in captured.err
    assert REDACTED in captured.err
    printed = json.loads(captured.err.strip().splitlines()[-1])
    assert isinstance(printed["detail"], list)
    assert printed["detail"][0]["message"] == "denied"


# ── Codex C3: duplicate definitions collapse to exactly one, the new one ────

def test_c3_duplicate_definitions_replaced_first_and_later_dropped(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PM_CLIENT_SECRET=old-1\nBOT_TOKEN=keep\nexport PM_CLIENT_SECRET=old-2\nPM_CLIENT_ID=id-old\nPM_CLIENT_SECRET=old-3\n"
    )
    update_env_file(str(env_path), {"PM_CLIENT_ID": "id-new", "PM_CLIENT_SECRET": SENTINEL})
    lines = env_path.read_text().splitlines()
    assert lines == ["PM_CLIENT_SECRET=" + SENTINEL, "BOT_TOKEN=keep", "PM_CLIENT_ID=id-new"]
    assert sum(1 for l in lines if l.split("=")[0].replace("export ", "") == "PM_CLIENT_SECRET") == 1
    assert "old-" not in env_path.read_text()


# ── Codex X1/X2: credential-shaped STRING LEAVES inside parsed JSON ──────────

def test_x2_dict_string_leaf_in_error_body_never_reaches_stderr(capsys):
    body = json.dumps({"detail": f"denied: client_secret={CANARY} please retry"}).encode()
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 403, "err", {}, io.BytesIO(body))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    captured = capsys.readouterr()
    assert CANARY not in captured.err
    assert REDACTED in captured.err
    assert "denied" in captured.err and "please retry" in captured.err  # control


def test_x1_x2_bare_string_array_error_body_never_reaches_stderr(capsys):
    # X1 second path: an array whose elements are bare strings has no keys
    # for the key walk to see; the string-leaf scrub is what covers it.
    body = json.dumps([f"client_secret={CANARY}", "field required"]).encode()
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 400, "err", {}, io.BytesIO(body))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    captured = capsys.readouterr()
    assert CANARY not in captured.err
    assert REDACTED in captured.err
    printed = json.loads(captured.err.strip().splitlines()[-1])
    assert isinstance(printed["detail"], list)
    assert printed["detail"][1] == "field required"  # control: list + prose preserved


def test_x2_high_entropy_string_leaf_in_error_body_is_scrubbed():
    from cli_anything.propertymeld.utils import normalize_http_error
    err = normalize_http_error(500, json.dumps({"message": f"trace {CANARY} end"}))
    assert CANARY not in json.dumps(err)
    assert "trace" in err["message"] and "end" in err["message"]


def test_x2_output_json_string_leaves_get_precise_shapes_only(capsys):
    # Ordinary output: Bearer/key=value shapes inside a string are scrubbed...
    output_json({"description": f"call with Bearer {CANARY} now", "note": f"password: {CANARY}"})
    out = capsys.readouterr().out
    assert CANARY not in out
    data = json.loads(out)
    assert data["description"].startswith("call with Bearer") and data["description"].endswith("now")


def test_x2_output_json_does_not_apply_entropy_rule_to_normal_ids(capsys):
    # ...but a UUID or long identifier in a normal payload MUST survive, or
    # the CLI is unusable. Pins the full/narrow split.
    uuid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    long_id = "meld_20260908T045400Z_a1b2c3d4e5f6"
    output_json({"id": uuid, "ref": long_id, "hash": "d41d8cd98f00b204e9800998ecf8427e"})
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == uuid and data["ref"] == long_id
    assert data["hash"] == "d41d8cd98f00b204e9800998ecf8427e"
    assert scrub_sensitive_text_narrow(uuid) == uuid
    assert scrub_sensitive_text(uuid) == REDACTED  # the full rule WOULD redact it


# ── Codex X3: railway binary missing must not lose the secret ────────────────

def test_x3_railway_oserror_recorded_env_still_written_exit_1(runner, tmp_path):
    env_dir = tmp_path / "envdir"; env_dir.mkdir()
    env_path = env_dir / "blue.env"
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)), \
         patch("subprocess.run", side_effect=FileNotFoundError("railway: command not found")):
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-railway", "--update-env", str(env_path)])
    assert result.exit_code == 1, result.output
    assert SENTINEL not in result.output
    data = json.loads(result.output)
    assert data["ok"] is False
    rail = [d for d in data["deliveries"] if d["path"] == "railway"][0]
    assert rail["status"] == "error" and "could not be run" in rail["detail"]
    assert rail["rollback"] == "skipped"  # the read also failed, nothing to restore
    env = [d for d in data["deliveries"] if d["path"] == "env"][0]
    assert env["status"] == "ok"
    assert f"PM_CLIENT_SECRET={SENTINEL}\n" in env_path.read_text()


# ── Codex N1: quoted credential values are redacted as a unit ────────────────

PHRASE = "correct horse battery staple"


@pytest.mark.parametrize(
    "text,expect_gone,must_keep",
    [
        (f'password: "{PHRASE}" then more', [PHRASE, "horse"], ['password: "', '"', "then more"]),
        (f"token='{PHRASE}; extra' tail", [PHRASE, "extra"], ["token='", "'", "tail"]),
        (f'{{"api_key": "{PHRASE}", "ok": true}}', [PHRASE], ['"api_key": "', '"ok": true']),
        (f'password: "say \\"hi\\" {PHRASE}" end', [PHRASE], ["end"]),
    ],
)
def test_n1_quoted_values_fully_redacted(text, expect_gone, must_keep):
    out = scrub_sensitive_text(text, high_entropy=False)
    for g in expect_gone:
        assert g not in out, (g, out)
    assert REDACTED in out
    for k in must_keep:
        assert k in out, (k, out)


def test_n1_unquoted_value_still_redacts_exactly_the_token():
    out = scrub_sensitive_text("client_secret=abc123&next=1 rest", high_entropy=False)
    assert out == f"client_secret={REDACTED}&next=1 rest"


def test_n1_real_error_path_quoted_value_with_spaces_never_reaches_stderr(capsys):
    body = json.dumps({"detail": f'auth failed for password: "{PHRASE}" retry'}).encode()
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 403, "err", {}, io.BytesIO(body))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    e = capsys.readouterr().err
    assert PHRASE not in e and "horse" not in e
    assert REDACTED in e and "auth failed" in e and "retry" in e


def test_n1_output_json_quoted_value_with_semicolon_fully_redacted(capsys):
    output_json({"note": f'reset token: "{PHRASE}; part2" done'})
    out = capsys.readouterr().out
    assert PHRASE not in out and "part2" not in out
    data = json.loads(out)
    assert data["note"].startswith('reset token: "') and data["note"].endswith("done")


# ── Codex N2: incomplete rotate response must deliver nothing ────────────────

@pytest.mark.parametrize("bad", [{"client_secret": None}, {"client_secret": ""}, {"client_id": None}, {"client_secret": "   "}])
def test_n2_incomplete_rotate_response_delivers_nothing_and_exits_1(runner, tmp_path, bad):
    env_dir = tmp_path / "envdir"; env_dir.mkdir()
    env_path = env_dir / "blue.env"
    original = "BOT_TOKEN=keep\nPM_CLIENT_SECRET=old\n"
    env_path.write_text(original)
    resp = dict(ROTATE_RESULT); resp.update(bad)
    with patch.object(http_backend, "rotate_api_key", return_value=resp), \
         patch("subprocess.run") as spawn:
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-railway", "--update-env", str(env_path)])
    assert result.exit_code == 1, result.output
    spawn.assert_not_called()
    assert env_path.read_text() == original
    data = json.loads(result.output)
    assert data["ok"] is False and data["deliveries"] == []
    missing = next(iter(bad))
    assert missing in data["error"]
    assert "minted server-side" in data["error"]
    assert "None" not in result.output.replace("ok\": false", "")
    assert SENTINEL not in result.output


# ── Codex R1 control: the accepted narrow-scrub design on ordinary output ────

def test_r1_bare_high_entropy_value_under_non_sensitive_key_survives_in_normal_output(capsys):
    # By design (reviewed): ordinary output applies only precise shapes, never
    # the entropy rule, so a bare high-entropy VALUE under a NON-sensitive key
    # is not redacted there (UUIDs and long ids must survive). The same value
    # IS redacted on the error path, where the full rule applies.
    output_json({"description": CANARY})
    assert CANARY in capsys.readouterr().out
    from cli_anything.propertymeld.utils import normalize_http_error
    assert CANARY not in json.dumps(normalize_http_error(500, json.dumps({"description": CANARY})))


# ── Codex Q1: an UNTERMINATED quoted value fails closed ─────────────────────

@pytest.mark.parametrize(
    "text,must_keep",
    [
        (f'password: "{PHRASE} and no close', ['password: "']),
        (f"token='{PHRASE} still open", ["token='"]),
    ],
)
def test_q1_unterminated_quote_extends_to_end_fail_closed(text, must_keep):
    out = scrub_sensitive_text(text, high_entropy=False)
    assert PHRASE not in out and "horse" not in out and "no close" not in out and "still open" not in out
    assert REDACTED in out
    for k in must_keep:
        assert k in out


def test_q1_terminated_quote_still_stops_at_its_close():
    out = scrub_sensitive_text(f'password: "{PHRASE}" then tail', high_entropy=False)
    assert out == f'password: "{REDACTED}" then tail'


def test_q1_real_error_path_unterminated_quote_never_reaches_stderr(capsys):
    body = json.dumps({"detail": f'bad password: "{PHRASE} no close'}).encode()
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 403, "err", {}, io.BytesIO(body))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    e = capsys.readouterr().err
    assert PHRASE not in e and "horse" not in e
    assert REDACTED in e and "bad password" in e


def test_q1_output_json_unterminated_single_quote_redacted(capsys):
    output_json({"note": f"reset token='{PHRASE} open"})
    out = capsys.readouterr().out
    assert PHRASE not in out and "horse" not in out
    assert json.loads(out)["note"].startswith("reset token='")


# ── Codex Q3: an existing but unusable env target refuses BEFORE minting ────

def _rotate_with_env(runner, env_path):
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)) as mint, \
         patch("subprocess.run") as spawn:
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-env", str(env_path)])
    return result, mint, spawn


def test_q3_unwritable_existing_target_refuses_before_minting(runner, tmp_path):
    # chmod 000 trips the W_OK check first, so this covers the NOT-WRITABLE
    # branch of the preflight (the read branch is covered by the 0o200 case).
    if os.geteuid() == 0:
        pytest.skip("root bypasses mode bits; the permission casualty needs an unprivileged runner")
    env_path = tmp_path / "locked.env"
    env_path.write_text("PM_CLIENT_SECRET=old\n")
    env_path.chmod(0o000)
    try:
        result, mint, spawn = _rotate_with_env(runner, env_path)
    finally:
        env_path.chmod(0o600)
    assert result.exit_code == 1, result.output
    mint.assert_not_called()
    spawn.assert_not_called()
    data = json.loads(result.output)
    assert data["ok"] is False and "Nothing was minted" in data["error"]
    assert "not writable" in data["error"]
    assert env_path.read_text() == "PM_CLIENT_SECRET=old\n"


def test_q3_write_only_existing_target_reaches_read_branch_and_refuses(runner, tmp_path):
    # 0o200 passes the W_OK check and then fails the read, so this is the
    # only shipped test that enters the except-OSError READ branch.
    if os.geteuid() == 0:
        pytest.skip("root bypasses mode bits; the permission casualty needs an unprivileged runner")
    env_path = tmp_path / "writeonly.env"
    env_path.write_text("PM_CLIENT_SECRET=old\n")
    env_path.chmod(0o200)
    try:
        result, mint, spawn = _rotate_with_env(runner, env_path)
    finally:
        env_path.chmod(0o600)
    assert result.exit_code == 1, result.output
    mint.assert_not_called()
    spawn.assert_not_called()
    data = json.loads(result.output)
    assert "cannot be read" in data["error"] and "Nothing was minted" in data["error"]
    assert env_path.read_text() == "PM_CLIENT_SECRET=old\n"


def test_q3_invalid_utf8_existing_target_refuses_before_minting(runner, tmp_path):
    env_path = tmp_path / "corrupt.env"
    original = b"PM_CLIENT_SECRET=old\n\xff\xfe\xfa\n"
    env_path.write_bytes(original)
    result, mint, spawn = _rotate_with_env(runner, env_path)
    assert result.exit_code == 1, result.output
    mint.assert_not_called()
    spawn.assert_not_called()
    data = json.loads(result.output)
    assert "not valid UTF-8" in data["error"] and "Nothing was minted" in data["error"]
    assert env_path.read_bytes() == original


def test_q3_valid_existing_target_proceeds(runner, tmp_path):
    env_path = tmp_path / "fine.env"
    env_path.write_text("BOT_TOKEN=keep\nPM_CLIENT_SECRET=old\n")
    result, mint, spawn = _rotate_with_env(runner, env_path)
    assert result.exit_code == 0, result.output
    mint.assert_called_once()
    assert f"PM_CLIENT_SECRET={SENTINEL}\n" in env_path.read_text()
    assert "BOT_TOKEN=keep" in env_path.read_text()


# ── Codex Q2: the non-JSON HTTP 200 fallback excerpt is scrubbed ─────────────

def test_q2_plaintext_200_body_fallback_never_prints_secret_to_stderr(capsys):
    # A non-HTML, non-JSON 200 body has no inferred status, so
    # _parse_json_body_or_exit takes the fallback branch that builds its own
    # excerpt. Drive the real function with that body.
    body = f"gateway notice: client_secret={CANARY} and Authorization: Bearer {CANARY} please retry".encode()
    with pytest.raises(SystemExit) as exc:
        http_backend._parse_json_body_or_exit(body)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert CANARY not in captured.err and CANARY not in captured.out
    assert REDACTED in captured.err
    printed = json.loads(captured.err.strip().splitlines()[-1])
    assert printed["error"] == "Non-JSON response body"
    assert "gateway notice" in printed["body_excerpt"] and "please retry" in printed["body_excerpt"]


def test_q2_fallback_excerpt_is_scrubbed_before_truncation(capsys):
    # The canary must START inside the 200-char window with at least 16 of its
    # characters before the cut, so that a truncate-first implementation would
    # visibly leak its head; a canary placed too close to the cut would leave
    # fewer characters than the assertion looks for and pass vacuously.
    prefix = "x " * 79 + "Authorization: Bearer "
    start = len(" ".join(prefix.split()))
    assert 150 < start <= 184, start
    with pytest.raises(SystemExit):
        http_backend._parse_json_body_or_exit(f"{prefix}{CANARY}".encode())
    e = capsys.readouterr().err
    assert CANARY[:16] not in e and CANARY not in e


# ── O1: the scrub-before-truncate ORDER is guarded by a BARE high-entropy value ─
#
# A Bearer-labelled canary cannot guard the order: after truncation the label
# plus a fragment still matches the auth rule, so truncate-then-scrub stays
# green. A bare high-entropy value straddling the 200-char cut leaves a
# fragment shorter than the 24-char entropy floor under truncate-first, which
# leaks, and is scrubbed whole under the shipped scrub-first order.

BARE = "Q7m3kP9zX2vL8nR4tW6yB1cF5hJ0dS3gA9eK2uM7v"  # 41 chars, letters+digits, no label
assert len(BARE) == 41 and any(c.isdigit() for c in BARE) and any(c.isalpha() for c in BARE)


def _straddling(prefix_words: int, lead: str) -> str:
    prefix = "x " * prefix_words + lead
    start = len(" ".join(prefix.split()))
    assert 150 < start <= 184, start  # at least 16 chars of BARE land inside the cut
    return f"{prefix}{BARE}"


def test_o1_q2_fallback_bare_high_entropy_straddle_head_absent(capsys):
    with pytest.raises(SystemExit):
        http_backend._parse_json_body_or_exit(_straddling(88, "notice ").encode())
    e = capsys.readouterr().err
    assert BARE[:16] not in e and BARE not in e


def test_o1_normalizer_html_excerpt_bare_high_entropy_straddle_head_absent():
    from cli_anything.propertymeld.utils import normalize_http_error
    html = "<html>" + _straddling(85, "notice ") + "</html>"
    excerpt = normalize_http_error(403, html)["body_excerpt"]
    assert BARE[:16] not in excerpt and BARE not in excerpt


def test_o1_normalizer_plaintext_detail_bare_high_entropy_straddle_head_absent():
    from cli_anything.propertymeld.utils import normalize_http_error
    text = _straddling(88, "notice ")
    # detail is capped at 300, so position the value across THAT cut.
    text = "y " * 50 + text
    detail = normalize_http_error(502, text)["detail"]
    assert BARE[:16] not in detail and BARE not in detail


# ── Codex S1: escaped serialized JSON inside a string leaf ───────────────────

ESC_DICT = '{"client_secret": "hunter2", "ok": true}'        # a leaf whose VALUE is serialized JSON
ESC_LIST = '[{"api_key": "hunter2"}, "field required"]'
DOUBLE = json.dumps(ESC_DICT)                                  # double-encoded: a JSON string of JSON
LITERAL_ESCAPED = 'msg={\\"client_secret\\": \\"hunter2\\"} end'  # literal backslash-quotes in the text


@pytest.mark.parametrize("leaf", [ESC_DICT, ESC_LIST, DOUBLE])
def test_s1_serialized_json_leaf_is_walked_on_ordinary_output(capsys, leaf):
    output_json({"note": leaf})
    out = capsys.readouterr().out
    assert "hunter2" not in out
    data = json.loads(out)
    inner = json.loads(data["note"])
    if isinstance(inner, str):
        inner = json.loads(inner)
    if isinstance(inner, dict):
        assert inner["client_secret"] == REDACTED and inner["ok"] is True
    else:
        assert inner[0]["api_key"] == REDACTED and inner[1] == "field required"


@pytest.mark.parametrize("leaf", [ESC_DICT, ESC_LIST])
def test_s1_serialized_json_leaf_is_walked_on_real_error_path(capsys, leaf):
    body = json.dumps({"detail": leaf}).encode()
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 403, "err", {}, io.BytesIO(body))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    e = capsys.readouterr().err
    assert "hunter2" not in e and REDACTED in e


# The flat text rule already catches a quoted "key": "value" pair, so the
# JSON-leaf WALK needs shapes the text rule cannot see: a sensitive key whose
# value is a nested object or a list. The text rule stops at the opening
# brace or bracket and the credential inside leaks; the walk drops the whole
# subtree under the key. These are the casualties that go red when the walk
# is disabled (mutation M10).
NESTED_OBJ = '{"token_config": {"inner": {"value": "hunter2"}}, "name": "svc"}'
NESTED_LIST = '{"secrets": ["hunter2", "alsoSecret1"], "count": 2}'


@pytest.mark.parametrize("leaf", [NESTED_OBJ, NESTED_LIST])
def test_s1_walk_catches_nested_container_under_sensitive_key_on_ordinary_output(capsys, leaf):
    output_json({"note": leaf})
    out = capsys.readouterr().out
    assert "hunter2" not in out and "alsoSecret1" not in out
    inner = json.loads(json.loads(out)["note"])
    key = "token_config" if "token_config" in leaf else "secrets"
    assert inner[key] == REDACTED
    assert inner.get("name") == "svc" or inner.get("count") == 2


@pytest.mark.parametrize("leaf", [NESTED_OBJ, NESTED_LIST])
def test_s1_walk_catches_nested_container_under_sensitive_key_on_real_error_path(capsys, leaf):
    body = json.dumps({"detail": leaf}).encode()
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 403, "err", {}, io.BytesIO(body))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    e = capsys.readouterr().err
    assert "hunter2" not in e and "alsoSecret1" not in e and REDACTED in e


# ── T1: the walk must not turn valid JSON into output a strict parser rejects ─
#
# Python's json.loads ACCEPTS Infinity and NaN, so a round-trip proves nothing
# on the broken output; and "no Infinity" alone cannot tell the fallback apart
# from a leaf that never reached the walk. So: parse with a STRICT reader,
# prove the leaf reached the walk (spy), and prove the fallback ran (the
# literal 1e400 survives only on the text path; the walk would re-serialize).

def _strict_loads(text):
    def _reject(c):
        raise ValueError(f"non-finite literal {c}")
    return json.loads(text, parse_constant=_reject)


def _walk_spy():
    from cli_anything.propertymeld import utils as u
    calls = []
    real = u._parse_json_leaf
    def spy(text):
        r = real(text)
        container = r[0] if isinstance(r, tuple) else r
        calls.append((text, type(container).__name__))
        return r
    return patch.object(u, "_parse_json_leaf", side_effect=spy), calls


def test_t1_out_of_range_number_leaf_is_strict_json_and_fallback_ran(capsys):
    leaf = '{"x": 1e400, "name": "svc"}'
    ctx, calls = _walk_spy()
    with ctx:
        output_json({"note": leaf})
    out = capsys.readouterr().out
    assert "Infinity" not in out and "NaN" not in out
    note = json.loads(out)["note"]
    _strict_loads(note)                       # strict reader accepts the emitted leaf
    assert "1e400" in note and "svc" in note  # original text preserved => text fallback ran
    assert any(t == leaf and kind == "dict" for t, kind in calls)  # the leaf reached the walk


def test_t1_credential_inside_out_of_range_leaf_is_still_scrubbed_by_fallback(capsys):
    leaf = '{"x": 1e400, "client_secret": "hunter2"}'
    ctx, calls = _walk_spy()
    with ctx:
        output_json({"note": leaf})
    out = capsys.readouterr().out
    assert "hunter2" not in out and REDACTED in out
    assert "Infinity" not in out and "NaN" not in out
    note = json.loads(out)["note"]
    _strict_loads(note)
    assert "1e400" in note
    assert any(t == leaf and kind == "dict" for t, kind in calls)


def test_t1_normal_json_leaf_still_walks_and_is_strict(capsys):
    leaf = '{"client_secret": "hunter2", "n": 1.5}'
    output_json({"note": leaf})
    out = capsys.readouterr().out
    inner = _strict_loads(json.loads(out)["note"])
    assert inner["client_secret"] == REDACTED and inner["n"] == 1.5


def test_t1_real_error_path_out_of_range_leaf_is_strict(capsys):
    body = json.dumps({"detail": '{"x": 1e400, "api_key": "hunter2"}'}).encode()
    err = urllib.error.HTTPError("https://app.propertymeld.com/x", 403, "err", {}, io.BytesIO(body))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(SystemExit):
            http_backend._http_get("/api/x", "sessionid=abc")
    e = capsys.readouterr().err
    assert "Infinity" not in e and "NaN" not in e and "hunter2" not in e
    printed = _strict_loads(e.strip().splitlines()[-1])
    assert "1e400" in printed["detail"]


# ── U1 / V1: the Railway pair, batching and rollback, stated honestly ────────
#
# On a NON-atomic backend no implementation can avoid a transient mixed pair
# when a batch partially applies, so "never mixed" is not an unconditional
# claim. Two halves guard what is actually true:
#   (a) success path, ATOMIC fake, snapshot after EVERY railway call: single-
#       call yields no mixed snapshot; per-variable sets yield the inter-call
#       snapshot NEWID+OLDSECRET even when both succeed. This guards batching.
#   (b) failure path, NON-atomic fake that lands the id, rejects the secret and
#       returns non-zero: the mixed window is exactly one call wide, the very
#       next railway call is the rollback, and the end state is both-old.
#       When the rollback itself fails the end state stays mixed and is
#       recorded as rollback error (honest limit).

OLD_PAIR = {"PM_CLIENT_ID": "old-id-111", "PM_CLIENT_SECRET": "old-secret-placeholder"}
NEW_PAIR = {"PM_CLIENT_ID": "cid-123", "PM_CLIENT_SECRET": SENTINEL}


def _mixed(state):
    return (state["PM_CLIENT_ID"] == NEW_PAIR["PM_CLIENT_ID"]) != (state["PM_CLIENT_SECRET"] == NEW_PAIR["PM_CLIENT_SECRET"])


def _fake_railway(*, atomic=True, fail_secret=False, fail_read=False, fail_rollback=False,
                  read_payload=None):
    """A fake railway CLI recording applied state and a snapshot after every call.

    atomic=True applies a --set batch all-or-nothing; atomic=False applies each
    --set in order and stops at the first rejected one (non-atomic backend).
    """
    state = dict(OLD_PAIR)
    history = []   # (label, snapshot) after each railway invocation
    calls = []
    def run(cmd, **kw):
        calls.append(list(cmd))
        if "--json" in cmd:
            if fail_read:
                history.append(("read", dict(state))); return _Proc(1, "read failed")
            payload = json.dumps(state) if read_payload is None else read_payload
            history.append(("read", dict(state))); return _Proc(0, stdout=payload)
        sets = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--set"]
        is_rollback = any(v.split("=", 1)[1] == OLD_PAIR["PM_CLIENT_SECRET"] for v in sets)
        rc = 0
        if is_rollback:
            if fail_rollback:
                rc = 1
            else:
                for kv in sets:
                    k, v = kv.split("=", 1); state[k] = v
        else:
            reject = {kv for kv in sets if kv.startswith("PM_CLIENT_SECRET=")} if fail_secret else set()
            if atomic and reject:
                rc = 1                                   # all-or-nothing: nothing lands
            else:
                for kv in sets:
                    if kv in reject:
                        rc = 1; break                    # non-atomic: earlier sets already landed
                    k, v = kv.split("=", 1); state[k] = v
        history.append(("rollback" if is_rollback else "set", dict(state)))
        return _Proc(rc, "set failed" if rc else "")
    return run, state, history, calls


def _rotate_railway(runner, run):
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)), patch("subprocess.run", side_effect=run):
        return runner.invoke(cli, ["api-keys", "rotate", "--update-railway"])


def test_v1a_success_history_never_holds_a_mixed_pair(runner):
    # Guards the BATCHING: per-variable sets would leave the inter-call
    # snapshot NEWID+OLDSECRET in the history even on full success.
    run, state, history, calls = _fake_railway(atomic=True)
    result = _rotate_railway(runner, run)
    assert result.exit_code == 0, result.output
    assert state == NEW_PAIR
    assert not any(_mixed(snap) for _, snap in history), history
    set_calls = [c for c in calls if "--set" in c]
    assert len(set_calls) == 1 and set_calls[0].count("--set") == 2
    rail = [d for d in json.loads(result.output)["deliveries"] if d["path"] == "railway"][0]
    assert rail["status"] == "ok" and "rollback" not in rail
    assert SENTINEL not in result.output


def test_v1b_non_atomic_failure_window_is_one_call_and_closed_by_rollback(runner):
    run, state, history, calls = _fake_railway(atomic=False, fail_secret=True)
    result = _rotate_railway(runner, run)
    assert result.exit_code == 1, result.output
    labels = [l for l, _ in history]
    mixed_idx = [i for i, (_, snap) in enumerate(history) if _mixed(snap)]
    assert mixed_idx, "the non-atomic fake must have produced the transient mixed state"
    # the window: exactly the failed batch call, immediately followed by the rollback
    assert labels[mixed_idx[0]] == "set" and labels[mixed_idx[0] + 1] == "rollback"
    assert len(mixed_idx) == 1
    assert state == OLD_PAIR                                   # end state both-old
    rollback = [c for c in calls if "--set" in c][-1]
    assert any(OLD_PAIR["PM_CLIENT_SECRET"] in a for a in rollback) and not any(SENTINEL in a for a in rollback)
    rail = [d for d in json.loads(result.output)["deliveries"] if d["path"] == "railway"][0]
    assert rail["status"] == "error" and rail["rollback"] == "ok"
    assert SENTINEL not in result.output and OLD_PAIR["PM_CLIENT_SECRET"] not in result.output


def test_v1c_non_atomic_failure_with_failed_rollback_leaves_mixed_and_records_it(runner):
    # Honest limit: when the rollback itself fails the pair stays mixed; the
    # command says so (rollback error) and exits 1 rather than claiming safety.
    run, state, history, calls = _fake_railway(atomic=False, fail_secret=True, fail_rollback=True)
    result = _rotate_railway(runner, run)
    assert result.exit_code == 1, result.output
    assert _mixed(state)
    rail = [d for d in json.loads(result.output)["deliveries"] if d["path"] == "railway"][0]
    assert rail["status"] == "error" and rail["rollback"] == "error" and "set failed" in rail["rollback_detail"]


def test_v1d_read_returns_only_one_of_the_pair_records_rollback_skipped(runner):
    run, state, history, calls = _fake_railway(atomic=False, fail_secret=True,
                                               read_payload=json.dumps({"PM_CLIENT_ID": "old-id-111"}))
    result = _rotate_railway(runner, run)
    assert result.exit_code == 1, result.output
    rail = [d for d in json.loads(result.output)["deliveries"] if d["path"] == "railway"][0]
    assert rail["status"] == "error" and rail["rollback"] == "skipped"


def test_v1e_read_returns_unparseable_json_records_rollback_skipped(runner):
    run, state, history, calls = _fake_railway(atomic=False, fail_secret=True, read_payload="not json {")
    result = _rotate_railway(runner, run)
    assert result.exit_code == 1, result.output
    rail = [d for d in json.loads(result.output)["deliveries"] if d["path"] == "railway"][0]
    assert rail["status"] == "error" and rail["rollback"] == "skipped"


def test_v1f_read_fails_then_set_fails_records_rollback_skipped(runner):
    run, state, history, calls = _fake_railway(atomic=True, fail_secret=True, fail_read=True)
    result = _rotate_railway(runner, run)
    assert result.exit_code == 1, result.output
    assert state == OLD_PAIR
    rail = [d for d in json.loads(result.output)["deliveries"] if d["path"] == "railway"][0]
    assert rail["status"] == "error" and rail["rollback"] == "skipped"


# ── V2: a pathologically deep serialized leaf must not crash the stdout boundary ─

def _deep_leaf(depth, secret):
    return "[" * depth + json.dumps({"client_secret": secret}) + "]" * depth


def test_v2_5000_deep_leaf_still_produces_output_with_credential_redacted(capsys):
    # Without the guard, RecursionError escapes output_json and the command
    # emits NOTHING; every pm command shares that boundary.
    output_json({"note": _deep_leaf(5000, "hunter2")})
    out = capsys.readouterr().out
    assert out.strip(), "the boundary must always produce output"
    assert "hunter2" not in out and REDACTED in out
    json.loads(out)  # the envelope itself is intact


def test_v2_depth_200_leaf_still_walks(capsys):
    output_json({"note": _deep_leaf(200, "hunter2")})
    out = capsys.readouterr().out
    assert "hunter2" not in out
    inner = json.loads(json.loads(out)["note"])
    for _ in range(200):
        inner = inner[0]
    assert inner["client_secret"] == REDACTED  # walked (re-serialized, key-redacted), not text-scrubbed


def _depth_where_parse_succeeds_but_walk_recurses():
    """Find a depth at the default recursion limit where json.loads succeeds
    but the key walk raises RecursionError (the walk uses more frames per
    level than the C parser). Returns None if no such depth exists here."""
    import sys
    from cli_anything.propertymeld import utils as u
    lo, hi, found = 50, 3000, None
    for depth in range(lo, hi, 25):
        leaf = _deep_leaf(depth, "x")
        try:
            parsed = json.loads(leaf)
        except RecursionError:
            break
        try:
            u.redact_sensitive(parsed, string_scrub=u.scrub_sensitive_text_narrow)
            json.dumps(parsed)
        except RecursionError:
            found = depth
            break
    return found


def test_v2_mid_depth_leaf_parse_succeeds_walk_recurses_still_produces_output(capsys):
    # The catch must sit around the WALK and DUMP, not only the parse: at this
    # depth json.loads succeeds and the recursion happens after it.
    depth = _depth_where_parse_succeeds_but_walk_recurses()
    if depth is None:
        pytest.skip("no depth on this interpreter where the parse succeeds but the walk recurses")
    from cli_anything.propertymeld import utils as u
    reached = []
    real = u._parse_json_leaf
    def spy(text):
        r = real(text)
        reached.append(r is not None)
        return r
    with patch.object(u, "_parse_json_leaf", side_effect=spy):
        output_json({"note": _deep_leaf(depth, "hunter2")})
    out = capsys.readouterr().out
    assert any(reached), "the leaf must have parsed and reached the walk"
    assert out.strip() and "hunter2" not in out and REDACTED in out
    json.loads(out)


# ── W1: the walk round-trips the string-encoding layer count ────────────────

SINGLE = json.dumps({"client_secret": "hunter2", "x": 1})
DOUBLE = json.dumps(SINGLE)
TRIPLE = json.dumps(DOUBLE)


def _layers(text):
    n, v = 0, text
    while isinstance(v, str):
        try:
            v = json.loads(v); n += 1
        except (TypeError, ValueError):
            break
    return n, v


@pytest.mark.parametrize("leaf,expected_layers", [(SINGLE, 1), (DOUBLE, 2)])
def test_w1_single_and_double_encoded_leaves_keep_their_layer_count(capsys, leaf, expected_layers):
    output_json({"note": leaf})
    note = json.loads(capsys.readouterr().out)["note"]
    layers, inner = _layers(note)
    assert layers == expected_layers, (layers, note)
    assert inner["client_secret"] == REDACTED and inner["x"] == 1
    assert "hunter2" not in note


def test_w1_no_sensitive_data_double_encoded_leaf_round_trips_unchanged_layer_count(capsys):
    leaf = json.dumps(json.dumps({"x": 1, "y": [1, 2]}))
    output_json({"note": leaf})
    note = json.loads(capsys.readouterr().out)["note"]
    layers, inner = _layers(note)
    assert layers == 2 and inner == {"x": 1, "y": [1, 2]}


def test_w1_triple_encoded_leaf_is_beyond_accepted_depth_and_text_scrubbed(capsys):
    # Declared limit: only two layers are peeled. A triple-encoded leaf is
    # not walked; the text scrub still catches the quoted pair inside it.
    output_json({"note": TRIPLE})
    note = json.loads(capsys.readouterr().out)["note"]
    assert "hunter2" not in note and REDACTED in note
    assert note == scrub_sensitive_text_narrow(TRIPLE)  # text path, not the walk


# ── W2: an INDENTED duplicate definition is matched and removed ─────────────

def test_w2_indented_duplicate_is_removed_and_first_definition_replaced(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PM_CLIENT_SECRET=old1\nOTHER=keep\n  PM_CLIENT_SECRET=old2\n\texport PM_CLIENT_SECRET=old3\n")
    update_env_file(str(env_path), {"PM_CLIENT_SECRET": SENTINEL})
    lines = env_path.read_text().splitlines()
    defs = [l for l in lines if l.strip().replace("export ", "").startswith("PM_CLIENT_SECRET=")]
    assert defs == [f"PM_CLIENT_SECRET={SENTINEL}"]          # exactly one, the new one, column zero
    assert "old1" not in env_path.read_text() and "old2" not in env_path.read_text() and "old3" not in env_path.read_text()
    assert "OTHER=keep" in lines


def test_w2_indented_first_definition_keeps_its_indentation_and_prefix(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("  export PM_CLIENT_SECRET=old1\nPM_CLIENT_SECRET=old2\n")
    update_env_file(str(env_path), {"PM_CLIENT_SECRET": SENTINEL})
    assert env_path.read_text().splitlines() == [f"  export PM_CLIENT_SECRET={SENTINEL}"]


def test_w2_indented_line_for_a_different_key_is_untouched(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PM_CLIENT_SECRET=old1\n  OTHER_INDENTED=keep2\n")
    update_env_file(str(env_path), {"PM_CLIENT_SECRET": SENTINEL})
    assert env_path.read_text().splitlines() == [f"PM_CLIENT_SECRET={SENTINEL}", "  OTHER_INDENTED=keep2"]


# ── Aussie P1: the pm work-orders complete refusal path scrubs BOTH streams ──

def test_p1_complete_refusal_credential_notes_absent_from_both_streams(runner):
    # The REAL path: pm work-orders complete -> http_backend.complete_meld ->
    # _complete_meld_fail -> emit_error. A credential-shaped --notes value must
    # not reach stdout OR stderr, and a redaction marker must be present.
    notes = f"Bearer {CANARY} client_secret={CANARY}"
    result = runner.invoke(cli, ["work-orders", "complete", "--meld-id", "1", "--notes", notes])
    assert result.exit_code == 1, result.output
    combined = result.stdout + (result.stderr or "")
    assert CANARY not in combined
    assert REDACTED in (result.stderr or "")
    # The refusal still says what it refused and why.
    err = json.loads((result.stderr or "").strip().splitlines()[-1])
    assert err["ok"] is False and str(err["meld_id"]) == "1"
    assert "completion_notes" in err and CANARY not in json.dumps(err)


def test_p1_emit_error_scrubs_dict_and_string_payloads():
    import io, contextlib
    from cli_anything.propertymeld.utils import emit_error
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        emit_error({"note": f"password={CANARY}", "meld_id": "1"})
    out = buf.getvalue()
    assert CANARY not in out and REDACTED in out and json.loads(out.strip())["meld_id"] == "1"
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        emit_error(f"failed with token {CANARY}")
    assert CANARY not in buf.getvalue() and REDACTED in buf.getvalue()


def test_p1_print_error_now_scrubs_its_message(capsys):
    from cli_anything.propertymeld.utils import print_error
    print_error(f"denied: api_key={CANARY}")
    err = capsys.readouterr().err
    assert CANARY not in err and REDACTED in err


def test_s1_literal_backslash_escaped_pairs_in_free_text_redacted():
    # The text-level backstop: escaped quote delimiters around key and value.
    out = scrub_sensitive_text(LITERAL_ESCAPED, high_entropy=False)
    assert "hunter2" not in out and REDACTED in out and out.startswith("msg=") and out.endswith("end")


@pytest.mark.parametrize("text", ["C:\\Users\\dave\\file.txt", "line1\\nline2", "regex \\d+ here", "{not json", "[1, 2"])
def test_s1_ordinary_strings_with_backslashes_or_braces_survive(capsys, text):
    output_json({"note": text})
    assert json.loads(capsys.readouterr().out)["note"] == text


# ── Codex S2: free-text scanning is linear, a CLI-stalling input cannot stall ─

def test_s2_64kb_letter_only_leaf_scrubs_fast_and_unchanged():
    big = "a" * (64 * 1024)
    t0 = time.perf_counter()
    out = scrub_sensitive_text(big)
    elapsed = time.perf_counter() - t0
    assert out == big
    # Generous bound so CI noise cannot flake it; the quadratic version took
    # 14 seconds on this input and minutes on larger ones.
    assert elapsed < 1.0, f"took {elapsed:.3f}s"


def test_s2_64kb_leaf_with_one_credential_still_redacts_it_fast():
    big = "a" * (30 * 1000) + " password=hunter2 " + "b" * (30 * 1000)
    t0 = time.perf_counter()
    out = scrub_sensitive_text(big)
    elapsed = time.perf_counter() - t0
    assert "hunter2" not in out and REDACTED in out
    assert out.startswith("a" * 100) and out.endswith("b" * 100)
    assert elapsed < 1.0, f"took {elapsed:.3f}s"


def test_s2_64kb_leaf_through_output_json_is_fast(capsys):
    t0 = time.perf_counter()
    output_json({"blob": "a" * (64 * 1024), "note": "password=hunter2"})
    elapsed = time.perf_counter() - t0
    out = capsys.readouterr().out
    assert "hunter2" not in out and elapsed < 1.0, f"took {elapsed:.3f}s"


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
