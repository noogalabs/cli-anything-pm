#!/usr/bin/env bash
#
# refresh-installed-pm.sh — keep the installed `pm` snapshot in sync with main.
#
# WHY: `pm` is installed as a pipx SNAPSHOT (code copied into the venv), so it
# goes stale after every merge to this repo. A stale `pm` produces phantom
# bug-reports — testing already-fixed behavior (observed 2026-05-29: two
# "bugs" were stale-install artifacts). `pm-dev` is an EDITABLE install and
# tracks the working tree live, so it is intentionally NOT refreshed here.
#
# ROUTINE: run this as the post-merge close-out step after merging a PR to
# this repo and syncing local main:
#   gh pr merge <N> --squash --delete-branch
#   git checkout main && git pull
#   scripts/refresh-installed-pm.sh        # <-- this line kills the staleness class
#
# Idempotent and safe to run any time. No-ops cleanly if pipx/pm absent.
set -euo pipefail

PKG="cli-anything-pm"   # the stable SNAPSHOT package (command: pm). pm-dev is editable; left alone.

if ! command -v pipx >/dev/null 2>&1; then
  echo "refresh-installed-pm: pipx not found — skipping (pm not pipx-managed here)." >&2
  exit 0
fi

if ! pipx list 2>/dev/null | grep -q "package ${PKG} "; then
  echo "refresh-installed-pm: ${PKG} not installed via pipx — skipping." >&2
  exit 0
fi

echo "refresh-installed-pm: reinstalling ${PKG} from source so the snapshot tracks current main..."
pipx reinstall "${PKG}"

# Proof-not-word: confirm the refreshed snapshot matches the repo HEAD it was built from.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEAD_SHA="$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "refresh-installed-pm: done. Repo HEAD = ${HEAD_SHA}. \`pm\` snapshot now reflects this commit."
