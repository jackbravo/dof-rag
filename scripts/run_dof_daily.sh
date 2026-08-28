#!/bin/zsh
set -eu

# Self-locate the repository when run from a checkout (e.g. --dry-run tests).
# install_dof_launchd.sh replaces this line with the absolute repository path
# baked into the copy installed under ~/Library/Application Support/DOF-RAG.
repo_dir=$(cd -- "${0:A:h}/.." && pwd)

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:/Applications/LibreOffice.app/Contents/MacOS:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONUNBUFFERED=1
export UV_CACHE_DIR="${TMPDIR:-/tmp}/dof-rag-uv-cache"

cd "$repo_dir"
exec uv run python -m scripts.update_dof_daily "$@"
