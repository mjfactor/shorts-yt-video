"""Story stage 1: Reddit-style micro-story Content via engine.gemini_client.

Moved from niche_story.py and extended for the 100-120s niche: re-hook
beats at 20-30s + 50-60s, word budget from TARGET * measured wps,
sentence/TTS/safety/copy gates, retry with backoff.
"""
from __future__ import annotations

import math
import random
import re
import time
from datetime import date, datetime, timezone

from engine import gemini_client as _gc
from engine.ffmpeg_utils import log, warn
from niches.base import Content

from .config import (DEFAULT_GENRES, DEFAULT_TEXT_MODELS, MODE,
                     StoryConfig)

AVOID_INTRO = "Already-aired stories — do NOT reuse these titles or premises:\n"
AVOID_OUTRO = "Your story must have a DIFFERENT premise from every entry above. "

_TTS_BAD_RE = re.compile(r"[*#_`|~@^<>{}\[\]\\]")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
# Stage-direction leak guard: rehooks are metadata pointers, never spoken —
# the model must never voice labels like "Rehook line:" inside the story.
_REHOOK_LITERAL_RE = re.compile(r"re[\s-]*hook", re.IGNORECASE)
# Markers of copied Reddit material (never air these).
_COPY_RE = re.compile(r"\br/\w+|reddit\.com|u/\w+|AITA\?|TIFU|ELI5", re.IGNORECASE)
# Safety refusals: graphic/hate/age-restricted bait.
_UNSAFE_RE = re.compile(
    r"\b(nude|naked|porn|molest|rape\b|lynch|kill (him|her|them)|"
    r"suicide|self-?harm|gore\b|torture)\b", re.IGNORECASE)
# Stacked discourse markers ("I mean, honestly, I stared at my phone").
# "I mean" is a self-repair marker that promises a rephrase of what was just
# said; "honestly" hedges the clause that follows it. Stacked, each expectation
# goes unmet and the sentence reads as a subtitle error. The first marker must
# open a sentence (or follow a comma) so legitimate prose like "he did well,
# actually it worked" is not caught.
_MARKER = (r"(?:i mean|so yeah|well|honestly|basically|anyway|actually)")
_STACKED_MARKER_RE = re.compile(
    r"(?:^|(?<=[.!?…])\s|(?<=,)\s)" + _MARKER + r"\s*,?\s*" + _MARKER + r"\b",
    re.IGNORECASE)
# Cinematic reaction lines. These are the recurring stock phrases that show up
# in aired stories and make the narration read as a film scene instead of a
# real memory — viewers call them out as fabricated. The REALISM section of
# the Gemini prompt steers the model away from them; this gate enforces it.
_CLICHE_RE = re.compile(
    r"worth every (single )?(penny|cent|dollar)"
    r"|(my )?blood (went|ran|raced|turned) (cold|ice)"
    r"|looked (me|him|her|them|us) dead in the eye"
    r"|like nothing had happened"
    r"|(it was|that was) priceless"
    r"|(my )?world (turned|shattered) upside down"
    r"|(a )?chill (ran down|down) (my|his|her|their) spine"
    # order-tolerant: "time froze for a second", "in that moment time stopped",
    # and the inverted "for a moment, time froze" all read as film, not memory.
    r"|time (froze|stopped)( for (a|one|that) (moment|second|beat))?"
    r"|for (a|one) moment,? time (froze|stopped)",
    re.IGNORECASE)

MAX_SENTENCE_WORDS = 30

# ---- consecutive-repeat guards (hardcoded; not config knobs) ----------------
# How much of an aired story the model gets to see in the dedup block. 60
# chars only exposed the hook, so the model could see "My boss billed me ..."
# four times over without ever seeing that the PAYOFF was identical too.
HISTORY_PREMISE_CHARS = 200
# What history.json actually stores per entry. Story scripts run ~400 words;
# keeping them whole in the committed file made it grow without bound, so
# only this excerpt of the premise is persisted (plus key_words + skeleton,
# which is all the dedup guards read).
HISTORY_EXCERPT_CHARS = 200
# key_words echoed after the premise (visuals/dedup signal).
HISTORY_KEYWORD_MAX = 8
# Overlap (Jaccard over significant words) allowed against a story. 0.35 is
# the general window; the newest entries get a much tighter bar because the
# same 400-word skeleton restated with one object swapped stayed under 0.35.
OVERLAP_MAX = 0.35
RECENT_OVERLAP_MAX = 0.22
# Only the most recent N same-niche entries get skeleton / tight-overlap
# treatment; older ones stay on the looser general rules.
RECENT_GUARD_N = 3
# Premise skeleton reads the title plus the opening of the story.
SKELETON_HEAD_CHARS = 150
# The recurring "invoice/charge" family: an authority figure billing the
# narrator for something, then an office/HR/payroll payoff. Same skeleton,
# different object -> must not air twice running.
SKELETON_ACTORS = (
    "boss", "manager", "supervisor", "coworker", "coworker", "co-worker",
    "colleague", "neighbor", "neighbour", "roommate", "flatmate", "landlord",
    "barber", "contractor", "plumber", "mechanic", "dentist", "accountant",
    "tailor", "groomer", "nanny", "babysitter", "sitter", "client", "customer",
    "teacher", "professor", "coach", "trainer", "therapist", "banker", "agent",
)
# Money-grievance tokens, all folded into one class so "billed", "charged",
# "invoice" and "fee" phrasings of the same story share a skeleton key.
SKELETON_MONEY_RE = re.compile(
    r"\b(bill(?:ed|ing|s)?|charg(?:e|ed|es|ing)|invoice|invoices|fee|fees|"
    r"rent|deduct(?:ed|ion)|dock(?:ed)?|overcharg(?:e|ed))\b")


def _genres(cfg: StoryConfig) -> tuple[str, ...]:
    """Genre pool from config (STORY_GENRES) else defaults."""
    return tuple(cfg.story_genres) or DEFAULT_GENRES


def _pick_genre(history: list[dict], cfg: StoryConfig) -> str:
    """Deterministic rotation over same-niche count; never repeats last genre."""
    pool = _genres(cfg)
    same = [h for h in (history or []) if h.get("niche", "") == MODE]
    n = len(same)
    genre = pool[n % len(pool)]
    last = str(same[-1].get("genre", "")).casefold() if same else ""
    if genre.casefold() == last and len(pool) > 1:
        genre = pool[(n + 1) % len(pool)]
    return genre


def _gender_bucket(now: datetime | None = None) -> int:
    """UTC half-day bucket: (days since epoch) * 2 + (0 if hour < 12 else 1).

    Advances on its own, so the 10:00 and 22:00 UTC scheduled runs can
    never land in the same bucket. Pure clock math — no history, no I/O.
    """
    t = now or datetime.now(timezone.utc)
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc)
    return (t.date().toordinal() - date(1970, 1, 1).toordinal()) * 2 \
        + (0 if t.hour < 12 else 1)


# First-person narrator cues ONLY — never side characters ("my sister",
# "my mother") and never bare props ("engagement ring"). The model decides
# everything probabilistic; these regexes are the hard override.
_FEMALE_NARRATOR_CUES = (
    re.compile(r"\bmy husband\b", re.IGNORECASE),
    re.compile(r"\bmy husband-to-be\b", re.IGNORECASE),
    re.compile(r"\bmy boyfriend\b", re.IGNORECASE),
    re.compile(r"\bmy fianc(?:e|ee|\u00e9e)\b", re.IGNORECASE),
    re.compile(r"\b(?:as|i am|i'm) a woman\b", re.IGNORECASE),
    re.compile(r"\b(?:as|i am|i'm) (?:a )?wife\b", re.IGNORECASE),
    re.compile(r"\b(?:i was|i'm|i am) pregnant\b", re.IGNORECASE),
    re.compile(r"\bmy pregnancy\b", re.IGNORECASE),
    re.compile(r"\bmy period\b", re.IGNORECASE),
)
_MALE_NARRATOR_CUES = (
    re.compile(r"\bmy wife\b", re.IGNORECASE),
    re.compile(r"\bmy wife-to-be\b", re.IGNORECASE),
    re.compile(r"\bmy girlfriend\b", re.IGNORECASE),
    re.compile(r"\bmy fianc\u00e9\b", re.IGNORECASE),
    re.compile(r"\b(?:as|i am|i'm) a man\b", re.IGNORECASE),
    re.compile(r"\b(?:as|i am|i'm) (?:a )?husband\b", re.IGNORECASE),
    re.compile(r"\bmy beard\b", re.IGNORECASE),
)


