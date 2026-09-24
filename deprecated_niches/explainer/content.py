"""Explainer stage 1: topics, prompt, Gemini script, offline fallback.

Moved verbatim from niche_explainer.py; core imports repointed to
engine/* + local helpers. generate_content() wraps the full
draft loop (Gemini -> OpenRouter -> fallback) into a Content.
"""

import json
import os
import random
import re
import time

from engine.ffmpeg_utils import log, warn
from engine.gemini_client import (DEFAULT_SAFETY_SETTINGS, GeminiClient,
                                  parse_keys)
from niches.base import Content, NicheConfig

MODE = "explainer"
NICHE_LABEL = "how things actually work"

# Local mirrors of core history windows (same defaults; cfg may narrow).
DEDUP_WINDOW = 110
HISTORY_PROMPT_N = 20
SIMILARITY_RETRIES = 2

# ---------------------------------------------------------------------------
# Topic pool: evergreen explainer topics across categories. One is drawn per
# run and forced into the prompt; history.json excludes recent ones.
# Rules for adding more: must explain ONE clear concept, be filmable with
# stock footage + diagrams, be understandable without technical knowledge,
# and avoid near-dupes of existing entries.
# ---------------------------------------------------------------------------
TOPICS = (
    # Technology
    "How does Face ID work",
    "How does GPS know where you are",
    "How does a touchscreen detect your finger",
    "How does Bluetooth connect devices",
    "How does a QR code store information",
    "How does noise canceling work in headphones",
    "How does wireless charging work",
    "How does a fingerprint scanner work",
    "How does a smart speaker understand you",
    "How does autocorrect predict words",
    # Internet & Computing
    "What happens when you open a website",
    "How does WiFi send data through the air",
    "How does HTTPS keep your data safe",
    "How does email travel across the world",
    "How does video streaming work",
    "How does a search engine find results",
    "How does cloud storage work",
    "How does two-factor authentication protect you",
    "How does a VPN hide your location",
    "How does an ad know what you searched for",
    # Science & Physics
    "Why does a microwave heat food",
    "How does a magnet stick to metal",
    "How does a battery store energy",
    "How does a solar panel turn light into electricity",
    "How does nuclear power generate electricity",
    "Why does ice float on water",
    "How does a laser work",
    "How does sound travel through walls",
    "Why does the sky change color at sunset",
    "How does a mirror reflect your image",
    # Biology & Health
    "How does your immune system fight a cold",
    "Why do you need sleep",
    "How do your muscles grow stronger",
    "How does your brain feel pain",
    "How does your tongue taste different flavors",
    "How does caffeine wake you up",
    "How does sunscreen protect your skin",
    "How does a vaccine teach your body to fight",
    "Why does your body get a fever",
    "How does your eye focus on objects",
    # Engineering & Everyday Objects
    "How does an elevator know which floor to go to",
    "How does a lock and key work",
    "How does air conditioning cool a room",
    "How does a refrigerator keep food cold",
    "How does a zipper work",
    "How does a toilet flush",
    "How does a smoke detector sense fire",
    "How does a thermostat control temperature",
    "How does a water filter clean water",
    "How does a 3D printer build objects",
    # Transportation
    "Why does an airplane stay in the air",
    "How does a submarine dive and surface",
    "How do electric cars charge",
    "How do anti-lock brakes stop a car",
    "How does a traffic light know when to change",
    "How does cruise control keep a steady speed",
    "How does a jet engine produce thrust",
    "How does a hybrid car switch between engines",
    "How does a self-driving car see the road",
    "How does a parachute slow you down",
    # Infrastructure
    "How does tap water get to your house",
    "How does the power grid deliver electricity",
    "How does a cell tower connect your call",
    "How does fiber optic cable carry the internet",
    "How does a sewage treatment plant clean water",
    "How does a wind turbine generate power",
    "How does an undersea cable connect continents",
    "How does a dam generate electricity",
    # Finance & Commerce
    "How does contactless payment work",
    "How does an ATM verify your card",
    "How does a credit score get calculated",
    "How does online banking keep your money safe",
    "How does cryptocurrency mining work",
    "How does a vending machine detect fake coins",
    # Food & Chemistry
    "Why does bread rise when you bake it",
    "How does carbonation put bubbles in drinks",
    "How does pasteurization make milk safe",
    "Why does salt preserve food",
    "How does a pressure cooker cook faster",
    "Why does popcorn pop",
    # AI & Modern Tech
    "How does RAG help AI answer questions",
    "How does image recognition identify objects",
    "How does a recommendation algorithm choose content",
    "How does speech-to-text convert your voice",
    "How does facial recognition find a match",
)

