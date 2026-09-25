"""Ranking stage 3: tiered stock visuals + leaderboard + mystery blur + mux.

Moved from core.py's ranking stock engine (query tables, gating, tiers,
segments, board_filter, mystery blur, mux fallbacks); HTTP/download via
engine.pexels, ffmpeg via engine.ffmpeg_utils, bed via engine.audio_mix.
Intermediates live in workdir; the Pexels cache dir keeps old filenames.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import textwrap
import time
from pathlib import Path

import requests

from engine import gemini_client as _gc
from engine import pexels as _px
from engine.audio_mix import loop_bed, synth_drone
from engine.ffmpeg_utils import (ffprobe_duration, find_font,
                                 has_ffmpeg_filter, log, retry, run_ffmpeg,
                                 subtitles_filter, warn)
from niches.base import Content, NarrationResult, VisualsResult

from .config import (BOARD_FONT_SIZE, BOARD_MAX_WIDTH, BOARD_MIN_FONT_SIZE,
                     BOARD_NAME_LEN, BOARD_ROW_H, BOARD_X, BOARD_Y,
                     CANVAS_H, CANVAS_W, RANK_FONTS, RankingConfig,
                     _HOMONYM_HEADS, is_bare_homonym_query)

W, H = CANVAS_W, CANVAS_H

_QUERY_STOPWORDS = frozenset({
    "a", "an", "the", "in", "on", "of", "at", "to", "for",
    "with", "from", "and", "or", "is", "are", "its", "it",
    "cinematic", "close", "creeping", "slowly", "swimming",
    "underwater", "slithering",
})

_GENERIC_HABITAT_WORDS = frozenset({
    "desert", "forest", "savanna", "savannah", "jungle", "ocean", "water",
    "river", "sea", "mountain", "mountains", "sky", "underwater", "swamp",
    "grassland", "plains", "trees", "dunes",
})

_DOMESTIC_DISQUALIFIERS = frozenset({
    "indoor", "indoors", "sofa", "couch", "living room", "pet", "pets",
    "sweater", "costume", "leash", "collar", "bedroom", "blanket",
    "domestic", "housecat", "kitten", "puppy", "furniture",
    "home", "apartment", "rug", "carpet",
})


def _pexels_key() -> str:
    """Stock API key (bare env, same as the old pipeline)."""
    return os.getenv("PEXELS_API_KEY", "")


def _is_wild_topic(topic: str) -> bool:
    """True if topic is about wildlife, nature, or wild creatures."""
    t = (topic or "").lower()
    wild_words = {
        "wild", "wildlife", "nature", "predator", "predators", "safari",
        "jungle", "forest", "ocean", "sea", "marine", "deep sea",
        "deadliest", "poisonous", "venomous", "carnivore",
    }
    return any(w in t for w in wild_words)


def _clean_stock_query(q: str, item_name: str = "") -> str:
    """Strip habitat/scenery words unless part of item_name."""
    clean_q = (q or "").strip()
    if not clean_q:
        return ""
    item_tokens = set(re.findall(r"[a-z0-9]+", (item_name or "").lower()))
    words = clean_q.split()
    kept = []
    for w in words:
        wl = re.sub(r"[^a-z0-9]", "", w.lower())
        if wl in _GENERIC_HABITAT_WORDS and wl not in item_tokens:
            continue
        kept.append(w)
    return " ".join(kept) if kept else clean_q


def _has_domestic_disqualifier(haystack: str) -> bool:
    """True when text mentions domestic/indoor keywords."""
    tokens = set(re.findall(r"[a-z0-9]+", (haystack or "").lower()))
    return bool(tokens & _DOMESTIC_DISQUALIFIERS)


def _query_words(q: str) -> list[str]:
    """Lowercase alphanum tokens minus stopwords."""
    return [w for w in re.findall(r"[a-z0-9]+", (q or "").lower())
            if w not in _QUERY_STOPWORDS]


def _query_variants(q: str) -> list[str]:
    """Full query + drop-last-word backoff until 2 words remain (max 3)."""
    full = (q or "").strip()
    if not full:
        return []
    words = full.split()
    if len(words) <= 2:
        return [full]
    variants = [full]
    while len(words) > 2 and len(variants) < 3:
        words = words[:-1]
        variants.append(" ".join(words))
    seen: list[str] = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    return seen


def _score_candidate(alt: str, url: str, words: list[str], phrase: str = "") -> int:
    """Score candidate text; exact phrase match earns a bonus."""
    h_alt = (alt or "").lower()
    h_url = (url or "").lower()
    tokens_alt = set(re.findall(r"[a-z0-9]+", h_alt))
    tokens_url = set(re.findall(r"[a-z0-9]+", h_url))
    alt_hits = sum(1 for w in words if w and (w in tokens_alt or (len(w) >= 6 and w in h_alt)))
    url_hits = sum(1 for w in words if w and (w in tokens_url or (len(w) >= 6 and w in h_url)))
    score = 2 * alt_hits + url_hits
    if phrase and len(phrase.split()) > 1:
        phrase_clean = phrase.lower()
        if phrase_clean in h_alt or phrase_clean in re.sub(r"[-_]", " ", h_alt):
            score += 6
        elif phrase_clean in re.sub(r"[-_]", " ", h_url):
            score += 4
    return score


_BOGUS_FAMILY_WORDS = frozenset({
    "said", "say", "says", "told", "talk", "speak", "go", "goes", "went", "get", "gets",
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by",
    "al", "el", "del", "de", "da", "di", "du", "la", "le", "les", "san", "santa", "von", "van", "der", "den",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "first", "second", "third",
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "jr", "sr",
    "big", "large", "small", "tiny", "new", "old", "top", "pro", "max", "ultra", "plus", "super", "hyper",
    "gt", "gts", "gtr", "rs", "amg", "sv", "evo", "svj", "ss", "v8", "v10", "v12", "edition", "series",
})

_DOMAIN_SYNONYMS: dict[str, set[str]] = {
    "yacht": {
        "yacht", "yachts", "superyacht", "superyachts", "megayacht", "megayachts",
        "boat", "boats", "ship", "ships", "vessel", "vessels", "watercraft",
        "sailing", "sail", "ocean", "sea", "marine", "deck", "harbor", "marina", "port", "water",
    },
    "ship": {
        "ship", "ships", "vessel", "vessels", "boat", "boats", "cruise", "cruises",
        "freighter", "cargo", "container", "tanker", "ocean", "sea", "harbor", "port", "marine", "sailing", "water",
    },
    "boat": {
        "boat", "boats", "ship", "ships", "vessel", "vessels", "yacht", "yachts",
        "watercraft", "sailing", "water", "sea", "ocean", "lake", "river", "harbor",
    },
    "submarine": {
        "submarine", "submarines", "sub", "subs", "submersible", "underwater",
        "naval", "navy", "ocean", "sea", "torpedo", "water",
    },
    "shipwreck": {
        "shipwreck", "shipwrecks", "wreck", "wrecks", "sunken", "underwater",
        "ocean", "sea", "submerged", "coral", "reef", "ship",
    },
    "airplane": {
        "plane", "planes", "airplane", "airplanes", "aircraft", "jet", "jets",
        "aviation", "flight", "flying", "airliner", "airliners", "airport", "runway", "sky",
    },
    "plane": {
        "plane", "planes", "airplane", "airplanes", "aircraft", "jet", "jets",
        "aviation", "flight", "flying", "airliner", "airliners", "airport", "runway", "sky",
    },
    "aircraft": {
        "plane", "planes", "airplane", "airplanes", "aircraft", "jet", "jets",
        "aviation", "flight", "flying", "airliner", "airliners", "airport", "runway", "sky",
    },
    "jet": {
        "jet", "jets", "fighter", "plane", "planes", "aircraft", "airplane", "airplanes",
        "aviation", "cockpit", "supersonic", "sky", "flight",
    },
    "helicopter": {
        "helicopter", "helicopters", "chopper", "choppers", "rotor", "rotors",
        "aircraft", "aviation", "flight", "sky",
    },
    "car": {
        "car", "cars", "automobile", "automobiles", "supercar", "supercars",
        "hypercar", "hypercars", "vehicle", "vehicles", "auto", "motor", "drive", "driving", "road", "racing",
    },
    "supercar": {
        "car", "cars", "automobile", "automobiles", "supercar", "supercars",
        "hypercar", "hypercars", "vehicle", "vehicles", "auto", "motor", "drive", "driving", "road", "speed",
    },
    "truck": {
        "truck", "trucks", "pickup", "pickups", "vehicle", "vehicles", "semi", "lorry", "road", "drive",
    },
    "motorcycle": {
        "motorcycle", "motorcycles", "motorbike", "motorbikes", "bike", "bikes", "moto", "rider", "riding", "road",
    },
    "train": {
        "train", "trains", "locomotive", "locomotives", "railway", "railroad",
        "rail", "rails", "metro", "subway", "bullet", "tracks",
    },
    "tank": {
        "tank", "tanks", "armor", "armored", "armour", "military", "army",
        "combat", "cannon", "turret", "tracks", "vehicle",
    },
    "rocket": {
        "rocket", "rockets", "spacecraft", "spaceship", "launch", "space",
        "nasa", "booster", "orbit", "astronaut", "propulsion",
    },
    "telescope": {
        "telescope", "telescopes", "observatory", "astronomy", "space", "optics", "stars",
    },
    "planet": {
        "planet", "planets", "solar", "system", "space", "astronomy", "orbit", "cosmos",
    },
    "moon": {
        "moon", "moons", "lunar", "orbit", "space", "astronomy", "crater", "craters",
    },
    "building": {
        "building", "buildings", "skyscraper", "skyscrapers", "architecture",
        "tower", "towers", "edifice", "facade", "structure", "structures", "city", "skyline",
    },
    "skyscraper": {
        "skyscraper", "skyscrapers", "building", "buildings", "tower", "towers",
        "architecture", "highrise", "high-rise", "skyline", "city",
    },
    "tower": {
        "tower", "towers", "building", "buildings", "skyscraper", "skyscrapers",
        "structure", "structures", "spire", "architecture",
    },
    "bridge": {
        "bridge", "bridges", "suspension", "viaduct", "overpass", "arch",
        "cables", "crossing", "river", "span", "water",
    },
    "castle": {
        "castle", "castles", "fortress", "fortresses", "fort", "palace", "palaces",
        "citadel", "medieval", "stone", "walls",
    },
    "palace": {
        "palace", "palaces", "castle", "castles", "royal", "mansion", "estate",
        "residence", "architecture", "court",
    },
    "temple": {
        "temple", "temples", "shrine", "shrines", "monastery", "sanctuary", "ancient", "stone", "architecture",
    },
    "church": {
        "church", "churches", "cathedral", "cathedrals", "basilica", "chapel", "architecture",
    },
    "mosque": {
        "mosque", "mosques", "minaret", "minarets", "dome", "architecture",
    },
    "dam": {
        "dam", "dams", "reservoir", "hydroelectric", "spillway", "water", "concrete", "lake", "river",
    },
    "tunnel": {
        "tunnel", "tunnels", "subway", "highway", "rail", "underground", "road",
    },
    "airport": {
        "airport", "airports", "terminal", "runway", "tarmac", "aviation", "airplane", "airplanes",
    },
    "lighthouse": {
        "lighthouse", "lighthouses", "beacon", "coast", "coastal", "ocean", "sea", "tower",
    },
    "fountain": {
        "fountain", "fountains", "water", "spray", "cascade", "pool", "monument",
    },
    "coaster": {
        "roller coaster", "rollercoaster", "coaster", "coasters", "ride", "rides", "amusement", "tracks",
    },
    "wheel": {
        "ferris wheel", "ferris", "wheel", "wheels", "amusement", "carnival", "fair",
    },
    "aquarium": {
        "aquarium", "aquariums", "tank", "fish", "marine", "underwater", "reef", "ocean",
    },
    "museum": {
        "museum", "museums", "exhibit", "exhibition", "gallery", "art", "sculpture", "artifact",
    },
    "library": {
        "library", "libraries", "books", "bookshelves", "reading",
    },
    "stadium": {
        "stadium", "stadiums", "arena", "arenas", "pitch", "field", "sports",
    },
}

_ANIMAL_DOMAINS: dict[str, set[str]] = {
    "snake": {"snake", "snakes", "serpent", "serpents", "reptile", "reptiles", "viper", "vipers", "cobra", "cobras", "python", "mamba", "venomous"},
    "spider": {"spider", "spiders", "arachnid", "arachnids", "tarantula", "web"},
    "shark": {"shark", "sharks", "predator", "ocean", "sea", "marine", "underwater", "fin", "fish"},
    "bird": {"bird", "birds", "avian", "feather", "feathers", "flight", "flying", "wing", "wings", "eagle", "hawk", "owl", "falcon"},
    "eagle": {"eagle", "eagles", "bird", "birds", "raptor", "flying", "flight"},
    "owl": {"owl", "owls", "bird", "birds", "raptor", "nocturnal", "feathers"},
    "cat": {"cat", "cats", "feline", "felines", "kitten", "panther", "tiger", "lion", "leopard", "cheetah", "jaguar"},
    "dog": {"dog", "dogs", "canine", "canines", "puppy", "puppies", "hound", "retriever", "terrier"},
    "bear": {"bear", "bears", "grizzly", "polar bear", "cub", "predator", "fur"},
    "crocodile": {"crocodile", "crocodiles", "alligator", "alligators", "reptile", "reptiles", "swamp", "river"},
    "reptile": {"reptile", "reptiles", "lizard", "lizards", "snake", "snakes", "iguana", "gecko"},
    "fish": {"fish", "fishes", "underwater", "ocean", "sea", "marine", "aquarium", "reef", "swimming"},
    "whale": {"whale", "whales", "cetacean", "ocean", "sea", "marine", "mammal"},
    "frog": {"frog", "frogs", "amphibian", "amphibians", "toad", "toads", "jungle"},
    "crab": {"crab", "crabs", "crustacean", "ocean", "sea", "beach", "sand", "shell"},
    "insect": {"insect", "insects", "bug", "bugs", "beetle", "butterfly", "moth", "ant", "bee"},
    "dinosaur": {"dinosaur", "dinosaurs", "fossil", "fossils", "jurassic", "prehistoric", "t-rex"},
}

_CATEGORY_NOUNS = frozenset(
    {k for k in _DOMAIN_SYNONYMS} | {f"{k}s" for k in _DOMAIN_SYNONYMS}
    | {k for k in _ANIMAL_DOMAINS} | {f"{k}s" for k in _ANIMAL_DOMAINS}
    | {d for _ds in _HOMONYM_HEADS.values() for d in _ds}
    | {f"{d}s" for _ds in _HOMONYM_HEADS.values() for d in _ds}
)


def _get_topic_domain_info(topic: str) -> tuple[str, set[str], bool]:
    """(base_subject, domain_keywords, is_object_topic) from topic."""
    t_clean = (topic or "").lower()
    t_tokens = set(re.findall(r"[a-z0-9]+", t_clean))
    for k, syns in _DOMAIN_SYNONYMS.items():
        if (k in t_tokens or (k.endswith("s") and k[:-1] in t_tokens)
                or (k + "s") in t_tokens or (" " in k and k in t_clean)):
            return k, syns, True
    for k, syns in _ANIMAL_DOMAINS.items():
        if (k in t_tokens or (k.endswith("s") and k[:-1] in t_tokens)
                or (k + "s") in t_tokens or (" " in k and k in t_clean)):
            return k, syns, False
    tqueries = _topic_stock_queries(topic)
    if tqueries:
        return tqueries[-1][0], set(tqueries[-1][1]), False
    return "", set(), False


def _item_require_words(item: str, domain: set[str] | None = None) -> dict:
    """Head/modifier/phrase/domain requirements for a ranked item name."""
    try:
        clean = (item or "").lower().strip()
        tokens = re.findall(r"[a-z0-9]+", clean)
        if not tokens:
            return {}
        head = tokens[-1]
        heads = [head, head[:-1] if head.endswith("s") and len(head) > 3 else head + "s"]
        return {"heads": heads, "modifiers": tokens[:-1],
                "phrase": clean, "domain": domain or set()}
    except Exception:
        return {}


def _meets_require(haystack: str, require) -> bool:
    """True when no gate applies or candidate text satisfies it."""
    if not require:
        return True
    h = (haystack or "").lower()
    h_tokens = set(re.findall(r"[a-z0-9]+", h))
    if not h_tokens:
        return False
    h_clean = re.sub(r"[-_]", " ", h)
    if isinstance(require, dict):
        domain = require.get("domain")
        if domain:
            has_domain = (any(d in h_tokens for d in domain if " " not in d) or
                          any(d in h_clean for d in domain if " " in d))
            if not has_domain:
                return False
        heads = require.get("heads", [])
        modifiers = require.get("modifiers", [])
        phrase = require.get("phrase", "")
        if phrase and len(phrase.split()) > 1 and (phrase in h or phrase in h_clean):
            return True
        if heads and not any(hd in h_tokens for hd in heads):
            return False
        if modifiers and not any(m in h_tokens for m in modifiers):
            return False
        return True
    if isinstance(require, (list, tuple, set)):
        for w in require:
            w_str = str(w).lower().strip()
            if " " in w_str:
                if w_str in h or w_str in h_clean:
                    return True
            else:
                if w_str in h_tokens:
                    return True
                _w_sing = w_str[:-1] if w_str.endswith("s") and len(w_str) > 3 else w_str
                if (len(w_str) >= 6 and w_str not in _HOMONYM_HEADS
                        and _w_sing not in _HOMONYM_HEADS and w_str in h):
                    return True
        return False
    return True


def _family_query_for_item(item: str, topic: str = "") -> tuple[str, list[str]]:
    """(query, require) from the item's family noun; ("", []) when unsafe."""
    try:
        tokens = re.findall(r"[a-z0-9]+", (item or "").lower())
        if not tokens:
            return "", []
        fam = tokens[-1]
        if fam in _BOGUS_FAMILY_WORDS or len(fam) < 3:
            return "", []
        _subj, domain_kw, is_obj = _get_topic_domain_info(topic)
        if is_obj:
            if fam not in domain_kw:
                return "", []
            return fam, [fam]
        if fam in _HOMONYM_HEADS:
            cands = list(_HOMONYM_HEADS[fam])
            topic_words = {str(d).lower() for d in (domain_kw or set()) if str(d).strip()}
            if _subj and str(_subj).strip():
                topic_words.add(str(_subj).lower())
            topic_words |= {w[:-1] if w.endswith("s") and len(w) > 3 else w + "s"
                            for w in list(topic_words) if w}
            best = next((d for d in cands if d in topic_words), None)
            if best is None:
                return "", []
            return f"{fam} {best}", [f"{fam} {d}" for d in cands]
        if _is_wild_topic(topic) and fam in {"cat", "dog", "pig", "horse", "duck", "rabbit", "goat", "sheep"}:
            return f"wild {fam}", [f"wild {fam}", fam]
        return fam, [fam]
    except Exception:
        return "", []


