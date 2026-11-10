"""One diagnostic call: finds out which audio route actually reaches the other person.

Dials the number, waits for pickup, then for each candidate route plays a short question
("can you hear me? say yes or no"), records the answer and asks Gemini what was said.
Also captures which programs use which audio device during the call (tests/diag_audio.py).
    python tests/diag_call.py 919876543210
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
import soundcard as sc

from core import audio, brain, phonelink, store, voice
from core.log import log

HERE = os.path.dirname(os.path.abspath(__file__))


def diag(secs, tag):
    out = subprocess.run([sys.executable, "-W", "ignore", os.path.join(HERE, "diag_audio.py"), str(secs)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(f"diag[{tag}]\n" + "\n".join(l for l in out.stdout.splitlines() if "unplugged" not in l) + out.stderr[-500:])


def transcribe(wav):
    import base64, json, urllib.request
    body = {"contents": [{"role": "user", "parts": [
        {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}},
        {"text": "Transcribe exactly what is said (Hindi/English). If nothing intelligible, reply: (silence)"}]}]}
    data = brain._post(body, store.gemini_key())
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def try_route(name, speaker, listen_mic, phrase):
    clip = voice.synth(phrase)
    with listen_mic.recorder(samplerate=16000, channels=1) as rec:
        t = threading.Thread(target=diag, args=(4, name))
        t.start()
        speaker.play(clip, samplerate=voice.TTS_RATE)
        rec.flush()
        reply = rec.record(16000 * 6)[:, 0]
        t.join()
    peak = float(np.abs(reply).max())
    heard = transcribe(audio.to_wav(reply)) if peak > 0.01 else "(silence)"
    log(f"ROUTE {name}: played {len(clip) / voice.TTS_RATE:.1f}s, reply peak {peak:.3f}, they said: {heard}")
    return heard


def main(number):
    log("=== DIAG CALL start")
    diag(1, "before call")
    phonelink.dial(number)
    t0 = time.time()
    while phonelink.call_state() != "active":
        if time.time() - t0 > 45:
            log("DIAG: not answered")
            phonelink.hang_up()
            return
        time.sleep(0.5)
    log("DIAG: answered")
    time.sleep(2.5)                       # let them say hello
    diag(2, "in call, idle")
    loop = sc.get_microphone(id=str(audio.real_speaker().name), include_loopback=True)
    routes = [("cable", next(s for s in sc.all_speakers() if s.name.startswith("CABLE Input")))]
    hf = [s for s in sc.all_speakers() if "hands-free" in s.name.lower()]
    routes += [("handsfree", s) for s in hf]
    log("DIAG: speakers now:", [s.name for s in sc.all_speakers()], "mics:", [m.name for m in sc.all_microphones()])
    for name, spk in routes:
        if phonelink.call_state() == "none":
            log("DIAG: call ended early")
            break
        try_route(name, spk, loop, "Namaste ji, yeh ek test hai. Kya aapko meri awaaz aa rahi hai? Haan ya na boliye.")
    phonelink.hang_up()
    log("=== DIAG CALL end")


if __name__ == "__main__":
    main(sys.argv[1])