# Categories for cross-vertical rotation (same concept as ranking niche).
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("technology", ("face id", "gps", "touchscreen", "bluetooth", "qr code",
                    "noise cancel", "wireless charg", "fingerprint", "smart speaker",
                    "autocorrect")),
    ("internet", ("website", "wifi", "https", "email", "streaming", "search engine",
                  "cloud", "two-factor", "vpn", "ad ")),
    ("science", ("microwave", "magnet", "battery", "solar panel", "nuclear",
                 "ice float", "laser", "sound", "sunset", "mirror")),
    ("biology", ("immune", "sleep", "muscle", "pain", "taste", "caffeine",
                 "sunscreen", "vaccine", "fever", "eye focus")),
    ("engineering", ("elevator", "lock and key", "air condition", "refrigerator",
                     "zipper", "toilet", "smoke detector", "thermostat",
                     "water filter", "3d printer")),
    ("transport", ("airplane", "submarine", "electric car", "brake", "traffic light",
                   "cruise control", "jet engine", "hybrid car", "self-driving",
                   "parachute")),
    ("infrastructure", ("tap water", "power grid", "cell tower", "fiber optic",
                        "sewage", "wind turbine", "undersea cable", "dam")),
    ("finance", ("contactless", "atm", "credit score", "banking", "cryptocurrency",
                 "vending machine")),
    ("food", ("bread", "carbonation", "pasteurization", "salt preserve",
              "pressure cooker", "popcorn")),
    ("ai", ("rag ", "image recognition", "recommendation", "speech-to-text",
            "facial recognition")),
)


def _topic_category(topic: str) -> str:
    """Category lane for a topic ("" when unclassifiable)."""
    t = str(topic or "").lower()
    for cat, keywords in _CATEGORY_KEYWORDS:
        if any(k in t for k in keywords):
            return cat
    return ""


# ---------------------------------------------------------------------------
# History avoidance
# ---------------------------------------------------------------------------
AVOID_INTRO = ("Already-used explainer topics — do NOT reuse any of these topics "
               "or titles:\n")
AVOID_OUTRO = ("Your topic must be DIFFERENT from every entry above. ")


def format_history_line(h: dict) -> str:
    return f"- \"{h.get('title', '?')}\" (topic={h.get('topic', '?')})"


# Stock policy: each cut searches the beat's OWN query (scene-specific).
USE_BEAT_QUERY = True
AMBIENT_QUERIES = ()
USE_MOOD = False


# ---------------------------------------------------------------------------
# AI topic proposal (mirrors ranking niche's propose_topic)
# ---------------------------------------------------------------------------
AI_TOPICS = os.getenv("AI_TOPICS", "on").strip().lower() not in (
    "0", "off", "false", "no")
AI_TOPIC_TRIES = 2

_model_order: list[str] = []  # working model floats to front (per process)


def _mark_model_working(model: str) -> None:
    """Move working model first so later calls skip dead models."""
    if model in _model_order and _model_order[0] != model:
        _model_order.remove(model)
        _model_order.insert(0, model)


def _recent_topic_names(history: list[dict], window: int = DEDUP_WINDOW) -> set:
    """Casefolded topics from the full enforced history window (same niche)."""
    return {str(h.get("topic", "")).casefold()
            for h in (history or [])[-window:]
            if h.get("niche", "") == MODE}


def _pick_avoiding(pool: tuple, recent: set,
                   history: list[dict] | None = None):
    """Pool pick excluding recent values; LRU fallback on exhaustion."""
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
        def _recency(v) -> int:
            return last_seen.get(str(v).casefold(), -1)
        oldest = min(_recency(v) for v in pool)
        return random.choice([v for v in pool if _recency(v) == oldest])
    return random.choice(pool)


def history_block(history: list[dict], n: int = HISTORY_PROMPT_N) -> str:
    """Prompt section listing recent same-niche entries Gemini must avoid."""
    recent = [h for h in (history or [])[-n:] if h.get("niche", "") == MODE]
    if not recent:
        return ""
    return AVOID_INTRO + "\n".join(format_history_line(h) for h in recent) + "\n" + AVOID_OUTRO


