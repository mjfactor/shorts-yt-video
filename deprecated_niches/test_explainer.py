"""Unit tests for explainer Shorts niche, visuals, and fixtures.

Archived reference copy: exercises deprecated_niches/explainer/ in place
(the live tree no longer ships it). Run standalone from the repo root:
`python deprecated_niches/test_explainer.py`.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from PIL import Image

# Repo-root bootstrap (same pattern as demo.py) so `niches.base` resolves.
# The deprecated package itself loads as deprecated_niches.explainer via
# namespace-package import — never as niches.explainer, so the live
# registry is unaffected and no suite paths are polluted.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deprecated_niches.explainer import content as explainer_content


class TestNicheExplainer(unittest.TestCase):
    """Test explainer content prompt generation, validation, and offline scripts."""

    def test_mode_and_topic_pool(self):
        self.assertEqual(explainer_content.MODE, "explainer")
        self.assertGreaterEqual(len(explainer_content.TOPICS), 30)
        # Topics should not look like Top-5 listicles
        for topic in explainer_content.TOPICS:
            self.assertFalse(topic.lower().startswith("5 "), f"Topic '{topic}' should not be a Top-5 ranking")
            self.assertFalse(topic.lower().startswith("top "), f"Topic '{topic}' should not be a Top-5 ranking")

    def test_build_prompt(self):
        prompt, slots = explainer_content.build_prompt([])
        self.assertIn("YouTube Shorts explainer script", prompt)
        self.assertIn("visual_hint", prompt)
        self.assertIsInstance(slots, dict)
        self.assertIn("topic", slots)
        self.assertIn("topic_source", slots)

    def test_fallback_script(self):
        meta, slots = explainer_content.fallback_script([])
        self.assertIn("topic", slots)
        self.assertIn("title", meta)
        self.assertIn("narration", meta)
        self.assertIn("beats", meta)
        # Validate through the ported explainer validator
        validated = explainer_content._validate_script(meta)
        self.assertIn("beats", validated)
        self.assertGreaterEqual(len(validated["beats"]), 5)

    def test_validate_extra_validates_visual_hints(self):
        sample = {
            "beats": [
                {
                    "text": "Have you ever wondered how an ATM safely gives you cash without making mistakes?",
                    "query": "atm machine cash",
                    "visual_hint": {
                        "type": "mechanism",
                        "description": "Cross-section of an ATM dispenser mechanism",
                        "components": ["Card Reader", "Computer", "Cash Dispenser"],
                    },
                },
                {
                    "text": "First, the reader decodes your magnetic stripe or EMV chip securely.",
                    "query": "credit card chip",
                    "visual_hint": {
                        "type": "process_flow",
                        "description": "Card data flow to bank verification server",
                        "components": ["ATM", "Encrypted Network", "Bank Server"],
                    },
                },
                {
                    "text": "High speed optical sensors count each bill by measuring thickness and optical reflectance.",
                    "query": "counting money sensor",
                    "visual_hint": {
                        "type": "diagram",
                        "description": "Optical sensor scanning bills for double-feeds",
                        "components": ["Light Emitter", "Bill", "Optical Sensor"],
                    },
                },
                {
                    "text": "If two bills stick together, the ATM instantly redirects them into a reject cassette.",
                    "query": "vault cash mechanism",
                    "visual_hint": {
                        "type": "comparison",
                        "description": "Single bill accepted vs double bill diverted to reject box",
                        "components": ["Good Bill", "Diverter Gate", "Reject Box"],
                    },
                },
                {
                    "text": "Finally, the motorized roller presents your cash once the transaction is logged.",
                    "query": "hand taking money atm",
                    "visual_hint": {
                        "type": "text_card",
                        "description": "Transaction complete summary",
                        "components": ["Account Debited", "Cash Dispensed"],
                    },
                },
            ]
        }
        explainer_content.validate_extra(sample)
        # Check that visual_hints are preserved
        for b in sample["beats"]:
            self.assertIn("visual_hint", b)

    def test_sanitize_title(self):
        dirty = "🔥 HOW DOES GPS WORK??? (Mind Blowing) #shorts"
        clean = explainer_content.sanitize_title(dirty, {"topic": "How does GPS work"})
        self.assertNotIn("#", clean)
        self.assertTrue(clean.endswith("?"))


class TestDemoFixtures(unittest.TestCase):
    """Test the inlined offline sample (fixture files removed)."""

    def test_sample_valid(self):
        # Rebuilt from sample_content() with no disk reads.
        content = explainer_content.sample_content()
        data = {"title": content.title,
                "narration": content.narration_text,
                "description": content.description,
                "tags": list(content.tags),
                "beats": [dict(b)
                          for b in (content.extras or {}).get("beats", [])]}
        self.assertTrue(data["title"])
        self.assertTrue(data["narration"].split())
        # Must pass the ported explainer script validation
        validated = explainer_content._validate_script(data)
        self.assertIn("title", validated)
        self.assertIn("beats", validated)
        self.assertGreaterEqual(len(validated["beats"]), 5)
        # Verify beats have visual_hints
        for b in validated["beats"]:
            self.assertIn("visual_hint", b, "Beat missing visual_hint")
            self.assertIn("type", b["visual_hint"])




if __name__ == "__main__":
    unittest.main()
