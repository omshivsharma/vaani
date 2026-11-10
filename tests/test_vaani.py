"""Vaani test suite. Runs without a phone, mic, internet or Gemini key, and never touches your real data.

Run:  python -m unittest discover -s tests -v      (from the vaani folder)
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

TMP = tempfile.mkdtemp(prefix="vaani-test-")
os.environ["VAANI_DATA"] = TMP
os.environ["VAANI_FAKE_BRAIN"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from core import audio, brain, campaign, engine, phonelink, store

GUEST = {"id": "g919876543210", "name": "Rahul Sharma", "first": "Rahul", "phone": "98765 43210", "phones": ["98765 43210"], "labels": []}


# ---------- fakes ----------
class FakeRecorder:
    """Serves pre-made 20 ms frames like soundcard's recorder."""
    def __init__(self, frames):
        self.frames = list(frames)

    def record(self, n):
        # when the script runs out, keep repeating the last frame (the line's steady background)
        f = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        time.sleep(0.002)
        return f.reshape(-1, 1)

    def flush(self):
        return None


def frames(*parts):
    """parts: (seconds, rms) -> list of 20 ms frames of noise at that loudness"""
    rng, out = np.random.default_rng(0), []
    for secs, level in parts:
        for _ in range(int(secs / 0.02)):
            out.append((rng.standard_normal(audio.FRAME) * level).astype(np.float32))
    return out


class SimLine(audio.Line):
    """A call line that 'hears' scripted utterances and swallows whatever the agent says."""
    def __init__(self, utterances, route="bluetooth"):
        self.stop_flag, self.level, self._rec, self.floor = False, 0.0, None, 0.001
        self.utterances, self.played, self.route = list(utterances), 0.0, route

    def open(self): pass
    def close(self): pass

    def play(self, samples, rate):
        self.played += len(samples) / rate

    def listen(self, **kw):
        if kw.get("max_len", 20) < 5:       # the short wait for their "Hello?" right after pickup
            return None
        return self.utterances.pop(0) if self.utterances else None


SPEECH = np.zeros(16000, dtype=np.float32)


class FakePhoneLink:
    """Plays the part of Phone Link: a scripted sequence of call states."""
    def __init__(self, states, comms_ok=True, issue="", reconnect_ok=True, fail_first_dial=False):
        self.states, self.dialled, self.hung_up = list(states), [], 0
        self.comms_ok = comms_ok           # what the Windows "communications mic is the cable" check reports
        self.issue, self.reconnect_ok, self.reconnects = issue, reconnect_ok, 0
        self.fail_first_dial = fail_first_dial

    def install(self, test):
        for name in ("dial", "call_state", "hang_up", "snapshot", "available", "connection_issue", "reconnect", "audio_to_pc"):
            orig = getattr(phonelink, name)
            test.addCleanup(setattr, phonelink, name, orig)
        phonelink.connection_issue = lambda: self.issue
        phonelink.reconnect = self.reconnect
        self.audio_checks = 0
        def audio_to_pc():
            self.audio_checks += 1
            return {"on_pc": True, "clicked": "", "seen": []}
        phonelink.audio_to_pc = audio_to_pc
        from core import sysaudio
        test.addCleanup(setattr, sysaudio, "check", sysaudio.check)
        sysaudio.check = lambda: {"comms_ok": self.comms_ok, "comms_mic": "x", "muted_mics": []}
        phonelink.dial = self.dial
        phonelink.call_state = self.call_state
        phonelink.hang_up = self.hang_up
        phonelink.snapshot = lambda: {}
        phonelink.available = lambda: True

    def dial(self, n):
        if self.fail_first_dial:
            self.fail_first_dial = False
            raise RuntimeError("Phone Link didn't start the call. Is your phone connected in Phone Link → Calls?")
        self.dialled.append(n)

    def reconnect(self):
        self.reconnects += 1
        if self.reconnect_ok:
            self.issue = ""
        return self.reconnect_ok

    def call_state(self):
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    def hang_up(self):
        self.hung_up += 1
        self.states = ["none"]
        return True


def quiet_voice(test):
    """Skip the online voice service: every phrase becomes silence of a matching length."""
    from core import voice
    test.addCleanup(setattr, voice, "synth", voice.synth)
    voice.synth = lambda text, *a, **k: np.zeros(len(text) * 200, dtype=np.float32)


