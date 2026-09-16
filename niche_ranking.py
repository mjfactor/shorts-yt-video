"""Ranking niche: Top-5 countdown Shorts (~2 min).

Self-contained: prompt, topic pool, offline fallback, history shape,
similarity rules, stock policy, still style, and leaderboard overlay.
Delete this file + its NICHES line in pipeline.py to drop the niche.
"""
import json
import os
import re

import core as C

MODE = "ranking"
NICHE_LABEL = "top-5 rankings and countdowns"

# Ranking topics: evergreen, factual, filmable with stock footage. One is
# drawn per run and forced into the prompt; history.json excludes recent ones.
# Rules for adding more: must rank 5 distinct, visible subjects (1-3 word
# names, stock-searchable), with broadly known rounded facts — no precise
# stats, no obscure items Gemini would invent, no near-dupes of existing
# entries (same topic or >=2 shared items triggers a wasted regen).
TOPICS = (
    # Launch set: animals, geography, structures, vehicles.
    "largest animals on Earth",
    "most dangerous animals in the world",
    "fastest animals on the planet",
    "tallest buildings in the world",
    "tallest mountains on Earth",
    "longest rivers in the world",
    "largest deserts on Earth",
    "deepest places in the ocean",
    "largest islands in the world",
    "most venomous snakes in the world",
    "strongest animals pound for pound",
    "smartest animals in the world",
    "biggest dog breeds in the world",
    "largest cat breeds in the world",
    "fastest production cars in the world",
    "biggest passenger airplanes ever built",
    "largest stadiums in the world",
    "tallest statues in the world",
    "largest pyramids in the world",
    "deepest lakes in the world",
    "most remote inhabited places on Earth",
    "largest volcanoes in the world",
    "biggest sharks that ever lived",
    "most expensive things ever sold",
    # Animals: birds, reptiles, sea life, records, pets.
    "largest birds on Earth",
    "fastest birds on the planet",
    "most colorful birds in the world",
    "largest reptiles on Earth",
    "longest snakes in the world",
    "biggest bears on Earth",
    "biggest wild cats in the world",
    "largest primates in the world",
    "largest owls in the world",
    "largest eagles in the world",
    "fastest sea animals in the ocean",
    "largest fish in the ocean",
    "most dangerous creatures in the ocean",
    "longest-lived animals on Earth",
    "strongest bite forces in the animal kingdom",
    "loudest animals in the world",
    "smartest dog breeds in the world",
    "fastest dog breeds in the world",
    "most popular pets in the world",
    "largest insects in the world",
    # Geography and nature.
    "tallest waterfalls in the world",
    "largest lakes in the world",
    "longest mountain ranges on Earth",
    "largest glaciers in the world",
    "most active volcanoes in the world",
    "deepest canyons on Earth",
    "largest coral reefs in the world",
    "biggest waves for surfing in the world",
    "tallest trees on Earth",
    "largest flowers in the world",
    "hottest places on Earth",
    "coldest inhabited places on Earth",
    "longest coastlines in the world",
    "most famous beaches in the world",
    "most dangerous roads in the world",
    # Structures, cities, landmarks.
    "longest bridges in the world",
    "largest castles in the world",
    "largest temples in the world",
    "largest dams in the world",
    "longest tunnels in the world",
    "largest airports in the world",
    "tallest roller coasters in the world",
    "biggest Ferris wheels in the world",
    "largest cruise ships in the world",
    "most famous landmarks in the world",
    "most visited cities in the world",
    "most visited museums in the world",
    "largest aquariums in the world",
    "oldest buildings on Earth",
    "biggest shipwrecks in history",
    # Vehicles and machines.
    "largest cargo airplanes ever built",
    "fastest fighter jets ever built",
    "largest helicopters ever built",
    "fastest motorcycles in the world",
    "fastest trains in the world",
    "largest yachts in the world",
    "largest submarines ever built",
    # Space, sports, food.
    "largest planets in the solar system",
    "biggest moons in the solar system",
    "most popular sports in the world",
    "hottest chili peppers in the world",
    "most expensive fruits in the world",
)

AVOID_INTRO = ("Already-used countdowns — do NOT reuse any of these topics, "
               "titles, or ranked items, and do NOT rank the same 5 things "
               "under a reworded topic:\n")