def propose_topic(history: list[dict], cfg: NicheConfig | None = None) -> str | None:
    """Ask Gemini for one fresh explainer topic. Returns topic or None."""
    ai_on = getattr(cfg, "ai_topics", AI_TOPICS) if cfg else AI_TOPICS
    tries = getattr(cfg, "ai_topic_tries", AI_TOPIC_TRIES) if cfg else AI_TOPIC_TRIES
    keys = parse_keys()
    if not ai_on or not keys:
        return None
    models = list(getattr(cfg, "gemini_models", []) or [])
    if cfg is None or not models:
        global _model_order
        if not _model_order:
            _model_order = [m.strip() for m in
                            os.getenv("GEMINI_MODELS",
                                      "gemini-3.8-flash,gemini-3.7-flash").split(",")
                            if m.strip()]
        models = list(_model_order)
    timeout = getattr(cfg, "gemini_timeout", 40) if cfg else 40
    window = getattr(cfg, "dedup_window", DEDUP_WINDOW) if cfg else DEDUP_WINDOW
    prompt_n = getattr(cfg, "history_prompt_n", HISTORY_PROMPT_N) if cfg else HISTORY_PROMPT_N
    recent = _recent_topic_names(history, window)
    same_niche = [h for h in (history or []) if h.get("niche", "") == MODE]
    excluded_cats = {_topic_category(h.get("topic", "")) for h in same_niche[-2:]}
    excluded_cats.discard("")
    avoid = "".join(f"- {h.get('topic', '?')}\n"
                    for h in (history or [])[-prompt_n:]
                    if h.get("niche", "") == MODE)
    cat_rule = (f" Rotate categories: do NOT propose these recently aired lanes: "
                f"{sorted(excluded_cats) or '(none yet)'}. "
                if excluded_cats else " ")
    prompt = (
        "Propose ONE explainer YouTube Shorts topic about how something in the "
        "real world works. Reply with ONLY valid JSON: {\"topic\": <question phrase>}. "
        "Rules: the topic must be a clear 'How does X work?' or 'Why does X happen?' "
        "question; it must be explainable in 80-85 seconds to a general audience; "
        "the concept must be filmable with stock footage and simple diagrams; "
        "no people names, no current events, no brand-specific topics. "
        "Good examples: 'How does GPS know where you are', "
        "'Why does a microwave heat food', 'How does an elevator know which floor to go to'. "
        f"{cat_rule}"
        f"Do NOT reuse any of these recent topics:\n{avoid or '(none yet)'}\n"
        "Output only the JSON object."
    )
    client = GeminiClient(keys, timeout=timeout)
    for round_no in range(1, tries + 1):
        data = None
        for model in models:
            try:
                data = client.generate_json(
                    model, prompt,
                    generation_config={"temperature": 0.8,
                                       "maxOutputTokens": 512})
                _mark_model_working(model)
                break
            except Exception as e:
                log(f"AI topic model {model} unusable ({e}), trying next...")
                time.sleep(2)
        if not isinstance(data, dict):
            continue
        topic = str(data.get("topic", "")).strip()
        problems = []
        if not (10 <= len(topic) <= 80):
            problems.append("bad topic length")
        if topic.casefold() in {t.casefold() for t in recent}:
            problems.append("topic already used")
        if excluded_cats and _topic_category(topic) in excluded_cats:
            problems.append(f"category '{_topic_category(topic)}' aired recently")
        # Must be a question-style topic
        topic_l = topic.lower()
        if not any(topic_l.startswith(q) for q in ("how ", "why ", "what ")):
            problems.append("not a how/why/what question")
        if problems:
            log(f"AI topic rejected ({'; '.join(problems)}), "
                f"round {round_no}/{tries}.")
            continue
        log(f"AI topic accepted: {topic}")
        return topic
    return None


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------
def build_prompt(history: list[dict], cfg: NicheConfig | None = None):
    """Gemini prompt for an explainer Short (~60-75s). Returns (prompt, slots)."""
    history = history or []
    window = getattr(cfg, "dedup_window", DEDUP_WINDOW) if cfg else DEDUP_WINDOW
    recent_topics = _recent_topic_names(history, window)
    topic = propose_topic(history, cfg)
    source = "ai" if topic else "pool"
    if topic is None:
        topic = _pick_avoiding(TOPICS, recent_topics, history)
    slots = {"topic": topic, "topic_source": source}
    category = _topic_category(topic)
    slots["category"] = category
    log(f"Explainer topic ({source}): {topic}")
    prompt = (
        f"Write a YouTube Shorts explainer script that answers: \"{topic}?\"\n\n"
        "This is a short, clear explanation of ONE real-world concept for a general audience. "
        "No technical jargon. No filler. No unsupported claims.\n\n"
        f"{history_block(history)}"
        "Return ONLY valid JSON with exactly these keys: title, narration, description, tags, beats.\n\n"
        "Title: must be a clear question, starting with 'How', 'Why', or 'What'. "
        "Example: 'How Does Face ID Work?' or 'Why Does a Microwave Heat Food?'. "
        "Keep it under 60 characters. One emoji allowed at the start or end only.\n\n"
        "Narration: exactly 195-205 words (~80-85 seconds spoken at ~145 WPM). "
        "Present tense, conversational, fifth-grade reading level. "
        "Short punchy sentences. One clear idea per sentence. No emojis in the spoken text. "
        "Structure: "
        "(1) A 1-2 sentence hook that makes the viewer curious — state something surprising "
        "about the concept or ask a relatable question. "
        "(2) 4-5 clear explanation steps, each building on the last — walk through HOW the "
        "thing actually works, step by step. Use analogies and comparisons to everyday things. "
        "(3) A 1-2 sentence closer with a satisfying 'so that's why...' conclusion.\n\n"
        "Description: exactly 2 sentences with 1-2 fitting emojis and 3 hashtags.\n\n"
        "Tags: array of exactly 5 plain-ASCII strings, no emoji.\n\n"
        "Beats: array of 6-7 objects in video order (hook, 4-5 explanation steps, closer). "
        "Each object must be:\n"
        "{\n"
        '  "text": <exact narration chunk for this scene>,\n'
        '  "query": <clean stock-footage search query, 1-3 words, concrete and filmable>,\n'
        '  "visual_hint": {\n'
        '    "type": <one of: "stock_footage", "diagram", "process_flow", "text_card">,\n'
        '    "description": <brief description of what should be shown on screen>,\n'
        '    "components": [<optional list of visual elements like "phone", "server", "arrow">],\n'
        '    "label": <optional text label to display on the diagram>\n'
        '  }\n'
        "}\n\n"
        'Visual hint types:\n'
        '- "stock_footage": real-world footage (person using phone, water flowing, car driving)\n'
        '- "diagram": a dynamic generative AI drawing. Can be any crazy 2D graphic: fractals, particle systems, waveforms, physics simulations, node graphs, 3D wireframes, scatter plots, molecular vibrations, etc. Feel free to describe extremely complex math or geometric animations! This will be coded by an AI using Python Pillow.\n'
        '- "process_flow": sequential steps with arrows showing a process\n'
        '- "text_card": key fact or definition displayed as styled text\n\n'
        "Rules:\n"
        "1. The beat texts joined in order must equal the full narration exactly.\n"
        "2. Each beat's query should match what is being explained in that moment.\n"
        "3. Visual hints for 'diagram' should be incredibly detailed and describe complex, aesthetic math/physics/geometric layouts to showcase the AI's rendering capabilities.\n"
        "4. Use stock_footage for real-world scenes, diagrams for technical concepts.\n"
        "5. The explanation must be accurate and not oversimplified to the point of being wrong.\n\n"
        "Do not include markdown, notes, or any extra keys. Output only the JSON object."
    )
    return prompt, slots


