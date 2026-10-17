"""Offline story prompt regression checks; never call Gemini or upload.

Run: python tools/test_story_prompt.py
These check prompt contracts, not the quality of generated stories.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from niches.story import content
from niches.story.config import StoryConfig


class StoryPromptTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StoryConfig(story_genres=("family",),
                               narrator_gender_mode="female")
        self.wps = 4.8

    def assert_story_guidance(self, prompt):
        self.assertIn("PREMISE SELECTION", prompt)
        self.assertIn("three genuinely different premises", prompt)
        self.assertIn("keep the existing premise", prompt)
        self.assertIn("TONE & VOICE", prompt)
        self.assertIn("Zero filler words is fine", prompt)
        self.assertIn("GROUNDED & BELIEVABLE", prompt)

    def test_premise_and_voice_come_before_output_format(self):
        prompt, _ = content.build_prompt([], self.cfg, self.wps)
        self.assert_story_guidance(prompt)
        self.assertLess(prompt.index("PREMISE SELECTION"),
                        prompt.index("TONE & VOICE"))
        self.assertLess(prompt.index("TONE & VOICE"),
                        prompt.index("Return ONLY valid JSON"))
        self.assertIn("not just scams and revenge", prompt)
        self.assertNotIn("Hook examples", prompt)
        self.assertNotIn("Skip dialogue tags", prompt)

    def test_configured_budget_and_slots_are_preserved(self):
        prompt, slots = content.build_prompt([], self.cfg, self.wps)
        lo, hi = content.target_words(self.cfg, self.wps)
        self.assertIn(f"TOTAL {lo}-{hi} words, aim {(lo + hi) // 2} words",
                      prompt)
        self.assertIn("for the 100-120s target", prompt)
        self.assertIn("genre must echo 'family'", prompt)
        self.assertEqual(slots, {
            "genre": "family", "topic": "family",
            "topic_source": "genre-rotation", "narrator_gender": "female",
        })

    def test_recent_premises_are_included_for_avoidance(self):
        history = [{
            "niche": "story", "title": "The spare key nobody returned",
            "premise": "My sister lent our spare key to someone I had never met.",
        }]
        prompt, _ = content.build_prompt(history, self.cfg, self.wps)
        self.assertIn(content.AVOID_INTRO, prompt)
        self.assertIn(history[0]["title"], prompt)
        self.assertIn(history[0]["premise"], prompt)
        self.assertIn("A DIFFERENT conflict mechanism", prompt)

    def test_spoken_and_metadata_contracts_are_preserved(self):
        prompt, _ = content.build_prompt([], self.cfg, self.wps)
        keys = ("title, card_text, card_highlight_word, hook, story_text, "
                "narration, genre, narrator_gender, key_words, tags, "
                "description, ending_type, pinned_comment, beats, rehooks, "
                "plus optional part_2_seed")
        self.assertIn("exactly these keys: " + keys, prompt)
        self.assertIn("story_text and narration must be IDENTICAL", prompt)
        self.assertIn("card line (<=8 words)", prompt)
        self.assertIn("card_highlight_word must be one word appearing in card_text",
                      prompt)
        self.assertIn("rehooks: metadata ONLY, never spoken", prompt)
        for slot in ("20-30s", "50-60s", "80-90s"):
            self.assertIn(slot, prompt)
        self.assertIn("must equal narration EXACTLY", prompt)
        self.assertIn("30 words or fewer", prompt)
        self.assertIn("'payoff'", prompt)
        self.assertIn("'cliffhanger'", prompt)
        self.assertIn("Safety (strict)", prompt)

    def test_retries_keep_premise_and_voice_for_short_and_long_drafts(self):
        error = ValueError("'story_text' out of range")
        for repair in (content._length_fix_prompt, content._shape_fix_prompt):
            for repetitions in (20, 100):
                with self.subTest(repair=repair.__name__, repeats=repetitions):
                    raw = {
                        "card_text": "My sister forgot the spare key",
                        "narration": "My sister forgot the spare key. " * repetitions,
                    }
                    prompt, slots = repair([], self.cfg, self.wps, raw, error)
                    self.assertTrue(prompt.startswith("CORRECTION"))
                    self.assertIn("same story", prompt.lower())
                    self.assert_story_guidance(prompt)
                    self.assertNotIn("sights, sounds", prompt)
                    self.assertEqual(slots["genre"], "family")

    def test_repair_requests_include_the_complete_previous_draft(self):
        narration = ("My sister forgot the spare key. "
                     "I lent her mine and had to wait outside. "
                     "She brought it back with a note I kept.")
        raw = {
            "title": "The note with my spare key",
            "card_text": "My sister forgot the spare key",
            "narration": narration,
            "story_text": narration,
            "beats": [{"text": narration}],
        }
        for repair in (content._length_fix_prompt, content._shape_fix_prompt):
            with self.subTest(repair=repair.__name__):
                prompt, _ = repair([], self.cfg, self.wps, raw,
                                   ValueError("'story_text' out of range"))
                self.assertTrue(json.dumps(raw, ensure_ascii=False) in prompt,
                                "Repair request must include the complete draft JSON")
                self.assertIn("PREVIOUS DRAFT", prompt)
                self.assertLess(prompt.index("PREVIOUS DRAFT"),
                                prompt.index("PREMISE SELECTION"))

    def test_empty_retry_drafts_keep_base_guidance(self):
        for repair in (content._length_fix_prompt, content._shape_fix_prompt):
            for raw in (None, {"narration": ""}):
                with self.subTest(repair=repair.__name__, raw=raw):
                    prompt, _ = repair([], self.cfg, self.wps, raw, None)
                    self.assert_story_guidance(prompt)
                    self.assertFalse(prompt.startswith("CORRECTION"))


if __name__ == "__main__":
    unittest.main()