AVOID_OUTRO = ("Your countdown must use a DIFFERENT topic and 5 DIFFERENT "
               "items from every entry above. ")

# Stock policy: each cut searches the beat's OWN query (literal item match —
# this is the niche's advantage: every subject is filmable stock).
USE_BEAT_QUERY = True
AMBIENT_QUERIES = ()
USE_MOOD = False


def format_history_line(h: dict) -> str:
    items = ", ".join(h.get("items", [])[:5])
    return (f"- \"{h.get('title', '?')}\" "
            f"(topic={h.get('topic', '?')}; items: {items})")


# Hybrid topics: Gemini may propose a fresh countdown topic each run, but the
# pool stays authoritative — a proposal only airs when it passes validation
# (fresh vs history, sane shape, 5 concrete filmable subjects). Anything else
# falls back to a pool draw, so the run never depends on the extra call.
# Kill switch (no workflow change needed): AI_TOPICS=off.
AI_TOPICS = os.getenv("AI_TOPICS", "on").strip().lower() not in (
    "0", "off", "false", "no")
AI_TOPIC_TRIES = 2  # proposal rounds before giving up to the pool


def _recent_topic_names(history: list[dict]) -> set:
    """Casefolded topics from recent same-niche history entries."""
    return {str(h.get("topic", "")).casefold()
            for h in (history or [])[-C.HISTORY_PROMPT_N:]
            if h.get("niche", "horror") == MODE}


def propose_topic(history: list[dict]) -> tuple[str | None, list[str] | None]:
    """Ask Gemini for one fresh countdown topic. Returns (topic, items_hint).

    (None, None) on any failure or rejection — the caller falls back to the
    pool. Uses the shared key rotation, so quota pain is minimal (1-2 calls).
    """
    if not AI_TOPICS or not C.GEMINI_KEYS:
        return None, None
    recent = _recent_topic_names(history)
    avoid = "".join(f"- {h.get('topic', '?')}\n"
                    for h in (history or [])[-C.HISTORY_PROMPT_N:]
                    if h.get("niche", "horror") == MODE)
    prompt = (
        "Propose ONE Top-5 countdown YouTube Shorts topic. Reply with ONLY "
        "valid JSON: {\"topic\": <noun phrase>, \"items\": [5 names]}. "
        "Rules: evergreen and factual; rank 5 distinct real subjects a camera "
        "could film with stock footage (animals, places, vehicles, structures, "
        "food — NO people names, NO events, NO abstract concepts); each item "
        "1-3 words, unique, plain ASCII. Topic examples: 'largest owls on "
        "Earth', 'longest bridges in the world'. No questions, no hooks, "
        "no clickbait, no markdown, no extra keys.\n"
        f"Do NOT reuse any of these recent topics or their subjects:\n{avoid or '(none yet)'}\n"
        "Output only the JSON object."
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    for round_no in range(1, AI_TOPIC_TRIES + 1):
        data = None
        for model in C.GEMINI_MODELS:
            try:
                url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                       f"{model}:generateContent")
                raw = C._fetch_gemini(url, payload)
                text = raw["candidates"][0]["content"]["parts"][0]["text"]
                text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                data = json.loads(text)
                break
            except Exception as e:
                C.log(f"AI topic model {model} unusable ({e}), trying next...")
        if not isinstance(data, dict):
            continue
        topic = str(data.get("topic", "")).strip()
        raw_items = data.get("items", [])
        items = [str(i).strip() for i in raw_items
                 if isinstance(i, str) and str(i).strip()]
        items = list(dict.fromkeys(items))[:5]
        problems = []
        if not (8 <= len(topic) <= 80) or "?" in topic:
            problems.append("bad topic shape")
        if topic.casefold() in recent:
            problems.append("topic already used")
        if len(items) < 5:
            problems.append("fewer than 5 subjects")
        if any(len(i.split()) > 4 or len(i) > 40 for i in items):
            problems.append("unfilmable subject names")
        if problems:
            C.log(f"AI topic rejected ({'; '.join(problems)}), "
                  f"round {round_no}/{AI_TOPIC_TRIES}.")
            continue
        C.log(f"AI topic accepted: {topic}")
        return topic, items
    return None, None


