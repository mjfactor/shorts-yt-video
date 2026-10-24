"""Offline checks for GeminiClient.synthesize_speech request contracts.

Run: python tools/test_gemini_tts_contract.py
No network, TTS, API keys, or filesystem outputs are needed — post() is
monkeypatched to capture the outgoing payload and return a canned
generateContent response.
"""
import base64
import io
import sys
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.gemini_client import GeminiClient, _uses_verbatim_tts

_STYLE = "whispering"
_TEXT = "I found the spare key inside a letter nobody would admit writing."
_VOICE = "Kore"
_38_MODEL = "gemini-3.8-flash-tts"
_LITE_38_MODEL = "gemini-3.8-flash-lite-tts"
_LEGACY_MODEL = "gemini-3.1-flash-tts-preview"


def _canned_response(pcm: bytes, mime: str = "audio/l16; rate=24000; channels=1") -> dict:
    return {
        "candidates": [{
            "content": {
                "parts": [{
                    "inlineData": {
                        "mimeType": mime,
                        "data": base64.b64encode(pcm).decode("ascii"),
                    }
                }]
            }
        }]
    }


def _make_wav(frames: bytes, rate: int = 24000) -> bytes:
    """Build a real in-memory 24 kHz mono 16-bit PCM WAV."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


class _CapturingClient(GeminiClient):
    """GeminiClient with a fake post() that records payloads/responses."""

    def __init__(self, responses, **kw):
        super().__init__(["fake-key"], **kw)
        self.calls: list[dict] = []
        self._responses = list(responses)

    def post(self, url, payload, *, timeout=None, validate=None):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if validate is not None:
            validate(response)
        return response


class VerbatimModelTests(unittest.TestCase):
    """gemini-3.8-* : strict verbatim transcript + speech_metadata + L16."""

    def test_text_sent_verbatim_with_style_annotation_and_l16_response_format(self):
        pcm = b"\x01\x02" * 32
        client = _CapturingClient([_canned_response(pcm)])
        audio, rate = client.synthesize_speech(
            _TEXT, voice=_VOICE, models=[_38_MODEL], style=_STYLE)

        self.assertEqual(audio, pcm)
        self.assertEqual(rate, 24000)
        call = client.calls[0]
        part = call["payload"]["contents"][0]["parts"][0]
        self.assertEqual(part["text"], _TEXT)
        self.assertNotIn("Say the following text", part["text"])
        self.assertEqual(part["speech_metadata"], {"style": _STYLE})
        response_format = call["payload"]["generationConfig"]["responseFormat"]
        self.assertEqual(response_format["audio"]["mimeType"], "AUDIO_L16")
        self.assertEqual(response_format["audio"]["sampleRate"], 24000)

    def test_empty_style_omits_speech_metadata_key(self):
        client = _CapturingClient([_canned_response(b"\x03\x04" * 8)])
        client.synthesize_speech(_TEXT, voice=_VOICE, models=[_LITE_38_MODEL], style="")

        part = client.calls[0]["payload"]["contents"][0]["parts"][0]
        self.assertNotIn("speech_metadata", part)


class LegacyModelTests(unittest.TestCase):
    """3.1/2.5 preview models keep the style-prefix contract, byte-identical."""

    def test_style_prefixed_text_without_speech_metadata_or_response_format(self):
        client = _CapturingClient([_canned_response(b"\x05\x06" * 8)])
        audio, rate = client.synthesize_speech(
            _TEXT, voice=_VOICE, models=[_LEGACY_MODEL], style=_STYLE)

        self.assertEqual(rate, 24000)
        call = client.calls[0]
        part = call["payload"]["contents"][0]["parts"][0]
        self.assertEqual(part["text"], f"{_STYLE} Say the following text:\n\n{_TEXT}")
        self.assertNotIn("speech_metadata", part)
        self.assertNotIn("responseFormat", call["payload"]["generationConfig"])

    def test_empty_style_sends_bare_text(self):
        client = _CapturingClient([_canned_response(b"\x05\x06" * 8)])
        client.synthesize_speech(_TEXT, voice=_VOICE, models=[_LEGACY_MODEL], style="")

        part = client.calls[0]["payload"]["contents"][0]["parts"][0]
        self.assertEqual(part["text"], _TEXT)


class RiffGuardTests(unittest.TestCase):
    def test_container_wav_response_is_unwrapped_to_pcm(self):
        frames = b"".join((i % 251).to_bytes(2, "little") for i in range(480))
        wav = _make_wav(frames, rate=24000)
        self.assertEqual(wav[:4], b"RIFF")
        client = _CapturingClient([
            _canned_response(wav, mime="audio/wav")])
        audio, rate = client.synthesize_speech(
            _TEXT, voice=_VOICE, models=[_38_MODEL], style="")

        self.assertEqual(audio, frames)
        self.assertEqual(rate, 24000)

    def test_raw_pcm_passthrough_is_unchanged(self):
        pcm = b"\x07\x08" * 16
        client = _CapturingClient([_canned_response(pcm)])
        audio, rate = client.synthesize_speech(
            _TEXT, voice=_VOICE, models=[_38_MODEL], style="")
        self.assertEqual(audio, pcm)
        self.assertEqual(rate, 24000)


class ModelFallbackTests(unittest.TestCase):
    def test_first_model_failure_falls_back_to_second(self):
        fallback_pcm = b"\x09\x0a" * 24
        client = _CapturingClient([
            RuntimeError("boom"),
            _canned_response(fallback_pcm),
        ])
        audio, rate = client.synthesize_speech(
            _TEXT, voice=_VOICE, models=[_38_MODEL, _LEGACY_MODEL], style=_STYLE)

        self.assertEqual(audio, fallback_pcm)
        self.assertEqual(rate, 24000)
        self.assertEqual(len(client.calls), 2)
        self.assertIn(_38_MODEL, client.calls[0]["url"])
        self.assertIn(_LEGACY_MODEL, client.calls[1]["url"])


class PredicateTests(unittest.TestCase):
    def test_verbatim_gating(self):
        self.assertTrue(_uses_verbatim_tts(_38_MODEL))
        self.assertTrue(_uses_verbatim_tts(_LITE_38_MODEL))
        self.assertFalse(_uses_verbatim_tts(_LEGACY_MODEL))
        self.assertFalse(_uses_verbatim_tts("gemini-2.5-flash-preview-tts"))


if __name__ == "__main__":
    unittest.main()
