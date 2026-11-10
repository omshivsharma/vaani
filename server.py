"""Vaani — your own AI calling + invitation agent.

Run:  python server.py      then open http://localhost:8765
"""
import csv
import faulthandler
import io
import json
import mimetypes
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from core import audio, campaign, engine, phonelink, phrases, simguest, store, sysaudio, voice

PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
WEB = Path(__file__).resolve().parent / "web"
current = {"session": None, "runner": None}


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second copy bind the same port, so two Vaanis could run at once
    allow_reuse_address = False


def state():
    s = store.settings()
    try:
        devs = audio.devices()
    except Exception as e:
        devs = {"speakers": [], "mics": [], "error": str(e)}
    return {
        "settings": s,
        "has_key": bool(store.gemini_key()),
        "campaign": store.campaign(),
        "contacts": store.contacts(),
        "status": store.status(),
        "recordings": sorted(p.stem for p in store.RECORDINGS.glob("*.wav") if "." not in p.stem),
        "silent_recordings": [slot for slot in ("opening", "closing")
                              if (store.RECORDINGS / f"{slot}.wav").exists() and voice.recording(slot) is None],
        "phrases": phrases.status(),
        "audio_check": sysaudio.check(),
        "voices": voice.VOICES,
        "personas": {k: v["label"] for k, v in simguest.PERSONAS.items()},
        "devices": devs,
        "cable_found": any("cable input" in n.lower() for n in devs.get("speakers", [])),
        "in_call_hours": engine.within_call_hours(),
        "phonelink": phonelink.available(),
        "pending": [c["id"] for c in campaign.pending(store.contacts(), store.status())],
        "call_audio_on_pc": audio.call_audio_on_pc(),
    }


def busy():
    r, s = current["runner"], current["session"]
    return (r and r.state != "done") or (s and not s.finished())


def live():
    r = current["runner"]
    s = r.session if r and r.session else current["session"]
    v = s.view() if s else {"state": "idle"}
    v["pc_audio"] = audio.call_audio_on_pc() if s and s.mode == "phone" and not s.finished() else None
    v["auto"] = r.view() if r else None
    return v


