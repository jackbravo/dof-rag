#!/bin/bash
# Install the DOF daily updater as a systemd --user timer.
# Renders @DOF_REPO_DIR@ in the checked-in unit template with the actual
# checkout path, so clones may live anywhere (including paths with spaces).
set -eu

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(dirname "$script_dir")"
source_service="$repo_dir/ops/systemd/dof-rag-daily.service"
source_timer="$repo_dir/ops/systemd/dof-rag-daily.timer"
target_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
target_service="$target_dir/dof-rag-daily.service"
target_timer="$target_dir/dof-rag-daily.timer"

mkdir -p "$target_dir" "$repo_dir/logs"

# Escape sed's replacement metacharacters so spaces and & are preserved.
escaped_repo_dir=$(printf '%s' "$repo_dir" | sed 's/[&|\]/\\&/g')

temporary_service="$target_service.new"
sed "s|@DOF_REPO_DIR@|$escaped_repo_dir|g" "$source_service" > "$temporary_service"
install -m 0644 "$source_timer" "$target_timer"
mv "$temporary_service" "$target_service"

systemctl --user daemon-reload
systemctl --user enable --now dof-rag-daily.timer
systemctl --user list-timers dof-rag-daily.timer --no-pager

if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
    echo "note: run 'loginctl enable-linger $USER' so the timer fires without an open session"
fi
