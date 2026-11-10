"""Checks Windows audio settings that silently break calls, so Vaani can warn you up front.

- Phone Link takes the call's outgoing voice from Windows' *communications* microphone, which
  must be "CABLE Output" or the guest hears silence.
- Your laptop mic must not be muted, or recordings and practice calls are silent.

Runs on Phone Link's UI-automation thread, because these Windows (COM) calls need a different
threading mode from the audio library used everywhere else.
"""
from . import phonelink


def _check():
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    enum = AudioUtilities.GetDeviceEnumerator()
    names = {d.id: d.FriendlyName for d in AudioUtilities.GetAllDevices()}

    def default(role):
        try:
            return names.get(enum.GetDefaultAudioEndpoint(1, role).GetId(), "")
        except Exception:
            return ""

    muted_mics = []
    for d in AudioUtilities.GetAllDevices():
        n = d.FriendlyName or ""
        if not n or "cable" in n.lower():
            continue
        try:
            dev = enum.GetDevice(d.id)
            if dev.GetState() != 1:
                continue
            vol = cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
            if n.lower().startswith("microphone") and (vol.GetMute() or vol.GetMasterVolumeLevelScalar() < 0.05):
                muted_mics.append(n)
        except Exception:
            continue
    comms = default(2)
    return {
        "comms_mic": comms,
        "comms_ok": "cable output" in comms.lower(),
        "muted_mics": muted_mics,
    }


def check():
    try:
        return phonelink._run(_check, timeout=15)
    except Exception as e:
        return {"error": str(e), "comms_ok": None, "muted_mics": []}
