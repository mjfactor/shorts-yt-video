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
        self.assertIn("Every sentence must do at least one job", prompt)
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


class SceneDressingGateTests(unittest.TestCase):
    """Enforces the prompt's 'no decorative weekdays/weather' rule.

    The validator counterpart of the TONE & VOICE scene-dressing ban:
    weather-on-weekday and indefinite-day framing are rejected (the retry
    then gets the exact fragment to cut), while timing that carries the
    plot — deadlines, event times, recurring schedules — stays legal.
    """

    CARD = "I found the invoice on my desk"
    S1 = ("I printed my badge photo and the original hiring letter and "
          "walked to the finance office.")
    S2 = "She said the fee was real but the signature was not mine."
    S3 = ("By Friday afternoon they had removed the charge and sent me "
          "a written apology.")

    def _draft(self, extra: str) -> dict:
        narration = " ".join((
            self.CARD + ".",
            "The printout in my folder listed a supply fee I never signed "
            "for, so I started reading every statement line by line.",
            extra,
            "I asked my manager about it and he told me to take it up with "
            "corporate.",
            "Corporate said the charge came from my department.",
            self.S1,
            "The clerk checked the system twice and frowned.",
            self.S2,
            "We reconcile the ledgers every Tuesday morning to catch "
            "mistakes early.",
            self.S3,
        ))
        return {
            "title": "My Desk Held a Surprise \U0001F632",
            "card_text": self.CARD,
            "card_highlight_word": "invoice",
            "hook": self.CARD,
            "story_text": narration,
            "narration": narration,
            "genre": "workplace",
            "narrator_gender": "female",
            "key_words": ["invoice", "desk", "fee", "manager"],
            "tags": ["story", "workplace"],
            "description": ("An invoice nobody signed showed up on her desk. "
                            "It did not stay there long."),
            "ending_type": "payoff",
            "pinned_comment": "Would you have fought the fee?",
            "rehooks": [
                {"at": "20-30s", "line": self.S1},
                {"at": "50-60s", "line": self.S2},
                {"at": "80-90s", "line": self.S3},
            ],
            "beats": [{"text": narration}],
        }

    def test_rejects_weather_on_weekday_scene_setting(self):
        draft = self._draft(
            "I checked the balance on a rainy Tuesday morning and saw "
            "a negative sign.")
        with self.assertRaisesRegex(ValueError, "decorative scene-setting"):
            content.validate_extra(draft)

    def test_rejects_indefinite_day_framing(self):
        draft = self._draft(
            "I came into work early on a Tuesday, holding my travel mug.")
        with self.assertRaisesRegex(ValueError, "decorative scene-setting"):
            content.validate_extra(draft)

    def test_allows_structural_weekday_timing(self):
        # Deadline ("By Friday afternoon"), event time ("on Thursday
        # morning") and recurring schedule ("every Tuesday morning") all
        # appear in this draft and must pass untouched.
        draft = self._draft(
            "The clerk called back on Thursday morning with the updated "
            "ledger.")
        extra = content.validate_extra(draft)
        self.assertEqual(extra["ending_type"], "payoff")


if __name__ == "__main__":
    unittest.main()
