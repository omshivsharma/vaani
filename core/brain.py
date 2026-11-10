"""The agent's brain: Gemini (free tier) listens to what the person said and decides the reply.

One request per turn does both jobs: the caller's audio goes in, and a JSON answer comes back
with what they said (transcript), what to say next, and whether the call is done.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

from . import store

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "heard": {"type": "STRING", "description": "Exact transcript of what the person just said"},
        "reply": {"type": "STRING", "description": "What the agent says next (empty if nothing)"},
        "end_call": {"type": "BOOLEAN"},
        "outcome": {"type": "STRING", "description": "One of the allowed outcomes, or empty if not known yet"},
        "note": {"type": "STRING", "description": "Short note for the owner: any detail worth remembering"},
        "unanswered": {"type": "STRING", "description": "A question the person asked that the facts/Q&A could not answer, else empty"},
        "clips": {"type": "ARRAY", "items": {"type": "STRING"},
                  "description": "Ids of the owner's recorded lines to play, in order, when they say what you want to say; else empty"},
    },
    "required": ["heard", "reply", "end_call", "outcome", "note", "unanswered", "clips"],
}

SCRIPT_RULES = {
    "Hinglish": "Speak natural Hinglish like an Indian family member. Write Hindi words in Devanagari and English words in Latin script, because a Hindi voice will read your reply aloud.",
    "Hindi": "Speak simple, warm Hindi, written in Devanagari.",
    "English": "Speak simple Indian English.",
}
# an English voice can't read Devanagari, so with one we ask for Hindi in Roman letters
ROMAN_RULES = {
    "Hinglish": "Speak natural Hinglish like an Indian family member. Write EVERYTHING in Latin script (e.g. 'Aap aa payenge?'), never Devanagari, because an English voice will read your reply aloud.",
    "Hindi": "Speak simple, warm Hindi, but write it in Latin script (e.g. 'Aap kaise hain?'), never Devanagari, because an English voice will read it aloud.",
}


def language_rule(language, voice_id):
    if voice_id.startswith("en-") and language in ROMAN_RULES:
        return ROMAN_RULES[language]
    return SCRIPT_RULES.get(language, SCRIPT_RULES["English"])


def fill(text, contact, owner):
    first = contact.get("first") or contact.get("name", "").split(" ")[0]
    return (text or "").replace("{first}", first).replace("{name}", contact.get("name", "")).replace("{owner}", owner or "the host")


def faq_text(campaign, contact, owner):
    pairs = [f for f in campaign.get("faqs", []) if f.get("q", "").strip() and f.get("a", "").strip()]
    return "\n".join(f"Q: {fill(f['q'], contact, owner)}\nA: {fill(f['a'], contact, owner)}" for f in pairs) or "(none yet)"


def recorded_lines_block(campaign):
    from . import phrases
    lines = phrases.prompt_block(campaign)
    if not lines:
        return ""
    return f"""
THE OWNER'S OWN RECORDED LINES (they play in the owner's real voice, instantly, so PREFER them):
{lines}
When one or more of these lines say what you want to say, put their ids in "clips" in the order to play
them, and set "reply" to exactly those lines' words joined together. Only write new words (with empty
"clips") when no recorded line fits. Example: asked the date -> clips ["faq_0", "will_you_come"].
"""


def system_prompt(campaign, contact, owner):
    c = campaign
    return f"""You are a phone agent calling {contact.get('name')} on behalf of {owner or 'the host'}.
You are an AI assistant and you already said so in the opening. If asked, say honestly that you are an AI assistant calling for {owner}.

GOAL: {fill(c['goal'], contact, owner)}

NAMES: spell these names exactly like this in your reply: {", ".join(f"{k} ({v})" for k, v in c.get("pronunciations", {}).items()) or "-"}

FACTS YOU MAY SHARE (never invent anything beyond these and the Q&A below):
{fill(c['facts'], contact, owner)}

