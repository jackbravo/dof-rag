#!/bin/zsh
set -eu

# Direct use self-locates from scripts/. The installed launchd plist sets the
# exact checkout path, allowing the copied runner to live in Application Support.
repo_dir="${DOF_REPO_DIR:-$(cd -- "${0:A:h}/.." && pwd)}"

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:/Applications/LibreOffice.app/Contents/MacOS:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONUNBUFFERED=1
export UV_CACHE_DIR="${TMPDIR:-/tmp}/dof-rag-uv-cache"

cd "$repo_dir"
exec uv run python -m scripts.update_dof_daily "$@"
