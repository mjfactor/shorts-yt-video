"""Zero-cost YouTube Shorts pipeline: Gemini -> edge-tts -> Pexels/Pillow -> FFmpeg -> YouTube.
Run: python pipeline.py  (all config via env vars, see .env.example)
Output: out/<date>_short.mp4 (+ .srt, metadata.json)

Layout: pipeline.py (this entry + niche registry), core.py (shared
machinery), niche_ranking.py / niche_horror.py (self-contained niches).
To delete a niche: delete its niche_*.py file and remove its line from
NICHES below — core.py never imports a niche, so nothing else changes.
"""
import os
import sys

import core
import niche_horror
import niche_ranking

NICHES = {"ranking": niche_ranking, "horror": niche_horror}

# Change here or via NICHE_MODE env (workflow wires vars.NICHE_MODE).
NICHE_MODE = os.getenv("NICHE_MODE", "ranking").strip().lower()
if NICHE_MODE not in NICHES:
    NICHE_MODE = "ranking"


def main():
    core.run(NICHES[NICHE_MODE])


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception as e:
        core.err(f"PIPELINE FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        core.notify_failure(e)
        sys.exit(1)
