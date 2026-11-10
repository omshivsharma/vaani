# Vaani: your own AI calling agent

Vaani calls everyone on your guest list **by itself**. For each guest it dials from your phone, notices when they pick up, speaks your message (in your own recorded voice if you recorded one), listens, answers their questions, saves the outcome and hangs up. Then it moves to the next guest. It can also send the same invitation on WhatsApp and SMS. Everything runs on your laptop.

## Start
Double-click the **Vaani** icon on your Desktop, or **`Start Vaani.bat`** in this folder. It opens http://localhost:8765. Keep the black window open while you use Vaani.

## The flow inside the app
1. **Settings**: paste your free Gemini key (aistudio.google.com/apikey).
2. **Contacts**: import a Google Contacts CSV, tick guests or paste a list of names.
3. **Message**: event facts, opening, closing, **cross-questions (Q&A)**, WhatsApp and SMS text.
4. **Voice**: record the opening and closing in your own voice (watch the level bar move), then pick the AI voice for live replies.
5. **Test & practice**:
   - **🤖 Full test call**: an AI guest (curious relative, tricky uncle, busy, can't come, or wrong number) talks to Vaani like a real call. It uses Vaani's real brain and voices, plays on your speaker if you want, and saves nothing to your guest list.
   - **🎙 Practice call**: you play the guest through your laptop mic (use earphones).
6. **Call & send**:
   - **🧪 Test: call only <guest>** makes one automatic call.
   - **▶ Call everyone not called yet** works through the whole list. You can **Skip** a guest or **Stop** at any time.
7. **Results**: outcome counts, full transcripts, questions Vaani couldn't answer, and a CSV download.

## How a call works
```
 Vaani ──tel: link──► Phone Link dialer ──Vaani clicks "Call"──► your Android phone dials the guest
 Phone Link in-call window:  "Calling" → timer "00:01"  = picked up   (read via UI Automation)
 Guest's voice:  phone ─Bluetooth─► Windows ─► Speaker (Realtek) ─loopback─► Vaani listens
 Vaani's voice:  Vaani ─► CABLE Input ═VB-Cable═► CABLE Output ─► Windows call mic ─Bluetooth─► guest
 Each turn:  listen (VAD) → Gemini (hears the audio, picks a reply, JSON) → your recording / edge-tts → speak
 End:        Vaani clicks "End call" in Phone Link and saves the outcome, or notices the guest hung up.
```
| Piece | What it uses | Cost |
|---|---|---|
| Phone line | Your SIM through Windows Phone Link | your normal call plan |
| Audio bridge | VB-Audio Virtual Cable | free |
| Understanding and replies | Gemini `gemini-flash-lite-latest`, free tier | free (rate-limited) |
| Voice | Your recordings, plus Microsoft neural voices via `edge-tts` | free |
| Automation | `pywinauto` (Phone Link), `pycaw` (Windows audio checks) | free |

## Required Windows audio settings
- **Communications microphone = CABLE Output.** Press Win+R → `mmsys.cpl` → Recording → right-click CABLE Output → *Set as Default Communication Device*. Vaani checks this before every call and refuses to dial if it's wrong, because the guest would hear silence.
- **Your laptop mic must not be muted**, or your recordings and practice calls are silent. Vaani warns you about this, rejects silent recordings when you save them, and on calls skips any silent recording and uses the AI voice instead.
- After a calling session, set your normal mic back if you need it for Meet or Zoom.

## Limits
- **Half-duplex.** Vaani doesn't listen while it speaks, so keep recordings short (under 20 s).
- **Speed:** Vaani greets about 1.3 s after pickup, because the greeting is prepared while the phone rings. Replies take about 3–5 s (Gemini hearing the audio), with a short "jī / achha" filler meanwhile.
- **Live replies use an AI voice, not yours.** Cloning your voice live needs a GPU or a paid service.
- **Phone Link's window comes to the front** when Vaani dials, because it needs a real click on the Call button. Don't use the mouse during the 1–2 seconds it takes to dial.

## Calling rules (India)
- Vaani tells people it is an AI assistant in the opening. Keep that line.
- Calls are blocked outside the hours set in Settings.
- "Don't call me" saves the contact as **do not call**, and Vaani never dials them again.
- **Commercial cold calling from a personal 10-digit SIM is against TRAI rules.** Numbers get disconnected and blacklisted. For sales calls, use a registered telemarketing number (140-series or a DLT-registered provider).

## Testing
```
python -m unittest discover -s tests -v      # 38 tests, no phone, mic or key needed, never touches your data
python tests/full_test_call.py tricky --aloud  # full AI-guest test call (curious|tricky|busy|declines|wrong)
python tests/diag_audio.py 3                 # live: which program uses which audio device
python tests/diag_call.py 91XXXXXXXXXX       # live: dials, asks "can you hear me?", reports what came back
```
Every call is logged in `data/vaani.log`.
