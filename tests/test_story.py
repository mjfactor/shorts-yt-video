"""Phase 3 story tests: mocked, no network. Python 3.11 unittest style."""
import copy
import json
import os
import re
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from engine import overlay_card, pexels, tts_cache
from engine.ass import write_ass
from engine.ffmpeg_utils import ass_cwd_args, escape_filter_path, subtitles_filter
from engine.gemini_client import pcm_to_wav
from niches.base import Content
from niches.story import content as story_content
from niches.story import narration as story_narr
from niches.story import visuals as story_visuals
from niches.story.config import StoryConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTENCES = [
    "The office clock ticked past midnight while tired workers finished reports quietly.",
    "My manager loved weekend emails and expected instant replies from everyone online.",
    "I brewed strong coffee and opened my laptop before sunrise each morning.",
    "Then I found out my boss told everyone I was putting the whole project at risk.",
    "Nobody shared lunch plans and my chair waited inside the cold storage room.",
    "I saved every late message and every weekend request inside one folder.",
    "So I printed every late night email and every weekend text for the meeting.",
    "The conference room fell silent when I placed the thick folder down gently.",
    "My boss never assigned another weekend shift after that quiet morning meeting.",
    "Colleagues cheered softly and shared snacks while sunlight filled the office.",
]


def _make_story(sentences=None):
    sentences = list(sentences or _SENTENCES)
    card = "I said no then they moved my desk"
    story = f"{card}. " + " ".join(sentences)
    hook = f"{card}."
    rehooks = [
        {"at": "20-30s", "line": sentences[3]},
        {"at": "50-60s", "line": sentences[6]},
        {"at": "80-90s", "line": sentences[8]},
    ]
    third = len(sentences) // 3
    beats = [
        {"text": f"{card}. " + " ".join(sentences[:third]),
         "query": "office desk night"},
        {"text": " ".join(sentences[third:2 * third]), "query": "empty office hallway"},
        {"text": " ".join(sentences[2 * third:]), "query": "conference room silent"},
    ]
    return {
        "title": "I Finally Said No to Weekend Work",
        "description": "She finally said no to weekend work. Would you have caved? #shorts",
        "tags": ["shorts", "storytime", "workplace", "revenge", "stories"],
        "narration": story,
        "story_text": story,
        "hook": hook,
        "genre": "workplace",
        "key_words": ["boss", "overtime", "office", "meeting", "weekend"],
        "bg_queries": [
            "office desk night",
            "empty office hallway",
            "work team meeting",
            "conference room silent",
            "city office windows",
            "desk clock overtime",
        ],
        "ending_type": "payoff",
        "pinned_comment": "Would YOU have said no?",
        "card_text": "I said no then they moved my desk",
        "card_highlight_word": "desk",
        "rehooks": rehooks,
        "beats": beats,
    }


def _words(n=5, step=0.5):
    vocab = ["elephant", "giraffe", "kangaroo", "penguin", "ostrich", "leopard"]
    out = []
    for i in range(n):
        out.append(
            {"word": vocab[i % len(vocab)], "start": round(i * step, 3),
             "end": round(i * step + 0.4, 3)}
        )
    return out


def _ass_dialogues(text):
    return [line for line in text.splitlines() if line.startswith("Dialogue:")]


def _ass_times(dlg):
    m = re.match(r"Dialogue: 0,([^,]+),([^,]+),", dlg)
    assert m, dlg
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# JSON validation + word budget
# ---------------------------------------------------------------------------

class TestStoryValidation(unittest.TestCase):
    def test_valid_payload_passes(self):
        data = _make_story()
        extra = story_content.validate_extra(data)
        self.assertIn("narration", extra)
        self.assertIn("beats", extra)
        self.assertEqual(extra["ending_type"], "payoff")

    def test_validation_rejects_bad(self):
        bad_mutations = {
            "missing title": lambda d: d.pop("title"),
            "missing beats": lambda d: d.pop("beats"),
            "too short": lambda d: d.update(
                {"narration": "too short", "story_text": "too short",
                 "beats": [{"text": "too short", "query": "office desk"}],
                 "rehooks": [{"at": "20-30s", "line": "too short"},
                             {"at": "50-60s", "line": "too short"}],
                 "hook": "too short"}),
            "tts hostile": lambda d: d.update(
                {"narration": d["narration"] + " *star*",
                 "story_text": d["story_text"] + " *star*",
                 "beats": [{"text": d["narration"] + " *star*", "query": "office"}]}),
            "copied marker": lambda d: d.update(
                {"narration": d["narration"] + " r/funny",
                 "story_text": d["story_text"] + " r/funny",
                 "beats": [{"text": d["narration"] + " r/funny", "query": "office"}]}),
            "unsafe": lambda d: d.update(
                {"narration": d["narration"] + " suicide",
                 "story_text": d["story_text"] + " suicide",
                 "beats": [{"text": d["narration"] + " suicide", "query": "office"}]}),
            "long sentence": lambda d: d.update(
                {"narration": "word " * 35,
                 "story_text": "word " * 35,
                 "hook": "word word word",
                 "beats": [{"text": "word " * 35, "query": "office"}],
                 "rehooks": [{"at": "20-30s", "line": "word " * 35},
                             {"at": "50-60s", "line": "word " * 35}]}),
            "hook mismatch": lambda d: d.update({"hook": "Totally different opening line here"}),
            "few bg queries": lambda d: d.update({"bg_queries": ["office"]}),
            "bad ending": lambda d: d.update({"ending_type": "twist"}),
            "missing rehooks": lambda d: d.update({"rehooks": []}),
            "beats mismatch": lambda d: d["beats"].append(
                {"text": "extra unmatched sentence here", "query": "office"}),
        }
        for label, mutate in bad_mutations.items():
            with self.subTest(case=label):
                data = _make_story()
                mutate(data)
                with self.assertRaises(ValueError, msg=label):
                    story_content.validate_extra(data)

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            story_content.validate_extra([])  # type: ignore[arg-type]


class TestStorySameModelRetry(unittest.TestCase):
    def test_prompt_has_hard_sentence_rule(self):
        prompt, _slots = story_content.build_prompt([], StoryConfig())
        self.assertIn("Every sentence MUST be 30 words or fewer", prompt)

    def test_validation_reject_retries_same_model_once(self):
        cfg = StoryConfig(target_min=30.0, target_max=55.0,
                          similarity_retries=0)
        good = _make_story()
        bad = copy.deepcopy(good)
        long_sent = ("This is a deliberately overlong sentence with far more "
                     "than thirty words in it so validation must reject it "
                     "outright without mercy today under the new limit for "
                     "sure right now indeed please.")
        for key in ("narration", "story_text"):
            bad[key] = bad[key] + " " + long_sent
        bad["beats"] = (list(good["beats"][:-1])
                        + [{"text": good["beats"][-1]["text"] + " " + long_sent,
                            "query": good["beats"][-1]["query"]}])
        with self.assertRaises(ValueError):
            story_content.validate_extra(bad, cfg, 2.9)
        client = mock.Mock()
        client.generate_json.side_effect = [bad, good]
        with mock.patch.object(story_content, "_make_client",
                               return_value=client):
            result = story_content.generate_content([], cfg)
        self.assertEqual(client.generate_json.call_count, 2)
        first_model = client.generate_json.call_args_list[0][0][0]
        second_model = client.generate_json.call_args_list[1][0][0]
        self.assertEqual(first_model, second_model)
        self.assertEqual(first_model, list(cfg.story_models)[0])
        # Retried draft accepted: narration matches the good payload.
        self.assertEqual(result.narration_text, good["narration"])


class TestStoryWordBudget(unittest.TestCase):
    def test_target_words_from_target_times_wps(self):
        cfg = StoryConfig()
        wps = 2.9
        lo, hi = story_content.target_words(cfg, wps)
        eff = story_content.effective_wps(cfg, wps)
        self.assertEqual(lo, round(cfg.target_min * eff))
        self.assertEqual(hi, round(cfg.target_max * eff))
        # Edge-aware: 2.9 * 1.2 = 3.48 * 1.08 ~= 3.76 -> ~376-451 window
        # for the default 100-120s targets (mid ~440w for 110s reality)
        self.assertGreaterEqual(lo, 360)
        self.assertLessEqual(hi, 470)
        self.assertLess(lo, hi)

    def test_word_budget_at_measured_edge_rate(self):
        # 4.3 wps history x +20% clamps to 4.2, x 1.08 overshoot ~= 4.54.
        cfg = StoryConfig()
        wps = 4.3
        lo, hi = story_content.target_words(cfg, wps)
        eff = story_content.effective_wps(cfg, wps)
        self.assertEqual(lo, round(100 * eff))
        self.assertEqual(hi, round(120 * eff))
        self.assertGreaterEqual(lo, 440)
        self.assertLessEqual(hi, 560)
        self.assertLess(lo, hi)

    def test_measure_wps_median_and_clamp(self):
        hist = [
            {"words": 290, "duration": 100.0},  # 2.9
            {"words": 300, "duration": 100.0},  # 3.0
            {"words": 310, "duration": 100.0},  # 3.1
        ]
        self.assertAlmostEqual(story_content.measure_wps(hist, 2.9), 3.0)
        self.assertEqual(story_content.measure_wps([], 2.9), 2.9)
        # clamp to [2.2, 4.2] (edge reality 3.5-3.8 must survive)
        fast = [{"words": 900, "duration": 100.0}]
        self.assertLessEqual(story_content.measure_wps(fast, 2.9), 4.2)
        slow = [{"words": 50, "duration": 100.0}]
        self.assertGreaterEqual(story_content.measure_wps(slow, 2.9), 2.2)

    def test_sanitize_title_caps_at_100(self):
        long_title = "Word " * 40
        out = story_content.sanitize_title(long_title)
        self.assertLessEqual(len(out), 100)
        self.assertTrue(out)

    def test_chunk_text_60_to_90_words(self):
        text = " ".join(_SENTENCES * 3)  # ~360 words
        chunks = story_narr.chunk_text(text)
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            n = len(chunk.split())
            self.assertLessEqual(n, 90)
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_pick_voice_and_genre_style(self):
        cfg = StoryConfig()
        voice, _why = story_narr.pick_voice(cfg, "seed-title|workplace")
        self.assertIn(voice, list(cfg.tts_voices))
        again, _ = story_narr.pick_voice(cfg, "seed-title|workplace")
        self.assertEqual(voice, again)
        self.assertTrue(story_narr.genre_style("scary"))
        self.assertTrue(story_narr.genre_style("unknown-genre-xyz"))


