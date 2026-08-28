# Daily DOF updates on macOS

The production corpus is updated by the macOS `launchd` job
`com.jackbravo.dof-rag-daily`. It runs at 05:30 local time and once whenever
the job is loaded. `launchd` is used instead of Unix cron because it is the
native macOS scheduler and runs a missed calendar job after the Mac wakes.

## What each run does

`scripts/update_dof_daily.py` runs the existing resumable pipeline in order:

1. Download both MAT and VES Word editions into the ignored `dof_word/` tree.
2. Convert new `.doc` files into the canonical `../dof_md` Markdown tree.
3. Append new documents to `dof_db/dof_corpus_l3.sqlite`.
4. Add the documents to FTS5 and build their chunk recipes.
5. Generate Jina binary embeddings and append them to the sqlite-vec index.
6. Verify that document, FTS, chunk, embedding, and vec0 coverage agree.

Downloads are content-checked: an HTML error page returned under a `.doc`
filename is rejected and retried on the next run. The converter applies the
same check and quarantines such files to `<name>.doc.invalid`, which is
invisible to both the `*.doc` conversion scan and the downloader's resume
glob, so a stale error page can never block the watermark forever.
Conversion failures also
keep the contiguous completion watermark unchanged while successfully
converted documents continue through the indexes. When a DOF listing page
has no Word links, SIDOF notices for that date are still checked.

The updater uses a non-blocking lock, so a scheduled run exits harmlessly if a
catch-up is still running. It keeps raw Word files for auditability and because
the download and conversion stages are resumable.

## Date-window behavior

Without arguments, the updater reads a contiguous completion watermark from
`var/dof_update_state.json`. On its first run it migrates from the last date in
the full-build manifest. If that watermark is behind, it starts on the
following day; otherwise it rechecks the last seven days. This means an outage
longer than seven days still catches up automatically, a recent-date test
cannot hide an older gap, and late or corrected DOF posts are revisited.

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
`~/Library/LaunchAgents/com.jackbravo.dof-rag-daily.plist`. Both the plist
and the tiny shell launcher under
`~/Library/Application Support/DOF-RAG/` are templates: the installer bakes
in this checkout's absolute paths, so the job works from any clone and any
account. Re-run the installer after moving the repository.
