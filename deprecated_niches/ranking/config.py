"""Ranking config: knobs (RANKING_ env overrides) + moved constants.

TOPICS / HOOK_STYLES / WEAK_OPENERS / BANNED_TITLE_HOOKS / _HOMONYM_HEADS
/ board geometry moved verbatim from niche_ranking.py (only the board
names lost their underscore so visuals.py can import them cleanly).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from niches.base import NicheConfig, env_bool, env_float, env_int, env_str

MODE = "ranking"

DEFAULT_TEXT_MODELS = (
    "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,"
    "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash,"
    "gemini-2.5-flash-lite"
)
DEFAULT_TTS_MODELS = "gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts"

CANVAS_W, CANVAS_H = 1080, 1920

RANK_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


@dataclass
class RankingConfig(NicheConfig):
    """Per-run knobs; RANKING_<name> wins, then bare <name>, then default."""
    voice: str = "Charon"
    voices: tuple[str, ...] = ("Charon",)
    edge_voice: str = "en-US-ChristopherNeural"
    tts_rate: str = "-5%"
    tts_pitch: str = "-10Hz"
    tts_volume: str = "+0%"
    tts_style: str = ""
    tone: str = "adaptive"
    target_wpm: int = 145
    pace_sec: int = 15
    text_models: tuple[str, ...] = ()
    tts_models: tuple[str, ...] = ()
    timeout: int = 40
    tts_timeout: int = 90
    tries: int = 2
    backoff: int = 5
    stills_per_beat: int = 3
    pexels_min_width: int = 1080
    pexels_cache_dir: str = "assets"
    min_audio_sec: float = 10.0
    max_audio_sec: float = 180.0
    bgm_volume: float = 0.08
    bed_globs: tuple[str, ...] = ("bgm*.mp3", "assets/bgm*.mp3")
    ai_topics: bool = True
    ai_topic_tries: int = 2
    similarity_retries: int = 2
    history_prompt_n: int = 20
    dedup_window: int = 110
    dedup_item_window: int = 30
    use_beat_query: bool = True
    use_mood: bool = False
    mystery_blur_max: float = 2.0
    gemini_keys: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, niche_name: str = MODE) -> "RankingConfig":
        """Read RANKING_* (bare names as fallback)."""
        def _tuple(name: str, default: str) -> tuple[str, ...]:
            return tuple(v.strip() for v in
                         env_str(name, default, "RANKING_").split(",") if v.strip())

        voices = _tuple("VOICES", env_str("VOICE", "Charon", "RANKING_") or "Charon")
        return cls(
            niche_name=niche_name or MODE,
            voice=env_str("VOICE", "Charon", "RANKING_"),
            voices=voices or ("Charon",),
            edge_voice=env_str("EDGE_VOICE", "en-US-ChristopherNeural", "RANKING_"),
            tts_rate=env_str("TTS_RATE", "-5%", "RANKING_"),
            tts_pitch=env_str("TTS_PITCH", "-10Hz", "RANKING_"),
            tts_volume=env_str("TTS_VOLUME", "+0%", "RANKING_"),
            tts_style=env_str("TTS_STYLE", "", "RANKING_"),
            tone=env_str("TONE", "adaptive", "RANKING_"),
            target_wpm=env_int("TARGET_WPM", 145, "RANKING_"),
            pace_sec=env_int("TTS_PACE_SEC", 15, "RANKING_"),
            text_models=_tuple("TEXT_MODELS", DEFAULT_TEXT_MODELS),
            tts_models=_tuple("TTS_MODELS", DEFAULT_TTS_MODELS),
            timeout=env_int("TIMEOUT", 40, "RANKING_"),
            tts_timeout=env_int("TTS_TIMEOUT", 90, "RANKING_"),
            tries=env_int("TRIES", 2, "RANKING_"),
            backoff=env_int("BACKOFF", 5, "RANKING_"),
            stills_per_beat=env_int("STILLS_PER_BEAT", 3, "RANKING_"),
            pexels_min_width=env_int("PEXELS_MIN_WIDTH", 1080, "RANKING_"),
            pexels_cache_dir=env_str("PEXELS_CACHE_DIR", "assets", "RANKING_"),
            min_audio_sec=env_float("MIN_AUDIO_SEC", 10.0, "RANKING_"),
            max_audio_sec=env_float("MAX_AUDIO_SEC", 180.0, "RANKING_"),
            bgm_volume=env_float("BGM_VOLUME", 0.08, "RANKING_"),
            ai_topics=env_bool("AI_TOPICS", True, "RANKING_"),
            ai_topic_tries=env_int("AI_TOPIC_TRIES", 2, "RANKING_"),
            similarity_retries=env_int("SIMILARITY_RETRIES", 2, "RANKING_"),
        )


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
    "largest planets in the solar system",
    "biggest moons in the solar system",
    "hottest chili peppers in the world",
    "most expensive fruits in the world",
    "most famous space missions in history",
    "largest telescopes in the world",
    "most famous festivals of lights in the world",
    "hotel room traps that overcharge you",
    "supercars that bankrupt owners",
    "watches that beat stocks",
    "stadium foods that cost more than tickets",
    "farm machines worth more than houses",
    "fire trucks the world envies",
    "tools that send DIYers to ER",
    "fastest police interceptors by country",
    "airport secrets gate agents never say",
    "foods chefs never order",
    "cruise-ship jobs with secret perks",
    "subway systems thieves love most",
    "bridges truckers fear most",
    "winter roads only pros drive",
    "cargo ships bigger than skyscrapers",
    "smallest apartments at luxury prices",
    "lighthouses that still save ships",
    "rental traps that steal deposits",
    "phone scams that steal millions",
    "parking-garage traps that steal cash",
    "tow fees that trap drivers",
    "toll roads that cost a fortune",
    "rest-stop scams that steal cash",
    "night-shift jobs that beat day pay",
    "kitchen dangers that send cooks to ER",
    "truck-stop nightmares that trap drivers",
    "marathon rescues that save runners",
    "delivery-van breakdowns that cost fleets",
    "highway pileups that trap drivers in fog",
    "gym machines that send beginners to ER",
    "e-bike battery fire risks",
    "ski lifts that trap riders mid-air",
    "locker-room thefts that steal gear",
    "roadside motels with secret cameras",
    "dam releases kayakers fear",
    "ferry crossings that risk lives in storms",
    "lighthouse storms sailors fear most",
)

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

WEAK_OPENERS = (
    "these are the 5",
    "human beings love",
    "deep beneath our feet",
    "nature hides",
    "push human engineering to",
)

BANNED_TITLE_HOOKS = (
    "number 1 is terrifying",
    "number one is terrifying",
    "#1 is terrifying",
    "will shock you",
    "you won't believe",
    "you won\u2019t believe",
    "you wont believe",
)

# Leaderboard geometry (1080x1920 canvas).
BOARD_X = 60
BOARD_Y = 710
BOARD_ROW_H = 100
BOARD_FONT_SIZE = 66
BOARD_NAME_LEN = 80
BOARD_MAX_WIDTH = CANVAS_W - (BOARD_X * 2)
BOARD_MIN_FONT_SIZE = 40

# Known homonym heads: bare-head queries collide with unrelated footage.
_HOMONYM_HEADS: dict[str, tuple[str, ...]] = {
    "monitor": ("lizard", "reptile"),
    "eclipse": ("yacht", "boat", "ship"),
    "hyperion": ("tree", "redwood"),
    "sequoia": ("tree", "redwood"),
    "sherman": ("tree", "redwood"),
    "python": ("snake", "reptile"),
    "cobra": ("snake", "reptile"),
    "viper": ("snake", "reptile"),
    "boa": ("snake", "constrictor"),
    "mamba": ("snake", "reptile"),
    "jaguar": ("cat", "feline"),
    "mustang": ("horse", "stallion"),
    "bronco": ("horse", "stallion"),
    "charger": ("horse", "stallion"),
    "falcon": ("bird", "eagle"),
    "raven": ("bird", "crow"),
    "stingray": ("fish", "ray"),
    "comet": ("fish", "goldfish"),
    "neon": ("fish", "tetra"),
    "angel": ("fish", "angelfish"),
    "atlas": ("moth", "insect"),
    "hercules": ("beetle", "insect"),
    "goliath": ("spider", "tarantula"),
    "titan": ("beetle", "insect"),
}


def is_bare_homonym_query(q: str) -> bool:
    """True for a lone homonym head ('Eclipse'); anchored forms are False."""
    try:
        toks = re.findall(r"[a-z0-9]+", (q or "").lower())
        if len(toks) != 1:
            return False
        t = toks[0]
        if t in _HOMONYM_HEADS:
            return True
        return t.endswith("s") and len(t) > 3 and t[:-1] in _HOMONYM_HEADS
    except Exception:
        return False