def _infer_narrator_gender(story_text: str) -> str | None:
    """Infer gender only when the first-person story has a clear signal."""
    text = str(story_text or "")
    female = sum(bool(pattern.search(text))
                 for pattern in _FEMALE_NARRATOR_CUES)
    male = sum(bool(pattern.search(text))
               for pattern in _MALE_NARRATOR_CUES)
    if female > male:
        return "female"
    if male > female:
        return "male"
    return None


def _narrator_gender(history: list[dict], cfg: StoryConfig,
                      model_choice: str = "",
                      now: datetime | None = None,
                      story_text: str = "") -> str:
    """Resolve the narrator gender for this video.

    auto (default), in priority order:
      1. a clear first-person cue in `story_text` (hard regex override),
      2. the model's own `narrator_gender` choice ("male"/"female"),
      3. the UTC half-day bucket (even bucket female, odd bucket male).

    The clock fallback is pure and independent of history, so a frozen,
    empty, or malformed history can never pin the voice. Forced modes
    (male/female) win outright. `history` stays accepted for compatibility
    and is deliberately unused. Never raises.
    """
    mode = str(getattr(cfg, "narrator_gender_mode", "auto") or "auto") \
        .strip().lower()
    if mode in ("male", "female"):
        return mode
    inferred = _infer_narrator_gender(story_text)
    if inferred is not None:
        return inferred
    choice = str(model_choice or "").strip().lower()
    if choice in ("male", "female"):
        return choice
    return "female" if _gender_bucket(now) % 2 == 0 else "male"


def _premise_excerpt(text: str) -> str:
    """Single-line excerpt of a script: what history.json stores instead.

    Collapses all whitespace and cuts to HISTORY_EXCERPT_CHARS. Kept shared
    by the writer (history_dedup_payload) and every reader so both sides
    agree on the exact bytes, which is what lets old full-narration entries
    and new excerpted ones be compared against each other.
    """
    return " ".join(str(text or "").split())[:HISTORY_EXCERPT_CHARS]


def _entry_premise(h: dict) -> str:
    """Dedup premise text of a history entry, for ANY on-disk shape.

    New entries store the trimmed excerpt under `premise` (and mirror it in
    `narration` for backward compat); entries written before the trim still
    carry the whole script under `narration`. Prefer `premise`, fall back to
    `narration`. Never rewrites existing entries.
    """
    return str(h.get("premise") or h.get("narration") or "").strip()


def format_history_line(h: dict) -> str:
    """One-line history entry for the dedup prompt block.

    Shows the premise excerpt, the key_words, and the billed/fee skeleton
    when the entry has one. The skeleton is worth a slot in the prompt: it
    names the story SHAPE directly ("boss|money"), which is exactly what the
    model kept missing when four near-identical invoice stories aired in a
    row behind titles that merely swapped the object. Still one line.
    """
    premise = _premise_excerpt(_entry_premise(h))[:HISTORY_PREMISE_CHARS]
    kw = h.get("key_words") or h.get("items") or []
    if isinstance(kw, str):
        kw = [kw]
    kw_str = ",".join(str(k).strip()
                      for k in list(kw)[:HISTORY_KEYWORD_MAX]
                      if str(k).strip())
    line = (f"- \"{h.get('title', '?')}\" "
            f"(genre={h.get('genre', '?')}) premise: {premise}...")
    if kw_str:
        line = f"{line} keywords: {kw_str}"
    skel = _entry_skeleton(h)
    return f"{line} skeleton: {skel}" if skel else line


def history_dedup_payload(title: str, narration: str,
                          key_words: list | None = None) -> dict:
    """Trimmed dedup payload for one history.json entry (story lane).

    Replaces the full ~400-word script, which made the committed history file
    grow without bound. Everything the dedup guards actually read survives:
    the premise excerpt, the key_words, and the billed/fee skeleton. The
    `narration` key is kept (holding the excerpt, not the script) so anything
    still reading it keeps working.

    Existing entries are NEVER rewritten — the file shrinks on its own as
    HISTORY_KEEP rolls old full-narration entries out of the flat cap.
    """
    excerpt = _premise_excerpt(narration)
    kw = [str(k).strip() for k in (key_words or []) if str(k).strip()]
    return {"narration": excerpt, "premise": excerpt,
            "key_words": kw[:HISTORY_KEYWORD_MAX],
            "skeleton": _skeleton_key(title, excerpt) or ""}


def history_block(history: list[dict], prompt_n: int) -> str:
    """Recent same-niche entries the model must not repeat."""
    recent = [h for h in (history or [])[-prompt_n:]
              if h.get("niche", "") == MODE]
    if not recent:
        return ""
    return AVOID_INTRO + "\n".join(format_history_line(h) for h in recent) + "\n" + AVOID_OUTRO


