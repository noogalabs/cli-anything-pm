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
    def __init__(self, rc, stderr=""):
        self.returncode = rc
        self.stderr = stderr


def test_c1_railway_delivery_status_survives_redaction_ok(runner):
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)), \
         patch("subprocess.run", return_value=_Proc(0)):
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-railway"])
    assert result.exit_code == 0, result.output
    assert SENTINEL not in result.output
    data = json.loads(result.output)
    by_var = {d["var"]: d for d in data["deliveries"] if d["path"] == "railway"}
    # The status for PM_CLIENT_SECRET is READABLE: it sits under non-matching keys.
    assert by_var["PM_CLIENT_SECRET"]["status"] == "ok"
    assert by_var["PM_CLIENT_ID"]["status"] == "ok"
    assert "railway_updates" not in data
    assert REDACTED not in json.dumps(data["deliveries"])


def test_c1_railway_delivery_failure_readable_and_exits_1(runner):
    def fake_run(cmd, **kw):
        return _Proc(1, f"set failed for {cmd[3].split('=')[0]}") if "PM_CLIENT_SECRET" in cmd[3] else _Proc(0)
    with patch.object(http_backend, "rotate_api_key", return_value=dict(ROTATE_RESULT)), \
         patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(cli, ["api-keys", "rotate", "--update-railway"])
    assert result.exit_code == 1, result.output
    assert SENTINEL not in result.output
    data = json.loads(result.output)
    assert data["ok"] is False
    by_var = {d["var"]: d for d in data["deliveries"] if d["path"] == "railway"}
    assert by_var["PM_CLIENT_SECRET"]["status"] == "error"
    assert "set failed" in by_var["PM_CLIENT_SECRET"]["detail"]
    assert by_var["PM_CLIENT_ID"]["status"] == "ok"


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
    by = {(d["path"], d.get("var")): d for d in data["deliveries"]}
    assert by[("railway", "PM_CLIENT_ID")]["status"] == "error"
    assert by[("railway", "PM_CLIENT_SECRET")]["status"] == "error"
    assert "could not be run" in by[("railway", "PM_CLIENT_SECRET")]["detail"]
    env = [d for d in data["deliveries"] if d["path"] == "env"][0]
    assert env["status"] == "ok"
    # The fallback destination actually received the secret.
    assert f"PM_CLIENT_SECRET={SENTINEL}\n" in env_path.read_text()


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
