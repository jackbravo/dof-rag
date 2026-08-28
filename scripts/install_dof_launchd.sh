#!/bin/zsh
set -eu

script_dir=${0:A:h}
repo_dir=${script_dir:h}
source_plist="$repo_dir/ops/launchd/com.jackbravo.dof-rag-daily.plist"
target_dir="$HOME/Library/LaunchAgents"
target_plist="$target_dir/com.jackbravo.dof-rag-daily.plist"
support_dir="$HOME/Library/Application Support/DOF-RAG"
source_runner="$repo_dir/scripts/run_dof_daily.sh"
target_runner="$support_dir/run_dof_daily.sh"
domain="gui/$(id -u)"
label="com.jackbravo.dof-rag-daily"

mkdir -p "$target_dir" "$support_dir" "$repo_dir/logs"
plutil -lint "$source_plist"

if launchctl print "$domain/$label" >/dev/null 2>&1; then
    launchctl bootout "$domain/$label"
fi

# The checked-in plist and runner are templates: bake this account's paths in
# at install time so the job works from any checkout, not just one developer's.
sed -e "s|@DOF_RUNNER@|$target_runner|g" \
    -e "s|@DOF_LOG_DIR@|$repo_dir/logs|g" \
    "$source_plist" > "$target_plist"
chmod 0644 "$target_plist"

sed "s|^repo_dir=.*|repo_dir="$repo_dir"|" "$source_runner" > "$target_runner"
chmod 0755 "$target_runner"

launchctl bootstrap "$domain" "$target_plist"
launchctl enable "$domain/$label"
launchctl print "$domain/$label"