def fresh_data():
    for f in os.listdir(TMP):
        p = os.path.join(TMP, f)
        if os.path.isfile(p):
            os.remove(p)
    store.save("contacts", [GUEST])
    store.save("settings", {**store.DEFAULT_SETTINGS, "owner": "Omshiv", "call_hours": [0, 24]})


# ---------- tests ----------
class TestBasics(unittest.TestCase):
    def test_data_is_isolated(self):
        self.assertEqual(str(store.DATA), TMP)

    def test_normalize(self):
        self.assertEqual(engine.normalize("98765 43210"), "919876543210")
        self.assertEqual(engine.normalize("098765-43210"), "919876543210")
        self.assertEqual(engine.normalize("+91 98765 43210"), "919876543210")
        self.assertEqual(engine.normalize("0044 20 7946 0958"), "442079460958")
        self.assertEqual(engine.normalize("2079460958", cc="44"), "442079460958")

    def test_tel_link_uses_plain_10_digits_for_own_country(self):
        self.assertEqual(phonelink.tel_number("919876543210", "91"), "9876543210")
        self.assertEqual(phonelink.tel_number("442079460958", "91"), "+442079460958")

    def test_fill(self):
        self.assertEqual(brain.fill("Hi {first}, {owner} here for {name}", GUEST, "Omshiv"), "Hi Rahul, Omshiv here for Rahul Sharma")
        self.assertIn("the host", brain.fill("{owner}", GUEST, ""))

    def test_prompt_uses_only_answered_faqs(self):
        c = {**store.DEFAULT_CAMPAIGN, "faqs": [{"q": "Shaadi kab hai?", "a": "12 December"}, {"q": "Parking milegi?", "a": ""}]}
        p = brain.system_prompt(c, GUEST, "Omshiv")
        self.assertIn("12 December", p)
        self.assertNotIn("Parking", p)
        self.assertIn("unanswered", p)

    def test_english_voice_gets_roman_script(self):
        self.assertIn("Latin script", brain.language_rule("Hinglish", "en-IN-PrabhatNeural"))
        self.assertIn("Devanagari", brain.language_rule("Hinglish", "hi-IN-MadhurNeural"))
        self.assertEqual(brain.language_rule("English", "en-IN-PrabhatNeural"), brain.SCRIPT_RULES["English"])

    def test_hindi_text_goes_to_hindi_twin_voice(self):
        from core import voice
        asked = []
        async def fake_tts(text, v, rate):
            asked.append(v)
            return b"mp3"
        class Decoded:
            samples = (np.ones(2400, dtype=np.float32) * 0.2).tobytes()
        self.addCleanup(setattr, voice, "_tts_mp3", voice._tts_mp3)
        self.addCleanup(setattr, voice.miniaudio, "decode", voice.miniaudio.decode)
        voice._tts_mp3, voice.miniaudio.decode = fake_tts, lambda *a, **k: Decoded()
        voice.synth("जी।", "en-IN-PrabhatNeural")
        voice.synth("Okay.", "en-IN-PrabhatNeural")
        self.assertEqual(asked, ["hi-IN-MadhurNeural", "en-IN-PrabhatNeural"])

    def test_wav_roundtrip(self):
        a = (np.sin(np.arange(16000) / 10) * 0.5).astype(np.float32)
        path = os.path.join(TMP, "t.wav")
        open(path, "wb").write(audio.to_wav(a))
        b, rate = audio.read_wav(path)
        self.assertEqual(rate, 16000)
        self.assertLess(float(np.abs(a - b).max()), 0.001)


class TestRecordings(unittest.TestCase):
    def write(self, slot, samples, rate=48000):
        with open(store.RECORDINGS / f"{slot}.wav", "wb") as f:
            f.write(audio.to_wav(samples, rate))

    def tearDown(self):
        for p in store.RECORDINGS.glob("*.wav"):
            p.unlink()

    def test_silent_recording_is_ignored(self):
        from core import voice
        self.write("opening", np.zeros(48000 * 5, dtype=np.float32))
        self.assertIsNone(voice.recording("opening"))

    def test_dead_air_is_trimmed(self):
        from core import voice
        a = np.zeros(48000 * 6, dtype=np.float32)
        a[48000 * 2: 48000 * 4] = 0.3 * np.sin(np.arange(48000 * 2) / 5)
        self.write("opening", a)
        rec, rate = voice.recording("opening")
        self.assertTrue(2.0 <= len(rec) / rate <= 2.6, len(rec) / rate)


