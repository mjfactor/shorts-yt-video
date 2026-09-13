"""Horror niche: standalone scary-story Shorts (~2 min).

Self-contained: prompt, variety pools, offline fallback, history shape,
similarity rules, stock policy, and still style. To drop the niche, delete
this file + its NICHES line in pipeline.py (core.py never imports it).
"""
import random

import core as C

MODE = "horror"
NICHE_LABEL = "scary true horror stories with a twist ending"

# Variety pools: one slot is drawn per run and forced into the prompt, so
# consecutive runs can't all default to "Mark in a morgue". Fresh slots also
# avoid recent history.json entries (see _pick_avoiding), and the prompt
# quotes recent titles so Gemini can't repeat a story skeleton.
PROTAGONIST_NAMES = (
    "Mara", "Elena", "Jonas", "Priya", "Tomas", "Nadia", "Victor", "Lena",
    "Marcus", "Aisha", "Dario", "Sofia", "Kenji", "Rosa", "Felix", "Amara",
    "Silas", "Noor", "Petra", "Owen", "Yuki", "Henrik", "Lucia", "Omar",
    "Ingrid", "Caleb", "Mei", "Rafael", "Tara", "Dmitri",
)
PROTAGONIST_JOBS = (
    "night-shift nurse", "overnight security guard", "late-night radio host",
    "24-hour diner cook", "cemetery caretaker", "last-route bus driver",
    "roadside motel clerk", "lighthouse keeper",
    "graveyard-shift warehouse picker", "petrol station night attendant",
    "empty-office cleaner", "night baker", "night-shift taxi driver",
    "highway toll booth operator", "overnight ferry deckhand",
    "24-hour gym receptionist", "sleeping-train conductor",
    "cruise ship night steward",
)
STORY_LOCATIONS = (
    "abandoned elementary school", "roadside motel", "overnight sleeper train",
    "closed shopping mall", "forest ranger cabin", "subway maintenance tunnel",
    "rural clinic", "lighthouse", "apartment stairwell", "lakeside campsite",
    "24-hour laundromat", "empty church", "truck stop diner",
    "night market alley", "defunct amusement park", "underground parking garage",
    "remote fire lookout tower", "closed-down cinema", "foggy cliffside road",
    "deserted oil rig", "mountain tunnel rest stop", "old sanatorium",
    "riverside boathouse", "snowed-in ski lodge",
)
BROKEN_RULES = (
    "don't open it after midnight", "don't answer it", "don't look back",
    "don't follow the sound", "don't let it inside", "don't say its name",
    "don't turn off the light", "don't go downstairs", "don't whistle back",
    "don't pick up the coin", "don't knock twice", "don't sleep before 3am",
)
# Observed Gemini ruts — explicitly banned in the prompt each run.
BANNED_DEFAULTS = ("Mark", "morgue", "hospital")

# Ambient horror B-roll pool: eye-pleasing moody stock, deliberately NOT
# story-matched. Beats cut every ~4-7s, so variety matters more than literal
# accuracy. The horror mood is still forced on each pick.
AMBIENT_QUERIES = (
    "foggy forest night",
    "abandoned hallway dark",
    "haunted house night fog",
    "dark misty graveyard",
    "thunderstorm clouds night",
    "abandoned corridor dark",
    "creepy old door dark",
    "full moon clouds night",
    "dark forest mist",
    "candle flicker dark",
    "rainy window night dark",
    "old lantern night dark",
    "misty lake night",
    "abandoned church dark",
    "spooky stairs dark",
    "dark castle night fog",
)

AVOID_INTRO = ("Already-used stories — do NOT reuse any of these titles, twists, "
               "protagonist names, jobs, settings, or rules, and do NOT write a "
               "story with the same scare sequence:\n")
AVOID_OUTRO = ("Your story must use a DIFFERENT protagonist name, job, setting, "
               "rule, and twist from every entry above. ")

# Stock policy: ambient B-roll (see AMBIENT_QUERIES), mood-forced.
USE_BEAT_QUERY = False
USE_MOOD = True


def format_history_line(h: dict) -> str:
    return (
        f"- \"{h.get('title', '?')}\" "
        f"(protagonist={h.get('name', '?')}, job={h.get('job', '?')}, "
        f"setting={h.get('location', '?')}, rule={h.get('rule', '?')})"
    )


