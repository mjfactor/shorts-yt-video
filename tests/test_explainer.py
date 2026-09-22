"""Unit tests for explainer Shorts niche, visuals, and fixtures."""
import json
import os
import unittest
from pathlib import Path
from PIL import Image

import core
import niche_explainer
import explainer_visuals


class TestNicheExplainer(unittest.TestCase):
    """Test niche_explainer prompt generation, validation, and offline scripts."""

    def test_mode_and_topic_pool(self):
        self.assertEqual(niche_explainer.MODE, "explainer")
        self.assertGreaterEqual(len(niche_explainer.TOPICS), 30)
        # Topics should not look like Top-5 listicles
        for topic in niche_explainer.TOPICS:
            self.assertFalse(topic.lower().startswith("5 "), f"Topic '{topic}' should not be a Top-5 ranking")
            self.assertFalse(topic.lower().startswith("top "), f"Topic '{topic}' should not be a Top-5 ranking")

    def test_build_prompt(self):
        prompt, slots = niche_explainer.build_prompt([])
        self.assertIn("YouTube Shorts explainer script", prompt)
        self.assertIn("visual_hint", prompt)
        self.assertIsInstance(slots, dict)
        self.assertIn("topic", slots)
        self.assertIn("topic_source", slots)

    def test_fallback_script(self):
        meta, slots = niche_explainer.fallback_script([])
        self.assertIn("topic", slots)
        self.assertIn("title", meta)
        self.assertIn("narration", meta)
        self.assertIn("beats", meta)
        # Validate through core._validate_script with niche_explainer
        validated = core._validate_script(meta, niche=niche_explainer)
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
        niche_explainer.validate_extra(sample)
        # Check that visual_hints are preserved
        for b in sample["beats"]:
            self.assertIn("visual_hint", b)

    def test_sanitize_title(self):
        dirty = "🔥 HOW DOES GPS WORK??? (Mind Blowing) #shorts"
        clean = niche_explainer.sanitize_title(dirty, {"topic": "How does GPS work"})
        self.assertNotIn("#", clean)
        self.assertTrue(clean.endswith("?"))


class TestDemoFixtures(unittest.TestCase):
    """Test all fixture files in demo_fixtures/."""

    def test_fixtures_valid(self):
        fixture_dir = Path("demo_fixtures")
        self.assertTrue(fixture_dir.exists(), "demo_fixtures directory must exist")
        fixtures = list(fixture_dir.glob("*.json"))
        self.assertGreaterEqual(len(fixtures), 3, "Expected at least 3 demo fixtures")

        for fpath in fixtures:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Must pass core script validation
            validated = core._validate_script(data, niche=niche_explainer)
            self.assertIn("title", validated)
            self.assertIn("beats", validated)
            self.assertGreaterEqual(len(validated["beats"]), 5)
            # Verify beats have visual_hints
            for b in validated["beats"]:
                self.assertIn("visual_hint", b, f"Beat missing visual_hint in {fpath.name}")
                self.assertIn("type", b["visual_hint"])




if __name__ == "__main__":
    unittest.main()
