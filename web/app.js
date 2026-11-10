// Vaani front-end. Plain JS, talks to server.py on localhost.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let S = null;                 // server state
let picker = [], picked = new Set();
let current = null;           // contact selected in the console
let poll = null;

const api = {
  async get(p) { const r = await fetch(p); return r.json(); },
  async post(p, body, raw) {
    const r = await fetch(p, { method: "POST", headers: raw ? {} : { "Content-Type": "application/json" }, body: raw ? body : JSON.stringify(body) });
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.error || r.statusText); }
    return r;
  },
};

function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add("hidden"), ms);
}
function debounce(fn, ms = 500) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ---------------- helpers ----------------
function normalize(raw) {
  const cc = (S.settings.country_code || "91").replace(/\D/g, "");
  let d = String(raw).replace(/[^\d+]/g, "");
  if (d.startsWith("+")) return d.slice(1);
  if (d.startsWith("00")) return d.slice(2);
  d = d.replace(/^0+/, "");
  return d.length === 10 ? cc + d : d;
}
function fill(tpl, c) {
  const owner = S.settings.owner || "the host";
  return (tpl || "").replace(/\{first\}/g, c.first || c.name.split(" ")[0]).replace(/\{name\}/g, c.name).replace(/\{owner\}/g, owner);
}
const slug = s => String(s || "").toLowerCase().replace(/\s+/g, "-");
const outcomeChip = o => o ? `<span class="oc ${slug(o)}">${esc(o)}</span>` : "";
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);

// ---------------- navigation ----------------
function go(page) {
  $$(".side a").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  $$(".page").forEach(p => p.classList.toggle("hidden", p.id !== "page-" + page));
  ({ home: renderHome, contacts: renderGuests, message: renderPreview, voice: renderVoice,
     practice: renderPractice, console: renderConsole, results: renderResults, settings: renderSettings })[page]?.();
  try { localStorage.setItem("vaani.page", page); } catch {}
}
$$(".side a").forEach(a => a.onclick = () => go(a.dataset.page));

async function refresh() {
  S = await api.get("/api/state");
  const ready = {
    contacts: S.contacts.length > 0,
    message: !!S.settings.owner && !/<EVENT>/.test(S.campaign.facts + S.campaign.opening),
    voice: true,
    settings: S.has_key,
  };
  ready.voiceRec = S.recordings.length > 0;
  for (const k of ["contacts", "message", "settings"]) $("#chk-" + k).className = "chk " + (ready[k] ? "ok" : "no");
  $("#chk-voice").className = "chk " + (ready.voiceRec ? "ok" : "");
  S.ready = ready;
}

// ---------------- home ----------------
function renderHome() {
  $("#helloOwner").textContent = S.settings.owner ? ", " + S.settings.owner : "";
  const steps = [
    ["settings", S.has_key, "Add free Gemini key", "Vaani's brain. Takes 1 minute."],
    ["contacts", S.ready.contacts, `Guest list${S.contacts.length ? ` · ${S.contacts.length}` : ""}`, "Import from Gmail or type names."],
    ["message", S.ready.message, "Write the message", "Event details, opening line, WhatsApp text."],
    ["voice", S.ready.voiceRec, "Record your voice", "Optional but makes calls feel personal."],
    ["practice", null, "Practice call", "Talk to Vaani yourself before calling anyone."],
    ["console", null, "Call & send", "Go through guests: WhatsApp, SMS, AI call."],
  ];
  $("#homeSteps").innerHTML = steps.map(([p, ok, t, d]) => `
    <div class="step" data-go="${p}"><span class="st ${ok === true ? "ok" : ok === false ? "no" : ""}">${ok === true ? "✓ Done" : ok === false ? "To do" : "Ready when you are"}</span>
    <b>${t}</b><span class="muted small">${d}</span></div>`).join("");
  $$("#homeSteps .step").forEach(s => s.onclick = () => go(s.dataset.go));
}

// ---------------- contacts ----------------
function parseCSV(text) {
  const rows = []; let row = [], f = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { f += '"'; i++; } else q = false; } else f += c; }
    else if (c === '"') q = true;
    else if (c === ",") { row.push(f); f = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(f); rows.push(row); row = []; f = ""; }
    else f += c;
  }
  if (f || row.length) { row.push(f); rows.push(row); }
  return rows.filter(r => r.some(x => x.trim()));
}

// Works with the current Google CSV (First Name / Labels) and the older one (Name / Group Membership)
function contactsFromCSV(text) {
  const rows = parseCSV(text.replace(/^﻿/, ""));
  const head = rows.shift().map(h => h.trim().toLowerCase());
  const col = (...names) => { for (const n of names) { const i = head.indexOf(n); if (i >= 0) return i; } return -1; };
  const phoneCols = head.map((h, i) => /phone.*value|mobile phone|^phone$|home phone|business phone/.test(h) ? i : -1).filter(i => i >= 0);
  const iName = col("name"), iFirst = col("first name", "given name"), iMid = col("middle name", "additional name"),
        iLast = col("last name", "family name"), iNick = col("nickname"), iOrg = col("organization name", "organization 1 - name"),
        iFile = col("file as"), iLab = col("labels", "group membership");
  const g = (r, i) => (i >= 0 && r[i] ? r[i].trim() : "");
  return rows.map(r => {
    const first = g(r, iFirst);
    const name = g(r, iName) || [first, g(r, iMid), g(r, iLast)].filter(Boolean).join(" ") || g(r, iFile) || g(r, iNick) || g(r, iOrg);
    const phones = [];
    phoneCols.forEach(i => g(r, i).split(":::").map(p => p.trim()).filter(Boolean).forEach(p => phones.includes(p) || phones.push(p)));
    const labels = g(r, iLab).split(":::").map(l => l.replace(/^\*\s*/, "").trim()).filter(l => l && !/^(mycontacts|starred)$/i.test(l));
    return { name: name || phones[0] || "", first: first || (name || "").split(/\s+/)[0], phones, labels };
  }).filter(c => c.name && c.phones.length).sort((a, b) => a.name.localeCompare(b.name));
}

