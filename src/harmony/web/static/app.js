"use strict";
// Harmony web client. Talks to the same HTTP API the mobile app will use; audio
// plays in the browser via <audio> pointed at the same-origin /stream proxy.

const $ = (id) => document.getElementById(id);
const audio = $("audio");

const state = {
  queue: [],      // list of track objects currently loaded (search or playlist)
  index: -1,      // index of the playing track within queue
  playlist: null, // {service, id, title} when viewing an editable playlist, else null
  target: "browser", // "browser" (this tab's <audio>) or a device host to cast to
  targetVia: null,   // peer "host:port" when the device lives on another instance's LAN
  devicePaused: false,
};

const onDevice = () => state.target !== "browser";
// A device target is encoded as "host" (local) or "host::peerhost:port" (federated),
// so a single <select> value or data-attr carries both.
const encodeTarget = (host, via) => (via ? `${host}::${via}` : host);
function setTargetValue(value) {
  const i = (value || "").indexOf("::");
  state.target = i >= 0 ? value.slice(0, i) : (value || "browser");
  state.targetVia = i >= 0 ? value.slice(i + 2) : null;
}
// Merge {via} into a device play/control body only when set.
const withVia = (body) => (state.targetVia ? { ...body, via: state.targetVia } : body);

const fmtTime = (s) => {
  if (!s || s < 0 || !isFinite(s)) return "0:00";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
};
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
const serviceLabel = (s) => ({ ytmusic: "YT Music", qobuz: "Qobuz" }[s] || s);

// -- in-page dialogs (replace native prompt/confirm/alert; touch-friendly) --