class TestVoiceLibrary(unittest.TestCase):
    def setUp(self):
        fresh_data()
        store.save("campaign", {**store.DEFAULT_CAMPAIGN, "faqs": [{"q": "Shaadi kab hai?", "a": "25 November ko."}, {"q": "Parking?", "a": ""}],
                                "pronunciations": {"Rewari": "रेवाड़ी", "Ram Prakash": "राम प्रकाश"}})

    def tearDown(self):
        for p in store.RECORDINGS.glob("*.wav"):
            p.unlink()

    def record(self, item_id, secs=1.0):
        with open(store.RECORDINGS / f"{item_id}.wav", "wb") as f:
            f.write(audio.to_wav((0.3 * np.sin(np.arange(int(48000 * secs)) / 5)).astype(np.float32), 48000))

    def test_bank_has_opening_answered_faqs_and_common_lines(self):
        from core import phrases
        ids = [p["id"] for p in phrases.bank()]
        self.assertEqual(ids[:3], ["opening", "closing", "faq_0"])
        self.assertNotIn("faq_1", ids, "unanswered Q&A has nothing to record yet")
        self.assertIn("hello_check", ids)
        self.assertNotIn("{first}", phrases.bank()[0]["text"])

    def test_only_recorded_lines_are_offered_to_gemini(self):
        from core import phrases
        self.assertEqual(phrases.prompt_block(), "")
        self.record("faq_0")
        self.assertIn("faq_0", phrases.prompt_block())
        self.assertIn("faq_0", brain.system_prompt(store.campaign(), GUEST, "Omshiv"))

    def test_pronunciation_swaps_whole_names_longest_first(self):
        from core import voice
        self.assertEqual(voice.pronounce("Gaon Rewari, Ram Prakash ji, Rewariwale"), "Gaon रेवाड़ी, राम प्रकाश ji, Rewariwale")

    def test_gemini_voice_falls_back_to_hindi_voice(self):
        from core import voice
        used = []
        async def fake_tts(text, v, rate):
            used.append(v)
            return b"mp3"
        class Decoded:
            samples = (np.ones(2400, dtype=np.float32) * 0.2).tobytes()
        self.addCleanup(setattr, voice, "_tts_mp3", voice._tts_mp3)
        self.addCleanup(setattr, voice, "_gemini_tts", voice._gemini_tts)
        self.addCleanup(setattr, voice.miniaudio, "decode", voice.miniaudio.decode)
        def quota(*a):
            raise RuntimeError("HTTP 429 quota")
        voice._tts_mp3, voice._gemini_tts, voice.miniaudio.decode = fake_tts, quota, lambda *a, **k: Decoded()
        voice.synth("Namaste ji", "gemini:Charon")
        self.assertEqual(used, ["hi-IN-MadhurNeural"])

    def test_call_plays_your_recordings_when_gemini_picks_them(self):
        quiet_voice(self)
        self.record("faq_0", 1.0)
        self.record("will_you_come", 0.5)
        turns = iter([{"heard": "Shaadi kab hai?", "reply": "25 November ko. Toh aap aayenge na?", "clips": ["faq_0", "will_you_come"],
                       "end_call": False, "outcome": "", "note": "", "unanswered": ""},
                      {"heard": "Haan.", "reply": "Dhanyavaad!", "clips": [], "end_call": True, "outcome": "coming", "note": "", "unanswered": ""}])
        self.addCleanup(setattr, brain, "turn", brain.turn)
        brain.turn = lambda *a, **k: next(turns)
        pl = FakePhoneLink(["active"])
        pl.install(self)
        self.addCleanup(setattr, audio, "PhoneLine", audio.PhoneLine)
        line = SimLine([SPEECH, SPEECH])
        audio.PhoneLine = lambda *a: line
        s = engine.CallSession(GUEST, "phone")
        s.start()
        s.thread.join(timeout=30)
        mine = [t for t in s.transcript if t.get("voice") == "yours"]
        self.assertEqual(len(mine), 1)
        self.assertGreaterEqual(line.played, 1.4, "both recordings were played")
        self.assertEqual(s.outcome, "coming")


