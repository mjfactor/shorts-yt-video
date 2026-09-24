"""Phase 3 contract tests: niche protocol, registry, isolation, layering, history.

Story-only (explainer/ranking live under deprecated_niches/ for reference).
No network. Python 3.11 unittest style.
"""
import copy
import os
import unittest
from pathlib import Path
from unittest import mock

from niches import DEFAULT_NICHE, available_niches, load_niche
from niches.base import Content, Niche
from niches.history_migrate import (
    duplicate_reason,
    flatten_history,
    migrate_history,
)

NICHES = ("story",)
ARCHIVED_NICHES = ("explainer", "ranking")
PROTOCOL_METHODS = (
    "generate_content",
    "narrate",
    "build_visuals",
    "build_metadata",
    "sample_content",
)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    return load_niche(name)


class TestNicheProtocol(unittest.TestCase):
    def test_protocol_methods_present(self):
        for name in NICHES:
            with self.subTest(niche=name):
                niche = _load(name)
                self.assertIsInstance(niche, Niche)
                for meth in PROTOCOL_METHODS:
                    self.assertTrue(
                        callable(getattr(niche, meth, None)),
                        f"{name} missing protocol method {meth}",
                    )
                self.assertTrue(getattr(niche, "NAME", ""))
                self.assertTrue(getattr(niche, "LABEL", "") or getattr(niche, "MODE", ""))

    def test_build_metadata_shape(self):
        for name in NICHES:
            with self.subTest(niche=name):
                niche = _load(name)
                content = niche.sample_content()
                meta = niche.build_metadata(content)
                self.assertIn("title", meta)
                self.assertIn("description", meta)
                self.assertIn("tags", meta)
                self.assertEqual(meta["title"], content.title)


class TestSampleContent(unittest.TestCase):
    def test_sample_content_validates(self):
        for name in NICHES:
            with self.subTest(niche=name):
                niche = _load(name)
                content = niche.sample_content()
                self.assertIsInstance(content, Content)
                # title <= 100 chars, non-empty
                self.assertIsInstance(content.title, str)
                self.assertTrue(content.title.strip())
                self.assertLessEqual(len(content.title), 100)
                # narration words in range (generic floor; all niches are long-form)
                words = len(content.narration_text.split())
                self.assertGreaterEqual(
                    words, 20, f"{name} narration too short ({words} words)"
                )
                self.assertLessEqual(words, 5000)
                # tags / description present
                self.assertIsInstance(content.tags, list)
                self.assertTrue(content.tags)
                for tag in content.tags:
                    self.assertIsInstance(tag, str)
                    self.assertTrue(tag.strip())
                self.assertIsInstance(content.description, str)
                self.assertTrue(content.description.strip())


class TestRegistry(unittest.TestCase):
    def test_default_niche_is_story(self):
        self.assertEqual(DEFAULT_NICHE, "story")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NICHE", None)
            niche = load_niche(None)
            self.assertEqual(getattr(niche, "NAME", ""), "story")

    def test_niche_env_override(self):
        # registry normalizes case/whitespace around the name
        with mock.patch.dict(os.environ, {"NICHE": "  Story "}):
            niche = load_niche(None)
            self.assertEqual(getattr(niche, "NAME", ""), "story")

    def test_unknown_niche_error_lists_available(self):
        avail = available_niches()
        self.assertTrue(avail)
        with self.assertRaises(ImportError) as ctx:
            load_niche("nope-unknown-niche-xyz")
        msg = str(ctx.exception)
        self.assertIn("Available niches", msg)
        for name in avail:
            self.assertIn(name, msg)

    def test_auto_discovery(self):
        avail = available_niches()
        self.assertEqual(avail, sorted(avail))
        for name in NICHES:
            self.assertIn(name, avail)
        # archived niches live outside niches/ and must not be discovered
        for name in ARCHIVED_NICHES:
            self.assertNotIn(name, avail)
        # private packages (leading _) are skipped
        for name in avail:
            self.assertFalse(name.startswith("_"))
        # every discovered niche loads
        for name in avail:
            with self.subTest(niche=name):
                self.assertTrue(callable(getattr(load_niche(name), "sample_content", None)))


class TestIsolation(unittest.TestCase):
    def test_no_shared_mutable_state(self):
        for name in NICHES:
            with self.subTest(niche=name):
                niche = _load(name)
                first = niche.sample_content()
                second = niche.sample_content()
                self.assertIsNot(first, second)
                self.assertIsNot(first.tags, second.tags)
                self.assertIsNot(first.extras, second.extras)
                snapshot = copy.deepcopy(second)
                first.tags.append("__mutant__")
                first.extras["__mutant__"] = True
                if isinstance(first.extras.get("beats"), list):
                    first.extras["beats"].append({"text": "x", "query": "y"})
                self.assertEqual(second.tags, snapshot.tags)
                self.assertEqual(second.extras, snapshot.extras)

    def test_content_defaults_not_shared(self):
        first = Content(title="a", narration_text="hello world " * 10)
        second = Content(title="b", narration_text="hello world " * 10)
        self.assertIsNot(first.tags, second.tags)
        self.assertIsNot(first.extras, second.extras)


