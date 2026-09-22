"""Zero-cost YouTube Shorts pipeline: Gemini -> TTS -> Pexels/Visuals -> FFmpeg -> YouTube.
Run: python pipeline.py  (all config via env vars, see .env.example)
Output: out/<date>_short.mp4 (+ .srt, metadata.json)

Layout: pipeline.py (this entry), core.py (shared machinery),
niche_explainer.py (explainer niche: 'how the world works' Shorts).
To switch back to rankings: swap niche_explainer -> niche_ranking below.
"""
import sys

import core
import niche_explainer


def main():
    core.run(niche_explainer)


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception as e:
        core.err(f"PIPELINE FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        core.notify_failure(e)
        sys.exit(1)
