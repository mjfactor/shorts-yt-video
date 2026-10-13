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


# --- endpoint compat shim -------------------------------------------------
# edge-tts 7.2.8 (still the newest release) hardcodes the retired synthesis
# host speech.platform.bing.com. Microsoft moved Edge Read Aloud to
# api.msedgeservices.com; the old host 403s at the WebSocket upgrade while its
# REST voice list keeps answering 200, so --list-voices still looks healthy.
# We point the library at the current host and keep the retired one as a
# fallback for regions the new host has not reached yet.
#
# BOTH module-level copies must be set: communicate.py does
# `from .constants import WSS_URL`, so patching constants alone is a silent
# no-op. Commits nothing; remove once a release ships the migration (rany2/
# edge-tts#490, fixed by the still-unmerged PR #491).
_TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
_WSS_URLS = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    f"?Ocp-Apim-Subscription-Key={_TRUSTED_CLIENT_TOKEN}",
    "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1"
    f"?TrustedClientToken={_TRUSTED_CLIENT_TOKEN}",
)
_ENDPOINTS: tuple[str, ...] = _WSS_URLS
_SHIM_READY = False


def _install_endpoint_shim() -> None:
    """Aim edge_tts at the current synthesis host; no-op on a fixed release.

    Runs once, lazily, on first synth — edge_tts is imported here rather than at
    module import so an offline demo still imports this module cleanly.
    """
    global _ENDPOINTS, _SHIM_READY
    if _SHIM_READY:
        return
    _SHIM_READY = True
    try:
        import edge_tts.constants as _const
        import edge_tts.communicate as _comm
    except ImportError:  # pragma: no cover - diagnosed by the caller instead
        return
    installed = getattr(_comm, "WSS_URL", "")
    if "msedgeservices.com" in installed:
        # Upstream shipped the migration; trust it and stop carrying the legacy
        # host rather than pinning them to our copy of the URL.
        _ENDPOINTS = (installed,)
        return
    for mod in (_const, _comm):
        if getattr(mod, "WSS_URL", None):
            setattr(mod, "WSS_URL", _WSS_URLS[0])
    _ENDPOINTS = _WSS_URLS


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


async def _stream_once(text: str, voice: str, rate: str, pitch: str,
                       url: str) -> tuple[bytes, list[dict]]:
    """One Communicate().stream() against a specific endpoint URL.

    Communicate resolves WSS_URL at stream time, so the module global is set
    per attempt to allow cycling endpoints.
    """
    import edge_tts
    import edge_tts.communicate as _comm

    setattr(_comm, "WSS_URL", url)
    comm = edge_tts.Communicate(text, voice, rate=rate,
                                pitch=pitch,
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
    return bytes(audio), words


async def synth(text: str, voice: str, rate: str,
                out_mp3: str | Path, pitch: str = '+0Hz') -> list[dict]:
    """One library synth: mp3 bytes to out_mp3 + [{word, start, end}].

    Tries each known endpoint in turn (see the shim above) so a host that is
    live from this network is found even if the preferred one is not.

    Raises ValueError on empty audio (retried by synth_sync); raises
    TypeError/KeyError fast on programming errors (never retried).
    """
    _install_endpoint_shim()
    log(f"edge-tts v{edge_tts_version()}: voice={voice} rate={rate}")

    audio = b""
    words: list[dict] = []
    last: Exception | None = None
    for url in _ENDPOINTS:
        try:
            audio, words = await _stream_once(text, voice, rate, pitch, url)
        except (TypeError, KeyError):
            raise
        except Exception as e:  # noqa: BLE001 - try the next endpoint
            last = e
            warn(f"edge-tts endpoint {url.split('/')[2]} failed ({e}); "
                 f"trying next endpoint...")
            continue
        if audio:
            break
        last = ValueError("edge-tts returned no audio bytes (empty audio)")

    if not audio:
        assert last is not None
        raise last

    out = Path(out_mp3)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio)
    expect = len(text.split())
    if len(words) != expect:
        warn(f"edge-tts boundary events ({len(words)}) != narration words "
             f"({expect}); captions use event texts.")
    return words


async def _synth_with_retry(text: str, voice: str, rate: str,
                             out_mp3: str | Path, attempts: int,
                             waits: tuple[float, ...], pitch: str) -> list[dict]:
    """synth() with 4 attempts (2/4/8s) on connection/timeout/WS/HTTP/
    empty-audio only; TypeError/KeyError (and anything unexpected) fail
    fast without retrying."""
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await synth(text, voice, rate, out_mp3, pitch=pitch)
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
               waits: tuple[float, ...] = RETRY_WAITS,
               pitch: str = '+0Hz') -> list[dict]:
    """Blocking wrapper around synth() with retry.

    Uses asyncio.run normally; when a loop already runs in this thread
    (notebooks/tests), the synth is isolated on a worker thread instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_synth_with_retry(text, voice, rate, out_mp3,
                                             attempts, waits, pitch))
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(
_synth_with_retry(text, voice, rate, out_mp3,
                                   attempts, waits, pitch))).result()