function makeGuest(c, phone) {
  return { id: "g" + normalize(phone), name: c.name, first: c.first || c.name.split(" ")[0], phone, phones: c.phones || [phone], labels: c.labels || [] };
}

async function saveGuests(list) {
  const seen = new Set();
  const clean = list.filter(g => !seen.has(g.id) && seen.add(g.id));
  await api.post("/api/contacts", { contacts: clean });
  await refresh(); renderGuests();
}

function loadFile(file) {
  const rd = new FileReader();
  rd.onload = () => {
    try {
      picker = contactsFromCSV(rd.result); picked = new Set();
      $("#importInfo").innerHTML = `✓ Found <b>${picker.length}</b> contacts with a phone number. Tick the ones to invite below.`;
      $("#pickerCard").classList.remove("hidden");
      const labs = [...new Set(picker.flatMap(c => c.labels))].sort();
      $("#pickLabel").innerHTML = `<option value="">All labels</option>` + labs.map(l => `<option>${esc(l)}</option>`).join("");
      renderPicker();
    } catch (e) { $("#importInfo").textContent = "Couldn't read that file. Use Export → Google CSV."; }
  };
  rd.readAsText(file);
}
$("#csvFile").onchange = e => e.target.files[0] && loadFile(e.target.files[0]);
const drop = $("#drop");
drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); e.dataTransfer.files[0] && loadFile(e.dataTransfer.files[0]); };

function pickVisible() {
  const q = $("#pickSearch").value.trim().toLowerCase(), lab = $("#pickLabel").value, qd = q.replace(/\D/g, "");
  return picker.map((c, i) => ({ ...c, i })).filter(c =>
    (!q || c.name.toLowerCase().includes(q) || (qd && c.phones.join("").replace(/\D/g, "").includes(qd))) && (!lab || c.labels.includes(lab)));
}
function renderPicker() {
  const vis = pickVisible();
  $("#pickList").innerHTML = vis.slice(0, 1000).map(c => `
    <label class="item"><input type="checkbox" data-i="${c.i}" ${picked.has(c.i) ? "checked" : ""}>
    <div><div class="nm">${esc(c.name)} ${c.labels.map(l => `<span class="tag">${esc(l)}</span>`).join(" ")}</div><div class="ph">${esc(c.phones.join(", "))}</div></div></label>`).join("")
    || `<div class="empty">No contacts match.</div>`;
  $("#pickCount").textContent = `${picked.size} ticked`;
}
$("#pickList").onchange = e => { const i = +e.target.dataset.i; e.target.checked ? picked.add(i) : picked.delete(i); $("#pickCount").textContent = `${picked.size} ticked`; };
$("#pickSearch").oninput = renderPicker; $("#pickLabel").onchange = renderPicker;
$("#pickAll").onclick = () => { pickVisible().forEach(c => picked.add(c.i)); renderPicker(); };
$("#matchNames").onclick = () => {
  const names = $("#guestNames").value.split("\n").map(s => s.trim().toLowerCase()).filter(Boolean), miss = [];
  let n = 0;
  names.forEach(nm => {
    let hits = picker.map((c, i) => ({ c, i })).filter(x => x.c.name.toLowerCase() === nm);
    if (!hits.length) hits = picker.map((c, i) => ({ c, i })).filter(x => x.c.name.toLowerCase().includes(nm));
    if (!hits.length) miss.push(nm);
    hits.forEach(x => { if (!picked.has(x.i)) n++; picked.add(x.i); });
  });
  renderPicker();
  $("#matchInfo").innerHTML = `Ticked ${n}.` + (miss.length ? ` <span style="color:var(--danger)">Not found: ${esc(miss.join(", "))}</span>` : "");
};
$("#addPicked").onclick = async () => {
  const add = [...picked].map(i => makeGuest(picker[i], picker[i].phones[0]));
  if (!add.length) return toast("Tick some contacts first");
  await saveGuests([...S.contacts, ...add]);
  picked = new Set(); renderPicker(); toast(`Added ${add.length} to the guest list`);
};
$("#addManual").onclick = async () => {
  const add = $("#manualList").value.split("\n").map(l => l.trim()).filter(Boolean).map(l => {
    const m = l.match(/^(.*?)[,\t;]\s*([+\d][\d\s\-()]{6,})$/) || l.match(/^(.*?)\s+([+\d][\d\s\-()]{6,})$/);
    return m ? makeGuest({ name: m[1].trim() || m[2], phones: [m[2].trim()] }, m[2].trim()) : null;
  });
  const bad = add.filter(x => !x).length, good = add.filter(Boolean);
  if (good.length) { await saveGuests([...S.contacts, ...good]); $("#manualList").value = ""; }
  toast(`Added ${good.length}` + (bad ? ` · ${bad} line(s) had no phone number` : ""));
};
$("#clearGuests").onclick = async () => { if (confirm("Remove everyone from the guest list? (Call results are kept.)")) await saveGuests([]); };

