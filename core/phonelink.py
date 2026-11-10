"""Drives the Windows Phone Link app through UI Automation, so calls need no clicks from you.

- dial(number):   opens a tel: link (Phone Link fills its dialer with it), then presses Call.
                  (Phone Link's on-screen keypad ignores UI Automation presses, so we don't use it.)
- call_state():   "none" | "ringing" | "active", read from Phone Link's in-call controls
- hang_up():      presses Phone Link's End call button

All UI Automation work runs in a separate helper process (see _run), because COM objects must
stay on the thread that created them, and a COM crash must not take Vaani down with it.
"""
import os
import re
import subprocess
import time
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from . import store
from .log import log

APP_ID = r"shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App"
TIMER = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_INVISIBLE = {c: None for c in (0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x2066, 0x2067, 0x2068, 0x2069)}

# All UI Automation / Windows-audio (COM) work runs in ONE separate helper process. COM objects
# that get cleaned up on the wrong thread can crash a whole process; this way such a crash only
# takes down the helper, which is restarted on the next request, and Vaani itself keeps running.
_helper = {"pool": None}
_helper_lock = threading.Lock()


def _pool():
    with _helper_lock:
        if _helper["pool"] is None:
            _helper["pool"] = ProcessPoolExecutor(max_workers=1)
        return _helper["pool"]


def _run(fn, *a, timeout=40):
    for attempt in (1, 2):
        try:
            return _pool().submit(fn, *a).result(timeout=timeout)
        except BrokenProcessPool:
            log("phonelink: automation helper crashed, restarting it")
            with _helper_lock:
                _helper["pool"] = None
            if attempt == 2:
                raise


def _desktop():
    from pywinauto import Desktop   # imported on the UI thread on purpose
    return Desktop(backend="uia")


_pid = {"phone_link": None}


def _windows():
    """All top-level windows that belong to Phone Link (main window + in-call windows)."""
    d = _desktop()
    if _pid["phone_link"]:          # fast path: only Phone Link's own windows
        try:
            ws = d.windows(process=_pid["phone_link"])
            if ws:
                return ws
        except Exception:
            pass
    main = [w for w in d.windows() if w.window_text() == "Phone Link"]
    if not main:
        return []
    _pid["phone_link"] = main[0].process_id()
    return [w for w in d.windows() if w.process_id() == _pid["phone_link"]]


def _main_window(start=True):
    for w in _windows():
        if w.window_text() == "Phone Link":
            return w
    if not start:
        return None
    log("phonelink: starting Phone Link")
    subprocess.Popen(["explorer.exe", APP_ID])
    for _ in range(30):
        time.sleep(1)
        for w in _windows():
            if w.window_text() == "Phone Link":
                return w
    raise RuntimeError("Phone Link didn't open. Open it once by hand and link your phone.")


def _find(root, **crit):
    """First descendant matching automation_id / name / control_type, or None."""
    try:
        for e in root.descendants(**{k: v for k, v in crit.items() if k == "control_type"}):
            info = e.element_info
            if "auto_id" in crit and info.automation_id != crit["auto_id"]:
                continue
            if "name" in crit and (info.name or "") != crit["name"]:
                continue
            return e
    except Exception:
        return None
    return None


def _controls():
    """(button names, texts) across all Phone Link windows."""
    buttons, texts = [], []
    for w in _windows():
        try:
            for e in w.descendants():
                info = e.element_info
                n = (info.name or "").strip()
                if not n:
                    continue
                if info.control_type == "Button":
                    buttons.append((n, e))
                elif info.control_type == "Text":
                    texts.append(n.translate(_INVISIBLE).strip())   # Phone Link wraps times in direction marks
        except Exception:
            continue
    return buttons, texts


END_NAMES = ("End call",)     # the button in Phone Link's in-call window


