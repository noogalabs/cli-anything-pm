"""Behavioural casualties for the ci.yml duplicate-run gate.

These EXECUTE the shell that ships in .github/workflows/ci.yml rather than
asserting on its text, so a reworded comment cannot pass them and a changed
decision cannot hide behind one. The script is extracted by plain text parsing
because PyYAML is not installed in CI (`pip install pytest setuptools wheel`),
and importing it here would fail on a cold runner rather than on this machine.

Skipping the push build is only safe when a pull_request run actually covers the
commit, and `on:` fires pull_request for base=main ONLY. Hence the two boundary
casualties below, both found in seat review (aussie, 2026-09-05).
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _gate_script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("      - id: decide")
    body = text[text.index("run: |", start) + len("run: |") :]
    lines = []
    for line in body.split("\n")[1:]:
        if line.strip() and not line.startswith(" " * 10):
            break
        lines.append(line[10:] if line.startswith(" " * 10) else line)
    script = "\n".join(lines)
    assert "GITHUB_OUTPUT" in script, "gate script not extracted"
    return script


def _decide(tmp_path, *, event: str, ref: str, main_base_prs: str, all_prs: str | None = None) -> str:
    """Run the shipped gate script with `gh` stubbed, return the run= value."""
    script = _gate_script()
    subs = {
        "github.event_name": event,
        "github.ref_name": ref,
        "github.repository": "noogalabs/cli-anything-pm",
        "github.repository_owner": "noogalabs",
        "github.token": "stub",
    }
    for key, value in subs.items():
        script = re.sub(r"\$\{\{\s*" + re.escape(key) + r"\s*\}\}", value, script)
    assert "${{" not in script, f"unsubstituted expression remains: {script}"

    # Unique per call: one test may decide more than once.
    bindir = tmp_path / f"bin-{event}-{ref.replace('/', '_')}-{main_base_prs}-{all_prs}"
    bindir.mkdir(parents=True, exist_ok=True)
    # The stub answers as the real API would: the base=main query returns only
    # PRs targeting main, an unfiltered query returns every open PR on the head.
    # So a gate that drops the filter reads `all_prs` instead of `main_base_prs`
    # and the non-main-base casualty turns red. That is what makes it a casualty
    # rather than a restatement of the no-open-PR case.
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"base=main"* ]]; then echo %s; else echo %s; fi\n'
        % (main_base_prs, all_prs if all_prs is not None else main_base_prs),
        encoding="utf-8",
    )
    gh.chmod(0o755)

    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    runner = tmp_path / "gate.sh"
    runner.write_text(script, encoding="utf-8")
    subprocess.run(
        ["bash", str(runner)],
        check=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "GITHUB_OUTPUT": str(out)},
        capture_output=True,
    )
    values = dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    return values["run"]


def test_covered_branch_push_skips(tmp_path):
    """Positive control: without this the two casualties below pass vacuously."""
    assert _decide(tmp_path, event="push", ref="ci/some-branch", main_base_prs="1") == "false"


def test_push_with_no_open_pr_runs(tmp_path):
    assert _decide(tmp_path, event="push", ref="ci/some-branch", main_base_prs="0") == "true"


def test_non_main_base_pr_does_not_skip(tmp_path):
    """A PR to a non-main base produces NO covering pull_request run.

    `on: pull_request: branches: [main]`. If the gate counted that PR it would
    skip the push build and the commit would get no CI at all. The stub reports
    zero main-based PRs while a non-main-base PR exists, so a gate that queries
    without base=main sees 1 and wrongly skips.
    """
    assert _decide(
        tmp_path, event="push", ref="ci/some-branch", main_base_prs="0", all_prs="1"
    ) == "true"


def test_main_push_never_skips(tmp_path):
    """A same-repo PR whose HEAD is main must not gate a push to main."""
    assert _decide(tmp_path, event="push", ref="main", main_base_prs="3") == "true"


def test_non_push_events_never_gated(tmp_path):
    for event in ("pull_request", "workflow_dispatch"):
        assert _decide(tmp_path, event=event, ref="ci/some-branch", main_base_prs="5") == "true"


def test_query_filters_base_main():
    """The base=main filter is load-bearing, not cosmetic: without it the gate
    counts PRs whose pull_request run never fires."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "base=main" in text, "gate query lost its base=main filter"


def test_main_is_explicitly_exempt():
    assert 'github.ref_name }}" = "main"' in WORKFLOW.read_text(encoding="utf-8")