class TestListening(unittest.TestCase):
    def line(self, fr, cls=audio.Line):
        ln = cls.__new__(cls)
        audio.Line.__init__(ln, None, None)
        ln._rec, ln.floor = FakeRecorder(fr), 0.002
        return ln

    def test_silence_returns_none(self):
        self.assertIsNone(self.line(frames((3, 0.002))).listen(wait_for_speech=1))

    def test_speech_is_captured(self):
        out = self.line(frames((0.5, 0.002), (1.2, 0.1), (1.0, 0.002))).listen(wait_for_speech=2)
        self.assertIsNotNone(out)
        self.assertTrue(1.2 <= len(out) / 16000 <= 2.6, len(out) / 16000)

    def test_short_click_is_ignored(self):
        self.assertIsNone(self.line(frames((0.5, 0.002), (0.04, 0.2), (2, 0.002))).listen(wait_for_speech=1.5))

    def test_quiet_phone_speech_is_heard(self):
        # narrow-band phone audio is quieter: 0.03 RMS must count as speech on a PhoneLine
        out = self.line(frames((0.5, 0.001), (1.0, 0.03), (1.0, 0.001)), audio.PhoneLine).listen(wait_for_speech=2)
        self.assertIsNotNone(out)

    def test_floor_adapts_to_hiss(self):
        ln = self.line(frames((4, 0.006)))
        self.assertIsNone(ln.listen(wait_for_speech=3))
        self.assertGreater(ln.floor, 0.004)