def _anchor_query_with_subject(clean_q: str, item_phrase: str,
                               base_subj: str, domain_kw: set | None,
                               is_obj: bool = False) -> str:
    """Append the topic subject noun when the beat query is ungrounded."""
    try:
        q = (clean_q or "").strip()
        subj = (base_subj or "").strip().lower()
        if not q or not subj:
            return clean_q
        if is_obj:
            if not any(k in q.lower() for k in (set(domain_kw or set()) | {subj})):
                return f"{q} {base_subj.strip()}"
            return q
        q_tokens = set(re.findall(r"[a-z0-9]+", q.lower()))
        subj_forms = {subj, subj[:-1] if subj.endswith("s") and len(subj) > 3 else subj + "s"}
        domain_l = {str(d).lower() for d in (domain_kw or set()) if str(d).strip()}
        if q_tokens & subj_forms or q_tokens & domain_l:
            return q
        if any(d in q.lower() for d in domain_l if " " in d):
            return q
        itoks = re.findall(r"[a-z0-9]+", (item_phrase or "").lower())
        head = itoks[-1] if itoks else ""
        if not head:
            return q
        if len(q_tokens) > 1 and head in _HOMONYM_HEADS:
            cands = list(_HOMONYM_HEADS[head])
            topic_words = set(domain_l) | set(subj_forms)
            topic_words |= {w[:-1] if w.endswith("s") and len(w) > 3 else w + "s"
                            for w in list(topic_words) if w}
            best = next((d for d in cands if d in topic_words), None)
            if best is None:
                return q
            return f"{q} {best}"
        if len(q_tokens) == 1:
            head_forms = {head, head[:-1] if head.endswith("s") and len(head) > 3 else head + "s"}
            if head_forms & domain_l or head_forms & _CATEGORY_NOUNS:
                return q
            return f"{q} {base_subj.strip()}"
        return q
    except Exception:
        return clean_q


