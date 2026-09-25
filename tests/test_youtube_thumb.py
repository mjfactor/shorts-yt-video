"""YouTube upload and thumbnail handling unit tests: mocked, no network."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core

os.environ["YT_THUMB_VERIFY_DELAY_SEC"] = "0"
os.environ["YT_THUMB_WAIT_POLL_SEC"] = "0"
os.environ["YT_THUMB_WAIT_TIMEOUT_SEC"] = "0.01"


class FakeHttpError(Exception):
    def __init__(self, status: int, message: str = "error"):
        super().__init__(message)
        self.resp = mock.Mock()
        self.resp.status = status
        self.status_code = status


class TestYouTubeThumbnailWait(unittest.TestCase):
    def setUp(self):
        self._sleep_patch = mock.patch.object(core.time, "sleep")
        self._sleep_patch.start()

    def tearDown(self):
        self._sleep_patch.stop()
    def test_succeeded_available_returns_true(self):
        yt = mock.Mock()
        yt.videos().list().execute.return_value = {
            "items": [{
                "processingDetails": {
                    "processingStatus": "succeeded",
                    "thumbnailsAvailability": "available",
                }
            }]
        }
        self.assertTrue(core._wait_for_video_processable(yt, "vid123"))

    def test_succeeded_missing_thumbnails_availability_returns_true(self):
        yt = mock.Mock()
        yt.videos().list().execute.return_value = {
            "items": [{
                "processingDetails": {
                    "processingStatus": "succeeded",
                }
            }]
        }
        self.assertTrue(core._wait_for_video_processable(yt, "vid123"))

    def test_failed_status_returns_false(self):
        yt = mock.Mock()
        yt.videos().list().execute.return_value = {
            "items": [{
                "processingDetails": {
                    "processingStatus": "failed",
                }
            }]
        }
        self.assertFalse(core._wait_for_video_processable(yt, "vid123"))

    def test_terminated_status_returns_false(self):
        yt = mock.Mock()
        yt.videos().list().execute.return_value = {
            "items": [{
                "processingDetails": {
                    "processingStatus": "terminated",
                }
            }]
        }
        self.assertFalse(core._wait_for_video_processable(yt, "vid123"))

    def test_http_403_insufficient_scopes_returns_false_without_crash(self):
        yt = mock.Mock()
        yt.videos().list().execute.side_effect = FakeHttpError(
            403, "Request had insufficient authentication scopes."
        )
        # Should gracefully return False and not raise
        self.assertFalse(core._wait_for_video_processable(yt, "vid123"))

    def test_empty_items_returns_false(self):
        yt = mock.Mock()
        yt.videos().list().execute.return_value = {"items": []}
        self.assertFalse(core._wait_for_video_processable(yt, "vid123"))


class TestYouTubeHasCustomThumbnail(unittest.TestCase):
    def test_verified_true(self):
        yt = mock.Mock()
        yt.videos().list().execute.return_value = {
            "items": [{
                "contentDetails": {
                    "hasCustomThumbnail": True,
                }
            }]
        }
        self.assertIs(core._has_custom_thumbnail(yt, "vid123"), True)

    def test_checked_false(self):
        yt = mock.Mock()
        yt.videos().list().execute.return_value = {
            "items": [{
                "contentDetails": {
                    "hasCustomThumbnail": False,
                }
            }]
        }
        self.assertIs(core._has_custom_thumbnail(yt, "vid123"), False)

    def test_empty_items_returns_false(self):
        yt = mock.Mock()
        yt.videos().list().execute.return_value = {"items": []}
        self.assertIs(core._has_custom_thumbnail(yt, "vid123"), False)

    def test_http_403_insufficient_scopes_returns_none(self):
        yt = mock.Mock()
        yt.videos().list().execute.side_effect = FakeHttpError(
            403, "Request had insufficient authentication scopes."
        )
        self.assertIsNone(core._has_custom_thumbnail(yt, "vid123"))

    def test_general_exception_returns_none(self):
        yt = mock.Mock()
        yt.videos().list().execute.side_effect = RuntimeError("network down")
        self.assertIsNone(core._has_custom_thumbnail(yt, "vid123"))


class TestYouTubeSetAndVerifyThumbnail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.thumb = Path(self.tmp.name) / "thumb.jpg"
        self.thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
        os.environ["YT_THUMB_VERIFY_DELAY_SEC"] = "0"
        os.environ["YT_THUMB_WAIT_POLL_SEC"] = "0"
        os.environ["YT_THUMB_WAIT_TIMEOUT_SEC"] = "0.01"

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("YT_THUMB_VERIFY_DELAY_SEC", None)
        os.environ.pop("YT_THUMB_WAIT_POLL_SEC", None)
        os.environ.pop("YT_THUMB_WAIT_TIMEOUT_SEC", None)

    @mock.patch.object(core, "_wait_for_video_processable", return_value=True)
    @mock.patch.object(core, "_set_thumbnail_once", return_value={"items": [{"id": "t1"}]})
    @mock.patch.object(core, "_has_custom_thumbnail", return_value=True)
    def test_success_path_single_attempt(self, mock_has, mock_set, mock_wait):
        yt = mock.Mock()
        res = core._set_and_verify_thumbnail(yt, "vid123", self.thumb)
        self.assertTrue(res)
        mock_wait.assert_called_once_with(yt, "vid123")
        mock_set.assert_called_once_with(yt, "vid123", self.thumb)
        mock_has.assert_called_once_with(yt, "vid123")

    @mock.patch.object(core, "_wait_for_video_processable", return_value=False)
    @mock.patch.object(core, "_set_thumbnail_once", return_value={"items": [{"id": "t1"}]})
    @mock.patch.object(core, "_has_custom_thumbnail", return_value=None)
    def test_insufficient_scope_skips_retry_and_succeeds(self, mock_has, mock_set, mock_wait):
        """When OAuth lacks read scope, set succeeds once and returns True without looping."""
        yt = mock.Mock()
        res = core._set_and_verify_thumbnail(yt, "vid123", self.thumb)
        self.assertTrue(res)
        mock_set.assert_called_once_with(yt, "vid123", self.thumb)
        mock_has.assert_called_once_with(yt, "vid123")

    @mock.patch.object(core, "_wait_for_video_processable", return_value=True)
    @mock.patch.object(core, "_set_thumbnail_once", return_value={"items": [{"id": "t1"}]})
    def test_retries_when_has_custom_thumbnail_is_false(self, mock_set, mock_wait):
        """When hasCustomThumbnail is explicitly False, retries up to max_tries."""
        yt = mock.Mock()
        with mock.patch.object(core, "_has_custom_thumbnail", side_effect=[False, True]), \
             mock.patch.object(core, "time") as mock_time:
            res = core._set_and_verify_thumbnail(yt, "vid123", self.thumb)
            self.assertTrue(res)
            self.assertEqual(mock_set.call_count, 2)

    @mock.patch.object(core, "_wait_for_video_processable", return_value=True)
    @mock.patch.object(core, "_set_thumbnail_once")
    def test_unverified_channel_403_on_set_aborts_immediately(self, mock_set, mock_wait):
        yt = mock.Mock()
        mock_set.side_effect = FakeHttpError(403, "The caller does not have permission")
        res = core._set_and_verify_thumbnail(yt, "vid123", self.thumb)
        self.assertFalse(res)
        self.assertEqual(mock_set.call_count, 1)


class TestYouTubeUploadThumbGuards(unittest.TestCase):
    def test_missing_thumb_file_skips_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"\x00" * 10)
            missing = Path(tmp) / "nonexistent.jpg"

            env = {"YT_CLIENT_ID": "cid", "YT_CLIENT_SECRET": "sec", "YT_REFRESH_TOKEN": "ref"}
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch("google.oauth2.credentials.Credentials"), \
                 mock.patch("googleapiclient.discovery.build") as mock_build, \
                 mock.patch("googleapiclient.http.MediaFileUpload"), \
                 mock.patch.object(core, "retry", return_value=(None, {"id": "v123"})), \
                 mock.patch.object(core, "_set_and_verify_thumbnail") as mock_set_thumb:
                mock_yt = mock.Mock()
                mock_build.return_value = mock_yt
                vid_id = core.upload(video, {"title": "Test", "description": "Desc"}, thumb=missing)
                self.assertEqual(vid_id, "v123")
                mock_set_thumb.assert_not_called()

    def test_non_jpeg_thumb_file_skips_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"\x00" * 10)
            png_thumb = Path(tmp) / "thumb.png"
            png_thumb.write_bytes(b"\x89PNG\r\n\x1a\n")

            env = {"YT_CLIENT_ID": "cid", "YT_CLIENT_SECRET": "sec", "YT_REFRESH_TOKEN": "ref"}
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch("google.oauth2.credentials.Credentials"), \
                 mock.patch("googleapiclient.discovery.build") as mock_build, \
                 mock.patch("googleapiclient.http.MediaFileUpload"), \
                 mock.patch.object(core, "retry", return_value=(None, {"id": "v123"})), \
                 mock.patch.object(core, "_set_and_verify_thumbnail") as mock_set_thumb:
                mock_yt = mock.Mock()
                mock_build.return_value = mock_yt
                vid_id = core.upload(video, {"title": "Test", "description": "Desc"}, thumb=png_thumb)
                self.assertEqual(vid_id, "v123")
                mock_set_thumb.assert_not_called()

    def test_oversized_thumb_file_skips_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"\x00" * 10)
            huge_thumb = Path(tmp) / "huge.jpg"
            # 2MB + 1 byte
            huge_thumb.write_bytes(b"\x00" * (2 * 1024 * 1024 + 1))

            env = {"YT_CLIENT_ID": "cid", "YT_CLIENT_SECRET": "sec", "YT_REFRESH_TOKEN": "ref"}
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch("google.oauth2.credentials.Credentials"), \
                 mock.patch("googleapiclient.discovery.build") as mock_build, \
                 mock.patch("googleapiclient.http.MediaFileUpload"), \
                 mock.patch.object(core, "retry", return_value=(None, {"id": "v123"})), \
                 mock.patch.object(core, "_set_and_verify_thumbnail") as mock_set_thumb:
                mock_yt = mock.Mock()
                mock_build.return_value = mock_yt
                vid_id = core.upload(video, {"title": "Test", "description": "Desc"}, thumb=huge_thumb)
                self.assertEqual(vid_id, "v123")
                mock_set_thumb.assert_not_called()

    def test_valid_thumb_triggers_set_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"\x00" * 10)
            thumb = Path(tmp) / "thumb.jpg"
            thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            env = {"YT_CLIENT_ID": "cid", "YT_CLIENT_SECRET": "sec", "YT_REFRESH_TOKEN": "ref"}
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch("google.oauth2.credentials.Credentials") as mock_creds_cls, \
                 mock.patch("googleapiclient.discovery.build") as mock_build, \
                 mock.patch("googleapiclient.http.MediaFileUpload"), \
                 mock.patch.object(core, "retry", return_value=(None, {"id": "v123"})), \
                 mock.patch.object(core, "_set_and_verify_thumbnail") as mock_set_thumb:
                mock_yt = mock.Mock()
                mock_build.return_value = mock_yt
                vid_id = core.upload(video, {"title": "Test", "description": "Desc"}, thumb=thumb)
                self.assertEqual(vid_id, "v123")
                mock_set_thumb.assert_called_once_with(mock_yt, "v123", thumb)
                # Verify Credentials was instantiated without forcing restricted scopes
                mock_creds_cls.assert_called_once_with(
                    None, refresh_token="ref", client_id="cid", client_secret="sec",
                    token_uri="https://oauth2.googleapis.com/token"
                )


if __name__ == "__main__":
    unittest.main()
