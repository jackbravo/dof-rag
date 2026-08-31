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

plutil -lint "$source_plist"

render_plist() {
    local destination=$1
    local runner=$2
    local repository=$3

    # Let plutil write path values. Unlike sed string replacement, this
    # preserves spaces and XML-special characters in paths.
    install -m 0644 "$source_plist" "$destination"
    plutil -replace ProgramArguments.5 -string "$runner" "$destination"
    # On macOS, replacing an array index with plutil appends the value while
    # retaining the template element. Remove the now-stale placeholder so the
    # runner is not invoked with @DOF_RUNNER@ as an extra argument.
    plutil -remove ProgramArguments.6 "$destination"
    plutil -replace EnvironmentVariables.DOF_REPO_DIR \
        -string "$repository" "$destination"
    plutil -replace StandardOutPath \
        -string "$repository/logs/dof-daily.log" "$destination"
    plutil -replace StandardErrorPath \
        -string "$repository/logs/dof-daily.error.log" "$destination"
    plutil -lint "$destination"
}

if [[ ${1:-} == "--render-plist" ]]; then
    if (( $# != 4 )); then
        print -u2 "usage: $0 --render-plist OUTPUT RUNNER REPOSITORY"
        exit 2
    fi
    render_plist "$2" "$3" "$4"
    exit 0
fi

mkdir -p "$target_dir" "$support_dir" "$repo_dir/logs"
temporary_plist="$target_plist.new"
render_plist "$temporary_plist" "$target_runner" "$repo_dir"
install -m 0755 "$source_runner" "$target_runner"

if launchctl print "$domain/$label" >/dev/null 2>&1; then
    launchctl bootout "$domain/$label"
fi

mv "$temporary_plist" "$target_plist"

launchctl bootstrap "$domain" "$target_plist"
launchctl enable "$domain/$label"
launchctl print "$domain/$label"
