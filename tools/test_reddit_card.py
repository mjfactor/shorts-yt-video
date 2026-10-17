"""Offline checks for the public opening-card renderer; Pillow only.

Run: python tools/test_reddit_card.py
No network, TTS, FFmpeg, API keys, or persistent render outputs are needed.
"""
import inspect
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image, ImageColor, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import overlay_card
from niches.story.config import StoryConfig
from niches.story.visuals import STORY_CARD_THEME

_HOOK = "My sister hid the spare key from me."
_BODY_THEME = {
    "text_color": (17, 45, 91, 255),
    "highlight_color": (177, 21, 113, 255),
}


@dataclass
class _TextCall:
    text: str
    color: object
    bbox: tuple[float, float, float, float]
    image_size: tuple[int, int]


def _rgba(color):
    if isinstance(color, str):
        return ImageColor.getcolor(color, "RGBA")
    if isinstance(color, (tuple, list)):
        return tuple(color) + ((255,) if len(color) == 3 else ())
    return color


class StoryCardConfigTests(unittest.TestCase):
    def test_direct_defaults(self):
        config = StoryConfig()
        self.assertEqual(config.card_likes, "930+")
        self.assertEqual(config.card_shares, "930+")

    def test_from_env_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = StoryConfig.from_env()
        self.assertEqual(config.card_likes, "930+")
        self.assertEqual(config.card_shares, "930+")

    def test_environment_override_precedence(self):
        cases = (
            ({"CARD_LIKES": "12K", "CARD_SHARES": "73+"}, "12K", "73+"),
            ({"STORY_CARD_LIKES": "19K", "STORY_CARD_SHARES": "82+"},
             "19K", "82+"),
            ({"CARD_LIKES": "12K", "CARD_SHARES": "73+",
              "STORY_CARD_LIKES": "19K", "STORY_CARD_SHARES": "82+"},
             "19K", "82+"),
            ({"CARD_LIKES": "12K", "CARD_SHARES": "73+",
              "STORY_CARD_LIKES": "", "STORY_CARD_SHARES": ""},
             "12K", "73+"),
            ({"CARD_LIKES": "", "CARD_SHARES": "",
              "STORY_CARD_LIKES": "", "STORY_CARD_SHARES": ""},
             "930+", "930+"),
        )
        for env, likes, shares in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                config = StoryConfig.from_env()
                self.assertEqual(config.card_likes, likes)
                self.assertEqual(config.card_shares, shares)


class RedditCardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workdir = Path(tmp.name)
        self.render_count = 0

    def _render(self, text=_HOOK, highlight_word="key", **kwargs):
        self.render_count += 1
        options = {"handle": "@MiraReads", "avatar_path": None, **kwargs}
        out = options.pop("out_png", self.workdir / f"card_{self.render_count}.png")
        calls = []
        original = ImageDraw.ImageDraw.text
        signature = inspect.signature(original)
        bbox_keys = set(inspect.signature(ImageDraw.ImageDraw.textbbox).parameters)
        bbox_keys.difference_update(("self", "xy", "text"))

        def record_text(draw, xy, text, *args, **kwargs):
            start = len(calls)
            result = original(draw, xy, text, *args, **kwargs)
            # Multiline text can recursively call text(); count only leaf draws.
            if len(calls) == start and str(text).strip():
                values = dict(signature.bind(draw, xy, text, *args, **kwargs).arguments)
                values.update(values.pop("kwargs", {}))
                bbox_options = {key: values[key] for key in bbox_keys if key in values}
                calls.append(_TextCall(
                    str(text), _rgba(values.get("fill")),
                    draw.textbbox(xy, text, **bbox_options), draw._image.size))
            return result

        with patch.object(ImageDraw.ImageDraw, "text", new=record_text):
            result = overlay_card.render_spec_card(
                text, highlight_word, out_png=out, **options)
        self.assertIsInstance(result, Path)
        self.assertEqual(result, Path(out))
        with Image.open(result) as rendered:
            self.assertEqual(rendered.format, "PNG")
            image = rendered.copy()
        self.assertTrue(calls, "The card must actually draw text")
        return image, calls

    def _assert_word_color(self, calls, word, color):
        colors = [call.color for call in calls if word in call.text.split()]
        self.assertTrue(colors, f"Missing drawn word: {word!r}")
        self.assertTrue(all(actual == _rgba(color) for actual in colors),
                        f"{word!r} used {colors!r}, expected {_rgba(color)!r}")

    def _assert_counts(self, calls, likes, shares):
        words = [word for call in calls for word in call.text.split()]
        self.assertIn(likes, words)
        self.assertIn(shares, words)

    def _assert_text_inside(self, calls):
        for call in calls:
            with self.subTest(text=call.text, surface=call.image_size):
                left, top, right, bottom = call.bbox
                width, height = call.image_size
                self.assertGreaterEqual(left, 0, f"Text starts outside its image: {call}")
                self.assertGreaterEqual(top, 0, f"Text starts outside its image: {call}")
                self.assertLessEqual(right, width, f"Text exceeds its image width: {call}")
                self.assertLessEqual(bottom, height, f"Text exceeds its image height: {call}")

    def _assert_same_render(self, left, right):
        self.assertEqual(left.mode, right.mode)
        self.assertEqual(left.size, right.size)
        self.assertTrue(left.tobytes() == right.tobytes(),
                        "Equivalent public options must produce the same card")

    def test_png_has_transparent_corners_and_exact_requested_width(self):
        for width in (None, 720, 420):
            with self.subTest(width=width):
                options: dict[str, Any] = {} if width is None else {"width": width}
                # Exercise string paths and creation of an absent parent directory.
                options["out_png"] = str(self.workdir / "nested" / f"{width}.png")
                image, _ = self._render(**options)
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.width, 900 if width is None else width)
                self.assertGreater(image.height, 0)
                alpha = image.getchannel("A")
                for corner in ((0, 0), (image.width - 1, 0),
                               (0, image.height - 1),
                               (image.width - 1, image.height - 1)):
                    self.assertEqual(alpha.getpixel(corner), 0)
                self.assertIsNotNone(alpha.getbbox())

    def test_default_and_custom_theme_colors_and_punctuated_highlight(self):
        custom = {
            **_BODY_THEME,
            "card_fill": (231, 239, 247, 255),
            "handle_color": (25, 91, 57, 255),
            "engagement_color": (109, 61, 19, 255),
        }
        for theme in (None, custom):
            expected = {**overlay_card.SPEC_CARD_THEME, **(theme or {})}
            for token, highlight in (('"KEY,"', "key"), ("key!", ' "KEY?" ')):
                with self.subTest(custom=theme is not None, token=token):
                    hook = f"My sister left the {token} beside my keyring."
                    image, calls = self._render(hook, highlight, theme=theme)
                    self._assert_word_color(calls, token, expected["highlight_color"])
                    self._assert_word_color(calls, "sister", expected["text_color"])
                    self._assert_word_color(calls, "keyring.", expected["text_color"])
                    self._assert_word_color(calls, "@MiraReads", expected["handle_color"])
                    self._assert_word_color(calls, str(expected["likes_text"]),
                                            expected["engagement_color"])
                    colors = image.getcolors(maxcolors=image.width * image.height)
                    self.assertIsNotNone(colors)
                    palette = {color for _, color in (colors or ())}
                    self.assertIn(_rgba(expected["card_fill"]), palette)

    def test_empty_highlight_uses_normal_text_color(self):
        _, calls = self._render(highlight_word="", theme=_BODY_THEME)
        for word in _HOOK.split():
            self._assert_word_color(calls, word, _BODY_THEME["text_color"])
        self.assertNotIn(_BODY_THEME["highlight_color"], [call.color for call in calls])

    def test_default_and_theme_engagement_counts(self):
        defaults = overlay_card.SPEC_CARD_THEME
        for theme, likes, shares in (
                (None, str(defaults["likes_text"]), str(defaults["shares_text"])),
                ({"likes_text": "8.4K", "shares_text": "91+"}, "8.4K", "91+")):
            with self.subTest(theme=theme):
                _, calls = self._render(theme=theme)
                self._assert_counts(calls, likes, shares)

    def test_explicit_engagement_counts_override_theme(self):
        _, calls = self._render(
            theme={"likes_text": "111+", "shares_text": "222+"},
            likes="9.8K", shares="64+")
        self._assert_counts(calls, "9.8K", "64+")
        words = [word for call in calls for word in call.text.split()]
        self.assertNotIn("111+", words)
        self.assertNotIn("222+", words)

    def test_awards_and_reactions_kwargs_are_equivalent(self):
        references = {}
        for enabled in (False, True):
            image, calls = self._render(show_awards=enabled)
            references[enabled] = image
            self._assert_counts(calls, str(overlay_card.SPEC_CARD_THEME["likes_text"]),
                                str(overlay_card.SPEC_CARD_THEME["shares_text"]))
            alias, _ = self._render(show_reactions=enabled)
            self._assert_same_render(image, alias)
        # Compare renders from this run, not a fixed pixel snapshot or layout.
        self.assertTrue(references[False].size != references[True].size or
                        references[False].tobytes() != references[True].tobytes(),
                        "Enabling awards must visibly change the card")

    def test_theme_awards_and_reactions_flags(self):
        for enabled in (False, True):
            reference, _ = self._render(show_awards=enabled)
            for key in ("show_awards", "show_reactions"):
                with self.subTest(flag=key, enabled=enabled):
                    themed, _ = self._render(theme={key: enabled})
                    self._assert_same_render(reference, themed)

    def test_explicit_visibility_flags_override_theme_and_awards_wins(self):
        for enabled in (False, True):
            reference, _ = self._render(show_awards=enabled)
            theme = {"show_awards": not enabled, "show_reactions": not enabled}
            with self.subTest(enabled=enabled, option="show_reactions"):
                image, _ = self._render(theme=theme, show_reactions=enabled)
                self._assert_same_render(reference, image)
            with self.subTest(enabled=enabled, option="show_awards"):
                image, _ = self._render(theme=theme, show_awards=enabled,
                                        show_reactions=not enabled)
                self._assert_same_render(reference, image)

    def test_missing_avatar_uses_the_same_drawn_initial_fallback_as_none(self):
        missing = self.workdir / "missing-avatar.png"
        self.assertFalse(missing.exists())
        reference, _ = self._render(avatar_path=None)
        image, calls = self._render(avatar_path=missing)
        self._assert_same_render(reference, image)
        self.assertIn("M", [call.text for call in calls])
        self._assert_text_inside(calls)

    def test_long_hooks_handles_and_narrow_cards_keep_text_inside_its_image(self):
        longer = ("My sister borrowed our spare key and returned it with a note "
                  "that explained why the neighbors had stopped speaking to us.")
        long_handle = "@TheNeighbourhoodStoryArchiveAndWeekendFamilyAdviceClub"
        for width, handle, hook in (
                (900, "@MiraReads", _HOOK),
                (900, "@MiraReads", longer),
                (900, long_handle, _HOOK),
                (420, "@MiraReads", _HOOK),
                (360, long_handle, longer)):
            with self.subTest(width=width, handle=handle, hook=hook):
                image, calls = self._render(hook, "key", width=width, handle=handle)
                self.assertEqual(image.width, width)
                self._assert_text_inside(calls)

    def test_production_theme_has_bounded_text_transparent_corners_and_counts(self):
        config = StoryConfig()
        for width in (900, 420, 360):
            with self.subTest(width=width):
                image, calls = self._render(
                    width=width, theme=STORY_CARD_THEME,
                    handle="@TheNeighbourhoodStoryArchiveAndWeekendFamilyAdviceClub",
                    likes=config.card_likes, shares=config.card_shares)
                self.assertEqual(image.width, width)
                self.assertEqual(image.mode, "RGBA")
                for corner in ((0, 0), (width - 1, 0),
                               (0, image.height - 1), (width - 1, image.height - 1)):
                    self.assertEqual(image.getpixel(corner)[3], 0)
                counts = [word for call in calls for word in call.text.split()]
                self.assertEqual(counts.count("930+"), 2)
                self._assert_text_inside(calls)

    def test_normal_eight_word_and_longer_readable_hooks_lose_no_words(self):
        hooks = (
            (900, _HOOK),
            (900, "Grandmother's inheritance disappeared between breakfast and Sunday's meeting."),
            (900, ("My sister borrowed our spare key and returned it with a note "
                   "that explained why the neighbors had stopped speaking to us.")),
            (420, ("I found the spare key inside a letter that nobody in my family "
                   "would admit they had written.")),
        )
        body_colors = set(_BODY_THEME.values())
        for width, hook in hooks:
            with self.subTest(width=width, hook=hook):
                _, calls = self._render(hook, "key", width=width, theme=_BODY_THEME)
                drawn_words = [word for call in calls if call.color in body_colors
                               for word in call.text.split()]
                self.assertEqual(drawn_words, hook.split(),
                                 "Every hook word must be drawn, in order, without ellipsis")


if __name__ == "__main__":
    unittest.main()
