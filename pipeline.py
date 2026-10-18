"""Zero-cost story Shorts pipeline: Gemini -> TTS -> local-clip visuals -> FFmpeg -> YouTube.
Run: python pipeline.py  (all config via env vars, see .env.example)
Output: out/<date>_short.mp4 (+ .srt, metadata.json)

Layout: pipeline.py (this story-first entry), core.py (shared machinery),
niches/story (loaded via niches.load_niche(), NICHE env defaults to story).
"""
import sys

import core
from niches import load_niche


def main():
    core.run(load_niche())


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception as e:
        core.err(f"PIPELINE FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        core.notify_failure(e)
        sys.exit(1)