function renderGuests() {
  $("#guestCount").textContent = S.contacts.length;
  $("#guestList").innerHTML = S.contacts.map(g => `
    <div class="item"><div><div class="nm">${esc(g.name)} ${outcomeChip(S.status[g.id]?.outcome)}</div><div class="ph">+${esc(normalize(g.phone))}</div></div>
    ${g.phones.length > 1 ? `<select data-phone="${g.id}" style="width:auto">${g.phones.map(p => `<option ${p === g.phone ? "selected" : ""}>${esc(p)}</option>`).join("")}</select>` : ""}
    <button class="ghost danger x" data-rm="${g.id}">Remove</button></div>`).join("") || `<div class="empty">No guests yet. Import from Gmail or add by hand above.</div>`;
  $$("[data-rm]").forEach(b => b.onclick = () => saveGuests(S.contacts.filter(g => g.id !== b.dataset.rm)));
  $$("[data-phone]").forEach(s => s.onchange = () => saveGuests(S.contacts.map(g => g.id === s.dataset.phone ? { ...g, phone: s.value } : g)));
}

// ---------------- message ----------------
const saveCampaign = debounce(async () => {
  const data = {};
  $$("[data-c]").forEach(el => data[el.dataset.c] = el.value);
  await api.post("/api/campaign", data);
  await refresh(); $("#savedMsg").textContent = "Saved ✓"; setTimeout(() => $("#savedMsg").textContent = "", 1500);
});
$$("[data-c]").forEach(el => el.addEventListener("input", () => { S.campaign[el.dataset.c] = el.value; renderPreview(); saveCampaign(); }));
$("#owner").addEventListener("input", debounce(async () => { await api.post("/api/settings", { owner: $("#owner").value.trim() }); await refresh(); renderPreview(); }));
$$("#langSeg button").forEach(b => b.onclick = async () => {
  await api.post("/api/campaign", { language: b.dataset.v });
  const voice = b.dataset.v === "English" ? "en-IN-PrabhatNeural" : "hi-IN-MadhurNeural";
  if ((b.dataset.v === "English") !== S.settings.voice.startsWith("en-")) await api.post("/api/settings", { voice });
  await refresh(); fillMessageForm();
});
function fillMessageForm() {
  $$("[data-c]").forEach(el => { if (document.activeElement !== el) el.value = S.campaign[el.dataset.c] ?? ""; });
  if (document.activeElement !== $("#owner")) $("#owner").value = S.settings.owner;
  $$("#langSeg button").forEach(b => b.classList.toggle("on", b.dataset.v === S.campaign.language));
  renderPreview();
  renderFaqs();
}

// ---------------- cross-questions (Q&A) ----------------
const FAQ_IDEAS = [
  "Shaadi kab hai? (date)", "Kiski shaadi hai? (dulha / dulhan ka naam)", "Venue kahan hai? (address)",
  "Location / Google Maps link?", "Kaun kaun se functions hain? (haldi, mehndi, sangeet, phere)", "Kitne baje aana hai?",
  "Family ke saath aa sakte hain?", "Rukne ka intezaam hai? (stay)", "Station / airport se pickup milega?",
  "Dress code kya hai?", "Kisse contact karein? (number)", "Card kab milega?",
];
const saveFaqs = debounce(async () => {
  await api.post("/api/campaign", { faqs: S.campaign.faqs });
  $("#savedFaq").textContent = "Saved ✓"; setTimeout(() => $("#savedFaq").textContent = "", 1500);
  renderFaqCount();
});
function renderFaqCount() {
  $("#faqCount").textContent = `${(S.campaign.faqs || []).filter(f => f.q.trim() && f.a.trim()).length} answered`;
}
function addFaq(q, a = "") {
  S.campaign.faqs = S.campaign.faqs || [];
  if (S.campaign.faqs.some(f => f.q.trim().toLowerCase() === q.trim().toLowerCase())) return toast("Already in the list");
  S.campaign.faqs.push({ q, a }); renderFaqs(); saveFaqs();
  $$("#faqList textarea[data-f=a]").at(-1)?.focus();
}
function renderFaqs() {
  const faqs = S.campaign.faqs || [];
  const have = new Set(faqs.map(f => f.q));
  $("#faqSuggest").innerHTML = `<span class="muted small">Common questions:</span>` + FAQ_IDEAS.filter(q => !have.has(q)).map(q => `<button data-idea="${esc(q)}">+ ${esc(q)}</button>`).join("");
  $$("#faqSuggest [data-idea]").forEach(b => b.onclick = () => addFaq(b.dataset.idea));
  $("#faqList").innerHTML = faqs.map((f, i) => `
    <div class="faq ${f.a.trim() ? "" : "empty-a"}">
      <textarea data-f="q" data-i="${i}" rows="1" placeholder="Question they might ask">${esc(f.q)}</textarea>
      <textarea data-f="a" data-i="${i}" rows="1" placeholder="Answer Vaani should give (leave empty if not decided yet)">${esc(f.a)}</textarea>
      <button class="ghost danger" data-fdel="${i}" title="Remove">✕</button>
    </div>`).join("") || `<div class="empty">No questions yet. Tap a common question above or add your own.</div>`;
  $$("#faqList textarea").forEach(t => t.oninput = () => {
    S.campaign.faqs[+t.dataset.i][t.dataset.f] = t.value;
    t.closest(".faq").classList.toggle("empty-a", !S.campaign.faqs[+t.dataset.i].a.trim());
    saveFaqs();
  });
  $$("#faqList [data-fdel]").forEach(b => b.onclick = () => { S.campaign.faqs.splice(+b.dataset.fdel, 1); renderFaqs(); saveFaqs(); });
  renderFaqCount();
}
$("#faqAdd").onclick = () => { (S.campaign.faqs = S.campaign.faqs || []).push({ q: "", a: "" }); renderFaqs(); $$("#faqList textarea[data-f=q]").at(-1).focus(); };
function renderPreview() {
  const g = S.contacts[0] || { name: "Rahul Sharma", first: "Rahul" };
  $("#pvName").textContent = g.name;
  $("#pvOpening").textContent = fill(S.campaign.opening, g);
  const n = fill(S.campaign.sms_template, g).length;
  $("#smsLen").textContent = `(${n} chars${n > 160 ? ` → ${Math.ceil(n / 153)} SMS` : ""})`;
}