def measure_wps(history: list[dict]) -> float | None:
    """End-to-end speech rate (words/sec) from history entries carrying both
    `words` and `duration`, or **None** when history has nothing measurable.

    **None is load-bearing, not a fallback.** `effective_wps` treats a
    truthy `wps` as MEASURED (used as-is, no multipliers) and a falsy one as
    ASSUMED (applies EDGE_RATE then LENGTH_OVERSHOOT off
    `cfg.history_wps`). Returning the STORY_HISTORY_WPS default from here
    made an uncalibrated install look calibrated, so the assumed branch was
    unreachable and every draft was budgeted at 3.5 wps — well under the
    real ~4.8, producing 71s videos instead of 100-120s.

    Entries missing either key (everything written before core recorded
    them) or carrying junk are skipped silently — never raises. A returned
    value is the rate of FINISHED videos, so it already includes the edge
    voice rate and the length overshoot; effective_wps must not re-apply
    either. Clamped 2.5-5.5 (real edge runs sit ~4.5-5.5).
    """
    rates = []
    for h in (history or [])[-30:]:
        if not isinstance(h, dict):
            continue
        try:
            w = float(h.get("words", 0))
            d = float(h.get("duration", 0))
            if w > 20 and d > 10:
                rates.append(w / d)
        except (TypeError, ValueError):
            continue
    if not rates:
        return None
    rates.sort()
    mid = rates[len(rates) // 2]
    return min(5.5, max(2.5, mid))


def parse_edge_rate_pct(rate: str | None) -> float:
    """'+20%' -> 20.0 (edge relative-rate percent); unparseable -> 0.0."""
    m = re.match(r"\s*([+-]?)(\d+(?:\.\d+)?)\s*%?\s*$", rate or "")
    if not m:
        return 0.0
    val = float(m.group(2))
    return -val if m.group(1) == "-" else val


def effective_wps(cfg: StoryConfig, wps: float | None) -> float:
    """Speech rate used to size the word budget, clamped to 2.5-5.5 wps.

    Two branches, and the distinction matters:

    * `wps` supplied (it came from measure_wps over history entries that
      carry words+duration) is an END-TO-END rate measured off finished
      videos, so the edge relative rate and the length overshoot are
      already baked into it. It is used as-is: no edge multiplier, no
      overshoot. Re-applying them double-counted and made drafts
      overshoot the length ceiling by ~30%.
    * `wps` falsy (nothing measurable in history yet) falls back to
      STORY_HISTORY_WPS, which is an ASSUMED rate, so today's shape is
      kept: STORY_EDGE_RATE (+20% / +28% female) then
      STORY_LENGTH_OVERSHOOT (clamped 1.0-1.20).
    """
    try:
        base = float(wps) if wps else float(cfg.history_wps)
    except (TypeError, ValueError):
        base = float(cfg.history_wps)
    try:
        over = float(getattr(cfg, "length_overshoot", 1.12))
    except (TypeError, ValueError):
        over = 1.12
    over = min(1.20, max(1.0, over))
    if wps:
        return min(5.5, max(2.5, base))
    edge_pct = parse_edge_rate_pct(getattr(cfg, "edge_rate", "+20%"))
    scaled = base * (1.0 + edge_pct / 100.0)
    return min(5.5, max(2.5, scaled)) * over


def target_words(cfg: StoryConfig, wps: float | None) -> tuple[int, int]:
    """(lo, hi) word budget from TARGET_MIN/MAX * effective wps.

    Two shapes depending on whether `wps` is measured (see effective_wps):

    * calibrated (`wps` a real measurement, ~4.7 wps off finished videos):
      ~470-564 words -> ~99-119s.
    * uncalibrated (`measure_wps` returned None, HISTORY_WPS 3.5 with edge
      +20% -> 4.2, overshoot 1.12 -> 4.704): 470-564 words -> ~99-119s too.

    Both land in band. The two paths used to disagree because measure_wps
    returned its 3.5 default instead of None, so the uncalibrated branch
    never ran and every draft was budgeted at 3.5 wps (~350-420 words ->
    71-78s videos).
    """
    eff = effective_wps(cfg, wps)
    return (max(60, round(cfg.target_min * eff)),
            min(600, round(cfg.target_max * eff)))


def _str_list(v: object) -> list[str]:
    """Normalize a JSON value to a list of non-empty strings."""
    if not isinstance(v, list):
        return []
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def _check_sentences(story: str) -> str | None:
    """Longest-sentence gate (<=30 words); returns reason or None."""
    for sent in re.split(r"[.!?…]+", story):
        if len(sent.split()) > MAX_SENTENCE_WORDS:
            return (f"sentence over {MAX_SENTENCE_WORDS} words: "
                    f"'{sent.strip()[:60]}...'")
    return None


def _norm_card_words(s: str) -> list[str]:
    """Lowercased words, punctuation stripped, for card-line matching."""
    return [w.strip(".,!?\"'…").lower() for w in s.split()
            if w.strip(".,!?\"'…")]


def _starts_with_card(story: str, card_text: str) -> bool:
    """First sentence of story_text matches card_text (case-insensitive,
    trailing punctuation ignored). Empty card_text always matches."""
    if not _norm_card_words(card_text):
        return True
    first = re.split(r"[.!?…]+", story.strip(), maxsplit=1)[0]
    return _norm_card_words(first) == _norm_card_words(card_text)


_MICRO_WORDS = 5
_MICRO_FRAC = 0.02
# Under-floor grace: drafts up to this many words under the strict floor
# (lo - 15) still air with a warning instead of burning a retry or
# failing the run (~5s short at edge pace; 347/350 admitted, 243 still
# expands). Ceiling side keeps the plain micro margin only.
_MICRO_FLOOR_WORDS = 20


def _micro_margin(lo: int, hi: int) -> int:
    """Near-miss tolerance: max(5 words, 2% of the window mid)."""
    mid = (lo + hi) / 2
    return max(_MICRO_WORDS, math.ceil(_MICRO_FRAC * mid))


def _is_near_miss(n: int, lo: int, hi: int, tol: int = 15) -> bool:
    """True when n sits just outside the strict [lo-tol, hi+tol] window
    but inside the micro margin — accept without a retry call.

    The floor side uses the under-floor grace band (up to
    _MICRO_FLOOR_WORDS under); the ceiling side keeps the micro margin.
    """
    s_lo, s_hi = lo - tol, hi + tol
    if s_lo <= n <= s_hi:
        return False
    m = _micro_margin(lo, hi)
    return (s_lo - max(m, _MICRO_FLOOR_WORDS)) <= n <= (s_hi + m)


def validate_extra(data: dict, cfg: StoryConfig | None = None,
                   wps: float | None = None, *, micro_accept: bool = False) -> dict:
    """Strict-check the story payload. Raises ValueError when unusable.

    `micro_accept=True` widens the word-count gate: the ceiling keeps the
    micro margin (max(5 words, 2% of mid)) while the floor admits drafts up
    to _MICRO_FLOOR_WORDS under it with a warning, so near-miss drafts
    (e.g. 347/350 vs a 361 floor) air a few seconds short instead of
    burning a retry call. Every other gate is enforced unchanged.
    """
    if not isinstance(data, dict):
        raise ValueError("story output is not a JSON object")
    for key in ("title", "description", "tags", "beats"):
        if not data.get(key):
            raise ValueError(f"missing/invalid '{key}'")
    story = data.get("narration") or data.get("story_text")
    if not isinstance(story, str) or not story.strip():
        raise ValueError("missing/invalid 'story_text'/'narration'")
    story = story.strip()
    words = len(story.split())
    if cfg is not None:
        lo, hi = target_words(cfg, wps)
        s_lo, s_hi = lo - 15, hi + 15
        if micro_accept:
            m = _micro_margin(lo, hi)
            s_lo, s_hi = s_lo - max(m, _MICRO_FLOOR_WORDS), s_hi + m
    else:
        lo, hi = 80, 160
        s_lo, s_hi = lo, hi
    if words < s_lo or words > s_hi:
        raise ValueError(f"'story_text' out of range ({words} words, need {lo - 15 if cfg is not None else lo}-{hi + 15 if cfg is not None else hi})")
    if cfg is not None and micro_accept and words < lo - 15:
        warn(f"'story_text' {words} words is under the {lo - 15}-word floor "
             f"(grace to {lo - 15 - _MICRO_FLOOR_WORDS}) — airing with "
             f"warning; narration runs a few seconds short.")
    for field in ("story_text", "hook"):
        text = story if field == "story_text" else str(data.get(field, "") or "")
        bad = _TTS_BAD_RE.search(text) or _EMOJI_RE.search(text)
        if bad:
            raise ValueError(f"TTS-hostile character {bad.group()!r} in '{field}'")
    title = str(data.get("title", "") or "")
    if not _EMOJI_RE.search(title):
        raise ValueError("'title' must contain 1-2 emojis")
    if _COPY_RE.search(story):
        raise ValueError("story looks copied from Reddit (markers found)")
    unsafe = _UNSAFE_RE.search(story)
    if unsafe:
        raise ValueError(f"safety refusal trigger {unsafe.group()!r} in story")
    cliche = _CLICHE_RE.search(story)
    if cliche:
        raise ValueError(f"cliche reaction line {cliche.group()!r} in story "
                         "— use concrete behavior instead")
    long_sent = _check_sentences(story)
    if long_sent:
        raise ValueError(long_sent)
    stacked = _STACKED_MARKER_RE.search(story)
    if stacked:
        raise ValueError(
            f"stacked discourse marker {stacked.group().strip()!r} in story — "
            "one marker per sentence; 'I mean' must rephrase, not prefix")
    hook = str(data.get("hook", "") or "").strip()
    if hook and not story.startswith(hook[:20]):
        raise ValueError("'hook' must be the opening line of 'story_text'")
    card_text = str(data.get("card_text", "") or "").strip()
    if card_text and not _starts_with_card(story, card_text):
        raise ValueError("'story_text' must open with the card line "
                         "verbatim as its first sentence")
    if card_text and len(card_text.split()) > 8:
        raise ValueError(f"'card_text' must be <=8 words "
                         f"(got {len(card_text.split())})")
    key_words = _str_list(data.get("key_words"))
    ending = str(data.get("ending_type", "") or "").strip().lower()
    if ending not in ("payoff", "cliffhanger"):
        raise ValueError("'ending_type' must be 'payoff' or 'cliffhanger'")
    gender = str(data.get("narrator_gender", "") or "").strip().lower()
    if gender not in ("male", "female"):
        raise ValueError("'narrator_gender' must be 'male' or 'female'")
    want_slots = ("20-30s", "50-60s", "80-90s")
    rehooks = []
    for rh in (data.get("rehooks") or []):
        if not isinstance(rh, dict):
            raise ValueError("each 'rehooks' entry must be an object")
        at = str(rh.get("at", "")).strip()
        line = str(rh.get("line", "")).strip()
        if at not in want_slots:
            raise ValueError("'rehooks[].at' must be '20-30s', '50-60s' "
                             "or '80-90s'")
        if not line or line not in story:
            raise ValueError("'rehooks[].line' must be a line from the story")
        rehooks.append({"at": at, "line": line})
    if len(rehooks) != 3:
        raise ValueError("'rehooks' must hold exactly 3 re-hooks "
                         "(20-30s, 50-60s and 80-90s)")
    if sorted(r["at"] for r in rehooks) != sorted(want_slots):
        raise ValueError("'rehooks' must cover each slot once: 20-30s, "
                         "50-60s and 80-90s")
    beats = data.get("beats") or []
    joined = " ".join(str(b.get("text", "")) for b in beats
                      if isinstance(b, dict)).strip()
    if " ".join(joined.split()) != " ".join(story.split()):
        raise ValueError("beat texts joined must equal narration exactly")
    for label, snippet in (
            ("story_text", story),
            *[(f"beats[{i}].text", str(b.get("text", "")))
              for i, b in enumerate(beats) if isinstance(b, dict)],
            *[(f"rehooks[{i}].line", r["line"])
              for i, r in enumerate(rehooks)]):
        hit = _REHOOK_LITERAL_RE.search(snippet)
        if hit:
            raise ValueError(
                f"stage direction {hit.group()!r} in '{label}' "
                "(rehooks are metadata-only, never spoken)")
    extra = {
        "title": str(data.get("title", "") or "").strip(),
        "description": str(data.get("description", "") or "").strip(),
        "tags": _str_list(data.get("tags")),
        "items": key_words,
        "key_words": key_words,
        "genre": str(data.get("genre", "") or "").strip(),
        "card_text": str(data.get("card_text", "") or "").strip(),
        "card_highlight_word": str(data.get("card_highlight_word", "") or "").strip(),
        "hook": hook,
        "story_text": story,
        "narration": story,
        "ending_type": ending,
        "narrator_gender": gender,
        "pinned_comment": str(data.get("pinned_comment", "") or "").strip(),
        "rehooks": rehooks,
        "beats": [{"text": str(b.get("text", "")).strip(), "label": ""}
                  for b in beats if isinstance(b, dict)],
    }
    if isinstance(data.get("part_2_seed"), str) and data["part_2_seed"].strip():
        extra["part_2_seed"] = data["part_2_seed"].strip()
    return extra


def sanitize_title(title: str, slots: dict | None = None) -> str:
    """Hook-driven title, <=100 chars, with ' #shorts' when it fits.

    Emojis are preserved (only whitespace/hashtags/trailing punctuation
    are normalized) so the required 1-2 title emojis survive upload.
    """
    t = re.sub(r"\s+", " ", (title or "")).strip()
    t = re.sub(r"#\S+", "", t).strip()
    t = re.sub(r"\s*[–—\-:|;,.]+\s*$", "", t).strip()
    if len(t) > 100:
        cut = t[:100].rsplit(" ", 1)[0] or t[:100]
        t = cut.strip()
    if t and len(t) + len(" #shorts") <= 100:
        t = f"{t} #shorts"
    return t[:100].strip() or "Story Time"


def _sig(text: str) -> set[str]:
    """Significant lowercase words, for overlap comparison."""
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "at",
            "it", "its", "is", "was", "are", "be", "i", "you", "he", "she",
            "they", "we", "my", "his", "her", "with", "for", "as", "that",
            "this", "but", "not", "so", "if", "then", "there", "here"}
    return {w.strip(".,!?;:\"'").lower() for w in text.split()
            if len(w) > 3} - stop


