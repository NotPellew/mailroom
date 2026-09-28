"""End-to-end tests for web UI email ingestion (POST /api/ingest)."""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from Mailroom import app as app_module
from Mailroom.config import Config
from Mailroom.db import DB


class WebIngestApiTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.config_path = str(self.tmp_path / "config.json")
        self.config = Config(self.config_path)
        self.config.save()
        self.db = DB(self.config.database_path)

        self.app = app_module.create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.app.config["CSRF_TOKEN"]

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def headers(self, csrf=True, origin="http://127.0.0.1:5000"):
        h = {}
        if origin is not None:
            h["Origin"] = origin
        if csrf:
            h["X-CSRF-Token"] = self.csrf
        return h

    def _make_eml(self, message_id: str, subject: str, body: str) -> bytes:
        return (
            f"Message-ID: <{message_id}>\n"
            f"Subject: {subject}\n"
            f"From: Alice <alice@example.com>\n"
            f"To: Bob <bob@example.com>\n"
            f"Date: Mon, 28 Sep 2026 10:00:00 +0200\n"
            f"Content-Type: text/plain; charset=utf-8\n\n"
            f"{body}\n"
        ).encode("utf-8")

    def test_single_eml_upload(self):
        eml_bytes = self._make_eml("msg-1@test.com", "Test Subject 1", "Hello World 1")
        data = {
            "files": (io.BytesIO(eml_bytes), "test1.eml"),
        }
        res = self.client.post(
            "/api/ingest",
            data=data,
            content_type="multipart/form-data",
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["total_received"], 1)
        self.assertEqual(payload["ingested"], 1)
        self.assertEqual(payload["duplicates_or_existing"], 0)
        self.assertEqual(payload["failed"], 0)

        # Verify DB content via public boundary
        msg = self.db.get_message("local:local:msg-1@test.com")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["subject"], "Test Subject 1")

    def test_multiple_eml_upload(self):
        eml1 = self._make_eml("msg-10@test.com", "Batch 1", "Body 1")
        eml2 = self._make_eml("msg-20@test.com", "Batch 2", "Body 2")
        data = {
            "files": [
                (io.BytesIO(eml1), "mail1.eml"),
                (io.BytesIO(eml2), "mail2.eml"),
            ]
        }
        res = self.client.post(
            "/api/ingest",
            data=data,
            content_type="multipart/form-data",
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["total_received"], 2)
        self.assertEqual(payload["ingested"], 2)
        self.assertEqual(payload["duplicates_or_existing"], 0)

    def test_duplicate_eml_upload(self):
        eml = self._make_eml("duplicate-msg@test.com", "Original Subject", "Original Body")

        # First upload
        res1 = self.client.post(
            "/api/ingest",
            data={"files": (io.BytesIO(eml), "dup.eml")},
            content_type="multipart/form-data",
            headers=self.headers(),
        )
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.get_json()["ingested"], 1)
        self.assertEqual(res1.get_json()["duplicates_or_existing"], 0)

        # Second upload with same Message-ID
        res2 = self.client.post(
            "/api/ingest",
            data={"files": (io.BytesIO(eml), "dup.eml")},
            content_type="multipart/form-data",
            headers=self.headers(),
        )
        self.assertEqual(res2.status_code, 200)
        payload2 = res2.get_json()
        self.assertEqual(payload2["total_received"], 1)
        self.assertEqual(payload2["ingested"], 0)
        self.assertEqual(payload2["duplicates_or_existing"], 1)

    def test_zip_archive_upload(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("subfolder/msgA.eml", self._make_eml("zip-a@test.com", "Zip A", "Body A"))
            zf.writestr("msgB.eml", self._make_eml("zip-b@test.com", "Zip B", "Body B"))
            zf.writestr("__MACOSX/.msgA.eml", b"ignore macos metadata")
            zf.writestr(".DS_Store", b"ignore ds store")
            zf.writestr("notes.txt", b"not an eml")

        buf.seek(0)
        res = self.client.post(
            "/api/ingest",
            data={"files": (buf, "export.zip")},
            content_type="multipart/form-data",
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["total_received"], 2)
        self.assertEqual(payload["ingested"], 2)
        self.assertEqual(payload["duplicates_or_existing"], 0)

        self.assertIsNotNone(self.db.get_message("local:local:zip-a@test.com"))
        self.assertIsNotNone(self.db.get_message("local:local:zip-b@test.com"))

    def test_nested_zip_rejected(self):
        inner_buf = io.BytesIO()
        with zipfile.ZipFile(inner_buf, "w") as inner_zf:
            inner_zf.writestr("inner.eml", b"some content")
        inner_bytes = inner_buf.getvalue()

        outer_buf = io.BytesIO()
        with zipfile.ZipFile(outer_buf, "w") as outer_zf:
            outer_zf.writestr("nested.zip", inner_bytes)

        outer_buf.seek(0)
        res = self.client.post(
            "/api/ingest",
            data={"files": (outer_buf, "nested.zip")},
            content_type="multipart/form-data",
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Nested zip archives are not allowed", res.get_json()["error"])

    def test_csrf_protection(self):
        eml = self._make_eml("csrf-test@test.com", "Subject", "Body")
        res = self.client.post(
            "/api/ingest",
            data={"files": (io.BytesIO(eml), "test.eml")},
            content_type="multipart/form-data",
            headers=self.headers(csrf=False),
        )
        self.assertEqual(res.status_code, 403)

    def test_no_files_provided(self):
        res = self.client.post(
            "/api/ingest",
            data={},
            content_type="multipart/form-data",
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("No files provided", res.get_json()["error"])

    def test_corrupted_zip_archive(self):
        corrupt_bytes = b"PK\x03\x04not a valid zip file content"
        res = self.client.post(
            "/api/ingest",
            data={"files": (io.BytesIO(corrupt_bytes), "corrupt.zip")},
            content_type="multipart/form-data",
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Corrupted or invalid zip archive", res.get_json()["error"])

    def test_unsupported_file_tracked_as_failed(self):
        res = self.client.post(
            "/api/ingest",
            data={"files": (io.BytesIO(b"image binary data"), "picture.png")},
            content_type="multipart/form-data",
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["ingested"], 0)


if __name__ == "__main__":
    unittest.main()
