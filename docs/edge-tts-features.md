# edge-tts: unused features in the live story pipeline

Checked **2026-10-18** against upstream README, tagged source, releases, and current upstream HEAD. Scope: `engine/` + `niches/story/` + manual voice tools; **not** `deprecated_niches/`. Research only: no synthesis, live voice-catalogue request, dependency upgrade, or runtime/configuration edits. Female-pitch tuning is separate work; pitch is already integrated.

## Bottom line

The project already uses the main Edge storytelling controls: **voice, rate, pitch, streaming audio, and real word-boundary timings**. The strongest unused opportunity is **voice discovery and metadata-backed selection/auditioning**, not an undiscovered emotion/style switch. Volume, sentence boundaries, native subtitle helpers, and networking controls are available but mostly operational or alternative workflows. [Upstream overview][readme], [API][api], [voice API][voices].

## What is available versus used

| Feature | Current live-project use | Useful gap / recommendation |
| --- | --- | --- |
| **Voice choice, accents/locales** | Already supported through `EDGE_VOICE` / `EDGE_VOICES`; defaults are Andrew/Jenny. `tools/voice_samples.py` already auditions a broader hardcoded candidate set, including other locales and multilingual-named voices. [Config][config], [narration][narration], [auditions][auditions]. | More voice choices are not a missing API integration. Validate candidates against the **Edge** catalogue rather than assuming every Azure voice is available. [Voices][voices]. |
| **Voice listing, filtering, metadata** | No `list_voices()` / `VoicesManager` integration. Production gender/rate/pitch routing uses small name-substring lists; audition rate selection has its own larger heuristic. [Narration][narration], [auditions][auditions]. | **Highest value:** discover and validate voices, filter by actual gender/locale, and shortlist by descriptive tags before listening. This would also avoid treating unfamiliar female names as the global rate/pitch branch. [Voices][voices], [metadata schema][types]. |
| **Rate and pitch** | Already passed to `Communicate`; global/female settings, rate-based duration refits, and audition `--rate` / `--pitch` exist. [Client][client], [narration][narration], [auditions][auditions]. | Not unused. Separate calls with different settings could experiment with hook/payoff pacing, but joining and re-timing would be application work, **not** native inline expressive control. [API][api]. |
| **Synthesis volume** | Not passed, so upstream's `volume="+0%"` default applies. Final gain, bed ducking, and limiting already happen in the video mix. [Client][client], [visuals][visuals]. | Available as a signed percentage; low priority for storytelling and not a substitute for emotional delivery or final loudness control. [Prosody validation][prosody]. |
| **Sentence boundaries** | Not requested; the client correctly explicitly requests `boundary="WordBoundary"` for word-pop captions. [Client][client]. | Alternative for sentence-caption previews/QA, not an extra stream to turn on alongside word timings: the supported API selects **one granularity per call**. Do not replace production word boundaries with sentences. [API][api]. |
| **Built-in subtitles / metadata saving** | The project consumes boundary events itself and generates its own final SRT + styled ASS after audio edits. It does not use `SubMaker` or native `save()`. [Client][client], [narration][narration], [visuals][visuals]. | `SubMaker.feed()` / `get_srt()` can make raw-synthesis SRT; `save(audio_fname, metadata_fname)` writes audio plus **JSON-lines boundary metadata**, not SRT. Useful for diagnosis, but raw timings must not replace post-edit captions. [SubMaker][subs], [API][api]. |
| **Streaming / output helpers** | Async `Communicate.stream()` is already used for audio and word events; audio is accumulated in memory and written after each call. A project `synth_sync()` adds retries. [Client][client]. | Direct-to-file streaming could reduce buffering; native `save()`, `save_sync()`, and `stream_sync()` are unused conveniences, not better voices. See the released `stream_sync()` caveat below. [API][api]. |
| **Proxy, connector, timeouts** | No explicit project plumbing for these constructor options; upstream defaults still apply. Project retries and endpoint fallback are already present. [Client][client]. | Useful only if CI/network failures justify tuning: `proxy`, an `aiohttp` connector, `connect_timeout=10`, and `receive_timeout=60` seconds. These are socket-connect/read settings; `total=None` means **not an overall synthesis deadline**. Voice listing separately accepts `proxy` / `connector`. Both upstream paths use `trust_env=True`, so absence of explicit wiring does not imply environment settings are ignored. [API][api], [voices][voices]. |

