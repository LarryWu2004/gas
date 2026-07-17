from __future__ import annotations

from datetime import datetime, timedelta
import io
import tempfile
from pathlib import Path
import unittest
from urllib.parse import parse_qs, quote, urlparse
import zipfile

from gas_diagnosis import web_app as core
from gas_diagnosis.pdf_report import _pdf_html
from gas_diagnosis.server import create_app


class ProductionServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_paths = (core.ROOT, core.UPLOAD_DIR, core.REPORT_DIR, core.UPLOAD_LOG)
        core.ROOT = self.root
        core.UPLOAD_DIR = self.root / "outputs" / "web_uploads"
        core.REPORT_DIR = self.root / "outputs" / "web_diagnosis"
        core.UPLOAD_LOG = core.REPORT_DIR / "upload_diagnosis_log.csv"
        self.app = create_app({"TESTING": True, "MAX_CONTENT_LENGTH": 2 * 1024 * 1024})
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        core.ROOT, core.UPLOAD_DIR, core.REPORT_DIR, core.UPLOAD_LOG = self.original_paths
        self.temp_dir.cleanup()

    def test_health_and_security_headers(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")
        self.assertTrue(response.headers["X-Request-ID"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_pdf_html_expands_core_metric_details(self) -> None:
        source = (
            '<html><head><title>report</title></head><body>'
            '<details class="metric-details"><summary>P01</summary>'
            '<div class="metric-detail-body">calculation</div></details>'
            '<details class="other-details"><summary>other</summary></details>'
            '</body></html>'
        )
        printable = _pdf_html(source)
        self.assertIn('<details class="metric-details" open>', printable)
        self.assertIn('<details class="other-details">', printable)
        self.assertIn('id="pdf-print-layout"', printable)

    def test_upload_rejects_unsupported_extension(self) -> None:
        response = self.client.post(
            "/api/upload",
            data=b"not a workbook",
            headers={"X-Filename": "payload.exe"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(any(core.UPLOAD_DIR.iterdir()))

    def test_report_endpoint_is_confined_to_report_directory(self) -> None:
        report = core.REPORT_DIR / "job" / "diagnosis.pdf"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"%PDF-1.4\n% test report\n")
        allowed = self.client.get(core._public_report_link(report))
        self.assertEqual(allowed.status_code, 200)
        allowed.close()

        upload = core.UPLOAD_DIR / "private.csv"
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_text("secret", encoding="utf-8")
        denied = self.client.get("/file?path=outputs/web_uploads/private.csv")
        self.assertEqual(denied.status_code, 400)

    def test_csv_upload_runs_diagnosis_and_hides_server_paths(self) -> None:
        start = datetime(2026, 1, 1, 0, 0)
        rows = ["时间,出口压力"]
        for index in range(120):
            timestamp = start + timedelta(minutes=10 * index)
            pressure = 2.50 + ((index % 12) - 6) * 0.006
            rows.append(f"{timestamp:%Y-%m-%d %H:%M:%S},{pressure:.3f}")
        response = self.client.post(
            "/api/upload",
            data=("\n".join(rows) + "\n").encode("utf-8-sig"),
            headers={
                "X-Filename": quote("交付测试.csv"),
                "X-Performance-Params": quote('{"set_pressure_kpa":2.5}'),
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertNotIn("outputs", payload)
        self.assertNotIn("source_path", payload)
        report_link = payload["report_links"]["overview_pdf"]
        self.assertTrue(report_link.startswith("/file?path=outputs/web_diagnosis/"))
        self.assertTrue(report_link.endswith(".pdf"))
        self.assertNotIn(str(self.root), report_link)

        report_path = parse_qs(urlparse(report_link).query)["path"][0]
        report_response = self.client.get(f"/file?path={report_path}")
        self.assertEqual(report_response.status_code, 200)
        self.assertEqual(report_response.mimetype, "application/pdf")
        self.assertTrue(report_response.get_data().startswith(b"%PDF"))
        report_response.close()

    def test_batch_download_contains_pdf_reports(self) -> None:
        first = core.REPORT_DIR / "job-a" / "first.pdf"
        second = core.REPORT_DIR / "job-b" / "second.pdf"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_bytes(b"%PDF-1.4\nfirst\n")
        second.write_bytes(b"%PDF-1.4\nsecond\n")

        response = self.client.post(
            "/api/reports_zip",
            json={
                "reports": [
                    {"name": "站点甲", "path": core._public_report_link(first)},
                    {"name": "站点乙", "path": core._public_report_link(second)},
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), 2)
            self.assertTrue(all(name.endswith(".pdf") for name in names))


if __name__ == "__main__":
    unittest.main()
