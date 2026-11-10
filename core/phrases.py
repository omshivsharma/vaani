"""Your voice library: every line Vaani may need to say, which you record once in your own voice.

During a call Gemini picks the recorded lines that fit (by id), and Vaani plays YOUR recording:
no synthetic voice, no voice-generation delay, every name said the way you say it. Lines you
haven't recorded yet are spoken by the AI voice instead.
"""
import re

from . import store, voice

# lines every call can need (text = what you read out when recording)
COMMON = [
    ("filler_ji", "Filler while thinking", "Ji."),
    ("filler_achha", "Filler while thinking", "Achha."),
    ("filler_haanji", "Filler while thinking", "Haan ji."),
    ("hello_check", "When they're silent", "Hello? Kya aap mujhe sun pa rahe hain?"),
    ("will_you_come", "Asking for an answer", "Toh aap saparivar zaroor aayenge na?"),
    ("thanks_coming", "When they say yes", "Bahut achha laga sunkar! Aapka intezaar rahega."),
    ("call_back", "When they're busy", "Koi baat nahi ji, main baad mein phir call kar leta hoon. Namaste!"),
    ("not_coming", "When they can't come", "Koi baat nahi ji, aapka aashirwad hamare saath hai. Namaste!"),
    ("wrong_number", "Wrong number", "Maaf kijiyega, shayad galat number lag gaya. Namaste!"),
    ("dont_know", "When the answer isn't known", "Iske baare mein main aapko baad mein call karke bata dunga."),
    ("repeat", "When they didn't hear", "Ji, main phir se bata deta hoon."),
    ("hold_on", "When thinking takes long", "Ek second ji..."),
]


def bank(campaign=None):
    """[{id, label, text}] — the opening/closing, every answered Q&A, and the common lines."""
    c = campaign or store.campaign()
    owner = store.settings().get("owner") or "the host"
    fill = lambda t: (t or "").replace("{owner}", owner).replace("{first} ji", "ji").replace("{first}", "").replace("{name}", "")
    items = [
        {"id": "opening", "label": "Opening (no name, it goes to everyone)", "text": fill(c.get("opening"))},
        {"id": "closing", "label": "Closing", "text": fill(c.get("closing"))},
    ]
    for i, f in enumerate(c.get("faqs", [])):
        if f.get("q", "").strip() and f.get("a", "").strip():
            items.append({"id": f"faq_{i}", "label": f"Answer: {f['q']}", "text": f["a"].strip()})
    items += [{"id": k, "label": label, "text": text} for k, label, text in COMMON]
    return items


def recorded(item_id):
    """Your recording for this line (trimmed), or None."""
    return voice.recording(item_id)


def status(campaign=None):
    out = []
    for it in bank(campaign):
        p = store.RECORDINGS / f"{it['id']}.wav"
        out.append({**it, "recorded": p.exists(), "usable": p.exists() and recorded(it["id"]) is not None})
    return out


def valid_id(item_id):
    return bool(re.fullmatch(r"[a-z0-9_]{1,40}", item_id or ""))


def prompt_block(campaign=None):
    """The list Gemini sees: only lines you have actually recorded."""
    lines = [f'- {it["id"]}: "{it["text"]}"' for it in bank(campaign) if it["id"] not in ("opening",)
             and recorded(it["id"]) is not None]
    return "\n".join(lines)
