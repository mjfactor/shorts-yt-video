"""Spec-exact opening card tests (Pillow only, no binaries, no network)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import overlay_card
from niches.story import visuals as story_visuals
from niches.story.config import StoryConfig


def _fake_opener(mapping):
    """Fake ImageFont.truetype: basename -> (family, style) or raise."""

    class _FakeFont:
        def __init__(self, name):
            base = Path(name).name
            if base not in mapping:
                raise OSError(f"no such font: {name}")
            self._name = mapping[base]

        def getname(self):
            return self._name

    def _open(path, size=40):
        return _FakeFont(str(path))

    return _open

CARD_TEXT = "I said no, then they moved my desk"
HIGHLIGHT = "desk"
HANDLE = "@mystery"


def _render(text: str = CARD_TEXT, highlight_word: str = HIGHLIGHT,
            handle: str = HANDLE, avatar_path: str = "no-such-avatar.png"):
    tmp = tempfile.TemporaryDirectory()
    out = Path(tmp.name) / "spec_card.png"
    result = overlay_card.render_spec_card(
        text, highlight_word, handle=handle, avatar_path=avatar_path,
        out_png=out)
    return tmp, result


def _red_count(img):
    px = img.convert("RGBA")
    return sum(1 for p in px.getdata()
               if p[0] > 180 and p[1] < 80 and p[2] < 80 and p[3] > 128)


def _opaque_count(img):
    px = img.convert("RGBA")
    return sum(1 for p in px.getdata() if p[3] > 128)


class TestSpecCard(unittest.TestCase):
    def test_canvas_rgba_width_bounded(self):
        tmp, result = _render()
        try:
            from PIL import Image

            with Image.open(result) as img:
                self.assertEqual(img.mode, "RGBA")
                self.assertEqual(img.width, 900)
                # dynamic height, bounded by the 1080x1920 canvas
                self.assertGreater(img.height, 200)
                self.assertLessEqual(img.height, 1920)
                # no text overflow: drawn alpha bbox stays inside the canvas
                bbox = img.getchannel("A").getbbox()
                assert bbox is not None
                self.assertGreaterEqual(bbox[0], 0)
                self.assertGreaterEqual(bbox[1], 0)
                self.assertLessEqual(bbox[2], 900)
                self.assertLessEqual(bbox[3], img.height)
        finally:
            tmp.cleanup()

    def test_highlight_word_red_pixel_present(self):
        tmp, result = _render()
        try:
            from PIL import Image

            with Image.open(result) as img:
                self.assertGreater(_red_count(img), 0)
        finally:
            tmp.cleanup()

    def test_highlight_case_insensitive(self):
        tmp, result = _render(highlight_word="DESK")
        try:
            from PIL import Image

            with Image.open(result) as img:
                self.assertGreater(_red_count(img), 0)
        finally:
            tmp.cleanup()

    def test_avatar_circle_present(self):
        tmp, result = _render()
        try:
            from PIL import Image

            with Image.open(result) as img:
                # top-left avatar region (36,36,156,156) holds opaque pixels
                region = img.crop((36, 36, 156, 156))
                self.assertGreater(_opaque_count(region), 5000)
                # corners outside the ellipse stay transparent
                corner_alpha = img.convert("RGBA").tobytes()[3]
                self.assertLess(corner_alpha, 128)
        finally:
            tmp.cleanup()

    def test_heart_reads_as_heart(self):
        # Drawn alone: two-lobe top with dip, tapering point, symmetric.
        # The old 44px outline shape fails the width floor below.
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        overlay_card._draw_spec_heart(d, 28, 28, color=(150, 150, 155, 255))
        alpha = img.getchannel("A")
        bbox = alpha.getbbox()
        assert bbox is not None
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        self.assertGreaterEqual(w, 56)
        self.assertGreaterEqual(h, 56)
        px = img.convert("RGBA").tobytes()
        W = img.width

        def opaque(x, y):
            return px[(y * W + x) * 4 + 3] > 128

        # top-center dip: upper-third middle column mostly transparent
        dip = sum(1 for y in range(bbox[1], bbox[1] + h // 3)
                  if not opaque(bbox[0] + w // 2, y))
        self.assertGreater(dip, (h // 3) // 2)
        # lobes present: upper-third left and right quarters opaque
        for q in (1, 3):
            x0 = bbox[0] + (w * q) // 4
            lobe = sum(1 for y in range(bbox[1], bbox[1] + h // 3)
                       for x in range(x0 - 3, x0 + 4) if opaque(x, y))
            self.assertGreater(lobe, 0)
        # taper: bottom-quarter opaque width < middle opaque width
        def run_width(y):
            xs = [x for x in range(bbox[0], bbox[2]) if opaque(x, y)]
            return (max(xs) - min(xs) + 1) if xs else 0
        self.assertLess(run_width(bbox[1] + h * 7 // 8),
                        run_width(bbox[1] + h * 2 // 5))
        # left/right symmetry within 15%
        left = sum(1 for y in range(bbox[1], bbox[3])
                   for x in range(bbox[0], bbox[0] + w // 2) if opaque(x, y))
        right = sum(1 for y in range(bbox[1], bbox[3])
                    for x in range(bbox[0] + w // 2, bbox[2]) if opaque(x, y))
        total = left + right
        self.assertGreater(total, 0)
        self.assertLess(abs(left - right) / total, 0.15)

    def test_worst_case_eight_words_fit_at_floor(self):
        # Eight 12-char words must fit in 3 lines (32px floor), never clip.
        from engine.overlay_card import _spec_wrap_text

        worst = " ".join(["abcdefghijkl"] * 8)
        lines, _font, size, _lh = _spec_wrap_text(
            worst, "", 684, dict(overlay_card.SPEC_CARD_THEME))
        self.assertLessEqual(len(lines), 3)
        self.assertGreaterEqual(size, 32)

    def test_missing_avatar_fallback_initial(self):
        # nonexistent path -> solid circle + handle first letter, no crash
        tmp, result = _render(avatar_path="definitely/missing.png",
                              handle="@mystery")
        try:
            from PIL import Image

            with Image.open(result) as img:
                region = img.crop((36, 36, 156, 156))
                self.assertGreater(_opaque_count(region), 5000)
        finally:
            tmp.cleanup()

    def test_handle_default_unhinged(self):
        self.assertEqual(StoryConfig().channel_handle, "@UnhingedStories")
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("STORY_CHANNEL_HANDLE", "CHANNEL_HANDLE"):
                os.environ.pop(k, None)
            self.assertEqual(StoryConfig.from_env().channel_handle,
                             "@UnhingedStories")

    def test_find_bundled_font_internal_match(self):
        mapping = {
            "Montserrat-ExtraBold.ttf": ("Montserrat", "ExtraBold"),
            "Other.ttf": ("SomethingElse", "Regular"),
        }
        opener = _fake_opener(mapping)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "Montserrat-ExtraBold.ttf").write_bytes(b"fake")
            (d / "Other.ttf").write_bytes(b"fake")
            (d / "notes.txt").write_bytes(b"fake")
            hit = overlay_card.find_bundled_font(
                "Montserrat ExtraBold", font_dir=d, _opener=opener)
            self.assertEqual(hit, d / "Montserrat-ExtraBold.ttf")

    def test_find_bundled_font_family_and_subfamily_forms(self):
        # Family-only, subfamily-only, combined, and split-token queries
        # must all resolve the same cut (getname()[1] carries the weight).
        mapping = {"M.ttf": ("Montserrat", "ExtraBold")}
        opener = _fake_opener(mapping)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "M.ttf").write_bytes(b"fake")
            for needles in (("Montserrat",), ("ExtraBold",),
                            ("Montserrat ExtraBold",),
                            ("Montserrat", "ExtraBold")):
                hit = overlay_card.find_bundled_font(
                    *needles, font_dir=d, _opener=opener)
                self.assertEqual(hit, d / "M.ttf", f"needles={needles}")

    def test_find_bundled_font_tolerant_getname_shape(self):
        # 1- and 3-tuple getname() shapes must not crash the matcher.
        mapping = {"One.ttf": ("Solo",), "Three.ttf": ("Tri", "Bold", "X")}
        opener = _fake_opener(mapping)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "One.ttf").write_bytes(b"fake")
            (d / "Three.ttf").write_bytes(b"fake")
            self.assertEqual(
                overlay_card.find_bundled_font(
                    "Solo", font_dir=d, _opener=opener),
                d / "One.ttf")
            self.assertEqual(
                overlay_card.find_bundled_font(
                    "Tri", "Bold", font_dir=d, _opener=opener),
                d / "Three.ttf")

    def test_find_bundled_font_filename_alone_never_matches(self):
        # internal name rules: a Montserrat filename with an Arial
        # interior must NOT match.
        mapping = {"fonts/Fake.ttf": ("Arial", "Regular")}
        opener = _fake_opener(mapping)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "Montserrat-ExtraBold.ttf").write_bytes(b"fake")
            self.assertIsNone(overlay_card.find_bundled_font(
                "Montserrat ExtraBold", font_dir=d, _opener=opener))

    def test_fallback_preserved_without_bundled_font(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertIsNone(overlay_card.find_bundled_font(
                "Montserrat ExtraBold", font_dir=d))
            # card font lookup still resolves (system/Pillow fallback)
            font = overlay_card.load_font(40, bold=True)
            self.assertIsNotNone(font)

    def test_render_card_api_stable(self):
        # legacy entry point keeps its signature for old callers
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "legacy.png"
            result = overlay_card.render_card(
                CARD_TEXT, HIGHLIGHT, handle=HANDLE,
                avatar_path="no-such-avatar.png", out_png=out)
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 0)

    def test_share_reads_as_forward_share(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        overlay_card._draw_spec_share(d, 20, 20, color=(150, 150, 155, 255), size=44)
        bbox = img.getchannel("A").getbbox()
        assert bbox is not None
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        self.assertGreaterEqual(w, 36)
        self.assertGreaterEqual(h, 36)

    def test_custom_icon_stitching(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            tmp_d = Path(tmp)
            # Create a bright green test icon
            green_icon = tmp_d / "custom_heart.png"
            test_img = Image.new("RGBA", (48, 48), (0, 255, 0, 255))
            test_img.save(green_icon)

            out = tmp_d / "stitched_card.png"
            custom_theme = dict(overlay_card.SPEC_CARD_THEME)
            custom_theme["heart_icon_path"] = str(green_icon)
            overlay_card.render_spec_card(
                CARD_TEXT, HIGHLIGHT, handle=HANDLE,
                avatar_path="no-such-avatar.png", out_png=out,
                theme=custom_theme)

            with Image.open(out) as card_img:
                px = card_img.convert("RGBA")
                # Bright green pixel should be present from the stitched icon
                has_green = any(p[0] < 50 and p[1] > 200 and p[2] < 50 and p[3] > 128
                                for p in px.getdata())
                self.assertTrue(has_green)

    def test_upvote_reads_as_arrow(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        overlay_card._draw_spec_upvote(d, 20, 20, color=(255, 69, 0, 255), size=44)
        bbox = img.getchannel("A").getbbox()
        assert bbox is not None
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        self.assertGreaterEqual(w, 30)
        self.assertGreaterEqual(h, 35)

    def test_satire_colors_present(self):
        tmp, result = _render()
        try:
            from PIL import Image

            with Image.open(result) as img:
                px = img.convert("RGBA")
                # Orange upvote pixels
                has_orange = any(p[0] > 220 and p[1] > 50 and p[1] < 120 and p[2] < 50 and p[3] > 128
                                 for p in px.getdata())
                # Blue share pixels
                has_blue = any(p[0] < 60 and p[1] > 120 and p[2] > 200 and p[3] > 128
                               for p in px.getdata())
                self.assertTrue(has_orange)
                self.assertTrue(has_blue)
        finally:
            tmp.cleanup()


class TestSpecCardFilter(unittest.TestCase):
    def test_input_args_exact(self):
        args = story_visuals._card_input_args(Path("card.png"), 4.0)
        self.assertEqual(args[:-1], ["-loop", "1", "-t", "4.00", "-i"])
        self.assertTrue(str(args[-1]).endswith("card.png"))

    def test_filter_string_exact(self):
        self.assertEqual(
            story_visuals._card_filter(4.0),
            "[card]format=rgba,"
            "fade=t=in:st=0:d=0.2:alpha=1,"
            "fade=t=out:st=3.75:d=0.25:alpha=1[c];"
            "[base][c]overlay=x=(W-w)/2:y=H*0.30:"
            "enable='between(t,0,4.00)'[v1]")

    def test_filter_y_mapping_above_center(self):
        # y=H*0.30 = 576 top on 1920 canvas (was (H-h)/2-280 = 480 top);
        # card center stays above canvas center either way.
        filt = story_visuals._card_filter(3.0)
        self.assertIn("y=H*0.30", filt)
        self.assertNotIn("(H-h)/2", filt)


if __name__ == "__main__":
    unittest.main()
