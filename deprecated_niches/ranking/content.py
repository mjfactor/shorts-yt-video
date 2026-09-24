"""Ranking stage 1: Top-5 countdown script Content via engine.gemini_client.

Moved from niche_ranking.py (prompt/fallback/validate/similarity verbatim
in behavior); core.* calls repointed to engine.* + local helpers.
"""
from __future__ import annotations

import json
import random
import re
import time

from engine import gemini_client as _gc
from engine.ffmpeg_utils import log, warn
from niches.base import Content

from .config import (BANNED_TITLE_HOOKS, HOOK_STYLES, MODE, TOPICS,
                     WEAK_OPENERS, RankingConfig, is_bare_homonym_query)

_TOPIC_CANON = (("biggest", "largest"), ("quickest", "fastest"),
                ("highest", "tallest"), ("deepest", "longest"))


def _canon_topic(topic: str) -> str:
    """Casefolded topic with synonym superlatives in canonical form."""
    t = str(topic or "").casefold()
    for variant, canon in _TOPIC_CANON:
        t = re.sub(rf"\b{variant}\b", canon, t)
    return re.sub(r"\s+", " ", t).strip()


_LEADING_SUPERLATIVES = ("largest", "biggest", "tallest", "longest",
                         "fastest", "most")


def _leading_superlative(topic: str) -> str:
    """First-word superlative of a topic ("" when it leads with none)."""
    words = re.findall(r"[a-z]+", str(topic or "").lower())
    if words and words[0] in _LEADING_SUPERLATIVES:
        return words[0]
    return ""


_VERTICAL_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("danger-work", ("farm machine", "fire truck", "fire", "diy",
                     "interceptor", "police", "tool", "send", "envies",
                     "kitchen", "nightmare", "rescue", "marathon",
                     "delivery", "van", "breakdown", "pileup", "highway",
                     "fog", "gym", "ski", "lift")),
    ("insider", ("secret", "gate agent", "chef", "never order", "never say",
                 "perk", "cruise", "subway", "thief", "thieves", "airport",
                 "theft", "locker", "motel", "camera", "hidden")),
    ("infra", ("bridge", "trucker", "winter road", "cargo ship",
               "skyscraper", "apartment", "lighthouse", "fear most",
               "pros drive", "save ships", "kayak", "ferry", "storm")),
    ("money", ("overcharge", "bankrupt", "stock", "stadium food",
               "hotel room trap", "supercar", "watch", "ticket",
               "cost more", "worth more", "luxury price", "trap", "scam",
               "deposit", "rental", "fee", "toll", "fortune", "cost",
               "pay")),
    ("animals", ("animal", "bird", "reptile", "snake", "bear", "wild cat",
                 "primate", "owl", "fish", "shark", "dog breed", "cat breed",
                 "pet", "insect", "spider", "crocodile", "hippo", "elephant",
                 "giraffe", "whale")),
    ("nature", ("mountain", "river", "desert", "ocean", "island", "lake",
                "volcano", "waterfall", "glacier", "canyon", "reef", "wave",
                "tree", "flower", "beach", "cliff", "cave", "forest",
                "national park", "chili", "planet", "moon", "star",
                "constellation", "space", "telescope")),
    ("structures", ("building", "castle", "temple", "dam", "tunnel",
                    "airport", "coaster", "ferris", "cruise ship", "landmark",
                    "city", "museum", "aquarium", "shipwreck", "tower",
                    "palace", "church", "mosque", "library", "market",
                    "lighthouse", "wall", "stadium", "pyramid", "statue")),
    ("vehicles", ("car", "airplane", "helicopter", "jet", "motorcycle",
                  "train", "yacht", "submarine", "truck", "container ship",
                  "rocket", "tank", "cable car", "race track", "sailing")),
    ("food-life", ("sport", "fruit", "cheese", "bread", "coffee",
                   "street food", "dessert", "theme park", "zoo",
                   "festival")),
)


def _topic_vertical(topic: str) -> str:
    """Vertical lane for a topic ("" when unclassifiable)."""
    t = str(topic or "").lower()
    for vertical, keywords in _VERTICAL_KEYWORDS:
        if any(k in t for k in keywords):
            return vertical
    return ""