# ---------------------------------------------------------------------------
# Duration fit
# ---------------------------------------------------------------------------

class TestDurationFit(unittest.TestCase):
    def test_fit_speed_branches(self):
        # in-window first pass stays small; far durations push harder (capped)
        near = story_narr._fit_speed(story_narr.MID_TARGET)
        self.assertGreaterEqual(near, 1.05)
        self.assertLessEqual(near, 1.25)
        fast = story_narr._fit_speed(story_narr.MID_TARGET * 2)
        self.assertEqual(fast, 1.25)
        slow = story_narr._fit_speed(story_narr.MID_TARGET / 2)
        self.assertAlmostEqual(slow, 1 / 1.25)
        over = story_narr._fit_speed(130.0)
        self.assertGreater(over, 1.05)
        under = story_narr._fit_speed(80.0)
        self.assertLess(under, 1.0)

    def test_card_end_time_branches(self):
        words = [
            {"word": "hello", "start": 0.0, "end": 0.5},
            {"word": "world", "start": 0.5, "end": 1.0},
            {"word": "again", "start": 1.0, "end": 1.5},
        ]
        # card_text hit
        self.assertAlmostEqual(
            story_narr.card_end_time(words, "hello world", "hello"), 1.0
        )
        # hook fallback when card misses
        self.assertAlmostEqual(
            story_narr.card_end_time(words, "zzz missing", "hello"), 0.5
        )
        # first-words fallback
        self.assertAlmostEqual(
            story_narr.card_end_time(words, "", ""), 1.5
        )
        # empty words
        self.assertEqual(story_narr.card_end_time([], "", ""), 2.0)

    def test_trim_to_sentence_arg_list(self):
        words = [
            {"word": "Hello.", "start": 0.0, "end": 1.0},
            {"word": "World.", "start": 1.0, "end": 2.0},
            {"word": "Again.", "start": 2.0, "end": 90.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp3"
            src.write_bytes(b"fake")
            out = Path(tmp) / "out.mp3"
            with mock.patch(
                "niches.story.narration.run_ffmpeg"
            ) as run_mock:
                story_narr._trim_to_sentence(src, words, 85.0, out)
            cmd = run_mock.call_args[0][0]
            self.assertIsInstance(cmd, list)
            self.assertEqual(cmd[0], "ffmpeg")
            self.assertIn("-t", cmd)
            idx = cmd.index("-t")
            # trims at last sentence end (2.0) before max_seconds
            self.assertAlmostEqual(float(cmd[idx + 1]), 2.0)


# ---------------------------------------------------------------------------
# PCM / cache / ASS / card / pexels
# ---------------------------------------------------------------------------

class TestPcmToWav(unittest.TestCase):
    def test_pcm_to_wav_roundtrip(self):
        rate = 24000
        samples = [0, 1000, -1000, 32767, -32768] * 100
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tone.wav"
            result = pcm_to_wav(pcm, rate, out)
            self.assertTrue(result.exists())
            with wave.open(str(result), "rb") as w:
                self.assertEqual(w.getnchannels(), 1)
                self.assertEqual(w.getsampwidth(), 2)
                self.assertEqual(w.getframerate(), rate)
                self.assertEqual(w.getnframes(), len(samples))
                raw = w.readframes(len(samples))
            self.assertEqual(raw, pcm)


class TestTtsCache(unittest.TestCase):
    def test_hash_key_stable_and_sensitive(self):
        first = tts_cache.hash_key("hello", "Kore", "style", 1.0)
        self.assertEqual(first, tts_cache.hash_key("hello", "Kore", "style", 1.0))
        self.assertNotEqual(first, tts_cache.hash_key("hello!", "Kore", "style", 1.0))
        self.assertNotEqual(first, tts_cache.hash_key("hello", "Puck", "style", 1.0))

    def test_store_and_resume_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            key = tts_cache.hash_key("chunk", "Kore", "", 1.0)
            self.assertFalse(tts_cache.is_cached(cache, key, ".pcm"))
            tts_cache.store_bytes(b"pcm-bytes", cache, key, ".pcm")
            self.assertTrue(tts_cache.is_cached(cache, key, ".pcm"))
            self.assertEqual(
                (cache / f"{key}.pcm").read_bytes(), b"pcm-bytes"
            )
            # resume helpers: chunk_00/02 present, 01 missing
            for i in (0, 2):
                (Path(tmp) / tts_cache.chunk_name("narr", i)).write_bytes(b"x")
            with mock.patch("pathlib.Path", Path):
                cached = tts_cache.cached_indices(tmp, "narr", 3)
                missing = tts_cache.missing_indices(tmp, "narr", 3)
            self.assertEqual(cached, [0, 2])
            self.assertEqual(missing, [1])
            # empty slot does not count as cached
            empty_key = tts_cache.hash_key("empty", "Kore", "", 1.0)
            (cache / f"{empty_key}.pcm").write_bytes(b"")
            self.assertFalse(tts_cache.is_cached(cache, empty_key, ".pcm"))


class TestAssCaptions(unittest.TestCase):
    def test_one_event_per_word_gapless_pop_upper(self):
        words = _words(5)
        with tempfile.TemporaryDirectory() as tmp:
            ass_path = Path(tmp) / "caps.ass"
            write_ass(words, ass_path, upper=False, gapless=True)
            text = ass_path.read_text(encoding="utf-8")
            dialogues = _ass_dialogues(text)
            # long words (>4 chars) are never grouped: one event per word
            self.assertEqual(len(dialogues), len(words))
            # pop animation tags on every event
            for dlg in dialogues:
                self.assertIn("fscx", dlg)
            # gapless: each end == next start
            for first, second in zip(dialogues, dialogues[1:]):
                _, end = _ass_times(first)
                start, _ = _ass_times(second)
                self.assertEqual(end, start)
            # UPPER flag uppercases visible text
            ass_upper = Path(tmp) / "caps_upper.ass"
            write_ass(words, ass_upper, upper=True, gapless=True)
            upper_text = ass_upper.read_text(encoding="utf-8")
            for w in words:
                self.assertIn(str(w["word"]).upper(), upper_text)


class TestOverlayCard(unittest.TestCase):
    def test_overlay_card_png_non_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "card.png"
            result = overlay_card.render_card(
                "I said no then they moved my desk",
                "desk",
                handle="@mystery",
                avatar_path="no-such-avatar.png",
                out_png=out,
            )
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 0)
            from PIL import Image

            with Image.open(result) as img:
                self.assertEqual(img.format, "PNG")
                self.assertGreater(img.width, 0)
                self.assertGreater(img.height, 0)


class TestPickVideoFile(unittest.TestCase):
    def test_closest_to_1080x1920(self):
        files = [
            {"link": "http://x/small.mp4", "width": 640, "height": 960,
             "file_type": "video/mp4"},
            {"link": "http://x/exact.mp4", "width": 1080, "height": 1920,
             "file_type": "video/mp4"},
            {"link": "http://x/big.mp4", "width": 2160, "height": 3840,
             "file_type": "video/mp4"},
        ]
        picked = pexels.pick_video_file(files, target_w=1080, target_h=1920)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["link"], "http://x/exact.mp4")

    def test_prefers_height_over_target(self):
        files = [
            {"link": "http://x/wide.mp4", "width": 1070, "height": 1500,
             "file_type": "video/mp4"},
            {"link": "http://x/tall.mp4", "width": 800, "height": 2000,
             "file_type": "video/mp4"},
        ]
        picked = pexels.pick_video_file(files, target_w=1080, target_h=1920)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["link"], "http://x/tall.mp4")

    def test_none_when_unusable(self):
        self.assertIsNone(pexels.pick_video_file([], target_w=1080, target_h=1920))
        self.assertIsNone(
            pexels.pick_video_file([{"width": 1080, "height": 1920}], target_w=1080,
                                   target_h=1920)
        )

    def test_strict_mp4_only(self):
        files = [
            {"link": "http://x/a.mov", "width": 1080, "height": 1920,
             "file_type": "video/quicktime"},
            {"link": "http://x/b.mp4", "width": 720, "height": 1280,
             "file_type": "video/mp4"},
            {"link": "http://x/c.mp4", "width": 1080, "height": 1920},
        ]
        picked = pexels.pick_video_file(files, target_w=1080, target_h=1920)
        self.assertIsNotNone(picked)
        assert picked is not None
        # quicktime excluded, missing file_type excluded: only exact mp4
        self.assertEqual(picked["link"], "http://x/b.mp4")

    def test_smallest_height_at_or_above_target(self):
        files = [
            {"link": "http://x/huge.mp4", "width": 2160, "height": 3840,
             "file_type": "video/mp4"},
            {"link": "http://x/tall.mp4", "width": 1440, "height": 2560,
             "file_type": "video/mp4"},
            {"link": "http://x/exact.mp4", "width": 1080, "height": 1920,
             "file_type": "video/mp4"},
        ]
        picked = pexels.pick_video_file(files, target_w=1080, target_h=1920)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["link"], "http://x/exact.mp4")

    def test_tallest_when_none_reach_target(self):
        files = [
            {"link": "http://x/low.mp4", "width": 640, "height": 960,
             "file_type": "video/mp4"},
            {"link": "http://x/mid.mp4", "width": 1280, "height": 720,
             "file_type": "video/mp4"},
        ]
        picked = pexels.pick_video_file(files, target_w=1080, target_h=1920)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["link"], "http://x/low.mp4")

    def test_mocked_pexels_response_pick(self):
        # end-to-end pick from a mocked search response dict
        resp = {"videos": [
            {"id": 111, "user": {"name": "Alice"},
             "video_files": [
                 {"link": "http://x/111-low.mp4", "width": 640,
                  "height": 960, "file_type": "video/mp4"},
                 {"link": "http://x/111-hi.mp4", "width": 1080,
                  "height": 1920, "file_type": "video/mp4"}]},
            {"id": 222, "user": {"name": "Bob"},
             "video_files": [
                 {"link": "http://x/222.mp4", "width": 720,
                  "height": 1280, "file_type": "video/mp4"}]},
        ]}
        first = resp["videos"][0]
        picked = pexels.pick_video_file(first["video_files"],
                                        target_w=1080, target_h=1920)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["link"], "http://x/111-hi.mp4")
        self.assertEqual(pexels.parse_photographer(first), "Alice")
        self.assertEqual(pexels.cache_path("cache", 111).name, "111.mp4")