_TOPIC_META_NOUNS = frozenset({
    "breeds", "breed", "species", "types", "type", "kinds", "kind",
    "varieties", "variety", "categories", "category",
})
_NON_PLURAL_NOUNS = frozenset({
    "this", "gas", "glass", "grass", "boss", "cross", "crisis", "basis",
    "status", "circus", "canvas", "apparatus", "series", "species",
})
_SUPERLATIVES = {
    "largest": "large", "biggest": "big", "fastest": "fast", "tallest": "tall",
    "deepest": "deep", "deadliest": "deadly", "heaviest": "heavy", "wild": "wild",
    "scariest": "scary", "oldest": "old", "strongest": "strong", "longest": "long",
    "highest": "high", "smallest": "small", "coldest": "cold", "hottest": "hot",
    "giant": "giant", "massive": "massive",
}


def _topic_stock_queries(topic: str) -> list[tuple[str, list[str]]]:
    """Prioritized (query, require) fallbacks for the topic's subject noun."""
    try:
        raw_tokens = re.findall(r"[a-z0-9]+", (topic or "").lower())
        tokens = [w for w in raw_tokens if w not in _QUERY_STOPWORDS and w not in {
            "world", "earth", "ever", "all", "time", "history", "top", "5", "five", "most"
        }]
        if not tokens:
            return []
        primary_token = None
        qualifier = None
        for w in tokens:
            if w in _SUPERLATIVES:
                qualifier = _SUPERLATIVES[w]
                break
            elif w.endswith(("ous", "ive", "ful")):
                qualifier = w
                break
        for idx, w in enumerate(tokens):
            if w in _TOPIC_META_NOUNS and idx > 0:
                primary_token = tokens[idx - 1]
                if not qualifier and idx > 1 and tokens[idx - 2] not in _QUERY_STOPWORDS:
                    qualifier = tokens[idx - 2]
                break
        if not primary_token:
            for idx in range(len(tokens) - 1, -1, -1):
                w = tokens[idx]
                if w.endswith("ous") or w in _NON_PLURAL_NOUNS:
                    continue
                if len(w) >= 3 and (w.endswith("s") or w in {"aircraft", "sheep", "deer", "fish"}):
                    primary_token = w
                    if not qualifier and idx > 0 and tokens[idx - 1] not in _QUERY_STOPWORDS and not tokens[idx - 1].isdigit():
                        qualifier = tokens[idx - 1]
                    break
        if not primary_token and tokens:
            primary_token = tokens[-1]
            if not qualifier and len(tokens) > 1 and not tokens[-2].isdigit():
                qualifier = tokens[-2]
        p = primary_token or ""
        if p.endswith("ies") and len(p) > 4:
            sing, plur = p[:-3] + "y", p
        elif p.endswith(("ses", "xes", "ches", "shes")) and len(p) > 4:
            sing, plur = p[:-2], p
        elif p.endswith("s") and not p.endswith("ss") and len(p) > 3 and not p.endswith("ous"):
            sing, plur = p[:-1], p
        else:
            sing = p
            plur = p if p in {"aircraft", "sheep", "deer", "fish"} else (p + "s")
        clean_qual = qualifier or ""
        if clean_qual in _SUPERLATIVES:
            clean_qual = _SUPERLATIVES[clean_qual]
        queries = []
        if clean_qual and clean_qual not in {"most", "the", sing}:
            queries.append((f"{clean_qual} {sing}",
                            [f"{clean_qual} {plur}", f"{clean_qual} {sing}", plur, sing]))
        q_base = sing
        req_base = [plur, sing] if plur != sing else [sing]
        if (q_base, req_base) not in queries:
            queries.append((q_base, req_base))
        return queries
    except Exception:
        return []


