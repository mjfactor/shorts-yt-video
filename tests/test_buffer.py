"""Buffer/TikTok cross-post tests: mocked, no network. Python 3.11 unittest style."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core
from niches.base import Content, NarrationResult, VisualsResult


def _resp(payload):
    r = mock.Mock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class TestBufferTiktokUpload(unittest.TestCase):
    def test_missing_creds_returns_none_without_post(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BUFFER_API_KEY", None)
            with mock.patch.object(core.requests, "post") as post:
                self.assertIsNone(
                    core.buffer_tiktok_upload(Path("x.mp4"), {"title": "t"}))
                post.assert_not_called()

    def test_graphql_errors_returns_none(self):
        env = {"BUFFER_API_KEY": "key123",
               "CLOUDINARY_CLOUD_NAME": "cname",
               "CLOUDINARY_UPLOAD_PRESET": "preset"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("BUFFER_TIKTOK_CHANNEL_ID", None)
            payload = {"errors": [{"message": "nope",
                                    "extensions": {"code": "FORBIDDEN"}}]}
            with mock.patch.object(core.requests, "post",
                                    return_value=_resp(payload)):
                # errors[] must not raise out of the fail-soft wrapper.
                self.assertIsNone(
                    core.buffer_tiktok_upload(Path("x.mp4"), {"title": "t"}))

    def test_cloudinary_missing_url_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "short.mp4"
            video.write_bytes(b"\x00" * 16)
            env = {"BUFFER_API_KEY": "key123",
                    "BUFFER_TIKTOK_CHANNEL_ID": "ch1",
                    "CLOUDINARY_CLOUD_NAME": "cname",
                    "CLOUDINARY_UPLOAD_PRESET": "preset"}
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(core.requests, "post",
                                        return_value=_resp({})) as post:
                    self.assertIsNone(
                        core.buffer_tiktok_upload(video, {"title": "t"}))
                    # Explicit channel skips org queries: only Cloudinary.
                    post.assert_called_once()

    def test_success_posts_mutation_and_returns_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "short.mp4"
            video.write_bytes(b"\x00" * 16)
            cloud = _resp({"secure_url": "https://res.cloudinary.com/x/v.mp4"})
            buf = _resp({"data": {"createPost": {"post": {
                "id": "post123", "dueAt": "tomorrow"}}}})
            head = _resp({})
            head.status_code = 200
            env = {"BUFFER_API_KEY": "key123",
                    "BUFFER_TIKTOK_CHANNEL_ID": "ch1",
                    "CLOUDINARY_CLOUD_NAME": "cname",
                    "CLOUDINARY_UPLOAD_PRESET": "preset"}
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(core.requests, "post",
                                        side_effect=[cloud, buf]) as post, \
                     mock.patch.object(core.requests, "head",
                                        return_value=head):
                    self.assertEqual(
                        core.buffer_tiktok_upload(
                            video, {"title": "T", "description": "D"}),
                        "post123")
            self.assertEqual(post.call_count, 2)
            sent = post.call_args_list[1].kwargs["json"]
            self.assertIn("createPost", sent["query"])
            # Caption/ids travel via GraphQL variables (no interpolation).
            self.assertEqual(
                sent["variables"]["url"],
                "https://res.cloudinary.com/x/v.mp4")
            self.assertEqual(sent["variables"]["channelId"], "ch1")
            self.assertIn("T", sent["variables"]["text"])


    def test_defaults_used_when_no_cloudinary_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "short.mp4"
            video.write_bytes(b"\x00" * 16)
            env = {"BUFFER_API_KEY": "key123",
                   "BUFFER_TIKTOK_CHANNEL_ID": "ch1"}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("CLOUDINARY_CLOUD_NAME", None)
                os.environ.pop("CLOUDINARY_UPLOAD_PRESET", None)
                with mock.patch.object(
                        core, "_cloudinary_upload",
                        return_value="https://res.cloudinary.com/x/v.mp4") as cu, \
                     mock.patch.object(core, "_buffer_public_url_ok",
                                       return_value=True), \
                     mock.patch.object(
                         core, "_buffer_graphql",
                         return_value={"createPost": {"post": {
                             "id": "post123", "dueAt": "tomorrow"}}}):
                    self.assertEqual(
                        core.buffer_tiktok_upload(
                            video, {"title": "T", "description": "D"}),
                        "post123")
                cu.assert_called_once()
                self.assertEqual(cu.call_args[0][1:], ("epuxrxa5", "buffer_shorts"))

    def test_blank_cloudinary_env_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "short.mp4"
            video.write_bytes(b"\x00" * 16)
            env = {"BUFFER_API_KEY": "key123",
                   "BUFFER_TIKTOK_CHANNEL_ID": "ch1",
                   "CLOUDINARY_CLOUD_NAME": "   ",
                   "CLOUDINARY_UPLOAD_PRESET": ""}
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(
                        core, "_cloudinary_upload",
                        return_value="https://res.cloudinary.com/x/v.mp4") as cu, \
                     mock.patch.object(core, "_buffer_public_url_ok",
                                       return_value=True), \
                     mock.patch.object(
                         core, "_buffer_graphql",
                         return_value={"createPost": {"post": {
                             "id": "post123", "dueAt": "tomorrow"}}}):
                    self.assertEqual(
                        core.buffer_tiktok_upload(
                            video, {"title": "T", "description": "D"}),
                        "post123")
                cu.assert_called_once()
                self.assertEqual(cu.call_args[0][1:], ("epuxrxa5", "buffer_shorts"))


class TestRunRecordsBufferPostId(unittest.TestCase):
    def _niche(self, src):
        niche = mock.Mock(spec=["NAME", "generate_content", "narrate",
                                "build_visuals", "build_metadata"])
        niche.NAME = "story"
        content = Content(title="T", narration_text="hello world",
                          description="d", tags=["t"], extras={"slots": {}})
        niche.generate_content.return_value = content
        niche.narrate.return_value = NarrationResult(
            audio=src.parent / "n.mp3", duration=1.0, words=2, voice="v",
            extras={})
        niche.build_visuals.return_value = VisualsResult(
            mp4=src, srt=None, credits=[], used_ids=[], extras={})
        niche.build_metadata.return_value = {"title": "T", "description": "d",
                                             "tags": ["t"]}
        return niche

    def test_history_entry_carries_key_when_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            src.write_bytes(b"\x00" * 16)
            env = {"YT_CLIENT_ID": "", "YT_CLIENT_SECRET": "",
                   "YT_REFRESH_TOKEN": ""}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("BUFFER_API_KEY", None)
                with mock.patch.object(core, "OUT", Path(tmp)), \
                     mock.patch.object(core, "save_history") as save, \
                     mock.patch.object(core, "cleanup_intermediates"):
                    res = core.run(self._niche(src))
            entry = save.call_args[0][0]
            self.assertIn("tiktokBufferPostId", entry)
            self.assertIsNone(entry["tiktokBufferPostId"])
            self.assertIn("tiktok_buffer_post_id", res)
            self.assertIsNone(res["tiktok_buffer_post_id"])

    def test_history_entry_records_post_id_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            src.write_bytes(b"\x00" * 16)
            env = {"YT_CLIENT_ID": "", "YT_CLIENT_SECRET": "",
                   "YT_REFRESH_TOKEN": ""}
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(core, "OUT", Path(tmp)), \
                     mock.patch.object(core, "save_history") as save, \
                     mock.patch.object(core, "cleanup_intermediates"), \
                     mock.patch.object(core, "buffer_tiktok_upload",
                                       return_value="post999") as bt:
                    res = core.run(self._niche(src))
            bt.assert_called_once()
            self.assertEqual(save.call_args[0][0]["tiktokBufferPostId"],
                             "post999")
            self.assertEqual(res["tiktok_buffer_post_id"], "post999")


if __name__ == "__main__":
    unittest.main()