# ---------------------------------------------------------------------------
# Offline fallback scripts (3 variants, like ranking niche)
# ---------------------------------------------------------------------------
_FALLBACK_VARIANTS = (
    {
        "topic": "What happens when you open a website",
        "title": "🌐 What Happens When You Open a Website?",
        "description": "🌐 Ever wonder what happens in the split second after you type a URL? "
                       "Here's the journey your request takes. #shorts #internet #explained",
        "tags": ["shorts", "internet", "howthingswork", "explained", "technology"],
        "narration": (
            "Every time you type a website address, something incredible happens in less than a second. "
            "Your browser does not actually know where that website lives. "
            "So it asks a special server called a DNS server, which works like a phone book for the internet. "
            "It translates the human-readable name you typed into a unique number address, called an IP address. "
            "Without this step, your browser would have no idea where to go. "
            "Next, your request travels through cables, sometimes stretching thousands of miles under the ocean, "
            "to reach the server that holds the website. That server reads your request and sends back all the "
            "pieces: the text, the images, the layout instructions. "
            "Your browser then collects all those pieces and assembles them into the page you see. "
            "It reads the layout code to know where every element goes, downloads the images one by one, "
            "and applies the styling rules. "
            "All of this happens in about 200 milliseconds — faster than the blink of an eye. "
            "So the next time a page loads instantly, remember: your request just traveled halfway "
            "around the world and back, all before you noticed."
        ),
        "beats": [
            {"text": "Every time you type a website address, something incredible happens in less than a second. "
                     "Your browser does not actually know where that website lives.",
             "query": "person typing laptop",
             "visual_hint": {"type": "stock_footage",
                             "description": "Person typing a URL into a browser"}},
            {"text": "So it asks a special server called a DNS server, which works like a phone book for the internet. "
                     "It translates the human-readable name you typed into a unique number address, called an IP address. "
                     "Without this step, your browser would have no idea where to go.",
             "query": "server room data center",
             "visual_hint": {"type": "process_flow",
                             "description": "Browser sends request to DNS server, gets IP address back",
                             "components": ["browser", "arrow", "dns_server", "arrow", "ip_address"],
                             "label": "DNS Lookup"}},
            {"text": "Next, your request travels through cables, sometimes stretching thousands of miles under the ocean, "
                     "to reach the server that holds the website. That server reads your request and sends back all the "
                     "pieces: the text, the images, the layout instructions.",
             "query": "undersea internet cable",
             "visual_hint": {"type": "diagram",
                             "description": "Animated data packet traveling through undersea fiber cables to a web server",
                             "components": ["computer", "cable", "ocean", "server"],
                             "label": "Data Request"}},
            {"text": "Your browser then collects all those pieces and assembles them into the page you see. "
                     "It reads the layout code to know where every element goes, downloads the images one by one, "
                     "and applies the styling rules.",
             "query": "website loading browser",
             "visual_hint": {"type": "process_flow",
                             "description": "Browser assembling HTML, CSS, and images into a webpage",
                             "components": ["html_code", "css_style", "images", "arrow", "webpage"],
                             "label": "Page Rendering"}},
            {"text": "All of this happens in about 200 milliseconds — faster than the blink of an eye.",
             "query": "fast speed light",
             "visual_hint": {"type": "text_card",
                             "description": "200ms — faster than a blink",
                             "label": "200 Milliseconds"}},
            {"text": "So the next time a page loads instantly, remember: your request just traveled halfway "
                     "around the world and back, all before you noticed.",
             "query": "earth globe network",
             "visual_hint": {"type": "stock_footage",
                             "description": "Globe with network connections lighting up"}},
        ],
    },
    {
        "topic": "How does RAG help AI answer questions",
        "title": "🤖 How Does RAG Help AI Answer Questions?",
        "description": "🤖 RAG is the secret behind AI answers that actually cite sources. "
                       "Here's how it works in seconds. #shorts #ai #explained",
        "tags": ["shorts", "ai", "rag", "explained", "technology"],
        "narration": (
            "When you ask an AI a question, it normally guesses from what it memorized during training. "
            "But what if you need a real, verified answer? That is where RAG comes in. "
            "RAG stands for Retrieval Augmented Generation. "
            "Instead of relying only on memory, the AI first searches a database of real documents, "
            "like a student checking their notes before answering a test question. "
            "It scores thousands of passages in milliseconds and pulls the most relevant ones to the top. "
            "It finds the best matching passages and reads them carefully. "
            "Then it uses those passages as context to write an answer grounded in actual information. "
            "The key difference is that the AI is not inventing facts. It is drawing directly from real sources, "
            "which means you can check and verify exactly what it says. "
            "Think of it like this: a regular AI answers from memory alone, "
            "but a RAG-powered AI answers with its textbook open right in front of it. "
            "That is why RAG-powered answers tend to be far more accurate, more detailed, and far more trustworthy."
        ),
        "beats": [
            {"text": "When you ask an AI a question, it normally guesses from what it memorized during training. "
                     "But what if you need a real, verified answer? That is where RAG comes in.",
             "query": "artificial intelligence chatbot",
             "visual_hint": {"type": "text_card",
                             "description": "Title: What is RAG?",
                             "label": "Retrieval Augmented Generation"}},
            {"text": "RAG stands for Retrieval Augmented Generation. "
                     "Instead of relying only on memory, the AI first searches a database of real documents, "
                     "like a student checking their notes before answering a test question.",
             "query": "database search documents",
             "visual_hint": {"type": "process_flow",
                             "description": "Query goes to document database, retrieves relevant passages",
                             "components": ["question", "arrow", "database", "arrow", "documents"],
                             "label": "Step 1: Retrieve"}},
            {"text": "It scores thousands of passages in milliseconds and pulls the most relevant ones to the top. "
                     "It finds the best matching passages and reads them carefully.",
             "query": "data ranking search results",
             "visual_hint": {"type": "diagram",
                             "description": "Animated ranking bars of scored document passages being sorted in real time",
                             "components": ["passages", "score_bars", "arrow", "top_results"],
                             "label": "Relevance Scoring"}},
            {"text": "Then it uses those passages as context to write an answer grounded in actual information. "
                     "The key difference is that the AI is not inventing facts.",
             "query": "ai processing data",
             "visual_hint": {"type": "diagram",
                             "description": "AI model receiving retrieved documents as context input, generating a verified answer",
                             "components": ["documents", "arrow", "ai_model", "arrow", "answer"],
                             "label": "Step 2: Generate"}},
            {"text": "It is drawing directly from real sources, which means you can check and verify exactly what it says.",
             "query": "verified checkmark document",
             "visual_hint": {"type": "text_card",
                             "description": "Key difference: answers grounded in real sources",
                             "label": "Verifiable Answers"}},
            {"text": "Think of it like this: a regular AI answers from memory alone, "
                     "but a RAG-powered AI answers with its textbook open right in front of it. "
                     "That is why RAG-powered answers tend to be far more accurate, more detailed, and far more trustworthy.",
             "query": "student reading textbook",
             "visual_hint": {"type": "stock_footage",
                             "description": "Student studying with an open textbook"}},
        ],
    },
    {
        "topic": "Why does a microwave heat food",
        "title": "🔥 Why Does a Microwave Heat Food?",
        "description": "🔥 Your microwave doesn't cook with heat — it moves molecules. "
                       "Here's the science in 60 seconds. #shorts #science #explained",
        "tags": ["shorts", "science", "microwave", "explained", "howthingswork"],
        "narration": (
            "A microwave does not actually heat your food the way an oven does. "
            "It does something completely different, and the science behind it is fascinating. "
            "Inside every microwave, there is a device called a magnetron. "
            "It converts electrical energy into invisible waves called microwaves, "
            "which are a form of electromagnetic radiation, sitting between radio waves and infrared light on the spectrum. "
            "These waves travel through your food and specifically target water molecules. "
            "Water molecules are not perfectly balanced. They have a positive end and a negative end, just like a tiny magnet. "
            "When the alternating microwave field hits them, the molecules start frantically flipping back and forth, "
            "billions of times per second. "
            "All that rapid movement creates friction between the molecules. "
            "And friction generates heat. That is how your food gets hot from the inside out, not from the outside in. "
            "This is also why dry foods like crackers barely warm up in a microwave: almost no water molecules to spin. "
            "And it explains why the plate stays cool while the food gets scalding hot. "
            "So your microwave is not really cooking at all. It is just shaking water molecules until they heat themselves."
        ),
        "beats": [
            {"text": "A microwave does not actually heat your food the way an oven does. "
                     "It does something completely different, and the science behind it is fascinating.",
             "query": "microwave oven kitchen",
             "visual_hint": {"type": "stock_footage",
                             "description": "Person putting food in a microwave oven"}},
            {"text": "Inside every microwave, there is a device called a magnetron. "
                     "It converts electrical energy into invisible waves called microwaves, "
                     "which are a form of electromagnetic radiation, sitting between radio waves and infrared light on the spectrum.",
             "query": "electromagnetic waves spectrum",
             "visual_hint": {"type": "diagram",
                             "description": "Electromagnetic spectrum showing microwave band between radio and infrared, with animated wave patterns",
                             "components": ["spectrum_bar", "microwave_zone", "magnetron", "waves"],
                             "label": "Magnetron → Microwaves"}},
            {"text": "These waves travel through your food and specifically target water molecules. "
                     "Water molecules are not perfectly balanced. They have a positive end and a negative end, just like a tiny magnet.",
             "query": "water molecule model",
             "visual_hint": {"type": "diagram",
                             "description": "Zoomed-in animated diagram of a water molecule with positive and negative poles labeled, sitting in a microwave field",
                             "components": ["water_molecule", "positive_pole", "negative_pole", "field_arrows"],
                             "label": "Polar Molecule"}},
            {"text": "When the alternating microwave field hits them, the molecules start frantically flipping back and forth, "
                     "billions of times per second. All that rapid movement creates friction between the molecules.",
             "query": "molecular vibration physics",
             "visual_hint": {"type": "diagram",
                             "description": "Dozens of water molecules spinning and vibrating chaotically, collision sparks between them",
                             "components": ["molecules", "rotation_arrows", "friction_sparks"],
                             "label": "Molecular Friction"}},
            {"text": "And friction generates heat. That is how your food gets hot from the inside out, not from the outside in. "
                     "This is also why dry foods like crackers barely warm up: almost no water molecules to spin.",
             "query": "hot food steam",
             "visual_hint": {"type": "text_card",
                             "description": "Spinning → Friction → Heat (inside-out)",
                             "label": "Friction = Heat"}},
            {"text": "So your microwave is not really cooking at all. It is just shaking water molecules until they heat themselves.",
             "query": "microwave oven close up",
             "visual_hint": {"type": "stock_footage",
                             "description": "Close-up of food rotating inside a microwave"}},
        ],
    },
)


