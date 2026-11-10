"""Auto-calling: goes through the guest list one call at a time, with no clicks needed.

Skips people already called (any outcome) and anyone marked "do not call". People who didn't
pick up get one more try at the end of the list (if retry is on).
"""
import threading
import time

from . import engine, store
from .log import log

GAP = 8                       # seconds between calls
RETRY_OUTCOMES = {"no answer", "declined / busy", "no response", "no audio"}
FINAL_SKIP = {"do not call", "wrong number"}
STOP_ERRORS = ("gemini", "phone link", "isn't connected", "cable output", "virtual cable")
# a call "didn't land" with these, so the guest still needs calling
NOT_REACHED = RETRY_OUTCOMES | {"", "stopped", "incomplete", "hung up", "call back later"}


def pending(contacts, status, include_called=False):
    """Guests who still need a call: never reached, or (include_called) everyone except do-not-call."""
    out = []
    for c in contacts:
        o = status.get(c["id"], {}).get("outcome", "")
        if o in FINAL_SKIP:
            continue
        if o not in NOT_REACHED and not include_called:
            continue
        out.append(c)
    return out


class Runner:
    def __init__(self, contacts, retry=True, session_factory=engine.CallSession, gap=GAP):
        self.queue = list(contacts)
        self.retry = retry
        self.factory = session_factory
        self.gap = gap
        self.state = "running"          # running | stopping | done
        self.index = 0
        self.log = []                   # [{name, outcome}]
        self.session = None
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        log(f"campaign: start, {len(self.queue)} guest(s)")
        self.thread.start()

    def stop(self):
        self.state = "stopping"
        self._stop.set()
        if self.session:
            self.session.stop()

    def view(self):
        return {"state": self.state, "index": self.index, "total": len(self.queue),
                "current": self.session.contact["name"] if self.session and not self.session.finished() else "",
                "log": self.log, "queue": [c["name"] for c in self.queue]}

    def _call(self, c):
        self.session = self.factory(c, "phone")
        self.session.start()
        self.session.thread.join()
        self.log.append({"name": c["name"], "outcome": self.session.outcome, "error": self.session.error})
        return self.session.outcome

    def _run(self):
        retry_later = []
        try:
            for i, c in enumerate(self.queue):
                if self._stop.is_set():
                    break
                if not engine.within_call_hours():
                    log("campaign: outside calling hours, stopping")
                    self.log.append({"name": "—", "outcome": "stopped: outside calling hours", "error": ""})
                    break
                self.index = i + 1
                if self._call(c) in RETRY_OUTCOMES:
                    retry_later.append(c)
                err = (self.session.error or "").lower()
                if any(k in err for k in STOP_ERRORS):
                    # a setup problem (Gemini, phone not linked, audio) would fail every guest the same way
                    log("campaign: setup problem, stopping so we don't burn through the list:", self.session.error)
                    break
                if i < len(self.queue) - 1 and self._stop.wait(self.gap):
                    break
            if self.retry and retry_later and not self._stop.is_set():
                log(f"campaign: retrying {len(retry_later)} unanswered")
                self.queue += retry_later
                for c in retry_later:
                    if self._stop.wait(self.gap * 2):
                        break
                    self.index += 1
                    self._call(c)
        finally:
            self.state = "done"
            log("campaign: done", self.log)
