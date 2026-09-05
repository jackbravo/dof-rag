# Daily DOF updates

In production the corpus is updated automatically once per day: on macOS by
the `launchd` job `com.jackbravo.dof-rag-daily`, on Linux by the systemd user
timer `dof-rag-daily.timer`. Both run at 05:30 local time and catch up on
missed runs (launchd refires after the Mac wakes; the timer uses
`Persistent=true`). `launchd`/`systemd` are used instead of Unix cron because
they are the native schedulers.

## What each run does

`scripts/update_dof_daily.py` runs the existing resumable pipeline in order:

1. Download both MAT and VES Word editions into the ignored `dof_word/` tree.
2. Convert new `.doc` files into the canonical `../dof_md` Markdown tree.
3. Append new documents to `dof_db/dof_corpus_l3.sqlite`.
4. Add the documents to FTS5 and build their chunk recipes.
5. Generate Jina binary embeddings and append them to the sqlite-vec index.

Downloads are content-checked: an HTML error page returned under a `.doc`
filename is rejected and retried on the next run. The converter applies the
same check and quarantines such files to `<name>.doc.invalid`, which is
invisible to both the `*.doc` conversion scan and the downloader's resume
glob, so a stale error page can never block the watermark forever.
Conversion is restricted to the active date window, so an unrelated failed
file elsewhere in the same year cannot block today's watermark. A failed stage
stops the run and leaves the contiguous completion watermark unchanged; files
already downloaded or converted remain available for the next run. When a DOF
listing page has no Word links, SIDOF notices for that date are still checked.
Empty HTTP 200 responses count as successful only when the expected dated
DOF/SIDOF page structure or DOF's explicit empty-date message is present.

The updater uses a non-blocking lock, so a scheduled run exits harmlessly if a
catch-up is still running. It keeps raw Word files for auditability and because
the download and conversion stages are resumable.

## Date-window behavior

Without arguments, the updater reads a contiguous completion watermark from
`var/dof_update_state.json`. On its first run it migrates from the last date in
the full-build manifest. It starts on the day after the watermark when the
database is more than seven days behind; otherwise it starts at the beginning
of the seven-day overlap. This means an outage longer than seven days still
catches up automatically, a recent-date test cannot hide an older gap, and
late posts with new note IDs are discovered. Existing files are intentionally
kept for auditability, so same-path corrections require explicit replacement
detection and are not refreshed by the overlap alone. Historical test windows
never move an existing completion watermark backward.

Preview the window without changing anything:

```bash
scripts/run_dof_daily.sh --dry-run
```

Run a specific test window:

```bash
scripts/run_dof_daily.sh --start-date 2026-08-24 --end-date 2026-08-27
```

Start or resume the full catch-up explicitly:

```bash
scripts/run_dof_daily.sh --start-date 2026-04-25
```

The default command already selects that catch-up start while the database is
at 2026-04-24, so an explicit date is usually unnecessary.

## launchd operations

Install or reload the job:

```bash
scripts/install_dof_launchd.sh
```

Inspect it:

```bash
launchctl print gui/$(id -u)/com.jackbravo.dof-rag-daily
```

Follow logs:

```bash
tail -f logs/dof-daily.log logs/dof-daily.error.log
```

Trigger an extra run (the overlap lock still applies):

```bash
launchctl kickstart gui/$(id -u)/com.jackbravo.dof-rag-daily
```

Unload it without deleting the plist:

```bash
launchctl bootout gui/$(id -u)/com.jackbravo.dof-rag-daily
```

The checked-in plist is at
`ops/launchd/com.jackbravo.dof-rag-daily.plist`; the installed copy is
`~/Library/LaunchAgents/com.jackbravo.dof-rag-daily.plist`. The installer
writes paths with `plutil` and passes the checkout through `DOF_REPO_DIR` to
the tiny launcher under `~/Library/Application Support/DOF-RAG/`, so spaces
and XML-special characters in account or clone paths are preserved. Re-run
the installer after moving the repository.

The chunk store accepts only the current `CHUNKER_VERSION`. A version change
must rebuild `dof_chunks.sqlite`, `dof_vectors_jina_binary.sqlite`, and
`dof_vec0_jina_binary.sqlite` together; the daily updater refuses to mix old
and new chunk recipes in live retrieval indexes.

The daily pipeline is intentionally append-only. Replacing an already ingested
path or repairing historical corpus rows is an offline migration and must
rebuild or explicitly reconcile the affected derived indexes.

## systemd operations (Linux)

Install or reload the timer (renders the checkout path into the unit template,
then enables and starts the timer):

```bash
scripts/install_dof_systemd.sh
```

The checked-in template units are `ops/systemd/dof-rag-daily.service` and
`ops/systemd/dof-rag-daily.timer`; the installed rendered copies live in
`~/.config/systemd/user/`. Re-run the installer after moving the repository.
The runner `scripts/run_dof_daily_linux.sh` accepts the same arguments as the
macOS runner, e.g. `scripts/run_dof_daily_linux.sh --dry-run`.

Inspect and operate:

```bash
systemctl --user list-timers dof-rag-daily.timer
journalctl --user -u dof-rag-daily.service -e
tail -f logs/dof-daily.log logs/dof-daily.error.log
systemctl --user start dof-rag-daily.service   # extra run; the overlap lock applies
systemctl --user disable --now dof-rag-daily.timer
```

Enable lingering so the timer fires without an open session:

```bash
loginctl enable-linger $USER
```
