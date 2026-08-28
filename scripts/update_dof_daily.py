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
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import sqlite_vec

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


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


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
        position = stream.seek(0, os.SEEK_END)
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


def command_environment() -> dict[str, str]:
    env = os.environ.copy()
    home = str(Path.home())
    preferred = [
        f"{home}/.local/bin",
        f"{home}/.cargo/bin",
        "/Applications/LibreOffice.app/Contents/MacOS",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    env["PATH"] = ":".join(preferred)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_step(label: str, command: list[str], *, check: bool = True) -> int:
    LOG.info("stage=%s", label)
    result = subprocess.run(command, cwd=REPO, env=command_environment(), check=False)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)
    return result.returncode


def preflight(args: argparse.Namespace) -> None:
    required_files = [
        args.corpus_db,
        args.chunks_db,
        args.vectors_db,
        args.vec0_db,
    ]
    executables = ["soffice", "pandoc"]
    if not args.skip_embeddings:
        required_files.append(args.gguf)
        executables.append("llama-server")
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise RuntimeError("missing required file(s): " + ", ".join(missing))
    if not args.corpus.is_dir():
        raise RuntimeError(f"canonical corpus directory not found: {args.corpus}")
    for executable in executables:
        if not shutil.which(executable, path=command_environment()["PATH"]):
            raise RuntimeError(f"required executable not found: {executable}")
    free_gib = shutil.disk_usage(args.corpus).free / 2**30
    if free_gib < args.minimum_free_gb:
        raise RuntimeError(
            f"only {free_gib:.1f} GiB free; require {args.minimum_free_gb:.1f} GiB"
        )


def snapshot(args: argparse.Namespace) -> dict[str, int | str | None]:
    with closing(connect(args.corpus_db)) as corpus:
        documents, latest, max_document = corpus.execute(
            "SELECT COUNT(*), MAX(publication_date), MAX(document_id) FROM documents"
        ).fetchone()
        fts = corpus.execute("SELECT COUNT(*) FROM documents_fts_docsize").fetchone()[0]
    with closing(sqlite3.connect(args.chunks_db)) as chunks_db:
        chunks, max_chunk_document = chunks_db.execute(
            "SELECT COUNT(*), MAX(document_id) FROM chunks"
        ).fetchone()
    with closing(sqlite3.connect(args.vectors_db)) as vectors_db:
        vectors = vectors_db.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
    with closing(sqlite3.connect(args.vec0_db)) as vec0_db:
        vec0_db.enable_load_extension(True)
        sqlite_vec.load(vec0_db)
        vec0 = vec0_db.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]
    return {
        "documents": documents,
        "latest_publication": latest,
        "max_document_id": max_document,
        "fts_documents": fts,
        "chunks": chunks,
        "max_chunk_document_id": max_chunk_document,
        "vectors": vectors,
        "vec0_vectors": vec0,
    }


