"""Audio diagnostics: every endpoint, its state, live peak level, and which programs are using it.

Run during a call to see where Phone Link takes the call audio from:
    python tests/diag_audio.py [seconds]
"""
import sys
import time
from ctypes import POINTER, cast

import psutil
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation, IAudioSessionManager2

STATES = {1: "active", 2: "disabled", 4: "not present", 8: "unplugged"}


def sessions(dev):
    mgr = cast(dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None), POINTER(IAudioSessionManager2))
    out, en = [], mgr.GetSessionEnumerator()
    for i in range(en.GetCount()):
        from pycaw.pycaw import IAudioSessionControl2
        ctl = en.GetSession(i).QueryInterface(IAudioSessionControl2)
        pid, st = ctl.GetProcessId(), ctl.GetState()   # state 1 = active
        try:
            name = psutil.Process(pid).name() if pid else "system"
        except Exception:
            name = f"pid {pid}"
        if st == 1:
            out.append(name)
    return out


def main(secs=3.0):
    enum = AudioUtilities.GetDeviceEnumerator()
    rows = []
    for d in AudioUtilities.GetAllDevices():
        n = d.FriendlyName or ""
        if not n:
            continue
        dev = enum.GetDevice(d.id)
        state = dev.GetState()
        if state not in (1, 8):
            continue
        meter = None
        if state == 1:
            meter = cast(dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None), POINTER(IAudioMeterInformation))
        rows.append([n, STATES.get(state, state), meter, 0.0, sessions(dev) if state == 1 else []])
    t0 = time.time()
    while time.time() - t0 < secs:
        for r in rows:
            if r[2]:
                r[3] = max(r[3], r[2].GetPeakValue())
        time.sleep(0.05)
    for n, st, _, peak, ses in rows:
        print(f"{n[:52]:52} {st:10} peak={peak:.3f} users={','.join(sorted(set(ses))) or '-'}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 3.0)