def _build_fallback_meta(variant: dict) -> tuple[dict, dict]:
    """Assemble a (meta, slots) pair from one fallback variant dict."""
    slots = {"topic": variant["topic"],
             "topic_source": "fallback",
             "category": _topic_category(variant["topic"])}
    return {
        "title": variant["title"],
        "narration": variant["narration"],
        "description": variant["description"],
        "tags": list(variant["tags"]),
        "beats": list(variant["beats"]),
    }, slots


def fallback_script(history: list[dict] | None = None) -> tuple[dict, dict]:
    """Offline explainer script. Least-recently-aired variant wins."""
    history = history or []

    def _last_pos(topic_cf: str) -> int:
        pos = -1
        for i, h in enumerate(history):
            if (h.get("niche", "") == MODE
                    and str(h.get("topic", "")).casefold() == topic_cf):
                pos = i
        return pos

    ranked = sorted(_FALLBACK_VARIANTS,
                    key=lambda v: _last_pos(v["topic"].casefold()))
    last = history[-1] if history else {}
    last_topic = str(last.get("topic", "")).casefold()
    chosen = ranked[0]
    for variant in ranked:
        if variant["topic"].casefold() != last_topic:
            chosen = variant
            break
    log(f"Offline fallback: '{chosen['topic']}'.")
    return _build_fallback_meta(chosen)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_extra(data: dict) -> dict:
    """Validate visual_hint structures in beats. Never fatal — strips invalid hints."""
    beats = data.get("beats")
    if not isinstance(beats, list):
        return {}
    valid_types = {"stock_footage", "diagram", "process_flow", "text_card"}
    for b in beats:
        if not isinstance(b, dict):
            continue
        hint = b.get("visual_hint")
        if not isinstance(hint, dict):
            continue
        # Normalize type to known values; unknown → stock_footage fallback
        vtype = str(hint.get("type", "")).strip().lower().replace(" ", "_")
        # Accept common variations
        if vtype in ("svg_diagram", "svg"):
            vtype = "diagram"
        if vtype not in valid_types:
            vtype = "stock_footage"
        hint["type"] = vtype
        # Ensure description exists
        if not isinstance(hint.get("description"), str):
            hint["description"] = ""
        # Normalize components to a list of strings
        comps = hint.get("components")
        if isinstance(comps, list):
            hint["components"] = [str(c).strip() for c in comps if str(c).strip()]
        else:
            hint["components"] = []
        # Normalize label
        if not isinstance(hint.get("label"), str):
            hint["label"] = ""
    return {}