class TestLayering(unittest.TestCase):
    def _sources(self):
        files = [REPO_ROOT / "core.py"]
        engine_dir = REPO_ROOT / "engine"
        files.extend(sorted(engine_dir.glob("*.py")))
        return [p for p in files if p.exists()]

    def test_no_niche_imports_in_core_engine(self):
        # Shared contracts (niches.base / niches.history_migrate) are allowed;
        # concrete niches must never be imported by core/ or engine/.
        banned = (
            "niches.story",
            "niches.explainer",
            "niches.ranking",
            "niches._template",
            "niche_explainer",
            "niche_ranking",
            "niche_story",
            "from niches import",
            "import niches",
        )
        allowed_markers = ("niches.base", "niches.history_migrate")
        offenders = []
        for path in self._sources():
            text = path.read_text(encoding="utf-8")
            for marker in banned:
                if marker in text:
                    # `import niches` / `from niches import` only offends when it
                    # is not one of the allowed shared-contract imports.
                    if marker in ("from niches import", "import niches"):
                        lines = [
                            line.strip()
                            for line in text.splitlines()
                            if marker in line
                        ]
                        if all(
                            any(allow in line for allow in allowed_markers)
                            for line in lines
                        ):
                            continue
                    offenders.append(f"{path.name}: {marker}")
        self.assertEqual(offenders, [])

    def test_legacy_root_modules_removed(self):
        # Pre-Protocol root niche modules were deleted in Phase 3c; the
        # niches/* ports are the only implementations left.
        for name in ("niche_explainer.py", "niche_ranking.py",
                     "niche_story.py", "explainer_visuals.py",
                     "story_visuals.py"):
            self.assertFalse((REPO_ROOT / name).exists(),
                             f"legacy root module still present: {name}")
        text = (REPO_ROOT / "core.py").read_text(encoding="utf-8")
        for marker in ("niche_explainer", "niche_ranking", "niche_story",
                       "explainer_visuals", "story_visuals",
                       "_LegacyAdapter", "_validate_script", "adapt_legacy"):
            self.assertNotIn(marker, text,
                             f"core.py still references legacy {marker}")

    def test_engine_never_imports_core(self):
        offenders = []
        for path in sorted((REPO_ROOT / "engine").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "import core" in stripped or "from core" in stripped:
                    offenders.append(f"{path.name}: {stripped}")
        self.assertEqual(offenders, [])


class TestHistoryMigration(unittest.TestCase):
    def test_flat_to_grouped_lossless(self):
        flat = [
            {"date": "2026-01-03", "niche": "story", "title": "C", "topic": "t3"},
            {"date": "2026-01-01", "niche": "story", "title": "A", "topic": "t1"},
            {"date": "2026-01-02", "niche": "story", "title": "B", "genre": "g2"},
            {"date": "2026-01-04", "title": "D-no-niche", "topic": "t4"},
            "not-a-dict",
            None,
        ]
        grouped = migrate_history(flat, default_niche="story")
        self.assertEqual(set(grouped), {"story"})
        # missing niche defaults to `story`
        self.assertTrue(any(h.get("title") == "D-no-niche" for h in grouped["story"]))
        # non-dict entries skipped
        total = sum(len(v) for v in grouped.values())
        self.assertEqual(total, 4)
        # flatten is ordered by date and lossless (same multiset of titles)
        back = flatten_history(grouped)
        self.assertEqual(
            [h.get("date", "") for h in back],
            sorted(h.get("date", "") for h in back),
        )
        self.assertEqual(
            sorted(h["title"] for h in back),
            ["A", "B", "C", "D-no-niche"],
        )
        # round-trip preserves entry payloads (copies, not aliases)
        by_title = {h["title"]: h for h in back}
        self.assertEqual(by_title["A"]["topic"], "t1")
        self.assertEqual(by_title["B"]["genre"], "g2")

    def test_duplicate_reason(self):
        hist = [{"title": "Hello World", "topic": "Space"}]
        reason_title = duplicate_reason({"title": "hello world", "topic": "other"}, hist)
        self.assertIsNotNone(reason_title)
        self.assertIn("already used", str(reason_title))
        reason_topic = duplicate_reason({"title": "other", "topic": "SPACE"}, hist)
        self.assertIsNotNone(reason_topic)
        self.assertIn("already used", str(reason_topic))
        self.assertIsNone(duplicate_reason({"title": "fresh", "topic": "fresh"}, hist))
        self.assertIsNone(duplicate_reason({"title": "fresh", "topic": "fresh"}, []))


if __name__ == "__main__":
    unittest.main()
