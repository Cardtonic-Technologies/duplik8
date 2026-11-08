"""Endpoint regressions with OCR dependencies stubbed to avoid model downloads."""

import importlib
import os
import sys
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import media_download


with patch.dict(sys.modules, {name: Mock() for name in ("paddleocr", "cv2", "numpy")}):
    with patch.dict(os.environ, {}, clear=True):
        app = importlib.import_module("app")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)
        self.auth = patch.multiple(app, BASIC_AUTH_USERNAME=None, BASIC_AUTH_PASSWORD=None)
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.download = patch.object(app, "download_image", return_value=b"image")
        self.fetch = self.download.start()
        self.addCleanup(self.download.stop)
        app.ocr_engine.ocr.reset_mock()
        app.ocr_engine.ocr.return_value = [[[[[0, 0], [1, 0], [1, 1], [0, 1]], ("hello", 0.98765)]]]
        app.cv2.imdecode.side_effect = None
        app.cv2.imdecode.return_value = object()

    def analyze(self, **kwargs):
        return self.client.post("/analyze", json={"image_url": "https://images.example/a?token=secret"}, **kwargs)

    def test_no_auth_configured_preserves_open_access_and_response_shape(self):
        response = self.analyze()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "message": "Analysis successful", "count": 1,
            "data": [{"text": "hello", "confidence": 0.9877, "box": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
        })

    def test_auth_failures_do_not_download_or_run_ocr(self):
        with patch.multiple(app, BASIC_AUTH_USERNAME="internal", BASIC_AUTH_PASSWORD="password"):
            for kwargs in ({}, {"auth": ("wrong", "password")}, {"auth": ("internal", "wrong")}):
                with self.subTest(kwargs=kwargs):
                    response = self.analyze(**kwargs)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["WWW-Authenticate"], "Basic")
            self.fetch.assert_not_called()
            app.ocr_engine.ocr.assert_not_called()
            self.assertEqual(self.analyze(auth=("internal", "password")).status_code, 200)

    def test_partial_auth_configuration_keeps_existing_behavior(self):
        for username, password, credentials in (
            ("internal", None, ("internal", "anything")),
            (None, "password", ("anything", "password")),
        ):
            with self.subTest(username=username), patch.multiple(
                app, BASIC_AUTH_USERNAME=username, BASIC_AUTH_PASSWORD=password
            ):
                self.assertEqual(self.analyze().status_code, 401)
                self.assertEqual(self.analyze(auth=credentials).status_code, 200)

    def test_health_remains_accessible_with_auth_configured(self):
        with patch.multiple(app, BASIC_AUTH_USERNAME="internal", BASIC_AUTH_PASSWORD="password"):
            self.assertEqual(self.client.get("/").status_code, 200)
        self.fetch.assert_not_called()

    def test_host_allowlist_is_passed_to_downloader(self):
        with patch.object(app, "MEDIA_ALLOWED_HOSTS", frozenset({"images.example"})):
            self.assertEqual(self.analyze().status_code, 200)
        self.fetch.assert_called_once_with(
            "https://images.example/a?token=secret", allowed_hosts=frozenset({"images.example"})
        )

    def test_download_errors_are_safe_and_preserve_size_limit_status(self):
        self.fetch.side_effect = media_download.MediaDownloadError("Image exceeds the download size limit.", 413)
        response = self.analyze()
        self.assertEqual(response.status_code, 413)
        self.assertNotIn("secret", response.text)
        app.ocr_engine.ocr.assert_not_called()

    def test_actual_downloader_redacts_exception_credentials_at_endpoint(self):
        self.fetch.side_effect = media_download.download_image
        with patch.object(media_download, "_resolve", side_effect=OSError("https://user:password@host/?token=secret")):
            response = self.analyze()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Failed to download image."})
        app.ocr_engine.ocr.assert_not_called()

    def test_invalid_images_and_decode_errors_have_safe_responses(self):
        app.cv2.imdecode.return_value = None
        self.assertEqual(self.analyze().status_code, 400)
        app.cv2.imdecode.side_effect = ValueError("secret")
        response = self.analyze()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "Image processing failed."})
        app.ocr_engine.ocr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
