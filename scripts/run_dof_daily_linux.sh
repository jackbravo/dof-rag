#!/bin/bash
# Linux counterpart to run_dof_daily.sh (launchd/zsh on macOS).
# Self-locates the repo from scripts/. DOF_REPO_DIR overrides.
set -eu

repo_dir="${DOF_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONUNBUFFERED=1

cd "$repo_dir"

# Prefer the repo's pre-built venv (slim install); fall back to uv run, which
# materializes the full locked environment on first use.
if [[ -x "$repo_dir/.venv/bin/python" ]]; then
    exec "$repo_dir/.venv/bin/python" -m scripts.update_dof_daily "$@"
fi
exec uv run python -m scripts.update_dof_daily "$@"