// ---------------- voice ----------------
function pronWords() {
  const words = {};
  $$("#pronList .pron").forEach(r => { const w = $("[data-w]", r).value.trim(), s = $("[data-s]", r).value.trim(); if (w) words[w] = s; });
  return words;
}
const savePron = debounce(async () => {
  const words = pronWords();
  await api.post("/api/campaign", { pronunciations: words });
  S.campaign.pronunciations = words;
  $("#savedPron").textContent = "Saved ✓"; setTimeout(() => $("#savedPron").textContent = "", 1500);
});
function renderPron() {
  const words = Object.entries(S.campaign.pronunciations || {});
  $("#pronList").innerHTML = words.map(([w, s]) => `<div class="row pron" style="margin-top:6px">
      <input data-w value="${esc(w)}" placeholder="Name as written (e.g. Rewari)" style="width:200px">
      <span class="muted">→</span><input data-s value="${esc(s)}" placeholder="Hindi spelling (e.g. रेवाड़ी)" style="width:200px">
      <button data-hear>▶</button><button class="ghost danger" data-prdel>✕</button></div>`).join("");
  $$("#pronList input").forEach(i => i.oninput = savePron);
  $$("#pronList [data-prdel]").forEach(b => b.onclick = () => { b.closest(".pron").outerHTML = ""; savePron(); });
  $$("#pronList [data-hear]").forEach(b => b.onclick = async () => {
    const r = b.closest(".pron"), word = $("[data-w]", r).value.trim(), say = $("[data-s]", r).value.trim();
    try {   // the server applies the saved dictionary, so send the Hindi spelling directly for an instant preview
      const res = await api.post("/api/tts", { text: `${say || word} ji, namaste.`, voice: S.settings.voice });
      new Audio(URL.createObjectURL(await res.blob())).play();
    } catch (e) { toast("Couldn't play: " + e.message); }
  });
}
$("#pronAdd").onclick = () => { S.campaign.pronunciations = { ...pronWords(), "": "" }; renderPron(); $$("#pronList [data-w]").at(-1).focus(); };

