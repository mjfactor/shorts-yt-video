"""Ranking niche: Top-5 countdown Shorts (~2 min).

Self-contained: prompt, topic pool, offline fallback, history shape,
similarity rules, stock policy, still style, and leaderboard overlay.
Delete this file + its NICHES line in pipeline.py to drop the niche.
"""
import json
import os
import random
import re
import time

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
    "fastest sea animals in the ocean",
    "largest fish in the ocean",
    "longest-lived animals on Earth",
    "smartest dog breeds in the world",
    "most popular pets in the world",
    "largest spiders in the world",
    # Geography and nature.
    "tallest waterfalls in the world",
    "longest mountain ranges on Earth",
    "largest glaciers in the world",
    "most active volcanoes in the world",
    "deepest canyons on Earth",
    "largest coral reefs in the world",
    "biggest waves for surfing in the world",
    "tallest trees on Earth",
    "most dangerous roads in the world",
    "largest caves in the world",
    "most visited national parks in the world",
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
    "oldest buildings on Earth",
    "biggest shipwrecks in history",
    "most famous towers in the world",
    "largest palaces in the world",
    "most famous lighthouses in the world",
    "longest walls in the world",
    # Vehicles and machines.
    "largest cargo airplanes ever built",
    "fastest fighter jets ever built",
    "largest helicopters ever built",
    "fastest motorcycles in the world",
    "fastest trains in the world",
    "largest yachts in the world",
    "largest submarines ever built",
    "most popular pickup trucks in the world",
    "largest container ships in the world",
    "biggest rockets ever built",
    "most famous tanks in history",
    "most famous cable cars in the world",
    "most famous race tracks in the world",
    "largest sailing ships in the world",
    # Space, sports, food.
    "largest planets in the solar system",
    "biggest moons in the solar system",
    "hottest chili peppers in the world",
    "most expensive fruits in the world",
    "most famous space missions in history",
    "largest telescopes in the world",
    # More variety: recurring light celebrations (filmable, no dates).
    "most famous festivals of lights in the world",
    # Money traps: price-ranked, stock-filmable subjects, rounded costs.
    "hotel room traps that overcharge you",
    "supercars that bankrupt owners",
    "watches that beat stocks",
    "stadium foods that cost more than tickets",
    # Danger jobs/machines: danger- or price-ranked workhorses.
    "farm machines worth more than houses",
    "fire trucks the world envies",
    "tools that send DIYers to ER",
    "fastest police interceptors by country",
    # Insider secrets: danger-ranked places and jobs with hidden rules.
    "airport secrets gate agents never say",
    "foods chefs never order",
    "cruise-ship jobs with secret perks",
    "subway systems thieves love most",
    # Infra extremes: size- or danger-ranked structures and routes.
    "bridges truckers fear most",
    "winter roads only pros drive",
    "cargo ships bigger than skyscrapers",
    "smallest apartments at luxury prices",
    "lighthouses that still save ships",
    # Wave 2 money traps: fee/scam-ranked, rounded costs.
    "rental traps that steal deposits",
    "phone scams that steal millions",
    "parking-garage traps that steal cash",
    "tow fees that trap drivers",
    "toll roads that cost a fortune",
    "rest-stop scams that steal cash",
    "night-shift jobs that beat day pay",
    # Wave 2 danger-work: ER- or damage-ranked jobs and machines.
    "kitchen dangers that send cooks to ER",
    "truck-stop nightmares that trap drivers",
    "marathon rescues that save runners",
    "delivery-van breakdowns that cost fleets",
    "highway pileups that trap drivers in fog",
    "gym machines that send beginners to ER",
    "e-bike battery fire risks",
    "ski lifts that trap riders mid-air",
    # Wave 2 insider secrets: theft- and hidden-camera-ranked spots.
    "locker-room thefts that steal gear",
    "roadside motels with secret cameras",
    # Wave 2 infra extremes: storm- and fear-ranked crossings.
    "dam releases kayakers fear",
    "ferry crossings that risk lives in storms",
    "lighthouse storms sailors fear most",
)

# Topic synonyms normalized to canonical form before same-topic compares,
# so "biggest sharks" vs "largest sharks" counts as the same topic.
_TOPIC_CANON = (("biggest", "largest"), ("quickest", "fastest"),
                ("highest", "tallest"), ("deepest", "longest"))


