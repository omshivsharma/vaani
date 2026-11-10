"""One call, start to finish, running in a background thread.

Phone:    dialing -> waiting (ringing) -> answered -> talking -> hang up -> done
Practice: no dialing; the agent starts talking straight away on your laptop mic/speaker.

Pickup and hang-up are read from Phone Link's call window (core/phonelink.py). If that ever
fails, pressing "They picked up" (Space) in the app still works as a manual fallback.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
import time
from datetime import datetime

from . import audio, brain, phonelink, phrases, store, sysaudio, voice
from .log import log

RING_TIMEOUT = 45        # seconds to let it ring before giving up
# outcomes that mean the call never really got through to the person
NOT_REACHED = {"", "no answer", "declined / busy", "no response", "no audio", "hung up", "stopped", "incomplete"}
_brain_pool = ThreadPoolExecutor(max_workers=2)
HOLD_AFTER = 3.0         # if Gemini hasn't answered after this many seconds, say "Ek second ji..."


def normalize(raw, cc="91"):
    d = "".join(ch for ch in str(raw) if ch.isdigit() or ch == "+")
    if d.startswith("+"):
        return d[1:]
    if d.startswith("00"):
        return d[2:]
    d = d.lstrip("0")
    return cc + d if len(d) == 10 else d


class CallSession:
    def __init__(self, contact, mode, persona="curious", aloud=True):
        self.contact, self.mode = contact, mode          # mode: phone | practice | simulate (full test mode)
        self.persona, self.aloud = persona, aloud
        self.state = "starting"
        self.transcript = []          # [{role, text, t}]
        self.outcome, self.note, self.error = "", "", ""
        self.unanswered = []          # questions Vaani couldn't answer -> shown on Results to add to Q&A
        self.route = ""
        self.started = datetime.now()
        self.answered = threading.Event()
        self.remote_hung_up = False
        self.audio_check = None
        self.line = None
        self._stop = False
        self._turn = 0
        self._hold = None
        self.number = normalize(contact["phone"], store.settings()["country_code"]) if contact.get("phone") else ""
        self.thread = threading.Thread(target=self._run, daemon=True)

    # ---- controls from the UI ----
    def start(self):
        self.thread.start()

    def picked_up(self):
        self.answered.set()

    def stop(self):
        self._stop = True
        if self.line:
            self.line.stop_flag = True
        self.answered.set()
        if self.state not in ("done", "error"):
            self.state = "stopping"

    def finished(self):
        return self.state in ("done", "error")

    def view(self):
        return {
            "contact": self.contact, "mode": self.mode, "number": self.number, "state": self.state,
            "transcript": self.transcript, "outcome": self.outcome, "note": self.note, "error": self.error,
            "unanswered": self.unanswered, "route": self.route,
            "level": round(self.line.level, 2) if self.line else 0,
            "seconds": int((datetime.now() - self.started).total_seconds()),
        }

    # ---- the call ----
    def _say(self, text, slot=None, prepared=None):
        if self._stop:
            return
        self.transcript.append({"role": "agent", "text": text, "t": time.time()})
        self.state = "speaking"
        log("agent:", text)
        if prepared:
            voice.play_slot(self.line, prepared)
        elif slot:
            voice.play_recording_or_speak(self.line, slot, text)
        else:
            voice.speak(self.line, text)

    def _say_clips(self, text, clips):
        """Play the owner's own recorded lines, in order (instant: nothing to generate)."""
        if self._stop:
            return
        self.transcript.append({"role": "agent", "text": text, "t": time.time(), "voice": "yours"})
        self.state = "speaking"
        log("agent (your recordings):", clips, text)
        for k in clips:
            if self.line.stop_flag:
                break
            self.line.play(*phrases.recorded(k))

    def _think(self, c, owner, utt, fillers):
        """Ask Gemini for the reply; meanwhile say a tiny "जी / अच्छा" so the guest isn't left in silence."""
        self.state = "thinking"
        history = [{"role": h["role"], "text": h["text"]} for h in self.transcript]
        t = time.time()
        job = _brain_pool.submit(brain.turn, c, self.contact, owner, history, audio.to_wav(utt))
        if fillers:
            voice.play_slot(self.line, fillers[self._turn % len(fillers)], optional=True)
        try:
            out = job.result(timeout=max(0.1, HOLD_AFTER - (time.time() - t)))
        except FutureTimeout:            # Gemini is slow right now: say "Ek second ji..." like a person would
            if self._hold and not self._stop:
                voice.play_slot(self.line, self._hold, optional=True)
            out = job.result()
        log(f"brain {time.time() - t:.1f}s:", out)
        return out

    def _wait_for_pickup(self):
        """True once the call is answered; False if it was rejected, unanswered or stopped."""
        self.state = "waiting"
        t0, seen, last = time.time(), False, None
        while time.time() - t0 < RING_TIMEOUT:
            if self._stop:
                return False
            if self.answered.is_set():
                log("call: marked answered by hand")
                return True
            st = phonelink.call_state()           # ~0.25 s per check
            if st != last:
                log(f"call: phone link state {st} after {time.time() - t0:.1f}s",
                    phonelink.snapshot() if st == "unknown" else "")
                last = st
            if st in ("ringing", "active"):
                seen = True
            if st == "active":
                return True
            if st == "none" and seen:
                self.outcome = "declined / busy"
                return False
            self.answered.wait(0.1)
        self.outcome = "no answer"
        phonelink.hang_up()
        return False

    def _watch_hangup(self):
        """Background: notice when the guest hangs up, so we stop talking to nobody."""
        while not self.finished() and not self._stop:
            time.sleep(1.0)
            if self.state in ("speaking", "listening", "thinking") and phonelink.call_state() == "none":
                log("call: guest hung up")
                self.remote_hung_up = True
                self.stop()
                return

    def _run(self):
        s, c = store.settings(), store.campaign()
        owner = s["owner"]
        log(f"=== call start: {self.contact.get('name')} mode={self.mode}")
        try:
            if self.mode == "phone":
                if sysaudio.check().get("comms_ok") is False:
                    raise RuntimeError("Guests won't hear Vaani: Windows' communications microphone isn't CABLE Output. "
                                       "Press Win+R → mmsys.cpl → Recording → right-click CABLE Output → Set as Default Communication Device.")
                issue = phonelink.connection_issue()
                if issue:
                    log("call: phone not linked:", issue)
                    self.state = "reconnecting"
                    if not phonelink.reconnect():
                        raise RuntimeError(f"Your phone isn't connected to Phone Link. Phone Link says: \"{issue}\". "
                                           "Turn on Wi-Fi and Bluetooth on the phone, open Link to Windows on it, then try again.")
            # everything slow happens BEFORE pickup: the greeting audio is made while the phone rings
            opening = brain.fill(c["opening"], self.contact, owner)
            greeting = voice.prepare_slot("opening", opening)
            fillers = voice.prepare_fillers(c["language"])
            self._hold = voice.prepare_slot("hold_on", "Ek second ji..." if c["language"] != "English" else "One second please...")
            if self.mode == "phone":
                self.state = "dialing"
                try:
                    phonelink.dial(self.number)
                except RuntimeError as e:
                    if "didn't start the call" not in str(e):
                        raise
                    # usually the phone dropped off Phone Link: restart it, then try exactly once more
                    log("call: call didn't start, reconnecting and retrying once")
                    self.state = "reconnecting"
                    if not phonelink.reconnect():
                        raise RuntimeError("Phone Link couldn't reach your phone. Turn on Wi-Fi and Bluetooth on the phone, "
                                           "open Link to Windows on it, then try again.")
                    self.state = "dialing"
                    phonelink.dial(self.number)
                self.line = audio.PhoneLine(s["out_device"], s["in_device"])
                self.route = self.line.route
                self.line.open()                 # audio devices warm up during ringing
                if not self._wait_for_pickup():
                    return
                log("call: answered")
                # the call's audio must be on the laptop, or Vaani and the guest can't hear each other
                self.audio_check = phonelink.audio_to_pc()
                self.line.reset()                # forget the ringback tone
                threading.Thread(target=self._watch_hangup, daemon=True).start()
                # greet right after their "Hello?" (or after 1 s if they stay quiet)
                self.state = "listening"
                self.line.listen(wait_for_speech=1.0, end_silence=0.35, max_len=2.5)
                if getattr(self.line, "last_peak", None) == 0.0:   # dead line: try once more to bring the audio to the PC
                    log("call: no call audio on the laptop after pickup", phonelink.audio_to_pc())
            elif self.mode == "simulate":
                from .simguest import SimGuestLine
                self.line = SimGuestLine(self.persona, self.contact, lambda: list(self.transcript), self.aloud)
                self.route = self.line.route
                self.line.open()
                self.state = "listening"         # the AI guest picks up and says hello, like a real call
                self.line.listen(wait_for_speech=1.0, end_silence=0.35, max_len=2.5)
            else:
                self.line = audio.PracticeLine()
                self.route = "practice"
                self.line.open()

            self._say(opening, prepared=greeting)
            silent_turns = 0
            for _ in range(int(c.get("max_turns", 8))):
                if self._stop:
                    break
                self.state = "listening"
                utt = self.line.listen(end_silence=0.6)
                if self._stop:
                    break
                if utt is None:
                    silent_turns += 1
                    if silent_turns >= 2:
                        if self.mode == "phone" and getattr(self.line, "last_peak", None) == 0.0:
                            self.outcome = "no audio"
                            self.error = ("No sound came from the call, so the call audio was probably on your phone, not the laptop. "
                                          "In Phone Link's call window choose the PC for audio, and keep the phone off Bluetooth earbuds.")
                        else:
                            self.outcome = self.outcome or "no response"
                        break
                    self._say("Hello? Kya aap mujhe sun pa rahe hain?" if c["language"] != "English" else "Hello? Can you hear me?",
                              slot="hello_check")
                    continue
                silent_turns = 0
                out = self._think(c, owner, utt, fillers)
                self._turn += 1
                self.transcript.append({"role": "person", "text": out.get("heard", ""), "t": time.time()})
                if out.get("outcome"):
                    self.outcome = out["outcome"]
                if out.get("note"):
                    self.note = out["note"]
                if out.get("unanswered", "").strip():
                    self.unanswered.append(out["unanswered"].strip())
                clips = [k for k in out.get("clips") or [] if phrases.valid_id(k) and phrases.recorded(k) is not None]
                if clips:
                    self._say_clips(out.get("reply") or "", clips)
                elif out.get("reply"):
                    closing = out.get("end_call") and _is_closing(out["reply"], c, self.contact, owner)
                    self._say(out["reply"], slot="closing" if closing else None)
                if out.get("end_call"):
                    break
            else:
                self._say(brain.fill(c["closing"], self.contact, owner), slot="closing")
        except Exception as e:  # surface every failure in the UI instead of dying silently
            self.error = str(e)
            self.state = "error"
            log("call: ERROR", repr(e))
        finally:
            if self.line:
                self.line.close()
            if self.mode == "phone" and not self.remote_hung_up:
                time.sleep(0.5)
                phonelink.hang_up()
            dead_line = self.mode == "phone" and self.line is not None and getattr(self.line, "max_peak", 1) == 0.0
            if dead_line and self.outcome in ("", "no response") and not self.error:
                self.outcome = "no audio"
                self.error = ("No sound came from the call, so the call audio was probably on your phone, not the laptop. "
                              "In Phone Link's call window choose the PC for audio, and keep the phone off Bluetooth earbuds.")
            if not self.outcome:
                if self.remote_hung_up:
                    self.outcome = "hung up"
                elif self._stop:
                    self.outcome = "stopped"
                else:
                    self.outcome = "incomplete"
            self._save()
            log(f"=== call end: outcome={self.outcome} error={self.error or '-'}")
            if self.state != "error":
                self.state = "done"

    def _save(self):
        if self.mode in ("practice", "simulate"):    # tests never touch real guests' results
            return
        cid = self.contact["id"]
        prev = store.status().get(cid, {})
        if self.outcome in NOT_REACHED and prev.get("outcome") and prev["outcome"] not in NOT_REACHED:
            # a call that didn't get through must not erase a real earlier answer (e.g. "coming")
            store.update_status(cid, last_call=self.started.isoformat(timespec="seconds"),
                                note=f"{prev.get('note', '')} (last call: {self.outcome})".strip())
        else:
            store.update_status(cid, outcome=self.outcome, note=self.note, last_call=self.started.isoformat(timespec="seconds"))
        store.append_call({
            "contact_id": cid, "name": self.contact.get("name"), "number": self.number,
            "started": self.started.isoformat(timespec="seconds"), "outcome": self.outcome,
            "note": self.note, "error": self.error, "transcript": self.transcript,
            "unanswered": self.unanswered, "route": self.route,
        })


def _is_closing(reply, c, contact, owner):
    return reply.strip() == brain.fill(c["closing"], contact, owner).strip()


def within_call_hours():
    lo, hi = store.settings()["call_hours"]
    return lo <= datetime.now().hour < hi
