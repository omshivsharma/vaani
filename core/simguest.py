"""Full test mode: an AI "guest" talks to Vaani like a real call, with no phone needed.

The guest is played by Gemini (as a relative with a chosen personality) and spoken with a
different neural voice. Its speech goes through Vaani's real ears (audio -> Gemini), Vaani's
real brain and real voice, so the whole conversation is tested end to end. With aloud=True
both sides are played on the laptop speaker, so you can listen to the call.
"""
import urllib.error

import numpy as np

from . import audio, brain, store, voice
from .log import log

GUEST_VOICE = "hi-IN-SwaraNeural"

PERSONAS = {
    "curious": {
        "label": "Curious relative: asks date, venue, functions, then says yes",
        "brief": "You are a warm village relative from Haryana. Ask, one per turn, how the wedding is related to you if unclear, "
                 "the date, the venue, when the preetibhoj (dinner) is, and when the baraat leaves. Then say you will come with family.",
    },
    "tricky": {
        "label": "Tricky uncle: asks things not on the card (stay, dress code, parking)",
        "brief": "You are a talkative uncle. Ask about rukne ka intezaam (stay), dress code and parking, one per turn. "
                 "Then ask the date. Finally say 'theek hai, dekhte hain' (maybe).",
    },
    "busy": {
        "label": "Busy person: asks to call back later",
        "brief": "You are busy driving. Say you can't talk right now and ask them to call back in the evening.",
    },
    "declines": {
        "label": "Can't come: politely declines",
        "brief": "You are a relative who is travelling abroad that week. Listen, congratulate warmly, ask the date, then say you cannot come.",
    },
    "wrong": {
        "label": "Wrong number",
        "brief": "You don't know anyone named Omshiv. Say it's a wrong number, politely.",
    },
}


def _guest_reply(persona, history, contact):
    """What the simulated guest says next (short Hinglish, Roman script)."""
    lines = "\n".join(f"{'CALLER' if h['role'] == 'agent' else 'YOU'}: {h['text']}" for h in history) or "(the phone just rang and you picked up)"
    body = {
        "system_instruction": {"parts": [{"text":
            f"You are role-playing {contact.get('name', 'a guest')}, a person receiving a phone call. {PERSONAS[persona]['brief']} "
            "Speak like a real person on the phone: ONE short sentence in casual Hinglish, written in Latin script. "
            "Never describe actions, never use quotes, just the words you say."}]},
        "contents": [{"role": "user", "parts": [{"text": f"Call so far:\n{lines}\n\nWhat do you say now?"}]}],
        "generationConfig": {"temperature": 0.8, "thinkingConfig": {"thinkingLevel": "minimal"}},
    }
    try:
        data = brain._post(body, store.gemini_key())
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        body["generationConfig"].pop("thinkingConfig")   # older models don't know thinkingLevel
        data = brain._post(body, store.gemini_key())
    return data["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"')


def _to_16k(a, rate=voice.TTS_RATE):
    n = int(len(a) * audio.RATE / rate)
    return np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)


class SimGuestLine(audio.Line):
    """A call line whose other end is the AI guest."""
    min_thresh = 0.01

    def __init__(self, persona, contact, get_history, aloud=True):
        self.persona, self.contact, self.get_history, self.aloud = persona, contact, get_history, aloud
        spk = audio.real_speaker()
        super().__init__(spk, None)
        self.route = "simulation"
        self.guest_lines = []          # what the guest said (for the test report)

    def describe(self):
        return f"simulated guest ({self.persona})" + (" on your speaker" if self.aloud else "")

    def open(self):
        log("audio: opened", self.describe())

    def close(self):
        pass

    def play(self, samples, rate):
        if self.stop_flag or samples is None or not len(samples):
            return
        if self.aloud:                  # let you hear Vaani's side
            self.speaker.play(samples.astype(np.float32), samplerate=rate)

    def listen(self, wait_for_speech=8.0, end_silence=0.8, max_len=20.0):
        if self.stop_flag:
            return None
        text = _guest_reply(self.persona, self.get_history(), self.contact)
        self.guest_lines.append(text)
        log("guest (simulated):", text)
        clip = voice.synth(text, GUEST_VOICE)
        if self.aloud:
            self.speaker.play(clip, samplerate=voice.TTS_RATE)
        return _to_16k(clip)            # Vaani hears the real audio, just like on a phone call
