"""Regression guard: the Playwright recapture script must ship inside the package.

Bug (fixed): _RECAPTURE_SCRIPT was resolved 3 dirs up from http_backend.py into a
repo-root scripts/ directory that setup.py never shipped (find_packages() only, no
scripts=/package_data/data_files). On a packaged (pipx/pip non-editable) install
that path resolved into site-packages/scripts/ which does not exist, so auto-recapture
was dead and every cookie-session expiry hard-exited instead of transparently
re-authing.

The fix moves the script under the package (cli_anything/propertymeld/recapture/) and
resolves it relative to THIS module. These assertions guard both halves: the path is
inside the package, and the file actually exists on disk where it was resolved.
"""
import os

from cli_anything.propertymeld import http_backend as hb


def test_recapture_script_resolves_inside_package():
    # Path must point inside the shipped package subdir, never a repo-root scripts/.
    norm = hb._RECAPTURE_SCRIPT.replace(os.sep, "/")
    assert "cli_anything/propertymeld/recapture" in norm, norm
    assert norm.endswith("pm-recapture-session-playwright.py"), norm
    # Never the old, never-shipped location.
    assert "/scripts/pm-recapture-session-playwright.py" not in norm, norm


def test_recapture_script_file_exists():
    # Resolves true in both editable (in-repo under package) and packaged
    # (shipped under site-packages) installs. Was False on packaged installs
    # before the fix.
    assert os.path.exists(hb._RECAPTURE_SCRIPT), hb._RECAPTURE_SCRIPT
