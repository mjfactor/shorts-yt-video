"""Ranking niche: Top-5 countdown Shorts (~2 min).

Self-contained: prompt, topic pool, offline fallback, history shape,
similarity rules, stock policy, still style, and leaderboard overlay.
Delete this file + its NICHES line in pipeline.py to drop the niche.
"""
import core as C

MODE = "ranking"
NICHE_LABEL = "top-5 rankings and countdowns"

# Ranking topics: evergreen, factual, filmable with stock footage. One is
# drawn per run and forced into the prompt; history.json excludes recent ones.
TOPICS = (
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


def build_prompt(history: list[dict]):
    """Gemini prompt for a Top-5 countdown (~2 min). Returns (prompt, slots)."""
    history = history or []
    recent_topics = {str(h.get("topic", "")).casefold()
                     for h in history[-C.HISTORY_PROMPT_N:]
                     if h.get("niche", "horror") == MODE}
    topic = C._pick_avoiding(TOPICS, recent_topics)
    slots = {"topic": topic}
    C.log(f"Ranking topic: {topic}")
    prompt = (
        f"Write a Top-5 countdown YouTube Shorts script about the {topic}. "
        "It must be complete in this one video: no part 1/2, no episode markers. "
        f"{C.history_block(history, MODE, format_history_line, AVOID_INTRO, AVOID_OUTRO)}"
        "Return ONLY valid JSON with keys: title (<=60 chars, scary-hook style, "
        "e.g. 'Top 5 ... — Number 1 Is Terrifying'; vary the title shape every run), "
        "narration (280-300 words so the video runs about 2 minutes / ~120 seconds, "
        "present tense, fifth-grade reading level, short punchy sentences, one clear "
        "idea per sentence; structure: a 1-2 sentence hook that teases number 1 "
        "without naming it, then the countdown from number 5 up to number 1 where "
        "each entry names the item plus 1-2 striking concrete facts with ROUNDED "
        "approximate numbers like 'over 800 meters', then a 1-sentence closer that "
        "asks viewers to comment their own ranking; use commas and ellipses for "
        "dramatic pauses before each reveal), "
        "description (2 sentences + 3 hashtags), "
        "tags (array of 5 strings), "
        "items (array of the 5 ranked item names, number 5 first, number 1 last; "
        "keep each name SHORT, 1-3 words, since it is also shown on screen), "
        "beats (array of 7 objects in video order: hook, number 5, number 4, "
        "number 3, number 2, number 1, closer; each {text: that beat's exact "
        "narration chunk, query: literal stock-video search showing exactly that "
        "beat's subject, e.g. 'lion walking savanna', label: the rank digit "
        "burned big on screen — '5','4','3','2','1' for the five item beats in "
        "order, '' for hook and closer}; "
        "beat texts joined must equal the full narration). "
        "Facts must be widely known and uncontroversial — NEVER invent precise "
        "statistics, dates, or records; approximate openly ('about', 'over', 'nearly')."
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
        "It can wait without moving for hours, barely breathing. Then it explodes out of the water in half a second. "
        "No animal in its river stands a chance once those jaws lock shut. "
        "Number 4, the hippopotamus. Nearly 2 tonnes of pure muscle hiding under the river surface. "
        "It looks slow and lazy, floating with just its eyes above the water. It is not slow. "
        "A hippo can outrun a human on land, easily, and it is one of the most dangerous animals in Africa. "
        "Number 3, the giraffe. Almost 6 meters tall, towering over every other land animal alive. "
        "Its heart alone weighs as much as a grown man, just to pump blood all the way up that neck. "
        "And when it drinks, it must spread its front legs wide, dropping its head into the most vulnerable position in nature. "
        "Number 2, the African bush elephant. Over 6 tonnes. The heaviest animal walking the Earth today. "
        "One step shakes the ground. One charge flattens trees like grass. "
        "Yet it greets family members by touching trunks, and it mourns its dead for days. "
        "And number 1, the blue whale. Nearly 30 meters long. Over 100 tonnes. "
        "Its heart is the size of a small car. Its call travels across entire oceans. "
        "A single baby drinks hundreds of liters of milk every single day. "
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
        "title": "Top 5 Largest Animals on Earth #shorts",
        "narration": narration,
        "description": "The 5 largest animals on Earth, ranked. #shorts #animals #top5",
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


def similarity_reason(data: dict, slots: dict, recent: list[dict]):
    """Repeat reason for same-topic or shared ranked items, else None."""
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
            "items": list(meta.get("items", [])[:10])}


def beat_anchor(beats: list[dict]) -> str:
    """Ranking stills are standalone subjects — no continuity anchor."""
    return ""


def image_prompt(beat_text: str, query: str, anchor: str = "") -> str:
    """One vibrant subject photo for a countdown beat (CF fallback)."""
    core = C._clean_beat_text(beat_text, limit=700)
    return (
        "Create one single vertical 9:16 vibrant cinematic wildlife/nature "
        "photograph for this exact countdown beat. Show one clear subject only: "
        "the main animal, object, or place, the visible action, and the location. "
        "Use the visual search cue as the scene anchor. "
        "Make the subject obvious at a glance, with realistic anatomy, natural "
        "daylight or golden-hour lighting, strong foreground subject, and a "
        "readable silhouette. Do not illustrate abstract words literally. "
        "No collage, no split screen, no multiple scenes, no text, no subtitles, "
        "no watermark, no logo, no horror styling, no blood. "
        f"Narration beat: {core} "
        f"Visual search cue: {query[:180]}. "
        "Sharp focus, vivid natural colors, photorealistic, "
        "vertical composition, subject centered for a YouTube Short."
    )


# Leaderboard geometry (1080x1920 canvas).
_BOARD_X = 60
_BOARD_Y = 520
_BOARD_ROW_H = 100
_BOARD_FONT_SIZE = 56
_BOARD_NAME_LEN = 14


def _clean_board_text(name: str, limit: int = _BOARD_NAME_LEN) -> str:
    """Short ASCII-only board label (drawtext uses one font, no emoji)."""
    import re
    keep = re.sub(r"[^A-Za-z0-9 .'&,!?()\-]", "", name or "")
    keep = re.sub(r"\s+", " ", keep).strip()
    return keep[:limit].rstrip()


def _escape_drawtext(text: str) -> str:
    """Escape a value for drawtext filter syntax (order matters)."""
    return (text.replace("\\", "\\\\").replace(":", "\\:")
                 .replace(",", "\\,").replace("'", "\\'"))


def board_filter(beats, items, durations, get_font, has_drawtext) -> str:
    """Persistent leaderboard overlay (trailing comma) or "" when N/A.

    Every rank slot ("1."–"5.") is on screen from the start; each fills in
    with its item name the moment that rank is revealed, with the current
    rank highlighted. get_font/has_drawtext are core probes, called lazily
    so horror runs never pay for (or warn about) them.
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
        esc_name = _escape_drawtext(f"{r}. {name}")
        fill = (f"drawtext=fontfile='{esc_font}':text='{esc_name}':"
                f"fontsize={_BOARD_FONT_SIZE}:fontcolor=white:"
                f"borderw=2:bordercolor=black:x={_BOARD_X}:y={y}:"
                f"enable='between(t,{reveal:.2f},{total:.2f})'")
        hot = (f"drawtext=fontfile='{esc_font}':text='{esc_name}':"
               f"fontsize={_BOARD_FONT_SIZE}:fontcolor=red:"
               f"borderw=2:bordercolor=black:x={_BOARD_X}:y={y}:"
               f"enable='between(t,{reveal:.2f},{end:.2f})'")
        parts += [fill, hot]
    return ",".join(parts) + ","