class TestPhoneCall(unittest.TestCase):
    def setUp(self):
        fresh_data()
        quiet_voice(self)
        self.addCleanup(setattr, engine, "RING_TIMEOUT", engine.RING_TIMEOUT)
        self.addCleanup(setattr, audio, "PhoneLine", audio.PhoneLine)
        self.addCleanup(setattr, audio, "call_audio_on_pc", audio.call_audio_on_pc)
        audio.call_audio_on_pc = lambda: True

    def run_call(self, pl, utterances):
        pl.install(self)
        line = SimLine(utterances)
        audio.PhoneLine = lambda *a: line
        s = engine.CallSession(GUEST, "phone")
        s.start()
        s.thread.join(timeout=30)
        self.assertTrue(s.finished(), s.state)
        return s, line

    def test_auto_pickup_full_conversation(self):
        pl = FakePhoneLink(["ringing", "ringing", "active"])
        s, line = self.run_call(pl, [SPEECH, SPEECH])
        self.assertEqual(pl.dialled, ["919876543210"])
        self.assertEqual(s.outcome, "coming")
        self.assertEqual(s.route, "bluetooth")
        self.assertGreater(line.played, 0)
        self.assertEqual(pl.hung_up, 1, "Vaani should hang up after the goodbye")
        saved = store.status()[GUEST["id"]]
        self.assertEqual(saved["outcome"], "coming")
        self.assertEqual(json.loads(open(os.path.join(TMP, "calls.jsonl"), encoding="utf-8").readline())["route"], "bluetooth")

    def test_declined(self):
        pl = FakePhoneLink(["ringing", "ringing", "none"])
        s, line = self.run_call(pl, [])
        self.assertEqual(s.outcome, "declined / busy")
        self.assertEqual(line.played, 0)

    def test_no_answer_times_out_and_hangs_up(self):
        engine.RING_TIMEOUT = 2
        pl = FakePhoneLink(["ringing"])
        s, _ = self.run_call(pl, [])
        self.assertEqual(s.outcome, "no answer")
        self.assertGreaterEqual(pl.hung_up, 1)

    def test_manual_pickup_fallback(self):
        # Phone Link's window can't be read ("unknown") -> pressing Space still starts the talk
        pl = FakePhoneLink(["unknown"])
        pl.install(self)
        line = SimLine([SPEECH, SPEECH])
        audio.PhoneLine = lambda *a: line
        s = engine.CallSession(GUEST, "phone")
        s.start()
        time.sleep(1)
        s.picked_up()
        s.thread.join(timeout=30)
        self.assertEqual(s.outcome, "coming")

    def test_silent_guest(self):
        pl = FakePhoneLink(["active"])
        s, _ = self.run_call(pl, [])
        self.assertEqual(s.outcome, "no response")
        self.assertIn("Hello?", " ".join(t["text"] for t in s.transcript))

    def test_guest_hangs_up_mid_call(self):
        class SlowLine(SimLine):
            def listen(self, **kw):
                for _ in range(40):
                    if self.stop_flag:
                        return None
                    time.sleep(0.1)
                return None
        pl = FakePhoneLink(["active"])
        pl.install(self)
        audio.PhoneLine = lambda *a: SlowLine([])
        s = engine.CallSession(GUEST, "phone")
        s.start()
        time.sleep(1.2)
        pl.states = ["none"]
        s.thread.join(timeout=30)
        self.assertEqual(s.outcome, "hung up")
        self.assertEqual(pl.hung_up, 0, "no need to hang up a call that's already over")

    def test_refuses_to_dial_when_guests_cant_hear(self):
        pl = FakePhoneLink(["active"], comms_ok=False)
        s, _ = self.run_call(pl, [])
        self.assertEqual(s.state, "error")
        self.assertIn("CABLE Output", s.error)
        self.assertEqual(pl.dialled, [], "must not ring anyone if they'd only hear silence")

    def test_greeting_is_ready_before_pickup(self):
        """The greeting audio is made while it rings, so Vaani speaks within moments of pickup."""
        from core import voice
        made = []
        real = voice.synth
        voice.synth = lambda text, *a, **k: (made.append((time.time(), text)), real(text))[1]
        pl = FakePhoneLink(["ringing"] * 8 + ["active"])
        s, line = self.run_call(pl, [SPEECH, SPEECH])
        opening_first = brain.fill(store.campaign()["opening"], GUEST, "Omshiv").split("!")[0]
        greet_made = [t for t, text in made if text.startswith(opening_first)]
        answered = next(x["t"] for x in s.transcript if x["role"] == "agent")
        self.assertTrue(greet_made and greet_made[0] < answered, "greeting must be synthesised before it's needed")

    def test_filler_plays_while_thinking(self):
        pl = FakePhoneLink(["active"])
        s, line = self.run_call(pl, [SPEECH, SPEECH])
        self.assertEqual(s.outcome, "coming")
        # opening + 2 fillers + 2 replies were all played; fillers never enter the transcript
        self.assertEqual([t["role"] for t in s.transcript], ["agent", "person", "agent", "person", "agent"])

    def test_reconnects_a_dropped_phone_then_calls(self):
        pl = FakePhoneLink(["active"], issue="Enable Wi-Fi on I2403 and scan QR code to reconnect")
        s, _ = self.run_call(pl, [SPEECH, SPEECH])
        self.assertEqual((pl.reconnects, s.outcome), (1, "coming"))

    def test_retries_once_when_the_call_doesnt_start(self):
        pl = FakePhoneLink(["active"], fail_first_dial=True)
        s, _ = self.run_call(pl, [SPEECH, SPEECH])
        self.assertEqual((pl.reconnects, len(pl.dialled), s.outcome), (1, 1, "coming"))

    def test_gives_up_clearly_if_phone_stays_disconnected(self):
        pl = FakePhoneLink(["active"], issue="Enable Wi-Fi on I2403 and scan QR code to reconnect", reconnect_ok=False)
        s, _ = self.run_call(pl, [])
        self.assertEqual(s.state, "error")
        self.assertIn("Enable Wi-Fi", s.error)
        self.assertEqual(pl.dialled, [])

    def test_failed_call_keeps_earlier_real_answer(self):
        store.update_status(GUEST["id"], outcome="coming", note="said yes")
        pl = FakePhoneLink(["ringing", "ringing", "none"])     # this time they decline
        self.run_call(pl, [])
        self.assertEqual(store.status()[GUEST["id"]]["outcome"], "coming")

    def test_checks_call_audio_is_on_the_laptop_after_pickup(self):
        pl = FakePhoneLink(["active"])
        s, _ = self.run_call(pl, [SPEECH, SPEECH])
        self.assertGreaterEqual(pl.audio_checks, 1)

    def test_dead_line_is_reported_as_no_audio_not_hung_up(self):
        class DeadLine(SimLine):
            max_peak = 0.0
            def listen(self, **kw):
                self.last_peak = 0.0
                return None
        pl = FakePhoneLink(["active"])
        pl.install(self)
        audio.PhoneLine = lambda *a: DeadLine([])
        s = engine.CallSession(GUEST, "phone")
        s.start()
        s.thread.join(timeout=30)
        self.assertEqual(s.outcome, "no audio")
        self.assertIn("call audio was probably on your phone", s.error)
        self.assertGreaterEqual(pl.audio_checks, 2, "should try again to move the audio to the PC")
        self.assertIn(GUEST["id"], [c["id"] for c in campaign.pending([GUEST], store.status())], "must be called again later")

    def test_failed_filler_never_breaks_the_call(self):
        from core import voice
        quiet = voice.synth
        def flaky(text, *a, **k):
            if text in sum(voice.FILLERS.values(), []):
                raise RuntimeError("No audio was received")
            return quiet(text)
        voice.synth = flaky
        pl = FakePhoneLink(["active"])
        s, _ = self.run_call(pl, [SPEECH, SPEECH])
        self.assertEqual((s.outcome, s.error), ("coming", ""))

    def test_errors_are_reported_not_swallowed(self):
        pl = FakePhoneLink(["active"])
        pl.install(self)
        def boom(*a):
            raise RuntimeError("Call audio isn't on the laptop")
        audio.PhoneLine = boom
        s = engine.CallSession(GUEST, "phone")
        s.start()
        s.thread.join(timeout=30)
        self.assertEqual(s.state, "error")
        self.assertIn("Call audio", s.error)
        self.assertEqual(pl.hung_up, 1, "a failed call must still be hung up")