PREPARED ANSWERS FOR CROSS-QUESTIONS (answer in your own short words, in the call's language):
{faq_text(c, contact, owner)}

If they ask something not covered above, don't guess: say {owner} will call them back about it, and put the question in "unanswered".
{recorded_lines_block(campaign)}

STYLE: {fill(c['instructions'], contact, owner)}
LANGUAGE: {language_rule(c['language'], store.settings()['voice'])} If the person clearly prefers another language, switch to it.

RULES:
- This is a live phone call. Replies must be short (max 2 sentences) and easy to hear.
- Each turn you receive the person's audio. Put an exact transcript in "heard". If it is silence, noise, music or a caller tune, set heard to "" and reply with a short "Hello?".
- If they say it's a wrong number, they are busy, or ask not to be called: apologise briefly, set the outcome (wrong number / call back later / do not call) and end_call=true.
- When the goal is reached, set the outcome, say the closing and set end_call=true. Closing to use: "{fill(c['closing'], contact, owner)}"
- Allowed outcomes: {c['outcomes']}. Leave outcome empty until you are sure.
- Never guess how the person is related to the family or what they know; if they ask how they're related, just say who the groom's family is.
- Never ask for money, OTPs, bank details or personal documents."""


def turn(campaign, contact, owner, history, wav_bytes):
    """history: list of {"role": "agent"|"person", "text": ...}. Returns the parsed JSON dict."""
    if os.getenv("VAANI_FAKE_BRAIN"):
        return _fake(history)
    key = store.gemini_key()
    if not key:
        raise RuntimeError("No Gemini API key. Add it in Settings (free from aistudio.google.com/apikey).")

    # Gemini wants the chat to start with "user" and alternate, so merge back-to-back turns
    contents = [{"role": "user", "parts": [{"text": "(Call connected. The person picked up.)"}]}]
    for h in history:
        role = "model" if h["role"] == "agent" else "user"
        text = h["text"] or "(silence)"
        if contents[-1]["role"] == role:
            contents[-1]["parts"].append({"text": text})
        else:
            contents.append({"role": role, "parts": [{"text": text}]})
    if contents[-1]["role"] == "user":
        contents.append({"role": "model", "parts": [{"text": "(listening)"}]})
    contents.append({"role": "user", "parts": [
        {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav_bytes).decode()}},
        {"text": "(This is the person's latest audio on the call. Respond as JSON.)"},
    ]})
    body = {
        "system_instruction": {"parts": [{"text": system_prompt(campaign, contact, owner)}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.5,
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            # speed matters more than deep thought on a call: ~1.2 s instead of ~4.4 s per reply.
            # (Gemini 3 models reject the older {"thinkingBudget": 0} with HTTP 400.)
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    try:
        data = _post(body, key)
    except urllib.error.HTTPError as e:
        if e.code == 400:   # an older model may not know thinkingLevel; retry without it
            body["generationConfig"].pop("thinkingConfig")
            data = _post(body, key)
        else:
            raise RuntimeError(f"Gemini error {e.code}: {e.read().decode(errors='ignore')[:300]}")
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    out = json.loads(text)
    out.setdefault("outcome", "")
    out.setdefault("note", "")
    out.setdefault("unanswered", "")
    out.setdefault("clips", [])
    return out


def _post(body, key, tries=3):
    model = store.settings()["gemini_model"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    for i in range(tries):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # 429 = free-tier rate limit, 5xx = Google hiccup: wait a moment and try again
            if e.code in (429, 500, 503) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if i < tries - 1:
                continue
            raise RuntimeError("Gemini didn't respond (check internet)")


def _fake(history):
    """Offline stand-in used by the self-test (VAANI_FAKE_BRAIN=1)."""
    n = sum(1 for h in history if h["role"] == "person")
    if n == 0:
        return {"heard": "Haan ji, boliye.", "reply": "Aap aa paayenge kya?", "end_call": False, "outcome": "", "note": "", "clips": []}
    return {"heard": "Haan, zaroor aaunga.", "reply": "Bahut badhiya, dhanyavaad!", "end_call": True, "outcome": "coming", "note": "Confirmed.", "clips": []}