def _canon_topic(topic: str) -> str:
    """Casefolded topic with synonym superlatives in canonical form."""
    t = str(topic or "").casefold()
    for variant, canon in _TOPIC_CANON:
        t = re.sub(rf"\b{variant}\b", canon, t)
    return re.sub(r"\s+", " ", t).strip()


# Leading superlatives: proposing the same one twice in a row is rejected.
_LEADING_SUPERLATIVES = ("largest", "biggest", "tallest", "longest",
                         "fastest", "most")


def _leading_superlative(topic: str) -> str:
    """First-word superlative of a topic ("" when it leads with none)."""
    words = re.findall(r"[a-z]+", str(topic or "").lower())
    if words and words[0] in _LEADING_SUPERLATIVES:
        return words[0]
    return ""


# Cross-vertical rotation: every topic maps to one vertical; AI proposals
# must avoid the last 2 aired verticals so the channel stops stacking the
# same lane back to back. New-vertical keywords win over legacy ones
# ("fire trucks" is danger-work, not vehicles; "cargo ships" is infra).
# Order matters: danger-work/insider/infra win over money ("farm machines
# worth more than houses" is danger-work, "smallest apartments at luxury
# prices" is infra), and all four new lanes win over legacy buckets.
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


# Rank-metric lexicon: an AI proposal must rank by price, speed, danger,
# or size. Insider/infra framings count via their threat/extreme cues
# (secret/never/fear/save/pro/drive), so the mandated new verticals pass.
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
    """Closing clause of a title: text after the last dash/colon split
    ("5 Largest Animals — Number 1 Is Huge" -> "number 1 is huge").
    Plain titles with no dash/colon suffix have no clause (""). Compared
    casefolded against recent titles."""
    t = str(title or "").strip()
    t = re.sub(r"^[\W_]*((top\s+)?5)\b", "", t,
               flags=re.IGNORECASE).strip()
    parts = re.split(r"[–—\-:|;,.]+", t)
    if len(parts) < 2:
        return ""
    return re.sub(r"\s+", " ", parts[-1].strip().casefold())


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
    """Casefolded topics from the full enforced history window (same niche).

    Programmatic freshness gates scan DEDUP_WINDOW (110); only the quoted
    prompt block stays at HISTORY_PROMPT_N (advisory bans, token sanity).
    """
    return {str(h.get("topic", "")).casefold()
            for h in (history or [])[-C.DEDUP_WINDOW:]
            if h.get("niche", "horror") == MODE}


def propose_topic(history: list[dict]) -> tuple[str | None, list[str] | None]:
    """Ask Gemini for one fresh countdown topic. Returns (topic, items_hint).

    (None, None) on any failure or rejection — the caller falls back to the
    pool. Uses the shared key rotation, so quota pain is minimal (1-2 calls).
    """
    if not AI_TOPICS or not C.GEMINI_KEYS:
        return None, None
    recent = _recent_topic_names(history)
    # Cross-vertical rotation: the last 2 aired verticals are off-limits,
    # matched via history topic text (older entries may lack a recorded
    # vertical). Same leading superlative twice in a row is also banned.
    same_niche = [h for h in (history or [])
                  if h.get("niche", "horror") == MODE]
    excluded_verticals = {_topic_vertical(h.get("topic", ""))
                          for h in same_niche[-2:]}
    excluded_verticals.discard("")
    last_superlative = (_leading_superlative(same_niche[-1].get("topic", ""))
                        if same_niche else "")
    avoid = "".join(f"- {h.get('topic', '?')}\n"
                    for h in (history or [])[-C.HISTORY_PROMPT_N:]
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
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.8,
            "maxOutputTokens": 1024,
        },
        "safetySettings": C._GEMINI_SAFETY_SETTINGS,
    }
    for round_no in range(1, AI_TOPIC_TRIES + 1):
        data = None
        for model in C.GEMINI_MODELS:
            try:
                url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                       f"{model}:generateContent")
                raw = C._fetch_gemini(url, payload, tries=1, backoff=0)
                text = raw["candidates"][0]["content"]["parts"][0]["text"]
                text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                data = json.loads(text)
                break
            except Exception as e:
                C.log(f"AI topic model {model} unusable ({e}), trying next...")
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
            problems.append(
                f"vertical '{_topic_vertical(topic)}' aired in last 2 runs")
        if last_superlative and _leading_superlative(topic) == last_superlative:
            problems.append(
                f"leading superlative '{last_superlative}' aired last run")
        if _canon_topic(topic) in {_canon_topic(t) for t in recent}:
            problems.append("topic already used")
        if len(items) < 5:
            problems.append("fewer than 5 subjects")
        if any(len(i.split()) > 4 or len(i) > 40 for i in items):
            problems.append("unfilmable subject names")
        if not problems:
            # Subject pre-check: >=2 hinted items (casefolded) intersecting
            # ANY single recent entry's items rejects the round, within the
            # existing AI_TOPIC_TRIES. Enforced vs the item window (last 30),
            # not the full topic window — common animals false-positive.
            hint_set = {i.casefold() for i in items}
            for h in (history or [])[-C.DEDUP_ITEM_WINDOW:]:
                if h.get("niche", "horror") != MODE:
                    continue
                old_items = {str(i).strip().casefold()
                             for i in (h.get("items") or []) if str(i).strip()}
                if hint_set and old_items and len(hint_set & old_items) >= 2:
                    problems.append(
                        f"subjects already ranked in '{h.get('title', '?')}'")
                    break
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


