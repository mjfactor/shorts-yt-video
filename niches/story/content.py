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

from engine import gemini_client as _gc
from engine.ffmpeg_utils import log, warn
from niches.base import Content

from .config import (DEFAULT_GENRES, DEFAULT_TEXT_MODELS, MODE,
                     StoryConfig)

AVOID_INTRO = "Already-aired stories — do NOT reuse these titles or premises:\n"
AVOID_OUTRO = "Your story must have a DIFFERENT premise from every entry above. "

USE_BEAT_QUERY = True
AMBIENT_QUERIES: tuple = ()
USE_MOOD = False

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

MAX_SENTENCE_WORDS = 30


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


def format_history_line(h: dict) -> str:
    """One-line history entry for the dedup prompt block."""
    premise = str(h.get("narration", "")).strip().replace("\n", " ")[:60]
    return (f"- \"{h.get('title', '?')}\" "
            f"(genre={h.get('genre', '?')}) premise: {premise}...")


def history_block(history: list[dict], prompt_n: int) -> str:
    """Recent same-niche entries the model must not repeat."""
    recent = [h for h in (history or [])[-prompt_n:]
              if h.get("niche", "") == MODE]
    if not recent:
        return ""
    return AVOID_INTRO + "\n".join(format_history_line(h) for h in recent) + "\n" + AVOID_OUTRO