def _validate_script(data: dict) -> dict:
    """Strict-check script JSON. Raises ValueError when unusable."""
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
    if nwords < 120:  # explainer floor (~60-75s at ~145 WPM)
        raise ValueError(f"'narration' too short ({nwords} words, need 120+)")
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
        norm_b = {"text": t.strip(), "query": q.strip(), "label": ""}
        if isinstance(b.get("visual_hint"), dict):
            norm_b["visual_hint"] = b["visual_hint"]
        norm_beats.append(norm_b)
    bwords = sum(len(b["text"].split()) for b in norm_beats)
    if bwords < 120:
        raise ValueError(f"'beats' cover too little text ({bwords} words, need 120+)")
    out = {
        "title": title.strip()[:100],
        "narration": narration.strip(),
        "description": description.strip(),
        "tags": [t.strip() for t in tags if t.strip()][:10],
        "beats": norm_beats,
    }
    out.update(validate_extra(data))
    return out


def sanitize_title(title: str, slots: dict | None = None) -> str:
    """Enforce 'How/Why/What' question title format. Never raises."""
    t = (title or "").strip()
    # Strip hashtags if present
    t = re.sub(r"#\S+", "", t).strip()
    # Strip trailing punctuation-noise
    t = re.sub(r"\s*[–—\-:|;,.]+\s*$", "", t).strip()
    # Check if it's already a good question title
    low = t.casefold()
    # Remove leading emoji for format check
    text_only = re.sub(r"^[^\w]+", "", t).strip()
    text_low = text_only.lower()
    is_question = any(text_low.startswith(q) for q in ("how ", "why ", "what "))
    if is_question and 12 <= len(t) <= 100:
        # Add question mark if missing
        if not t.endswith("?"):
            t = t.rstrip(".!") + "?"
        return t[:100].strip()
    # Rebuild from topic
    topic = ((slots or {}).get("topic") or "").strip()
    if topic:
        t = f"{topic[0].upper()}{topic[1:]}"
        if not t.endswith("?"):
            t += "?"
        return t[:100].strip()
    return "How Does This Actually Work?"


