"""Local JSON storage. Everything lives in vaani/data/ on this laptop; nothing is uploaded."""
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("VAANI_DATA") or ROOT / "data")   # tests point this at a temp folder
RECORDINGS = DATA / "recordings"
DATA.mkdir(exist_ok=True)
RECORDINGS.mkdir(exist_ok=True)

_lock = threading.Lock()

DEFAULT_SETTINGS = {
    "owner": "",                 # whose behalf the agent calls on
    "country_code": "91",
    "voice": "hi-IN-MadhurNeural",
    "voice_rate": "+0%",
    "gemini_model": "gemini-flash-lite-latest",   # ~2 s per turn; the full Flash model took ~30 s, too slow for a call
    "out_device": "",            # where the agent's voice goes in phone mode (e.g. "CABLE Input")
    "in_device": "",             # whose loopback we listen to in phone mode (default speaker if blank)
    "call_hours": [10, 20],      # phone calls only allowed between these hours
}

DEFAULT_CAMPAIGN = {
    "name": "My invitation",
    "language": "Hinglish",
    "goal": "Invite the person to the event and find out if they can come.",
    "facts": "Event: <EVENT>\nDate: <DATE>\nTime: <TIME>\nVenue: <VENUE>\nDress code / notes: <NOTES>",
    "opening": "Namaste {first} ji! Yeh {owner} ki taraf se ek AI assistant ki call hai. {owner} aapko <EVENT> ke liye personally invite karna chahte hain. Kya aapke paas ek minute hai?",
    "closing": "Bahut dhanyavaad {first} ji! Details aapko WhatsApp par bhi bhej di jayengi. Namaste!",
    "instructions": "Be warm and respectful, like a family member calling. Keep every reply to one or two short sentences. If they ask something you don't know, say {owner} will call them back.",
    "outcomes": "coming, not coming, maybe, call back later, wrong number, do not call",
    "max_turns": 8,
    "faqs": [],                  # [{"q": ..., "a": ...}] prepared answers for cross-questions
    # how names must sound: written form -> Hindi spelling the voice reads correctly
    "pronunciations": {"Omshiv": "ओमशिव"},
    "wa_template": "Hi {first}! 🎉\n\nYou're warmly invited to <EVENT> on <DATE> at <TIME>.\nVenue: <VENUE>\n\nIt would mean a lot to have you with us. Please reply to confirm.\n\n– {owner}",
    "sms_template": "Hi {first}, you're invited to <EVENT> on <DATE>, <TIME> at <VENUE>. Hope to see you! - {owner}",
}


def _path(name):
    return DATA / f"{name}.json"


def load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(default, dict):
            return {**default, **data}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return default.copy() if hasattr(default, "copy") else default


def save(name, data):
    with _lock:
        tmp = _path(name).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path(name))


def settings():
    return load("settings", DEFAULT_SETTINGS)


def campaign():
    return load("campaign", DEFAULT_CAMPAIGN)


def contacts():
    return load("contacts", [])


def status():
    """contact id -> {wa, sms, outcome, note, last_call}"""
    return load("status", {})


def update_status(cid, **fields):
    with _lock:
        st = load("status", {})
        st[cid] = {**st.get(cid, {}), **fields}
    save("status", st)


def append_call(record):
    with _lock:
        with open(DATA / "calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def gemini_key():
    if os.getenv("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


def set_gemini_key(key):
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [l for l in lines if not l.strip().startswith("GEMINI_API_KEY=")]
    lines.append(f"GEMINI_API_KEY={key.strip()}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
