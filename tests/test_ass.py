"""Exact-spec word-pop ASS + upload SRT tests (no binaries, no network)."""
import re
import tempfile
import unittest
from pathlib import Path

from engine.ass import (WORDPOP_POP, _wordpop_groups, wordpop_ts,
                        write_wordpop_ass)
from engine.srt import srt_ts, write_upload_srt

POP_TAG = r"{\an5\fscx70\fscy70\t(0,90,\fscx112\fscy112)\t(90,160,\fscx100\fscy100)}"
YELLOW = r"{\c&H0000FFFF&}"


def _words(text, start=0.0, step=0.4, span=0.3):
    toks = text.split()
    return [{"word": w, "start": round(start + i * step, 3),
             "end": round(start + i * step + span, 3)}
            for i, w in enumerate(toks)]


def _dialogues(ass_text):
    return [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue:")]


def _dlg_times(dlg):
    m = re.match(r"Dialogue: 0,([^,]+),([^,]+),", dlg)
    assert m, dlg
    return m.group(1), m.group(2)


class TestWordpopTs(unittest.TestCase):
    def test_formatter_cases(self):
        self.assertEqual(wordpop_ts(3.2), "0:00:03.20")
        self.assertEqual(wordpop_ts(3725.5), "1:02:05.50")
        self.assertEqual(wordpop_ts(0), "0:00:00.00")
        self.assertEqual(wordpop_ts(61.05), "0:01:01.05")


class TestWordpopHeader(unittest.TestCase):
    def test_byte_exact_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(_words("elephant giraffe"), out)
            text = out.read_text(encoding="utf-8")
        head, _, _events = text.partition("[Events]")
        self.assertIn("ScriptType: v4.00+", head)
        self.assertIn("PlayResX: 1080", head)
        self.assertIn("PlayResY: 1920", head)
        self.assertIn("WrapStyle: 2", head)
        self.assertIn("ScaledBorderAndShadow: yes", head)
        self.assertIn(
            "Style: Pop,Montserrat ExtraBold,120,&H00FFFFFF,&H00FFFFFF,"
            "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,3,5,60,60,0,1",
            head)


class TestWordpopEvents(unittest.TestCase):
    def test_long_words_one_event_each(self):
        words = _words("elephant giraffe kangaroo")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(words, out)
            dlgs = _dialogues(out.read_text(encoding="utf-8"))
        self.assertEqual(len(dlgs), 3)

    def test_short_words_grouped_max_three(self):
        words = _words("a big cat sat on a mat")
        groups = _wordpop_groups(words)
        for g in groups:
            self.assertLessEqual(len(g), 3)
            self.assertLessEqual(
                len(" ".join(x["word"] for x in g)), 16)  # ~14 + tolerance
        self.assertEqual(sum(len(g) for g in groups), len(words))

    def test_start_before_end_no_overlaps(self):
        words = _words("elephant giraffe kangaroo ostrich")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(words, out)
            dlgs = _dialogues(out.read_text(encoding="utf-8"))
        prev_end = None
        for dlg in dlgs:
            s, e = _dlg_times(dlg)
            self.assertLess(s, e)
            if prev_end is not None:
                self.assertGreaterEqual(s, prev_end)  # chained, no overlap
            prev_end = e
        # gapless: each end == next start
        for first, second in zip(dlgs, dlgs[1:]):
            self.assertEqual(_dlg_times(first)[1], _dlg_times(second)[0])

    def test_last_event_word_end_plus_point15(self):
        words = _words("elephant giraffe", start=1.0, step=1.0, span=0.8)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(words, out)
            _s, e = _dlg_times(_dialogues(
                out.read_text(encoding="utf-8"))[-1])
        self.assertEqual(e, "0:00:02.95")  # 1.8 + 0.15

    def test_override_tags_on_every_event(self):
        words = _words("elephant giraffe kangaroo")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(words, out)
            dlgs = _dialogues(out.read_text(encoding="utf-8"))
        self.assertTrue(dlgs)
        for dlg in dlgs:
            self.assertIn(POP_TAG, dlg)

    def test_first_caption_after_card_end(self):
        words = _words(" ".join(f"word{i}" for i in range(20)),
                       start=0.0, step=0.5, span=0.4)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(words, out, start_after=3.8)
            dlgs = _dialogues(out.read_text(encoding="utf-8"))
        self.assertTrue(dlgs)
        first_start = _dlg_times(dlgs[0])[0]
        self.assertGreaterEqual(first_start, "0:00:03.80")

    def test_drop_events_fully_before_card(self):
        words = _words("alpha beta", start=0.0, step=0.4, span=0.3)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(words, out, start_after=99.0)
            dlgs = _dialogues(out.read_text(encoding="utf-8"))
        self.assertEqual(dlgs, [])

    def test_gap_over_point4_filled_by_extension(self):
        words = ([{"word": "elephant", "start": 0.0, "end": 0.3}]
                 + [{"word": "giraffe", "start": 5.0, "end": 5.3}])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(words, out)
            dlgs = _dialogues(out.read_text(encoding="utf-8"))
        self.assertEqual(len(dlgs), 2)
        self.assertEqual(_dlg_times(dlgs[0])[1], "0:00:05.00")


class TestWordpopHighlight(unittest.TestCase):
    def _one_word_ass(self, word, keys=None, upper=False):
        # long words stay solo so each event tests exactly one word
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(
                [{"word": word, "start": 0.0, "end": 0.5}], out,
                key_words=keys, upper=upper)
            return _dialogues(out.read_text(encoding="utf-8"))[0]

    def test_yellow_on_numbers_keywords_caps(self):
        self.assertIn(YELLOW, self._one_word_ass("profit300"))
        self.assertIn(YELLOW, self._one_word_ass("elephant", keys={"elephant"}))
        self.assertIn(YELLOW, self._one_word_ass("ELEPHANT"))
        self.assertIn(r"{\r}", self._one_word_ass("profit300"))

    def test_plain_word_has_no_yellow(self):
        dlg = self._one_word_ass("giraffe")
        self.assertNotIn(YELLOW, dlg)
        self.assertIn("giraffe", dlg)

    def test_upper_flag(self):
        dlg = self._one_word_ass("giraffe", upper=True)
        self.assertIn("GIRAFFE", dlg)

    def test_escape_braces_backslash_newline(self):
        dlg = self._one_word_ass("a{b}\\c")
        self.assertIn(r"a\{b\}\\c", dlg)
        self.assertNotIn("a{b}", dlg)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            write_wordpop_ass(
                [{"word": "line1\nline2", "start": 0.0, "end": 0.5}], out)
            body = _dialogues(out.read_text(encoding="utf-8"))[0]
        self.assertIn(r"line1\Nline2", body)


class TestUploadSrt(unittest.TestCase):
    def test_srt_formatter(self):
        self.assertEqual(srt_ts(3.2), "00:00:03,200")
        self.assertEqual(srt_ts(3725.5), "01:02:05,500")

    def test_cues_six_to_eight_words_indexed(self):
        words = _words(" ".join(f"w{i}" for i in range(20)),
                       start=0.0, step=0.5, span=0.4)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "u.srt"
            write_upload_srt(words, out)
            text = out.read_text(encoding="utf-8")
        blocks = [b for b in text.strip().split("\n\n") if b.strip()]
        self.assertTrue(all(6 <= len(b.splitlines()[2].split()) <= 8
                            for b in blocks))
        self.assertEqual([b.splitlines()[0] for b in blocks],
                         [str(n) for n in range(1, len(blocks) + 1)])
        for b in blocks:
            self.assertRegex(b.splitlines()[1],
                             r"\d\d:\d\d:\d\d,\d\d\d --> \d\d:\d\d:\d\d,\d\d\d")


if __name__ == "__main__":
    unittest.main()