def _skeleton_key(title: str, narration: str) -> str | None:
    """Setup-level fingerprint of a story, independent of the object in it.

    Two stories that bill/charge the narrator for different things share the
    same skeleton ("boss|money"), which Jaccard over full 400-word texts
    misses because every concrete noun differs. Only the title plus the first
    SKELETON_HEAD_CHARS of narration are read, and the actor is matched on
    word boundaries: a plain substring search picks up incidental prose
    ("I called the customer service line") and invents an actor the story
    never had. Returns None when the story has no recognizable skeleton (no
    bill/fee/invoice grievance), so the general overlap rules still apply.
    """
    head = f"{title} {str(narration)[:SKELETON_HEAD_CHARS]}".casefold()
    if not SKELETON_MONEY_RE.search(head):
        return None
    actor = next((a for a in SKELETON_ACTORS
                  if re.search(rf"\b{re.escape(a)}\b", head)), "")
    return f"{actor or 'unknown'}|money"


def _entry_skeleton(h: dict) -> str | None:
    """Skeleton of a history entry, for ANY on-disk shape.

    Prefers the `skeleton` value stored at write time, else derives it from
    the entry's premise. Deriving from the excerpt is equivalent to deriving
    from the full script because _skeleton_key only reads the first
    SKELETON_HEAD_CHARS chars, which the excerpt preserves verbatim — so old
    full-narration entries and new trimmed ones classify identically.
    """
    stored = str(h.get("skeleton") or "").strip()
    if stored:
        return stored
    return _skeleton_key(str(h.get("title", "")), _entry_premise(h))


def similarity_reason(data: dict, slots: dict, recent: list[dict],
                      item_recent: list[dict] | None = None) -> str | None:
    """Repeat reason for title/premise dupes, skeleton repeats, or overlap.

    Reads history entries of either shape: pre-trim entries carrying the
    full script under `narration`, and trimmed entries carrying a premise
    excerpt plus `key_words` and `skeleton`. Exact premise matching compares
    excerpts on BOTH sides, so a repeat is caught against old and new entries
    alike instead of silently failing once the file is trimmed. Overlap
    against the general window still uses >0.35; the newest RECENT_GUARD_N
    entries use the tighter RECENT_OVERLAP_MAX (0.22). Consecutive entries are
    additionally checked for an identical billed/fee skeleton.
    """
    title = str(data.get("title", "")).strip().casefold()
    for h in recent:
        if str(h.get("title", "")).strip().casefold() == title:
            return "title already used"
    story = data.get("story_text") or data.get("narration", "")
    # Excerpt both sides: full-vs-full, full-vs-excerpt and excerpt-vs-excerpt
    # all collapse to the same comparison.
    premise = _premise_excerpt(story).casefold()
    if premise:
        for h in recent:
            if _premise_excerpt(_entry_premise(h)).casefold() == premise:
                return "premise already used"

    # Consecutive skeleton guard: same actor + same money grievance as any of
    # the last RECENT_GUARD_N aired entries means the same story shape.
    new_skel = _skeleton_key(str(data.get("title", "")), str(story))
    if new_skel:
        for h in list(recent)[-RECENT_GUARD_N:]:
            if _entry_skeleton(h) == new_skel:
                return (f"consecutive premise repeats '{h.get('title', '?')}' "
                        "(same billed/fee skeleton)")

    window = item_recent if item_recent is not None else recent[-30:]
    new_sig = _sig(f"{data.get('title', '')} {story}")
    tail = {id(h) for h in window[-RECENT_GUARD_N:]}
    for h in window[-30:]:
        old_sig = _sig(f"{h.get('title', '')} {_entry_premise(h)}")
        if not old_sig or not new_sig:
            continue
        # Recency-weighted threshold: same bar for the general window, tighter
        # for the entries that just aired. Identity (not equality) decides
        # recency so an identical-content duplicate can't flip the bar.
        limit = (RECENT_OVERLAP_MAX if id(h) in tail else OVERLAP_MAX)
        if len(new_sig & old_sig) / len(new_sig | old_sig) > limit:
            if limit == RECENT_OVERLAP_MAX:
                return (f"story overlaps recent '{h.get('title', '?')}' "
                        f"(recent overlap >{RECENT_OVERLAP_MAX:.0%})")
            return (f"story overlaps '{h.get('title', '?')}' "
                    f"(word overlap >{OVERLAP_MAX:.0%})")
    return None


def history_fields(slots: dict, meta: dict) -> dict:
    """Extra history.json entry fields for the story niche."""
    kw = meta.get("key_words") or meta.get("items") or []
    return {"topic": (slots or {}).get("topic", ""),
            "topicSource": (slots or {}).get("topic_source", "genre-rotation"),
            "genre": (slots or {}).get("genre", meta.get("genre", "")),
            "items": list(kw)[:10]}


def board_filter(beats, items, durations, get_font, has_drawtext,
                 title: str = "") -> str:
    """Story niche has no leaderboard overlay."""
    return ""