_METRIC_WORDS = frozenset({
    "price", "cost", "expensive", "cheap", "overcharge", "bankrupt",
    "worth", "luxury", "ticket", "stock", "money", "paid", "beat",
    "fastest", "quickest", "speed", "fast", "slow", "interceptor",
    "police", "danger", "dangerous", "deadly", "fear", "risk", "trap",
    "thief", "thieves", "steal", "pro", "pros", "drive", "trucker",
    "winter", "secret", "never", "envy", "envies", "save", "tool",
    "diy", "fire", "agent", "largest", "biggest", "tallest", "longest",
    "smallest", "bigger", "big", "tall", "long", "small", "large",
    "tiny", "huge", "giant", "highest", "deepest",
})
_ER_TOKEN_RE = re.compile(r"\ber\b", re.IGNORECASE)


def _topic_has_metric(topic: str) -> bool:
    """True when the topic names a price/speed/danger/size rank metric."""
    t = str(topic or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", t))
    if _ER_TOKEN_RE.search(t):
        return True
    return any(w in _METRIC_WORDS for w in tokens
               ) or any(w in t for w in ("diy", "trucker", "skyscraper"))


def _title_clause(title: str) -> str:
    """Closing clause after the last dash/colon split, casefolded ("" if none)."""
    t = str(title or "").strip()
    t = re.sub(r"^[\W_]*((top\s+)?5)\b", "", t, flags=re.IGNORECASE).strip()
    parts = re.split(r"[–—\-:|;,.]+", t)
    if len(parts) < 2:
        return ""
    return re.sub(r"\s+", " ", parts[-1].strip().casefold())


AVOID_INTRO = ("Already-used countdowns — do NOT reuse any of these topics, "
               "titles, or ranked items, and do NOT rank the same 5 things "
               "under a reworded topic:\n")
AVOID_OUTRO = ("Your countdown must use a DIFFERENT topic and 5 DIFFERENT "
               "items from every entry above. ")


def format_history_line(h: dict) -> str:
    """One prompt line for a past entry."""
    items = ", ".join(h.get("items", [])[:5])
    return (f"- \"{h.get('title', '?')}\" "
            f"(topic={h.get('topic', '?')}; items: {items})")


def history_block(history: list[dict], prompt_n: int) -> str:
    """Prompt section listing recent same-niche entries to avoid."""
    recent = [h for h in (history or [])[-prompt_n:]
              if h.get("niche", "horror") == MODE]
    if not recent:
        return ""
    return AVOID_INTRO + "\n".join(format_history_line(h) for h in recent) + "\n" + AVOID_OUTRO


def _pick_avoiding(pool: tuple, recent: set, history: list[dict] | None = None):
    """Random fresh pool pick; LRU fallback when the pool is exhausted."""
    folded = {str(v).casefold() for v in recent}
    fresh = [v for v in pool if str(v).casefold() not in folded]
    if fresh:
        return random.choice(fresh)
    if history:
        last_seen: dict[str, int] = {}
        for pos, h in enumerate(history):
            t = str(h.get("topic", "")).casefold()
            if t:
                last_seen[t] = pos
        recency = lambda v: last_seen.get(str(v).casefold(), -1)
        oldest = min(recency(v) for v in pool)
        return random.choice([v for v in pool if recency(v) == oldest])
    return random.choice(pool)


def _recent_topic_names(history: list[dict], dedup_window: int) -> set:
    """Casefolded same-niche topics over the enforced history window."""
    return {str(h.get("topic", "")).casefold()
            for h in (history or [])[-dedup_window:]
            if h.get("niche", "horror") == MODE}


def _make_client(cfg: RankingConfig) -> _gc.GeminiClient | None:
    """Engine client from cfg keys (env fallback); None when keyless."""
    keys = list(cfg.gemini_keys) or _gc.parse_keys()
    if not keys:
        return None
    return _gc.GeminiClient(keys, timeout=cfg.timeout,
                            tts_timeout=cfg.tts_timeout,
                            tries=cfg.tries, backoff=cfg.backoff)


def propose_topic(history: list[dict], cfg: RankingConfig,
                  client: _gc.GeminiClient | None) -> tuple[str | None, list[str] | None]:
    """Ask Gemini for one fresh countdown topic; (None, None) → pool fallback."""
    if not cfg.ai_topics or client is None:
        return None, None
    recent = _recent_topic_names(history, cfg.dedup_window)
    same_niche = [h for h in (history or []) if h.get("niche", "horror") == MODE]
    excluded_verticals = {_topic_vertical(h.get("topic", "")) for h in same_niche[-2:]}
    excluded_verticals.discard("")
    last_superlative = (_leading_superlative(same_niche[-1].get("topic", ""))
                        if same_niche else "")
    avoid = "".join(f"- {h.get('topic', '?')}\n"
                    for h in (history or [])[-cfg.history_prompt_n:]
                    if h.get("niche", "horror") == MODE)
    rotation_rule = (
        f" Rotate verticals: do NOT propose these recently aired lanes: "
        f"{sorted(excluded_verticals) or '(none yet)'}. "
        if excluded_verticals else " ")
    superlative_rule = (
        f" Do NOT start the topic with the superlative '{last_superlative}'. "
        if last_superlative else " ")
    prompt = (
        "Propose ONE Top-5 countdown YouTube Shorts topic. Reply with ONLY "
        "valid JSON: {\"topic\": <noun phrase>, \"items\": [5 names]}. "
        "Rules: evergreen and factual; rank 5 distinct real subjects with abundant "
        "stock footage on free stock sites like Pexels (popular animals, famous places, "
        "common vehicle types, iconic structures; avoid ultra-rare species or obscure "
        "subspecies that lack stock footage — NO people names, NO events, NO abstract concepts); "
        "rank by ONE concrete metric (price, speed, danger, or size); keep the "
        "topic itself hookable and under 40 characters; "
        "each item 1-3 words, unique, plain ASCII. Topic examples: 'largest owls on "
        "Earth', 'longest bridges in the world'. No questions, no hooks, "
        "no clickbait, no markdown, no extra keys.\n"
        f"{rotation_rule}{superlative_rule}"
        f"Do NOT reuse any of these recent topics or their subjects:\n{avoid or '(none yet)'}\n"
        "Output only the JSON object."
    )
    for round_no in range(1, cfg.ai_topic_tries + 1):
        data = None
        for model in cfg.text_models:
            try:
                data = client.generate_json(model, prompt, generation_config={
                    "temperature": 0.8, "maxOutputTokens": 1024})
                break
            except Exception as e:
                log(f"AI topic model {model} unusable ({e}), trying next...")
                time.sleep(2)
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
        if len(topic) >= 40:
            problems.append("topic not hookable (40+ chars)")
        if not _topic_has_metric(topic):
            problems.append("no price/speed/danger/size rank metric")
        if excluded_verticals and _topic_vertical(topic) in excluded_verticals:
            problems.append(f"vertical '{_topic_vertical(topic)}' aired in last 2 runs")
        if last_superlative and _leading_superlative(topic) == last_superlative:
            problems.append(f"leading superlative '{last_superlative}' aired last run")
        if _canon_topic(topic) in {_canon_topic(t) for t in recent}:
            problems.append("topic already used")
        if len(items) < 5:
            problems.append("fewer than 5 subjects")
        if any(len(i.split()) > 4 or len(i) > 40 for i in items):
            problems.append("unfilmable subject names")
        if not problems:
            hint_set = {i.casefold() for i in items}
            for h in (history or [])[-cfg.dedup_item_window:]:
                if h.get("niche", "horror") != MODE:
                    continue
                old_items = {str(i).strip().casefold()
                             for i in (h.get("items") or []) if str(i).strip()}
                if hint_set and old_items and len(hint_set & old_items) >= 2:
                    problems.append(f"subjects already ranked in '{h.get('title', '?')}'")
                    break
        if problems:
            log(f"AI topic rejected ({'; '.join(problems)}), round {round_no}/{cfg.ai_topic_tries}.")
            continue
        log(f"AI topic accepted: {topic}")
        return topic, items
    return None, None


def _items_hint_line(hint: list[str] | None) -> str:
    """Nudge the script toward proposed subjects without trusting them."""
    if not hint:
        return ""
    return (f"Suggested subjects (verify each against known facts; swap out "
            f"any that does not fit): {', '.join(hint)}. ")


def build_prompt(history: list[dict], cfg: RankingConfig,
                 client: _gc.GeminiClient | None = None) -> tuple[str, dict]:
    """Gemini prompt for a Top-5 countdown. Returns (prompt, slots)."""
    history = history or []
    recent_topics = _recent_topic_names(history, cfg.dedup_window)
    topic, hint = propose_topic(history, cfg, client)
    source = "ai" if topic else "pool"
    if topic is None:
        topic = _pick_avoiding(TOPICS, recent_topics, history)
    slots = {"topic": topic, "topic_source": source}
    hook_style, hook_direction = random.choice(HOOK_STYLES)
    slots["hook_style"] = hook_style
    log(f"Ranking topic ({source}): {topic}")
    prompt = (
        f"Write a Top-5 countdown YouTube Shorts script about the {topic}. "
        "This must be a single complete video with no part 1/2, no episode markers, and no recap setup. "
        f"{history_block(history, cfg.history_prompt_n)}"
        f"{_items_hint_line(hint)}"
        "Return ONLY valid JSON with exactly these keys: title, narration, description, tags, items, beats. "
        "Title: plain ranked-list format, must start with '5' and state exactly what is ranked. Example valid titles: '5 Largest Animals on Earth' or '5 Most Expensive Things Ever Sold'. Keep it under 60 characters, no question hook, no curiosity-gap phrasing, no 'which' or 'where' setup, no 'Number 1...' suffix, no clickbait. Do not use any banned title hooks such as 'Number 1 Is Terrifying', 'Number One Is Terrifying', '#1 Is Terrifying', 'Will Shock You', or 'You Won't Believe'. One emoji is allowed only at the very start or end, but the on-screen banner still shows the same title without the emoji. "
        "Narration: exactly 195-205 words (~80-85 seconds spoken at 145 WPM), present tense, fifth-grade reading level, short punchy sentences, one clear idea per sentence, no emojis, spoken aloud. Structure: 1-2 sentence hook teasing number 1 without naming it, written in this rotating style — "
        f"{hook_direction} "
        "The hook must contain ONE concrete stakes claim about number 1 (a rounded number or a size, weight, or height comparison using approximate language like 'about', 'over', or 'nearly'). Never open the narration with any of these stale openers: 'these are the 5', 'human beings love', 'deep beneath our feet', 'nature hides', 'push human engineering to'. "
        "Then the countdown from number 5 to number 1, each entry naming the item and 1-2 rounded, widely known facts using approximate language like 'about', 'over', or 'nearly'; then a 1-sentence closer asking viewers to comment their own ranking. Use commas and ellipses for dramatic pauses before each reveal. "
        "Description: exactly 2 sentences, include 1-2 fitting emojis and 3 hashtags, but emojis are metadata-only and never burned into the video. "
        "Tags: array of exactly 5 plain-ASCII strings, no emoji. "
        "Items: array of exactly 5 ranked item names, number 5 first and number 1 last. Keep each name short, 1-3 words, unique, and readable on screen. "
        "Beats: array of exactly 7 objects in video order: hook, number 5, number 4, number 3, number 2, number 1, closer. Each object must be {\"text\": <exact narration chunk>, \"query\": <clean stock search query, 1-3 words; for proper names, vehicles, ships, yachts, buildings, or machines that do not contain the subject noun, ALWAYS append the subject noun (e.g. 'Eclipse yacht', 'Dubai yacht', 'Al Said yacht', 'Chiron supercar', 'Burj Khalifa tower'); for animals/nature use the exact species or breed name (e.g. 'saltwater crocodile', 'cheetah'); do NOT append habitat nouns or scenery words like 'water', 'desert', 'forest'>, \"label\": <\"\" for hook and closer, or \"5\", \"4\", \"3\", \"2\", \"1\" for the five ranked beats>}. "
        "Hard validation rules: (1) the 7 beat texts joined in order must equal the full narration exactly, (2) each of the 5 items must appear in the narration, (3) all 5 item names must be unique, (4) the chosen topic and ranked items must differ from recent history, (5) facts must be broadly known and safe; do not invent precise dates, records, or statistics. Example valid beat object: {'text': 'At number 5, the Eclipse yacht. Over 160 meters long.', 'query': 'Eclipse yacht', 'label': '5'}. "
        "Do not include markdown, notes, or any extra keys. Output only the JSON object."
    )
    return prompt, slots


def _build_fallback_meta(variant: dict) -> tuple[dict, dict]:
    """(meta, slots) from one fallback variant; 7 word-even beats + queries."""
    narration = variant["narration"]
    words = narration.split()
    queries = variant["beat_queries"]
    per = max(1, -(-len(words) // 7))
    rank_labels = ["", "5", "4", "3", "2", "1", ""]
    beats = []
    for i in range(7):
        chunk = " ".join(words[i * per:(i + 1) * per])
        if chunk:
            beats.append({"text": chunk, "query": queries[i], "label": rank_labels[i]})
    slots = {"topic": variant["topic"]}
    return {
        "title": variant["title"],
        "narration": narration,
        "description": variant["description"],
        "tags": list(variant["tags"]),
        "items": list(variant["items"]),
        "beats": beats,
    }, slots


_FALLBACK_VARIANTS = (
    {
        "topic": "largest animals on Earth",
        "items": ["Saltwater Crocodile", "Hippopotamus", "Giraffe",
                  "African Bush Elephant", "Blue Whale"],
        "title": "\U0001F981 Top 5 Largest Animals on Earth #shorts",
        "description": "\U0001F42C The 5 largest animals on Earth, ranked \U0001F30A. #shorts #animals #top5",
        "tags": ["shorts", "top5", "animals", "ranking", "countdown"],
        "narration": (
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
        ),
        "beat_queries": ["crocodile eyes water surface", "saltwater crocodile water",
                         "hippopotamus river africa", "giraffe savanna walking",
                         "african elephant walking dust", "blue whale underwater",
                         "ocean waves aerial"],
    },
    {
        "topic": "tallest waterfalls in the world",
        "items": ["Niagara Falls", "Iguazu Falls", "Victoria Falls",
                  "Yosemite Falls", "Angel Falls"],
        "title": "\U0001F30A Top 5 Tallest Waterfalls in the World #shorts",
        "description": "\U0001F30A The 5 tallest waterfalls in the world, ranked \U0001F30A. #shorts #waterfalls #top5",
        "tags": ["shorts", "top5", "waterfalls", "ranking", "countdown"],
        "narration": (
            "These are the 5 tallest waterfalls in the world. And number 1 drops so far, the water turns to mist before it lands. "
            "Stay until the end, because the top spot falls nearly a full kilometer straight down. "
            "At number 5, Niagara Falls. About 50 meters of pure thunder on the border of two countries. "
            "Millions of visitors stand in its spray every year. It never stops, day or night. "
            "Number 4, Iguazu Falls. Over 80 meters tall and wider than a city block. "
            "Hundreds of separate cascades crash through the jungle. The locals call it the Devil's Throat. "
            "Number 3, Victoria Falls. Over 100 meters of falling Zambezi River. "
            "The spray rises so high it makes its own rain. You can hear it from kilometers away. "
            "Number 2, Yosemite Falls. Nearly 740 meters down a granite cliff in California. "
            "In spring the snowmelt turns it into a roaring giant. By late summer it slows to a silver thread. "
            "And number 1, Angel Falls. Nearly 1000 meters straight off a tabletop mountain in Venezuela. "
            "The water falls so far it drifts away as clouds. Nothing on Earth drops further. "
            "Comment which waterfall you would visit first, and follow for the next top 5."
        ),
        "beat_queries": ["waterfall aerial jungle", "niagara falls aerial",
                         "iguazu falls devil throat", "victoria falls zimbabwe",
                         "yosemite falls california", "angel falls venezuela",
                         "waterfall rainbow mist"],
    },
    {
        "topic": "tallest buildings in the world",
        "items": ["Eiffel Tower", "Empire State", "Taipei 101",
                  "Shanghai Tower", "Burj Khalifa"],
        "title": "\U0001F3D9\uFE0F Top 5 Tallest Buildings in the World #shorts",
        "description": "\U0001F3D9\uFE0F The 5 tallest buildings in the world, ranked \U0001F30A. #shorts #buildings #top5",
        "tags": ["shorts", "top5", "buildings", "ranking", "countdown"],
        "narration": (
            "These are the 5 tallest buildings in the world. And number 1 is so tall, planes have to fly around it. "
            "Stay until the end, because the top spot reaches nearly a full kilometer into the sky. "
            "At number 5, the Eiffel Tower. About 330 meters tall, standing over Paris for more than a century. "
            "It was once the tallest structure on Earth. And it still stops traffic every single day. "
            "Number 4, the Empire State. Over 440 meters of New York legend. "
            "It held the world record for decades. Its lights change color for every holiday. "
            "Number 3, Taipei 101. Over 500 meters tall, built to survive earthquakes and typhoons. "
            "It looks like a stack of bamboo boxes. Engineers call it one of the safest towers ever built. "
            "Number 2, the Shanghai Tower. Over 630 meters of twisting glass. "
            "It turns a full rotation as it climbs. From the top, the whole city looks like a model. "
            "And number 1, the Burj Khalifa. Nearly 830 meters tall. Over 160 floors. "
            "Its elevators are among the fastest on the planet. Nothing built by humans stands taller. "
            "Comment which tower you would climb first, and follow for the next top 5."
        ),
        "beat_queries": ["city skyline night aerial", "eiffel tower paris day",
                         "empire state building night", "taipei 101 taiwan",
                         "shanghai tower skyscraper", "burj khalifa dubai",
                         "dubai skyline sunset"],
    },
)


def fallback_script(history: list[dict] | None = None) -> tuple[dict, dict]:
    """Offline countdown; least-recent variant wins, never repeats last video."""
    history = history or []

    def _last_pos(topic_cf: str) -> int:
        pos = -1
        for i, h in enumerate(history):
            if (h.get("niche", "horror") == MODE
                    and str(h.get("topic", "")).casefold() == topic_cf):
                pos = i
        return pos

    ranked = sorted(_FALLBACK_VARIANTS, key=lambda v: _last_pos(v["topic"].casefold()))
    last = history[-1] if history else {}
    last_key = (str(last.get("topic", "")).casefold(),
                tuple(sorted(str(i).strip().casefold() for i in (last.get("items") or []))))
    chosen = ranked[0]
    for variant in ranked:
        vkey = (variant["topic"].casefold(),
                tuple(sorted(i.casefold() for i in variant["items"])))
        if vkey != last_key:
            chosen = variant
            break
    if chosen is ranked[0]:
        log(f"Offline fallback: '{chosen['topic']}' (least-recent variant).")
    else:
        log(f"Offline fallback: least-recent variant repeats last video, using '{chosen['topic']}' instead.")
    return _build_fallback_meta(chosen)


def validate_extra(data: dict) -> dict:
    """Ranked items: validated + bare-homonym beat-query backstop. Raises ValueError."""
    items = data.get("items", None)
    if items is None:
        return {}
    if (not isinstance(items, list) or not items
            or not all(isinstance(t, str) and t.strip() for t in items)):
        raise ValueError("'items' must be a non-empty list of strings")
    clean_items = [t.strip() for t in items if t.strip()]
    beats = data.get("beats", [])
    labeled = [b for b in (beats or []) if isinstance(b, dict)
               and str(b.get("label", "")).isdigit()]
    for b, name in zip(labeled, clean_items + [""] * len(labeled)):
        q = str(b.get("query", ""))
        if is_bare_homonym_query(q):
            raise ValueError(
                f"beat query '{q.strip()}' for '{name or '?'}' is a bare "
                "homonym head lacking the subject noun (e.g. 'Eclipse yacht')")
    return {"items": clean_items[:10]}


_BANNED_TITLE_RES = (
    r"\s*[–—\-:|]\s*number\s+1\s+is\s+terrifying.*$",
    r"\s*[–—\-:|]\s*number\s+one\s+is\s+terrifying.*$",
    r"\s*[–—\-:|]\s*#1\s+is\s+terrifying.*$",
    r"\s*[–—\-:|]\s*will\s+shock\s+you.*$",
    r"\s*[–—\-:|]\s*you\s+won['\u2019]?t\s+believe.*$",
)

_TITLE_FORMAT_RE = re.compile(r"^[\W_]*((top\s+)?5)\b", re.IGNORECASE)


def sanitize_title(title: str, slots: dict | None = None) -> str:
    """Enforce channel title format. Never raises (returns a safe title)."""
    t = (title or "").strip()
    for pat in _BANNED_TITLE_RES:
        t = re.sub(pat, "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s*[–—\-:|;,.]+\s*$", "", t).strip()
    low = t.casefold()
    has_banned = any(hook in low for hook in BANNED_TITLE_HOOKS)
    if has_banned or not _TITLE_FORMAT_RE.match(t) or len(t) < 12:
        topic = ((slots or {}).get("topic") or "").strip()
        t = f"5 {topic[0].upper() + topic[1:]}" if topic else "Top 5 Countdown"
    return t[:100].strip() or "Top 5 Countdown"


def similarity_reason(data: dict, slots: dict, recent: list[dict],
                      item_recent: list[dict] | None = None) -> str | None:
    """Repeat reason (same topic / shared items / banned hook), else None."""
    title = str(data.get("title", "")).casefold()
    for hook in BANNED_TITLE_HOOKS:
        if hook in title:
            return f"title uses banned overused hook '{hook}'"
    if _canon_topic(slots.get("topic", "")) in {
            _canon_topic(h.get("topic", "")) for h in recent}:
        return f"topic '{slots.get('topic')}' already used"
    clause = _title_clause(data.get("title", ""))
    if clause:
        for h in [x for x in (recent or []) if x.get("niche", "horror") == MODE][-10:]:
            if _title_clause(h.get("title", "")) == clause:
                return (f"closing title clause '{clause}' already aired in "
                        f"'{h.get('title', '?')}'")
    scope = item_recent if item_recent is not None else recent
    new_items = {str(i).strip().casefold()
                 for i in (data.get("items") or []) if str(i).strip()}
    for h in scope:
        old_items = {str(i).strip().casefold()
                     for i in (h.get("items") or []) if str(i).strip()}
        if new_items and old_items and len(new_items & old_items) >= 2:
            return (f"shares ranked items with '{h.get('title', '?')}' "
                    f"({sorted(new_items & old_items)[:3]})")
    return None


def history_fields(slots: dict, meta: dict) -> dict:
    """History entry fields for this run."""
    return {"topic": slots.get("topic", ""),
            "topicSource": slots.get("topic_source", "pool"),
            "hookStyle": slots.get("hook_style", ""),
            "vertical": _topic_vertical(slots.get("topic", "")),
            "items": list(meta.get("items", [])[:10])}


def _story_signature(text: str) -> set:
    """Significant lowercase words, for overlap comparison."""
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "at",
            "it", "its", "is", "was", "are", "be", "i", "you", "he", "she",
            "they", "we", "my", "his", "her", "with", "for", "as", "that",
            "this", "but", "not", "so", "if", "then", "there", "here"}
    return {w.strip(".,!?;:\"'").lower() for w in text.split()
            if len(w) > 3} - stop


def similar_to_history(data: dict, slots: dict, history: list[dict],
                       cfg: RankingConfig) -> str | None:
    """Repeat reason vs full/item windows + story overlap, else None."""
    window = [h for h in (history or [])[-cfg.dedup_window:]
              if h.get("niche", "horror") == MODE]
    if not window:
        return None
    recent_titles = {str(h.get("title", "")).strip().casefold() for h in window}
    if str(data.get("title", "")).strip().casefold() in recent_titles:
        return "title already used"
    item_window = [h for h in (history or [])[-cfg.dedup_item_window:]
                   if h.get("niche", "horror") == MODE]
    norm_slots = dict(slots or {}, topic=_canon_topic((slots or {}).get("topic", "")))
    norm_window = [dict(h, topic=_canon_topic(h.get("topic", ""))) for h in window]
    reason = similarity_reason(data, norm_slots, norm_window, item_window)
    if reason is not None:
        return reason
    new_sig = _story_signature(f"{data.get('title', '')} {data.get('narration', '')}")
    for h in item_window:
        old_sig = _story_signature(f"{h.get('title', '')} {h.get('narration', '')}")
        if not old_sig or not new_sig:
            continue
        overlap = len(new_sig & old_sig) / len(new_sig | old_sig)
        if overlap > 0.35:
            return (f"story overlaps '{h.get('title', '?')}' (word overlap {overlap:.0%})")
    return None


_HOOK_STAKES_RE = re.compile(
    r"\d|over|about|nearly|tonnes|meters|taller|heavier|weighs|stands|"
    r"bigger|deadlier|faster", re.IGNORECASE)
_HOOK_NAMES_ONE_RE = re.compile(r"number\s*1|#1|number one", re.IGNORECASE)


def hook_violation(data: dict) -> str | None:
    """Reason the hook beat violates the stakes rule, else None."""
    try:
        beats = data.get("beats") or []
        hook = str(beats[0].get("text", "")) if beats else ""
    except (AttributeError, IndexError, TypeError):
        return "hook beat unreadable"
    if _HOOK_NAMES_ONE_RE.search(hook):
        return "hook names number 1 instead of teasing it"
    if not _HOOK_STAKES_RE.search(hook):
        return "hook has no stakes signal (digit or scale/danger word)"
    return None


def validate_script(data: dict) -> dict:
    """Strict-check script JSON (ranking: 180+ words). Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("Gemini output is not a JSON object")
    title = data.get("title")
    narration = data.get("narration")
    description = data.get("description")
    tags = data.get("tags")
    beats = data.get("beats")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("missing/invalid 'title' (must be non-empty string)")
    if not isinstance(narration, str) or len(narration.split()) < 20:
        raise ValueError("'narration' missing or too short (<20 words)")
    nwords = len(narration.split())
    if nwords < 180:
        raise ValueError(f"'narration' too short ({nwords} words, need 180+)")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("missing/invalid 'description' (must be non-empty string)")
    if (not isinstance(tags, list) or not tags
            or not all(isinstance(t, str) and t.strip() for t in tags)):
        raise ValueError("'tags' must be a non-empty list of strings")
    if not isinstance(beats, list) or not beats:
        raise ValueError("'beats' must be a non-empty list")
    norm_beats = []
    for b in beats[:8]:
        if not isinstance(b, dict):
            raise ValueError("each beat must be an object with text+query")
        t, q = b.get("text"), b.get("query")
        if not isinstance(t, str) or not t.strip():
            raise ValueError("beat 'text' must be a non-empty string")
        if not isinstance(q, str) or not q.strip():
            raise ValueError("beat 'query' must be a non-empty string")
        lab = b.get("label", "")
        if not isinstance(lab, str):
            lab = ""
        lab = "".join(c for c in lab if c.isdigit())[:2]
        norm_beats.append({"text": t.strip(), "query": q.strip(), "label": lab})
    for b in norm_beats:
        if b["label"].isdigit() and is_bare_homonym_query(b["query"]):
            raise ValueError(
                f"beat query '{b['query']}' is a bare homonym head lacking the "
                "subject noun (e.g. 'Eclipse yacht', 'monitor lizard')")
    bwords = sum(len(b["text"].split()) for b in norm_beats)
    if bwords < 180:
        raise ValueError(f"'beats' cover too little text ({bwords} words, need 180+)")
    out = {
        "title": title.strip()[:100],
        "narration": narration.strip(),
        "description": description.strip(),
        "tags": [t.strip() for t in tags if t.strip()][:10],
        "beats": norm_beats,
    }
    out.update(validate_extra(data))
    return out


def _to_content(meta: dict, slots: dict) -> Content:
    """Validated meta + slots → stage Content (beats/items live in extras)."""
    return Content(
        title=meta["title"],
        narration_text=meta["narration"],
        description=meta.get("description", ""),
        tags=list(meta.get("tags", [])),
        extras={"beats": list(meta.get("beats", [])),
                "items": list(meta.get("items", [])),
                "topic": slots.get("topic", ""),
                "topic_source": slots.get("topic_source", "pool"),
                "hook_style": slots.get("hook_style", ""),
                "slots": dict(slots)})


def generate_content(history: list[dict], cfg: RankingConfig) -> Content:
    """Gemini script with hook/similarity regens; offline fallback when keyless."""
    history = history or []
    client = _make_client(cfg)
    hook_regen_used = False
    if client is not None:
        models = list(cfg.text_models)
        for _attempt in range(1 + cfg.similarity_retries):
            prompt, slots = build_prompt(history, cfg, client)
            data = None
            for model in models:
                try:
                    data = validate_script(client.generate_json(model, prompt, generation_config={
                        "temperature": 0.7, "maxOutputTokens": 4096}))
                    if model in models and models[0] != model:
                        models.remove(model)
                        models.insert(0, model)
                    break
                except Exception as e:
                    log(f"Gemini model {model} unusable ({e}), trying next...")
                    time.sleep(2)
            if data is None:
                log("All Gemini models failed, using fallback.")
                break
            hook_problem = hook_violation(data)
            if hook_problem is not None and not hook_regen_used:
                hook_regen_used = True
                warn(f"Hook weak ({hook_problem}), regenerating once for a fresh draft...")
                continue
            if hook_problem is not None:
                log(f"Hook still weak ({hook_problem}), accepting best-effort.")
            reason = similar_to_history(data, slots, history, cfg)
            if reason is None:
                data["title"] = sanitize_title(data.get("title", ""), slots)
                log(f"Gemini script OK: {data.get('title')}")
                return _to_content(data, slots)
            if _attempt < cfg.similarity_retries:
                warn(f"Gemini draft too similar to history ({reason}), regenerating...")
                continue
            warn(f"Gemini draft still similar ({reason}), accepting to avoid failing the run.")
            data["title"] = sanitize_title(data.get("title", ""), slots)
            return _to_content(data, slots)
    warn("Gemini unavailable: using standalone fallback script.")
    meta, slots = fallback_script(history)
    reason = similar_to_history(meta, slots, history, cfg)
    if reason is not None:
        warn(f"Offline fallback repeats history ({reason}), airing anyway.")
    return _to_content(meta, slots)


def sample_content() -> Content:
    """Offline sample (first fallback variant) for demos/tests."""
    meta, slots = _build_fallback_meta(_FALLBACK_VARIANTS[0])
    out = _to_content(meta, slots)
    out.extras["offline"] = True
    return out
