#!/bin/bash
# Install the DOF human-evaluation services (scheduler + web) as systemd
# --user units. Renders @DOF_REPO_DIR@ in the checked-in unit templates
# with the actual checkout path, so clones may live anywhere.
set -eu

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(dirname "$script_dir")"
target_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$target_dir" "$repo_dir/logs"

# Escape sed's replacement metacharacters so spaces and & are preserved.
escaped_repo_dir=$(printf '%s' "$repo_dir" | sed 's/[&|\]/\\&/g')

for unit in dof-human-eval-scheduler.service dof-human-eval-web.service; do
    temporary="$target_dir/$unit.new"
    sed "s|@DOF_REPO_DIR@|$escaped_repo_dir|g" "$repo_dir/ops/systemd/$unit" \
        > "$temporary"
    mv "$temporary" "$target_dir/$unit"
done

systemctl --user daemon-reload
# The scheduler migrates the database before the web workers validate it.
systemctl --user enable --now dof-human-eval-scheduler.service
systemctl --user enable --now dof-human-eval-web.service
systemctl --user status dof-human-eval-scheduler.service dof-human-eval-web.service --no-pager || true

if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
    echo "note: run 'loginctl enable-linger $USER' so the services survive logout"
fi
