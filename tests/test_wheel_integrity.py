"""Wheel contents and installed public-CLI regression tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import venv
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(command, *, cwd, env=None):
    return subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_generated_build_trees_are_ignored_and_untracked():
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("repository metadata is unavailable")

    tracked = _run(
        ["git", "ls-files", "--", "build", "dist"],
        cwd=REPO_ROOT,
    )
    assert tracked.stdout == ""

    ignored = _run(
        [
            "git",
            "check-ignore",
            "build/lib/cli_anything/propertymeld/cli.py",
            "dist/cli_anything_pm.whl",
        ],
        cwd=REPO_ROOT,
    )
    assert ignored.stdout.splitlines() == [
        "build/lib/cli_anything/propertymeld/cli.py",
        "dist/cli_anything_pm.whl",
    ]


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory):
    """Build with a deliberately newer stale staging module present."""
    workspace = tmp_path_factory.mktemp("wheel-integrity")
    source_tree = workspace / "source"
    shutil.copytree(
        REPO_ROOT,
        source_tree,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "*.egg-info",
            "dist",
        ),
    )

    stale_cli = source_tree / "build/lib/cli_anything/propertymeld/cli.py"
    stale_cli.parent.mkdir(parents=True, exist_ok=True)
    stale_cli.write_text(
        "import click\n\n@click.group()\ndef cli():\n    pass\n",
        encoding="utf-8",
    )
    future = time.time() + 3600
    os.utime(stale_cli, (future, future))

    dist_dir = workspace / "dist"
    _run(
        [sys.executable, "setup.py", "bdist_wheel", "--dist-dir", dist_dir],
        cwd=source_tree,
    )
    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1
    return source_tree, wheels[0], workspace


def test_wheel_python_modules_match_source_bytes(built_wheel):
    source_tree, wheel_path, _ = built_wheel
    expected = {
        path.relative_to(source_tree).as_posix(): path.read_bytes()
        for path in (source_tree / "cli_anything").rglob("*.py")
    }

    with zipfile.ZipFile(wheel_path) as archive:
        actual = {
            name: archive.read(name)
            for name in archive.namelist()
            if name.startswith("cli_anything/") and name.endswith(".py")
        }

    assert actual.keys() == expected.keys()
    assert actual == expected


def test_installed_wheel_exposes_complete_insights_cli(built_wheel):
    _, wheel_path, workspace = built_wheel
    venv_dir = workspace / "installed-wheel"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    pm = venv_dir / ("Scripts/pm.exe" if os.name == "nt" else "bin/pm")
    _run([python, "-m", "pip", "install", wheel_path], cwd=workspace)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    commands = {
        ("insights", "--help"): ("melds", "turnovers", "benchmarks"),
        ("insights", "melds", "--help"): ("--project", "--non-project"),
        ("insights", "turnovers", "--help"): ("--project", "--non-project"),
        ("insights", "benchmarks", "--help"): ("--project", "--non-project"),
    }
    for arguments, expected_members in commands.items():
        result = _run([pm, *arguments], cwd=workspace, env=env)
        for member in expected_members:
            assert member in result.stdout

    location = _run(
        [
            python,
            "-c",
            "import cli_anything.propertymeld.cli as m; print(m.__file__)",
        ],
        cwd=workspace,
        env=env,
    )
    assert str(venv_dir) in location.stdout
    assert str(REPO_ROOT) not in location.stdout
