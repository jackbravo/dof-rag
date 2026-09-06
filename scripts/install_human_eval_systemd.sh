#!/bin/bash
# Install the DOF human-evaluation services (scheduler + web) as systemd
# --user units. Renders @DOF_REPO_DIR@ in the checked-in unit templates
# with the actual checkout path.
set -eu

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(dirname "$script_dir")"
target_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

case "$repo_dir" in
    *[[:space:]]*)
        echo "error: the checkout path must not contain whitespace: $repo_dir" >&2
        exit 1
        ;;
esac

mkdir -p "$target_dir" "$repo_dir/logs"

# Escape sed's replacement metacharacters.
escaped_repo_dir=$(printf '%s' "$repo_dir" | sed 's/[&|\]/\\&/g')

for unit in dof-human-eval-scheduler.service dof-human-eval-web.service; do
    temporary="$target_dir/$unit.new"
    sed "s|@DOF_REPO_DIR@|$escaped_repo_dir|g" "$repo_dir/ops/systemd/$unit" \
        > "$temporary"
    mv "$temporary" "$target_dir/$unit"
done

# Installation starts inactive services but deliberately does not interrupt
# active runs. After upgrading code/units, operators must stop web admission
# and restart both processes explicitly (daemon-reload alone is insufficient).
systemctl --user daemon-reload
# Launch the scheduler first; web workers perform a bounded schema-readiness wait.
systemctl --user enable --now dof-human-eval-scheduler.service
systemctl --user enable --now dof-human-eval-web.service
systemctl --user status dof-human-eval-scheduler.service dof-human-eval-web.service --no-pager || true

echo "For upgrades, apply the new code/units with:"
echo "  systemctl --user stop dof-human-eval-web.service"
echo "  systemctl --user restart dof-human-eval-scheduler.service"
echo "  systemctl --user start dof-human-eval-web.service"

if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
    echo "note: run 'loginctl enable-linger $USER' so the services survive logout"
fi
