"""Plain SRT writer (upload captions). Style-free; pairs with ass.py."""
from __future__ import annotations

import math
import re
from pathlib import Path


def srt_ts(sec: float) -> str:
    """Seconds -> SRT timestamp HH:MM:SS,mmm."""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - math.floor(sec)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def group_words(words: list[dict], short_len: int = 4,
                max_group: int = 3) -> list[list[dict]]:
    """Group 1 short word with following shorts (<=max_group); punctuation kept."""
    groups: list[list[dict]] = []
    i = 0
    words = [dict(w) for w in (words or [])]
    while i < len(words):
        w = str(words[i].get("word", ""))
        if len(w.strip(".,!?\"'")) <= short_len and i + 1 < len(words):
            j = min(i + max_group, len(words))
            k = i + 1
            while k < j and len(str(words[k].get("word", "")).strip(".,!?\"'")) <= short_len:
                k += 1
            take = k if k > i + 1 else i + 2
            groups.append(words[i:take])
            i = take
        else:
            groups.append([words[i]])
            i += 1
    return groups


def cues_from_words(words: list[dict], gapless: bool = True) -> list[tuple[float, float, str]]:
    """Words -> (start, end, text) cues; gapless sets end=next.start."""
    groups = group_words(words)
    cues: list[tuple[float, float, str]] = []
    for gi, g in enumerate(groups):
        start = float(g[0].get("start", 0.0))
        if gapless and gi + 1 < len(groups):
            end = float(groups[gi + 1][0].get("start", g[-1].get("end", start)))
        else:
            end = float(g[-1].get("end", start + 0.4))
        if end <= start:
            end = start + 0.2
        cues.append((start, end, " ".join(str(x.get("word", "")) for x in g)))
    return cues


def _plain(text: str) -> str:
    """Strip ASS override tags for the upload SRT."""
    return re.sub(r"\{\\[^}]*\}", "", text)


def write_srt(words: list[dict], out_srt: str | Path,
              gapless: bool = True) -> Path:
    """Write one cue per word group; returns the .srt path."""
    out = Path(out_srt)
    cues = cues_from_words(words, gapless=gapless)
    lines: list[str] = []
    for n, (s, e, text) in enumerate(cues, 1):
        lines += [str(n), f"{srt_ts(s)} --> {srt_ts(e)}", _plain(text), ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return out


def _upload_groups(words: list[dict], per: int = 7) -> list[list[dict]]:
    """Chunk words into ~6-8-word cues, distributed evenly.

    Single short cue when the input is tiny; near-totals that admit no
    exact 6-8 split land within ±1 word.
    """
    clean = [dict(w) for w in (words or []) if str(w.get("word", "")).strip()]
    n = len(clean)
    if not n:
        return []
    count = max(1, round(n / per))
    base, extra = divmod(n, count)
    groups: list[list[dict]] = []
    i = 0
    for k in range(count):
        take = base + (1 if k < extra else 0)
        groups.append(clean[i:i + take])
        i += take
    return groups


def write_upload_srt(words: list[dict], out_srt: str | Path) -> Path:
    """Upload-only SRT: ~6-8 words per cue, natural word timings.

    Never burned in; pairs with the word-pop ASS from ass.py.
    """
    out = Path(out_srt)
    lines: list[str] = []
    for num, g in enumerate(_upload_groups(words), 1):
        s = float(g[0].get("start", 0.0))
        e = float(g[-1].get("end", s + 0.4))
        if e <= s:
            e = s + 0.2
        lines += [str(num), f"{srt_ts(s)} --> {srt_ts(e)}",
                  " ".join(str(x.get("word", "")) for x in g), ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return out
