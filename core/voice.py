"""Turning text into speech.

- Your own recordings (data/recordings/<slot>.wav) are used for the opening/closing when present.
- Everything else uses a free Microsoft neural voice via edge-tts (needs internet, no account).
Long replies are split into sentences that are synthesised in parallel, so the first sentence
starts playing while the rest are still being made.
"""
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

import edge_tts
import miniaudio
import numpy as np

from . import audio, store
from .log import log

TTS_RATE = 24000
VOICES = [
    {"id": "hi-IN-MadhurNeural", "label": "Hindi / Hinglish — male (Madhur)"},
    {"id": "hi-IN-SwaraNeural", "label": "Hindi / Hinglish — female (Swara)"},
    {"id": "en-IN-PrabhatNeural", "label": "Indian English — male (Prabhat)"},
    {"id": "en-IN-NeerjaNeural", "label": "Indian English — female (Neerja)"},
    {"id": "gemini:Charon", "label": "Gemini natural voice — male (more human, ~1-2 s slower, free daily limit)"},
    {"id": "gemini:Kore", "label": "Gemini natural voice — female (more human, ~1-2 s slower, free daily limit)"},
]
GEMINI_TTS_MODEL = "gemini-3.8-flash-lite-tts"   # fastest of the Gemini voices on this key (15 s of speech in 4.5 s)
GEMINI_FALLBACK = {"gemini:Charon": "hi-IN-MadhurNeural", "gemini:Kore": "hi-IN-SwaraNeural"}

_pool = ThreadPoolExecutor(max_workers=3)


async def _tts_mp3(text, voice, rate):
    out = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
        if chunk["type"] == "audio":
            out += chunk["data"]
    return bytes(out)


# English voices can't read Hindi script; the same-gender Hindi voice can read both scripts
HINDI_TWIN = {"en-IN-PrabhatNeural": "hi-IN-MadhurNeural", "en-IN-NeerjaNeural": "hi-IN-SwaraNeural"}
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def is_english_voice(voice=None):
    return (voice or store.settings()["voice"]).startswith("en-")


def pronounce(text, words=None):
    """Swap names for their Hindi spelling (e.g. Rewari -> रेवाड़ी) so the voice says them right.
    Tested: the English voice said "Omshiv" as "Umesh Shiv"; the Hindi voice with ओमशिव gets it right."""
    words = store.campaign().get("pronunciations", {}) if words is None else words
    for written in sorted(words, key=len, reverse=True):      # longest first: "Ram Prakash" before "Ram"
        say = words[written].strip()
        if written.strip() and say:
            text = re.sub(rf"(?<!\w){re.escape(written)}(?!\w)", say, text, flags=re.I)
    return text


def _gemini_tts(text, name):
    """Gemini's natural voice. Names stay in Roman spelling: Gemini read Hindi-script names worse, and a
    longer instruction with pronunciation hints made it produce mostly-silent audio (both tested)."""
    import base64, json, urllib.request
    prompt = f"Say naturally in Hinglish: {text}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": name}}}}}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TTS_MODEL}:generateContent",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": store.gemini_key()})
    part = json.loads(urllib.request.urlopen(req, timeout=30).read())["candidates"][0]["content"]["parts"][0]["inlineData"]
    raw = base64.b64decode(part["data"])
    if raw[:4] == b"RIFF":                       # some models return a WAV file, others raw 24 kHz PCM
        import io, wave
        with wave.open(io.BytesIO(raw)) as w:
            rate, raw = w.getframerate(), w.readframes(w.getnframes())
    else:
        rate = 24000
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    if rate != TTS_RATE:
        a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * TTS_RATE / rate)), np.arange(len(a)), a).astype(np.float32)
    return trim(a, TTS_RATE, keep=0.08)