**Voice metadata details:** the schema includes `Name`, `ShortName`, `Gender`, `Locale`, `FriendlyName`, `Status`, `SuggestedCodec`, and `VoiceTag.ContentCategories` / `VoiceTag.VoicePersonalities`. Tags can be empty. `VoicesManager.create()` adds `Language`; typed `find()` filters are exact-match `Gender`, `Locale`, and `Language`. Nested category/personality membership needs application-side filtering. These tags describe a voice; they are **not selectable speaking styles**. Catalogue lookup is online, though `create(custom_voices=...)` can consume a previously obtained snapshot. No individual candidate's current availability was tested here. [Voices][voices], [schema][types].

**Exact prosody syntax:** in both 7.2.8 and 7.3.1, rate/volume require a sign plus integer percentage (e.g. `+20%`, `-10%`), and pitch requires a sign plus integer Hz (e.g. `+5Hz`). Defaults are `+0%`, `+0%`, `+0Hz`. Decimal values, semitones, pitch percentages, contour/range, and named values such as `fast` are not accepted by this API. The validator specifies syntax, not a verified safe numeric range for the service. CLI negative values need `--rate=-10%` form; production uses the Python library, so that CLI parsing issue does not apply. [Pinned validator][prosody], [current validator][prosody-current], [README][readme].

## Boundaries, output, and version caveats

- **Version matters:** [`requirements.txt`][requirements] pins **7.2.8**, not latest. The latest release observed was **7.3.1**, published 2026-10-13; upstream HEAD was `bd44c1d` from 2026-10-14. Features above are already in 7.2.8 unless stated otherwise. [Releases][releases], [current HEAD fix][sync-fix].
- **Boundary defaults changed:** 7.1.0 added sentence-boundary selection; **7.2.0 made `SentenceBoundary` the default** and removed the old `merge_cues` / `--words-in-cue` workflow. Pinned 7.2.8 and current source still default to sentences. The project override is correct and load-bearing. The inspected 7.2.8/7.3.1 CLI does **not** expose a boundary flag; select word boundaries through the Python constructor. [7.1.0][release-71], [7.2.0][release-72], [API][api], [pinned CLI][cli], [current CLI][cli-current].
- **Timings and format:** offsets/durations are 100-ns ticks (10,000,000 per second); the project's conversion is correct. The library internally splits long text and returns streamed audio/metadata. **7.2.8 already includes** CBR-byte-count inter-chunk timing compensation. Output is hardcoded to **24-kHz, 48-kbit/s, mono MP3**; a `.wav` filename does not request PCM, nor does `SuggestedCodec` select an output format. PCM conversion is downstream work. [API][api], [7.2.8 release][release-728].
- **Upgrade review, not an automatic recommendation:** 7.3.0 adds Sec-MS-GEC v2 support and CLI progress; 7.3.1 fixes locale-dependent server-date parsing. Both matter more to transport reliability than expression. The client's comment that 7.2.8 is “still the newest release” is stale. However, 7.3.1 still declares the `speech.platform.bing.com` endpoint, so do **not** assume an upgrade makes the local endpoint shim redundant. No endpoint health was tested. [7.3.0][release-730], [7.3.1][release-731], [current constants][constants], [local shim][client].
- **Do not casually replace the blocking wrapper:** `stream_sync()` in 7.2.8 and 7.3.1 can hang when async streaming fails; error propagation was fixed in **unreleased HEAD** `bd44c1d`. The project uses its own wrapper around async streaming, not that method. [Released API][api-current], [fix][sync-fix], [client][client].
- **CLI-only conveniences:** `--file` / stdin input, audio file/stdout, subtitle file/stderr, and `edge-playback` exist. CLI progress / `--no-progress` arrived in 7.3.0 and do not affect this Python integration. These add little over the existing production-path audition tool. `SubMaker` emits SRT, not styled ASS or burned captions. [README][readme], [CLI][cli-current], [SubMaker][subs].

## Not supported by edge-tts

The upstream README explicitly says custom SSML support was removed: the accepted shape is **one voice containing one prosody element**. The library escapes input text and builds that wrapper itself. Adding SSML strings to narration does not expose more controls. [README][readme], [API][api].

Consequently, do **not** advertise these as unused Edge features:

