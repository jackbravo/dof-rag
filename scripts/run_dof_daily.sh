#!/bin/zsh
set -eu

repo_dir="/Users/jackbravo/Documents/jackbravo/dof-rag"

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:/Applications/LibreOffice.app/Contents/MacOS:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONUNBUFFERED=1
export UV_CACHE_DIR="${TMPDIR:-/tmp}/dof-rag-uv-cache"

cd "$repo_dir"
exec uv run python -m scripts.update_dof_daily "$@"
