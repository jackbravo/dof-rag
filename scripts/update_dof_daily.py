#!/usr/bin/env python3
"""Resumable daily update for the production DOF corpus and search stores.

The default date window is self-healing: start with the day after the newest
publication in the corpus database, or seven days ago if the database is
already current.  Every pipeline stage is resumable and the process holds a
non-blocking file lock so launchd cannot overlap runs.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from corpus_store.db import connect
from corpus_store.sampler import parse_metadata

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = REPO.parent
DEFAULT_WORD_DIR = REPO / "dof_word"
DEFAULT_CORPUS = WORKSPACE / "dof_md"
DEFAULT_DB_DIR = REPO / "dof_db"
DEFAULT_MANIFEST = REPO / "var" / "dof_incremental_manifest.jsonl"
DEFAULT_LOCK = REPO / "var" / "dof_update.lock"
DEFAULT_STATE = REPO / "var" / "dof_update_state.json"
DEFAULT_GGUF = Path.home() / "dof-gguf" / "jina-v5-small-retrieval-F16.gguf"
CORPUS_VERSION = "dof-full-v1"

LOG = logging.getLogger("dof-update")


def iter_dates(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def choose_start_date(last_publication: date, end: date, lookback_days: int) -> date:
    """Choose a catch-up start while retaining a late-publication overlap."""
    overlap_start = end - timedelta(days=lookback_days - 1)
    catchup_start = last_publication + timedelta(days=1)
    return min(overlap_start, catchup_start)


def latest_publication(corpus_db: Path) -> date | None:
    with closing(connect(corpus_db)) as conn:
        value = conn.execute("SELECT MAX(publication_date) FROM documents").fetchone()[0]
    return date.fromisoformat(value) if value else None


def last_jsonl_record(path: Path) -> dict | None:
    """Read the last non-empty JSONL record without loading a large manifest."""
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with path.open("rb") as stream:
        position = stream.seek(0, 2)
        data = b""
        while position > 0:
            size = min(4096, position)
            position -= size
            stream.seek(position)
            data = stream.read(size) + data
            lines = [line for line in data.splitlines() if line.strip()]
            if len(lines) >= 2 or position == 0:
                return json.loads(lines[-1]) if lines else None
    return None


def completed_through(state: Path, full_manifest: Path, corpus_db: Path) -> date | None:
    """Return the contiguous update watermark, with migration fallbacks."""
    if state.is_file():
        payload = json.loads(state.read_text(encoding="utf-8"))
        return date.fromisoformat(payload["completed_through"])
    record = last_jsonl_record(full_manifest)
    if record and record.get("publication_date"):
        return date.fromisoformat(record["publication_date"])
    return latest_publication(corpus_db)


def write_state(path: Path, completed: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"completed_through": completed.isoformat()}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_manifest(corpus: Path, start: date, end: date, output: Path) -> int:
    """Write a small manifest containing Markdown files in the update window."""
    records: list[dict] = []
    for day in iter_dates(start, end):
        day_dir = corpus / day.strftime("%Y/%m/%d%m%Y")
        if not day_dir.is_dir():
            continue
        for path in sorted(day_dir.rglob("*.md")):
            if path.name.endswith(".bak") or not path.is_file():
                continue
            relpath = str(path.relative_to(corpus))
            records.append(
                {
                    "relpath": relpath,
                    "size_bytes": path.stat().st_size,
                    **parse_metadata(relpath),
                }
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary.replace(output)
    return len(records)


def run_step(label: str, *command: str) -> None:
    LOG.info("stage=%s", label)
    subprocess.run([sys.executable, *command], cwd=REPO, check=True)


def conversion_command(workers: int, start: date, end: date) -> list[str]:
    """Build a converter command scoped to the active publication window."""
    years = [str(year) for year in range(start.year, end.year + 1)]
    return [
        "convert_doc_to_md.py",
        "--years",
        *years,
        "--start-date",
        start.isoformat(),
        "--end-date",
        end.isoformat(),
        "--workers",
        str(workers),
        "--input-dir",
        str(DEFAULT_WORD_DIR),
        "--output-dir",
        str(DEFAULT_CORPUS),
    ]


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return None
    stream.seek(0)
    stream.truncate()
    stream.write(str(os.getpid()))
    stream.flush()
    return stream


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--sleep-delay", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gguf", type=Path, default=DEFAULT_GGUF)
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    args = make_parser().parse_args()
    if args.lookback_days < 1:
        raise SystemExit("--lookback-days must be at least 1")
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")

    corpus_db = DEFAULT_DB_DIR / "dof_corpus_l3.sqlite"
    chunks_db = DEFAULT_DB_DIR / "dof_chunks.sqlite"
    vectors_db = DEFAULT_DB_DIR / "dof_vectors_jina_binary.sqlite"
    vec0_db = DEFAULT_DB_DIR / "dof_vec0_jina_binary.sqlite"

    if not corpus_db.is_file():
        raise SystemExit(
            f"corpus database not found: {corpus_db}\n"
            "run the full corpus build first (docs/full-corpus-build.md)"
        )

    last_publication = latest_publication(corpus_db)
    watermark = completed_through(
        DEFAULT_STATE, DEFAULT_DB_DIR / "manifest_full.jsonl", corpus_db
    )
    if args.start_date:
        start = args.start_date
    elif watermark:
        start = choose_start_date(watermark, args.end_date, args.lookback_days)
    else:
        raise SystemExit("empty corpus database; provide --start-date explicitly")
    if start > args.end_date:
        raise SystemExit("start date must not be after end date")

    LOG.info(
        "window=%s..%s completed_through=%s database_latest=%s",
        start,
        args.end_date,
        watermark,
        last_publication,
    )
    if args.dry_run:
        return 0

    lock = acquire_lock(DEFAULT_LOCK)
    if lock is None:
        LOG.info("another DOF update is already running; exiting")
        return 0

    try:
        run_step(
            "download",
            "get_word_dof.py",
            start.strftime("%d/%m/%Y"),
            args.end_date.strftime("%d/%m/%Y"),
            "--output-dir",
            str(DEFAULT_WORD_DIR),
            "--editions",
            "both",
            "--sleep-delay",
            str(args.sleep_delay),
        )

        run_step("convert", *conversion_command(args.workers, start, args.end_date))

        manifest_count = build_manifest(
            DEFAULT_CORPUS, start, args.end_date, DEFAULT_MANIFEST
        )
        LOG.info("manifest=%s documents=%d", DEFAULT_MANIFEST, manifest_count)
        if manifest_count:
            run_step(
                "corpus",
                "-m",
                "corpus_store.ingest",
                "--corpus",
                str(DEFAULT_CORPUS),
                "--manifest",
                str(DEFAULT_MANIFEST),
                "--db",
                str(corpus_db),
                "--level",
                "3",
                "--corpus-version",
                CORPUS_VERSION,
            )

        run_step(
            "fts",
            "scripts/build_fts_full.py",
            "--corpus-db",
            str(corpus_db),
        )
        run_step(
            "chunks",
            "-m",
            "corpus_store.chunk_index",
            "--corpus-db",
            str(corpus_db),
            "--chunks-db",
            str(chunks_db),
        )
        run_step(
            "embeddings",
            "-m",
            "corpus_store.embed",
            "--corpus-db",
            str(corpus_db),
            "--chunks-db",
            str(chunks_db),
            "--vectors-db",
            str(vectors_db),
            "--gguf",
            str(args.gguf),
        )
        run_step(
            "vec0",
            "scripts/build_vec0_full.py",
            "--vectors-db",
            str(vectors_db),
            "--vec0-db",
            str(vec0_db),
        )

        if watermark is None or start <= watermark + timedelta(days=1):
            completed = max(watermark, args.end_date) if watermark else args.end_date
            write_state(DEFAULT_STATE, completed)
            LOG.info("completed_through=%s", completed)
        else:
            LOG.info(
                "leaving completed_through=%s unchanged after non-contiguous test window",
                watermark,
            )
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
