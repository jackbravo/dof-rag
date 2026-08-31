import hashlib
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sqlite_vec

import convert_doc_to_md
import get_word_dof
from convert_doc_to_md import convert_single_doc
from corpus_store import chunk_index
from corpus_store.embed import (
    SCHEMA as VECTOR_SCHEMA,
)
from corpus_store.embed import (
    apply_chunk_vector_invalidations,
)
from corpus_store.ingest import SCHEMA, ingest_batch
from get_word_dof import (
    has_valid_download,
    is_valid_sidof_listing,
    is_valid_word_payload,
)
from rag_poc.chunker import DocPattern
from scripts.build_fts_full import FTS_DDL, repair_fts_documents
from scripts.build_vec0_full import (
    DDL as VEC0_DDL,
)
from scripts.build_vec0_full import (
    DELETIONS_DDL,
    apply_vector_deletions,
)
from scripts.update_dof_daily import (
    build_manifest,
    choose_start_date,
    completed_through,
    conversion_command,
    last_jsonl_record,
    non_regressing_watermark,
)


class DailyUpdateTests(unittest.TestCase):
    def test_catches_up_from_day_after_database(self):
        self.assertEqual(
            choose_start_date(date(2026, 4, 24), date(2026, 8, 27), 7),
            date(2026, 4, 25),
        )

    def test_uses_overlap_when_database_is_current(self):
        self.assertEqual(
            choose_start_date(date(2026, 8, 27), date(2026, 8, 27), 7),
            date(2026, 8, 21),
        )

    def test_manifest_contains_only_requested_dates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            included = root / "2026/08/26082026/MAT/001_DOF_20260826_MAT_1.md"
            excluded = root / "2026/08/20082026/MAT/001_DOF_20260820_MAT_2.md"
            included.parent.mkdir(parents=True)
            excluded.parent.mkdir(parents=True)
            included.write_text("included", encoding="utf-8")
            excluded.write_text("excluded", encoding="utf-8")
            manifest = root / "manifest.jsonl"

            count = build_manifest(
                root, date(2026, 8, 24), date(2026, 8, 27), manifest
            )

            self.assertEqual(count, 1)
            self.assertIn("001_DOF_20260826_MAT_1.md", manifest.read_text())
            self.assertNotIn("001_DOF_20260820_MAT_2.md", manifest.read_text())

    def test_existing_note_id_survives_page_reordering(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            existing = directory / "005_DOF_20260826_MAT_12345.doc"
            existing.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 1016)
            self.assertTrue(has_valid_download(directory, "12345"))
            self.assertFalse(has_valid_download(directory, "99999"))

    def test_html_error_page_is_not_a_word_download(self):
        payload = b"<!DOCTYPE html><title>500 error</title>" + b"x" * 1024
        self.assertFalse(is_valid_word_payload(payload))

    def test_only_known_word_signatures_are_valid(self):
        self.assertTrue(
            is_valid_word_payload(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 1024)
        )
        self.assertTrue(is_valid_word_payload(b"PK\x03\x04" + b"x" * 1024))
        self.assertFalse(
            is_valid_word_payload(b"\xef\xbb\xbf{\"error\":true}" + b"x" * 1024)
        )

    def test_sidof_listing_requires_requested_edition_tab(self):
        html = (
            "<html><head><title>Diario Oficial de la Federación</title></head>"
            "<body><div id='resp-tab3'>26-08-2026</div></body></html>"
        )
        self.assertTrue(
            is_valid_sidof_listing(html, "26", "08", "2026", "MAT")
        )
        self.assertFalse(
            is_valid_sidof_listing(html, "26", "08", "2026", "VES")
        )

    def test_conversion_scan_is_limited_to_active_date_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "2026/04/24042026/MAT/001_DOF_old.doc"
            active = root / "2026/08/26082026/MAT/001_DOF_active.doc"
            future = root / "2026/08/28082026/MAT/001_DOF_future.doc"
            for path in (old, active, future):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"word")

            with mock.patch.object(convert_doc_to_md, "INPUT_DIR", root):
                files = convert_doc_to_md.find_doc_files(
                    [2026], date(2026, 8, 25), date(2026, 8, 27)
                )

            self.assertEqual(files, [active])

    def test_updater_passes_the_active_window_to_converter(self):
        args = SimpleNamespace(
            workers=2,
            word_dir=Path("word"),
            corpus=Path("markdown"),
        )
        command = conversion_command(
            args, date(2025, 12, 30), date(2026, 1, 2)
        )
        self.assertEqual(
            command[command.index("--years") + 1:command.index("--start-date")],
            ["2025", "2026"],
        )
        self.assertEqual(command[command.index("--start-date") + 1], "2025-12-30")
        self.assertEqual(command[command.index("--end-date") + 1], "2026-01-02")

    def test_ingest_repairs_oversized_row_missing_segments(self):
        with tempfile.TemporaryDirectory() as temporary:
            corpus = Path(temporary)
            relpath = "2026/08/17082026/MAT/006_DOF_test.md"
            source = corpus / relpath
            source.parent.mkdir(parents=True)
            source.write_text("á" * 40, encoding="utf-8")
            raw = source.read_bytes()
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO documents(path, year, publication_date, section, markdown,"
                " byte_length, sha256, corpus_version) VALUES (?, 2026, '2026-08-17',"
                " 'MAT', '', ?, ?, 'dof-full-v1')",
                (relpath, len(raw), hashlib.sha256(raw).digest()),
            )
            conn.execute(
                "CREATE TRIGGER preserve_empty_markdown BEFORE UPDATE OF markdown"
                " ON documents BEGIN SELECT RAISE(ABORT, 'markdown was rewritten'); END"
            )
            doc_id = conn.execute("SELECT document_id FROM documents").fetchone()[0]

            inserted, _ = ingest_batch(
                conn,
                corpus,
                [{
                    "relpath": relpath,
                    "year": 2026,
                    "publication_date": "2026-08-17",
                    "section": "MAT",
                }],
                segment_threshold=16,
                corpus_version="dof-full-v1",
            )

            self.assertEqual(inserted, 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM document_segments WHERE document_id = ?",
                    (doc_id,),
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute("SELECT document_id FROM documents").fetchone()[0], doc_id
            )
            conn.close()

    def test_ingest_repairs_partial_segments(self):
        with tempfile.TemporaryDirectory() as temporary:
            corpus = Path(temporary)
            relpath = "2026/08/17082026/MAT/006_DOF_test.md"
            source = corpus / relpath
            source.parent.mkdir(parents=True)
            source.write_text("á" * 40, encoding="utf-8")
            raw = source.read_bytes()
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO documents(path, year, publication_date, section, markdown,"
                " byte_length, sha256, corpus_version) VALUES (?, 2026, '2026-08-17',"
                " 'MAT', '', ?, ?, 'dof-full-v1')",
                (relpath, len(raw), hashlib.sha256(raw).digest()),
            )
            doc_id = conn.execute("SELECT document_id FROM documents").fetchone()[0]
            # An interrupted import left only the first of three segments.
            conn.execute(
                "INSERT INTO document_segments (document_id, segment_index,"
                " start_offset, end_offset, segment_text) VALUES (?, 0, 0, 16, ?)",
                (doc_id, "á" * 16),
            )

            inserted, _ = ingest_batch(
                conn,
                corpus,
                [{
                    "relpath": relpath,
                    "year": 2026,
                    "publication_date": "2026-08-17",
                    "section": "MAT",
                }],
                segment_threshold=16,
                corpus_version="dof-full-v1",
            )

            self.assertEqual(inserted, 1)
            segments = conn.execute(
                "SELECT segment_text FROM document_segments"
                " WHERE document_id = ? ORDER BY segment_index",
                (doc_id,),
            ).fetchall()
            self.assertEqual(len(segments), 3)
            self.assertEqual("".join(row[0] for row in segments), "á" * 40)
            self.assertEqual(
                conn.execute(
                    "SELECT previous_markdown, fts_pending, chunks_pending"
                    " FROM document_repairs WHERE document_id = ?",
                    (doc_id,),
                ).fetchone(),
                ("á" * 16, 1, 1),
            )
            conn.close()

    def test_ingest_skips_structurally_complete_segmented_doc(self):
        with tempfile.TemporaryDirectory() as temporary:
            corpus = Path(temporary)
            relpath = "2026/08/17082026/MAT/006_DOF_test.md"
            source = corpus / relpath
            source.parent.mkdir(parents=True)
            source.write_text("á" * 40, encoding="utf-8")
            raw = source.read_bytes()
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO documents(path, year, publication_date, section, markdown,"
                " byte_length, sha256, corpus_version) VALUES (?, 2026, '2026-08-17',"
                " 'MAT', '', ?, ?, 'dof-full-v1')",
                (relpath, len(raw), hashlib.sha256(raw).digest()),
            )
            doc_id = conn.execute("SELECT document_id FROM documents").fetchone()[0]
            for index, start in enumerate((0, 16, 32)):
                segment = ("á" * 40)[start:start + 16]
                conn.execute(
                    "INSERT INTO document_segments (document_id, segment_index,"
                    " start_offset, end_offset, segment_text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (doc_id, index, start, start + len(segment), segment),
                )

            inserted, _ = ingest_batch(
                conn,
                corpus,
                [{
                    "relpath": relpath,
                    "year": 2026,
                    "publication_date": "2026-08-17",
                    "section": "MAT",
                }],
                segment_threshold=16,
                corpus_version="dof-full-v1",
            )

            self.assertEqual(inserted, 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM document_segments WHERE document_id = ?",
                    (doc_id,),
                ).fetchone()[0],
                3,
            )
            conn.close()

    def test_ingest_repairs_segments_with_wrong_offsets_and_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            corpus = Path(temporary)
            relpath = "2026/08/17082026/MAT/006_DOF_test.md"
            source = corpus / relpath
            source.parent.mkdir(parents=True)
            source.write_text("á" * 40, encoding="utf-8")
            raw = source.read_bytes()
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO documents(path, year, publication_date, section, markdown,"
                " byte_length, sha256, corpus_version) VALUES (?, 2026, '2026-08-17',"
                " 'MAT', '', ?, ?, 'dof-full-v1')",
                (relpath, len(raw), hashlib.sha256(raw).digest()),
            )
            doc_id = conn.execute("SELECT document_id FROM documents").fetchone()[0]
            # Count and total text length look complete, but the second row has
            # corrupt offsets/content. The old SUM(LENGTH) check accepted this.
            segments = [
                (0, 0, 16, "á" * 16),
                (1, 99, 115, "x" * 16),
                (2, 32, 40, "á" * 8),
            ]
            conn.executemany(
                "INSERT INTO document_segments (document_id, segment_index,"
                " start_offset, end_offset, segment_text) VALUES (?, ?, ?, ?, ?)",
                [(doc_id, *segment) for segment in segments],
            )

            inserted, _ = ingest_batch(
                conn,
                corpus,
                [{
                    "relpath": relpath,
                    "year": 2026,
                    "publication_date": "2026-08-17",
                    "section": "MAT",
                }],
                segment_threshold=16,
                corpus_version="dof-full-v1",
            )

            self.assertEqual(inserted, 1)
            repaired = conn.execute(
                "SELECT segment_index, start_offset, end_offset, segment_text"
                " FROM document_segments WHERE document_id = ? ORDER BY segment_index",
                (doc_id,),
            ).fetchall()
            self.assertEqual(
                repaired,
                [
                    (0, 0, 16, "á" * 16),
                    (1, 16, 32, "á" * 16),
                    (2, 32, 40, "á" * 8),
                ],
            )
            conn.close()

    def test_html_download_is_quarantined_for_redownload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            doc = root / "001_DOF_20260826_MAT_12345.doc"
            doc.write_bytes(b"<!DOCTYPE html><title>500</title>" + b"x" * 1024)

            result = convert_single_doc(doc, root / "001_DOF_20260826_MAT_12345.md")

            self.assertEqual(result["status"], "invalid_download")
            self.assertFalse(doc.exists())
            self.assertTrue(
                (root / "001_DOF_20260826_MAT_12345.doc.invalid").exists()
            )
            # The quarantined file is invisible to the downloader's resume
            # glob, so the notice is re-fetched on the next download pass.
            self.assertFalse(has_valid_download(root, "12345"))

    def test_non_word_error_body_is_quarantined_for_redownload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            doc = root / "001_DOF_20260826_MAT_12345.doc"
            doc.write_bytes(b"\xef\xbb\xbf{\"error\":true}" + b"x" * 1024)

            result = convert_single_doc(doc, root / "001_DOF_20260826_MAT_12345.md")

            self.assertEqual(result["status"], "invalid_download")
            self.assertFalse(doc.exists())
            self.assertTrue((root / "001_DOF_20260826_MAT_12345.doc.invalid").exists())

    def test_sidof_notices_checked_when_page_has_no_word_links(self):
        session = mock.Mock()
        response = mock.Mock()
        response.text = (
            "<html><head><title>DOF - Diario Oficial de la Federación</title></head>"
            "<body><div id='cuerpo_principal'>"
            "No hay datos para la fecha seleccionada</div></body></html>"
        )
        response.raise_for_status = lambda: None
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                get_word_dof, "process_sidof_notices", return_value=0
            ) as notices:
                downloaded = get_word_dof.process_dof_page(
                    session, "26/08/2026", "MAT", Path(temporary), sleep_delay=0
                )
        self.assertEqual(downloaded, 0)
        notices.assert_called_once()

    def test_malformed_empty_listing_is_an_error(self):
        session = mock.Mock()
        response = mock.Mock()
        response.text = "<html><title>500 error</title><body></body></html>"
        response.raise_for_status = lambda: None
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(get_word_dof, "ERROR_COUNT", 0),
                mock.patch.object(
                    get_word_dof, "process_sidof_notices", return_value=0
                ) as notices,
            ):
                downloaded = get_word_dof.process_dof_page(
                    session, "26/08/2026", "MAT", Path(temporary), sleep_delay=0
                )
                self.assertEqual(get_word_dof.ERROR_COUNT, 1)
        self.assertEqual(downloaded, 0)
        notices.assert_called_once()

    def test_malformed_sidof_listing_is_an_error(self):
        session = mock.Mock()
        response = mock.Mock()
        response.text = "<html><title>500 error</title><body></body></html>"
        response.raise_for_status = lambda: None
        session.get.return_value = response
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(get_word_dof, "ERROR_COUNT", 0),
        ):
            downloaded = get_word_dof.process_sidof_notices(
                session,
                "26",
                "08",
                "2026",
                "MAT",
                Path(temporary),
                sleep_delay=0,
            )
            self.assertEqual(get_word_dof.ERROR_COUNT, 1)
        self.assertEqual(downloaded, 0)

    def test_chunk_store_rejects_a_mixed_chunker_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus_db = root / "corpus.sqlite"
            chunks_db = root / "chunks.sqlite"

            setup = sqlite3.connect(corpus_db)
            setup.executescript(SCHEMA)
            setup.execute(
                "INSERT INTO corpus_meta (key, value) VALUES ('corpus_version', 'v1')"
            )
            markdown = "# Decreto\n\n" + "contenido del decreto " * 40
            raw = markdown.encode("utf-8")
            setup.execute(
                "INSERT INTO documents(path, year, publication_date, section, markdown,"
                " byte_length, sha256, corpus_version)"
                " VALUES ('2026/08/26082026/MAT/001.md', 2026, '2026-08-26', 'MAT',"
                " ?, ?, ?, 'v1')",
                (markdown, len(raw), hashlib.sha256(raw).digest()),
            )
            setup.commit()
            setup.close()

            stale = sqlite3.connect(chunks_db)
            stale.executescript(chunk_index.SCHEMA)
            stale.execute(
                "INSERT INTO chunks (document_id, path, chunk_index, pattern,"
                " start_offset, end_offset, spans_json, token_count, heading_path,"
                " chunk_hash, chunker_version, corpus_version)"
                " VALUES (1, 'x', 0, 'PLAIN', 0, 1, '[]', 1, NULL, ?, 'old-version', 'v1')",
                (b"0" * 32,),
            )
            stale.commit()
            stale.close()

            argv = [
                "chunk_index",
                "--corpus-db", str(corpus_db),
                "--chunks-db", str(chunks_db),
            ]
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaisesRegex(
                    RuntimeError, "Rebuild the chunk, vector, and vec0 stores"
                ):
                    chunk_index.main()

            check = sqlite3.connect(chunks_db)
            versions = dict(
                check.execute(
                    "SELECT chunker_version, COUNT(*) FROM chunks GROUP BY 1"
                ).fetchall()
            )
            check.close()
            # The incompatible row is preserved, but no current-version rows
            # or vectors can be mixed into this store.
            self.assertEqual(versions["old-version"], 1)
            self.assertNotIn(chunk_index.CHUNKER_VERSION, versions)

    def test_repaired_document_rebuilds_chunks_below_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus_db = root / "corpus.sqlite"
            chunks_db = root / "chunks.sqlite"
            markdown = "repaired document content long enough to align"

            corpus = sqlite3.connect(corpus_db)
            corpus.executescript(SCHEMA)
            corpus.execute(
                "INSERT INTO corpus_meta(key, value) VALUES ('corpus_version', 'v1')"
            )
            corpus.execute(
                "INSERT INTO documents(path, year, publication_date, section, markdown,"
                " byte_length, sha256, corpus_version) VALUES"
                " ('2026/08/26082026/MAT/001.md', 2026, '2026-08-26', 'MAT',"
                " ?, ?, ?, 'v1')",
                (
                    markdown,
                    len(markdown.encode()),
                    hashlib.sha256(markdown.encode()).digest(),
                ),
            )
            corpus.execute(
                "INSERT INTO document_repairs"
                " (document_id, repaired_sha256, previous_markdown,"
                " fts_pending, chunks_pending) VALUES (1, ?, 'stale', 0, 1)",
                (hashlib.sha256(markdown.encode()).digest(),),
            )
            corpus.commit()
            corpus.close()

            chunks = sqlite3.connect(chunks_db)
            chunks.executescript(chunk_index.SCHEMA)
            chunks.execute(
                "INSERT INTO chunks (chunk_id, document_id, path, chunk_index, pattern,"
                " start_offset, end_offset, spans_json, token_count, heading_path,"
                " chunk_hash, chunker_version, corpus_version) VALUES"
                " (10, 1, 'stale', 0, 'PLAIN', 0, 5, '[]', 1, '[]', ?, ?, 'v1')",
                (b"0" * 32, chunk_index.CHUNKER_VERSION),
            )
            chunks.commit()
            chunks.close()

            generated = SimpleNamespace(
                text=markdown,
                pattern=DocPattern.PLAIN_TEXT,
                chunk_index=0,
                heading_path=[],
            )
            argv = [
                "chunk_index",
                "--corpus-db", str(corpus_db),
                "--chunks-db", str(chunks_db),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(chunk_index, "split_text", return_value=[generated]),
                mock.patch.object(chunk_index, "_count_tokens", return_value=6),
            ):
                chunk_index.main()

            check = sqlite3.connect(chunks_db)
            current = check.execute(
                "SELECT chunk_id, path, chunk_hash FROM chunks WHERE document_id = 1"
            ).fetchone()
            self.assertGreater(current[0], 10)
            self.assertEqual(current[1], "2026/08/26082026/MAT/001.md")
            self.assertEqual(current[2], hashlib.sha256(markdown.encode()).digest())
            self.assertEqual(
                check.execute(
                    "SELECT chunk_id FROM chunk_vector_invalidations"
                ).fetchall(),
                [(10,)],
            )
            check.close()
            corpus = sqlite3.connect(corpus_db)
            self.assertEqual(
                corpus.execute(
                    "SELECT chunks_pending FROM document_repairs WHERE document_id = 1"
                ).fetchone()[0],
                0,
            )
            corpus.close()

    def test_new_chunks_are_flushed_before_repair_checkpoint_advances(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus_db = root / "corpus.sqlite"
            chunks_db = root / "chunks.sqlite"
            corpus = sqlite3.connect(corpus_db)
            corpus.executescript(SCHEMA)
            corpus.execute(
                "INSERT INTO corpus_meta(key, value) VALUES ('corpus_version', 'v1')"
            )
            for path, text in (
                ("old.md", "old document"),
                ("new.md", "new document"),
                ("repair.md", "repaired document"),
            ):
                raw = text.encode()
                corpus.execute(
                    "INSERT INTO documents(path, year, publication_date, section,"
                    " markdown, byte_length, sha256, corpus_version)"
                    " VALUES (?, 2026, '2026-08-26', 'MAT', ?, ?, ?, 'v1')",
                    (path, text, len(raw), hashlib.sha256(raw).digest()),
                )
            corpus.execute(
                "INSERT INTO document_repairs"
                " (document_id, repaired_sha256, previous_markdown,"
                " fts_pending, chunks_pending) VALUES (3, ?, 'stale', 0, 1)",
                (hashlib.sha256(b"repaired document").digest(),),
            )
            corpus.commit()
            corpus.close()

            chunks = sqlite3.connect(chunks_db)
            chunks.executescript(chunk_index.SCHEMA)
            chunks.execute(
                "INSERT INTO chunks (chunk_id, document_id, path, chunk_index, pattern,"
                " start_offset, end_offset, spans_json, token_count, heading_path,"
                " chunk_hash, chunker_version, corpus_version) VALUES"
                " (1, 1, 'old.md', 0, 'plain_text', 0, 3, '{\"l\":\"old\"}',"
                " 1, '[]', ?, ?, 'v1')",
                (b"0" * 32, chunk_index.CHUNKER_VERSION),
            )
            chunks.commit()
            chunks.close()

            class ChunkConnectionProxy:
                def __init__(self, connection):
                    self.connection = connection
                    self.chunk_inserts = 0

                def __enter__(self):
                    self.connection.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.connection.__exit__(*args)

                def executemany(self, sql, parameters):
                    rows = list(parameters)
                    if sql.lstrip().startswith("INSERT INTO chunks"):
                        self.chunk_inserts += 1
                        # Before the fix, document 2 is the final flush after
                        # repair document 3 has already advanced the checkpoint.
                        if rows and rows[0][0] == 2 and self.chunk_inserts > 1:
                            raise RuntimeError("simulated crash before final flush")
                    return self.connection.executemany(sql, rows)

                def __getattr__(self, name):
                    return getattr(self.connection, name)

            real_connect = sqlite3.connect
            proxy = None

            def connect_for_test(path, *args, **kwargs):
                nonlocal proxy
                connection = real_connect(path, *args, **kwargs)
                if str(path) == str(chunks_db):
                    proxy = ChunkConnectionProxy(connection)
                    return proxy
                return connection

            def generated(text, *_):
                return [
                    SimpleNamespace(
                        text=text,
                        pattern=DocPattern.PLAIN_TEXT,
                        chunk_index=0,
                        heading_path=[],
                    )
                ]
            argv = [
                "chunk_index",
                "--corpus-db", str(corpus_db),
                "--chunks-db", str(chunks_db),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(chunk_index.sqlite3, "connect", side_effect=connect_for_test),
                mock.patch.object(chunk_index, "split_text", side_effect=generated),
                mock.patch.object(chunk_index, "_count_tokens", return_value=1),
            ):
                chunk_index.main()

            self.assertEqual(proxy.chunk_inserts, 2)
            check = sqlite3.connect(chunks_db)
            self.assertEqual(
                check.execute(
                    "SELECT document_id FROM chunks ORDER BY document_id"
                ).fetchall(),
                [(1,), (2,), (3,)],
            )
            check.close()

    def test_repaired_document_replaces_stale_fts_tokens(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        new_text = "newtoken repaired publication"
        conn.execute(
            "INSERT INTO documents(path, year, publication_date, section, markdown,"
            " byte_length, sha256, corpus_version) VALUES"
            " ('x.md', 2026, '2026-08-26', 'MAT', ?, ?, ?, 'v1')",
            (new_text, len(new_text), hashlib.sha256(new_text.encode()).digest()),
        )
        conn.execute(
            "INSERT INTO document_repairs"
            " (document_id, repaired_sha256, previous_markdown,"
            " fts_pending, chunks_pending) VALUES (1, ?, 'oldtoken stale', 1, 0)",
            (hashlib.sha256(new_text.encode()).digest(),),
        )
        conn.execute(FTS_DDL)
        conn.execute(
            "INSERT INTO documents_fts(rowid, markdown) VALUES (1, 'oldtoken stale')"
        )
        conn.commit()

        repaired, newly_indexed = repair_fts_documents(conn)

        self.assertEqual((repaired, newly_indexed), (1, 0))
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM documents_fts"
                " WHERE documents_fts MATCH 'oldtoken'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM documents_fts"
                " WHERE documents_fts MATCH 'newtoken'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT previous_markdown, fts_pending FROM document_repairs"
            ).fetchone(),
            (None, 0),
        )
        conn.close()

    def test_chunk_and_vec0_deletion_queues_are_consumed(self):
        chunks = sqlite3.connect(":memory:")
        chunks.executescript(chunk_index.SCHEMA)
        chunks.execute(
            "INSERT INTO chunk_vector_invalidations(chunk_id) VALUES (7)"
        )
        vectors = sqlite3.connect(":memory:")
        vectors.executescript(VECTOR_SCHEMA)
        vectors.execute(
            "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES (7, ?)",
            (b"x" * 128,),
        )

        self.assertEqual(apply_chunk_vector_invalidations(chunks, vectors), 1)
        self.assertEqual(vectors.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0], 0)
        self.assertEqual(vectors.execute("SELECT chunk_id FROM vector_deletions").fetchall(), [(7,)])
        self.assertEqual(chunks.execute("SELECT COUNT(*) FROM chunk_vector_invalidations").fetchone()[0], 0)

        vec0 = sqlite3.connect(":memory:")
        vec0.enable_load_extension(True)
        sqlite_vec.load(vec0)
        vec0.execute(VEC0_DDL)
        vec0.execute(
            "INSERT INTO chunk_vec(rowid, embedding) VALUES (7, vec_bit(?))",
            (b"x" * 128,),
        )
        vectors.execute(DELETIONS_DDL)
        vectors.commit()

        self.assertEqual(apply_vector_deletions(vec0, vectors), 1)
        self.assertEqual(vec0.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0], 0)
        self.assertEqual(vectors.execute("SELECT COUNT(*) FROM vector_deletions").fetchone()[0], 0)
        vec0.close()
        vectors.close()
        chunks.close()

    def test_launchd_renderer_preserves_spaces_and_xml_characters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "rendered.plist"
            runner = root / "Application Support/DOF & RAG/run.sh"
            repository = root / "DOF & RAG/repo"
            subprocess.run(
                [
                    "zsh",
                    "scripts/install_dof_launchd.sh",
                    "--render-plist",
                    str(output),
                    str(runner),
                    str(repository),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            with output.open("rb") as stream:
                payload = plistlib.load(stream)
            self.assertEqual(
                payload["ProgramArguments"],
                [
                    "/usr/bin/nice",
                    "-n",
                    "10",
                    "/usr/bin/caffeinate",
                    "-i",
                    str(runner),
                ],
            )
            self.assertEqual(
                payload["EnvironmentVariables"]["DOF_REPO_DIR"], str(repository)
            )
            self.assertEqual(
                payload["StandardOutPath"], str(repository / "logs/dof-daily.log")
            )

    def test_full_manifest_is_initial_contiguous_watermark(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.jsonl"
            manifest.write_text(
                '{"publication_date": "2026-04-23"}\n'
                '{"publication_date": "2026-04-24"}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                completed_through(root / "missing-state.json", manifest, root / "db"),
                date(2026, 4, 24),
            )
            self.assertEqual(
                last_jsonl_record(manifest), {"publication_date": "2026-04-24"}
            )

    def test_historical_window_never_moves_watermark_backward(self):
        self.assertEqual(
            non_regressing_watermark(date(2026, 8, 28), date(2026, 8, 27)),
            date(2026, 8, 28),
        )
        self.assertEqual(
            non_regressing_watermark(date(2026, 8, 27), date(2026, 8, 28)),
            date(2026, 8, 28),
        )


if __name__ == "__main__":
    unittest.main()
