"""Zero-cost YouTube Shorts pipeline: Gemini -> edge-tts -> Pexels/Pillow -> FFmpeg -> YouTube.
Run: python pipeline.py  (all config via env vars, see .env.example)
Output: out/<date>_short.mp4 (+ .srt, metadata.json)

Layout: pipeline.py (this entry), core.py (shared machinery),
niche_ranking.py (Top-5 countdown niche, behind the interface documented
at the top of core.py).
"""
import sys

import core
import niche_ranking


def main():
    core.run(niche_ranking)


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception as e:
        core.err(f"PIPELINE FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        core.notify_failure(e)
        sys.exit(1)