def verify(state: dict[str, int | str | None], *, embeddings_complete: bool) -> None:
    if state["documents"] != state["fts_documents"]:
        raise RuntimeError(f"FTS coverage mismatch: {state}")
    if state["max_document_id"] != state["max_chunk_document_id"]:
        raise RuntimeError(f"chunk coverage mismatch: {state}")
    if embeddings_complete and state["chunks"] != state["vectors"]:
        raise RuntimeError(f"embedding coverage mismatch: {state}")
    if state["vectors"] != state["vec0_vectors"]:
        raise RuntimeError(f"vec0 coverage mismatch: {state}")


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
    parser.add_argument("--start-date", type=parse_iso_date)
    parser.add_argument("--end-date", type=parse_iso_date, default=date.today())
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--sleep-delay", type=float, default=1.0)
    parser.add_argument("--skip-embeddings", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--minimum-free-gb", type=float, default=5.0)
    parser.add_argument("--word-dir", type=Path, default=DEFAULT_WORD_DIR)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--db-dir", type=Path, default=DEFAULT_DB_DIR)
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

    args.corpus_db = args.db_dir / "dof_corpus_l3.sqlite"
    args.chunks_db = args.db_dir / "dof_chunks.sqlite"
    args.vectors_db = args.db_dir / "dof_vectors_jina_binary.sqlite"
    args.vec0_db = args.db_dir / "dof_vec0_jina_binary.sqlite"

    if not args.corpus_db.is_file():
        raise SystemExit(
            f"corpus database not found: {args.corpus_db}\n"
            "run the full corpus build first (docs/full-corpus-build.md), "
            "or pass --db-dir"
        )

    last_publication = latest_publication(args.corpus_db)
    watermark = completed_through(
        args.state, args.db_dir / "manifest_full.jsonl", args.corpus_db
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

    lock = acquire_lock(args.lock)
    if lock is None:
        LOG.info("another DOF update is already running; exiting")
        return 0

    try:
        preflight(args)
        before = snapshot(args)
        LOG.info("before=%s", json.dumps(before, sort_keys=True))

        download_code = run_step(
            "download",
            [
                sys.executable,
                "get_word_dof.py",
                start.strftime("%d/%m/%Y"),
                args.end_date.strftime("%d/%m/%Y"),
                "--output-dir",
                str(args.word_dir),
                "--editions",
                "both",
                "--sleep-delay",
                str(args.sleep_delay),
            ],
            check=False,
        )

        years = [str(year) for year in range(start.year, args.end_date.year + 1)]
        conversion_code = run_step(
            "convert",
            [
                sys.executable,
                "convert_doc_to_md.py",
                "--years",
                *years,
                "--workers",
                str(args.workers),
                "--input-dir",
                str(args.word_dir),
                "--output-dir",
                str(args.corpus),
            ],
            check=False,
        )

        manifest_count = build_manifest(args.corpus, start, args.end_date, args.manifest)
        LOG.info("manifest=%s documents=%d", args.manifest, manifest_count)
        if manifest_count:
            run_step(
                "corpus",
                [
                    sys.executable,
                    "-m",
                    "corpus_store.ingest",
                    "--corpus",
                    str(args.corpus),
                    "--manifest",
                    str(args.manifest),
                    "--db",
                    str(args.corpus_db),
                    "--level",
                    "3",
                    "--corpus-version",
                    CORPUS_VERSION,
                ],
            )

        run_step(
            "fts",
            [
                sys.executable,
                "scripts/build_fts_full.py",
                "--corpus-db",
                str(args.corpus_db),
            ],
        )
        run_step(
            "chunks",
            [
                sys.executable,
                "-m",
                "corpus_store.chunk_index",
                "--corpus-db",
                str(args.corpus_db),
                "--chunks-db",
                str(args.chunks_db),
            ],
        )
        if not args.skip_embeddings:
            run_step(
                "embeddings",
                [
                    sys.executable,
                    "-m",
                    "corpus_store.embed",
                    "--corpus-db",
                    str(args.corpus_db),
                    "--chunks-db",
                    str(args.chunks_db),
                    "--vectors-db",
                    str(args.vectors_db),
                    "--gguf",
                    str(args.gguf),
                ],
            )
        run_step(
            "vec0",
            [
                sys.executable,
                "scripts/build_vec0_full.py",
                "--vectors-db",
                str(args.vectors_db),
                "--vec0-db",
                str(args.vec0_db),
            ],
        )

        after = snapshot(args)
        verify(after, embeddings_complete=not args.skip_embeddings)
        LOG.info("after=%s", json.dumps(after, sort_keys=True))
        incomplete = []
        if download_code:
            incomplete.append("one or more downloads failed")
        if conversion_code:
            incomplete.append("one or more Word conversions failed")
        if incomplete:
            raise RuntimeError(
                "; ".join(incomplete)
                + "; completed files were indexed and the next run will retry the rest"
            )
        if watermark is None or start <= watermark + timedelta(days=1):
            write_state(args.state, args.end_date)
            LOG.info("completed_through=%s", args.end_date)
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