def similarity_reason(data: dict, slots: dict, recent: list[dict],
                      item_recent: list[dict] | None = None):
    """Repeat reason for same-topic or high narration overlap, else None."""
    topic = str(slots.get("topic", "")).casefold()
    for h in recent:
        if str(h.get("topic", "")).casefold() == topic:
            return f"topic '{slots.get('topic')}' already used"
    # Title dedup
    title = str(data.get("title", "")).strip().casefold()
    for h in recent:
        if str(h.get("title", "")).strip().casefold() == title:
            return "title already used"
    return None


def history_fields(slots: dict, meta: dict) -> dict:
    return {"topic": slots.get("topic", ""),
            "topicSource": slots.get("topic_source", "pool"),
            "category": slots.get("category", "")}


def board_filter(beats, items, durations, get_font, has_drawtext,
                 title: str = "") -> str:
    """Explainer niche has no leaderboard overlay."""
    return ""


# ---------------------------------------------------------------------------
# Full draft loop (mirrors core.gen_script for the explainer path; the
# hook-stakes regen is ranking-only and intentionally skipped here)
# ---------------------------------------------------------------------------
def _try_openrouter(prompt: str, cfg: NicheConfig | None = None) -> dict | None:
    """OpenRouter :free failover. Returns validated meta or None; never raises."""
    import requests

    models = list(getattr(cfg, "openrouter_models", []) or
                  [m.strip() for m in
                   os.getenv("OPENROUTER_MODELS",
                             "meta-llama/llama-3.3-70b-instruct:free,"
                             "openai/gpt-oss-120b:free").split(",")
                   if m.strip()][:2])[:2]
    key = (os.getenv("OPENROUTER_API_KEY", "") or "").strip()
    if not key:
        log("OpenRouter skipped (OPENROUTER_API_KEY unset).")
        return None
    for m in models:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": m,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.7,
                      "max_tokens": 4096},
                timeout=15)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = _validate_script(json.loads(text))
            log(f"OpenRouter script OK via {m}.")
            return data
        except Exception as e:
            warn(f"OpenRouter model {m} unusable ({e}), trying next...")
    return None


def _meta_to_content(meta: dict, slots: dict) -> Content:
    """Validated meta+slots -> Content (beats/slots ride in extras)."""
    return Content(
        title=meta.get("title", ""),
        narration_text=meta.get("narration", ""),
        description=meta.get("description", ""),
        tags=list(meta.get("tags", [])),
        extras={"beats": list(meta.get("beats", [])),
                "slots": dict(slots or {}),
                "topic": (slots or {}).get("topic", ""),
                "category": (slots or {}).get("category", ""),
                "topic_source": (slots or {}).get("topic_source", "")},
    )