class TestFullTestMode(unittest.TestCase):
    def setUp(self):
        fresh_data()
        quiet_voice(self)

    def test_ai_guest_conversation_runs_end_to_end_and_saves_nothing(self):
        from core import simguest
        said = iter(["Hello?", "Haan boliye, shaadi kab hai?", "Theek hai, aayenge."])
        self.addCleanup(setattr, simguest, "_guest_reply", simguest._guest_reply)
        simguest._guest_reply = lambda persona, history, contact: next(said)
        s = engine.CallSession(GUEST, "simulate", persona="curious", aloud=False)
        s.start()
        s.thread.join(timeout=30)
        self.assertEqual((s.state, s.route, s.outcome), ("done", "simulation", "coming"))
        self.assertEqual(s.line.guest_lines[0], "Hello?", "the AI guest answers the phone first")
        self.assertNotIn(GUEST["id"], store.status(), "test calls must not touch real guest results")

    def test_every_persona_has_a_brief(self):
        from core import simguest
        for k, p in simguest.PERSONAS.items():
            self.assertTrue(p["label"] and p["brief"], k)


class FakeSession:
    script = {}

    def __init__(self, contact, mode):
        self.contact, self.mode, self.error, self.state = contact, mode, "", "done"
        self.outcome = FakeSession.script.get(contact["id"], ["coming"]).pop(0)
        self.thread = threading.Thread(target=lambda: None)

    def start(self):
        self.thread.start()

    def stop(self):
        pass

    def finished(self):
        return True

    def view(self):
        return {"state": self.state, "mode": self.mode, "contact": self.contact, "outcome": self.outcome}