def _end_button():
    """Phone Link's End call button, found with a native UI Automation search (~0.1 s instead of
    walking the whole window, which includes your entire call history and took 1-2 s)."""
    for w in _windows():
        for name in END_NAMES:
            found = w.descendants(control_type="Button", title=name)
            if found:
                return found[0]
    return None


def _state():
    b = _end_button()
    if b is None:
        return "none"
    try:
        top = b.top_level_parent()
        texts = [t.window_text().translate(_INVISIBLE).strip() for t in top.descendants(control_type="Text")]
    except Exception:
        texts = []
    # ringing shows "Calling"; once answered, a call timer like 00:01 appears
    return "active" if any(TIMER.match(t) for t in texts) else "ringing"


def _hang_up():
    b = _end_button()
    if b is not None:
        b.invoke()
        log("phonelink: pressed end call")
        return True
    return False


TO_PC = re.compile(r"(send|transfer|switch|move).*(to )?(this )?(pc|computer)|use (this )?(pc|computer)|call on pc", re.I)


def _audio_to_pc():
    """Make sure the call's audio is on the laptop. Phone Link's call window says "Call on PC" when it is
    (and offers "Send call to mobile device"); otherwise press the button that brings it to the PC."""
    end = _end_button()
    if end is None:
        return {"on_pc": None, "clicked": "", "seen": []}
    top = end.top_level_parent()
    texts = [t.window_text().strip() for t in top.descendants(control_type="Text")]
    buttons = [b for b in top.descendants(control_type="Button")]
    names = [b.element_info.name or "" for b in buttons]
    if "Call on PC" in texts or any("mobile device" in n.lower() for n in names):
        return {"on_pc": True, "clicked": "", "seen": names}
    for b, n in zip(buttons, names):
        if TO_PC.search(n) and "mobile" not in n.lower():
            b.invoke()
            log("phonelink: call audio was on the phone; pressed", repr(n))
            return {"on_pc": False, "clicked": n, "seen": names}
    log("phonelink: call audio seems to be on the phone and no 'to PC' button was found", names, texts)
    return {"on_pc": False, "clicked": "", "seen": names}


def _snapshot():
    """What Phone Link shows right now (buttons + a few texts); written to the log for each state change."""
    buttons, texts = _controls()
    return {"buttons": [n for n, _ in buttons if not re.fullmatch(r"[0-9*#]", n)][:30], "texts": texts[:12]}


def _dialer_digits():
    b = _call_button()
    return re.sub(r"\D", "", b.element_info.name or "") if b else ""


def _clear_dialer():
    """Empty Phone Link's dialer (leftover digits would be glued onto the next number)."""
    w = _main_window(start=False)
    if w is None or not _dialer_digits():
        return True
    back = next(iter(w.descendants(control_type="Button", title="Backspace")), None)
    if back is None:
        return False
    for _ in range(30):                     # UI Automation press first (doesn't move your mouse)
        if not _dialer_digits():
            return True
        try:
            back.invoke()
        except Exception:
            break
    if _dialer_digits():                    # Phone Link ignored it: real clicks instead
        w.set_focus()
        time.sleep(0.3)
        for _ in range(30):
            if not _dialer_digits():
                break
            back.click_input()
    ok = not _dialer_digits()
    log("phonelink: cleared dialer" if ok else "phonelink: couldn't clear dialer")
    return ok


def _call_button():
    w = _main_window(start=False)
    return _find(w, auto_id="ButtonCall") if w else None