def generate_content(history: list[dict], cfg: NicheConfig) -> Content:
    """Full script draft: Gemini (similarity regens) -> OpenRouter -> fallback."""
    history = history or []
    window = getattr(cfg, "dedup_window", DEDUP_WINDOW)
    retries = getattr(cfg, "similarity_retries", SIMILARITY_RETRIES)
    models = list(getattr(cfg, "gemini_models", []) or [])
    if not models:
        global _model_order
        if not _model_order:
            _model_order = [m.strip() for m in
                            os.getenv("GEMINI_MODELS",
                                      "gemini-3.8-flash,gemini-3.7-flash").split(",")
                            if m.strip()]
        models = list(_model_order)
    timeout = getattr(cfg, "gemini_timeout", 40)
    keys = parse_keys()
    if keys:
        client = GeminiClient(keys, timeout=timeout)
        for attempt in range(1 + retries):
            prompt, slots = build_prompt(history, cfg)
            data = None
            for model in models:
                try:
                    data = client.generate_json(
                        model, prompt,
                        generation_config={"temperature": 0.7,
                                           "maxOutputTokens": 4096})
                    _mark_model_working(model)
                    data = _validate_script(data)
                    break
                except Exception as e:
                    log(f"Gemini model {model} unusable ({e}), trying next...")
                    time.sleep(2)
            if data is None:
                log("All Gemini models failed, trying OpenRouter :free failover.")
                data = _try_openrouter(prompt, cfg)
                if data is None:
                    log("OpenRouter failed too, using fallback.")
                    break
            recent = [h for h in history[-window:]
                      if h.get("niche", "") == MODE]
            reason = similarity_reason(data, slots, recent)
            data["title"] = sanitize_title(data.get("title", ""), slots)
            if reason is None:
                log(f"Gemini script OK: {data.get('title')}")
                return _meta_to_content(data, slots)
            if attempt < retries:
                warn(f"Gemini draft too similar to history ({reason}), "
                     f"regenerating (attempt {attempt + 1}/{retries})...")
                continue
            warn(f"Gemini draft still similar ({reason}), accepting "
                 f"to avoid failing the run.")
            return _meta_to_content(data, slots)
    warn("Gemini unavailable: using standalone fallback script.")
    meta, slots = fallback_script(history)
    return _meta_to_content(meta, slots)


def sample_content() -> Content:
    """Offline sample (inlined website explainer; no fixture files)."""
    fx = {
        "topic": "What happens when you open a website",
        "title": "🌐 What Happens When You Open a Website?",
        "description": ("🌐 Ever wonder what happens in the split second "
                        "after you type a URL? #shorts #internet #explained"),
        "tags": ["shorts", "internet", "howthingswork", "explained",
                 "technology"],
        "narration": (
            "Every time you type a website address, something incredible "
            "happens in less than a second. Your browser does not actually "
            "know where that website lives. So it asks a special server "
            "called a DNS server, which works like a phone book for the "
            "internet. It translates the name you typed into a number "
            "address. Your request then travels through cables, sometimes "
            "under the ocean, to reach the server that holds the website. "
            "That server reads your request and sends back all the pieces. "
            "Your browser collects all those pieces and assembles them "
            "into the page you see. All of this happens in about 200 "
            "milliseconds. So the next time a website loads instantly, "
            "remember: your request just traveled halfway around the world "
            "and back."),
        "beats": [
            {"text": ("Every time you type a website address, something "
                      "incredible happens in less than a second."),
             "query": "person typing laptop",
             "visual_hint": {
                 "type": "text_card",
                 "description": "What happens when you open a website?",
                 "label": "What Happens When You\nOpen a Website?"}},
            {"text": ("Your browser does not actually know where that "
                      "website lives. So it asks a special server called a "
                      "DNS server, which works like a phone book for the "
                      "internet. It translates the name you typed into a "
                      "number address."),
             "query": "server room data center",
             "visual_hint": {
                 "type": "process_flow",
                 "description": ("Browser sends request to DNS server, gets "
                                 "IP address back"),
                 "components": ["Browser", "DNS Server", "IP Address"],
                 "label": "DNS Lookup"}},
            {"text": ("Your request then travels through cables, sometimes "
                      "under the ocean, to reach the server that holds the "
                      "website. That server reads your request and sends "
                      "back all the pieces."),
             "query": "undersea cable ocean",
             "visual_hint": {
                 "type": "diagram",
                 "description": ("Data traveling through undersea cables to "
                                 "a web server"),
                 "components": ["Computer", "Cable", "Ocean", "Server"],
                 "label": "Data Request"}},
            {"text": ("Your browser collects all those pieces and assembles "
                      "them into the page you see. All of this happens in "
                      "about 200 milliseconds."),
             "query": "website loading browser",
             "visual_hint": {
                 "type": "process_flow",
                 "description": ("Browser assembling HTML, CSS, and images "
                                 "into a webpage"),
                 "components": ["HTML", "CSS", "Images", "Webpage"],
                 "label": "Page Rendering"}},
            {"text": ("So the next time a website loads instantly, remember: "
                      "your request just traveled halfway around the world "
                      "and back."),
             "query": "earth globe network",
             "visual_hint": {
                 "type": "text_card",
                 "description": "Your request just traveled around the world",
                 "label": "Halfway Around\nthe World in 200ms"}},
        ],
    }
    slots = {"topic": fx["topic"],
             "topic_source": "sample",
             "category": _topic_category(fx["topic"])}
    return Content(
        title=fx["title"],
        narration_text=fx["narration"],
        description=fx["description"],
        tags=list(fx["tags"]),
        extras={"beats": [dict(b) for b in fx["beats"]],
                "slots": slots, "topic": slots["topic"],
                "category": slots["category"],
                "topic_source": "sample"},
    )