def calls():
    p = store.DATA / "calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def report_csv():
    st, buf = store.status(), io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Phone", "WhatsApp", "SMS", "Call outcome", "Note", "Last call"])
    for c in store.contacts():
        x = st.get(c["id"], {})
        w.writerow([c["name"], c.get("phone", ""), "sent" if x.get("wa") else "", "sent" if x.get("sms") else "",
                    x.get("outcome", ""), x.get("note", ""), x.get("last_call", "")])
    return buf.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self):
        raw = self._body()
        return json.loads(raw) if raw else {}

    # ---------- GET ----------
    def do_GET(self):
        try:
            self._get(urlparse(self.path).path)
        except Exception as e:   # answer with the error instead of dropping the connection
            self._send(500, {"error": str(e)})

    def _get(self, path):
        if path == "/api/state":
            return self._send(200, state())
        if path == "/api/live":
            return self._send(200, live())
        if path == "/api/calls":
            return self._send(200, calls())
        if path == "/api/report.csv":
            return self._send(200, report_csv(), "text/csv; charset=utf-8")
        if path.startswith("/api/recording/"):
            p = store.RECORDINGS / (Path(path).name + ".wav")
            return self._send(200, p.read_bytes(), "audio/wav") if p.exists() else self._send(404, {"error": "none"})
        f = (WEB / (path.lstrip("/") or "index.html")).resolve()
        if WEB in f.parents or f == WEB / "index.html":
            if f.is_file():
                return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        self._send(404, {"error": "not found"})

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/settings":
                data = self._json()
                store.save("settings", {**store.settings(), **data})
                return self._send(200, {"ok": True})
            if path == "/api/key":
                store.set_gemini_key(self._json()["key"])
                return self._send(200, {"ok": True})
            if path == "/api/campaign":
                store.save("campaign", {**store.campaign(), **self._json()})
                return self._send(200, {"ok": True})
            if path == "/api/contacts":
                store.save("contacts", self._json()["contacts"])
                return self._send(200, {"ok": True})
            if path == "/api/mark":
                d = self._json()
                store.update_status(d["id"], **{d["field"]: d["value"]})
                return self._send(200, {"ok": True})
            if path.startswith("/api/recording/"):
                slot = Path(path).name
                if not phrases.valid_id(slot):
                    return self._send(400, {"error": "bad slot"})
                body = self._body()
                tmp = store.RECORDINGS / f"{slot}.new.wav"
                tmp.write_bytes(body)
                a, rate = audio.read_wav(tmp)
                if voice.is_silent(a):
                    tmp.unlink()
                    return self._send(400, {"error": "That recording is silent, so it was not saved. Your laptop mic is probably muted: "
                                                     "Windows Settings → System → Sound → Input → Microphone Array → unmute and raise the volume, then record again."})
                tmp.replace(store.RECORDINGS / f"{slot}.wav")
                return self._send(200, {"ok": True, "seconds": round(len(a) / rate, 1)})
            if path == "/api/tts":
                d = self._json()
                return self._send(200, voice.preview_wav(d["text"], d.get("voice")), "audio/wav")
            if path == "/api/call/start":
                d = self._json()
                if busy():
                    return self._send(409, {"error": "A call is already running. End it first."})
                if d["mode"] == "phone" and not engine.within_call_hours():
                    lo, hi = store.settings()["call_hours"]
                    return self._send(400, {"error": f"Calls are only allowed between {lo}:00 and {hi}:00 (change in Settings)."})
                current["runner"] = None
                current["session"] = engine.CallSession(d["contact"], d["mode"], d.get("persona", "curious"), d.get("aloud", True))
                current["session"].start()
                return self._send(200, {"ok": True})
            if path == "/api/auto/start":
                d = self._json()
                if busy():
                    return self._send(409, {"error": "A call is already running. End it first."})
                if not engine.within_call_hours():
                    lo, hi = store.settings()["call_hours"]
                    return self._send(400, {"error": f"Calls are only allowed between {lo}:00 and {hi}:00 (change in Settings)."})
                guests = store.contacts()
                if d.get("ids"):
                    queue = [g for g in guests if g["id"] in d["ids"]]
                else:
                    queue = campaign.pending(guests, store.status())
                if not queue:
                    return self._send(400, {"error": "Nobody left to call."})
                current["session"] = None
                current["runner"] = campaign.Runner(queue, retry=d.get("retry", True) and not d.get("ids"))
                current["runner"].start()
                return self._send(200, {"ok": True, "count": len(queue)})
            if path == "/api/auto/stop":
                if current["runner"]:
                    current["runner"].stop()
                return self._send(200, {"ok": True})
            if path == "/api/call/answered":
                r = current["runner"]
                s = r.session if r and r.session else current["session"]
                if s:
                    s.picked_up()
                return self._send(200, {"ok": True})
            if path == "/api/call/skip":
                r = current["runner"]
                if r and r.session:
                    r.session.stop()
                return self._send(200, {"ok": True})
            if path == "/api/call/stop":
                r = current["runner"]
                if r:
                    r.stop()
                if current["session"]:
                    current["session"].stop()
                return self._send(200, {"ok": True})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/recording/"):
            p = store.RECORDINGS / (Path(path).name + ".wav")
            if p.exists() and p.parent == store.RECORDINGS:
                p.unlink()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    # if Windows audio/automation ever crashes the process, leave a traceback behind in data/crash.log
    faulthandler.enable(open(store.DATA / "crash.log", "a", encoding="utf-8"), all_threads=True)
    url = f"http://localhost:{PORT}"
    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        # port taken: most likely Vaani is already running in another window, so just open it
        print(f"Vaani is already running. Opening {url}")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        sys.exit(0)
    print("=" * 60)
    print(f"  Vaani is running at {url}")
    print("  KEEP THIS WINDOW OPEN while you use Vaani.")
    print("  Closing it (or Ctrl+C) stops Vaani.")
    print("=" * 60, flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