# Rotating hook templates: one is picked per run (random.choice) and spliced
# into the Narration clause, so openers stop sounding samey. Every template
# teases #1 WITHOUT naming it, avoids all BANNED_TITLE_HOOKS phrasing,
# runs ~15-30 words, and stays spoken-aloud fifth-grade level. Examples are
# style examples only — each carries a do-not-copy guard because Gemini
# otherwise repeats example sentences verbatim.
HOOK_STYLES = (
    ("stakes",
     "Open with STAKES + SCALE: state one rounded size, weight, or height "
     "claim about number 1 as a comparison to something familiar "
     "(weighing over X, heavier or taller than Y combined), without naming "
     "it. Style example only, do not repeat this example sentence: "
     "'Number 1 weighs over 100 tonnes, heavier than 20 elephants combined. "
     "And it is still growing.'"),
    ("contrarian",
     "Open by busting a common MYTH: say what everyone thinks holds the "
     "record, then reveal it is not even close, without naming number 1. "
     "Style example only, do not repeat this example sentence: 'Everyone "
     "thinks the lion is the strongest hunter alive. It is not even close "
     "to number 1.'"),
    ("sensory",
     "Open with a SENSORY close-up: one vivid sound, sight, or size image "
     "tied to number 1, something a viewer can picture or hear, without "
     "naming it. Style example only, do not repeat this example sentence: "
     "'Imagine teeth the size of bananas, and a roar heard kilometers away. "
     "That is number 1 waiting.'"),
    ("record",
     "Open by framing number 1 as an unbeaten RECORD: nothing alive or "
     "built today comes close, without naming it. Style example only, do "
     "not repeat this example sentence: 'Nothing alive today comes close "
     "to number 1. It stands nearly 30 meters tall, taller than 5 houses.'"),
)

# Stale openers that already aired to death (see history.json). Prompt-level
# ban only — never open with these; no raising validation, so a draft that
# slips through never breaks the run.
WEAK_OPENERS = (
    "these are the 5",
    "human beings love",
    "deep beneath our feet",
    "nature hides",
    "push human engineering to",
)