function toast(text, ok) {
  let t = $("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = text; t.className = "show" + (ok ? " ok" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 3000);
}

function _modal(inner) {
  const root = document.createElement("div");
  root.className = "modal-back";
  root.innerHTML = `<div class="modal">${inner}</div>`;
  document.body.appendChild(root);
  const close = () => root.remove();
  root.addEventListener("click", (e) => { if (e.target === root) close(); });
  return { root, close };
}

// Resolves to a string (single field), an object (when `fields` given), or null.
function modalPrompt({ title, label, value = "", placeholder = "", okText = "OK", fields }) {
  return new Promise((resolve) => {
    const flds = fields || [{ name: "value", label, value, placeholder, type: "text" }];
    const html = flds.map((f) => f.type === "select"
      ? `<label>${esc(f.label)}</label><select data-name="${esc(f.name)}">${(f.options || []).map((o) =>
          `<option value="${esc(o.value)}"${o.value === f.value ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`
      : `<label>${esc(f.label)}</label><input data-name="${esc(f.name)}" type="text" value="${esc(f.value || "")}" placeholder="${esc(f.placeholder || "")}" />`
    ).join("");
    const m = _modal(`<h2>${esc(title)}</h2>${html}<div class="modal-acts">` +
      `<button class="act ghost" data-x>Cancel</button><button class="act" data-ok>${esc(okText)}</button></div>`);
    const read = () => { const o = {}; m.root.querySelectorAll("[data-name]").forEach((el) => o[el.dataset.name] = el.value.trim()); return o; };
    const ok = () => { const o = read(); m.close(); resolve(fields ? o : o.value); };
    const cancel = () => { m.close(); resolve(null); };
    m.root.querySelector("[data-ok]").onclick = ok;
    m.root.querySelector("[data-x]").onclick = cancel;
    m.root.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); ok(); }
      else if (e.key === "Escape") { cancel(); }
    });
    const first = m.root.querySelector("[data-name]"); if (first) first.focus();
  });
}

function modalConfirm(title, body) {
  return new Promise((resolve) => {
    const m = _modal(`<h2>${esc(title)}</h2>${body ? `<p class="muted">${esc(body)}</p>` : ""}` +
      `<div class="modal-acts"><button class="act ghost" data-x>Cancel</button><button class="act" data-ok>OK</button></div>`);
    m.root.querySelector("[data-ok]").onclick = () => { m.close(); resolve(true); };
    m.root.querySelector("[data-x]").onclick = () => { m.close(); resolve(false); };
  });
}

let harmonyKey = "";
try { harmonyKey = localStorage.getItem("harmonyKey") || ""; } catch { harmonyKey = ""; }
const keyHeaders = (extra) => Object.assign(harmonyKey ? { "X-Harmony-Key": harmonyKey } : {}, extra || {});
const keyParam = () => (harmonyKey ? `?key=${encodeURIComponent(harmonyKey)}` : "");
async function promptKey() {
  const k = await modalPrompt({ title: "Personal key required",
    label: "This Harmony instance requires a personal key:", placeholder: "personal key" });
  if (k) { harmonyKey = k.trim(); try { localStorage.setItem("harmonyKey", harmonyKey); } catch { /* ignore */ } return true; }
  return false;
}

async function api(path, _retry) {
  const r = await fetch(path, { headers: keyHeaders() });
  if (r.status === 401 && !_retry && await promptKey()) return api(path, true);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

async function apiPost(path, body, _retry) {
  const r = await fetch(path, { method: "POST", headers: keyHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body || {}) });
  if (r.status === 401 && !_retry && await promptKey()) return apiPost(path, body, true);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

// -- rendering --------------------------------------------------------------

function renderTracks(tracks) {
  const list = $("list");
  const pl = state.playlist;
  const toolbar = pl ? `
    <div class="toolbar-row">
      <button class="act ghost" id="pl-rename">Rename</button>
      <button class="act ghost" id="pl-delete">Delete</button>
    </div>` : "";
  if (!tracks.length) { list.innerHTML = toolbar + `<p class="hint">No tracks.</p>`; wirePlaylistToolbar(); return; }
  const rows = tracks.map((t, i) => `
    <div class="trow" data-i="${i}">
      <button class="play" title="Play">▶</button>
      <div class="title">${esc(t.title)}<span class="badge">${serviceLabel(t.service)}</span></div>
      <div class="artist">${esc(t.artist)}</div>
      <div class="dur">${fmtTime(t.duration_s)}</div>
      <div class="rowacts">
        <button class="mini add" title="Add to playlist">＋</button>
        ${pl ? `<button class="mini rem" title="Remove from this playlist">✕</button>` : ""}
      </div>
    </div>`).join("");
  list.innerHTML = toolbar + `<div class="tracks">${rows}</div>`;
  state.queue = tracks;
  list.querySelectorAll(".trow").forEach((row) => {
    const i = Number(row.dataset.i);
    row.querySelector(".play").addEventListener("click", () => playAt(i));
    row.querySelector(".add").addEventListener("click", (e) => openAddMenu(e.currentTarget, tracks[i]));
    const rem = row.querySelector(".rem");
    if (rem) rem.addEventListener("click", () => removeFromPlaylist(tracks[i], i));
  });
  wirePlaylistToolbar();
  highlightPlaying();
}

function wirePlaylistToolbar() {
  const pl = state.playlist;
  if (!pl) return;
  if ($("pl-rename")) $("pl-rename").onclick = async () => {
    const title = await modalPrompt({ title: "Rename playlist", label: "New name", value: pl.title, okText: "Rename" });
    if (!title) return;
    try { await apiPost(`/api/playlists/${encodeURIComponent(pl.service)}/${encodeURIComponent(pl.id)}/rename`, { title });
      pl.title = title; $("view-title").textContent = title; loadPlaylistsSilently(); }
    catch (e) { toast("Rename failed: " + e.message); }
  };
  if ($("pl-delete")) $("pl-delete").onclick = async () => {
    if (!(await modalConfirm("Delete playlist", `Delete “${pl.title}”? This can’t be undone.`))) return;
    try { await apiPost(`/api/playlists/${encodeURIComponent(pl.service)}/${encodeURIComponent(pl.id)}/delete`, {});
      state.playlist = null; setView("playlists"); }
    catch (e) { toast("Delete failed: " + e.message); }
  };
}

async function removeFromPlaylist(track, i) {
  const pl = state.playlist; if (!pl) return;
  try {
    await apiPost(`/api/playlists/${encodeURIComponent(pl.service)}/${encodeURIComponent(pl.id)}/remove`, { track_ids: [track.id] });
    const rest = state.queue.slice(0, i).concat(state.queue.slice(i + 1));
    renderTracks(rest);
  } catch (e) { toast("Remove failed: " + e.message); }
}

let _playlistCache = null;
async function loadPlaylistsSilently() { try { _playlistCache = (await api("/api/playlists")).playlists || []; } catch { /* ignore */ } }

async function openAddMenu(anchor, track) {
  if (!_playlistCache) await loadPlaylistsSilently();
  document.querySelectorAll(".addmenu").forEach((m) => m.remove());
  const menu = document.createElement("div");
  menu.className = "addmenu";
  menu.innerHTML = (_playlistCache || []).map((p) =>
    `<div data-service="${esc(p.service)}" data-id="${esc(p.id)}">${esc(p.title)} <span class="s">${serviceLabel(p.service)}</span></div>`).join("")
    || `<div class="s">No playlists</div>`;
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${Math.min(r.bottom, window.innerHeight - menu.offsetHeight - 8)}px`;
  menu.style.left = `${Math.max(8, r.left - 180)}px`;
  menu.querySelectorAll("div[data-service]").forEach((row) => row.addEventListener("click", async () => {
    try { await apiPost(`/api/playlists/${encodeURIComponent(row.dataset.service)}/${encodeURIComponent(row.dataset.id)}/add`, { track_ids: [track.id] }); toast("Added.", true); }
    catch (e) { toast("Add failed: " + e.message); }
    menu.remove();
  }));
  setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }), 0);
}

function renderPlaylists(playlists) {
  const list = $("list");
  const bar = `<div class="toolbar-row"><button class="act" id="pl-new">＋ New playlist</button></div>`;
  if (!playlists.length) { list.innerHTML = bar + `<p class="hint">No playlists yet.</p>`; $("pl-new").onclick = newPlaylist; return; }
  list.innerHTML = bar + `<div class="plgrid">${playlists.map((p) => `
    <div class="plcard" data-service="${esc(p.service)}" data-id="${esc(p.id)}">
      ${p.artwork_url ? `<img class="art" src="${esc(p.artwork_url)}" alt="" />` : `<div class="art"></div>`}
      <div class="t">${esc(p.title)}</div>
      <div class="s">${serviceLabel(p.service)} · ${p.track_count ?? "?"} tracks</div>
    </div>`).join("")}</div>`;
  $("pl-new").onclick = newPlaylist;
  list.querySelectorAll(".plcard").forEach((card) => {
    card.addEventListener("click", () => openPlaylist(card.dataset.service, card.dataset.id, card.querySelector(".t").textContent));
  });
}

function highlightPlaying() {
  document.querySelectorAll(".trow").forEach((row) =>
    row.classList.toggle("playing", Number(row.dataset.i) === state.index));
}

// -- views ------------------------------------------------------------------

function setView(view) {
  document.querySelectorAll("#nav li[data-view], #mobilenav button").forEach((el) =>
    el.classList.toggle("active", el.dataset.view === view));
  if (view === "search") { $("view-title").textContent = "Search"; $("search-input").focus(); }
  else if (view === "playlists") { $("view-title").textContent = "Playlists"; loadPlaylists(); }
  else if (view === "accounts") { $("view-title").textContent = "Accounts"; renderAccounts(); }
  else if (view === "sync") { $("view-title").textContent = "Sync"; renderSync(); }
  else if (view === "devices") { $("view-title").textContent = "Devices"; renderDevices(); }
}

async function renderDevices(refresh) {
  const list = $("list");
  list.innerHTML = `<p class="hint">${refresh ? "Scanning your network…" : "Loading devices…"}</p>`;
  let devices = [];
  try { devices = (await api(`/api/devices?peers=1${refresh ? "&refresh=1" : ""}`)).devices || []; }
  catch (e) { list.innerHTML = `<p class="hint">Couldn't load devices: ${esc(e.message)}</p>`; return; }
  const targets = [{ host: "browser", name: "This browser", kind: "" }, ...devices];
  list.innerHTML = `<div style="max-width:640px">
    <div style="display:flex;align-items:center;gap:.6rem;padding:.5rem 0">
      <p class="hint" style="text-align:left;flex:1;margin:0">Pick where playback goes. Casting relays the
      stream on this instance's LAN to the device; the Now Playing bar then controls it.</p>
      <button class="act ghost" id="dev-rescan">Rescan</button>
    </div>
    ${targets.map((d) => {
      const value = d.host === "browser" ? "browser" : encodeTarget(d.host, d.via);
      const active = value === encodeTarget(state.target, state.targetVia);
      const sub = d.host === "browser" ? "" :
        `${esc(d.kind === "cast" ? "Chromecast" : (d.kind || "device"))} · ${esc(d.host)}${d.via ? ` · via ${esc(d.via_name || d.via)}` : ""}`;
      return `<div class="card" style="${active ? "border-color:var(--accent)" : ""}">
        <div style="display:flex;align-items:center;gap:.7rem">
          <span style="font-size:1.4rem">${d.host === "browser" ? "💻" : (d.kind === "cast" ? "📺" : "📻")}</span>
          <div style="flex:1;min-width:0">
            <div style="font-weight:600">${esc(d.name)}${active ? ` <span class="badge">output</span>` : ""}${d.via ? ` <span class="badge" style="background:var(--muted)">remote</span>` : ""}</div>
            ${sub ? `<div class="muted" style="font-size:12px">${sub}</div>` : ""}
          </div>
          <button class="act ${active ? "" : "ghost"} setout" data-target="${esc(value)}">${active ? "Selected" : "Use"}</button>
        </div>
      </div>`;
    }).join("")}
    ${devices.length ? "" : `<p class="muted" style="padding:.5rem">No cast devices found yet. WiiM/UPnP/Chromecast renderers on this instance's network are auto-discovered — press Rescan. Devices on another instance's LAN appear here too, tagged “via …”, and cast through that instance.</p>`}
  </div>`;
  const rescan = $("dev-rescan");
  if (rescan) rescan.onclick = () => renderDevices(true);
  list.querySelectorAll(".setout").forEach((b) => b.onclick = () => {
    const prevOnDevice = onDevice();
    setTargetValue(b.dataset.target);
    const sel = $("np-device"); if (sel) sel.value = b.dataset.target;  // keep the Now Playing selector in sync
    if (!prevOnDevice && onDevice()) audio.pause();  // handing off to a device
    renderDevices();
  });
}

async function renderSync() {
  const list = $("list");
  list.innerHTML = `<p class="hint">Loading playlists…</p>`;
  let pls = [];
  try { pls = (await api("/api/playlists")).playlists || []; } catch (e) { list.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  const opts = pls.map((p) => `<option value="${esc(p.service)}::${esc(p.id)}">${esc(p.title)} — ${serviceLabel(p.service)}</option>`).join("");
  list.innerHTML = `
    <div class="card" style="max-width:640px">
      <h2>Sync playlists</h2>
      <p class="muted">Match tracks across services and mirror one playlist onto another.
      Preview first — nothing is written until you apply.</p>
      <label class="muted">Source</label><select id="sy-src" style="width:100%">${opts}</select>
      <label class="muted" style="margin-top:.5rem;display:block">Target</label><select id="sy-tgt" style="width:100%">${opts}</select>
      <label class="muted" style="margin-top:.5rem;display:block">Direction</label>
      <select id="sy-dir" style="width:100%">
        <option value="a_to_b">Mirror source → target</option>
        <option value="b_to_a">Mirror target → source</option>
        <option value="two_way">Two-way</option>
      </select>
      <div style="margin-top:.75rem;display:flex;gap:.5rem">
        <button class="act" id="sy-plan">Preview</button>
        <button class="act" id="sy-apply" disabled>Apply</button>
      </div>
      <p id="sy-msg" class="muted"></p>
    </div>`;
  let token = null;
  const parse = (v) => ({ service: v.split("::")[0], id: v.split("::").slice(1).join("::") });
  $("sy-plan").onclick = async () => {
    $("sy-msg").textContent = "Planning…"; $("sy-apply").disabled = true; token = null;
    try {
      const r = await apiPost("/api/sync/plan", { source: parse($("sy-src").value), target: parse($("sy-tgt").value), direction: $("sy-dir").value });
      token = r.token;
      $("sy-msg").textContent = `${r.adds} to add, ${r.removes} to remove, ${r.unmatched} unmatched.` + (r.notes.length ? " " + r.notes.join(" ") : "");
      $("sy-apply").disabled = false;
    } catch (e) { $("sy-msg").textContent = "Plan failed: " + e.message; }
  };
  $("sy-apply").onclick = async () => {
    if (!token) return;
    $("sy-msg").textContent = "Applying…"; $("sy-apply").disabled = true;
    try {
      const r = await apiPost("/api/sync/apply", { token });
      $("sy-msg").textContent = `Added ${r.added}, removed ${r.removed}${r.failed ? ", " + r.failed + " failed" : ""}.`;
    } catch (e) { $("sy-msg").textContent = "Apply failed: " + e.message; }
    token = null;
  };
}

async function renderAccounts() {
  const list = $("list");
  list.innerHTML = `<p class="hint">Loading accounts…</p>`;
  let accounts = [], prefs = { personal_key: "" }, instances = [];
  try { accounts = (await api("/api/accounts")).accounts || []; } catch { /* show forms anyway */ }
  try { prefs = await api("/api/preferences"); } catch { /* ignore */ }
  try { instances = (await api("/api/instances")).instances || []; } catch { /* none */ }
  const status = (svc) => accounts.find((a) => a.service === svc) || { authenticated: false };
  const q = status("qobuz"), y = status("ytmusic");
  list.innerHTML = `
    <div style="max-width:640px">
      <p class="hint" style="text-align:left;padding:.5rem 0">
        The server holds these credentials on behalf of every client (this browser
        and the mobile app) — clients never store them.</p>

      <div class="card">
        <h2>Personal key</h2>
        <p class="muted">A shared secret you set identically on all your Harmony
        instances and apps. A signed-out app finds instances on your network and
        may use one — sharing its credentials — only when the keys match.</p>
        <input id="pk" type="text" style="width:100%;font-family:monospace" placeholder="your personal key" value="${esc(prefs.personal_key || "")}" />
        <div style="margin-top:.5rem"><button class="act" id="pk-save">Save key</button></div>
      </div>

      <div class="card">
        <h2>Sync accounts from another instance</h2>
        <p class="muted">Copy the streaming credentials from another Harmony
        instance that has the <em>same personal key</em> — handy for a fresh
        server or a second machine. Set your personal key above first; the copy
        is encrypted with it.</p>
        <select id="adopt-peer" style="width:100%">
          <option value="">— pick a discovered instance —</option>
          ${instances.map((p) => `<option value="${esc(p.host)}:${esc(p.port)}">${esc(p.name)} (${esc(p.host)}:${esc(p.port)})${p.source === "manual" ? " · saved" : ""}</option>`).join("")}
        </select>
        <input id="adopt-host" type="text" style="width:100%;margin-top:.4rem" placeholder="or host:port — e.g. 192.168.1.10:8080 or a tailnet IP" />
        <div style="margin-top:.5rem;display:flex;gap:.5rem">
          <button class="act" id="adopt-go">Sync accounts</button>
          <button class="act ghost" id="peer-remember" title="Save this instance so it stays in the list (needed across a tailnet — mDNS won't rediscover it)">Remember instance</button>
        </div>
        <p id="adopt-msg" class="muted"></p>
      </div>

      <div class="card">
        <h2>YouTube Music <span class="badge">${y.stale ? "session expired — reconnect" : (y.authenticated ? "signed in" + (y.account ? " · " + esc(y.account) : "") : "signed out")}</span></h2>
        <p class="muted">One click — Harmony detects a signed-in YouTube session from
        a browser on <em>the server's machine</em>. No setup, no pasting. (First,
        sign in to music.youtube.com in a browser on that machine.)</p>
        <div id="yt-code" class="muted" style="margin:.6rem 0"></div>
        <div style="display:flex;gap:.5rem">
          <button class="act" id="yt-detect">${y.stale ? "Reconnect" : "Connect YouTube"}</button>
          ${y.authenticated ? `<button class="act ghost" id="yt-out">Sign out</button>` : ""}
        </div>
        <details style="margin-top:.6rem"><summary class="muted">Advanced sign-in options</summary>
          <p class="muted" style="margin-top:.5rem">Paste request headers from a logged-in music.youtube.com tab (DevTools → a request → copy request headers):</p>
          <textarea id="yt-headers" rows="3" style="width:100%;font-family:monospace;font-size:12px" placeholder="Cookie: …"></textarea>
          <div style="margin-top:.4rem"><button class="act ghost" id="yt-save">Save headers</button></div>
          <p class="muted" style="margin-top:.7rem">Or Google OAuth — durable, but needs a one-time Google Cloud “TV and Limited Input” client
          (<a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">console</a>):</p>
          <input id="yt-cid" type="text" placeholder="Client ID" style="width:100%;margin-top:.3rem" />
          <input id="yt-cs" type="text" placeholder="Client secret" style="width:100%;margin-top:.3rem" />
          <div style="margin-top:.4rem;display:flex;gap:.5rem"><button class="act ghost" id="yt-client-save">Save client</button><button class="act ghost" id="yt-connect">Connect via OAuth</button></div>
        </details>
      </div>

      <div class="card">
        <h2>Qobuz <span class="badge">${q.stale ? "session expired — paste a fresh token" : (q.authenticated ? "signed in" + (q.account ? " · " + esc(q.account) : "") : "signed out")}</span></h2>
        <p class="muted">Paste your <code>X-User-Auth-Token</code> (DevTools → Application → Local Storage
        on play.qobuz.com, or a request header).</p>
        <input id="qb-token" type="text" style="width:100%;font-family:monospace;font-size:12px" placeholder="user auth token" />
        <div style="margin-top:.5rem;display:flex;gap:.5rem">
          <button class="act" id="qb-save">Save</button>
          ${q.authenticated ? `<button class="act ghost" id="qb-out">Sign out</button>` : ""}
        </div>
      </div>
      <p id="acct-msg" class="muted"></p>
    </div>`;

  const msg = (t, ok) => { const m = $("acct-msg"); m.textContent = t; m.style.color = ok ? "#2ec27e" : "var(--muted)"; };
  const after = () => { loadAccounts(); renderAccounts(); };
  $("pk-save").onclick = async () => {
    const k = ($("pk").value || "").trim();
    try {
      await apiPost("/api/preferences", { personal_key: k });
      // Keep talking to the server after it starts requiring the key: store the
      // same key this client sends, so we don't 401 on the next request.
      harmonyKey = k;
      try { localStorage.setItem("harmonyKey", harmonyKey); } catch { /* ignore */ }
      msg("Personal key saved.", true);
    } catch (e) { msg("Failed: " + e.message); }
  };
  $("adopt-go").onclick = async () => {
    const target = ($("adopt-host").value.trim()) || $("adopt-peer").value;
    const am = $("adopt-msg");
    am.style.color = "var(--muted)"; am.textContent = "Syncing…";
    let body = {};
    if (target) {
      const i = target.lastIndexOf(":");
      const host = (i > 0 ? target.slice(0, i) : target).trim();
      const port = i > 0 ? Number(target.slice(i + 1)) : 8080;
      if (!host) { am.textContent = "Enter a host."; return; }
      body = { host, port: port || 8080 };
    }
    try {
      const r = await apiPost("/api/credentials/adopt", body);
      if (r.ok) {
        am.textContent = `Synced ${(r.imported || []).length} credential(s).`;
        am.style.color = "#2ec27e";
        // Remember a manually-typed instance so it persists in this dropdown and
        // the Route tab — mDNS won't rediscover it across a tailnet/routed subnet.
        if (body.host) { try { await apiPost("/api/peers", body); } catch { /* best-effort */ } }
        loadAccounts(); setTimeout(renderAccounts, 900);
      } else {
        am.textContent = r.reason || "Nothing to sync — pick an instance or enter host:port.";
      }
    } catch (e) { am.textContent = "Failed: " + e.message; }
  };
  const rememberBtn = $("peer-remember");
  if (rememberBtn) rememberBtn.onclick = async () => {
    const target = $("adopt-host").value.trim();
    const am = $("adopt-msg");
    if (!target) { am.style.color = "var(--muted)"; am.textContent = "Enter host:port to remember."; return; }
    const i = target.lastIndexOf(":");
    const host = (i > 0 ? target.slice(0, i) : target).trim();
    const port = i > 0 ? Number(target.slice(i + 1)) : 8080;
    am.style.color = "var(--muted)"; am.textContent = "Adding…";
    try {
      const r = await apiPost("/api/peers", { host, port: port || 8080 });
      if (r.ok) { am.textContent = `Remembered ${esc(r.peer.name)}.`; am.style.color = "#2ec27e"; setTimeout(renderAccounts, 700); }
      else { am.textContent = r.reason || "Couldn't reach that instance."; }
    } catch (e) { am.textContent = "Failed: " + e.message; }
  };
  $("yt-save").onclick = async () => {
    const h = $("yt-headers").value.trim(); if (!h) return msg("Paste headers first.");
    try { await apiPost("/api/accounts/ytmusic/browser", { headers: h }); msg("YouTube Music saved.", true); after(); }
    catch (e) { msg("Failed: " + e.message); }
  };
  $("yt-detect").onclick = async () => {
    $("yt-code").textContent = "Detecting a signed-in browser on the server…";
    try { await apiPost("/api/accounts/ytmusic/autodetect", {}); $("yt-code").textContent = "Connected! ✓"; loadAccounts(); setTimeout(renderAccounts, 800); }
    catch (e) { $("yt-code").textContent = e.message; }
  };
  $("yt-client-save").onclick = async () => {
    try { await apiPost("/api/accounts/ytmusic/oauth/client", { client_id: $("yt-cid").value, client_secret: $("yt-cs").value }); msg("OAuth client saved.", true); }
    catch (e) { msg("Failed: " + e.message); }
  };
  let ytPoll = null;
  $("yt-connect").onclick = async () => {
    if (ytPoll) { clearInterval(ytPoll); ytPoll = null; }
    $("yt-code").textContent = "Starting…";
    let r;
    try { r = await apiPost("/api/accounts/ytmusic/oauth/start", {}); }
    catch (e) { $("yt-code").textContent = "Couldn't start: " + e.message + " (set up the OAuth client above first)"; return; }
    $("yt-code").innerHTML = `Open <a href="${esc(r.full_url)}" target="_blank" rel="noopener">${esc(r.verification_url)}</a> and enter code <b style="font-size:1.3em">${esc(r.user_code)}</b>, then approve.`;
    ytPoll = setInterval(async () => {
      let p;
      try { p = await apiPost("/api/accounts/ytmusic/oauth/poll", { poll_token: r.poll_token }); }
      catch (e) { clearInterval(ytPoll); ytPoll = null; $("yt-code").textContent = "Failed: " + e.message; return; }
      if (p.status === "done") { clearInterval(ytPoll); ytPoll = null; $("yt-code").textContent = "Connected! ✓"; loadAccounts(); setTimeout(renderAccounts, 800); }
    }, (r.interval || 5) * 1000);
  };
  $("qb-save").onclick = async () => {
    const t = $("qb-token").value.trim(); if (!t) return msg("Paste a token first.");
    try { await apiPost("/api/accounts/qobuz/token", { token: t }); msg("Qobuz saved.", true); after(); }
    catch (e) { msg("Failed: " + e.message); }
  };
  if ($("yt-out")) $("yt-out").onclick = async () => { await apiPost("/api/accounts/ytmusic/signout"); after(); };
  if ($("qb-out")) $("qb-out").onclick = async () => { await apiPost("/api/accounts/qobuz/signout"); after(); };
}

async function doSearch(q) {
  state.playlist = null;
  $("view-title").textContent = "Search";
  $("list").innerHTML = `<p class="hint">Searching…</p>`;
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    const tracks = r.tracks || [];
    if (r.playlists && r.playlists.length && !tracks.length) renderPlaylists(r.playlists);
    else renderTracks(tracks);
  } catch (e) { $("list").innerHTML = `<p class="hint">Search failed: ${esc(e.message)}</p>`; }
}

async function loadPlaylists() {
  state.playlist = null;
  $("list").innerHTML = `<p class="hint">Loading playlists…</p>`;
  try {
    _playlistCache = (await api("/api/playlists")).playlists || [];
    renderPlaylists(_playlistCache);
  } catch (e) { $("list").innerHTML = `<p class="hint">Couldn't load playlists: ${esc(e.message)}</p>`; }
}

async function openPlaylist(service, id, title) {
  state.playlist = { service, id, title };
  $("view-title").textContent = title || "Playlist";
  $("list").innerHTML = `<p class="hint">Loading tracks…</p>`;
  try { renderTracks((await api(`/api/playlists/${encodeURIComponent(service)}/${encodeURIComponent(id)}/tracks`)).tracks || []); }
  catch (e) { $("list").innerHTML = `<p class="hint">Couldn't load tracks: ${esc(e.message)}</p>`; }
}

async function newPlaylist() {
  const r = await modalPrompt({ title: "New playlist", okText: "Create", fields: [
    { name: "title", label: "Title", type: "text", placeholder: "Playlist name" },
    { name: "service", label: "Service", type: "select", value: "qobuz",
      options: [{ value: "qobuz", label: "Qobuz" }, { value: "ytmusic", label: "YT Music" }] },
  ] });
  if (!r || !r.title) return;
  try { await apiPost("/api/playlists", { service: r.service, title: r.title }); loadPlaylists(); }
  catch (e) { toast("Create failed: " + e.message); }
}

function acctStatusText(a) {
  if (a.stale) return "session expired";
  if (a.account) return esc(a.account);
  if (a.authenticated) return "signed in";
  return "signed out";
}

async function loadAccounts() {
  try {
    const r = await api("/api/accounts");
    $("accounts").innerHTML = (r.accounts || []).map((a) =>
      `<div class="acct"><span class="dot ${a.authenticated && !a.stale ? "ok" : ""}"></span>${serviceLabel(a.service)} · ${acctStatusText(a)}</div>`).join("");
  } catch { /* leave blank */ }
}

// -- playback ---------------------------------------------------------------

async function playAt(i) {
  if (i < 0 || i >= state.queue.length) return;
  const t = state.queue[i];
  state.index = i;
  highlightPlaying();
  $("np-title").textContent = t.title;
  $("np-artist").textContent = t.artist;
  $("nowplaying").classList.remove("empty");
  $("np-art").src = t.artwork_url || "";
  try {
    if (onDevice()) {
      await apiPost(`/api/devices/${encodeURIComponent(state.target)}/play`,
        withVia({ service: t.service, id: t.id, meta: { title: t.title, artist: t.artist, album: t.album, art_url: t.artwork_url, duration_s: t.duration_s } }));
      state.devicePaused = false;
      $("np-play").textContent = "⏸";
    } else {
      const r = await api(`/api/resolve?service=${encodeURIComponent(t.service)}&id=${encodeURIComponent(t.id)}`);
      audio.src = `/stream/${r.token}${keyParam()}`;
      await audio.play();
    }
  } catch (e) {
    $("np-title").textContent = `Couldn't play: ${e.message}`;
  }
}

$("np-device").addEventListener("change", (e) => {
  const prev = state.target;
  setTargetValue(e.target.value);
  if (prev === "browser" && onDevice()) audio.pause();       // handing off to a device
});

$("np-play").addEventListener("click", async () => {
  if (onDevice()) {
    if (state.index < 0) { if (state.queue.length) return playAt(0); return; }
    try { await apiPost(`/api/devices/${encodeURIComponent(state.target)}/${state.devicePaused ? "resume" : "pause"}`, withVia({}));
      state.devicePaused = !state.devicePaused; $("np-play").textContent = state.devicePaused ? "▶" : "⏸"; } catch { /* ignore */ }
    return;
  }
  if (!audio.src) { if (state.queue.length) playAt(0); return; }
  audio.paused ? audio.play() : audio.pause();
});
$("np-prev").addEventListener("click", () => playAt(state.index - 1));
$("np-next").addEventListener("click", () => playAt(state.index + 1));
audio.addEventListener("ended", () => playAt(state.index + 1));
audio.addEventListener("play", () => { $("np-play").textContent = "⏸"; });
audio.addEventListener("pause", () => { $("np-play").textContent = "▶"; });
audio.addEventListener("loadedmetadata", () => {
  $("np-seek").max = Math.floor(audio.duration || 1);
  $("np-dur").textContent = fmtTime(audio.duration);
});
audio.addEventListener("timeupdate", () => {
  if (!seeking) { $("np-seek").value = Math.floor(audio.currentTime); }
  $("np-pos").textContent = fmtTime(audio.currentTime);
});
let seeking = false;
$("np-seek").addEventListener("input", () => { seeking = true; $("np-pos").textContent = fmtTime($("np-seek").value); });
$("np-seek").addEventListener("change", () => { audio.currentTime = Number($("np-seek").value); seeking = false; });
$("np-vol").addEventListener("input", () => {
  if (onDevice()) { apiPost(`/api/devices/${encodeURIComponent(state.target)}/volume`, withVia({ level: Number($("np-vol").value) })).catch(() => {}); }
  else { audio.volume = $("np-vol").value / 100; }
});

async function loadDevices() {
  try {
    const devs = (await api("/api/devices?peers=1")).devices || [];
    const sel = $("np-device");
    // Rebuild, keeping the first "This browser" option.
    while (sel.options.length > 1) sel.remove(1);
    for (const d of devs) {
      const o = document.createElement("option");
      o.value = encodeTarget(d.host, d.via);
      o.textContent = d.via ? `${d.name} (via ${d.via_name || d.via})` : d.name;
      sel.appendChild(o);
    }
  } catch { /* no devices */ }
}

// -- wiring -----------------------------------------------------------------

$("search").addEventListener("submit", (e) => { e.preventDefault(); const q = $("search-input").value.trim(); if (q) doSearch(q); });
document.querySelectorAll("#nav li[data-view], #mobilenav button").forEach((el) => el.addEventListener("click", () => setView(el.dataset.view)));
$("accounts").addEventListener("click", () => setView("accounts"));
loadAccounts();
loadDevices();

// Progressive web app: install + offline shell.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => { /* non-fatal */ }));
}