def build_prompt(history: list[dict], cfg: StoryConfig,
                 wps: float | None = None) -> tuple[str, dict]:
    """Gemini prompt for a 100-120s Reddit-style story. Returns (prompt, slots)."""
    history = history or []
    genre = _pick_genre(history, cfg)
    gender = _narrator_gender(history, cfg)
    slots = {"genre": genre, "topic": genre, "topic_source": "genre-rotation",
             "narrator_gender": gender}
    log(f"Story genre ({slots['topic_source']}): {genre}")
    wps = wps or cfg.history_wps
    lo, hi = target_words(cfg, wps)
    eff = effective_wps(cfg, wps)
    mid = (lo + hi) // 2
    secs = f"{cfg.target_min:g}-{cfg.target_max:g}"
    lo_secs = f"{lo / eff:.0f}" if eff > 0 else "?"
    hi_secs = f"{hi / eff:.0f}" if eff > 0 else "?"
    prompt = (
        f"Write a YouTube Shorts reddit-style story in the '{genre}' lane.\n\n"
        "This is ONE original, self-contained micro-story told in first or "
        "second person, the kind of tale that goes viral on story channels. "
        "It must be 100 percent original — never copy or retell a real Reddit "
        "post, and never use real people, brands, or defamatory places.\n\n"
        "TONE & VOICE (NATURAL STORYTELLING):\n"
        "Tell this story the way a real person would tell it to a friend over coffee—"
        "natural, unfiltered, emotional. Use everyday language with the occasional "
        "filler word ('um', 'honestly', 'so yeah', 'basically'), but at most ONE "
        "per sentence and never two in a row — 'I mean, honestly' is wrong and "
        "reads as a subtitle error. 'I mean' must actually restart or rephrase "
        "what you just said, never sit as a bare prefix; 'honestly' may open a "
        "sentence on its own. "
        "Vary your sentence rhythm: mix short punchy lines with longer flowing ones. "
        "Show real human reactions: confusion, anger, disbelief, satisfaction. Name "
        "key people simply ('my boss Dave', 'this guy Carl') but let minor characters "
        "stay generic ('the HR lady', 'some random customer'). NO robotic patterns: "
        "avoid theatrical phrases like 'little did I know', 'catastrophic failure', "
        "'under the false guise', or 'panic swept the room'. Real people say 'I had "
        "no idea', 'total disaster', 'they lied', 'everyone freaked out'. Keep it "
        "concrete and physical—real objects, real actions, real consequences.\n\n"
        "GROUNDED & BELIEVABLE (audiences are skeptical):\n"
        "These stories are told in first person like real memories, so every "
        "detail must survive a skeptical viewer. Write like someone recounting "
        "something that actually happened to them, not like a film scene.\n"
        "- Imperfect outcomes: real life rarely delivers clean justice. The "
        "payoff can be partial, costly, or unsatisfying. Let the narrator pay a "
        "price, lose something, or end up unsure. Do NOT always deliver a perfect "
        "takedown, a total confession, or a flawless win.\n"
        "- No cinematic reaction lines. Never write 'worth every penny', 'looked "
        "me dead in the eye', 'my blood went cold', 'like nothing had happened', "
        "'priceless', 'chill down my spine', 'the world tilted', or anything "
        "close — the validator rejects these. Show the reaction through what "
        "people DO (someone stops talking, puts a cup down, changes the "
        "subject) instead of narrated interior drama.\n"
        "- Keep the mechanism plausible: the narrator is an ordinary person "
        "with ordinary resources. Tech, legal, medical or financial moves must "
        "be things a real person could actually discover and do. No secret "
        "router backdoors, no fake notarizations that magically hold, no "
        "instant database access, no conveniently retained video evidence "
        "nobody thought to check.\n"
        "- Leave one loose thread: real memories skip, hedge, and trail off. "
        "Let one detail stay unresolved or slightly uncertain. Do not wrap up "
        "every thread neatly.\n"
        "- Concrete over cinematic: real numbers ('forty-two dollars', 'two "
        "years'), real objects, real time stamps, real small annoyances — not "
        "weather, lighting, or camera language.\n"
        "- Keep it in the past tense with the narrator's own limited knowledge: "
        "they only know what they personally saw, heard, or were told. They may "
        "be wrong about the cause.\n\n"
        f"LENGTH MATH — HARD REQUIREMENT (validator REJECTS short scripts):\n"
        f"TOTAL {lo}-{hi} words, aim {mid} words (never the floor). "
        f"At ~{eff:.1f} wps, {lo} words ~= {lo_secs}s and {hi} words ~= "
        f"{hi_secs}s for the {secs}s target. Under {lo} words is "
        f"under-length and REJECTED — count every word before answering "
        f"and deliver the FULL {mid}-word count, never a short draft. "
        f"Under {lo - 15 - _MICRO_FLOOR_WORDS} words is unusable even on "
        f"grace.\n\n"
        f"{history_block(history, cfg.history_prompt_n)}"
        "Return ONLY valid JSON with exactly these keys: title, card_text, "
        "card_highlight_word, hook, story_text, narration, genre, narrator_gender, key_words, "
        "tags, description, ending_type, pinned_comment, beats, "
        "rehooks, plus optional part_2_seed.\n\n"
        f"Story flow (natural human pacing, {lo}-{hi} words total, about {secs} seconds):\n"
        "Start with a hook that drops you into the tension ('My landlord kept my "
        "deposit over a single nail hole'). Then tell the story the way it actually "
        "unfolded—setup, escalation, your response, and the outcome. Don't think "
        "in rigid beats or formulas. Think: What happened? How did I feel? What did "
        "I do next? What happened then?\n\n"
        "Natural story rhythm:\n"
        "- Open with the card line (<=8 words) that makes people stop scrolling\n"
        "- Quick setup: who's involved, what the situation was\n"
        "- Rising tension: things get worse, you realize something's wrong\n"
        "- Your move: you take action, find proof, set something in motion\n"
        "- The confrontation: where it all comes to a head\n"
        "- The outcome: justice, revenge, consequences, or a cliffhanger\n\n"
        "Build natural turning points around 20-30s, 50-60s, and 80-90s where the "
        "story shifts—but don't telegraph them. Just let the story naturally escalate. "
        "These should feel like 'wait, WHAT?' moments, not labeled plot points.\n\n"
        "Ending must deliver: either satisfying payoff (clear consequences, real justice) "
        "or a genuine cliffhanger that demands part 2. Set ending_type to 'payoff' or "
        "'cliffhanger'. Only cliffhangers get part_2_seed (one sentence teasing what "
        "happens next).\n\n"
        "Hook examples (voice reference only—never copy, always invent fresh): "
        "'My landlord kept my deposit over a nail hole.' 'My roommate ate my "
        "leftovers one too many times.' 'The hallway light flickered at 3 AM.'\n\n"
        "story_text and narration must be IDENTICAL: the full spoken story, "
        f"{lo}-{hi} words (card line included in the total). "
        f"genre must echo '{genre}'. "
        f"narrator_gender is whatever the narrator of THIS story is: decide "
        f"it from the story's own first-person context and write the narration "
        f"to match. First inspect the narrator's own cues — 'my husband', 'my "
        f"wife', 'my fiancee', 'I was pregnant', 'as a woman', 'as a man' — and "
        f"set narrator_gender to the one the narrator IS. Pick the gender the "
        f"story fits best when the cues are softer, and never let a side "
        f"character's gender decide it ('my sister', 'my boss') and never "
        f"invent a cue just to justify a gender. Only when the story is truly "
        f"gender-neutral with no first-person signal at all, fall back to the "
        f"scheduled UTC default \"{gender}\". "
        "key_words: 4-8 lowercase content words (people, places, objects, "
        "moods) for dedup and visuals.\n\n"
        "rehooks: metadata ONLY, never spoken — exactly 3 objects "
        "{\"at\": \"20-30s\" | \"50-60s\" | "
        "\"80-90s\", \"line\": <exact story line>}. Each line must be a plain "
        "in-world sentence already appearing verbatim in the story. NEVER write "
        "the words 'rehook', 're-hook', or 'stage direction', and never use labels "
        "like 'Rehook line:', anywhere in story_text/narration/beats/hook — the "
        "narrator speaks every word, so any label would be voiced aloud. "
        "Re-hooks must be natural in-story narrative turns "
        "invented uniquely for your story (never copy example text). "
        "NEVER break the fourth wall with viewer survey questions like 'Do you think I am wrong?' or "
        "'Am I the jerk?'. Keep the listener fully inside the story.\n\n"
        "TTS rules (strict): Keep sentences under 30 words—break longer ones into two. "
        "Vary your rhythm naturally: some sentences flow longer (15-20 words), others "
        "punch short (3-8 words). Example: 'He stared at me. Nobody said anything. "
        "Then I pulled out the email.' Plain text only—no emojis, no markdown "
        "(*, #, _, `, |, ~, @, ^, < >). Write numbers as words ('three AM', 'twenty "
        "bucks'). Skip dialogue tags ('he said')—just describe what happened.\n\n"
        "Safety (strict): no graphic sexual content, no violence against "
        "minors, no hate toward any group, nothing age-restricted or "
        "demonetization-bait. Everyday conflict, petty revenge, workplace "
        "drama, spooky-but-safe chills only.\n\n"
        "title: hook-driven, curiosity-gap, <=60 chars, with 1-2 fitting "
        "emojis, no hashtags (the uploader appends tagging). description: exactly 2 sentences "
        "with 1-2 fitting emojis and 3 hashtags. tags: exactly 5 plain-ASCII "
        "strings, no emoji. pinned_comment: ONE engaging question or call to "
        "comment (<=150 chars, 1 emoji max).\n\n"
        "beats: array of 3-5 objects in spoken order. Each object must be "
        "{\"text\": <exact story chunk>}. The beat texts "
        "joined with single spaces must equal narration EXACTLY.\n\n"
        f"Beat structure (divide story into 3-5 natural chunks, totalling {lo}-{hi} words):\n"
        "Split the story where visual scenes naturally change—not by formula, but by "
        "what's actually happening. Aim for roughly "
        f"{mid // 5} words per beat on average, but let the story breathe—some beats "
        "can be 70 words, others 100+. The final beat must deliver a real ending "
        "(60+ words minimum)—never rush it with 'and that was that' or 'follow for "
        "part 2' without payoff.\n\n"
        "Do not include markdown, notes, or any extra keys. Output only the "
        "JSON object."
    )
    return prompt, slots