def measure_wps(history: list[dict], default: float = 2.9) -> float:
    """Speech rate from history entries carrying words+duration, else default."""
    rates = []
    for h in (history or [])[-30:]:
        try:
            w = float(h.get("words", 0))
            d = float(h.get("duration", 0))
            if w > 20 and d > 10:
                rates.append(w / d)
        except (TypeError, ValueError):
            continue
    if not rates:
        return default
    rates.sort()
    mid = rates[len(rates) // 2]
    return min(4.2, max(2.2, mid))


def parse_edge_rate_pct(rate: str | None) -> float:
    """'+20%' -> 20.0 (edge relative-rate percent); unparseable -> 0.0."""
    m = re.match(r"\s*([+-]?)(\d+(?:\.\d+)?)\s*%?\s*$", rate or "")
    if not m:
        return 0.0
    val = float(m.group(2))
    return -val if m.group(1) == "-" else val


def effective_wps(cfg: StoryConfig, wps: float) -> float:
    """Edge-aware speech rate: history wps scaled by STORY_EDGE_RATE,
    clamped to 2.2-4.2 wps, then multiplied by STORY_LENGTH_OVERSHOOT
    (clamped 1.0-1.20) so drafts aim mid-range, not the bare floor."""
    try:
        base = float(wps) if wps else float(cfg.history_wps)
    except (TypeError, ValueError):
        base = float(cfg.history_wps)
    edge_pct = parse_edge_rate_pct(getattr(cfg, "edge_rate", "+20%"))
    scaled = base * (1.0 + edge_pct / 100.0)
    scaled = min(4.2, max(2.2, scaled))
    try:
        over = float(getattr(cfg, "length_overshoot", 1.08))
    except (TypeError, ValueError):
        over = 1.08
    over = min(1.20, max(1.0, over))
    return scaled * over


def target_words(cfg: StoryConfig, wps: float) -> tuple[int, int]:
    """(lo, hi) word budget from TARGET_MIN/MAX * edge-aware effective wps.

    At 100-120s, history 2.9 wps, edge +20% and 1.08 overshoot this is
    roughly 390-490 words (mid ~440w), matching the 3.5-3.8 wps edge
    reality with headroom instead of the old 290-348w short floor.
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


def _micro_margin(lo: int, hi: int) -> int:
    """Near-miss tolerance: max(5 words, 2% of the window mid)."""
    mid = (lo + hi) / 2
    return max(_MICRO_WORDS, math.ceil(_MICRO_FRAC * mid))


def _is_near_miss(n: int, lo: int, hi: int, tol: int = 15) -> bool:
    """True when n sits just outside the strict [lo-tol, hi+tol] window
    but inside the micro margin — accept without a retry call."""
    s_lo, s_hi = lo - tol, hi + tol
    if s_lo <= n <= s_hi:
        return False
    m = _micro_margin(lo, hi)
    return (s_lo - m) <= n <= (s_hi + m)


def validate_extra(data: dict, cfg: StoryConfig | None = None,
                   wps: float = 2.9, *, micro_accept: bool = False) -> dict:
    """Strict-check the story payload. Raises ValueError when unusable.

    `micro_accept=True` widens ONLY the word-count gate by a micro margin
    (max(5 words, 2% of mid)) so near-miss drafts (e.g. off-by-one) are
    accepted without burning a retry model call. Every other gate is
    enforced unchanged.
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
            s_lo, s_hi = s_lo - m, s_hi + m
    else:
        lo, hi = 80, 160
        s_lo, s_hi = lo, hi
    if words < s_lo or words > s_hi:
        raise ValueError(f"'story_text' out of range ({words} words, need {lo - 15 if cfg is not None else lo}-{hi + 15 if cfg is not None else hi})")
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
    long_sent = _check_sentences(story)
    if long_sent:
        raise ValueError(long_sent)
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
    bg_queries = _str_list(data.get("bg_queries"))[:8]
    if len(bg_queries) < 5:
        raise ValueError("'bg_queries' must hold 5-8 queries")
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
        "bg_queries": bg_queries,
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
        "beats": [{"text": str(b.get("text", "")).strip(),
                   "query": str(b.get("query", "")).strip(), "label": ""}
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


def similarity_reason(data: dict, slots: dict, recent: list[dict],
                      item_recent: list[dict] | None = None) -> str | None:
    """Repeat reason for title/premise dupes or >35% overlap, else None."""
    title = str(data.get("title", "")).strip().casefold()
    for h in recent:
        if str(h.get("title", "")).strip().casefold() == title:
            return "title already used"
    premise = str(data.get("story_text") or data.get("narration", "")).strip().casefold()
    for h in recent:
        if str(h.get("narration", "")).strip().casefold() == premise:
            return "premise already used"
    window = item_recent if item_recent is not None else recent[-30:]
    new_sig = _sig(f"{data.get('title', '')} "
                   f"{data.get('story_text') or data.get('narration', '')}")
    for h in window[-30:]:
        old_sig = _sig(f"{h.get('title', '')} {h.get('narration', '')}")
        if not old_sig or not new_sig:
            continue
        if len(new_sig & old_sig) / len(new_sig | old_sig) > 0.35:
            return f"story overlaps '{h.get('title', '?')}' (word overlap >35%)"
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
    slots = {"genre": genre, "topic": genre, "topic_source": "genre-rotation"}
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
        "filler word or natural pause ('I mean...', 'Honestly...', 'So yeah...'). "
        "Vary your sentence rhythm: mix short punchy lines with longer flowing ones. "
        "Show real human reactions: confusion, anger, disbelief, satisfaction. Name "
        "key people simply ('my boss Dave', 'this guy Carl') but let minor characters "
        "stay generic ('the HR lady', 'some random customer'). NO robotic patterns: "
        "avoid theatrical phrases like 'little did I know', 'catastrophic failure', "
        "'under the false guise', or 'panic swept the room'. Real people say 'I had "
        "no idea', 'total disaster', 'they lied', 'everyone freaked out'. Keep it "
        "concrete and physical—real objects, real actions, real consequences.\n\n"
        f"LENGTH MATH — HARD REQUIREMENT (validator REJECTS short scripts):\n"
        f"TOTAL {lo}-{hi} words, aim {mid} words (never the floor). "
        f"At ~{eff:.1f} wps, {lo} words ~= {lo_secs}s and {hi} words ~= "
        f"{hi_secs}s for the {secs}s target. Under {lo} words is "
        f"under-length and REJECTED — count every word before answering "
        f"and deliver the FULL {mid}-word count, never a short draft.\n\n"
        f"{history_block(history, cfg.history_prompt_n)}"
        "Return ONLY valid JSON with exactly these keys: title, card_text, "
        "card_highlight_word, hook, story_text, narration, genre, narrator_gender, key_words, "
        "bg_queries, tags, description, ending_type, pinned_comment, beats, "
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
        "narrator_gender is the narrator's own gender (\"male\" or \"female\", "
        "default \"female\" when ambiguous — never infer it from other characters). "
        "key_words: 4-8 lowercase content words (people, places, objects, "
        "moods) for dedup and visuals. bg_queries: 5-8 background-footage "
        "search queries, 1-3 concrete filmable words each "
        "(e.g. 'office desk night').\n\n"
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
        "{\"text\": <exact story chunk>, \"query\": <1-3 word filmable stock "
        "query>}. The beat texts joined with single spaces must equal "
        "narration EXACTLY. Each query should match its moment.\n\n"
        f"Beat structure (divide story into 3-5 natural chunks, totalling {lo}-{hi} words):\n"
        "Split the story where visual scenes naturally change—not by formula, but by "
        "what's actually happening. Each beat needs a filmable 1-3 word query (like "
        "'office desk night', 'security footage', 'kitchen morning'). Aim for roughly "
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


def _length_fix_prompt(history: list[dict], cfg: StoryConfig, wps: float,
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


def _make_client(cfg: StoryConfig):
    """Engine client or None when keyless (offline path)."""
    keys = list(cfg.gemini_keys)
    if not keys:
        return None
    return _gc.GeminiClient(keys, timeout=cfg.timeout,
                            tts_timeout=cfg.tts_timeout,
                            tries=cfg.tries, backoff=cfg.backoff)


def _to_content(meta: dict, slots: dict, wps: float) -> Content:
    """Validated meta + slots → stage Content (all story fields in extras)."""
    return Content(
        title=meta["title"],
        narration_text=meta["narration"],
        description=meta.get("description", ""),
        tags=list(meta.get("tags", [])),
        extras={"beats": list(meta.get("beats", [])),
                "items": list(meta.get("items", meta.get("key_words", []))),
                "key_words": list(meta.get("key_words", [])),
                "bg_queries": list(meta.get("bg_queries", [])),
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
                "slots": dict(slots)})


def generate_content(history: list[dict], cfg: StoryConfig) -> Content:
    """Gemini story with similarity regens; offline fallback when keyless."""
    history = history or []
    wps = measure_wps(history, cfg.history_wps)
    client = _make_client(cfg)
    if client is not None:
        models = list(getattr(cfg, "story_models", None)
                      or DEFAULT_TEXT_MODELS)
        for _attempt in range(1 + cfg.similarity_retries):
            prompt, slots = build_prompt(history, cfg, wps)
            data = None
            for mi, model in enumerate(models):
                try:
                    raw = client.generate_json(
                        model, prompt, generation_config={
                            "temperature": 0.8, "maxOutputTokens": 8192})
                except Exception as e:
                    log(f"Story model {model} unusable ({e}), trying next...")
                    time.sleep(cfg.backoff if mi + 1 >= len(models) else 2)
                    continue
                try:
                    data = validate_extra(raw, cfg, wps, micro_accept=True)
                except ValueError as e:
                    # Fixable shape reject (sentence length, hook/beats
                    # mismatch, ...): one same-model retry before
                    # discarding this model. Out-of-range drafts get a
                    # measured lengthen/tighten prompt aiming mid-range;
                    # other rejects get a fresh prompt. Near-misses
                    # (within micro margin) are accepted without a retry
                    # call so an off-by-one never burns a model.
                    if _is_length_error(e):
                        n0 = _draft_word_count(raw)
                        if n0 is not None:
                            _lo, _hi = target_words(cfg, wps)
                            if _is_near_miss(n0, _lo, _hi):
                                log(f"Story model {model} draft near-miss "
                                    f"({n0} words vs {_lo - 15}-{_hi + 15}), "
                                    "micro-accepting without retry...")
                                try:
                                    data = validate_extra(
                                        raw, cfg, wps, micro_accept=True)
                                except ValueError as e_nm:
                                    log(f"Story model {model} draft rejected "
                                        f"({e_nm}), retrying same model once...")
                                    data = None
                                if data is not None:
                                    if mi:
                                        models.remove(model)
                                        models.insert(0, model)
                                    break
                        if data is None:
                            log(f"Story model {model} draft length rejected "
                                f"({e}), lengthen/tighten retry same model...")
                    else:
                        log(f"Story model {model} draft rejected ({e}), "
                            "retrying same model once...")
                    time.sleep(2)
                    try:
                        if _is_length_error(e):
                            retry_prompt, _retry_slots = _length_fix_prompt(
                                history, cfg, wps, raw, e)
                        else:
                            retry_prompt, _retry_slots = build_prompt(
                                history, cfg, wps)
                        data = validate_extra(
                            client.generate_json(
                                model, retry_prompt, generation_config={
                                    "temperature": 0.8,
                                    "maxOutputTokens": 8192}),
                            cfg, wps, micro_accept=True)
                    except Exception as e2:
                        log(f"Story model {model} unusable ({e2}), "
                            "trying next...")
                        time.sleep(cfg.backoff if mi + 1 >= len(models)
                                   else 2)
                        continue
                if mi:
                    models.remove(model)
                    models.insert(0, model)
                break
            if data is None:
                log("All story models failed, using fallback.")
                break
            window = [h for h in history[-cfg.dedup_window:]
                      if h.get("niche", "") == MODE]
            reason = similarity_reason(data, slots, window, window[-30:])
            if reason is None:
                data["title"] = sanitize_title(data.get("title", ""), slots)
                log(f"Story script OK: {data.get('title')}")
                return _to_content(data, slots, wps)
            if _attempt < cfg.similarity_retries:
                warn(f"Story draft too similar ({reason}), regenerating...")
                continue
            warn(f"Story draft still similar ({reason}), accepting best-effort.")
            data["title"] = sanitize_title(data.get("title", ""), slots)
            return _to_content(data, slots, wps)
    warn("Gemini unavailable: using standalone fallback story.")
    meta, slots = fallback_script(history)
    return _to_content(meta, slots, wps)


_FALLBACK_VARIANTS = (
    {
        "title": "I Finally Said No to Weekend Work 😤",
        "card_text": "I said no, then they moved my desk",
        "card_highlight_word": "desk",
        "hook": "I said no, then they moved my desk.",
        "genre": "workplace",
        "narrator_gender": "female",
        "key_words": ["boss", "overtime", "office", "meeting", "weekend"],
        "bg_queries": ["office desk night", "empty office hallway",
                       "work team meeting", "conference room silent",
                       "city office windows", "desk clock overtime"],
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
                      "I finally said no."),
             "query": "office desk night"},
            {"text": ("On Monday, nobody would talk to me. My chair was moved "
                      "to the storage room, and my name was missing from the "
                      "team lunch order. Then I found out my boss told everyone "
                      "I was putting the whole project at risk."),
             "query": "empty office hallway"},
            {"text": ("So I printed every late night email and every weekend "
                      "text, and I brought them to the big meeting. I laid them "
                      "on the table and said, if hard work matters here, the "
                      "records speak for themselves. The room went silent, and "
                      "my boss never asked me to work a weekend again. Comment "
                      "if you would have done the same, and follow for the next story."),
             "query": "conference room silent"},
        ],
    },
    {
        "title": "My Neighbor Learned to Fear My Blender 🔊",
        "card_text": "He drummed at midnight, I blended at seven",
        "card_highlight_word": "blended",
        "hook": "He drummed at midnight, I blended at seven.",
        "genre": "revenge",
        "narrator_gender": "male",
        "key_words": ["neighbor", "drums", "blender", "apartment", "deal"],
        "bg_queries": ["apartment building night", "man sleeping bed",
                       "kitchen blender morning", "neighbors handshake",
                       "drum kit room", "morning kitchen light"],
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
                      "midnight, and politeness got me nowhere."),
             "query": "apartment building night"},
            {"text": ("So I learned his schedule. He slept until noon after "
                      "his night shifts, every single day."),
             "query": "man sleeping bed"},
            {"text": ("I bought the loudest blender I could find. "
                      "Every morning at seven, I made the thickest smoothie "
                      "of my life. "
                      "On day three, he knocked on my door, half asleep, and "
                      "begged for mercy. We made a deal that same morning. No "
                      "drums after ten, no blender before nine. We have been "
                      "perfect neighbors ever since. Comment your best petty "
                      "revenge below, and follow for the next story."),
             "query": "kitchen blender morning"},
        ],
    },
    {
        "title": "The Gas Station Phone Rings at 3 AM 📞😱",
        "card_text": "Do not turn around",
        "card_highlight_word": "around",
        "hook": "Do not turn around.",
        "genre": "scary",
        "narrator_gender": "female",
        "key_words": ["gas station", "night shift", "phone call", "cameras",
                      "hallway"],
        "bg_queries": ["gas station night", "old telephone ringing",
                       "security camera monitor", "dark hallway shadows",
                       "empty store night", "fluorescent lights flicker"],
        "tags": ["shorts", "storytime", "scary", "truestory", "stories"],
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
                      "three in the morning."),
             "query": "gas station night"},
            {"text": ("A small voice whispered, do not turn around, and then "
                      "the line went dead. My hands were shaking, but I checked "
                      "the cameras, and the store was empty."),
             "query": "security camera monitor"},
            {"text": ("Then every screen froze on the same frame, the back "
                      "hallway, where something tall was standing. I grabbed my "
                      "keys and ran, and I never looked back. The next night, "
                      "the phone rang again at exactly three. Comment if you "
                      "want part two, and follow for the next story."),
             "query": "dark hallway shadows"},
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
        "bg_queries": list(variant["bg_queries"]),
        "tags": list(variant["tags"]),
        "description": variant["description"],
        "ending_type": variant["ending_type"],
        "narrator_gender": variant.get("narrator_gender", ""),
        "pinned_comment": variant["pinned_comment"],
        "rehooks": [dict(r) for r in variant.get("rehooks", [])],
        "beats": [{"text": b["text"], "query": b["query"], "label": ""}
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
    out = _to_content(meta, slots, wps=2.9)
    out.extras["offline"] = True
    return out
