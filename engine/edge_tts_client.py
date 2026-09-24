"""Keyless edge-tts narration via the edge_tts library (WordBoundary events).

Microsoft Edge's online TTS service: free with no API key, but an
UNOFFICIAL endpoint with no SLA — treat as a preview path, never
load-bearing. No commercial-rights claim is made about the output
voices; check the service terms for your own use.

Timings come from the service's WordBoundary events (100-ns ticks →
seconds via TICKS_PER_SECOND), so captions sync to the actual synth.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .ffmpeg_utils import log, warn

TICKS_PER_SECOND = 10_000_000  # WordBoundary offset/duration are 100-ns ticks

RETRY_WAITS = (2.0, 4.0, 8.0)  # sleeps between the 4 synth attempts

try:  # edge-tts pulls aiohttp along; tolerate partial installs
    from aiohttp import ClientError as _AioClientError
    _NET_ERRS: tuple = (OSError, TimeoutError, ValueError, _AioClientError)
except ImportError:  # pragma: no cover - aiohttp ships with edge-tts
    _NET_ERRS = (OSError, TimeoutError, ValueError)


def edge_tts_version() -> str:
    """Installed edge-tts version (the caller logs it at synth start)."""
    import edge_tts

    return getattr(edge_tts, "__version__", "unknown")


def _edge_retryable() -> tuple:
    """Extra retryable types from the edge_tts package itself."""
    try:
        import edge_tts.exceptions as _ee
        return (_ee.EdgeTTSException,)  # base of NoAudioReceived/WebSocketError/...
    except ImportError:  # pragma: no cover - diagnosed below instead
        return ()


async def synth(text: str, voice: str, rate: str,
                out_mp3: str | Path) -> list[dict]:
    """One library synth: mp3 bytes to out_mp3 + [{word, start, end}].

    Raises ValueError on empty audio (retried by synth_sync); raises
    TypeError/KeyError fast on programming errors (never retried).
    """
    import edge_tts

    log(f"edge-tts v{edge_tts_version()}: voice={voice} rate={rate}")
    comm = edge_tts.Communicate(text, voice, rate=rate,
                                boundary="WordBoundary")
    audio = bytearray()
    words: list[dict] = []
    async for chunk in comm.stream():
        ctype = chunk.get("type")
        if ctype == "audio":
            audio.extend(chunk.get("data") or b"")
        elif ctype == "WordBoundary":
            start = float(chunk.get("offset", 0)) / TICKS_PER_SECOND
            end = (float(chunk.get("offset", 0))
                   + float(chunk.get("duration", 0))) / TICKS_PER_SECOND
            words.append({"word": str(chunk.get("text", "")),
                          "start": round(start, 3), "end": round(end, 3)})
    if not bytes(audio):
        raise ValueError("edge-tts returned no audio bytes (empty audio)")
    out = Path(out_mp3)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(audio))
    expect = len(text.split())
    if len(words) != expect:
        warn(f"edge-tts boundary events ({len(words)}) != narration words "
             f"({expect}); captions use event texts.")
    return words


async def _synth_with_retry(text: str, voice: str, rate: str,
                            out_mp3: str | Path, attempts: int,
                            waits: tuple[float, ...]) -> list[dict]:
    """synth() with 4 attempts (2/4/8s) on connection/timeout/WS/HTTP/
    empty-audio only; TypeError/KeyError (and anything unexpected) fail
    fast without retrying."""
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await synth(text, voice, rate, out_mp3)
        except (TypeError, KeyError):
            raise
        except _NET_ERRS + _edge_retryable() as e:
            last = e
            if attempt >= max(1, attempts):
                break
            wait = waits[attempt - 1] if attempt - 1 < len(waits) else waits[-1]
            log(f"edge-tts failed (attempt {attempt}/{attempts}: {e}), "
                f"retrying in {wait:g}s...")
            time.sleep(wait)
        except Exception:
            raise
    assert last is not None
    raise last


def synth_sync(text: str, voice: str, rate: str, out_mp3: str | Path, *,
               attempts: int = 4,
               waits: tuple[float, ...] = RETRY_WAITS) -> list[dict]:
    """Blocking wrapper around synth() with retry.

    Uses asyncio.run normally; when a loop already runs in this thread
    (notebooks/tests), the synth is isolated on a worker thread instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_synth_with_retry(text, voice, rate, out_mp3,
                                             attempts, waits))
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(
                _synth_with_retry(text, voice, rate, out_mp3,
                                  attempts, waits))).result()