# ---------------------------------------------------------------------------
# Planner + ffmpeg arg lists
# ---------------------------------------------------------------------------

class TestPlanner(unittest.TestCase):
    def test_total_segments_clips_no_repeat(self):
        # Uniqueness caps segments at pool size: 40 clips for 25-40 segs.
        narration_dur = 82.5
        total = narration_dur + 0.4
        segs = story_visuals.plan(total, n_clips=40, seed=123)
        self.assertGreaterEqual(len(segs), 25)
        self.assertLessEqual(len(segs), 40)
        self.assertGreaterEqual(len(segs), 12 // 1)  # sanity
        self.assertAlmostEqual(sum(s["dur"] for s in segs), total, places=2)
        for seg in segs:
            self.assertLess(seg["clip_idx"], 40)
            self.assertGreater(seg["dur"], 0)
            self.assertGreaterEqual(seg["speed"], 2.0)
            self.assertLessEqual(seg["speed"], 3.0)
        # hard rule: no clip id twice in one video ...
        self.assertEqual(len({s["clip_idx"] for s in segs}), len(segs))
        # ... so no clip repeats within 4 segments either
        for i, seg in enumerate(segs):
            window = [s["clip_idx"] for s in segs[max(0, i - 4):i]]
            self.assertNotIn(seg["clip_idx"], window,
                             f"clip {seg['clip_idx']} repeats within 4 at {i}")

    def test_planner_empty_total(self):
        self.assertEqual(story_visuals.plan(0.0, n_clips=12), [])


class TestPlannerSpec(unittest.TestCase):
    def _clips(self, n=12, dur=30.0):
        return [{"id": f"vid{i}", "duration": dur} for i in range(n)]

    def test_exact_total_within_one_frame(self):
        # Pools sized for uniqueness (shortfall path would shrink counts).
        for total in (26.9, 82.9, 103.5):
            segs = story_visuals.plan(total, seed=7,
                                      clips=self._clips(40, 30.0))
            self.assertAlmostEqual(sum(s["dur"] for s in segs), total,
                                   places=2)
            # within 1 frame @30fps
            self.assertLessEqual(abs(sum(s["dur"] for s in segs) - total),
                                 1 / 30 + 1e-6)

    def test_out_duration_in_bounds(self):
        segs = story_visuals.plan(82.9, seed=7, clips=self._clips())
        for s in segs:
            self.assertGreaterEqual(s["dur"], 2.5)
            self.assertLessEqual(s["dur"], 4.5)
        # tiny totals: bounds win over the segment-count floor
        tiny = story_visuals.plan(10.0, seed=7, clips=self._clips(3, 30.0))
        self.assertTrue(tiny)
        for s in tiny:
            self.assertGreaterEqual(s["dur"], 2.5)
            self.assertLessEqual(s["dur"], 4.5)

    def test_no_repeat_within_four_and_window_math(self):
        clips = self._clips(12, 40.0)
        segs = story_visuals.plan(82.9, seed=11, clips=clips)
        by_clip = {c["id"]: c["duration"] for c in clips}
        for i, s in enumerate(segs):
            window = [x["clip_idx"] for x in segs[max(0, i - 4):i]]
            self.assertNotIn(s["clip_idx"], window)
            # window = out * speed, inside the source clip
            self.assertAlmostEqual(s["window"], s["dur"] * s["speed"],
                                   places=2)
            self.assertGreaterEqual(s["speed"], 2.0)
            self.assertLessEqual(s["speed"], 3.0)
            clip_dur = by_clip[s["clip_id"]]
            self.assertLessEqual(s["start_offset"] + s["window"],
                                 clip_dur + 1e-6)

    def test_no_overlapping_source_windows(self):
        clips = self._clips(4, 60.0)
        segs = story_visuals.plan(82.9, seed=5, clips=clips)
        used: dict = {}
        for s in segs:
            span = (s["start_offset"], s["start_offset"] + s["window"])
            for prev in used.setdefault(s["clip_idx"], []):
                overlap = min(span[1], prev[1]) - max(span[0], prev[0])
                self.assertLessEqual(overlap, 1e-6)
            used[s["clip_idx"]].append(span)

    def test_twelve_distinct_clips(self):
        segs = story_visuals.plan(82.9, seed=7, clips=self._clips(12, 40.0))
        distinct = {s["clip_idx"] for s in segs}
        self.assertGreaterEqual(len(distinct), 12)


class TestFfmpegArgs(unittest.TestCase):
    def test_ass_cwd_args_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            ass_path = Path(tmp) / "sub dir" / "captions.ass"
            ass_path.parent.mkdir(parents=True, exist_ok=True)
            ass_path.write_text("data", encoding="utf-8")
            args, cwd = ass_cwd_args(ass_path)
            self.assertIsInstance(args, list)
            self.assertEqual(args[0], "-vf")
            self.assertIn("ass=", args[1])
            # cwd is the resolved parent; filter uses the bare filename
            self.assertEqual(cwd, str(ass_path.resolve().parent))
            rel = ass_path.resolve().name
            self.assertIn(rel, args[1])
            # filter uses the bare filename: no drive colon, no slash in slot
            slot = args[1].split("fontsdir=")[0]
            self.assertTrue(slot.startswith("ass="))
            filename = slot[len("ass="):].rstrip(":")
            self.assertNotIn(":", filename)
            self.assertNotIn("/", filename)
            self.assertNotIn("\\", filename)

    def test_subtitles_and_seg_card_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            ass_path = Path(tmp) / "captions.ass"
            ass_path.write_text("data", encoding="utf-8")
            subs = subtitles_filter(ass_path.resolve())
            self.assertTrue(subs.startswith("subtitles='"))
            self.assertNotIn("force_style", subs)
            escaped = escape_filter_path(ass_path.resolve())
            self.assertIn(escaped, subs)
        seg_filter = story_visuals._seg_filter(1.2)
        self.assertIn("scale=1080:1920", seg_filter)
        self.assertIn("crop=1080:1920", seg_filter)
        self.assertIn("fps=30", seg_filter)
        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "card.png"
            card.write_bytes(b"fakepng")
            args = story_visuals._card_input_args(card, 4.0)
            self.assertEqual(
                args[:-1], ["-loop", "1", "-t", "4.00", "-i"])
            self.assertTrue(str(args[-1]).endswith("card.png"))
            filt = story_visuals._card_filter(4.0)
            self.assertEqual(
                filt,
                "[card]format=rgba,"
                "fade=t=in:st=0:d=0.2:alpha=1,"
                "fade=t=out:st=3.75:d=0.25:alpha=1[c];"
                "[base][c]overlay=x=(W-w)/2:y=H*0.30:"
                "enable='between(t,0,4.00)'[v1]")

    def test_transcode_and_concat_arg_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            clips = [workdir / "clip0.mp4", workdir / "clip1.mp4"]
            for clip in clips:
                clip.write_bytes(b"fake")
            segs = [
                {"clip_idx": 0, "start_offset": 1.0, "dur": 3.0, "speed": 1.2},
                {"clip_idx": 1, "start_offset": 0.5, "dur": 3.0, "speed": 1.3},
            ]
            captured = []

            def _fake_run(cmd, step="ffmpeg"):
                self.assertIsInstance(cmd, list)
                self.assertEqual(cmd[0], "ffmpeg")
                captured.append(cmd)
                Path(cmd[-1]).write_bytes(b"seg")

            with mock.patch(
                "niches.story.visuals.ffprobe_duration", return_value=60.0
            ), mock.patch(
                "niches.story.visuals.run_ffmpeg", side_effect=_fake_run
            ):
                outs = story_visuals._transcode_segments(clips, segs, workdir)
            self.assertEqual(len(outs), 2)
            self.assertTrue(all("-vf" in cmd for cmd in captured))
            # concat uses the concat demuxer with an arg list
            captured.clear()

            def _fake_concat(cmd, step="ffmpeg"):
                captured.append(cmd)
                Path(cmd[-1]).write_bytes(b"bg")

            seg_files = [workdir / "bg_000.mp4", workdir / "bg_001.mp4"]
            for p in seg_files:
                p.write_bytes(b"seg")
            with mock.patch(
                "niches.story.visuals.run_ffmpeg", side_effect=_fake_concat
            ):
                out = story_visuals._concat(seg_files, workdir)
            self.assertTrue(out.exists())
            self.assertIn("concat", captured[0])
            self.assertEqual(captured[0][0], "ffmpeg")


# ---------------------------------------------------------------------------
# edge_tts library client (mocked Communicate: no network)
# ---------------------------------------------------------------------------

class _FakeComm:
    """Stand-in for edge_tts.Communicate: canned chunks + call counting."""
    calls = []
    mode = "ok"  # ok | empty | boom
    events = []
    audio = b"A" * 1000

    def __init__(self, text, voice, **kw):
        _FakeComm.calls.append((text, voice, kw))

    async def stream(self):
        if _FakeComm.mode == "boom":
            raise TypeError("boom")
        if _FakeComm.mode != "empty":
            yield {"type": "audio", "data": _FakeComm.audio}
        for ev in _FakeComm.events:
            yield {"type": "WordBoundary", **ev}


class TestEdgeClient(unittest.TestCase):
    def setUp(self):
        _FakeComm.calls = []
        _FakeComm.mode = "ok"
        _FakeComm.events = []
        _FakeComm.audio = b"A" * 1000

    def _events(self, words, start=0.1, step=0.25, span=0.2):
        return [{"text": w, "offset": int((start + i * step) * 10_000_000),
                 "duration": int(span * 10_000_000)}
                for i, w in enumerate(words)]

    def test_synth_tick_math(self):
        from engine import edge_tts_client as ec

        _FakeComm.events = self._events(["Hello", "world"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.mp3"
            with mock.patch("edge_tts.Communicate", _FakeComm):
                words = ec.synth_sync("Hello world", "v", "+20%", out,
                                      waits=(0, 0, 0))
            self.assertEqual(out.read_bytes(), b"A" * 1000)
            self.assertEqual([w["word"] for w in words], ["Hello", "world"])
            # 100-ns ticks -> seconds
            self.assertAlmostEqual(words[0]["start"], 0.1)
            self.assertAlmostEqual(words[0]["end"], 0.3)
            self.assertAlmostEqual(words[1]["start"], 0.35)

    def test_synth_event_count_warn(self):
        from engine import edge_tts_client as ec

        _FakeComm.events = self._events(["a", "b"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.mp3"
            with mock.patch("edge_tts.Communicate", _FakeComm), \
                mock.patch("engine.edge_tts_client.warn") as warn_mock:
                words = ec.synth_sync("one two three four five", "v", "+20%",
                                      out, waits=(0, 0, 0))
            warn_mock.assert_called_once()
            self.assertIn("2", warn_mock.call_args[0][0])
            self.assertEqual([w["word"] for w in words], ["a", "b"])

    def test_synth_empty_audio_retried(self):
        from engine import edge_tts_client as ec

        _FakeComm.mode = "empty"
        _FakeComm.events = self._events(["a"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.mp3"
            with mock.patch("edge_tts.Communicate", _FakeComm):
                with self.assertRaises(ValueError):
                    ec.synth_sync("hello", "v", "+20%", out, waits=(0, 0, 0))
        self.assertEqual(len(_FakeComm.calls), 4)

    def test_synth_typeerror_not_retried(self):
        from engine import edge_tts_client as ec

        _FakeComm.mode = "boom"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.mp3"
            with mock.patch("edge_tts.Communicate", _FakeComm):
                with self.assertRaises(TypeError):
                    ec.synth_sync("hello", "v", "+20%", out, waits=(0, 0, 0))
        self.assertEqual(len(_FakeComm.calls), 1)


# ---------------------------------------------------------------------------
# Edge-tts preview branch (library client, mocked: no network)
# ---------------------------------------------------------------------------

class TestStoryEdgeBranch(unittest.TestCase):
    def _edge_cfg(self, **kw):
        cfg = StoryConfig(
            tts_engine="edge", edge_voice="en-US-AvaNeural",
            edge_voices=("en-US-AndrewNeural",),
            edge_rate="+20%", narration_speed=1.15,
            max_seconds=90.0, target_min=80.0, target_max=85.0,
            tries=1, backoff=1)
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def _events(self, words, start=0.0, step=0.25, span=0.2):
        return [{"word": w, "start": round(start + i * step, 3),
                 "end": round(start + i * step + span, 3)}
                for i, w in enumerate(words)]

    def _run_branch(self, text, events, dur, cfg=None, durs=None):
        """Run _narrate_edge fully mocked; return (result, synth_mock, cmds)."""
        cfg = cfg or self._edge_cfg()
        content = self._content(text=text)
        cmds = []

        def _fake_run(cmd, step="ffmpeg"):
            cmds.append((cmd, step))

        ff = durs if durs is not None else dur
        with mock.patch("engine.edge_tts_client.synth_sync",
                        return_value=[dict(e) for e in events]) as synth, \
            mock.patch("niches.story.narration.run_ffmpeg",
                       side_effect=_fake_run), \
            mock.patch("niches.story.narration.ffprobe_duration",
                       side_effect=(lambda p: ff) if not isinstance(ff, list)
                       else ff):
            with tempfile.TemporaryDirectory() as tmp:
                res = story_narr._narrate_edge(content, cfg, Path(tmp))
        return res, synth, cmds

    def _content(self, text="Hello world. How are you today?"):
        return Content(
            title="Edge preview", narration_text=text, description="d",
            tags=["preview"],
            extras={"genre": "workplace", "card_text": "Hello world",
                    "card_highlight_word": "world", "hook": "Hello world.",
                    "bg_queries": [], "key_words": []})

    def test_config_edge_defaults_and_env(self):
        cfg = StoryConfig()
        self.assertEqual(cfg.tts_engine, "edge")
        self.assertEqual(cfg.tts_provider_order, "edge,gemini")
        self.assertEqual(cfg.edge_voice, "en-US-AvaNeural")
        self.assertEqual(cfg.edge_voices,
                         ("en-US-AndrewNeural", "en-US-AriaNeural",
                          "en-US-GuyNeural"))
        self.assertEqual(cfg.edge_rate, "+20%")
        self.assertEqual(cfg.target_min, 100.0)
        self.assertEqual(cfg.target_max, 120.0)
        self.assertEqual(cfg.target_min_seconds, 100.0)
        self.assertEqual(cfg.target_max_seconds, 120.0)
        self.assertEqual(cfg.max_seconds, 130.0)
        env = {"STORY_TTS_ENGINE": "edge", "STORY_EDGE_VOICE": "en-US-GuyNeural",
               "STORY_EDGE_RATE": "+10%", "TTS_ENGINE": "gemini",
               "STORY_TARGET_MIN_SECONDS": "81",
               "STORY_TARGET_MIN": "70"}
        with mock.patch.dict(os.environ, env, clear=False):
            for k in ("STORY_EDGE_VOICES",):
                os.environ.pop(k, None)
            cfg2 = StoryConfig.from_env()
        # STORY_ prefix wins over the bare fallback
        self.assertEqual(cfg2.tts_engine, "edge")
        self.assertEqual(cfg2.edge_voice, "en-US-GuyNeural")
        self.assertEqual(cfg2.edge_rate, "+10%")
        # TARGET_MIN_SECONDS alias wins over TARGET_MIN
        self.assertEqual(cfg2.target_min_seconds, 81.0)
        self.assertEqual(cfg2.target_min, 81.0)
        # STORY_EDGE_VOICES (plural) wins over the singular fallback
        env["STORY_EDGE_VOICES"] = "en-US-AriaNeural,en-US-JennyNeural"
        with mock.patch.dict(os.environ, env, clear=False):
            cfg3 = StoryConfig.from_env()
        self.assertEqual(cfg3.edge_voices,
                         ("en-US-AriaNeural", "en-US-JennyNeural"))

    def test_edge_voice_pool_precedence(self):
        # unset -> default trio; singular-only -> one-voice pool
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("STORY_EDGE_VOICES", "STORY_EDGE_VOICE",
                      "EDGE_VOICES", "EDGE_VOICE"):
                os.environ.pop(k, None)
            cfg = StoryConfig.from_env()
        self.assertEqual(len(cfg.edge_voices), 3)
        with mock.patch.dict(os.environ, {"STORY_EDGE_VOICE": "en-US-GuyNeural"},
                             clear=False):
            os.environ.pop("STORY_EDGE_VOICES", None)
            cfg2 = StoryConfig.from_env()
        self.assertEqual(cfg2.edge_voices, ("en-US-GuyNeural",))
        voice, _why = story_narr._edge_voice_for(cfg, "seed")
        self.assertIn(voice, list(cfg.edge_voices))

    def test_parse_and_refit_rate(self):
        self.assertAlmostEqual(story_narr._parse_edge_rate("+20%"), 0.20)
        self.assertAlmostEqual(story_narr._parse_edge_rate("+0%"), 0.0)
        self.assertAlmostEqual(story_narr._parse_edge_rate("bogus"), 0.0)
        # 105s @ +20% re-aims at the 110s midpoint
        self.assertEqual(story_narr._edge_refit_rate("+20%", 105.0), "+15%")
        # clamp +0..+40%
        self.assertEqual(story_narr._edge_refit_rate("+20%", 200.0), "+40%")
        self.assertEqual(story_narr._edge_refit_rate("+20%", 10.0), "+0%")

    def test_no_atempo_silenceremove_in_edge_path(self):
        # Library path: plain mp3->wav convert only, timings stay sample-true.
        ticks = "Hello world. How are you today?".split()
        res, synth, cmds = self._run_branch(
            "Hello world. How are you today?",
            self._events(ticks), 82.0)
        self.assertEqual(synth.call_count, 1)
        flat = " | ".join(" ".join(c) for c, _ in cmds)
        self.assertNotIn("atempo", flat)
        self.assertNotIn("silenceremove", flat)
        self.assertNotIn("loudnorm", flat)
        self.assertIn("pcm_s16le", flat)
        self.assertEqual(res.extras["tts_engine"], "edge")
        self.assertEqual(res.extras["chunks"], 1)
        self.assertIsNone(res.extras["fit_action"])
        self.assertIn("in 80-85 window", res.extras["fit"])

    def test_word_mapping_match_keeps_narration_text(self):
        # counts match: narration spelling on event times
        events = [{"word": "hello", "start": 0.5, "end": 0.9},
                  {"word": "WORLD", "start": 0.9, "end": 1.4}]
        words = story_narr._map_edge_words(events, "Hello, WORLD.")
        self.assertEqual([w["word"] for w in words], ["Hello,", "WORLD."])
        self.assertAlmostEqual(words[0]["start"], 0.5)
        self.assertAlmostEqual(words[1]["end"], 1.4)

    def test_word_mapping_differ_uses_event_text(self):
        events = [{"word": "edge", "start": 0.0, "end": 0.5}]
        words = story_narr._map_edge_words(events, "one two three four")
        self.assertEqual([w["word"] for w in words], ["edge"])

    def test_edge_sanity_violations(self):
        ok = self._events(["a", "b", "c"], start=0.0, step=1.0, span=0.8)
        self.assertIsNone(story_narr._check_edge_sanity(ok, 10.0))
        bad_end = [dict(ok[0], end=ok[0]["start"] - 0.1)] + ok[1:]
        self.assertIn("end<start",
                      story_narr._check_edge_sanity(bad_end, 10.0) or "")
        bad_order = [ok[0], dict(ok[1], start=ok[0]["start"] - 0.5,
                                 end=ok[0]["start"] - 0.1)] + ok[2:]
        self.assertIn("non-decreasing",
                      story_narr._check_edge_sanity(bad_order, 10.0) or "")
        bad_last = ok[:-1] + [dict(ok[-1], end=99.0)]
        self.assertIn("beyond duration",
                      story_narr._check_edge_sanity(bad_last, 10.0) or "")

    def test_fitting_slight_resynth_and_clamp(self):
        # 78s @ +20% re-aimed at mid-110 clamps to the +0% floor,
        # then the mocked 82s lands in the (custom 80-85) window.
        text = " ".join(f"word{i}" for i in range(100))
        events = self._events(text.split(), step=0.7, span=0.6)
        cfg = self._edge_cfg()
        content = self._content(text=text)
        cmds = []
        with mock.patch("engine.edge_tts_client.synth_sync",
                        return_value=[dict(e) for e in events]) as synth, \
            mock.patch("niches.story.narration.run_ffmpeg",
                       side_effect=lambda c, step="f": cmds.append(c)), \
            mock.patch("niches.story.narration.ffprobe_duration",
                       side_effect=[78.0, 82.0]):
            with tempfile.TemporaryDirectory() as tmp:
                res = story_narr._narrate_edge(content, cfg, Path(tmp))
        self.assertEqual(synth.call_count, 2)
        self.assertEqual(synth.call_args_list[1][0][2], "+0%")
        self.assertEqual(res.duration, 82.0)
        self.assertIsNone(res.extras["fit_action"])
        self.assertIn("re-synth 82.0s", res.extras["fit"])

    def test_fitting_far_off_flags_gemini_text(self):
        # 60s: lengthen action, single synth, no re-synth loop.
        text = " ".join(f"word{i}" for i in range(100))
        events = self._events(text.split(), step=0.5, span=0.4)
        res, synth, _cmds = self._run_branch(text, events, 60.0)
        self.assertEqual(synth.call_count, 1)
        self.assertIn("gemini-text-lengthen", res.extras["fit_action"] or "")
        # far-off high side (MAX raised so no trim interferes)
        cfg = self._edge_cfg(max_seconds=200.0)
        res2, _s2, _c2 = self._run_branch(text, events, 100.0, cfg=cfg)
        self.assertIn("gemini-text-tighten", res2.extras["fit_action"] or "")

    def test_fitting_trim_over_max(self):
        # 95s past MAX_SECONDS=90: sentence trim + warn, words filtered.
        text = "Hello world. " + " ".join(f"word{i}" for i in range(98))
        events = self._events(text.split(), step=0.9, span=0.8)
        cfg = self._edge_cfg()
        content = self._content(text=text)
        with mock.patch("engine.edge_tts_client.synth_sync",
                        return_value=[dict(e) for e in events]), \
            mock.patch("niches.story.narration.run_ffmpeg"), \
            mock.patch("niches.story.narration.ffprobe_duration",
                       side_effect=[95.0, 95.0]):
            with tempfile.TemporaryDirectory() as tmp:
                res = story_narr._narrate_edge(content, cfg, Path(tmp))
        self.assertIn("trimmed", res.extras["fit"])
        for w in res.extras["words"]:
            self.assertLess(float(w["start"]), 95.0)

    def test_trim_below_target_min_raises_config_error(self):
        # MAX_SECONDS=45 against an 80-85s target is a config
        # contradiction, not a narration problem: fail loud naming
        # STORY_MAX_SECONDS instead of trimming into a wps failure.
        text = "Hello world. " + " ".join(f"word{i}" for i in range(98))
        events = self._events(text.split(), step=0.9, span=0.8)
        cfg = self._edge_cfg(max_seconds=45.0)
        content = self._content(text=text)
        with mock.patch("engine.edge_tts_client.synth_sync",
                        return_value=[dict(e) for e in events]), \
            mock.patch("niches.story.narration.run_ffmpeg"), \
            mock.patch("niches.story.narration.ffprobe_duration",
                        return_value=101.0):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, "STORY_MAX_SECONDS"):
                    story_narr._narrate_edge(content, cfg, Path(tmp))

    def test_edge_chunk_fallback_offsets_timings(self):
        # Whole-script call fails -> per-sentence synths, same voice+rate.
        # Text opens with the card line (no prepend normalization).
        text = "Hello world. Delta epsilon zeta. Eta theta iota."
        sents = story_narr._split_sentences(text)
        self.assertEqual(len(sents), 3)
        per = [[{"word": w, "start": round(i * 0.3, 3),
                 "end": round(i * 0.3 + 0.25, 3)}
                for i, w in enumerate(s.split())] for s in sents]

        def _fake_synth(t, voice, rate, out, **kw):
            self.assertEqual(rate, "+20%")
            if t == text:
                raise ConnectionError("ws down")
            idx = sents.index(t)
            Path(out).write_bytes(b"fake")
            return [dict(e) for e in per[idx]]

        cfg = self._edge_cfg()
        content = self._content(text=text)
        durs = {"narr_edge_c00": 10.0, "narr_edge_c01": 10.0,
                "narr_edge_c02": 10.0}

        def _fake_dur(p):
            name = Path(p).stem
            return durs.get(name, 30.0)

        with mock.patch("engine.edge_tts_client.synth_sync",
                        side_effect=_fake_synth) as synth, \
            mock.patch("niches.story.narration.run_ffmpeg"), \
            mock.patch("niches.story.narration.ffprobe_duration",
                       side_effect=_fake_dur):
            with tempfile.TemporaryDirectory() as tmp:
                res = story_narr._narrate_edge(content, cfg, Path(tmp))
        self.assertEqual(res.extras["chunks"], 3)
        self.assertEqual(len(res.extras["words"]), 8)
        # second sentence starts at the first chunk's duration
        self.assertAlmostEqual(res.extras["words"][2]["start"], 10.0)
        voices = {c[0][1] for c in synth.call_args_list}
        self.assertEqual(len(voices), 1)  # same voice every chunk

    def test_postprocess_has_no_stop_trim(self):
        # Regression: stop_periods/stop_duration collapsed 26s of -21 dB
        # speech to 5.2s on ffmpeg 9.0.1; the areverse idiom is exact.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp3"
            src.write_bytes(b"fake")
            out = Path(tmp) / "out.mp3"
            calls = []

            def _fake_run(cmd, step="ffmpeg"):
                calls.append((cmd, step))

            with mock.patch("niches.story.narration.run_ffmpeg",
                           side_effect=_fake_run):
                story_narr._postprocess(src, out)
            self.assertEqual(len(calls), 1)
            cmd = calls[0][0]
            self.assertEqual(cmd[0], "ffmpeg")
            af = cmd[cmd.index("-af") + 1]
            self.assertNotIn("stop_periods", af)
            self.assertNotIn("stop_duration", af)
            self.assertIn("silenceremove", af)
            self.assertIn("areverse", af)
            self.assertIn("loudnorm", af)

    def test_edge_wps_guard_rejects_absurd_rate(self):
        # 100 words in 4s = 25 wps (the 126-words-in-5.2s anomaly) must fail.
        text = " ".join(f"word{i}" for i in range(100))
        events = self._events(text.split(), step=0.04, span=0.03)
        cfg = self._edge_cfg()
        content = self._content(text=text)
        with mock.patch("engine.edge_tts_client.synth_sync",
                        return_value=[dict(e) for e in events]), \
            mock.patch("niches.story.narration.run_ffmpeg"), \
            mock.patch("niches.story.narration.ffprobe_duration",
                       return_value=4.0):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError) as ctx:
                    story_narr._narrate_edge(content, cfg, Path(tmp))
            self.assertIn("wps", str(ctx.exception))

    def test_edge_wps_guard_accepts_realistic_rate(self):
        # 100 words in 25s = 4 wps must pass (20-35s deliverable band).
        text = " ".join(f"word{i}" for i in range(100))
        events = self._events(text.split(), step=0.25, span=0.2)
        res, _synth, _cmds = self._run_branch(text, events, 25.0)
        self.assertGreaterEqual(res.duration, 20.0)
        self.assertLessEqual(res.duration, 35.0)
        wps = res.extras["wps_measured"]
        self.assertGreaterEqual(wps, 2.5)
        self.assertLessEqual(wps, 5.0)

    def test_fallback_off_by_default(self):
        # Explicit edge-only order: total edge failure propagates, gemini untouched.
        cfg = self._edge_cfg(tts_provider_order="edge")
        content = self._content(text="Only one sentence here.")
        content.extras["card_text"] = ""  # no card: no prepend normalization
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("engine.edge_tts_client.synth_sync",
                           side_effect=ConnectionError("down")), \
                mock.patch("niches.story.narration._gc.GeminiClient",
                           side_effect=AssertionError("gemini touched")):
                # single sentence: chunk fallback also refuses, error out
                with self.assertRaises(ValueError):
                    story_narr._narrate_edge(content, cfg, Path(tmp))

    def test_fallback_enabled_uses_gemini(self):
        # "edge,gemini": whole-script gemini path + provider flag + log line.
        from niches.base import NarrationResult

        cfg = self._edge_cfg(tts_provider_order="edge,gemini")
        content = self._content(text="Only one sentence here.")
        fake = NarrationResult(
            audio=Path("g.mp3"), duration=30.0, words=5, voice="Charon",
            style="s", extras={"words": [], "tts_engine": "gemini"})
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("engine.edge_tts_client.synth_sync",
                           side_effect=ConnectionError("down")), \
                mock.patch("niches.story.narration.narrate",
                           return_value=fake) as narr_mock, \
                mock.patch("niches.story.narration.log") as log_mock:
                res = story_narr._narrate_edge(content, cfg, Path(tmp))
            narr_mock.assert_called_once()
            self.assertEqual(res.extras["tts_provider"], "gemini")
            self.assertEqual(res.extras["tts_engine"], "gemini")
            logged = " ".join(c[0][0] for c in log_mock.call_args_list)
            self.assertIn("estimated timings, captions may drift", logged)

    def test_narrate_dispatches_edge(self):
        cfg = self._edge_cfg()
        content = self._content()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("niches.story.narration._narrate_edge",
                           return_value="EDGE-RES") as branch, \
                mock.patch("niches.story.narration._gc.GeminiClient",
                           side_effect=AssertionError("gemini touched")):
                out = story_narr.narrate(content, cfg, Path(tmp))
            self.assertEqual(out, "EDGE-RES")
            branch.assert_called_once()

    def test_narrate_gemini_path_unchanged(self):
        # explicit gemini takes the gemini path
        content = self._content()
        cfg = self._edge_cfg(tts_engine="gemini")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "niches.story.narration._narrate_edge",
                side_effect=AssertionError("edge touched")), \
                mock.patch(
                    "niches.story.narration._gc.GeminiClient",
                    side_effect=RuntimeError("no keys")) as cli:
                with self.assertRaises(RuntimeError):
                    story_narr.narrate(content, cfg, Path(tmp))
                cli.assert_called_once()

    def test_narrate_unknown_engine_falls_back_to_edge(self):
        # unknown + empty engines fall back to the edge default
        content = self._content()
        for eng in ("bogus", ""):
            with self.subTest(engine=eng):
                cfg = self._edge_cfg(tts_engine=eng)
                with tempfile.TemporaryDirectory() as tmp:
                    with mock.patch(
                        "niches.story.narration._narrate_edge",
                        return_value="EDGE-RES") as branch, \
                        mock.patch(
                            "niches.story.narration._gc.GeminiClient",
                            side_effect=AssertionError("gemini touched")):
                        out = story_narr.narrate(content, cfg, Path(tmp))
                    self.assertEqual(out, "EDGE-RES")
                    branch.assert_called_once()


# ---------------------------------------------------------------------------
# Demo mux mapping + audible-audio guard (mocked ffmpeg: no binaries)
# ---------------------------------------------------------------------------

class TestDemoMuxMapping(unittest.TestCase):
    def test_story_mux_maps_narration_audio(self):
        import demo as demo_mod

        content = Content(
            title="Mux check", narration_text="Hello world today",
            description="d", tags=["t"],
            extras={"card_text": "Hello world",
                    "card_highlight_word": "world", "hook": "Hello world",
                    "bg_queries": [], "key_words": []})
        ticks = "Hello world today".split()
        words = [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.3}
                 for i, w in enumerate(ticks)]
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            clip = wd / "bg0.mp4"
            clip.write_bytes(b"fake")
            audio = wd / "narr.mp3"
            audio.write_bytes(b"fake")
            out = wd / "demo.mp4"
            cmds = []

            def _fake_run(cmd, **kw):
                cmds.append(cmd)
                m = mock.Mock()
                m.returncode = 0
                return m

            with mock.patch("demo.subprocess.run", side_effect=_fake_run):
                demo_mod._render_story_captioned(
                    [clip], audio, words, content, out, wd)
            final = [c for c in cmds if "-filter_complex" in c]
            self.assertEqual(len(final), 1)
            cmd = final[0]
            self.assertEqual(cmd[0], "ffmpeg")
            # narration track mapped and primary; never dropped
            maps = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-map"]
            self.assertIn("[vout]", maps)
            self.assertIn("1:a", maps)
            self.assertNotIn("-an", cmd)
            # voice encoded as AAC
            self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")


class TestDemoAudioGuard(unittest.TestCase):
    def _run_mock(self, ffprobe_out, ffmpeg_err):
        def _fake(cmd, **kw):
            m = mock.Mock()
            m.returncode = 0
            if cmd[0] == "ffprobe":
                m.stdout = ffprobe_out
            else:
                m.stderr = ffmpeg_err
            return m
        return _fake

    def _streams_json(self, n_audio=1):
        streams = [{"index": 0, "codec_name": "h264",
                    "codec_type": "video"}]
        streams += [{"index": i + 1, "codec_name": "aac",
                     "codec_type": "audio"} for i in range(n_audio)]
        return json.dumps({"streams": streams})

    def test_guard_passes_on_loud_audio(self):
        import demo as demo_mod

        err = ("[Parsed_volumedetect_0 @ 0x0] mean_volume: -16.3 dB\n"
               "[Parsed_volumedetect_0 @ 0x0] max_volume: -1.9 dB\n")
        with mock.patch("demo.subprocess.run",
                        side_effect=self._run_mock(
                            self._streams_json(1), err)):
            n, db = demo_mod._assert_audible_audio(Path("x.mp4"))
        self.assertEqual(n, 1)
        self.assertAlmostEqual(db, -16.3)

    def test_guard_fails_without_audio_stream(self):
        import demo as demo_mod

        with mock.patch("demo.subprocess.run",
                        side_effect=self._run_mock(
                            self._streams_json(0), "")):
            with self.assertRaises(RuntimeError):
                demo_mod._assert_audible_audio(Path("x.mp4"))

    def test_guard_fails_on_silence(self):
        import demo as demo_mod

        err = "[Parsed_volumedetect_0 @ 0x0] mean_volume: -inf dB\n"
        with mock.patch("demo.subprocess.run",
                        side_effect=self._run_mock(
                            self._streams_json(1), err)):
            with self.assertRaises(RuntimeError):
                demo_mod._assert_audible_audio(Path("x.mp4"))


class TestSegCmdSpec(unittest.TestCase):
    def test_seg_filter_exact_order(self):
        self.assertEqual(
            story_visuals._seg_filter(1.2),
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setpts=PTS/1.200,fps=30,format=yuv420p")

    def test_seg_cmd_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            clip = workdir / "clip.mp4"
            clip.write_bytes(b"fake")
            segs = [{"clip_idx": 0, "start_offset": 1.0, "dur": 3.0,
                     "speed": 1.2}]
            cmds = []

            def _fake_run(cmd, step="ffmpeg", cwd=None):
                cmds.append(cmd)
                Path(cmd[-1]).write_bytes(b"seg")

            with mock.patch("niches.story.visuals.ffprobe_duration",
                           return_value=60.0), \
                mock.patch("niches.story.visuals.run_ffmpeg",
                           side_effect=_fake_run):
                story_visuals._transcode_segments([clip], segs, workdir)
            self.assertEqual(len(cmds), 1)
            cmd = cmds[0]
            self.assertEqual(cmd[0], "ffmpeg")
            self.assertIn("-an", cmd)
            self.assertEqual(cmd[cmd.index("-crf") + 1], "20")
            self.assertEqual(cmd[cmd.index("-preset") + 1], "veryfast")
            self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")


class TestMixFilterSpec(unittest.TestCase):
    def test_mix_chain_exact(self):
        self.assertEqual(
            story_visuals._mix_filter(82.9),
            "[2:a]volume=0.12[b];"
            "[b][1:a]sidechaincompress=threshold=0.05:ratio=8:"
            "attack=20:release=300[duck];"
            "[1:a][duck]amix=inputs=2:duration=first:normalize=0,"
            "afade=t=out:st=80.90:d=2[aout]")


class TestLibassGuard(unittest.TestCase):
    def test_require_ass_passes_when_present(self):
        from engine import ffmpeg_utils as fu

        m = mock.Mock()
        m.stdout = ("Filters:\n"
                    "  ... ass                  V->V       Render ASS.\n")
        with mock.patch("subprocess.run", return_value=m):
            fu.require_ass_filter()  # must not raise

    def test_require_ass_raises_when_missing(self):
        from engine import ffmpeg_utils as fu

        m = mock.Mock()
        m.stdout = "Filters:\n  ... scale V->V Scale.\n"
        with mock.patch("subprocess.run", return_value=m):
            with self.assertRaises(RuntimeError) as ctx:
                fu.require_ass_filter()
        self.assertIn("libass", str(ctx.exception))

    def test_run_ffmpeg_forwards_cwd(self):
        from engine import ffmpeg_utils as fu

        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0)
            fu.run_ffmpeg(["ffmpeg", "-version"], "probe", cwd="/tmp/xyz")
        self.assertEqual(run_mock.call_args[1].get("cwd"), "/tmp/xyz")


class TestCardFirst(unittest.TestCase):
    def test_prompt_requires_card_first(self):
        prompt, _slots = story_content.build_prompt([], StoryConfig())
        self.assertIn("must open with the card line verbatim", prompt)
        self.assertIn("hook: the card line verbatim", prompt)

    def test_validation_accepts_card_first(self):
        extra = story_content.validate_extra(_make_story())
        self.assertEqual(extra["hook"], "I said no then they moved my desk.")

    def test_validation_rejects_non_card_first(self):
        data = _make_story()
        rest = data["story_text"].split(". ", 1)[1]
        data["story_text"] = rest
        data["narration"] = rest
        data["beats"][0]["text"] = data["beats"][0]["text"].split(". ", 1)[1]
        data["hook"] = rest.split(". ", 1)[0] + "."
        with self.assertRaisesRegex(ValueError, "card line"):
            story_content.validate_extra(data)

    def test_prompt_requires_third_rehook_and_hook_examples(self):
        prompt, _slots = story_content.build_prompt([], StoryConfig())
        self.assertIn("80-90s", prompt)
        self.assertIn("THIRD re-hook", prompt)
        self.assertIn("never copy these lines", prompt)
        self.assertIn("exactly 3 objects", prompt)

    def test_validation_accepts_exactly_three_rehooks(self):
        extra = story_content.validate_extra(_make_story())
        self.assertEqual(len(extra["rehooks"]), 3)
        self.assertEqual([r["at"] for r in extra["rehooks"]],
                         ["20-30s", "50-60s", "80-90s"])

    def test_validation_rejects_bad_rehook_sets(self):
        base = _make_story()
        # only 2 slots
        two = dict(base, rehooks=base["rehooks"][:2])
        with self.assertRaisesRegex(ValueError, "exactly 3"):
            story_content.validate_extra(two)
        # 4 entries
        four = dict(base, rehooks=base["rehooks"] + [dict(base["rehooks"][0])])
        with self.assertRaisesRegex(ValueError, "exactly 3"):
            story_content.validate_extra(four)
        # wrong `at` value
        bad_at = dict(base, rehooks=[dict(r) for r in base["rehooks"]])
        bad_at["rehooks"][2]["at"] = "30-40s"
        with self.assertRaisesRegex(ValueError, "20-30s.*50-60s.*80-90s"):
            story_content.validate_extra(bad_at)
        # duplicated slot (missing 80-90s)
        dup = dict(base, rehooks=[dict(r) for r in base["rehooks"]])
        dup["rehooks"][2]["at"] = "20-30s"
        with self.assertRaisesRegex(ValueError, "each slot once"):
            story_content.validate_extra(dup)
        # line not verbatim in story
        bad_line = dict(base, rehooks=[dict(r) for r in base["rehooks"]])
        bad_line["rehooks"][2]["line"] = "Never aired anywhere before."
        with self.assertRaisesRegex(ValueError, "line from the story"):
            story_content.validate_extra(bad_line)

    def _recard(self, data, card):
        """Swap the card line everywhere gates look (story/beats/hook)."""
        old_first, rest = data["story_text"].split(". ", 1)
        _ = old_first
        data["story_text"] = f"{card}. {rest}"
        data["narration"] = data["story_text"]
        data["beats"][0]["text"] = f"{card}. " + data["beats"][0]["text"].split(". ", 1)[1]
        data["hook"] = f"{card}."
        data["card_text"] = card
        return data

    def test_card_text_word_cap(self):
        ok = self._recard(_make_story(),
                          "one two three four five six seven eight")
        extra = story_content.validate_extra(ok)
        self.assertEqual(extra["card_text"], ok["card_text"])
        bad = self._recard(_make_story(),
                           "one two three four five six seven eight nine")
        with self.assertRaisesRegex(ValueError, "<=8 words"):
            story_content.validate_extra(bad)

    def test_fallback_cards_within_cap(self):
        for variant in story_content._FALLBACK_VARIANTS:
            n = len(variant["card_text"].split())
            self.assertLessEqual(n, 8, variant["title"])
            self.assertIn(variant["card_highlight_word"].lower(),
                          variant["card_text"].lower())

    def test_fallback_and_sample_three_rehooks(self):
        for hist in ([], [{"niche": "story", "genre": "workplace"}]):
            meta, _slots = story_content.fallback_script(hist)
            self.assertEqual(len(meta["rehooks"]), 3)
            self.assertEqual([r["at"] for r in meta["rehooks"]],
                             ["20-30s", "50-60s", "80-90s"])
            for r in meta["rehooks"]:
                self.assertIn(r["line"], meta["story_text"])
            validated = story_content.validate_extra(meta)
            self.assertEqual(len(validated["rehooks"]), 3)
        # every bundled variant is validation-clean (incl. card-first)
        for variant in story_content._FALLBACK_VARIANTS:
            meta, _slots = story_content._build_fallback_meta(variant)
            validated = story_content.validate_extra(meta)
            self.assertEqual(len(validated["rehooks"]), 3)
            self.assertTrue(story_content._starts_with_card(
                meta["story_text"], meta["card_text"]))
        sample = story_content.sample_content()
        self.assertEqual(len(sample.extras["rehooks"]), 3)

    def test_fallback_and_sample_card_first(self):
        for hist in ([], [{"niche": "story", "genre": "workplace"}]):
            meta, _slots = story_content.fallback_script(hist)
            self.assertTrue(
                story_content._starts_with_card(
                    meta["story_text"], meta["card_text"]),
                f"fallback opens with card: {meta['title']}")
            self.assertTrue(
                meta["story_text"].startswith(meta["hook"][:20]))
        sample = story_content.sample_content()
        card = sample.extras["card_text"]
        self.assertTrue(
            story_narr._starts_with_card(sample.narration_text, card))
        self.assertTrue(sample.narration_text.startswith(
            sample.extras["hook"][:20]))

    def test_prepend_normalization(self):
        legacy = "My boss asked me to work Saturday."
        fixed, changed = story_narr._ensure_card_first(
            legacy, "I said no then they moved my desk")
        self.assertTrue(changed)
        self.assertTrue(fixed.startswith(
            "I said no then they moved my desk. My boss"))
        # idempotent: no double prepend when already present
        same, changed2 = story_narr._ensure_card_first(
            fixed, "I said no then they moved my desk")
        self.assertFalse(changed2)
        self.assertEqual(same, fixed)
        # empty card text: no-op
        same3, changed3 = story_narr._ensure_card_first(legacy, "")
        self.assertFalse(changed3)
        self.assertEqual(same3, legacy)

    def test_card_end_anchored_to_head(self):
        timed = [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.5)]
        vocab = ["hello", "world", "mystery", "phrase", "here"]
        words = [{"word": w, "start": s, "end": e}
                 for w, (s, e) in zip(vocab, timed)]
        # head match wins
        self.assertAlmostEqual(
            story_narr.card_end_time(words, "hello world", ""), 1.0)
        # mid-list repeat must NOT match: falls back to first-words timing
        self.assertAlmostEqual(
            story_narr.card_end_time(words, "mystery phrase", ""), 2.5)


class TestEvergreenPool(unittest.TestCase):
    def test_cleaning_queries_present_and_conventional(self):
        pool = list(story_visuals.EVERGREEN_QUERIES)
        for q in ("window cleaning", "pressure washing", "car wash",
                  "floor mopping", "soap sponge", "vacuum carpet"):
            self.assertIn(q, pool)
        # bg_queries convention: 1-3 concrete filmable words each
        for q in pool:
            self.assertGreaterEqual(len(q.split()), 1)
            self.assertLessEqual(len(q.split()), 3)
        # no duplicates (gather_clips dedups anyway; keep the pool clean)
        self.assertEqual(len(pool), len(set(pool)))

    def test_gather_query_assembly_includes_cleaning(self):
        # Offline: keyless gather fails on clips, but the assembled query
        # list (the part under test) must carry the cleaning pool.
        seen = {}

        def _fake_fetch_many(query, api_key, cache_dir, used, limit=2):
            seen[query] = True
            return []

        with tempfile.TemporaryDirectory() as tmp:
            cfg = StoryConfig(pexels_key="fake-key-for-assembly",
                              bg_dir="no-such-dir",
                              pexels_cache_dir=str(Path(tmp) / "cache"))
            with mock.patch("niches.story.visuals._fetch_many",
                            side_effect=_fake_fetch_many):
                with self.assertRaises(RuntimeError):
                    story_visuals.gather_clips(["office desk night"],
                                               "workplace", cfg)
        for q in ("window cleaning", "pressure washing", "car wash"):
            self.assertIn(q, seen)


class TestClipUniqueness(unittest.TestCase):
    def _pool(self, n=40, dur=30.0):
        return [{"id": f"vid{i}", "duration": dur} for i in range(n)]

    def test_full_120s_plan_all_ids_distinct(self):
        total = 120.0 + 0.4
        segs = story_visuals.plan(total, seed=7, clips=self._pool())
        ids = [s["clip_id"] for s in segs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(segs), 25)
        self.assertLessEqual(len(segs), 40)
        self.assertAlmostEqual(sum(s["dur"] for s in segs), total, places=2)
        for s in segs:
            self.assertGreaterEqual(s["dur"], 2.5)
            self.assertLessEqual(s["dur"], 4.5)

    def test_pool_expansion_two_clips_per_query(self):
        # Mocked Pexels: K=2 yields 2 distinct clips for one query.
        videos = [
            {"id": 101, "user": {"name": "A"},
             "video_files": [{"link": "http://x/101.mp4", "width": 1080,
                              "height": 1920, "file_type": "video/mp4"}]},
            {"id": 102, "user": {"name": "B"},
             "video_files": [{"link": "http://x/102.mp4", "width": 1080,
                              "height": 1920, "file_type": "video/mp4"}]},
            {"id": 103, "user": {"name": "C"},
             "video_files": [{"link": "http://x/103.mp4", "width": 1080,
                              "height": 1920, "file_type": "video/mp4"}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            used: set = set()
            with mock.patch("engine.pexels.search_videos",
                           return_value=videos), \
                mock.patch("engine.pexels.download",
                           side_effect=lambda url, dest: Path(dest)
                           .write_bytes(b"fake") or Path(dest)):
                res = story_visuals._fetch_many("window cleaning", "key",
                                                cache, used, limit=2)
            self.assertEqual(len(res), 2)
            vids = [v for _, v, _ in res]
            self.assertEqual(len(set(vids)), 2)
            self.assertEqual(used, set(vids))
            self.assertTrue(all((cache / f"{v}.mp4").exists() for v in vids))

    def test_shortfall_longer_segs_plus_warning_no_repeats(self):
        # 12 clips cannot cover 82.9s uniquely: longer capped cuts +
        # a loud shortfall warning, still zero repeats.
        clips = self._pool(12, 40.0)
        with mock.patch("niches.story.visuals.log") as log_mock:
            segs = story_visuals.plan(82.9, seed=7, clips=clips)
        logged = " ".join(c[0][0] for c in log_mock.call_args_list)
        self.assertIn("SHORTFALL", logged)
        self.assertIn("12 clips", logged)
        self.assertEqual(len(segs), 12)
        self.assertEqual(len({s["clip_idx"] for s in segs}), 12)
        for s in segs:
            self.assertLessEqual(s["dur"], 4.5)

    def test_clips_per_query_config(self):
        self.assertEqual(StoryConfig().clips_per_query, 2)
        with mock.patch.dict(os.environ, {"STORY_CLIPS_PER_QUERY": "3"}):
            self.assertEqual(StoryConfig.from_env().clips_per_query, 3)


class TestCleaningPriority(unittest.TestCase):
    def test_speed_and_cleaning_config_defaults_and_env(self):
        cfg = StoryConfig()
        self.assertEqual(cfg.bg_speed_min, 2.0)
        self.assertEqual(cfg.bg_speed_max, 3.0)
        self.assertEqual(cfg.cleaning_clips_per_query, 3)
        self.assertAlmostEqual(cfg.cleaning_share, 0.55)
        env = {"STORY_BG_SPEED_MIN": "2.5", "STORY_BG_SPEED_MAX": "3.5",
               "STORY_CLEANING_CLIPS_PER_QUERY": "4",
               "STORY_CLEANING_SHARE": "0.7", "BG_SPEED_MIN": "9.9",
               "CLEANING_SHARE": "0.1"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg2 = StoryConfig.from_env()
        # STORY_ prefix wins over the bare fallback
        self.assertEqual(cfg2.bg_speed_min, 2.5)
        self.assertEqual(cfg2.bg_speed_max, 3.5)
        self.assertEqual(cfg2.cleaning_clips_per_query, 4)
        self.assertAlmostEqual(cfg2.cleaning_share, 0.7)

    def test_cleaning_queries_ahead_of_ambient(self):
        pool = list(story_visuals.EVERGREEN_QUERIES)
        first_ambient = min(pool.index(q)
                            for q in story_visuals.AMBIENT_QUERIES)
        last_cleaning = max(pool.index(q)
                            for q in story_visuals.CLEANING_QUERIES)
        self.assertLess(last_cleaning, first_ambient)

    def test_gather_cleaning_priority_order_limits_and_kinds(self):
        limits = {}

        def _fake_fetch_many(query, api_key, cache_dir, used, limit=2):
            limits[query] = limit
            vid = f"vid-{query.replace(' ', '-')}"
            return [(Path(f"/tmp/{vid}.mp4"), vid, "Pexels: T")]

        with tempfile.TemporaryDirectory() as tmp:
            cfg = StoryConfig(pexels_key="fake-key",
                              bg_dir="no-such-dir",
                              pexels_cache_dir=str(Path(tmp) / "cache"))
            with mock.patch("niches.story.visuals._fetch_many",
                            side_effect=_fake_fetch_many):
                clips, _credits, _ids, kinds = story_visuals.gather_clips(
                    ["office desk night"], "workplace", cfg)
        self.assertEqual(len(clips), len(kinds))
        # cleaning fetched deeper than story/ambient
        for q in story_visuals.CLEANING_QUERIES:
            self.assertEqual(limits[q], 3)
        self.assertEqual(limits["office desk night"], 2)
        self.assertEqual(limits["city night lights"], 2)
        # kinds follow query priority: story block, then every cleaning
        # clip precedes every ambient clip
        clean_pos = [i for i, k in enumerate(kinds) if k == "cleaning"]
        amb_pos = [i for i, k in enumerate(kinds) if k == "ambient"]
        self.assertTrue(clean_pos)
        self.assertTrue(amb_pos)
        self.assertLess(max(clean_pos), min(amb_pos))
        self.assertEqual(kinds[0], "story")

    def _tagged_pool(self, n_clean=18, n_other=22, dur=30.0):
        pool = [{"id": f"clean{i}", "duration": dur, "kind": "cleaning"}
                for i in range(n_clean)]
        pool += [{"id": f"story{i}", "duration": dur, "kind": "story"}
                 for i in range(n_other)]
        return pool

    def test_plan_cleaning_share_and_consecutive_runs(self):
        total = 82.9
        segs = story_visuals.plan(total, seed=7, clips=self._tagged_pool())
        # 82.9/3.5 -> need 25 segments; 55% -> exactly 14 cleaning
        self.assertEqual(len(segs), 25)
        self.assertAlmostEqual(sum(s["dur"] for s in segs), total, places=2)
        n_clean = sum(1 for s in segs if s["kind"] == "cleaning")
        self.assertEqual(n_clean, 14)
        self.assertGreaterEqual(n_clean / len(segs), 0.50)
        # consecutive satisfying runs, starting at segment 0
        self.assertEqual(segs[0]["kind"], "cleaning")
        best = cur = 0
        for s in segs:
            cur = cur + 1 if s["kind"] == "cleaning" else 0
            best = max(best, cur)
        self.assertGreaterEqual(best, 3)
        # planner contracts survive the partition
        self.assertEqual(len({s["clip_id"] for s in segs}), len(segs))
        for s in segs:
            self.assertGreaterEqual(s["dur"], 2.5)
            self.assertLessEqual(s["dur"], 4.5)
            self.assertGreaterEqual(s["speed"], 2.0)
            self.assertLessEqual(s["speed"], 3.0)

    def test_plan_share_zero_restores_uniform_mix(self):
        segs = story_visuals.plan(82.9, seed=7,
                                  clips=self._tagged_pool(),
                                  cleaning_share=0.0)
        self.assertEqual(len(segs), 25)
        n_clean = sum(1 for s in segs if s["kind"] == "cleaning")
        # uniform hypergeometric draw (25 picks from 18C/22S):
        # mixed, never the forced 14-run layout
        self.assertGreaterEqual(n_clean, 2)
        self.assertLessEqual(n_clean, 18)
        kinds = [s["kind"] for s in segs]
        self.assertIn("story", kinds)
        self.assertAlmostEqual(sum(s["dur"] for s in segs), 82.9, places=2)

    def test_plan_long_windows_fit_short_sources(self):
        pool = ([{"id": f"clean{i}", "duration": 8.0, "kind": "cleaning"}
                 for i in range(20)]
                + [{"id": f"story{i}", "duration": 8.0, "kind": "story"}
                   for i in range(20)])
        segs = story_visuals.plan(82.9, seed=7, clips=pool)
        by_id = {c["id"]: c["duration"] for c in pool}
        for s in segs:
            self.assertLessEqual(s["start_offset"] + s["window"],
                                 by_id[s["clip_id"]] + 1e-6)
        self.assertEqual(len({s["clip_id"] for s in segs}), len(segs))


class TestStoryBed(unittest.TestCase):
    def _pool(self):
        return [Path(f"bgm{i}.mp3") for i in (1, 2, 3)]

    def test_pool_pick_random_logged(self):
        cfg = StoryConfig(bed_path="", bgm_glob="bgm*.mp3")
        with mock.patch.object(Path, "glob", return_value=self._pool()), \
            mock.patch("niches.story.visuals.random.choice",
                       side_effect=lambda seq: seq[1]), \
            mock.patch("niches.story.visuals.log") as log_mock:
            pick = story_visuals._pick_bgm(cfg)
        self.assertEqual(pick, Path("bgm2.mp3"))
        logged = " ".join(c[0][0] for c in log_mock.call_args_list)
        self.assertIn("bgm2.mp3", logged)

    def test_explicit_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "mine.mp3"
            track.write_bytes(b"fake")
            cfg = StoryConfig(bed_path=str(track), bgm_glob="bgm*.mp3")
            with mock.patch.object(Path, "glob",
                                   side_effect=AssertionError("no glob")):
                self.assertEqual(story_visuals._pick_bgm(cfg), track)

    def test_empty_pool_means_drone(self):
        cfg = StoryConfig(bed_path="", bgm_glob="bgm*.mp3")
        with mock.patch.object(Path, "glob", return_value=[]):
            self.assertIsNone(story_visuals._pick_bgm(cfg))

    def test_missing_override_warns_then_drone(self):
        cfg = StoryConfig(bed_path="no-such-track.mp3", bgm_glob="bgm*.mp3")
        with mock.patch.object(Path, "glob", return_value=[]), \
            mock.patch("niches.story.visuals.warn") as warn_mock:
            self.assertIsNone(story_visuals._pick_bgm(cfg))
        warned = " ".join(c[0][0] for c in warn_mock.call_args_list)
        self.assertIn("no-such-track.mp3", warned)

    def test_bgm_glob_config(self):
        self.assertEqual(StoryConfig().bgm_glob, "bgm*.mp3")
        with mock.patch.dict(os.environ, {"STORY_BGM_GLOB": "music/*.mp3"}):
            self.assertEqual(StoryConfig.from_env().bgm_glob, "music/*.mp3")


if __name__ == "__main__":
    unittest.main()
