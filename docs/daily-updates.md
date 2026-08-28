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
Conversion is restricted to the active date window, so an unrelated failed
file elsewhere in the same year cannot block today's watermark. Failures
inside the active window keep the contiguous completion watermark unchanged
while successfully converted documents continue through the indexes. When a
DOF listing page has no Word links, SIDOF notices for that date are still
checked. Empty HTTP 200 responses count as successful only when the expected
dated DOF/SIDOF page structure or DOF's explicit empty-date message is present.

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

If ingestion repairs an interrupted oversized document, it records that
document in a durable repair ledger. The FTS builder replaces the stale terms,
the chunk builder revisits the document even when it is below its checkpoint,
and the embedding and vec0 builders consume deletion queues before adding the
replacement chunks. The updater advances its watermark only after those queues
are empty and all four indexes agree on coverage.
