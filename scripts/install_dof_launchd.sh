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

install -m 0644 "$source_plist" "$target_plist"
install -m 0755 "$source_runner" "$target_runner"
launchctl bootstrap "$domain" "$target_plist"
launchctl enable "$domain/$label"
launchctl print "$domain/$label"