let recState = null;
function renderVoice() {
  renderPron();
  const lib = S.phrases || [];
  $("#libCount").textContent = `${lib.filter(p => p.usable).length} of ${lib.length} recorded`;
  $("#library").innerHTML = lib.map(p => {
    const busy = recState?.slot === p.id, silent = p.recorded && !p.usable;
    return `<div class="rec">
      <div class="grow"><div class="small muted">${esc(p.label)}</div><div class="say">“${esc(p.text)}”</div></div>
      ${busy ? `<span class="dot"></span><div class="meter"><div id="recLevel"></div></div><button class="primary" data-stop>■ Stop</button>`
             : `<button data-rec="${p.id}">● ${p.recorded ? "Re-record" : "Record"}</button>
                ${p.recorded ? `<button data-play="${p.id}">▶</button><button class="ghost danger" data-del="${p.id}" title="Delete">✕</button>` : ""}
                ${silent ? `<span class="small" style="color:var(--danger)">⚠ silent, re-record</span>` : p.usable ? `<span class="ok-tick">✓ your voice</span>` : ``}`}
    </div>`;
  }).join("");
  const muted = S.audio_check?.muted_mics || [];
  $("#micWarn").innerHTML = muted.length ? `⚠ Windows has <b>${esc(muted.join(", "))}</b> muted, so recordings will be silent.
    Open Windows Settings → System → Sound → Input → choose it → unmute and raise the volume, then reload this page.` : "";
  $("#micWarn").classList.toggle("hidden", !muted.length);
  $$("[data-rec]").forEach(b => b.onclick = () => startRec(b.dataset.rec));
  $$("[data-stop]").forEach(b => b.onclick = stopRec);
  $$("[data-play]").forEach(b => b.onclick = () => new Audio(`/api/recording/${b.dataset.play}?t=${Date.now()}`).play());
  $$("[data-del]").forEach(b => b.onclick = async () => { await fetch(`/api/recording/${b.dataset.del}`, { method: "DELETE" }); await refresh(); renderVoice(); });
  $("#voiceList").innerHTML = S.voices.map(v => `<label><input type="radio" name="voice" value="${v.id}" ${v.id === S.settings.voice ? "checked" : ""}> ${esc(v.label)}</label>`).join("");
  $$("#voiceList input").forEach(r => r.onchange = async () => { await api.post("/api/settings", { voice: r.value }); await refresh(); });
  $$("#rateSeg button").forEach(b => { b.classList.toggle("on", b.dataset.v === S.settings.voice_rate); b.onclick = async () => { await api.post("/api/settings", { voice_rate: b.dataset.v }); await refresh(); renderVoice(); }; });
}
async function startRec(slot) {
  try {
    const opts = { echoCancellation: true, noiseSuppression: true };
    let stream = await navigator.mediaDevices.getUserMedia({ audio: opts });
    // after installing VB-Cable, Windows may make "CABLE Output" the default mic, which records silence
    if (/cable/i.test(stream.getAudioTracks()[0].label)) {
      const real = (await navigator.mediaDevices.enumerateDevices()).find(d => d.kind === "audioinput" && !/cable/i.test(d.label) && d.deviceId !== "default" && d.deviceId !== "communications");
      if (real) { stream.getTracks().forEach(t => t.stop()); stream = await navigator.mediaDevices.getUserMedia({ audio: { ...opts, deviceId: { exact: real.deviceId } } }); }
    }
    const ctx = new AudioContext(), src = ctx.createMediaStreamSource(stream), proc = ctx.createScriptProcessor(4096, 1, 1), chunks = [];
    proc.onaudioprocess = e => {
      const d = new Float32Array(e.inputBuffer.getChannelData(0));
      chunks.push(d);
      const lvl = Math.sqrt(d.reduce((s, x) => s + x * x, 0) / d.length);
      $("#recLevel") && ($("#recLevel").style.width = Math.min(100, lvl * 800) + "%");
    };
    src.connect(proc); proc.connect(ctx.destination);
    recState = { slot, stream, ctx, proc, chunks };
    renderVoice();
  } catch (e) { toast("Microphone not available: " + e.message); }
}
async function stopRec() {
  const { slot, stream, ctx, proc, chunks } = recState; recState = null;
  proc.disconnect(); stream.getTracks().forEach(t => t.stop());
  const wav = encodeWav(chunks, ctx.sampleRate); ctx.close();
  try {
    const r = await (await api.post(`/api/recording/${slot}`, wav, true)).json();
    toast(`Saved your recording (${r.seconds}s)` + (r.seconds > 20 ? ". Tip: keep it under 20 s; Vaani can't listen while it plays" : ""), 5000);
  } catch (e) { toast(e.message, 9000); }
  await refresh(); renderVoice();
}
function encodeWav(chunks, rate) {
  const len = chunks.reduce((n, c) => n + c.length, 0), buf = new ArrayBuffer(44 + len * 2), v = new DataView(buf);
  const w = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + len * 2, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, len * 2, true);
  let o = 44;
  for (const c of chunks) for (const s of c) { v.setInt16(o, Math.max(-1, Math.min(1, s)) * 32767, true); o += 2; }
  return new Blob([buf], { type: "audio/wav" });
}
$("#previewVoice").onclick = async () => {
  $("#pvStatus").textContent = "Generating…";
  try {
    const g = S.contacts[0] || { name: "Rahul Sharma", first: "Rahul" };
    const r = await api.post("/api/tts", { text: fill(S.campaign.opening, g), voice: S.settings.voice });
    new Audio(URL.createObjectURL(await r.blob())).play(); $("#pvStatus").textContent = "";
  } catch (e) { $("#pvStatus").textContent = "Couldn't generate (needs internet): " + e.message; }
};

// ---------------- live call view (shared by practice + console) ----------------
const STATE_TEXT = { starting: "Starting…", reconnecting: "Reconnecting your phone…", dialing: "Dialling…", waiting: "Ringing", speaking: "Vaani is speaking", listening: "Listening…",
  thinking: "Thinking…", stopping: "Ending…", done: "Call finished", error: "Problem", idle: "" };