def _is_length_error(err: Exception) -> bool:
    """True when the validator rejected the draft for word-count range."""
    return "out of range" in str(err).lower()


def _draft_word_count(raw: object) -> int | None:
    """Word count of a raw draft payload, or None when unavailable."""
    if not isinstance(raw, dict):
        return None
    draft = raw.get("narration") or raw.get("story_text") or ""
    if not isinstance(draft, str) or not draft.strip():
        return None
    return len(draft.split())


def _length_fix_prompt(history: list[dict], cfg: StoryConfig, wps: float | None,
                       raw: object, err: Exception) -> tuple[str, dict]:
    """Same-model length retry prompt with the measured word delta.

    Aims mid-range, not the floor: short drafts EXPAND beats 2-4 with
    concrete actions, evidence, and consequences; long drafts CONDENSE
    setup adjectives first. The sentence ask scales with the delta
    (~12 words/sentence over beats 2-4: 1 sentence each when |delta|<20,
    otherwise delta/12 total, capped at 3 per beat) so small misses stop
    overshooting past the far edge. The beats-joined==narration contract
    and all other gates stay in the base prompt, which this header
    prepends to.
    """
    lo, hi = target_words(cfg, wps)
    mid = (lo + hi) // 2
    n = _draft_word_count(raw)
    base_prompt, slots = build_prompt(history, cfg, wps)
    if n is None:
        return base_prompt, slots
    if n < lo:
        delta = mid - n
        per_beat = max(1, min(3, round(delta / 36)))
        total = per_beat * 3
        s_word = "sentence" if per_beat == 1 else "sentences"
        header = (
            f"CORRECTION — your draft is {n} words but the validator needs "
            f"{lo}-{hi} (aim {mid}, +{delta} words). EXPAND the SAME story "
            f"(same hook, same twist, same ending): add {per_beat} full "
            f"{s_word} "
            f"of concrete action, evidence, and rising consequences to EACH of "
            f"beats 2, 3 and 4 (~{total} sentences / ~{delta} words total, "
            f"do NOT pad with repetitive internal monologue), "
            f"keep every sentence 30 words or fewer, keep beat texts joined "
            f"equal to narration EXACTLY, and return the FULL {lo}-{hi}-word JSON "
            f"with the same keys. Never return another short draft.\n\n")
    else:
        delta = n - mid
        per_beat = max(1, min(3, round(delta / 36)))
        total = per_beat * 3
        s_word = "sentence" if per_beat == 1 else "sentences"
        header = (
            f"CORRECTION — your draft is {n} words but the validator needs "
            f"{lo}-{hi} (aim {mid}, cut ~{delta} words). CONDENSE the SAME "
            f"story (same hook, same twist, same ending): trim setup "
            f"adjectives and merge setup sentences first "
            f"(about {per_beat} {s_word} per beat, ~{total} sentences / "
            f"~{delta} words total), keep beats 3-4 "
            f"and the final beat (>=60 words) fully expanded, keep every "
            f"sentence 30 words or fewer, keep beat texts joined equal to "
            f"narration EXACTLY, and return the FULL {lo}-{hi}-word JSON "
            f"with the same keys.\n\n")
    return header + base_prompt, slots


def _shape_fix_prompt(history: list[dict], cfg: StoryConfig, wps: float | None,
                      raw: object, err: Exception | None) -> tuple[str, dict]:
    """Same-model shape retry prompt carrying the validator's specifics.

    Mirrors _length_fix_prompt: the base prompt is rebuilt and a
    CORRECTION header is prepended naming the exact validator error,
    the offending sentence excerpt(s), and the card-vs-first-sentence
    diff. Instructs a surgical fix only (split >30w sentences at
    conjunctions, card verbatim as sentence 1 — shortened to <=8 words
    first when the card itself is overlong, beats joined == narration
    exactly, under-range totals pulled back with scaled sensory-beat
    expansion per beat (same delta math as _length_fix_prompt) and
    over-range totals condensed). Non-dict/empty drafts fall back to a
    fresh prompt. The micro-accept path in the caller is unchanged.
    """
    lo, hi = target_words(cfg, wps)
    mid = (lo + hi) // 2
    base_prompt, slots = build_prompt(history, cfg, wps)
    if not isinstance(raw, dict):
        return base_prompt, slots
    narr = raw.get("narration") or raw.get("story_text") or ""
    if not isinstance(narr, str) or not narr.strip():
        return base_prompt, slots
    narr = narr.strip()
    n = len(narr.split())
    long_hits = [s.strip() for s in re.split(r"[.!?…]+", narr)
                 if s.strip() and len(s.split()) > MAX_SENTENCE_WORDS][:2]
    card = str(raw.get("card_text", "") or "").strip()
    first = re.split(r"[.!?…]+", narr, maxsplit=1)[0].strip()
    card_n = len(card.split()) if card else 0
    specifics = [f"validator error: {err}."]
    for s in long_hits:
        specifics.append(
            f"offending sentence ({len(s.split())} words): "
            f"'{s[:120]}...' — split it into two sentences at a "
            "conjunction (and/but/because/so/when/while/which/that).")
    if card and card_n > 8:
        specifics.append(
            f"card line is {card_n} words ('{card}') — shorten the card "
            f"itself to 8 words or fewer, then copy that shortened card "
            f"VERBATIM as sentence 1 (replace the opening sentence "
            f"wholesale, do not merge).")
    elif card and first != card:
        specifics.append(
            f"card line is '{card}' but the story opens with "
            f"'{first[:80]}' — copy the card line VERBATIM as sentence 1 "
            f"(replace the opening sentence wholesale, do not merge).")
    s_lo, s_hi = lo - 15, hi + 15
    if n < s_lo:
        deficit = mid - n
        per_beat = max(1, min(3, round(deficit / 36)))
        total = per_beat * 3
        s_word = "sentence" if per_beat == 1 else "sentences"
        specifics.append(
            f"draft is {n} words but needs {s_lo}-{s_hi} (aim {mid}, "
            f"+{deficit} words): EXPAND the same story by adding "
            f"{per_beat} full {s_word} of concrete sensory beats "
            f"(sights, sounds, physical actions, evidence, rising "
            f"consequences) to EACH of beats 2, 3 and 4 (~{total} "
            f"sentences / ~{deficit} words total, no filler monologue), "
            f"keep the card line verbatim as sentence 1, keep every "
            f"sentence 30 words or fewer, and keep beat texts joined "
            f"equal to narration EXACTLY.")
    elif n > s_hi:
        specifics.append(
            f"draft is {n} words but needs {s_lo}-{s_hi}: CONDENSE by "
            f"cutting ~{n - mid} words of setup adjectives first, keeping "
            f"beats 3-4 and the final beat fully expanded.")
    else:
        specifics.append(
            f"hold the total near {n} words inside {lo}-{hi} (aim {mid}).")
    specifics.append(
        f"keep the SAME story (same hook, twist, ending), keep every "
        f"sentence 30 words or fewer, keep beat texts joined equal to "
        f"narration EXACTLY — surgical fix only, no rewrite.")
    return "CORRECTION — " + " ".join(specifics) + "\n\n" + base_prompt, slots