- `mstts:express-as` emotion/speaking styles, `styledegree`, age/gender role-play, or a `style="cheerful"` option. A voice named `Expressive` or tagged `Friendly` does not provide those switches. Microsoft's style/role controls are documented for **Azure Speech**, with voice-specific support. [Microsoft voice/SSML docs][azure-voice].
- Custom `<break>` / `mstts:silence`, `<emphasis>`, phonemes/lexicons/`say-as`, bookmarks, multiple voice/prosody blocks in one request, pitch contours, or an exact-duration SSML target. Azure documents these richer SSML capabilities; the Edge wrapper does not expose them. Pauses or voice changes constructed with separate synth calls and FFmpeg are application-level workarounds, not Edge SSML support. [Edge restriction][readme], [Microsoft structure/events][azure-structure], [Microsoft voice/prosody][azure-voice].
- Selectable Azure PCM/high-bitrate formats, custom/personal voices, or an assumption that Azure's catalogue and metadata (`StyleList`, `RolePlayList`, `WordsPerMinute`) are the Edge catalogue. Azure is a separate authenticated service with its own costs and availability. No commercial-use rights or service guarantees are established by this note. [Edge API][api], [Microsoft REST API][azure-rest].

## Existing boundary-handling correctness flags (not fixed)

1. **Empty word metadata can succeed silently.** `_stream_once()` only collects `WordBoundary`; `synth()` accepts nonempty audio even when that list is empty, merely warning about the count. `_check_edge_sanity([], duration)` returns `None`. Thus a changed/missing boundary response could reach captions without timed words. The explicit `WordBoundary` setting is present today, so this is a missing invariant, **not** a current default-selection bug. An offline, in-memory check confirmed `_check_edge_sanity([], 110.0) is None`. [Client][client], [sanity/mapping code][narration].
2. **Leading-silence trimming is not fully reflected in timestamps.** `_trim_gap()` removes leading silence, but `_edge_chunked()` retains the original event times and adds only the concatenation offset. `_edge_single_surgery()` subtracts the initial slice start, but not any further leading audio removed by `_trim_gap()`. If trimming removes leading time, captions can remain late by that amount; duration-based concatenation offsets and monotonic clamps do not correct the within-segment shift. This is a static code-level finding, **not a measured failure on a generated recording**. [Trim and timing-rebase implementations][narration].

**Priority:** metadata-backed voice auditions first; boundary invariants/rebasing before new timing modes; networking knobs only where failures justify them. There is no hidden Edge emotion API to enable.

[readme]: https://github.com/rany2/edge-tts/blob/bd44c1d1aaebdc1db7f47456ee12431b42ec7cac/README.md
[api]: https://github.com/rany2/edge-tts/blob/7.2.8/src/edge_tts/communicate.py
[api-current]: https://github.com/rany2/edge-tts/blob/7.3.1/src/edge_tts/communicate.py
[voices]: https://github.com/rany2/edge-tts/blob/7.2.8/src/edge_tts/voices.py
[types]: https://github.com/rany2/edge-tts/blob/7.2.8/src/edge_tts/typing.py
[prosody]: https://github.com/rany2/edge-tts/blob/7.2.8/src/edge_tts/data_classes.py
[prosody-current]: https://github.com/rany2/edge-tts/blob/7.3.1/src/edge_tts/data_classes.py
[subs]: https://github.com/rany2/edge-tts/blob/7.2.8/src/edge_tts/submaker.py
[cli]: https://github.com/rany2/edge-tts/blob/7.2.8/src/edge_tts/util.py
[cli-current]: https://github.com/rany2/edge-tts/blob/7.3.1/src/edge_tts/util.py
[releases]: https://github.com/rany2/edge-tts/releases
[release-71]: https://github.com/rany2/edge-tts/releases/tag/7.1.0
[release-72]: https://github.com/rany2/edge-tts/releases/tag/7.2.0
[release-728]: https://github.com/rany2/edge-tts/releases/tag/7.2.8
[release-730]: https://github.com/rany2/edge-tts/releases/tag/7.3.0
[release-731]: https://github.com/rany2/edge-tts/releases/tag/7.3.1
[constants]: https://github.com/rany2/edge-tts/blob/7.3.1/src/edge_tts/constants.py
[sync-fix]: https://github.com/rany2/edge-tts/commit/bd44c1d1aaebdc1db7f47456ee12431b42ec7cac
[azure-voice]: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-synthesis-markup-voice
[azure-structure]: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-synthesis-markup-structure
[azure-rest]: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-text-to-speech
[requirements]: ../requirements.txt
[client]: ../engine/edge_tts_client.py
[config]: ../niches/story/config.py
[narration]: ../niches/story/narration.py
[auditions]: ../tools/voice_samples.py
[visuals]: ../niches/story/visuals.py