def _search_videos(query: str, api_key: str) -> list[dict]:
    """Portrait/medium video search via engine.pexels (retry like the old path)."""
    return retry("Pexels search",
                 lambda: _px.search_videos(query, api_key, per_page=8,
                                           orientation="portrait", size="medium"),
                 tries=3, backoff=5)


def _search_photos(query: str, api_key: str) -> list[dict]:
    """Portrait/large photo search (engine has no photo primitive; requests direct)."""
    def _get():
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": api_key},
            params={"query": query, "per_page": 10,
                    "orientation": "portrait", "size": "large"},
            timeout=30)
        r.raise_for_status()
        return r.json().get("photos", [])
    return retry("Pexels photo search", _get, tries=3, backoff=5)


def _best_video(query: str, api_key: str, cache_dir: Path, min_width: int,
                used: set | None = None, require=None,
                is_wild: bool = False, phrase: str = "") -> tuple:
    """Best literal-match stock video. Returns (dest|None, score, key|None, credit)."""
    if not api_key:
        return None, 0, None, ""
    try:
        words = _query_words(query)
        threshold = min(2, len(words)) if len(words) > 1 else 1
        best_video = None
        best_pick_file = None
        best_score = -1
        for variant in _query_variants(query):
            for v in _search_videos(variant, api_key):
                if used is not None and v.get("id") in used:
                    continue
                url = v.get("url", "")
                v_tags = " ".join(t.get("name", "") if isinstance(t, dict) else str(t)
                                  for t in v.get("tags", []))
                haystack = f"{url} {v_tags}".strip() if v_tags else url
                if is_wild and _has_domestic_disqualifier(haystack):
                    continue
                if not _meets_require(haystack, require):
                    continue
                score = _score_candidate(v_tags or url, url, words, phrase)
                if score < threshold:
                    continue
                cands = sorted((f for f in v.get("video_files", []) if f.get("link")),
                               key=lambda f: f.get("width", 0))
                big_enough = [f for f in cands if f.get("width", 0) >= min_width]
                pick = (big_enough or cands)
                if not pick:
                    continue
                if score > best_score:
                    best_score = score
                    best_video = v
                    best_pick_file = pick[0]
            if best_video is not None and best_score >= threshold:
                break
        if best_video is None or best_pick_file is None:
            return None, 0, None, ""
        dest = cache_dir / f"pexels_{best_video['id']}.mp4"
        if not dest.exists():
            _px.download(best_pick_file["link"], dest)
        credit = f"Pexels: {_px.parse_photographer(best_video)}".rstrip()
        return dest, best_score, best_video["id"], credit
    except Exception as e:
        warn(f"Pexels failed for '{query}' ({e}).")
    return None, 0, None, ""


def _best_photos(query: str, api_key: str, cache_dir: Path, min_width: int,
                 n: int = 1, used: set | None = None, require=None,
                 is_wild: bool = False, phrase: str = "") -> list[tuple]:
    """Top-N literal-match photos as (dest, score, key, alt, credit)."""
    if n <= 0 or not api_key:
        return []
    try:
        words = _query_words(query)
        threshold = min(2, len(words)) if len(words) > 1 else 1
        pool: dict = {}
        for variant in _query_variants(query):
            for p in _search_photos(variant, api_key):
                pid = p.get("id")
                if pid in pool:
                    continue
                if used is not None and f"photo_{pid}" in used:
                    continue
                alt = p.get("alt", "")
                url = p.get("url", "")
                haystack = f"{alt} {url}"
                if is_wild and _has_domestic_disqualifier(haystack):
                    continue
                if not _meets_require(haystack, require):
                    continue
                score = _score_candidate(alt, url, words, phrase)
                if score < threshold:
                    continue
                w = p.get("width", 0) or 0
                if w and w < min_width:
                    continue
                src = p.get("src", {})
                if not src.get("original"):
                    continue
                h = p.get("height", 0) or 0
                rank = abs(w / h - 9 / 16) if w and h else 1.0
                pool[pid] = (p, score, (w or 0) * (h or 0), rank)
            if len(pool) >= n:
                break
        if not pool:
            return []
        ranked = sorted(pool.values(), key=lambda t: (-t[1], -t[2], t[3]))
        out = []
        for (p, score, _area, _rank) in ranked[:max(1, n)]:
            dest = cache_dir / f"pexels_{p['id']}.jpg"
            if not dest.exists():
                try:
                    _px.download(p["src"]["original"], dest)
                except Exception as e:
                    if dest.exists():
                        dest.unlink()
                    warn(f"Pexels photo download failed for '{query}' ({e}).")
                    continue
            credit = f"Pexels: {(p.get('photographer') or '').strip()}".rstrip()
            out.append((dest, score, f"photo_{p['id']}", p.get("alt", ""), credit))
        return out
    except Exception as e:
        warn(f"Pexels photo failed for '{query}' ({e}).")
    return []