def _items_hint_line(hint: list[str] | None) -> str:
    """Nudge the script toward the proposed subjects without trusting them."""
    if not hint:
        return ""
    return (f"Suggested subjects (verify each against known facts; swap out "
            f"any that does not fit): {', '.join(hint)}. ")


def build_prompt(history: list[dict]):
    """Gemini prompt for a Top-5 countdown (~2 min). Returns (prompt, slots)."""
    history = history or []
    recent_topics = _recent_topic_names(history)
    topic, hint = propose_topic(history)
    source = "ai" if topic else "pool"
    if topic is None:
        topic = C._pick_avoiding(TOPICS, recent_topics)
    slots = {"topic": topic, "topic_source": source}
    C.log(f"Ranking topic ({source}): {topic}")
    prompt = (
        f"Write a Top-5 countdown YouTube Shorts script about the {topic}. "
        "This must be a single complete video with no part 1/2, no episode markers, and no recap setup. "
        f"{C.history_block(history, MODE, format_history_line, AVOID_INTRO, AVOID_OUTRO)}"
        f"{_items_hint_line(hint)}"
        "Return ONLY valid JSON with exactly these keys: title, narration, description, tags, items, beats. "
        "Title: plain ranked-list format, must start with '5' and state exactly what is ranked. Example valid titles: '5 Largest Animals on Earth' or '5 Most Expensive Things Ever Sold'. Keep it under 60 characters, no question hook, no curiosity-gap phrasing, no 'which' or 'where' setup, no 'Number 1...' suffix, no clickbait. Do not use any banned title hooks such as 'Number 1 Is Terrifying', 'Number One Is Terrifying', '#1 Is Terrifying', 'Will Shock You', or 'You Won't Believe'. One emoji is allowed only at the very start or end, but the on-screen banner still shows the same title without the emoji. "
        "Narration: 200-220 words, present tense, fifth-grade reading level, short punchy sentences, one clear idea per sentence, no emojis, spoken aloud. Structure: 1-2 sentence hook teasing number 1 without naming it; then the countdown from number 5 to number 1, each entry naming the item and 1-2 rounded, widely known facts using approximate language like 'about', 'over', or 'nearly'; then a 1-sentence closer asking viewers to comment their own ranking. Use commas and ellipses for dramatic pauses before each reveal. "
        "Description: exactly 2 sentences, include 1-2 fitting emojis and 3 hashtags, but emojis are metadata-only and never burned into the video. "
        "Tags: array of exactly 5 plain-ASCII strings, no emoji. "
        "Items: array of exactly 5 ranked item names, number 5 first and number 1 last. Keep each name short, 1-3 words, unique, and readable on screen. "
        "Beats: array of exactly 7 objects in video order: hook, number 5, number 4, number 3, number 2, number 1, closer. Each object must be {\"text\": <exact narration chunk>, \"query\": <literal stock-video search phrase for that subject>, \"label\": <\"\" for hook and closer, or \"5\", \"4\", \"3\", \"2\", \"1\" for the five ranked beats>}. "
        "Hard validation rules: (1) the 7 beat texts joined in order must equal the full narration exactly, (2) each of the 5 items must appear in the narration, (3) all 5 item names must be unique, (4) the chosen topic and ranked items must differ from recent history, (5) facts must be broadly known and safe; do not invent precise dates, records, or statistics. Example valid beat object: {'text': 'At number 5, the saltwater crocodile. Over 6 meters long.', 'query': 'saltwater crocodile water surface', 'label': '5'}. "
        "Do not include markdown, notes, or any extra keys. Output only the JSON object."
    )
    return prompt, slots


