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
                    if status == 429:
                        key_server_only = False
                        if attempt == tries:
                            break
                        wait = max(20.0, backoff * attempt * 4) + random.uniform(1.0, 3.0)
                        log(f"Gemini key {idx + 1}/{n} rate-limited, waiting {wait:.1f}s...")
                        time.sleep(wait)
                        continue
                    if status is not None and 400 <= status < 500 and status != 408:
                        key_server_only = False
                        if k < n - 1:
                            log(f"Gemini key {idx + 1}/{n} rejected (HTTP {status}), failing over...")
                        break
                    if status is not None and 500 <= status < 600:
                        key_server_only = False
                    if isinstance(e, requests.exceptions.Timeout):
                        if k < n - 1:
                            log(f"Gemini key {idx + 1}/{n} timed out, failing over...")
                        break
                    if attempt == tries:
                        break
                    wait = backoff * attempt
                    log(f"Gemini(key {idx + 1}/{n}) failed ({e}), retrying in {wait}s...")
                    time.sleep(wait)
            if key_server_only:
                server_only_failures += 1
                if server_only_failures >= 2 and k < n - 1:
                    log("Gemini endpoint looks down, skipping remaining keys...")
                    break
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
        """JSON-mode completion: forces application/json, strips fences, parses."""
        cfg = dict(generation_config or {})
        cfg.setdefault("responseMimeType", "application/json")
        text = self.generate_text(model, prompt, generation_config=cfg)
        clean = text.strip()
        m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", clean, re.S)
        if m:
            clean = m.group(1)
        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError as e:
            raise ValueError(f"Model {model} returned invalid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise ValueError(f"Model {model} returned non-object JSON")
        return parsed

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
