"""Full test call: Vaani talks to an AI guest end to end (real Gemini, real voices, no phone).

    python tests/full_test_call.py                 # curious relative, silent
    python tests/full_test_call.py tricky --aloud  # hear both sides on your speaker
Personas: curious, tricky, busy, declines, wrong
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import engine, simguest

persona = next((a for a in sys.argv[1:] if not a.startswith("--")), "curious")
guest = {"id": "test", "name": "Ramesh Chacha", "first": "Ramesh", "phone": ""}
s = engine.CallSession(guest, "simulate", persona=persona, aloud="--aloud" in sys.argv)
t = time.time()
s.start()
s.thread.join(timeout=400)
v = s.view()
print(f"\n===== FULL TEST CALL: {simguest.PERSONAS[persona]['label']} =====")
for line in v["transcript"]:
    print(f"{'VAANI' if line['role'] == 'agent' else 'GUEST'}: {line['text']}")
print(f"\noutcome: {v['outcome']} | note: {v['note']} | couldn't answer: {v['unanswered'] or '-'} | error: {v['error'] or '-'} | {time.time() - t:.0f}s")