def fallback_script(history: list[dict] | None = None) -> tuple[dict, dict]:
    """Offline Top-5 countdown (~2 min). Topic avoids recent history."""
    history = history or []
    recent_topics = {str(h.get("topic", "")).casefold()
                     for h in history[-C.HISTORY_PROMPT_N:]
                     if h.get("niche", "horror") == MODE}
    topic = C._pick_avoiding(TOPICS, recent_topics)
    # Fixed fallback set: largest animals (evergreen, stock-friendly).
    topic = "largest animals on Earth" if "largest animals on earth" not in recent_topics else topic
    items = ["Saltwater Crocodile", "Hippopotamus", "Giraffe",
             "African Bush Elephant", "Blue Whale"]
    narration = (
        "These are the 5 largest animals on Earth. And number 1 is so huge, it is bigger than a passenger jet. "
        "Stay until the end, because you will not believe how big it gets. "
        "At number 5, the saltwater crocodile. Over 6 meters long, with jaws that crush bone like paper. "
        "It waits without moving for hours. Then it explodes out of the water in half a second. "
        "Number 4, the hippopotamus. Nearly 2 tonnes of muscle hiding under the river surface. "
        "It looks slow and lazy. It is not slow. A hippo can outrun a human on land. "
        "Number 3, the giraffe. Almost 6 meters tall, towering over every land animal alive. "
        "Its heart alone weighs as much as a grown man, just to pump blood all the way up that neck. "
        "Number 2, the African bush elephant. Over 6 tonnes. The heaviest animal walking the Earth today. "
        "One step shakes the ground. One charge flattens trees like grass. "
        "And number 1, the blue whale. Nearly 30 meters long. Over 100 tonnes. "
        "Its heart is the size of a small car. Its call travels across entire oceans. "
        "Nothing alive today even comes close. Comment which giant shocked you most, and follow for the next top 5."
    )
    words = narration.split()
    beat_queries = ["crocodile eyes water surface", "hippopotamus river africa",
                    "giraffe savanna walking", "african elephant walking dust",
                    "blue whale underwater", "blue whale tail ocean",
                    "ocean waves aerial"]
    # 7 beats: hook, #5..#1, closer — split word-even across them.
    per = max(1, -(-len(words) // 7))
    rank_labels = ["", "5", "4", "3", "2", "1", ""]
    beats = []
    for i in range(7):
        chunk = " ".join(words[i * per:(i + 1) * per])
        if chunk:
            beats.append({"text": chunk, "query": beat_queries[i],
                          "label": rank_labels[i]})
    slots = {"topic": topic}
    return {
        "title": "\U0001F981 Top 5 Largest Animals on Earth #shorts",
        "narration": narration,
        "description": "\U0001F42C The 5 largest animals on Earth, ranked \U0001F30A. #shorts #animals #top5",
        "tags": ["shorts", "top5", "animals", "ranking", "countdown"],
        "items": items,
        "beats": beats,
    }, slots


def validate_extra(data: dict) -> dict:
    """Ranked-item list: validated, kept for history/board. Raises ValueError."""
    items = data.get("items", None)
    if items is None:
        return {}
    if (not isinstance(items, list) or not items
            or not all(isinstance(t, str) and t.strip() for t in items)):
        raise ValueError("'items' must be a non-empty list of strings")
    return {"items": [t.strip() for t in items if t.strip()][:10]}


# Overused title hooks: the prompt bans them, and this is the backstop —
# a draft using one is treated as a repeat (regenerated, best-effort) AND
# stripped by sanitize_title (deterministic — the regen alone leaks, since
# best-effort accept keeps the draft after SIMILARITY_RETRIES).
BANNED_TITLE_HOOKS = (
    "number 1 is terrifying",
    "number one is terrifying",
    "#1 is terrifying",
    "will shock you",
    "you won't believe",
    "you won\u2019t believe",
    "you wont believe",
)


# Suffix clauses that pollute titles when Gemini tacks them on
# ("5 Largest Animals — Number 1 Is Terrifying 😲"). sanitize_title strips
# them; titles that still don't match the "5 ..." channel format (question
# hooks etc.) are rebuilt from the run topic instead.
_BANNED_TITLE_RES = (
    r"\s*[–—\-:|]\s*number\s+1\s+is\s+terrifying.*$",
    r"\s*[–—\-:|]\s*number\s+one\s+is\s+terrifying.*$",
    r"\s*[–—\-:|]\s*#1\s+is\s+terrifying.*$",
    r"\s*[–—\-:|]\s*will\s+shock\s+you.*$",
    r"\s*[–—\-:|]\s*you\s+won['\u2019]?t\s+believe.*$",
)

_TITLE_FORMAT_RE = re.compile(r"^[\W_]*((top\s+)?5)\b", re.IGNORECASE)


def sanitize_title(title: str, slots: dict | None = None) -> str:
    """Enforce the channel title format. Never raises (returns a safe title).

    Strips banned-hook suffix clauses, then rebuilds from the run topic when
    the title still doesn't start with "5"/"Top 5" (leading emoji allowed).
    """
    t = (title or "").strip()
    for pat in _BANNED_TITLE_RES:
        t = re.sub(pat, "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s*[–—\-:|;,.]+\s*$", "", t).strip()
    low = t.casefold()
    # Suffix-strip misses mid-title hooks ("5 Cities You Won't Believe"),
    # so re-check the banned list on what remains.
    has_banned = any(hook in low for hook in BANNED_TITLE_HOOKS)
    if has_banned or not _TITLE_FORMAT_RE.match(t) or len(t) < 12:
        topic = ((slots or {}).get("topic") or "").strip()
        t = f"5 {topic[0].upper() + topic[1:]}" if topic else "Top 5 Countdown"
    return t[:100].strip() or "Top 5 Countdown"


def similarity_reason(data: dict, slots: dict, recent: list[dict]):
    """Repeat reason for same-topic, shared ranked items, or banned title
    hook, else None."""
    title = str(data.get("title", "")).casefold()
    for hook in BANNED_TITLE_HOOKS:
        if hook in title:
            return f"title uses banned overused hook '{hook}'"
    if str(slots.get("topic", "")).casefold() in {
            str(h.get("topic", "")).casefold() for h in recent}:
        return f"topic '{slots.get('topic')}' already used"
    new_items = {str(i).strip().casefold()
                 for i in (data.get("items") or []) if str(i).strip()}
    for h in recent:
        old_items = {str(i).strip().casefold()
                     for i in (h.get("items") or []) if str(i).strip()}
        if new_items and old_items and len(new_items & old_items) >= 2:
            return (f"shares ranked items with '{h.get('title', '?')}' "
                    f"({sorted(new_items & old_items)[:3]})")
    return None


def history_fields(slots: dict, meta: dict) -> dict:
    return {"topic": slots.get("topic", ""),
            "topicSource": slots.get("topic_source", "pool"),
            "items": list(meta.get("items", [])[:10])}


# Leaderboard geometry (1080x1920 canvas).
_BOARD_X = 60
_BOARD_Y = 710
_BOARD_ROW_H = 100
_BOARD_FONT_SIZE = 66
_BOARD_NAME_LEN = 80
_BOARD_MAX_WIDTH = C.W - (_BOARD_X * 2)
_BOARD_MIN_FONT_SIZE = 40


def _clean_board_text(name: str, limit: int = _BOARD_NAME_LEN) -> str:
    """Short ASCII-only board label (drawtext uses one font, no emoji)."""
    import re
    normalized = (name or "").replace("—", " - ").replace("–", " - ").replace("'", "")
    keep = re.sub(r"[^A-Za-z0-9 .&,!?()\-]", "", normalized)
    keep = re.sub(r"\s+", " ", keep).strip()
    if len(keep) <= limit:
        return keep
    clipped = keep[:limit].rsplit(" ", 1)[0].rstrip()
    return clipped or keep[:limit].rstrip()


def _escape_drawtext(text: str) -> str:
    """Escape a value for drawtext filter syntax (order matters).

    A literal single quote inside text='...' must close the quote, add an
    escaped quote, and reopen: ' -> '\\'' (e.g. Crime d'Amour ->
    'Crime d'\\''Amour'). Plain \\' unbalances the quoting and splits the
    filtergraph at the next comma (No such filter: '26.62').
    """
    return (text.replace("\\", "\\\\").replace(":", "\\:")
                 .replace(",", "\\,").replace("'", "'\\''"))


def _board_font_size(text: str, font_path: str) -> int:
    """Shrink only long leaderboard labels to fit the safe board width."""
    try:
        from PIL import ImageFont
        for size in range(_BOARD_FONT_SIZE, _BOARD_MIN_FONT_SIZE - 1, -1):
            font = ImageFont.truetype(font_path, size)
            width = font.getlength(text) if hasattr(font, "getlength") else font.getbbox(text)[2]
            if width <= _BOARD_MAX_WIDTH:
                return size
    except Exception:
        pass
    return _BOARD_MIN_FONT_SIZE


def _fit_board_label(display_name: str, font_path: str) -> tuple[int, str]:
    """(size, text) guaranteed to fit the safe width: shrink first, then
    word-truncate only the pathological remainder (real item names are
    1-3 words and never reach this). Keeps the "N. " prefix."""
    size = _board_font_size(display_name, font_path)
    text = display_name
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(font_path, size)
        get = (font.getlength if hasattr(font, "getlength")
               else lambda t: font.getbbox(t)[2])
        while get(text) > _BOARD_MAX_WIDTH and " " in text.strip():
            text = text.strip().rsplit(" ", 1)[0].rstrip()
    except Exception:
        pass
    return size, text


def board_filter(beats, items, durations, get_font, has_drawtext,
                 title: str = "") -> str:
    """Persistent leaderboard overlay (trailing comma) or "" when N/A.

    Every rank slot ("1."–"5.") is on screen from the start; each fills in
    with its item name the moment that rank is revealed, with the current
    rank highlighted. get_font/has_drawtext are core probes, called lazily.
    """
    labeled = [(i, b.get("label", "")) for i, b in enumerate(beats or [])
               if str(b.get("label", "")).isdigit()]
    if not labeled:
        return ""
    font = get_font()
    if font is None or not has_drawtext():
        return ""
    starts: list[float] = []
    t = 0.0
    for d in durations or []:
        starts.append(t)
        try:
            t += max(0.0, float(d))
        except (TypeError, ValueError):
            pass
    total = t if t > 0 else 9999.0
    # Pair labeled beats (reveal order, #5 first) with item names positionally.
    names = [_clean_board_text(n) for n in (items or [])]
    pairs = list(zip(labeled, names + [""] * len(labeled)))
    ranks = sorted({int(lab) for _, lab in labeled})
    row_of = {r: idx for idx, r in enumerate(ranks)}
    esc_font = font.replace(":", "\\:")
    parts = []
    # Keep a safe side margin. Longer titles wrap at word boundaries instead
    # of being clipped or truncated.
    clean_title = _clean_board_text(title, 100)
    title_words = clean_title.split()
    title_lines = []
    line = ""
    for word in title_words:
        candidate = f"{line} {word}".strip()
        if line and len(candidate) > 24:
            title_lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        title_lines.append(line)
    if title_lines:
        title_y = 140 - (len(title_lines) - 1) * 36
        for line_no, title_line in enumerate(title_lines):
            parts.append(
                f"drawtext=fontfile='{esc_font}':text='{_escape_drawtext(title_line)}':"
                f"fontsize=70:fontcolor=white:borderw=3:bordercolor=black:"
                f"x=(w-text_w)/2:y={title_y + line_no * 72}")
    for r in ranks:
        y = _BOARD_Y + row_of[r] * _BOARD_ROW_H
        base = (f"drawtext=fontfile='{esc_font}':text='{r}.':"
                f"fontsize={_BOARD_FONT_SIZE}:fontcolor=white@0.6:"
                f"borderw=2:bordercolor=black:x={_BOARD_X}:y={y}")
        parts.append(base)
    for (beat_idx, lab), name in pairs:
        if not name or beat_idx >= len(starts):
            continue
        r = int(lab)
        y = _BOARD_Y + row_of[r] * _BOARD_ROW_H
        reveal = starts[beat_idx]
        end = starts[beat_idx + 1] if beat_idx + 1 < len(starts) else total
        display_name = f"{r}. {name}"
        name_font_size, display_name = _fit_board_label(display_name, font)
        esc_name = _escape_drawtext(display_name)
        fill = (f"drawtext=fontfile='{esc_font}':text='{esc_name}':"
            f"fontsize={name_font_size}:fontcolor=white:"
                f"borderw=2:bordercolor=black:x={_BOARD_X}:y={y}:"
                f"enable='between(t,{reveal:.2f},{total:.2f})'")
        hot = (f"drawtext=fontfile='{esc_font}':text='{esc_name}':"
               f"fontsize={name_font_size}:fontcolor=red:"
               f"borderw=2:bordercolor=black:x={_BOARD_X}:y={y}:"
               f"enable='between(t,{reveal:.2f},{end:.2f})'")
        parts += [fill, hot]
    return ",".join(parts) + ","