def _beat_synonyms(beats: list[dict] | None, cfg: RankingConfig) -> list[list[str]]:
    """Alternate search phrasings per beat; one batched call, best-effort []."""
    n = len(beats or [])
    if not n:
        return []
    keys = list(cfg.gemini_keys) or _gc.parse_keys()
    if not keys:
        log("Beat synonyms skipped (no Gemini keys); tiers unchanged.")
        return [[] for _ in range(n)]
    client = _gc.GeminiClient(keys, timeout=20, tries=cfg.tries, backoff=cfg.backoff)
    lines = []
    for idx, b in enumerate(beats or []):
        q = str((b or {}).get("query", "")).strip()
        t = " ".join(str((b or {}).get("text", "")).split())[:100]
        lines.append(f"{idx + 1}. query='{q}' text='{t}'")
    prompt = (
        "For each of these video beats, suggest 2-3 alternate stock-footage "
        "search phrasings for the SAME subject (alternates, not new "
        "subjects). Reply with ONLY valid JSON: "
        "{\"synonyms\": [[...], ...]} with exactly "
        f"{n} inner lists in beat order. Rules: each alternate 1-3 words, "
        "plain ASCII, portrait-friendly concrete nouns; keep proper names "
        "anchored with the subject noun (e.g. 'Eclipse yacht' -> "
        "['luxury yacht', 'superyacht deck']); no abstract concepts, "
        "no extra keys.\n"
        + "\n".join(lines) + "\nOutput only the JSON object."
    )
    for model in cfg.text_models:
        try:
            lists = client.generate_json(model, prompt, generation_config={
                "temperature": 0.3, "maxOutputTokens": 512}).get("synonyms")
            if not isinstance(lists, list) or len(lists) != n:
                raise ValueError("synonyms shape mismatch")
            out = []
            for idx, alts in enumerate(lists):
                keep: list[str] = []
                orig = str(((beats or [])[idx] or {}).get("query", "")).strip().casefold()
                if isinstance(alts, list):
                    for a in alts:
                        if not isinstance(a, str):
                            continue
                        s = a.strip()
                        if (not s or len(s) > 40 or not s.isascii()
                                or not 1 <= len(s.split()) <= 3
                                or s.casefold() == orig or s in keep):
                            continue
                        keep.append(s)
                        if len(keep) >= 3:
                            break
                out.append(keep)
            log(f"Beat synonyms: {sum(len(k) for k in out)} alternates for {n} beats.")
            return out
        except Exception as e:
            log(f"Synonym model {model} unusable ({e}), trying next...")
            time.sleep(2)
    log("Beat synonyms unavailable, exact/family/topic tiers unchanged.")
    return [[] for _ in range(n)]


def _split_beat(text: str, n: int) -> list[str]:
    """Split beat narration into n word-even parts for mid-beat cuts."""
    words = text.split()
    if n <= 1 or len(words) <= n:
        return [text]
    size = len(words) / n
    parts = [" ".join(words[int(i * size):int((i + 1) * size)]).strip()
             for i in range(n)]
    return [p for p in parts if p] or [text]


def _stock_offset(stock: Path, part_idx: int, parts: int) -> float | None:
    """Seek so mid-beat cuts show different footage. None = start."""
    if part_idx == 0 or parts <= 1:
        return None
    try:
        return max(0.0, ffprobe_duration(stock) * part_idx / parts)
    except Exception:
        return None


def beat_card(i: int, text: str, workdir: Path) -> Path:
    """Title card carrying the beat's text (gap filler, always matches)."""
    from PIL import Image, ImageDraw  # lazy: Pillow only needed when carding

    colors = [(18, 18, 40), (40, 18, 30), (18, 40, 35), (35, 35, 18)]
    img = Image.new("RGB", (W, H), colors[i % len(colors)])
    ImageDraw.Draw(img).multiline_text((80, 800), textwrap.fill(text[:220], width=28),
                                       fill=(255, 255, 255), spacing=8)
    p = workdir / f"card_{i}.png"
    img.save(p)
    return p