def build_prompt(history: list[dict]):
    """Gemini prompt for a Top-5 countdown (~2 min). Returns (prompt, slots)."""
    history = history or []
    recent_topics = _recent_topic_names(history)
    topic, hint = propose_topic(history)
    source = "ai" if topic else "pool"
    if topic is None:
        topic = C._pick_avoiding(TOPICS, recent_topics, history)
    slots = {"topic": topic, "topic_source": source}
    hook_style, hook_direction = random.choice(HOOK_STYLES)
    slots["hook_style"] = hook_style
    C.log(f"Ranking topic ({source}): {topic}")
    prompt = (
        f"Write a Top-5 countdown YouTube Shorts script about the {topic}. "
        "This must be a single complete video with no part 1/2, no episode markers, and no recap setup. "
        f"{C.history_block(history, MODE, format_history_line, AVOID_INTRO, AVOID_OUTRO)}"
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
    """Assemble a (meta, slots) pair from one fallback variant dict.

    Same shape as a validated script: 7 beats (hook, #5..#1, closer) split
    word-even across the narration, each with a stock-filmable query.
    """
    narration = variant["narration"]
    words = narration.split()
    queries = variant["beat_queries"]
    # 7 beats: hook, #5..#1, closer — split word-even across them.
    per = max(1, -(-len(words) // 7))
    rank_labels = ["", "5", "4", "3", "2", "1", ""]
    beats = []
    for i in range(7):
        chunk = " ".join(words[i * per:(i + 1) * per])
        if chunk:
            beats.append({"text": chunk, "query": queries[i],
                          "label": rank_labels[i]})
    slots = {"topic": variant["topic"]}
    return {
        "title": variant["title"],
        "narration": narration,
        "description": variant["description"],
        "tags": list(variant["tags"]),
        "items": list(variant["items"]),
        "beats": beats,
    }, slots


# Offline fallback rotation: three self-consistent hard-coded videos (each
# passes _validate_script). Topics are pool topics so history comparisons
# work; items are distinct, famous, correctly ordered, stock-filmable.
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
    """Offline Top-5 countdown (~2 min). Least-recently-aired variant wins.

    Never airs the identical topic+items as the immediately previous video:
    if the least-recent variant matches the last entry exactly, the next
    variant is taken instead. Variety without Gemini, no repeats back to back.
    """
    history = history or []

    def _last_pos(topic_cf: str) -> int:
        pos = -1
        for i, h in enumerate(history):
            if (h.get("niche", "horror") == MODE
                    and str(h.get("topic", "")).casefold() == topic_cf):
                pos = i  # newest last: larger = more recent; -1 = never aired
        return pos

    ranked = sorted(_FALLBACK_VARIANTS,
                    key=lambda v: _last_pos(v["topic"].casefold()))
    last = history[-1] if history else {}
    last_key = (str(last.get("topic", "")).casefold(),
                tuple(sorted(str(i).strip().casefold()
                             for i in (last.get("items") or []))))
    chosen = ranked[0]
    for variant in ranked:
        vkey = (variant["topic"].casefold(),
                tuple(sorted(i.casefold() for i in variant["items"])))
        if vkey != last_key:
            chosen = variant
            break
    if chosen is ranked[0]:
        C.log(f"Offline fallback: '{chosen['topic']}' (least-recent variant).")
    else:
        C.log(f"Offline fallback: least-recent variant repeats last video, "
              f"using '{chosen['topic']}' instead.")
    return _build_fallback_meta(chosen)


def validate_extra(data: dict) -> dict:
    """Ranked-item list: validated, kept for history/board. Raises ValueError."""
    items = data.get("items", None)
    if items is None:
        return {}
    if (not isinstance(items, list) or not items
            or not all(isinstance(t, str) and t.strip() for t in items)):
        raise ValueError("'items' must be a non-empty list of strings")
    clean_items = [t.strip() for t in items if t.strip()]
    # Ranking backstop for bare homonym beat queries ('Eclipse' for item
    # 'Eclipse'): core rejects these too, this cites the ranked item so the
    # regen path knows which slot to fix. Multi-word and ordinary nouns pass.
    beats = data.get("beats", [])
    labeled = [b for b in (beats or []) if isinstance(b, dict)
               and str(b.get("label", "")).isdigit()]
    for b, name in zip(labeled, clean_items + [""] * len(labeled)):
        q = str(b.get("query", ""))
        if C._is_bare_homonym_query(q):
            raise ValueError(
                f"beat query '{q.strip()}' for '{name or '?'}' is a bare "
                "homonym head lacking the subject noun (e.g. 'Eclipse yacht')")
    return {"items": clean_items[:10]}


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


def similarity_reason(data: dict, slots: dict, recent: list[dict],
                      item_recent: list[dict] | None = None):
    """Repeat reason for same-topic, shared ranked items, or banned title
    hook, else None. `recent` is the full-window list for the topic gate;
    `item_recent` (defaults to `recent`) is the last-30 list for the
    shared-items gate, so common animals don't false-positive across 110."""
    title = str(data.get("title", "")).casefold()
    for hook in BANNED_TITLE_HOOKS:
        if hook in title:
            return f"title uses banned overused hook '{hook}'"
    # Same-topic compare on canonical form: synonym superlatives
    # (largest/biggest, fastest/quickest, tallest/highest, longest/deepest)
    # count as the same topic.
    if _canon_topic(slots.get("topic", "")) in {
            _canon_topic(h.get("topic", "")) for h in recent}:
        return f"topic '{slots.get('topic')}' already used"
    # Same closing title clause twice in the last 10 airs is a repeat.
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
    return {"topic": slots.get("topic", ""),
            "topicSource": slots.get("topic_source", "pool"),
            "hookStyle": slots.get("hook_style", ""),
            "vertical": _topic_vertical(slots.get("topic", "")),
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