def synth(text, voice=None, rate=None):
    s = store.settings()
    voice, rate = voice or s["voice"], rate or s["voice_rate"]
    if voice.startswith("gemini:"):
        try:
            return _gemini_tts(text, voice.split(":", 1)[1])
        except Exception as e:                   # quota used up / network: never let a call go silent
            log(f"voice: Gemini voice failed ({e}); using the Hindi voice instead")
            voice = GEMINI_FALLBACK.get(voice, "hi-IN-MadhurNeural")
    text = pronounce(text)
    if DEVANAGARI.search(text) and voice in HINDI_TWIN:
        voice = HINDI_TWIN[voice]
    mp3 = b""
    for v in (voice, HINDI_TWIN.get(voice, voice)):   # retry once (with the Hindi twin for English voices)
        try:
            mp3 = asyncio.run(_tts_mp3(text, v, rate))
            if mp3:
                break
        except Exception as e:
            log(f"voice: {v} couldn't say {text!r}: {e}")
    if not mp3:
        raise RuntimeError("The voice service returned no audio (check internet)")
    d = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=TTS_RATE)
    a = np.frombuffer(d.samples, dtype=np.float32).copy()
    return trim(a, TTS_RATE, keep=0.08)     # the service pads every phrase with ~1.5 s of silence


def sentences(text):
    parts = re.split(r"(?<=[.!?।])\s+", text.strip())
    return [p for p in parts if p.strip()]


def prepare(text):
    """Start synthesising `text` now (sentence by sentence, in parallel); play it later with play_prepared."""
    return [_pool.submit(synth, s) for s in sentences(text)]


def play_prepared(line, futures, optional=False):
    """optional=True (fillers): if the voice service fails, just skip it instead of failing the call."""
    for f in futures:
        if line.stop_flag:
            break
        try:
            clip = f.result()
        except Exception as e:
            if not optional:
                raise
            log("voice: skipped a filler:", e)
            continue
        line.play(clip, TTS_RATE)


def speak(line, text):
    """Synthesise and play `text` on `line`; the first sentence plays while the rest are still being made."""
    play_prepared(line, prepare(text))


FILLERS = {"English": ["Okay.", "Right.", "I see."], "roman": ["Ji.", "Achha.", "Haan ji."], "default": ["जी।", "अच्छा।", "हाँ जी।"]}


FILLER_CLIPS = ("filler_ji", "filler_achha", "filler_haanji")


def prepare_fillers(language):
    """Tiny acknowledgements played while Gemini thinks, so the guest doesn't hear dead air.
    Your own recorded "Ji / Achha / Haan ji" are used when you've recorded them."""
    mine = [r for r in (recording(k) for k in FILLER_CLIPS) if r is not None]
    if mine:
        return [("recording", r) for r in mine]
    key = "English" if language == "English" else "roman" if is_english_voice() else "default"
    return [("tts", [_pool.submit(synth, t)]) for t in FILLERS[key]]


SILENT_PEAK = 0.01   # a recording quieter than this everywhere is silence (muted / wrong mic)


def is_silent(samples):
    return not len(samples) or float(np.abs(samples).max()) < SILENT_PEAK


def trim(samples, rate, keep=0.25):
    """Cut dead air at the start and end of your recording (keeps a little padding)."""
    loud = np.where(np.abs(samples) > SILENT_PEAK * 2)[0]
    if not len(loud):
        return samples
    pad = int(keep * rate)
    return samples[max(0, loud[0] - pad): loud[-1] + pad]


def recording(slot):
    """Your recorded clip for this slot, or None (then the AI voice reads the text instead)."""
    p = store.RECORDINGS / f"{slot}.wav"
    if not p.exists():
        return None
    a, rate = audio.read_wav(p)
    if is_silent(a):
        log(f"voice: {slot} recording is silent, using the AI voice instead")
        return None
    return trim(a, rate), rate


def prepare_slot(slot, text):
    """Your recording for this slot if usable, else start making the AI-voice version right away."""
    rec = recording(slot)
    return ("recording", rec) if rec is not None else ("tts", prepare(text))


def play_slot(line, prepared, optional=False):
    kind, x = prepared
    if kind == "recording":
        line.play(*x)
    else:
        play_prepared(line, x, optional=optional)


def play_recording_or_speak(line, slot, text):
    play_slot(line, prepare_slot(slot, text))


def preview_wav(text, voice):
    return audio.to_wav(synth(text, voice), TTS_RATE)