def visuals_for_beats(beats: list[dict], cfg: RankingConfig, workdir: Path,
                      items: list | None = None, topic: str = "") -> tuple[list[tuple], list[str]]:
    """Per beat: stills-per-beat cuts (video vs photos -> card). Tiers: exact
    -> synonym -> family -> topic -> topic-loose -> topic-reuse -> reused/card.
    Returns ([(clip, seek_or_None, beat_idx)], credits)."""
    api_key = _pexels_key()
    cache_dir = Path(cfg.pexels_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    clips: list[tuple] = []
    credits: list[str] = []
    used_pexels: set = set()
    labeled = [(idx, str(b.get("label", "")))
               for idx, b in enumerate(beats or []) if str(b.get("label", "")).isdigit()]
    names = list(items or [])
    gate_for: dict = {}
    fam_for: dict = {}
    is_wild = _is_wild_topic(topic)
    base_subj, domain_kw, is_obj = _get_topic_domain_info(topic)
    for (beat_idx, _lab), name in zip(labeled, names + [""] * len(labeled)):
        gate_for[beat_idx] = _item_require_words(name, domain=domain_kw) if name else {}
        fam_for[beat_idx] = _family_query_for_item(name, topic) if name else ("", [])
    tqueries = _topic_stock_queries(topic)
    need_syn = bool(api_key and cfg.use_beat_query
                    and any(gate_for.get(idx) for idx in range(len(beats or []))))
    syn_for = _beat_synonyms(beats, cfg) if need_syn else [[] for _ in (beats or [])]
    still_n = 0
    n_video = n_photo = n_card = 0
    for i, b in enumerate(beats):
        parts = _split_beat(b["text"], cfg.stills_per_beat)
        raw_query = b.get("query", "") if cfg.use_beat_query else ""
        tiered = bool(cfg.use_beat_query and gate_for.get(i))
        require = gate_for.get(i)
        item_phrase = require.get("phrase", "") if isinstance(require, dict) else ""
        clean_q = _clean_stock_query(raw_query, item_phrase) if tiered else raw_query
        query = (_anchor_query_with_subject(clean_q, item_phrase, base_subj, domain_kw, is_obj)
                 if tiered and base_subj else clean_q)
        vpath, vraw, vkey, vcredit = _best_video(
            query, api_key, cache_dir, cfg.pexels_min_width, used_pexels,
            require=require, is_wild=is_wild, phrase=item_phrase)
        plist = _best_photos(query, api_key, cache_dir, cfg.pexels_min_width,
                             len(parts), used_pexels, require=require,
                             is_wild=is_wild, phrase=item_phrase)
        tier, eff_query = "exact", query
        if tiered and vpath is None and not plist:
            for syn in (syn_for[i] if i < len(syn_for) else []):
                spath, sraw, skey, scredit = _best_video(
                    syn, api_key, cache_dir, cfg.pexels_min_width, used_pexels,
                    require=require, is_wild=is_wild, phrase=syn)
                splist = _best_photos(syn, api_key, cache_dir, cfg.pexels_min_width,
                                      len(parts), used_pexels, require=require,
                                      is_wild=is_wild, phrase=syn)
                if spath is not None or splist:
                    vpath, vraw, vkey, vcredit, plist = spath, sraw, skey, scredit, splist
                    tier, eff_query = "synonym", syn
                    break
        if tiered and vpath is None and not plist:
            fquery, frequire = fam_for.get(i, ("", []))
            if fquery and frequire:
                fpath, fraw, fkey, fcredit = _best_video(
                    fquery, api_key, cache_dir, cfg.pexels_min_width, used_pexels,
                    require=require, is_wild=is_wild, phrase=fquery)
                fplist = _best_photos(fquery, api_key, cache_dir, cfg.pexels_min_width,
                                      len(parts), used_pexels, require=frequire,
                                      is_wild=is_wild, phrase=fquery)
                if fpath is not None or fplist:
                    vpath, vraw, vkey, vcredit, plist = fpath, fraw, fkey, fcredit, fplist
                    tier, eff_query = "family", fquery
        if vpath is None and not plist and tqueries:
            for tq, treq in tqueries:
                tpath, traw, tkey, tcredit = _best_video(
                    tq, api_key, cache_dir, cfg.pexels_min_width, used_pexels,
                    require=treq, is_wild=is_wild, phrase=tq)
                tplist = _best_photos(tq, api_key, cache_dir, cfg.pexels_min_width,
                                      len(parts), used_pexels, require=treq,
                                      is_wild=is_wild, phrase=tq)
                if tpath is not None or tplist:
                    vpath, vraw, vkey, vcredit, plist = tpath, traw, tkey, tcredit, tplist
                    tier, eff_query = "topic", tq
                    break
        if vpath is None and not plist and tqueries and domain_kw:
            loose = sorted(domain_kw)
            for tq, _treq in tqueries:
                for u in (used_pexels, None):
                    lpath, lraw, lkey, lcredit = _best_video(
                        tq, api_key, cache_dir, cfg.pexels_min_width, u,
                        require=loose, is_wild=is_wild, phrase=tq)
                    lplist = _best_photos(tq, api_key, cache_dir, cfg.pexels_min_width,
                                          len(parts), u, require=loose,
                                          is_wild=is_wild, phrase=tq)
                    if lpath is not None or lplist:
                        vpath, vraw, vkey, vcredit, plist = lpath, lraw, lkey, lcredit, lplist
                        tier, eff_query = "topic-loose", tq
                        break
                if vpath is not None or plist:
                    break
        if vpath is None and not plist and tqueries:
            for tq, treq in tqueries:
                tpath, traw, tkey, tcredit = _best_video(
                    tq, api_key, cache_dir, cfg.pexels_min_width, used=None,
                    require=treq, is_wild=is_wild, phrase=tq)
                tplist = _best_photos(tq, api_key, cache_dir, cfg.pexels_min_width,
                                      len(parts), used=None, require=treq,
                                      is_wild=is_wild, phrase=tq)
                if tpath is not None or tplist:
                    vpath, vraw, vkey, vcredit, plist = tpath, traw, tkey, tcredit, tplist
                    tier, eff_query = "topic-reuse", tq
                    break
        vnorm = vraw * 2
        pbest = plist[0][1] if plist else 0
        vclip, photolist, kind = None, [], "card"
        winner, alt_snip = "none", ""
        if plist and (vpath is None or pbest > vnorm):
            photolist, kind = plist, f"{len(parts)}xphoto"
            for (_, _, k, *_rest) in plist:
                used_pexels.add(k)
            n_photo += len(parts)
            winner = plist[0][2]
            alt_snip = f" alt='{(plist[0][3] if len(plist[0]) > 3 else '')[:60]}'"
            credits += [p[4] for p in plist if len(p) > 4 and p[4]]
        elif vpath is not None:
            vclip, kind = vpath, "video"
            used_pexels.add(vkey)
            n_video += len(parts)
            winner = f"video_{vkey}"
            if vcredit:
                credits.append(vcredit)
        else:
            available_visuals = [c for (c, _, _) in clips if c and c.suffix in {".mp4", ".jpg"}]
            if not available_visuals:
                available_visuals = (list(cache_dir.glob("pexels_*.mp4"))
                                     + list(cache_dir.glob("pexels_*.jpg")))
            if available_visuals:
                vclip = available_visuals[i % len(available_visuals)]
                if vclip.suffix == ".mp4":
                    kind = "video"
                    n_video += len(parts)
                else:
                    photolist = [(vclip, 1, f"reused_{vclip.stem}", "reused visual")]
                    kind = f"{len(parts)}xphoto"
                    n_photo += len(parts)
                winner = f"reused_{vclip.stem}"
                tier = "reused"
            else:
                n_card += len(parts)
                tier = "card"
        via = f"->{eff_query}" if eff_query != query else ""
        log(f"Beat {i + 1} ('{query}'{via}): {kind} tier={tier} winner={winner}{alt_snip} (vscore={vnorm}, pscore={pbest}).")
        for j, part in enumerate(parts):
            clip, seek = None, None
            if vclip is not None:
                clip, seek = vclip, _stock_offset(vclip, j, len(parts))
            elif photolist:
                clip, seek = photolist[j][0] if j < len(photolist) else photolist[0][0], None
            if clip is None:
                available_visuals = [c for (c, _, _) in clips if c and c.suffix in {".mp4", ".jpg"}]
                if not available_visuals:
                    available_visuals = (list(cache_dir.glob("pexels_*.mp4"))
                                         + list(cache_dir.glob("pexels_*.jpg")))
                if available_visuals:
                    clip = available_visuals[still_n % len(available_visuals)]
                    seek = 2.0 * (j + 1) if clip.suffix == ".mp4" else None
                    warn(f"Beat {i + 1}.{j + 1} ('{query}'): no stock, reusing run visual {clip.name}.")
                else:
                    warn(f"Beat {i + 1}.{j + 1} ('{query}'): emergency fallback card.")
                    clip = beat_card(still_n, part, workdir)
            clips.append((clip, seek, i))
            still_n += 1
    log(f"Visuals: {len(clips)} cuts for {len(beats)} beats ({n_video} video, {n_photo} photo, {n_card} card).")
    return clips, sorted(set(credits))


def _clean_board_text(name: str, limit: int = BOARD_NAME_LEN) -> str:
    """Short ASCII-only board label (drawtext uses one font, no emoji)."""
    normalized = (name or "").replace("—", " - ").replace("–", " - ").replace("'", "")
    keep = re.sub(r"[^A-Za-z0-9 .&,!?()\-]", "", normalized)
    keep = re.sub(r"\s+", " ", keep).strip()
    if len(keep) <= limit:
        return keep
    clipped = keep[:limit].rsplit(" ", 1)[0].rstrip()
    return clipped or keep[:limit].rstrip()


def _escape_drawtext(text: str) -> str:
    """Escape a value for drawtext filter syntax (order matters)."""
    return (text.replace("\\", "\\\\").replace(":", "\\:")
                .replace(",", "\\,").replace("'", "'\\''"))


def _board_font_size(text: str, font_path: str) -> int:
    """Shrink only long labels to fit the safe board width."""
    try:
        from PIL import ImageFont
        for size in range(BOARD_FONT_SIZE, BOARD_MIN_FONT_SIZE - 1, -1):
            font = ImageFont.truetype(font_path, size)
            width = font.getlength(text) if hasattr(font, "getlength") else font.getbbox(text)[2]
            if width <= BOARD_MAX_WIDTH:
                return size
    except Exception:
        pass
    return BOARD_MIN_FONT_SIZE


def _fit_board_label(display_name: str, font_path: str) -> tuple[int, str]:
    """(size, text) guaranteed to fit: shrink first, then word-truncate."""
    size = _board_font_size(display_name, font_path)
    text = display_name
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(font_path, size)
        get = (font.getlength if hasattr(font, "getlength")
               else lambda t: font.getbbox(t)[2])
        while get(text) > BOARD_MAX_WIDTH and " " in text.strip():
            text = text.strip().rsplit(" ", 1)[0].rstrip()
    except Exception:
        pass
    return size, text


def board_filter(beats, items, durations, title: str = "") -> str:
    """Persistent leaderboard overlay (trailing comma) or "" when N/A."""
    labeled = [(i, b.get("label", "")) for i, b in enumerate(beats or [])
               if str(b.get("label", "")).isdigit()]
    if not labeled:
        return ""
    font = find_font(list(RANK_FONTS))
    if font is None or not has_ffmpeg_filter("drawtext"):
        return ""
    font = str(font)
    starts: list[float] = []
    t = 0.0
    for d in durations or []:
        starts.append(t)
        try:
            t += max(0.0, float(d))
        except (TypeError, ValueError):
            pass
    total = t if t > 0 else 9999.0
    names = [_clean_board_text(n) for n in (items or [])]
    pairs = list(zip(labeled, names + [""] * len(labeled)))
    ranks = sorted({int(lab) for _, lab in labeled})
    row_of = {r: idx for idx, r in enumerate(ranks)}
    esc_font = font.replace(":", "\\:")
    parts = []
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
        y = BOARD_Y + row_of[r] * BOARD_ROW_H
        parts.append(
            f"drawtext=fontfile='{esc_font}':text='{r}.':"
            f"fontsize={BOARD_FONT_SIZE}:fontcolor=white@0.6:"
            f"borderw=2:bordercolor=black:x={BOARD_X}:y={y}")
    for (beat_idx, lab), name in pairs:
        if not name or beat_idx >= len(starts):
            continue
        r = int(lab)
        y = BOARD_Y + row_of[r] * BOARD_ROW_H
        reveal = starts[beat_idx]
        end = starts[beat_idx + 1] if beat_idx + 1 < len(starts) else total
        display_name = f"{r}. {name}"
        name_font_size, display_name = _fit_board_label(display_name, font)
        esc_name = _escape_drawtext(display_name)
        parts.append(
            f"drawtext=fontfile='{esc_font}':text='{esc_name}':"
            f"fontsize={name_font_size}:fontcolor=white:"
            f"borderw=2:bordercolor=black:x={BOARD_X}:y={y}:"
            f"enable='between(t,{reveal:.2f},{total:.2f})'")
        parts.append(
            f"drawtext=fontfile='{esc_font}':text='{esc_name}':"
            f"fontsize={name_font_size}:fontcolor=red:"
            f"borderw=2:bordercolor=black:x={BOARD_X}:y={y}:"
            f"enable='between(t,{reveal:.2f},{end:.2f})'")
    return ",".join(parts) + ","


def mystery_blur_filter(beats: list[dict], durations: list[float],
                        max_secs: float = 2.0) -> str:
    """boxblur snippet for the #1 beat's reveal window, or "" when N/A."""
    try:
        idx = next(i for i, b in enumerate(beats or [])
                   if str((b or {}).get("label", "")) == "1")
    except StopIteration:
        return ""
    starts: list[float] = []
    t = 0.0
    for d in durations or []:
        starts.append(t)
        try:
            t += max(0.0, float(d))
        except (TypeError, ValueError):
            return ""
    if idx >= len(starts) or idx >= len(durations or []):
        return ""
    try:
        dur = max(0.0, float(durations[idx]))
    except (TypeError, ValueError):
        return ""
    blur_len = min(max_secs, dur / 2)
    if blur_len <= 0.1:
        return ""
    start = starts[idx]
    log(f"Mystery #1 blur: beat {idx + 1} blurred "
        f"{start:.2f}s->{start + blur_len:.2f}s (beat dur {dur:.2f}s), clear after.")
    return f"boxblur=20:5:enable='between(t,{start:.2f},{start + blur_len:.2f})',"


def srt_to_ass(srt: Path, ass: Path) -> Path:
    """Beat SRT -> styled ASS (top-center below title); style in header."""
    raw = srt.read_text(encoding="utf-8-sig")

    def _ts(ts: str) -> str:
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", ts.strip())
        if m is None:
            raise ValueError(f"bad SRT timestamp: {ts!r}")
        h, mi, s, ms = map(int, m.groups())
        return f"{h}:{mi:02d}:{s:02d}.{ms // 10:02d}"

    events = []
    for block in re.split(r"\r?\n\r?\n", raw.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip() != ""]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, _, end = lines[1].partition("-->")
        text = "\\N".join(lines[2:])
        events.append(f"Dialogue: 0,{_ts(start)}, {_ts(end).strip()},Default,,0,0,0,,{text}")
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\n"
        f"PlayResY: {H}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,DejaVu Sans,34,&H0000FFFF,&H000019FF,&HFF000000,"
        "&H00000000,-1,0,0,0,100,100,0,0,1,3,1,8,10,10,320,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    ass.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return ass


def _make_bed(duration: float, cfg: RankingConfig, workdir: Path) -> Path:
    """Loop a pool track at cfg volume, else synth a drone. Never raises."""
    bed = workdir / "bed.mp3"
    pool: list[Path] = []
    for glob_pat in cfg.bed_globs:
        pool += sorted(Path(".").glob(glob_pat))
    pool = [p for p in pool if p.is_file()]
    if pool:
        track = random.choice(pool)
        log(f"Bed: looping custom track {track} (1 of {len(pool)} in pool)")
        return loop_bed(track, duration, bed, volume=cfg.bgm_volume)
    log("Bed: generating layered copyright-safe drone")
    return synth_drone(duration, bed)


def _render_segments(clips: list[tuple], durations: list[float],
                     workdir: Path) -> Path:
    """Normalize cuts to 1080x1920 segments and concat. Returns video_only."""
    cuts_in_beat: dict = {}
    for _c, _, beat_idx in clips:
        cuts_in_beat[beat_idx] = cuts_in_beat.get(beat_idx, 0) + 1
    segs = []
    for i, (c, seek, beat_idx) in enumerate(clips):
        beat_dur = durations[beat_idx] if beat_idx < len(durations) else 2.0
        seg_dur = max(2.0, beat_dur / max(1, cuts_in_beat.get(beat_idx, 1)))
        seg = workdir / f"seg_{i}.mp4"
        if c.suffix == ".jpg":
            frames = max(60, int(seg_dur * 30))
            if i % 2 == 0:
                zb = (f"zoompan=z='min(zoom+0.0015,1.3)':d={frames}:"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}")
            else:
                zb = (f"zoompan=z='if(eq(on,1),1.3,max(zoom-0.0015,1.0))':d={frames}:"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}")
            run_ffmpeg(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(c),
                        "-vf", f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
                               f"crop={W * 2}:{H * 2},{zb},fps=30",
                        "-t", str(seg_dur), "-pix_fmt", "yuv420p", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "20", "-maxrate", "12M",
                        "-bufsize", "24M", str(seg)], f"segment-{i}-kenburns")
        elif c.suffix == ".png":
            frames = max(60, int(seg_dur * 30))
            if i % 2 == 0:
                zb = (f"zoompan=z='min(zoom+0.0006,1.06)':d={frames}:"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}")
            else:
                zb = (f"zoompan=z='if(eq(on,1),1.06,max(zoom-0.0006,1.0))':d={frames}:"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}")
            run_ffmpeg(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(c),
                        "-t", str(seg_dur),
                        "-vf", f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
                               f"crop={W * 2}:{H * 2},{zb},fps=30",
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20", "-maxrate", "12M", "-bufsize", "24M",
                        str(seg)], f"segment-{i}-slide")
        else:
            cmd = ["ffmpeg", "-y"]
            if seek:
                cmd += ["-ss", str(seek)]
            cmd += ["-stream_loop", "-1", "-i", str(c),
                    "-t", str(seg_dur),
                    "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-maxrate", "12M", "-bufsize", "24M",
                    "-an", str(seg)]
            run_ffmpeg(cmd, f"segment-{i}-clip")
        segs.append(seg)
    concat = workdir / "concat.txt"
    concat.write_text("".join(f"file '{s.resolve()}'\n" for s in segs))
    video_only = workdir / "video_only.mp4"
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat), "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-maxrate", "12M", "-bufsize", "24M",
                "-pix_fmt", "yuv420p", str(video_only)], "concat")
    return video_only


