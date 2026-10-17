#!/usr/bin/env bash
# Create this clone's local .venv and install pinned dependencies.
# Usage (from repo root):
#   bash scripts/bootstrap.sh
# Then run exports with:
#   .venv/Scripts/python.exe -m weread_exporter -b <book-id> ...   # Windows (Git Bash)
#   .venv/bin/python -m weread_exporter -b <book-id> ...            # Linux / macOS
set -euo pipefail

# Resolve the repository root (parent of scripts/).
repo_root() {
  cd "$(dirname "$0")/.." || exit 1
  # Git Bash on Windows exposes pwd -W; fall back to pwd elsewhere.
  if pwd -W >/dev/null 2>&1; then
    pwd -W
  else
    pwd
  fi
}

# Pick the venv python path for the current platform.
venv_python() {
  local root="$1"
  if [ -x "$root/.venv/Scripts/python.exe" ]; then
    echo "$root/.venv/Scripts/python.exe"
  else
    echo "$root/.venv/bin/python"
  fi
}

# Create .venv if missing and install requirements.txt into it.
initialize_project_venv() {
  local root="$1"
  cd "$root"
  local py
  py="$(venv_python "$root")"

  if [ ! -x "$py" ]; then
    echo "Creating .venv under $root"
    python -m venv .venv
    py="$(venv_python "$root")"
  else
    echo "Reusing existing .venv at $root/.venv"
  fi

  if [ ! -x "$py" ]; then
    echo "FATAL: failed to create $py" >&2
    exit 1
  fi

  echo "Installing requirements.txt into .venv"
  "$py" -m pip install --upgrade pip
  "$py" -m pip install -r "$root/requirements.txt"

  echo
  echo "Done. Run exports with this clone's interpreter only:"
  echo "  $py -m weread_exporter -b <book-id> ..."
}

ROOT="$(repo_root)"
initialize_project_venv "$ROOT"