def build_prompt(history: list[dict] | None = None):
    """Gemini prompt for a standalone ~2-min story. Returns (prompt, slots)."""
    history = history or []
    recent_names = {str(h.get("name", "")).casefold() for h in history[-C.HISTORY_PROMPT_N:]}
    recent_jobs = {str(h.get("job", "")).casefold() for h in history[-C.HISTORY_PROMPT_N:]}
    recent_locations = {str(h.get("location", "")).casefold() for h in history[-C.HISTORY_PROMPT_N:]}
    recent_rules = {str(h.get("rule", "")).casefold() for h in history[-C.HISTORY_PROMPT_N:]}
    name = C._pick_avoiding(PROTAGONIST_NAMES, recent_names)
    job = C._pick_avoiding(PROTAGONIST_JOBS, recent_jobs)
    age = random.randint(22, 45)
    location = C._pick_avoiding(STORY_LOCATIONS, recent_locations)
    rule = C._pick_avoiding(BROKEN_RULES, recent_rules)
    slots = {"name": name, "job": job, "age": age,
             "location": location, "rule": rule}
    banned = ", ".join(BANNED_DEFAULTS)
    C.log(f"Story slots: {name}, {age}, {job} @ {location} | rule: {rule}")
    prompt = (
        f"Write a standalone scary YouTube Shorts horror story about {NICHE_LABEL}. "
        "It must be complete in this one video: no part 1/2, no episode markers, no cliffhanger-for-next-time. "
        f"Never use these overused defaults anywhere (name, job, setting, or title words): {banned}. "
        f"{C.history_block(history, MODE, format_history_line, AVOID_INTRO, AVOID_OUTRO)}"
        "Return ONLY valid JSON with keys: title (<=60 chars, scary hook; "
        "vary the title shape every run — never reuse patterns like "
        "'The X Rule You Must Never Break' or 'Never ...'), "
        "narration (280-300 words so the video runs about 2 minutes / ~120 seconds, present tense, single POV, fifth-grade "
        "reading level and easy spoken English; short sentences, common words, one clear idea per sentence, "
        f"no fancy metaphors, no stage directions; the protagonist is {name}, {age}, {job}, and the story "
        f"happens ONLY in {location} — state both in the first 2 sentences, "
        "never a nameless 'she' in a generic house; deliver escalating concrete "
        "scares in order: 1) a wrong sound, 2) an object moves or changes, 3) it responds or touches, "
        "4) a false calm, 5) a final reversal; each scare must be filmable in one sentence with a concrete sensory detail "
        f"(what is seen/heard/felt, not abstract dread); the story's ONE broken rule is '{rule}' "
        "and it must drive the scares; "
        "no greetings, filler, backstory, repeated ideas, or moralizing; use commas and ellipses to force "
        "dramatic pauses before reveals; end with a complete chilling twist contained in this video), "
        "description (2 sentences + 3 hashtags), "
        "tags (array of 5 strings), "
        "beats (array of 7-8 objects in story order, each {text: that beat's exact narration chunk, "
        "query: concrete dark stock-video search for that beat like 'woman opening door dark hallway'}; "
        "beat texts joined must equal the full narration)."
    )
    return prompt, slots


