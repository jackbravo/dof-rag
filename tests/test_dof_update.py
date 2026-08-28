import hashlib
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from corpus_store.ingest import SCHEMA, ingest_batch
from get_word_dof import has_valid_download, is_valid_word_payload
from scripts.update_dof_daily import (
    build_manifest,
    choose_start_date,
    completed_through,
    last_jsonl_record,
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


if __name__ == "__main__":
    unittest.main()