class TestCampaign(unittest.TestCase):
    def guests(self, n):
        return [{"id": f"g{i}", "name": f"Guest {i}", "phone": f"98765000{i:02d}"} for i in range(n)]

    def test_pending_skips_called_and_do_not_call(self):
        g = self.guests(4)
        st = {"g0": {"outcome": "coming"}, "g1": {"outcome": "do not call"}, "g2": {"wa": True}}
        self.assertEqual([c["id"] for c in campaign.pending(g, st)], ["g2", "g3"])
        self.assertEqual([c["id"] for c in campaign.pending(g, st, include_called=True)], ["g0", "g2", "g3"])
        # calls that never reached the guest leave them on the to-call list
        for o in ("no answer", "stopped", "hung up", "call back later", "declined / busy"):
            self.assertIn("g0", [c["id"] for c in campaign.pending(g, {"g0": {"outcome": o}})], o)
        self.assertNotIn("g0", [c["id"] for c in campaign.pending(g, {"g0": {"outcome": "maybe"}})])

    def test_runs_everyone_and_retries_unanswered_once(self):
        fresh_data()
        FakeSession.script = {"g0": ["coming"], "g1": ["no answer", "maybe"], "g2": ["not coming"]}
        r = campaign.Runner(self.guests(3), retry=True, session_factory=FakeSession, gap=0.01)
        r.start()
        r.thread.join(timeout=10)
        self.assertEqual(r.state, "done")
        self.assertEqual([(x["name"], x["outcome"]) for x in r.log],
                         [("Guest 0", "coming"), ("Guest 1", "no answer"), ("Guest 2", "not coming"), ("Guest 1", "maybe")])

    def test_stop(self):
        fresh_data()
        FakeSession.script = {}
        r = campaign.Runner(self.guests(5), session_factory=FakeSession, gap=0.5)
        r.start()
        time.sleep(0.2)
        r.stop()
        r.thread.join(timeout=5)
        self.assertEqual(r.state, "done")
        self.assertLess(len(r.log), 5)

    def test_stops_outside_calling_hours(self):
        store.save("settings", {**store.DEFAULT_SETTINGS, "call_hours": [0, 0]})
        r = campaign.Runner(self.guests(2), session_factory=FakeSession, gap=0.01)
        r.start()
        r.thread.join(timeout=5)
        self.assertEqual(len([x for x in r.log if x["name"] != "—"]), 0)


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.srv = server.Server(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        fresh_data()
        self.server.current.update(session=None, runner=None)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=20) as r:
            return r.status, r.read()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_pages_and_state(self):
        for p in ("/", "/app.js", "/style.css"):
            self.assertEqual(self.get(p)[0], 200, p)
        st = json.loads(self.get("/api/state")[1])
        self.assertEqual(st["contacts"][0]["name"], "Rahul Sharma")
        self.assertIn("phonelink", st)

    def test_no_path_traversal(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/../server.py")

    def test_campaign_faqs_saved(self):
        self.post("/api/campaign", {"faqs": [{"q": "Kab?", "a": "12 Dec"}]})
        self.assertEqual(store.campaign()["faqs"][0]["a"], "12 Dec")

    def test_mark_and_report(self):
        self.post("/api/mark", {"id": GUEST["id"], "field": "outcome", "value": "coming"})
        csv = self.get("/api/report.csv")[1].decode("utf-8-sig")
        self.assertIn("Rahul Sharma", csv)
        self.assertIn("coming", csv)

    def test_auto_start_one_and_busy_guard(self):
        FakeSession.script = {}
        orig = campaign.Runner.__init__.__defaults__
        campaign.Runner.__init__.__defaults__ = (True, FakeSession, 0.01)
        self.addCleanup(setattr, campaign.Runner.__init__, "__defaults__", orig)
        code, body = self.post("/api/auto/start", {"ids": [GUEST["id"]]})
        self.assertEqual((code, body.get("count")), (200, 1))
        self.server.current["runner"].thread.join(timeout=5)
        live = json.loads(self.get("/api/live")[1])
        self.assertEqual(live["auto"]["state"], "done")
        self.assertEqual(live["auto"]["log"][0]["outcome"], "coming")

    def test_silent_recording_upload_is_rejected(self):
        def upload(samples):
            req = urllib.request.Request(self.base + "/api/recording/opening", data=audio.to_wav(samples, 48000), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(upload(np.zeros(48000 * 3, dtype=np.float32)), 400)
        self.assertFalse((store.RECORDINGS / "opening.wav").exists())
        self.assertEqual(upload((0.2 * np.sin(np.arange(48000 * 3) / 5)).astype(np.float32)), 200)
        self.assertTrue((store.RECORDINGS / "opening.wav").exists())
        (store.RECORDINGS / "opening.wav").unlink()

    def test_auto_start_refuses_when_nobody_left(self):
        store.update_status(GUEST["id"], outcome="coming")
        code, body = self.post("/api/auto/start", {})
        self.assertEqual(code, 400)
        self.assertIn("Nobody left", body["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