def _make_client(cfg: StoryConfig):
    """Engine client or None when keyless (offline path)."""
    keys = list(cfg.gemini_keys)
    if not keys:
        return None
    return _gc.GeminiClient(keys, timeout=cfg.timeout,
                            tts_timeout=cfg.tts_timeout,
                            tries=cfg.tries, backoff=cfg.backoff)


def _to_content(meta: dict, slots: dict, wps: float | None,
                history: list[dict] | None = None) -> Content:
    """Validated meta + slots â†’ stage Content (all story fields in extras).

    history is used to populate one extra for build_visuals:
      - video_number: len(same-niche history) â†’ BG clip rotation
    """
    history = history or []
    same_niche = [h for h in history if h.get("niche", "") == MODE]
    video_number = len(same_niche)
    return Content(
        title=meta["title"],
        narration_text=meta["narration"],
        description=meta.get("description", ""),
        tags=list(meta.get("tags", [])),
        extras={"beats": list(meta.get("beats", [])),
                "items": list(meta.get("items", meta.get("key_words", []))),
                "key_words": list(meta.get("key_words", [])),
                "genre": slots.get("genre", ""),
                "topic": slots.get("topic", ""),
                "topic_source": slots.get("topic_source", "genre-rotation"),
                "card_text": meta.get("card_text", ""),
                "card_highlight_word": meta.get("card_highlight_word", ""),
                "hook": meta.get("hook", ""),
                "story_text": meta.get("story_text", meta.get("narration", "")),
                "ending_type": meta.get("ending_type", ""),
                "narrator_gender": meta.get("narrator_gender", ""),
                "pinned_comment": meta.get("pinned_comment", ""),
                "part_2_seed": meta.get("part_2_seed", ""),
                "rehooks": list(meta.get("rehooks", [])),
                "wps": wps,
                "slots": dict(slots),
                "video_number": video_number})


def _model_attempts(cfg: StoryConfig, model: str) -> int:
    """Per-model draft budget: Lite models get cfg.lite_retries (default
    5) so misses converge; every other model keeps 2."""
    if "lite" in str(model or "").lower():
        try:
            return min(8, max(1, int(getattr(cfg, "lite_retries", 5))))
        except (TypeError, ValueError):
            return 5
    return 2


def generate_content(history: list[dict], cfg: StoryConfig) -> Content:
    """Gemini story with similarity regens; offline fallback only when keyless.

    When keys exist but every model fails (transport/JSON errors or
    unfixable validator rejects), raises RuntimeError instead of airing
    a short silent fallback — the error propagates through core.run
    (no history recorded) to pipeline notify_failure + exit 1.
    """
    history = history or []
    wps = measure_wps(history)
    client = _make_client(cfg)
    if client is not None:
        models = list(getattr(cfg, "story_models", None)
                      or DEFAULT_TEXT_MODELS)
        last_error: Exception | None = None
        for _attempt in range(1 + cfg.similarity_retries):
            prompt, slots = build_prompt(history, cfg, wps)
            data = None
            for mi, model in enumerate(models):
                tries = _model_attempts(cfg, model)
                raw_latest: object = None
                err_latest: Exception | None = None
                for attempt in range(1, tries + 1):
                    try:
                        if attempt == 1:
                            raw_latest = client.generate_json(
                                model, prompt, generation_config={
                                    "temperature": 0.8, "maxOutputTokens": 8192})
                        else:
                            # Every retry re-aims from the LATEST draft so a
                            # wrong-direction miss corrects instead of
                            # compounding: deficit/surplus to mid plus card
                            # shorten + verbatim sentence 1 in one prompt.
                            fix_prompt, _retry_slots = _shape_fix_prompt(
                                history, cfg, wps, raw_latest, err_latest)
                            raw_latest = client.generate_json(
                                model, fix_prompt, generation_config={
                                    "temperature": 0.8, "maxOutputTokens": 8192})
                    except Exception as e:
                        last_error = e
                        log(f"Story model {model} unusable ({e}), trying next...")
                        time.sleep(cfg.backoff if mi + 1 >= len(models) else 2)
                        raw_latest = None
                        break
                    try:
                        data = validate_extra(raw_latest, cfg, wps, micro_accept=True)
                    except ValueError as e:
                        err_latest = e
                        # Fixable reject: same-model retries before
                        # discarding this model. Length near-misses
                        # (within micro/grace margin) are accepted without
                        # a retry call so an off-by-one never burns a model.
                        if _is_length_error(e):
                            n0 = _draft_word_count(raw_latest)
                            if n0 is not None:
                                _lo, _hi = target_words(cfg, wps)
                                if _is_near_miss(n0, _lo, _hi):
                                    log(f"Story model {model} draft near-miss "
                                        f"({n0} words vs {_lo - 15}-{_hi + 15}), "
                                        "micro-accepting without retry...")
                                    try:
                                        data = validate_extra(
                                            raw_latest, cfg, wps, micro_accept=True)
                                    except ValueError as e_nm:
                                        err_latest = e_nm
                                        log(f"Story model {model} draft rejected "
                                            f"({e_nm}), retrying same model...")
                                        data = None
                                    if data is not None:
                                        break
                        if data is None and attempt < tries:
                            n_latest = _draft_word_count(raw_latest)
                            log(f"Story model {model} draft rejected "
                                f"({e})"
                                f"{f' [{n_latest} words]' if n_latest else ''}, "
                                f"retry {attempt + 1}/{tries} same model...")
                            time.sleep(2)
                            continue
                    if data is not None:
                        break
                if data is None:
                    if raw_latest is not None:
                        log(f"Story model {model} exhausted after {tries} "
                            "attempts, trying next...")
                    continue
                if mi:
                    models.remove(model)
                    models.insert(0, model)
                break
            if data is None:
                raise RuntimeError(
                    f"Story Gemini failed on all {len(models)} models "
                    f"(last error: {last_error}); refusing silent "
                    f"fallback — fix keys/quota/prompt and re-run."
                ) from last_error
            window = [h for h in history[-cfg.dedup_window:]
                      if h.get("niche", "") == MODE]
            # Resolve from the generated first-person story when possible;
            # the UTC bucket remains the deterministic ambiguous-case fallback.
            data["narrator_gender"] = _narrator_gender(
                history, cfg, str(data.get("narrator_gender", "")),
                story_text=" ".join(
                    str(value or "").strip()
                    for value in (
                        data.get("title", ""),
                        data.get("narration") or data.get("story_text") or "",
                    )
                    if str(value or "").strip()))
            slots["narrator_gender"] = data["narrator_gender"]
            reason = similarity_reason(data, slots, window, window[-30:])
            if reason is None:
                data["title"] = sanitize_title(data.get("title", ""), slots)
                log(f"Story script OK: {data.get('title')} "
                    f"(narrator={data['narrator_gender']})")
                return _to_content(data, slots, wps, history)
            if _attempt < cfg.similarity_retries:
                warn(f"Story draft too similar ({reason}), regenerating...")
                continue
            warn(f"Story draft still similar ({reason}), accepting best-effort.")
            data["title"] = sanitize_title(data.get("title", ""), slots)
            return _to_content(data, slots, wps, history)
    warn("No Gemini keys: using standalone fallback story.")
    return _offline_content(history, cfg, wps)


