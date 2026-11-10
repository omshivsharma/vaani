"""Plain text debug log at data/vaani.log, so every call can be diagnosed afterwards."""
import threading
from datetime import datetime

from . import store

_lock = threading.Lock()
PATH = store.DATA / "vaani.log"


def log(*parts):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + "  " + " ".join(str(p) for p in parts)
    with _lock:
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:   # Windows consoles can't print emoji from contact names; the file still has them
        print(line.encode("ascii", "replace").decode(), flush=True)