function liveHTML(v) {
  const head = `<div class="card"><div class="callhead">
    <span class="who-big">${esc(v.contact?.name || "")}</span><span class="state ${v.state}">${STATE_TEXT[v.state] || v.state}</span>
    ${v.state === "listening" ? `<div class="meter"><div style="width:${v.level * 100}%"></div></div>` : ""}
    <span class="muted">${Math.floor(v.seconds / 60)}:${String(v.seconds % 60).padStart(2, "0")}</span>
    ${["done", "error"].includes(v.state) ? "" : `<button class="danger" id="endCall" style="margin-left:auto">■ End <kbd>Esc</kbd></button>`}</div>
    ${v.state === "waiting" || v.state === "dialing" ? `<div class="bigask" style="margin-top:14px">📞 Ringing <b>+${esc(v.number)}</b>… Vaani starts by itself when they pick up.
       <div class="muted small" style="margin-top:6px">If they answered and Vaani stays silent, press <button id="pickedUp">They picked up <kbd>Space</kbd></button></div></div>` : ""}
    ${v.route === "bluetooth" ? `<p class="muted small">🔵 Talking through the phone's Bluetooth call audio</p>` : v.route === "cable" ? `<p class="muted small">🎚 Talking through the virtual cable</p>` : v.route === "simulation" ? `<p class="muted small">🤖 Full test call with an AI guest (nobody is dialled)</p>` : ""}
    ${v.error ? `<p style="color:var(--danger)">⚠ ${esc(v.error)}</p>` : ""}
    ${v.pc_audio === false && ["speaking", "listening", "thinking"].includes(v.state) ? `<p style="color:var(--danger)">⚠ The call's audio is still on your phone, so the guest can't hear Vaani.
       In the Phone Link call window, switch audio to <b>PC</b> (or see Settings → Phone bridge).</p>` : ""}
    ${v.state === "done" ? `<p>Outcome: ${outcomeChip(v.outcome)} ${v.note ? `<span class="muted">— ${esc(v.note)}</span>` : ""}</p>` : ""}
    </div>`;
  const a = v.auto;
  const auto = a ? `<div class="card"><div class="row between"><h3>Auto-calling · ${a.state === "done" ? "finished" : `call ${a.index} of ${a.total}`}</h3>
      ${a.state !== "done" ? `<div class="row"><button id="skipCall">Skip this guest</button><button class="danger" id="stopAuto">■ Stop auto-calling</button></div>` : ""}</div>
      <div class="progress"><div style="width:${a.total ? (a.log.length / a.total * 100) : 0}%"></div></div>
      ${a.log.map(x => `<div class="row between" style="padding:3px 0"><span>${esc(x.name)}</span>${outcomeChip(x.outcome)}${x.error ? `<span class="small" style="color:var(--danger)">${esc(x.error)}</span>` : ""}</div>`).join("")}</div>` : "";
  const tr = v.transcript?.length ? `<div class="card"><div class="transcript">${v.transcript.map(t =>
    `<div class="bubble ${t.role}"><span class="who">${t.role === "agent" ? "Vaani" : esc(v.contact?.first || "Them")}</span>${esc(t.text || "(no words heard)")}</div>`).join("")}</div></div>` : "";
  return auto + head + tr;
}

function watchLive(target, onDone) {
  clearInterval(poll);
  const tick = async () => {
    const v = await api.get("/api/live");
    if (v.state === "idle") return;
    const box = $(target);
    box.innerHTML = liveHTML(v);
    const tr = $(".transcript", box); if (tr) tr.scrollTop = tr.scrollHeight;
    $("#endCall", box) && ($("#endCall", box).onclick = () => api.post("/api/call/stop", {}));
    $("#pickedUp", box) && ($("#pickedUp", box).onclick = () => api.post("/api/call/answered", {}));
    $("#stopAuto", box) && ($("#stopAuto", box).onclick = () => api.post("/api/auto/stop", {}));
    $("#skipCall", box) && ($("#skipCall", box).onclick = () => api.post("/api/call/skip", {}));
    const sessionOver = ["done", "error"].includes(v.state);
    if (v.auto && sessionOver && v.state !== watchLive.last) { await refresh(); renderConsoleQueue(); }
    watchLive.last = v.state;
    if (sessionOver && (!v.auto || v.auto.state === "done")) { clearInterval(poll); poll = null; await refresh(); onDone?.(v); }
  };
  tick(); poll = setInterval(tick, 400);
}

document.addEventListener("keydown", e => {
  if (!poll || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (e.code === "Space" && $("#pickedUp")) { e.preventDefault(); api.post("/api/call/answered", {}); }
  if (e.code === "Escape") api.post("/api/call/stop", {});
});

async function startCall(contact, mode, target, onDone, extra = {}) {
  if (!S.has_key) { toast("Add your free Gemini key in Settings first"); return go("settings"); }
  try { await api.post("/api/call/start", { contact, mode, ...extra }); watchLive(target, onDone); }
  catch (e) { toast(e.message, 5000); }
}

// ---------------- practice ----------------
function renderPractice() {
  const muted = S.audio_check?.muted_mics || [];
  $("#practiceWarn").innerHTML = muted.length ? `⚠ <b>${esc(muted.join(", "))}</b> is muted in Windows, so Vaani won't hear you.
    Windows Settings → System → Sound → Input → choose it → unmute and raise the volume.` : "";
  $("#practiceWarn").classList.toggle("hidden", !muted.length);
  const opts = [{ id: "test", name: "Test Person", first: "Test", phone: "" }, ...S.contacts];
  $("#practiceAs").innerHTML = opts.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  $("#practiceStart").onclick = () => {
    const c = opts.find(o => o.id === $("#practiceAs").value);
    startCall(c, "practice", "#practiceLive");
  };
  $("#simPersona").innerHTML = Object.entries(S.personas || {}).map(([k, label]) => `<option value="${k}">${esc(label)}</option>`).join("");
  $("#simStart").onclick = () => startCall({ id: "test", name: "Ramesh Chacha", first: "Ramesh", phone: "" }, "simulate", "#practiceLive",
    null, { persona: $("#simPersona").value, aloud: $("#simAloud").checked });
}

// ---------------- console ----------------
function queueItems() {
  const f = $("#qFilter").value, q = $("#qSearch").value.trim().toLowerCase();
  const todo = new Set(S.pending);     // the server decides who still needs a call
  return S.contacts.filter(g => (f === "all" || (f === "todo" ? todo.has(g.id) : !todo.has(g.id))) && (!q || g.name.toLowerCase().includes(q)));
}
function renderConsoleQueue() {
  const items = queueItems();
  $("#qList").innerHTML = items.map(g => {
    const st = S.status[g.id] || {};
    return `<div class="q ${current?.id === g.id ? "sel" : ""}" data-id="${g.id}"><div>${esc(g.name)}<br><small>${st.wa ? "WA ✓ " : ""}${st.sms ? "SMS ✓" : ""}</small></div>${outcomeChip(st.outcome)}</div>`;
  }).join("") || `<div class="empty">${S.contacts.length ? "Nobody left here 🎉" : "Add guests first"}</div>`;
  $$("#qList .q").forEach(el => el.onclick = () => { current = S.contacts.find(g => g.id === el.dataset.id); renderConsole(); });
}
function renderConsole() {
  const items = queueItems();
  if (!current || !S.contacts.find(g => g.id === current.id)) current = items[0] || S.contacts[0] || null;
  renderConsoleQueue();
  renderStage();
}
$("#qFilter").onchange = renderConsole; $("#qSearch").oninput = renderConsole;

function renderStage() {
  const box = $("#stageCard");
  if (!current) { box.innerHTML = `<div class="empty">Add guests on the Contacts page, then come back here.</div>`; $("#phoneLive").innerHTML = ""; return; }
  const g = current, st = S.status[g.id] || {}, num = normalize(g.phone);
  const wa = `https://wa.me/${num}?text=${encodeURIComponent(fill(S.campaign.wa_template, g))}`;
  const sms = `sms:+${num}${isIOS ? "&" : "?"}body=${encodeURIComponent(fill(S.campaign.sms_template, g))}`;
  const dnc = st.outcome === "do not call";
  const outcomes = S.campaign.outcomes.split(",").map(s => s.trim()).filter(Boolean);
  box.innerHTML = `
    <div class="row between"><div><div class="who-big">${esc(g.name)}</div><div class="muted">+${esc(num)}</div></div>
      <div class="row"><span class="muted small">Outcome</span><select id="setOutcome"><option value="">—</option>${[...new Set([...outcomes, st.outcome].filter(Boolean))].map(o => `<option ${o === st.outcome ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></div></div>
    <div class="actions3">
      <a class="btn wa ${st.wa ? "done" : ""}" href="${wa}" target="_blank" rel="noopener" id="doWa">WhatsApp</a>
      <a class="btn sms ${st.sms ? "done" : ""}" href="${sms}" id="doSms">SMS</a>
      <button class="btn call" id="doCall" ${dnc ? "disabled title='Marked do not call'" : ""}>📞 AI call</button>
    </div>
    <p class="muted small">${dnc ? "⛔ This person asked not to be called." : S.phonelink ? "AI call dials from your phone via Phone Link." : "⚠ Phone Link isn't open. Open it and link your phone (Settings → Phone bridge). WhatsApp/SMS and Practice call still work."}
      ${st.note ? `<br>📝 ${esc(st.note)}` : ""}</p>
    <div class="row end"><button id="nextGuest">Next guest →</button></div>`;
  const todo = S.pending.length;
  $("#autoCard").innerHTML = `<h3>🤖 Auto-calling</h3>
    <p class="muted small" style="margin:0">Vaani dials each guest, waits for pickup, talks, saves the outcome, hangs up and moves on, with no clicks from you.
      ${S.phonelink ? "" : "<br>⚠ Phone Link isn't open. Vaani will open it."}
      ${S.audio_check?.comms_ok === false ? `<br><b style="color:var(--danger)">⚠ Guests won't hear Vaani: Windows' communications mic is "${esc(S.audio_check.comms_mic)}".
        Press Win+R → mmsys.cpl → Recording → right-click CABLE Output → Set as Default Communication Device.</b>` : ""}
      ${S.silent_recordings.length ? `<br>⚠ Your ${S.silent_recordings.join(" & ")} recording is silent, so Vaani uses the AI voice for it (re-record on the Voice page).` : ""}</p>
    <div class="row" style="margin-top:10px">
      <button id="testOne" ${dnc ? "disabled" : ""}>🧪 Test: call only ${esc(g.name)}</button>
      <button class="primary" id="autoAll" ${todo ? "" : "disabled"}>▶ Call everyone not called yet (${todo})</button>
    </div>`;
  $("#testOne").onclick = () => startAuto([g.id]);
  $("#autoAll").onclick = () => { if (confirm(`Vaani will now call ${todo} guest(s) one after another. Start?`)) startAuto(null); };
  $("#doWa").onclick = () => mark(g.id, "wa", true);
  $("#doSms").onclick = () => mark(g.id, "sms", true);
  $("#doCall").onclick = () => startCall(g, "phone", "#phoneLive", () => renderConsole());
  $("#setOutcome").onchange = e => mark(g.id, "outcome", e.target.value);
  $("#nextGuest").onclick = () => {
    const items = queueItems(), i = items.findIndex(x => x.id === g.id);
    current = items[i + 1] || items.find(x => x.id !== g.id) || g; $("#phoneLive").innerHTML = ""; renderConsole();
  };
}
async function startAuto(ids) {
  if (!S.has_key) { toast("Add your free Gemini key in Settings first"); return go("settings"); }
  try { await api.post("/api/auto/start", ids ? { ids } : {}); watchLive("#phoneLive", () => renderConsole()); }
  catch (e) { toast(e.message, 5000); }
}
async function mark(id, field, value) {
  await api.post("/api/mark", { id, field, value });
  setTimeout(async () => { await refresh(); renderConsole(); }, 300);
}

// ---------------- results ----------------
async function renderResults() {
  const st = Object.values(S.status), counts = {};
  st.forEach(x => x.outcome && (counts[x.outcome] = (counts[x.outcome] || 0) + 1));
  const tiles = [["Guests", S.contacts.length], ["WhatsApp sent", st.filter(x => x.wa).length], ["SMS sent", st.filter(x => x.sms).length],
    ...Object.entries(counts).sort((a, b) => b[1] - a[1])];
  $("#stats").innerHTML = tiles.map(([l, n]) => `<div class="stat"><div class="n">${n}</div><div class="l">${esc(l)}</div></div>`).join("");
  const calls = (await api.get("/api/calls")).reverse();
  const known = new Set((S.campaign.faqs || []).filter(f => f.a.trim()).map(f => f.q.trim().toLowerCase()));
  const open = [...new Map(calls.flatMap(c => (c.unanswered || []).map(q => [q.trim().toLowerCase(), { q: q.trim(), who: c.name }])))
    .values()].filter(x => !known.has(x.q.toLowerCase()));
  $("#unansweredCard").classList.toggle("hidden", !open.length);
  $("#unansweredList").innerHTML = open.map((x, i) => `<div class="uq"><span class="qt">❓ ${esc(x.q)} <span class="muted small">(${esc(x.who)})</span></span>
      <input class="grow" data-ua="${i}" placeholder="Type the answer"><button class="primary" data-uadd="${i}">Add to Q&amp;A</button></div>`).join("");
  $$("[data-uadd]").forEach(b => b.onclick = async () => {
    const i = +b.dataset.uadd, a = $(`[data-ua="${i}"]`).value.trim();
    if (!a) return toast("Type the answer first");
    const faqs = S.campaign.faqs || [], ex = faqs.find(f => f.q.trim().toLowerCase() === open[i].q.toLowerCase());
    ex ? (ex.a = a) : faqs.push({ q: open[i].q, a });
    await api.post("/api/campaign", { faqs }); await refresh(); renderResults(); toast("Added. Vaani will answer this from now on");
  });
  $("#callLog").innerHTML = calls.map(c => `<details class="log"><summary><b>${esc(c.name)}</b> ${outcomeChip(c.outcome)}
      <span class="muted small">${esc(c.started.replace("T", " "))}</span>${c.note ? `<span class="muted small">— ${esc(c.note)}</span>` : ""}</summary>
      <div class="transcript">${(c.transcript || []).map(t => `<div class="bubble ${t.role}"><span class="who">${t.role === "agent" ? "Vaani" : esc(c.name)}</span>${esc(t.text || "(no words heard)")}</div>`).join("")}</div></details>`).join("")
    || `<div class="empty">No calls yet.</div>`;
}

// ---------------- settings ----------------
function renderSettings() {
  const s = S.settings, d = S.devices;
  $("#keyState").innerHTML = S.has_key ? `<span class="tag">✓ saved</span>` : "";
  $("#cableState").innerHTML = S.cable_found ? `<span class="tag">✓ cable found</span>` : `<span class="muted small">cable not installed yet</span>`;
  $("#model").value = s.gemini_model; $("#cc").value = s.country_code;
  $("#hLo").value = s.call_hours[0]; $("#hHi").value = s.call_hours[1];
  const opt = (list, cur, blank) => `<option value="">${blank}</option>` + list.map(n => `<option ${n === cur ? "selected" : ""}>${esc(n)}</option>`).join("");
  $("#outDev").innerHTML = opt(d.speakers || [], s.out_device, "Auto (CABLE Input)");
  $("#inDev").innerHTML = opt(d.speakers || [], s.in_device, `Default speaker (${d.default_speaker || ""})`);
}
$("#saveKey").onclick = async () => {
  const k = $("#keyInput").value.trim(); if (!k) return;
  await api.post("/api/key", { key: k }); $("#keyInput").value = ""; await refresh(); renderSettings(); toast("Key saved");
};
const saveSettings = debounce(async () => {
  await api.post("/api/settings", {
    gemini_model: $("#model").value.trim() || "gemini-flash-lite-latest", country_code: $("#cc").value.replace(/\D/g, "") || "91",
    call_hours: [+$("#hLo").value || 0, +$("#hHi").value || 24], out_device: $("#outDev").value, in_device: $("#inDev").value,
  });
  await refresh(); toast("Settings saved", 1200);
});
["#model", "#cc", "#hLo", "#hHi", "#outDev", "#inDev"].forEach(sel => $(sel).addEventListener("change", saveSettings));

// ---------------- boot ----------------
(async () => {
  await refresh();
  fillMessageForm();
  let page = "home"; try { page = localStorage.getItem("vaani.page") || "home"; } catch {}
  go(page);
  const live = await api.get("/api/live");   // re-attach to a call that is still running after a page reload
  if (!["idle", "done", "error"].includes(live.state)) { go(live.mode === "phone" ? "console" : "practice"); watchLive(live.mode === "phone" ? "#phoneLive" : "#practiceLive"); }
})();