def _offline_content(history: list[dict], cfg: StoryConfig,
                     wps: float | None) -> Content:
    """Air the least-recently-used fallback variant as a real Content.

    Shared by the keyless path and the unreachable-API path so both produce
    an identical, fully downstreamed story (TTS, render, upload, history).
    """
    meta, slots = fallback_script(history)
    meta["narrator_gender"] = _narrator_gender(
        history, cfg, str(meta.get("narrator_gender", "")),
        story_text=" ".join(
            str(value or "").strip()
            for value in (
                meta.get("title", ""),
                meta.get("narration") or meta.get("story_text") or "",
            )
            if str(value or "").strip()))
    slots["narrator_gender"] = meta["narrator_gender"]
    out = _to_content(meta, slots, wps, history)
    out.extras["offline"] = True
    return out


_FALLBACK_VARIANTS = (
    {
        "title": "I Finally Said No to Weekend Work ðŸ˜¤",
        "card_text": "I said no, then they moved my desk",
        "card_highlight_word": "desk",
        "hook": "I said no, then they moved my desk.",
        "genre": "workplace",
        "narrator_gender": "female",
        "key_words": ["boss", "overtime", "office", "meeting", "weekend"],
        "tags": ["shorts", "storytime", "workplace", "revenge", "stories"],
        "description": ("She finally said no to weekend work, and the office "
                        "lost its mind. Would you have caved? "
                        "#shorts #storytime #workplace"),
        "ending_type": "payoff",
        "pinned_comment": "Would YOU have said no, or worked the Saturday?",
        "rehooks": [
            {"at": "20-30s",
             "line": "Then I found out my boss told everyone I was putting the whole project at risk."},
            {"at": "50-60s",
             "line": "So I printed every late night email and every weekend text, and I brought them to the big meeting."},
            {"at": "80-90s",
             "line": "The room went silent, and my boss never asked me to work a weekend again."},
        ],
        "beats": [
            {"text": ("I said no, then they moved my desk. My boss asked me "
                      "to work Saturday for the third weekend in a row, so "
                      "I finally said no.")},
            {"text": ("On Monday, nobody would talk to me. My chair was moved "
                      "to the storage room, and my name was missing from the "
                      "team lunch order. Then I found out my boss told everyone "
                      "I was putting the whole project at risk.")},
            {"text": ("So I printed every late night email and every weekend "
                      "text, and I brought them to the big meeting. I laid them "
                      "on the table and said, if hard work matters here, the "
                      "records speak for themselves. The room went silent, and "
                      "my boss never asked me to work a weekend again. Comment "
                      "if you would have done the same, and follow for the next story.")},
        ],
    },
    {
        "title": "My Neighbor Learned to Fear My Blender ðŸ”Š",
        "card_text": "He drummed at midnight, I blended at seven",
        "card_highlight_word": "blended",
        "hook": "He drummed at midnight, I blended at seven.",
        "genre": "revenge",
        "narrator_gender": "male",
        "key_words": ["neighbor", "drums", "blender", "apartment", "deal"],
        "tags": ["shorts", "storytime", "revenge", "neighbors", "stories"],
        "description": ("Midnight drums met a seven AM blender. Petty? Maybe. "
                        "Effective? Completely. #shorts #storytime #revenge"),
        "ending_type": "payoff",
        "pinned_comment": "Petty revenge: justified or too far? Sound off below.",
        "rehooks": [
            {"at": "20-30s",
             "line": "So I learned his schedule. He slept until noon after his night shifts, every single day."},
            {"at": "50-60s",
             "line": "On day three, he knocked on my door, half asleep, and begged for mercy."},
            {"at": "80-90s",
             "line": "We made a deal that same morning."},
        ],
        "beats": [
            {"text": ("He drummed at midnight, I blended at seven. My "
                      "upstairs neighbor played drums every night at "
                      "midnight, and politeness got me nowhere.")},
            {"text": ("So I learned his schedule. He slept until noon after "
                      "his night shifts, every single day.")},
            {"text": ("I bought the loudest blender I could find. "
                      "Every morning at seven, I made the thickest smoothie "
                      "of my life. "
                      "On day three, he knocked on my door, half asleep, and "
                      "begged for mercy. We made a deal that same morning. No "
                      "drums after ten, no blender before nine. We have been "
                      "perfect neighbors ever since. Comment your best petty "
                      "revenge below, and follow for the next story.")},
        ],
    },
    {
        "title": "The Gas Station Phone Rings at 3 AM ðŸ“žðŸ˜±",
        "card_text": "Do not turn around",
        "card_highlight_word": "around",
        "hook": "Do not turn around.",
        "genre": "scary",
        "narrator_gender": "female",
        "key_words": ["gas station", "night shift", "phone call", "cameras",
                      "hallway"],
        "tags": ["shorts", "storytime", "scary", "creepystory", "stories"],
        "description": ("The phone rang at three AM with a warning: do not turn "
                        "around. Part two at ten thousand likes. "
                        "#shorts #scarystory #storytime"),
        "ending_type": "cliffhanger",
        "pinned_comment": "Want part 2? Ten thousand likes and it drops.",
        "part_2_seed": "The cameras catch who was standing in the back hallway.",
        "rehooks": [
            {"at": "20-30s",
             "line": "A small voice whispered, do not turn around, and then the line went dead."},
            {"at": "50-60s",
             "line": "Then every screen froze on the same frame, the back hallway, where something tall was standing."},
            {"at": "80-90s",
             "line": "I grabbed my keys and ran, and I never looked back."},
        ],
        "beats": [
            {"text": ("Do not turn around. I work the night shift at a gas "
                      "station. Last Tuesday the phone rang at exactly "
                      "three in the morning.")},
            {"text": ("A small voice whispered, do not turn around, and then "
                      "the line went dead. My hands were shaking, but I checked "
                      "the cameras, and the store was empty.")},
            {"text": ("Then every screen froze on the same frame, the back "
                      "hallway, where something tall was standing. I grabbed my "
                      "keys and ran, and I never looked back. The next night, "
                      "the phone rang again at exactly three. Comment if you "
                      "want part two, and follow for the next story.")},
        ],
    },
)


def _build_fallback_meta(variant: dict) -> tuple[dict, dict]:
    """Assemble a (meta, slots) pair from one fallback variant dict."""
    story = " ".join(b["text"] for b in variant["beats"])
    slots = {"genre": variant["genre"], "topic": variant["genre"],
             "topic_source": "fallback"}
    meta = {
        "title": variant["title"],
        "card_text": variant["card_text"],
        "card_highlight_word": variant["card_highlight_word"],
        "hook": variant["hook"],
        "story_text": story,
        "narration": story,
        "genre": variant["genre"],
        "key_words": list(variant["key_words"]),
        "tags": list(variant["tags"]),
        "description": variant["description"],
        "ending_type": variant["ending_type"],
        "narrator_gender": variant.get("narrator_gender", ""),
        "pinned_comment": variant["pinned_comment"],
        "rehooks": [dict(r) for r in variant.get("rehooks", [])],
        "beats": [{"text": b["text"], "label": ""}
                  for b in variant["beats"]],
        "items": list(variant["key_words"]),
    }
    if variant.get("part_2_seed"):
        meta["part_2_seed"] = variant["part_2_seed"]
    return meta, slots


def fallback_script(history: list[dict] | None = None) -> tuple[dict, dict]:
    """Offline story script. Least-recently-aired variant wins."""
    history = history or []

    def _last_pos(genre_cf: str) -> int:
        pos = -1
        for i, h in enumerate(history):
            if (h.get("niche", "") == MODE
                    and str(h.get("genre", "")).casefold() == genre_cf):
                pos = i
        return pos

    ranked = sorted(_FALLBACK_VARIANTS,
                    key=lambda v: _last_pos(v["genre"].casefold()))
    same = [h for h in history if h.get("niche", "") == MODE]
    last_genre = str(same[-1].get("genre", "")).casefold() if same else ""
    chosen = ranked[0]
    for variant in ranked:
        if variant["genre"].casefold() != last_genre:
            chosen = variant
            break
    log(f"Offline fallback: '{chosen['title']}'.")
    return _build_fallback_meta(chosen)


def sample_content() -> Content:
    """Offline sample (first fallback variant) for demos/tests."""
    meta, slots = _build_fallback_meta(_FALLBACK_VARIANTS[0])
    out = _to_content(meta, slots, wps=None)
    out.extras["offline"] = True
    return out