def _mux_cmd(video_only: Path, audio: Path, bed: Path, out_path: Path,
             blur_s: str, board_s: str, subs_s) -> list[str]:
    """Final mux command (blur -> board -> subs -> yuv420p; amix audio)."""
    v = (f"[0:v]{blur_s}{board_s}{subs_s},format=yuv420p[vout]" if subs_s
         else f"[0:v]{blur_s}{board_s}format=yuv420p[vout]")
    achain = ("[1:a][2:a]amix=inputs=2:duration=first:"
              "dropout_transition=0:normalize=0[aout]")
    return ["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio), "-i", str(bed),
            "-filter_complex", f"{v};{achain}",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-maxrate", "12M", "-bufsize", "24M", "-c:a", "aac", "-b:a", "128k",
            "-ar", "44100", "-movflags", "+faststart", "-shortest", str(out_path)]


def _mux_with_fallbacks(video_only: Path, audio: Path, bed: Path,
                        out_path: Path, blur: str, board: str, subs) -> None:
    """Mux; degrade blur -> board -> subs in that order on filter errors."""
    try:
        subprocess.run(_mux_cmd(video_only, audio, bed, out_path, blur, board, subs),
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if blur and "boxblur" in stderr:
            warn("mystery #1 blur filter failed, rendering without blur.")
            blur = ""
            try:
                run_ffmpeg(_mux_cmd(video_only, audio, bed, out_path, blur, board, subs),
                           "mux-no-blur")
            except subprocess.CalledProcessError as e_nb:
                e = e_nb
                stderr = e_nb.stderr or ""
            else:
                log(f"Rendered {out_path} ({out_path.stat().st_size // 1024} KB)")
                return
        board_failed = bool(board and ("drawtext" in stderr or "No such filter" in stderr))
        subs_failed = bool(subs and "subtitles" in stderr)
        if board_failed:
            warn("niche overlay filter failed, rendering without it.")
            try:
                run_ffmpeg(_mux_cmd(video_only, audio, bed, out_path, blur, "", subs),
                           "mux-no-board")
            except subprocess.CalledProcessError:
                if not subs_failed:
                    raise
                warn("subtitles filter also failed, rendering plain.")
                run_ffmpeg(_mux_cmd(video_only, audio, bed, out_path, blur, "", None),
                           "mux-plain")
        elif subs_failed:
            warn("subtitles filter failed, rendering without burned captions.")
            try:
                run_ffmpeg(_mux_cmd(video_only, audio, bed, out_path, blur, board, None),
                           "mux-no-subs")
            except subprocess.CalledProcessError as e2:
                stderr2 = e2.stderr or ""
                if not (board and ("drawtext" in stderr2 or "No such filter" in stderr2)):
                    raise
                warn("niche overlay filter also failed, rendering without it.")
                try:
                    run_ffmpeg(_mux_cmd(video_only, audio, bed, out_path, blur, "", subs),
                               "mux-no-board")
                except subprocess.CalledProcessError:
                    warn("subtitles filter also failed, rendering plain.")
                    run_ffmpeg(_mux_cmd(video_only, audio, bed, out_path, blur, "", None),
                               "mux-plain")
        else:
            tail = (e.stderr or "").strip().splitlines()[-20:]
            log(f"ffmpeg failed at mux (exit {e.returncode}):")
            for line in tail:
                log(f"  {line}")
            raise
    log(f"Rendered {out_path} ({out_path.stat().st_size // 1024} KB)")


def build_visuals(content: Content, narration: NarrationResult,
                  cfg: RankingConfig, workdir: Path) -> VisualsResult:
    """Tiered stock per beat -> segments -> mux with board/blur/captions."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    beats = [dict(b) for b in (content.extras.get("beats") or [])]
    if not beats:
        raise ValueError("ranking build_visuals needs 7 beats in content.extras")
    items = list(content.extras.get("items") or [])
    topic = content.extras.get("topic", "") or ""
    durations = narration.extras.get("beat_durations")
    if not durations:
        weights = [max(1, len(b.get("text", "").split())) for b in beats]
        durations = [narration.duration * w / sum(weights) for w in weights]
    clips, credits = visuals_for_beats(beats, cfg, workdir, items, topic)
    video_only = _render_segments(clips, durations, workdir)
    bed = _make_bed(narration.duration, cfg, workdir)
    src_srt = Path(narration.extras.get("srt") or "")
    out_srt = workdir / "captions.srt"
    subs = None
    if src_srt.exists():
        out_srt.write_bytes(src_srt.read_bytes())
        try:
            ass = srt_to_ass(out_srt, workdir / "captions.ass")
            subs = subtitles_filter(ass.resolve())
        except Exception as e:
            warn(f"ASS conversion failed ({e}), burning raw SRT.")
            subs = subtitles_filter(out_srt.resolve())
    else:
        out_srt = None  # type: ignore[assignment]
    board = board_filter(beats, items, durations, content.title)
    blur = mystery_blur_filter(beats, durations, cfg.mystery_blur_max)
    out_mp4 = workdir / "short.mp4"
    _mux_with_fallbacks(video_only, narration.audio, bed, out_mp4, blur, board, subs)
    used_ids = sorted({c.stem for (c, _, _) in clips if c and c.suffix in {".mp4", ".jpg"}})
    return VisualsResult(mp4=out_mp4, srt=out_srt, credits=credits,
                         used_ids=used_ids,
                         extras={"beats": len(beats), "cuts": len(clips),
                                 "topic": topic, "bed": bed.name})
