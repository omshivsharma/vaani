"""End-to-end check of a call without a phone, mic or Gemini key.

A fake "guest" answers with synthesised speech, the fake brain replies, and the real
TTS + VAD + engine run in between. Nothing is played out loud.
Run:  python tests/selftest.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["VAANI_FAKE_BRAIN"] = "1"

import numpy as np
from core import audio, engine, voice


class SimLine(audio.Line):
    """Feeds pre-made guest audio instead of a mic; swallows the agent's audio."""
    def __init__(self, guest_lines):
        self.stop_flag, self.level, self._rec = False, 0.0, None
        self.queue = [voice.synth(t, "hi-IN-SwaraNeural") for t in guest_lines]
        self.played = 0.0

    def open(self): pass
    def close(self): pass

    def play(self, samples, rate):
        self.played += len(samples) / rate

    def listen(self, **kw):
        if not self.queue:
            return None
        a = self.queue.pop(0)
        # TTS is 24 kHz; the engine expects 16 kHz
        return np.interp(np.arange(0, len(a), 1.5), np.arange(len(a)), a).astype(np.float32)


s = engine.CallSession({"id": "t1", "name": "Rahul Sharma", "first": "Rahul", "phone": "9876543210"}, "practice")
assert s.number == "919876543210", s.number
sim = SimLine(["हाँ जी, बोलिए।", "हाँ, मैं ज़रूर आऊँगा।"])
audio.PracticeLine = lambda: sim
t = time.time()
s.start()
s.thread.join(timeout=120)
v = s.view()
for line in v["transcript"]:
    print(f"  {line['role']:>6}: {line['text']}")
print("state:", v["state"], "| outcome:", v["outcome"], "| error:", v["error"] or "-",
      f"| agent spoke {sim.played:.1f}s | took {time.time() - t:.1f}s")
assert v["state"] == "done" and v["outcome"] == "coming" and not v["error"]
assert sim.played > 3
print("SELFTEST OK")