def _press_call(number):
    """A tel: link only fills Phone Link's dialer; press its Call button once the number shows up."""
    want = number[-10:]
    for _ in range(20):
        b = _call_button()
        shown = re.sub(r"\D", "", b.element_info.name or "") if b else ""
        if shown in (want, number, "0" + want):     # exactly this number, nothing glued on
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(f"Phone Link didn't show the number to dial (it shows {shown or 'nothing'}).")
    def started():
        # the in-call controls appeared, or the dialer cleared (so the press went through)
        b = _call_button()
        still_typed = b is not None and want in re.sub(r"\D", "", b.element_info.name or "")
        return _state() != "none" or not still_typed

    # Phone Link ignores UI Automation "invoke" on its Call button, so do a real click,
    # with the window brought to the front first (otherwise the click lands on whatever covers it)
    _main_window(start=False).set_focus()
    time.sleep(0.5)
    _call_button().click_input()
    log("phonelink: clicked Call")
    for _ in range(16):
        time.sleep(0.5)
        if started():
            return
    raise RuntimeError("Phone Link didn't start the call. Is your phone connected in Phone Link → Calls?")


PROBLEM = re.compile(r"reconnect|scan (the )?qr|not connected|disconnected|can't connect|couldn't connect", re.I)


def _connection_issue():
    """Phone Link's own warning when the phone isn't linked (e.g. 'Enable Wi-Fi on … to reconnect'), else ''."""
    w = _main_window(start=False)
    if w is None:
        return ""
    for t in w.descendants(control_type="Text"):
        n = (t.window_text() or "").strip()
        if PROBLEM.search(n):
            return n
    return ""


def _restart_and_wait(wait=45):
    """Restart Phone Link so it re-links the phone; True once it's up with no connection warning."""
    log("phonelink: restarting Phone Link to reconnect the phone")
    subprocess.run(["taskkill", "/IM", "PhoneExperienceHost.exe", "/F"], capture_output=True)
    _pid["phone_link"] = None
    time.sleep(2)
    _main_window(start=True)
    t0 = time.time()
    time.sleep(5)                       # give it a moment to find the phone again
    while time.time() - t0 < wait:
        if not _connection_issue() and _call_button() is not None:
            log(f"phonelink: reconnected after {time.time() - t0:.0f}s")
            return True
        time.sleep(3)
    return False


def _available():
    return _main_window(start=False) is not None


# ---- public, thread-safe wrappers ----
def reconnect():
    try:
        return _run(_restart_and_wait, timeout=90)
    except Exception as e:
        log("phonelink: reconnect failed", e)
        return False


def audio_to_pc():
    try:
        return _run(_audio_to_pc, timeout=15)
    except Exception as e:
        log("phonelink: audio check failed", e)
        return {"on_pc": None, "clicked": "", "seen": []}


def connection_issue():
    try:
        return _run(_connection_issue, timeout=20)
    except Exception as e:
        log("phonelink: connection check failed", e)
        return ""


def dial(number):
    """number: full international digits, e.g. 919876543210"""
    if not available():
        _run(_main_window, timeout=60)   # start Phone Link first so the tel: link isn't lost
        time.sleep(2)
    if not _run(_clear_dialer, timeout=60):
        raise RuntimeError("Couldn't clear Phone Link's dialer. Delete the digits in it by hand, then try again.")
    os.startfile(f"tel:{tel_number(number)}")
    log("phonelink: opened dialer for", f"+{number[:-4]}****")
    _run(_press_call, number, timeout=60)


def tel_number(number, cc=None):
    """How to write the number in the tel: link. Phone Link garbles "+91…" (it showed +919876543210 as
    919876543210787), so numbers from your own country go in as plain 10 digits."""
    cc = cc or store.settings()["country_code"]
    if number.startswith(cc) and len(number) == len(cc) + 10:
        return number[len(cc):]
    return "+" + number


def call_state():
    try:
        return _run(_state, timeout=15)
    except Exception as e:
        log("phonelink: state error", e)
        return "unknown"


def hang_up():
    try:
        return _run(_hang_up, timeout=15)
    except Exception as e:
        log("phonelink: hang up error", e)
        return False


def snapshot():
    try:
        return _run(_snapshot, timeout=15)
    except Exception as e:
        return {"error": str(e)}


def available():
    try:
        return _run(_available, timeout=15)
    except Exception:
        return False
