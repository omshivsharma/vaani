"""Audio "lines": where the agent speaks and where it listens.

PracticeLine  - your laptop mic + speaker, so you can talk to the agent yourself.
PhoneLine     - a real call through Windows Phone Link. Preferred route: straight into the
                phone's Bluetooth call channel ("<phone> Hands-Free"), which Windows only turns
                on while the call audio is on the PC:
                  agent voice -> "Speakers (<phone> Hands-Free)"   = what the guest hears
                  guest voice <- "Microphone (<phone> Hands-Free)" = what the guest says
                Fallback (no Hands-Free endpoint): agent -> "CABLE Input" (Phone Link uses
                CABLE Output as its mic) and listen on the laptop speaker via loopback.
Both are half-duplex: the agent never listens while it is talking, so it can't hear itself.
"""
import io
import time
import warnings
import wave
from collections import deque

import numpy as np
import soundcard as sc

from .log import log

warnings.filterwarnings("ignore", message="data discontinuity in recording")

RATE = 16000          # what we record at (and send to Gemini)
FRAME = 320           # 20 ms


def devices():
    return {
        "speakers": [s.name for s in sc.all_speakers()],
        "mics": [m.name for m in sc.all_microphones()],
        "default_speaker": sc.default_speaker().name,
        "default_mic": sc.default_microphone().name,
    }


def to_wav(samples, rate=RATE):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        rate, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype=np.int16 if width == 2 else np.uint8).astype(np.float32)
    a = a / 32768 if width == 2 else (a - 128) / 128
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, rate


def rms(f):
    return float(np.sqrt(np.mean(f ** 2)))


class Line:
    min_thresh = 0.02     # speech must be at least this loud (RMS); phone lines override it

    def __init__(self, speaker, mic):
        self.speaker, self.mic = speaker, mic
        self.stop_flag = False
        self.level = 0.0          # live input level for the UI meter
        self.last_peak = None     # loudest frame heard in the last listen()
        self.max_peak = 0.0       # loudest frame heard during the whole call
        self._rec = None
        self.reset()

    def reset(self):
        """Forget what the line sounded like (e.g. the ringback tone before pickup)."""
        self.floor = 0.003                    # background-noise estimate
        self.hist = deque(maxlen=75)          # last 1.5 s of between-speech loudness

    def describe(self):
        return f"speak->{self.speaker.name} | listen<-{self.mic.name}"

    def open(self):
        self._rec = self.mic.recorder(samplerate=RATE, channels=1, blocksize=FRAME)
        self._rec.__enter__()
        log("audio: opened", self.describe())

    def close(self):
        if self._rec:
            try:
                self._rec.__exit__(None, None, None)
            except Exception:
                pass
            self._rec = None

    def play(self, samples, rate):
        if self.stop_flag or samples is None or not len(samples):
            return
        samples = samples.astype(np.float32)
        step = rate // 4          # quarter-second chunks, so a hang-up stops us almost at once
        with self.speaker.player(samplerate=rate, channels=1) as p:
            for i in range(0, len(samples), step):
                if self.stop_flag:
                    break
                p.play(samples[i:i + step])
        self._rec.flush()        # throw away whatever came in while we spoke

    def thresh(self):
        return max(self.floor * 2.5, self.min_thresh)

    def listen(self, wait_for_speech=8.0, end_silence=0.8, max_len=20.0):
        """Return one utterance as float32 @16k, or None if nobody spoke."""
        rec = self._rec
        rec.flush()
        pre, speech, loud_run, quiet, started, peak = [], [], 0, 0.0, None, 0.0
        t0 = time.time()
        while not self.stop_flag:
            f = rec.record(FRAME)[:, 0]
            r = rms(f)
            peak = max(peak, r)
            th = self.thresh()
            self.level = min(1.0, r / (th * 4))
            if started is None:
                # learn the line noise while nobody talks: the quiet end (20th percentile) of the last 1.5 s
                self.hist.append(r)
                warm = len(self.hist) >= 10
                if warm:
                    self.floor = float(np.percentile(self.hist, 20))
                pre = (pre + [f])[-15:]
                loud_run = loud_run + 1 if r > th and warm else 0
                if loud_run >= 4:
                    started = time.time()
                    speech = list(pre)
                elif time.time() - t0 > wait_for_speech:
                    break
            else:
                speech.append(f)
                quiet = quiet + 0.02 if r < th else 0.0
                if quiet >= end_silence or time.time() - started > max_len:
                    break
        self.level = 0.0
        got = len(speech) * FRAME / RATE if started else 0
        self.last_peak = peak       # exactly 0.0 = digital silence: no call audio is reaching the laptop at all
        self.max_peak = max(self.max_peak, peak)
        log(f"audio: listen -> {'%.1fs speech' % got if started else 'silence'} (peak {peak:.4f}, thresh {self.thresh():.4f})")
        if not started or self.stop_flag:
            return None
        return np.concatenate(speech)


def call_audio_on_pc():
    """True while the phone's Bluetooth call audio (Hands-Free) is routed to this laptop.
    Windows only activates the "<phone> Hands-Free" endpoints when the call audio is on the PC."""
    try:
        return any("hands-free" in m.name.lower() for m in sc.all_microphones())
    except Exception:
        return None


def real_mic():
    """The laptop's own mic. Windows often makes the virtual cable the default after installing it."""
    mic = sc.default_microphone()
    if "cable" not in mic.name.lower() and "hands-free" not in mic.name.lower():
        return mic
    return next((m for m in sc.all_microphones() if "cable" not in m.name.lower() and "hands-free" not in m.name.lower()), mic)


def real_speaker():
    spk = sc.default_speaker()
    if "cable" not in spk.name.lower() and "hands-free" not in spk.name.lower():
        return spk
    return next((s for s in sc.all_speakers() if "cable" not in s.name.lower() and "hands-free" not in s.name.lower()), spk)


class PracticeLine(Line):
    def __init__(self):
        super().__init__(real_speaker(), real_mic())


class PhoneLine(Line):
    min_thresh = 0.01     # phone audio is quieter and narrow-band

    def __init__(self, out_device="", in_device=""):
        hf_out = _find(sc.all_speakers(), "hands-free")
        hf_in = _find(sc.all_microphones(), "hands-free")
        if hf_out and hf_in and not out_device and not in_device:
            self.route = "bluetooth"
            super().__init__(hf_out, hf_in)
            return
        out = _find(sc.all_speakers(), out_device or "CABLE Input")
        if out is None:
            raise RuntimeError("Call audio isn't on the laptop and no virtual cable was found. "
                               "In the Phone Link call window, switch audio to the PC.")
        listen_on = _find(sc.all_speakers(), in_device) or real_speaker()
        mic = sc.get_microphone(id=str(listen_on.name), include_loopback=True)
        self.route = "cable"
        super().__init__(out, mic)


def _find(devs, name):
    if not name:
        return None
    for d in devs:
        if name.lower() in d.name.lower():
            return d
    return None
