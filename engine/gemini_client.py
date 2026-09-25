"""Gemini REST client: text + TTS with key rotation and JSON-mode helper.

Keys, models, timeouts, and voices are constructor/caller params —
no env policy is baked in (see from_env() for the optional loader).
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import time
import wave
from pathlib import Path

import requests

from .ffmpeg_utils import log

DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
]


def parse_keys(prefix: str = "GEMINI_API_KEY", extra_slots: int = 10) -> list[str]:
    """Collect keys: PREFIX (comma-separated ok) plus PREFIX_2..N. Order kept."""
    keys: list[str] = []
    raw = (os.getenv(prefix) or "").strip()
    for part in raw.split(","):
        part = part.strip()
        if part and part not in keys:
            keys.append(part)
    for i in range(2, extra_slots + 1):
        k = (os.getenv(f"{prefix}_{i}") or "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def pcm_to_wav(pcm: bytes, sample_rate: int, out_path: str | Path) -> Path:
    """Raw s16le mono PCM -> WAV (stdlib only)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm)
    return out


class GeminiClient:
    """Sticky-key client: 429 backs off same-key, 4xx/timeout fail over fast."""

    def __init__(self, keys: list[str], *, timeout: int = 40,
                 tts_timeout: int = 90, tries: int = 2, backoff: int = 5,
                 safety_settings: list[dict] | None = None) -> None:
        if not keys:
            raise ValueError("GeminiClient needs at least one API key")
        self.keys = list(keys)
        self.timeout = timeout
        self.tts_timeout = tts_timeout
        self.tries = tries
        self.backoff = backoff
        self.safety = safety_settings if safety_settings is not None else DEFAULT_SAFETY_SETTINGS
        self._key_idx = 0

    @classmethod
    def from_env(cls, prefix: str = "GEMINI_API_KEY", **kw) -> "GeminiClient":
        """Build from env (PREFIX + PREFIX_2..N, comma-separated ok)."""
        keys = parse_keys(prefix)
        if not keys:
            raise ValueError(f"{prefix} missing")
        return cls(keys, **kw)

    @staticmethod
    def _is_dead_model(status: int | None, exc: Exception) -> bool:
        """True when the model ID itself is unusable (not a key problem).

        404 always means unknown/retired model. 400 counts when the body
        points at the model (invalid/unknown/unsupported/not-found); other
        400s are payload errors — still not key errors, so they also abort
        the key loop instead of burning every key on the same dead ID.
        """
        if status == 404:
            return True
        if status == 400:
            try:
                resp = getattr(exc, "response", None)
                body = str(getattr(resp, "text", "") or "") if resp is not None else ""
                blob = f"{body} {exc}".lower()
            except Exception:
                blob = str(exc).lower()
            if "model" in blob and any(k in blob for k in (
                    "not found", "invalid", "unknown", "unsupported",
                    "not supported", "does not exist", "not exist",
                    "removed", "retired", "not available", "not valid")):
                return True
            # Any other 400 is a request/payload problem, never a key
            # problem — abort rotation all the same.
            return True
        return False

    @staticmethod
    def _status(exc: Exception) -> int | None:
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                return int(resp.status_code)
            except (TypeError, ValueError, AttributeError):
                pass
        return None

    def post(self, url: str, payload: dict, *, timeout: int | None = None,
             validate=None) -> dict:
        """POST with rotation. `url` is keyless; `validate` gates 200 payloads."""
        tries = self.tries
        backoff = self.backoff
        timeout = self.timeout if timeout is None else timeout
        n = len(self.keys)
        last_err: Exception | None = None
        server_only_failures = 0
        for k in range(n):
            idx = (self._key_idx + k) % n
            sep = "&" if "?" in url else "?"
            keyed = f"{url}{sep}key={self.keys[idx]}"
            key_server_only = True
            for attempt in range(1, tries + 1):
                try:
                    r = requests.post(keyed, json=payload, timeout=timeout)
                    r.raise_for_status()
                    data = r.json()
                    if validate is not None:
                        try:
                            validate(data)
                        except Exception as ve:
                            last_err = ve
                            key_server_only = False
                            if k < n - 1:
                                log(f"Gemini key {idx + 1}/{n} unusable payload, failing over...")
                            break
                    self._key_idx = idx
                    return data
                except Exception as e:
                    last_err = e
                    status = self._status(e)
                    if self._is_dead_model(status, e):
                        # Dead model ID (404, or 400 naming the model):
                        # abort the whole key loop at once so one bad ID
                        # never burns every key. The caller moves on to
                        # the next model; no sleep, no failover.
                        log(f"Gemini model in URL not available (HTTP {status}), "
                            "skipping remaining keys...")
                        raise
                    if status == 429:
                        key_server_only = False
                        if attempt == tries:
                            break
                        wait = max(20.0, backoff * attempt * 4) + random.uniform(1.0, 3.0)
                        log(f"Gemini key {idx + 1}/{n} rate-limited, waiting {wait:.1f}s...")
                        time.sleep(wait)
                        continue
                    if status in (401, 403, 408):
                        # Key/auth problems only: fail over to the next key.
                        key_server_only = False
                        if k < n - 1:
                            log(f"Gemini key {idx + 1}/{n} rejected (HTTP {status}), failing over...")
                        break
                    if status is not None and 400 <= status < 500:
                        # Any other 4xx is a request/model problem, never
                        # a key problem — abort rotation, let the caller
                        # try the next model.
                        key_server_only = False
                        log(f"Gemini request rejected (HTTP {status}), "
                            "not rotating keys...")
                        raise
                    # 5xx / timeouts / connection drops / generic transient:
                    # retry the SAME key up to `tries`, then rotate to the
                    # next key. 503 specifically means "this endpoint is
                    # temporarily overloaded" — rotating keys is exactly the
                    # right response since different keys may hit different
                    # backend shards.
                    if attempt >= tries:
                        key_server_only = True   # mark for rotation
                        break                    # move to next key
                    wait = backoff * attempt
                    log(f"Gemini(key {idx + 1}/{n}) failed ({e}), retrying in {wait}s...")
                    time.sleep(wait)
            if key_server_only:
                server_only_failures += 1
        assert last_err is not None
        raise last_err

    def generate_text(self, model: str, prompt: str, *,
                      generation_config: dict | None = None) -> str:
        """Single text completion; returns concatenated text parts."""
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation_config or {},
            "safetySettings": self.safety,
        }
        data = self.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            payload, timeout=self.timeout)
        cands = data.get("candidates", [])
        if not cands:
            raise ValueError(f"Model {model} returned no candidates")
        parts = (cands[0].get("content") or {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if p.get("text")).strip()
        if not text:
            raise ValueError(f"Model {model} returned empty text")
        return text

    def generate_json(self, model: str, prompt: str, *,
                      generation_config: dict | None = None) -> dict:
        """JSON-mode completion: forces application/json, strips fences, parses.

        Invalid/truncated JSON is retried across all keys (up to `tries` each)
        before giving up — the model may return cut-off JSON on an overloaded
        shard but succeed on the next key's shard.
        """
        cfg = dict(generation_config or {})
        cfg.setdefault("responseMimeType", "application/json")
        n = len(self.keys)
        last_err: Exception | None = None
        for k in range(n):
            idx = (self._key_idx + k) % n
            for attempt in range(1, self.tries + 1):
                try:
                    text = self.generate_text(model, prompt, generation_config=cfg)
                except Exception as e:
                    last_err = e
                    # generate_text already handles retries/rotation internally;
                    # if it raised, this model+key combo is done — break inner
                    # loop and let the outer key loop continue.
                    break
                clean = text.strip()
                m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", clean, re.S)
                if m:
                    clean = m.group(1)
                try:
                    parsed = json.loads(clean)
                except json.JSONDecodeError as e:
                    last_err = ValueError(
                        f"Model {model} returned invalid JSON (key {idx+1}/{n}, "
                        f"attempt {attempt}): {e}")
                    log(f"Gemini key {idx+1}/{n} invalid JSON ({e}), "
                        f"retrying with next key...")
                    # Force key rotation by advancing key index.
                    self._key_idx = (idx + 1) % n
                    break  # try next key
                if not isinstance(parsed, dict):
                    last_err = ValueError(
                        f"Model {model} returned non-object JSON")
                    break
                self._key_idx = idx
                return parsed
        assert last_err is not None
        raise last_err

    def synthesize_speech(self, text: str, *, voice: str, models: list[str],
                          style: str = "") -> tuple[bytes, int]:
        """TTS one utterance via first working model. Returns (pcm, rate)."""
        prompt = f"{style} Say the following text:\n\n{text}" if style else text
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
            },
            "safetySettings": self.safety,
        }
        last_err: Exception | None = None
        for model in models:
            try:
                url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                       f"{model}:generateContent")

                def _validate(raw, _model=model):
                    parts = raw["candidates"][0]["content"].get("parts", [])
                    inline = next((p.get("inlineData") for p in parts if p.get("inlineData")), None)
                    if not inline or not inline.get("data"):
                        raise ValueError(f"TTS model {_model} returned no audio")

                raw = self.post(url, payload, timeout=self.tts_timeout, validate=_validate)
                parts = raw["candidates"][0]["content"].get("parts", [])
                inline = next((p.get("inlineData") for p in parts if p.get("inlineData")), None)
                if not inline or not inline.get("data"):
                    raise ValueError(f"TTS model {model} returned no audio")
                rate_m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
                rate = int(rate_m.group(1)) if rate_m else 24000
                return base64.b64decode(inline["data"]), rate
            except Exception as e:
                last_err = e
                log(f"Gemini TTS model {model} unusable ({e}), trying next...")
        raise ValueError(f"All Gemini TTS models failed ({last_err})")