def fallback_script(history: list[dict] | None = None) -> tuple[dict, dict]:
    """Offline standalone story (~2 min). Slots avoid recent history."""
    history = history or []
    recent_names = {str(h.get("name", "")).casefold() for h in history[-C.HISTORY_PROMPT_N:]}
    recent_locations = {str(h.get("location", "")).casefold() for h in history[-C.HISTORY_PROMPT_N:]}
    recent_rules = {str(h.get("rule", "")).casefold() for h in history[-C.HISTORY_PROMPT_N:]}
    name = C._pick_avoiding(PROTAGONIST_NAMES, recent_names)
    job = random.choice(PROTAGONIST_JOBS)
    age = random.randint(22, 45)
    location = C._pick_avoiding(STORY_LOCATIONS, recent_locations)
    rule = C._pick_avoiding(BROKEN_RULES, recent_rules)
    narration = (
        f"Do not watch this alone. {name}, {age}, works as a {job}, and tonight is the last shift in the {location}. "
        f"The only rule is simple. {rule[0].upper() + rule[1:]}. "
        f"At 1am {name} hears it. Three slow knocks from the far end of the {location}. "
        f"The sound is wrong. Too patient. Too even. Like something counting. "
        f"{name} tells herself it is the pipes. Just the old building settling in the cold. "
        f"Then the knocks answer the thought. Two knocks for yes. {name} never spoke out loud. "
        f"The air turns colder near the floor, and the breath fogs even indoors. "
        f"Hands shaking, {name} checks the small monitor on the desk. The corridor is empty. "
        f"But the far door stands open on the screen, black beyond it, deeper than dark should be. "
        f"In person the corridor shows the same door. Closed. Locked from the outside. "
        f"{name} looks back at the monitor. Now a pale face fills the frame, staring straight out. "
        f"It wears a smile that does not fit its mouth, stretched a little too wide. "
        f"The lights flicker once, twice, then hold, humming louder than before. "
        f"On the screen the face tilts, as if it heard {name} breathing. "
        f"The corridor lights die completely, and {name} hears slow footsteps coming closer, though nothing has moved. "
        f"A speaker crackles, and the voice sounds almost like {name}. It says, do not turn around. "
        f"{name} turns anyway. The corridor is empty. The knocking has stopped. "
        f"Because now it stands right behind the chair, breathing on the back of the neck. "
        f"And on the monitor, the chair is empty, because {name} is already gone."
    )
    words = narration.split()
    beat_queries = ["dark bedroom night", "abandoned hallway night",
                    "creepy door dark", "security monitor dark room",
                    "foggy forest night", "dark corridor footsteps",
                    "pale face dark screen"]
    beats = []
    per = max(1, -(-len(words) // 7))  # ~7 even chunks for a ~2-min story
    for i in range(7):
        chunk = " ".join(words[i * per:(i + 1) * per])
        if chunk:
            beats.append({"text": chunk, "query": beat_queries[i]})
    slots = {"name": name, "job": job, "age": age,
             "location": location, "rule": rule}
    return {
        "title": f"The {location} rule {name} broke #shorts",
        "narration": narration,
        "description": f"A standalone scary horror short with a twist ending. #shorts #horror #scarystory",
        "tags": ["shorts", "horror", "scarystory", "creepy", "truestory"],
        "beats": beats,
    }, slots


def validate_extra(data: dict) -> dict:
    """Horror adds no extra meta fields."""
    return {}


def similarity_reason(data: dict, slots: dict, recent: list[dict]):
    """Repeat reason for reused name or setting+rule combo, else None."""
    recent_names = {str(h.get("name", "")).casefold() for h in recent}
    if str(slots.get("name", "")).casefold() in recent_names:
        return f"protagonist name '{slots.get('name')}' already used"
    if slots.get("location") and slots.get("rule"):
        for h in recent:
            if (str(h.get("location", "")).casefold()
                    == str(slots.get("location", "")).casefold()
                    and str(h.get("rule", "")).casefold()
                    == str(slots.get("rule", "")).casefold()):
                return (f"setting+rule combo already used "
                        f"('{slots.get('location')}' / '{slots.get('rule')}')")
    return None


def history_fields(slots: dict, meta: dict) -> dict:
    return {"name": slots.get("name", ""),
            "job": slots.get("job", ""),
            "age": slots.get("age", ""),
            "location": slots.get("location", ""),
            "rule": slots.get("rule", "")}


def beat_anchor(beats: list[dict]) -> str:
    """Opening-beat anchor so CF stills share one protagonist/setting."""
    return beats[0].get("query", "") if beats else ""


def image_prompt(beat_text: str, query: str, anchor: str = "") -> str:
    """One dark still for a story beat (CF fallback)."""
    core = C._clean_beat_text(beat_text, limit=700)
    same = (f"Keep the same protagonist and time of night as: {anchor}. "
            if anchor else
            "Keep the same protagonist and time of night throughout the story. ")
    return (
        "Create one single vertical 9:16 cinematic horror film still for this exact story beat. "
        "Show one clear moment only: the main subject, the visible action, and the location. "
        "Use the visual search cue as the scene anchor. " + same +
        "Make the action obvious at a glance, with realistic anatomy, natural "
        "lighting, strong foreground subject, and a readable silhouette. Do not illustrate abstract "
        "words literally; convert fear, silence, or danger into a visible detail in the scene. "
        "No collage, no split screen, no multiple scenes, no text, no subtitles, no watermark, "
        "no logo, no daytime, no cheerful colors. "
        f"Narration beat: {core} "
        f"Visual search cue: {query[:180]}. "
        "Dark blue-black shadows, muted red accent, cinematic low-key lighting, photorealistic, "
        "vertical composition, subject centered for a YouTube Short."
    )


def board_filter(beats, items, durations, get_font, has_drawtext) -> str:
    """Horror has no persistent overlay."""
    return ""
