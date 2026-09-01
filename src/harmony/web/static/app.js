"use strict";
// Harmony web client. Talks to the same HTTP API the mobile app uses; audio
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

const ICON = (name, extra) => `<svg class="ico${extra ? " " + extra : ""}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

const fmtTime = (s) => {
  if (!s || s < 0 || !isFinite(s)) return "0:00";
  const t = Math.floor(s), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  const mm = h ? String(m).padStart(2, "0") : m;
  return (h ? `${h}:` : "") + `${mm}:${String(sec).padStart(2, "0")}`;
};
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
const serviceLabel = (s) => ({ ytmusic: "YouTube Music", qobuz: "Qobuz" }[s] || s);
const nTracks = (n) => (n == null ? "" : `${n} ${n === 1 ? "track" : "tracks"}`);
const deviceIcon = (d) => (d.host === "browser" ? "computer" : d.kind === "cast" ? "tv" : "speaker");
const deviceKindLabel = (d) => (d.kind === "cast" ? "Chromecast" : d.kind === "wiim" ? "WiiM" : (d.kind || "device"));

// -- shared state blocks (empty / error / loading) --------------------------

const emptyState = (icon, title, body, action) => `<div class="state">${ICON(icon)}
  <h2>${esc(title)}</h2>${body ? `<p>${esc(body)}</p>` : ""}
  ${action ? `<button class="act" id="${action.id}">${esc(action.label)}</button>` : ""}</div>`;
const errorState = (title, detail, retryId) => `<div class="state">${ICON("alert")}
  <h2>${esc(title)}</h2>${detail ? `<p class="muted">${esc(detail)}</p>` : ""}
  <button class="act" id="${retryId}">Try again</button></div>`;
const loadingState = (label) => `<div class="loading"><span class="spinner"></span> ${esc(label || "Loading…")}</div>`;

// -- in-page dialogs (replace native prompt/confirm/alert; touch-friendly) --

function toast(text, kind) {
  const t = $("toast");
  t.textContent = text;
  t.className = "show" + (kind === true || kind === "ok" ? " ok" : kind === "error" ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 3200);
}
const toastErr = (text) => toast(text, "error");

function _modal(inner) {
  const root = document.createElement("div");
  root.className = "modal-back";
  root.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${inner}</div>`;
  document.body.appendChild(root);
  const close = () => root.remove();
  root.addEventListener("click", (e) => { if (e.target === root) close(); });
  return { root, close };
}

// Resolves to a string (single field), an object (when `fields` given), or null.
function modalPrompt({ title, label, value = "", placeholder = "", okText = "OK", type = "text", fields }) {
  return new Promise((resolve) => {
    const flds = fields || [{ name: "value", label, value, placeholder, type }];
    const html = flds.map((f) => f.type === "select"
      ? `<label>${esc(f.label)}</label><select data-name="${esc(f.name)}">${(f.options || []).map((o) =>
          `<option value="${esc(o.value)}"${o.value === f.value ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`
      : `<label>${esc(f.label)}</label><input data-name="${esc(f.name)}" type="${f.type === "password" ? "password" : "text"}" value="${esc(f.value || "")}" placeholder="${esc(f.placeholder || "")}" autocomplete="off" />`
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

function modalConfirm(title, body, { okText = "OK", danger = false } = {}) {
  return new Promise((resolve) => {
    const m = _modal(`<h2>${esc(title)}</h2>${body ? `<p class="muted">${esc(body)}</p>` : ""}` +
      `<div class="modal-acts"><button class="act ghost" data-x>Cancel</button>` +
      `<button class="act${danger ? " danger" : ""}" data-ok>${esc(okText)}</button></div>`);
    const done = (v) => { m.close(); resolve(v); };
    m.root.querySelector("[data-ok]").onclick = () => done(true);
    m.root.querySelector("[data-x]").onclick = () => done(false);
    m.root.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); done(true); }
      else if (e.key === "Escape") { done(false); }
    });
    m.root.querySelector("[data-x]").focus();
  });
}

let harmonyKey = "";
try { harmonyKey = localStorage.getItem("harmonyKey") || ""; } catch { harmonyKey = ""; }
const keyHeaders = (extra) => Object.assign(harmonyKey ? { "X-Harmony-Key": harmonyKey } : {}, extra || {});
const keyParam = () => (harmonyKey ? `?key=${encodeURIComponent(harmonyKey)}` : "");
async function promptKey() {
  const k = await modalPrompt({ title: "Personal key required", type: "password",
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

function renderTracks(tracks, opts = {}) {
  const list = $("list");
  const pl = state.playlist;
  const toolbar = pl ? `
    <div class="toolbar-row">
      <span class="muted">${esc(nTracks(tracks.length))}</span>
      <span style="flex:1"></span>
      <button class="act ghost" id="pl-rename">Rename</button>
      <button class="act ghost" id="pl-delete">Delete</button>
    </div>` : "";
  if (!tracks.length) {
    const empty = pl
      ? emptyState("playlists", "This playlist is empty", "Find songs in Search and use ＋ to add them here.")
      : emptyState("search", opts.query ? `No results for “${opts.query}”` : "Nothing here",
                   opts.query ? "Try a different title or artist." : "Search for a song to get started.");
    list.innerHTML = toolbar + empty; wirePlaylistToolbar(); return;
  }
  const rows = tracks.map((t, i) => `
    <div class="trow" data-i="${i}">
      <button class="play" aria-label="Play ${esc(t.title)}">${ICON("play")}</button>
      <div class="title"><span class="tt">${esc(t.title)}</span><span class="badge">${esc(serviceLabel(t.service))}</span></div>
      <div class="artist">${esc(t.artist)}</div>
      <div class="dur">${fmtTime(t.duration_s)}</div>
      <div class="rowacts">
        <button class="mini add" aria-label="Add to playlist">${ICON("add")}</button>
        ${pl ? `<button class="mini rem" aria-label="Remove from this playlist">${ICON("remove")}</button>` : ""}
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
      pl.title = title; $("view-title").textContent = title; loadPlaylistsSilently(); toast("Playlist renamed.", "ok"); }
    catch (e) { toastErr("Couldn’t rename the playlist: " + e.message); }
  };
  if ($("pl-delete")) $("pl-delete").onclick = async () => {
    if (!(await modalConfirm("Delete playlist", `Delete “${pl.title}”? This can’t be undone.`, { okText: "Delete", danger: true }))) return;
    try { await apiPost(`/api/playlists/${encodeURIComponent(pl.service)}/${encodeURIComponent(pl.id)}/delete`, {});
      state.playlist = null; setView("playlists"); toast("Playlist deleted.", "ok"); }
    catch (e) { toastErr("Couldn’t delete the playlist: " + e.message); }
  };
}

async function removeFromPlaylist(track, i) {
  const pl = state.playlist; if (!pl) return;
  try {
    await apiPost(`/api/playlists/${encodeURIComponent(pl.service)}/${encodeURIComponent(pl.id)}/remove`, { track_ids: [track.id] });
    const rest = state.queue.slice(0, i).concat(state.queue.slice(i + 1));
    renderTracks(rest);
  } catch (e) { toastErr("Couldn’t remove the track: " + e.message); }
}

let _playlistCache = null;
async function loadPlaylistsSilently() { try { _playlistCache = (await api("/api/playlists")).playlists || []; } catch { /* ignore */ } }

async function openAddMenu(anchor, track) {
  if (!_playlistCache) await loadPlaylistsSilently();
  document.querySelectorAll(".addmenu").forEach((m) => m.remove());
  const menu = document.createElement("div");
  menu.className = "addmenu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = (_playlistCache || []).map((p) =>
    `<div role="menuitem" tabindex="0" data-service="${esc(p.service)}" data-id="${esc(p.id)}">${esc(p.title)} <span class="s">${esc(serviceLabel(p.service))}</span></div>`).join("")
    + `<div role="menuitem" tabindex="0" class="new" data-new>＋ New playlist…</div>`;
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 8)}px`;
  menu.style.left = `${Math.min(Math.max(8, r.left - 160), window.innerWidth - menu.offsetWidth - 8)}px`;
  const close = () => menu.remove();
  menu.querySelectorAll("div[data-service]").forEach((row) => row.addEventListener("click", async () => {
    close();
    try { await apiPost(`/api/playlists/${encodeURIComponent(row.dataset.service)}/${encodeURIComponent(row.dataset.id)}/add`, { track_ids: [track.id] });
      toast(`Added to “${row.textContent.trim()}”.`, "ok"); }
    catch (e) { toastErr("Couldn’t add to the playlist: " + e.message); }
  }));
  menu.querySelector("[data-new]").addEventListener("click", async () => { close(); await newPlaylist(track); });
  menu.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  document.addEventListener("keydown", function esc(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); } });
  setTimeout(() => document.addEventListener("click", close, { once: true }), 0);
}

function renderPlaylists(playlists) {
  const list = $("list");
  const bar = `<div class="toolbar-row"><button class="act" id="pl-new">${ICON("add")} New playlist</button></div>`;
  if (!playlists.length) {
    list.innerHTML = bar + emptyState("playlists", "No playlists yet",
      "Create one to start collecting tracks — or sign in to a service to see your existing playlists.",
      { id: "pl-empty-new", label: "New playlist" });
    $("pl-new").onclick = () => newPlaylist();
    $("pl-empty-new").onclick = () => newPlaylist();
    return;
  }
  list.innerHTML = bar + `<div class="plgrid">${playlists.map((p) => `
    <div class="plcard" data-service="${esc(p.service)}" data-id="${esc(p.id)}" data-art="${esc(p.artwork_url || "")}">
      <div class="art">${ICON("music")}</div>
      <div class="t">${esc(p.title)}</div>
      <div class="s">${esc(serviceLabel(p.service))}${p.track_count != null ? " · " + nTracks(p.track_count) : ""}</div>
    </div>`).join("")}</div>`;
  $("pl-new").onclick = () => newPlaylist();
  list.querySelectorAll(".plcard").forEach((card) => {
    const url = card.dataset.art;
    if (url) {
      const img = new Image();
      img.className = "art"; img.alt = "";
      img.onload = () => { const slot = card.querySelector(".art"); if (slot) slot.replaceWith(img); };
      img.src = url;
    }
    card.addEventListener("click", () => openPlaylist(card.dataset.service, card.dataset.id, card.querySelector(".t").textContent));
  });
}

function highlightPlaying() {
  document.querySelectorAll(".trow").forEach((row) => {
    const active = Number(row.dataset.i) === state.index;
    row.classList.toggle("playing", active);
    const btn = row.querySelector(".play");
    if (btn) btn.innerHTML = active ? `<span class="eq"><span></span><span></span><span></span></span>` : ICON("play");
  });
}

// -- views ------------------------------------------------------------------

function highlightNav(view) {
  document.querySelectorAll("#nav li[data-view]").forEach((el) => {
    const on = el.dataset.view === view;
    el.classList.toggle("active", on); el.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll("#mobilenav button").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
}

function setView(view) {
  highlightNav(view);
  if (view === "search") { $("view-title").textContent = "Search"; $("search-input").focus(); }
  else if (view === "playlists") { $("view-title").textContent = "Playlists"; loadPlaylists(); }
  else if (view === "accounts") { $("view-title").textContent = "Accounts"; renderAccounts(); }
  else if (view === "sync") { $("view-title").textContent = "Sync"; renderSync(); }
  else if (view === "devices") { $("view-title").textContent = "Devices"; renderDevices(); }
}

async function renderDevices(refresh) {
  const list = $("list");
  list.innerHTML = loadingState(refresh ? "Scanning your network…" : "Loading devices…");
  let devices = [];
  try { devices = (await api(`/api/devices?peers=1${refresh ? "&refresh=1" : ""}`)).devices || []; }
  catch (e) { list.innerHTML = errorState("Couldn’t load devices", e.message, "dev-retry"); $("dev-retry").onclick = () => renderDevices(refresh); return; }
  const targets = [{ host: "browser", name: "This browser", kind: "" }, ...devices];
  list.innerHTML = `<div class="page-narrow">
    <div class="device-row" style="padding:var(--sp-2) 0">
      <p class="muted" style="flex:1;margin:0">Pick where playback goes. Casting relays the stream to the
      device on its network; the Now Playing bar then controls it.</p>
      <button class="act ghost" id="dev-rescan">Rescan</button>
    </div>
    ${targets.map((d) => {
      const value = d.host === "browser" ? "browser" : encodeTarget(d.host, d.via);
      const active = value === encodeTarget(state.target, state.targetVia);
      const sub = d.host === "browser" ? "Plays in this tab"
        : `${esc(deviceKindLabel(d))} · ${esc(d.host)}${d.via ? ` · via ${esc(d.via_name || d.via)}` : ""}`;
      return `<div class="card device-row${active ? " selected" : ""}">
        ${ICON(deviceIcon(d), "dev-ico")}
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;display:flex;align-items:center;gap:.4rem">${esc(d.name)}
            ${active ? `<span class="badge">output</span>` : ""}${d.via ? `<span class="badge remote">remote</span>` : ""}</div>
          <div class="muted" style="font-size:12px">${sub}</div>
        </div>
        ${active ? `<span class="muted">Current output</span>`
          : `<button class="act ghost setout" data-target="${esc(value)}">Use</button>`}
      </div>`;
    }).join("")}
    ${devices.length ? "" : `<p class="muted" style="padding:var(--sp-2)">No cast devices found yet. WiiM, UPnP, and Chromecast
      renderers on this instance’s network are found automatically — press Rescan. Devices on another instance’s
      LAN show up here too, tagged “via …”, and cast through that instance.</p>`}
  </div>`;
  $("dev-rescan").onclick = () => renderDevices(true);
  list.querySelectorAll(".setout").forEach((b) => b.onclick = () => {
    const prevOnDevice = onDevice();
    setTargetValue(b.dataset.target);
    const sel = $("np-device"); if (sel) sel.value = b.dataset.target;
    if (!prevOnDevice && onDevice()) audio.pause();
    loadDevices();
    renderDevices();
  });
}

async function renderSync() {
  const list = $("list");
  list.innerHTML = loadingState("Loading playlists…");
  let pls = [];
  try { pls = (await api("/api/playlists")).playlists || []; }
  catch (e) { list.innerHTML = errorState("Couldn’t load playlists", e.message, "sy-retry"); $("sy-retry").onclick = renderSync; return; }
  if (pls.length < 1) {
    list.innerHTML = emptyState("sync", "Nothing to sync yet", "Sign in to a service and load some playlists first.");
    return;
  }
  const opts = pls.map((p) => `<option value="${esc(p.service)}::${esc(p.id)}">${esc(p.title)} — ${esc(serviceLabel(p.service))}</option>`).join("");
  list.innerHTML = `
    <div class="card page-narrow">
      <h2>Sync playlists</h2>
      <p class="muted">Match tracks across services and mirror one playlist onto another.
      Preview first — nothing is written until you apply.</p>
      <label class="muted field">Source</label><select id="sy-src">${opts}</select>
      <label class="muted field">Target</label><select id="sy-tgt">${opts}</select>
      <label class="muted field">Direction</label>
      <select id="sy-dir">
        <option value="a_to_b">Source → target</option>
        <option value="b_to_a">Target → source</option>
        <option value="two_way">Two-way merge</option>
      </select>
      <div class="field-acts">
        <button class="act" id="sy-plan">Preview</button>
        <button class="act" id="sy-apply" disabled>Apply</button>
      </div>
      <p id="sy-msg" class="muted msg"></p>
    </div>`;
  if ($("sy-tgt").options.length > 1) $("sy-tgt").selectedIndex = 1;
  let token = null;
  const parse = (v) => ({ service: v.split("::")[0], id: v.split("::").slice(1).join("::") });
  const same = () => $("sy-src").value === $("sy-tgt").value;
  const syncMsg = (t, cls) => { const m = $("sy-msg"); m.textContent = t; m.className = "muted msg" + (cls ? " " + cls : ""); };
  const checkSame = () => { const s = same(); $("sy-plan").disabled = s; if (s) syncMsg("Pick two different playlists."); else if (!token) syncMsg(""); };
  $("sy-src").onchange = checkSame; $("sy-tgt").onchange = checkSame; checkSame();
  $("sy-plan").onclick = async () => {
    syncMsg("Planning…"); $("sy-apply").disabled = true; token = null;
    try {
      const r = await apiPost("/api/sync/plan", { source: parse($("sy-src").value), target: parse($("sy-tgt").value), direction: $("sy-dir").value });
      token = r.token;
      syncMsg(`${r.adds} to add · ${r.removes} to remove · ${r.unmatched} unmatched.` + (r.notes.length ? " " + r.notes.join(" ") : ""), "ok");
      $("sy-apply").disabled = false;
    } catch (e) { syncMsg("Couldn’t build the plan: " + e.message, "err"); }
  };
  $("sy-apply").onclick = async () => {
    if (!token) return;
    syncMsg("Applying…"); $("sy-apply").disabled = true;
    try {
      const r = await apiPost("/api/sync/apply", { token });
      syncMsg(`Done — added ${r.added}, removed ${r.removed}${r.failed ? `, ${r.failed} failed` : ""}.`, "ok");
    } catch (e) { syncMsg("Couldn’t apply the plan: " + e.message, "err"); }
    token = null;
  };
}

async function renderAccounts() {
  const list = $("list");
  list.innerHTML = loadingState("Loading accounts…");
  let accounts = [], prefs = { personal_key: "" }, instances = [];
  try { accounts = (await api("/api/accounts")).accounts || []; } catch { /* show forms anyway */ }
  try { prefs = await api("/api/preferences"); } catch { /* ignore */ }
  try { instances = (await api("/api/instances")).instances || []; } catch { /* none */ }
  const status = (svc) => accounts.find((a) => a.service === svc) || { authenticated: false };
  const q = status("qobuz"), y = status("ytmusic");
  const badge = (a) => a.stale ? "session expired" : a.authenticated ? "signed in" + (a.account ? " · " + esc(a.account) : "") : "signed out";
  list.innerHTML = `
    <div class="page-narrow">
      <p class="muted field">The server holds these credentials for every client (this browser and the
        mobile app) — clients never store them.</p>

      <div class="card">
        <h2>Personal key</h2>
        <p class="muted">A shared secret you set identically on all your Harmony instances and apps.
        A signed-out app finds instances on your network and may use one — sharing its credentials —
        only when the keys match.</p>
        <div style="display:flex;gap:.5rem" class="field">
          <input id="pk" type="password" class="mono" style="flex:1" placeholder="your personal key" value="${esc(prefs.personal_key || "")}" autocomplete="off" />
          <button class="act ghost" id="pk-show" type="button">Show</button>
        </div>
        <div class="field-acts"><button class="act" id="pk-save">Save key</button></div>
      </div>

      <div class="card">
        <h2>Sync accounts from another instance</h2>
        <p class="muted">Copy the streaming credentials from another Harmony instance with the
        <em>same personal key</em> — handy for a fresh server or a second machine. Set your personal
        key above first; the copy is encrypted with it.</p>
        <select id="adopt-peer" class="field" style="width:100%">
          <option value="">— pick a discovered instance —</option>
          ${instances.map((p) => `<option value="${esc(p.host)}:${esc(p.port)}">${esc(p.name)} (${esc(p.host)}:${esc(p.port)})${p.source === "manual" ? " · saved" : ""}</option>`).join("")}
        </select>
        <input id="adopt-host" type="text" class="field" placeholder="or host:port — e.g. 192.168.1.10:8080 or a tailnet IP" />
        <div class="field-acts">
          <button class="act" id="adopt-go">Sync accounts</button>
          <button class="act ghost" id="peer-remember" title="Save this instance so it stays in the list (needed across a tailnet — mDNS won’t rediscover it)">Remember instance</button>
        </div>
        <p id="adopt-msg" class="muted msg"></p>
      </div>

      <div class="card">
        <h2>YouTube Music <span class="badge">${badge(y)}</span></h2>
        <p class="muted">One click — Harmony detects a signed-in YouTube session from a browser on
        <em>the server’s machine</em>. No setup, no pasting. (First, sign in to music.youtube.com in a
        browser on that machine.)</p>
        <div id="yt-code" class="muted field"></div>
        <div class="field-acts">
          <button class="act" id="yt-detect">${y.stale ? "Reconnect" : "Connect YouTube"}</button>
          ${y.authenticated ? `<button class="act ghost" id="yt-out">Sign out</button>` : ""}
        </div>
        <details class="field"><summary class="muted">Advanced sign-in options</summary>
          <p class="muted field">Paste request headers from a logged-in music.youtube.com tab (DevTools → a request → copy request headers):</p>
          <textarea id="yt-headers" rows="3" class="mono" placeholder="Cookie: …"></textarea>
          <div class="field-acts"><button class="act ghost" id="yt-save">Save headers</button></div>
          <p class="muted field">Or Google OAuth — durable, but needs a one-time Google Cloud “TV and Limited Input” client
          (<a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">console</a>):</p>
          <input id="yt-cid" type="text" class="field" placeholder="Client ID" />
          <input id="yt-cs" type="text" class="field" placeholder="Client secret" />
          <div class="field-acts"><button class="act ghost" id="yt-client-save">Save client</button><button class="act ghost" id="yt-connect">Connect via OAuth</button></div>
        </details>
      </div>

      <div class="card">
        <h2>Qobuz <span class="badge">${badge(q)}</span></h2>
        <p class="muted">Paste your <code>X-User-Auth-Token</code> (DevTools → Application → Local Storage
        on play.qobuz.com, or a request header).</p>
        <input id="qb-token" type="text" class="mono field" placeholder="user auth token" />
        <div class="field-acts">
          <button class="act" id="qb-save">Save</button>
          ${q.authenticated ? `<button class="act ghost" id="qb-out">Sign out</button>` : ""}
        </div>
      </div>
      <p id="acct-msg" class="muted msg"></p>
    </div>`;

  const msg = (t, ok) => { const m = $("acct-msg"); m.textContent = t; m.className = "muted msg" + (ok ? " ok" : t ? " err" : ""); };
  const after = () => { loadAccounts(); renderAccounts(); };
  $("pk-show").onclick = () => { const el = $("pk"); const show = el.type === "password"; el.type = show ? "text" : "password"; $("pk-show").textContent = show ? "Hide" : "Show"; };
  $("pk-save").onclick = async () => {
    const k = ($("pk").value || "").trim();
    try {
      await apiPost("/api/preferences", { personal_key: k });
      harmonyKey = k;
      try { localStorage.setItem("harmonyKey", harmonyKey); } catch { /* ignore */ }
      msg("Personal key saved.", true);
    } catch (e) { msg("Couldn’t save the key: " + e.message); }
  };
  const setAdoptMsg = (t, ok) => { const am = $("adopt-msg"); am.textContent = t; am.className = "muted msg" + (ok ? " ok" : ""); };
  $("adopt-go").onclick = async () => {
    const target = ($("adopt-host").value.trim()) || $("adopt-peer").value;
    setAdoptMsg("Syncing…");
    let body = {};
    if (target) {
      const i = target.lastIndexOf(":");
      const host = (i > 0 ? target.slice(0, i) : target).trim();
      const port = i > 0 ? Number(target.slice(i + 1)) : 8080;
      if (!host) { setAdoptMsg("Enter a host."); return; }
      body = { host, port: port || 8080 };
    }
    try {
      const r = await apiPost("/api/credentials/adopt", body);
      if (r.ok) {
        setAdoptMsg(`Synced ${(r.imported || []).length} credential(s).`, true);
        if (body.host) { try { await apiPost("/api/peers", body); } catch { /* best-effort */ } }
        loadAccounts(); setTimeout(renderAccounts, 900);
      } else {
        setAdoptMsg(r.reason || "Nothing to sync — pick an instance or enter host:port.");
      }
    } catch (e) { setAdoptMsg("Couldn’t sync: " + e.message); }
  };
  $("peer-remember").onclick = async () => {
    const target = $("adopt-host").value.trim();
    if (!target) { setAdoptMsg("Enter host:port to remember."); return; }
    const i = target.lastIndexOf(":");
    const host = (i > 0 ? target.slice(0, i) : target).trim();
    const port = i > 0 ? Number(target.slice(i + 1)) : 8080;
    setAdoptMsg("Adding…");
    try {
      const r = await apiPost("/api/peers", { host, port: port || 8080 });
      if (r.ok) { setAdoptMsg(`Remembered ${esc(r.peer.name)}.`, true); setTimeout(renderAccounts, 700); }
      else { setAdoptMsg(r.reason || "Couldn’t reach that instance."); }
    } catch (e) { setAdoptMsg("Couldn’t add: " + e.message); }
  };
  $("yt-save").onclick = async () => {
    const h = $("yt-headers").value.trim(); if (!h) return msg("Paste headers first.");
    try { await apiPost("/api/accounts/ytmusic/browser", { headers: h }); msg("YouTube Music saved.", true); after(); }
    catch (e) { msg("Couldn’t save the headers: " + e.message); }
  };
  $("yt-detect").onclick = async () => {
    $("yt-code").textContent = "Detecting a signed-in browser on the server…";
    try { await apiPost("/api/accounts/ytmusic/autodetect", {}); $("yt-code").textContent = "Connected."; loadAccounts(); setTimeout(renderAccounts, 800); }
    catch (e) { $("yt-code").textContent = e.message; }
  };
  $("yt-client-save").onclick = async () => {
    try { await apiPost("/api/accounts/ytmusic/oauth/client", { client_id: $("yt-cid").value, client_secret: $("yt-cs").value }); msg("OAuth client saved.", true); }
    catch (e) { msg("Couldn’t save the client: " + e.message); }
  };
  let ytPoll = null;
  $("yt-connect").onclick = async () => {
    if (ytPoll) { clearInterval(ytPoll); ytPoll = null; }
    $("yt-code").textContent = "Starting…";
    let r;
    try { r = await apiPost("/api/accounts/ytmusic/oauth/start", {}); }
    catch (e) { $("yt-code").textContent = "Couldn’t start: " + e.message + " (set up the OAuth client above first)"; return; }
    $("yt-code").innerHTML = `Open <a href="${esc(r.full_url)}" target="_blank" rel="noopener">${esc(r.verification_url)}</a> and enter code <b style="font-size:1.3em">${esc(r.user_code)}</b>, then approve.`;
    ytPoll = setInterval(async () => {
      let p;
      try { p = await apiPost("/api/accounts/ytmusic/oauth/poll", { poll_token: r.poll_token }); }
      catch (e) { clearInterval(ytPoll); ytPoll = null; $("yt-code").textContent = "Couldn’t connect: " + e.message; return; }
      if (p.status === "done") { clearInterval(ytPoll); ytPoll = null; $("yt-code").textContent = "Connected."; loadAccounts(); setTimeout(renderAccounts, 800); }
    }, (r.interval || 5) * 1000);
  };
  $("qb-save").onclick = async () => {
    const t = $("qb-token").value.trim(); if (!t) return msg("Paste a token first.");
    try { await apiPost("/api/accounts/qobuz/token", { token: t }); msg("Qobuz saved.", true); after(); }
    catch (e) { msg("Couldn’t save the token: " + e.message); }
  };
  if ($("yt-out")) $("yt-out").onclick = async () => { await apiPost("/api/accounts/ytmusic/signout"); after(); };
  if ($("qb-out")) $("qb-out").onclick = async () => { await apiPost("/api/accounts/qobuz/signout"); after(); };
}

async function doSearch(q) {
  state.playlist = null;
  highlightNav("search");
  $("view-title").textContent = "Search";
  $("list").innerHTML = loadingState(`Searching for “${q}”…`);
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    const tracks = r.tracks || [];
    if (r.playlists && r.playlists.length && !tracks.length) renderPlaylists(r.playlists);
    else renderTracks(tracks, { query: q });
  } catch (e) { $("list").innerHTML = errorState("Search failed", e.message, "s-retry"); $("s-retry").onclick = () => doSearch(q); }
}

async function loadPlaylists() {
  state.playlist = null;
  $("list").innerHTML = loadingState("Loading playlists…");
  try {
    _playlistCache = (await api("/api/playlists")).playlists || [];
    renderPlaylists(_playlistCache);
  } catch (e) { $("list").innerHTML = errorState("Couldn’t load playlists", e.message, "pl-retry"); $("pl-retry").onclick = loadPlaylists; }
}

async function openPlaylist(service, id, title) {
  state.playlist = { service, id, title };
  $("view-title").textContent = title || "Playlist";
  $("list").innerHTML = loadingState("Loading tracks…");
  try { renderTracks((await api(`/api/playlists/${encodeURIComponent(service)}/${encodeURIComponent(id)}/tracks`)).tracks || []); }
  catch (e) { $("list").innerHTML = errorState("Couldn’t load tracks", e.message, "t-retry"); $("t-retry").onclick = () => openPlaylist(service, id, title); }
}

async function newPlaylist(addTrack) {
  const r = await modalPrompt({ title: "New playlist", okText: "Create", fields: [
    { name: "title", label: "Title", type: "text", placeholder: "Playlist name" },
    { name: "service", label: "Service", type: "select", value: "qobuz",
      options: [{ value: "qobuz", label: "Qobuz" }, { value: "ytmusic", label: "YouTube Music" }] },
  ] });
  if (!r || !r.title) return;
  try {
    const created = await apiPost("/api/playlists", { service: r.service, title: r.title });
    toast("Playlist created.", "ok");
    if (addTrack && created && created.id) {
      try { await apiPost(`/api/playlists/${encodeURIComponent(r.service)}/${encodeURIComponent(created.id)}/add`, { track_ids: [addTrack.id] }); toast(`Added to “${r.title}”.`, "ok"); }
      catch { /* non-fatal */ }
    }
    _playlistCache = null;
    if (!state.playlist) loadPlaylists();
  } catch (e) { toastErr("Couldn’t create the playlist: " + e.message); }
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
    const html = (r.accounts || []).map((a) =>
      `<span class="acct"><span class="dot ${a.authenticated && !a.stale ? "ok" : ""}"></span>${esc(serviceLabel(a.service))} · ${acctStatusText(a)}</span>`).join("");
    $("accounts").innerHTML = html || "Accounts →";
  } catch { $("accounts").innerHTML = "Accounts →"; }
}

// -- playback ---------------------------------------------------------------

function setArt(url) {
  const el = $("np-art");
  el.innerHTML = "";
  if (url) {
    const img = new Image();
    img.alt = ""; img.style.cssText = "width:100%;height:100%;object-fit:cover;border-radius:inherit";
    img.onerror = () => { el.classList.add("fallback"); el.innerHTML = ICON("music"); };
    el.classList.remove("fallback"); el.appendChild(img); img.src = url;
  } else { el.classList.add("fallback"); el.innerHTML = ICON("music"); }
}

function setPlayIcon(playing) {
  const b = $("np-play");
  b.setAttribute("aria-label", playing ? "Pause" : "Play");
  b.innerHTML = ICON(playing ? "pause" : "play");
}

function currentDeviceName() {
  const sel = $("np-device");
  return sel && sel.selectedOptions[0] ? sel.selectedOptions[0].textContent : "device";
}

function updateCastChip() {
  const via = $("np-via");
  const np = $("nowplaying");
  if (onDevice()) {
    via.classList.add("show");
    via.querySelector("span").textContent = `Playing on ${currentDeviceName()}`;
    np.classList.add("casting");
  } else { via.classList.remove("show"); np.classList.remove("casting"); }
}

let devicePoll = null;
function stopDevicePoll() { if (devicePoll) { clearInterval(devicePoll); devicePoll = null; } }
function startDevicePoll() {
  stopDevicePoll();
  devicePoll = setInterval(async () => {
    if (!onDevice()) return stopDevicePoll();
    try {
      const q = state.targetVia ? `?via=${encodeURIComponent(state.targetVia)}` : "";
      const s = await api(`/api/devices/${encodeURIComponent(state.target)}/status${q}`);
      if (s.duration_s) { $("np-seek").max = s.duration_s; $("np-dur").textContent = fmtTime(s.duration_s); }
      if (s.position_s != null) { $("np-seek").value = s.position_s; $("np-pos").textContent = fmtTime(s.position_s); }
    } catch { /* device may be mid-buffer; ignore */ }
  }, 1500);
}

function updateMediaSession(t) {
  if (!("mediaSession" in navigator)) return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title || "", artist: t.artist || "", album: t.album || "",
      artwork: t.artwork_url ? [{ src: t.artwork_url, sizes: "512x512" }] : [],
    });
  } catch { /* ignore */ }
}

async function playAt(i) {
  if (i < 0 || i >= state.queue.length) return;
  const t = state.queue[i];
  state.index = i;
  highlightPlaying();
  $("np-title").textContent = t.title;
  $("np-artist").textContent = t.artist;
  $("nowplaying").classList.remove("empty");
  setArt(t.artwork_url);
  updateMediaSession(t);
  updateCastChip();
  try {
    if (onDevice()) {
      await apiPost(`/api/devices/${encodeURIComponent(state.target)}/play`,
        withVia({ service: t.service, id: t.id, meta: { title: t.title, artist: t.artist, album: t.album, art_url: t.artwork_url, duration_s: t.duration_s } }));
      state.devicePaused = false;
      setPlayIcon(true);
      startDevicePoll();
    } else {
      stopDevicePoll();
      const r = await api(`/api/resolve?service=${encodeURIComponent(t.service)}&id=${encodeURIComponent(t.id)}`);
      audio.src = `/stream/${r.token}${keyParam()}`;
      await audio.play();
    }
  } catch (e) {
    toastErr(`Couldn’t play “${t.title}”: ${e.message}`);
  }
}

$("np-device").addEventListener("change", (e) => {
  const prev = state.target;
  setTargetValue(e.target.value);
  if (prev === "browser" && onDevice()) audio.pause();
  if (!onDevice()) stopDevicePoll();
  updateCastChip();
});

$("np-play").addEventListener("click", async () => {
  if (onDevice()) {
    if (state.index < 0) { if (state.queue.length) return playAt(0); return; }
    try { await apiPost(`/api/devices/${encodeURIComponent(state.target)}/${state.devicePaused ? "resume" : "pause"}`, withVia({}));
      state.devicePaused = !state.devicePaused; setPlayIcon(!state.devicePaused); } catch { /* ignore */ }
    return;
  }
  if (!audio.src) { if (state.queue.length) playAt(0); return; }
  audio.paused ? audio.play() : audio.pause();
});
$("np-prev").addEventListener("click", () => playAt(state.index - 1));
$("np-next").addEventListener("click", () => playAt(state.index + 1));
audio.addEventListener("ended", () => playAt(state.index + 1));
audio.addEventListener("play", () => setPlayIcon(true));
audio.addEventListener("pause", () => setPlayIcon(false));
audio.addEventListener("loadedmetadata", () => {
  $("np-seek").max = Math.floor(audio.duration || 1);
  $("np-dur").textContent = fmtTime(audio.duration);
});
audio.addEventListener("timeupdate", () => {
  if (!seeking) { $("np-seek").value = Math.floor(audio.currentTime); }
  $("np-pos").textContent = fmtTime(audio.currentTime);
});
let seeking = false;
$("np-seek").addEventListener("input", () => { if (onDevice()) return; seeking = true; $("np-pos").textContent = fmtTime($("np-seek").value); });
$("np-seek").addEventListener("change", () => { if (onDevice()) return; audio.currentTime = Number($("np-seek").value); seeking = false; });
$("np-vol").addEventListener("input", () => {
  if (onDevice()) { apiPost(`/api/devices/${encodeURIComponent(state.target)}/volume`, withVia({ level: Number($("np-vol").value) })).catch(() => {}); }
  else { audio.volume = $("np-vol").value / 100; }
});

if ("mediaSession" in navigator) {
  const ms = navigator.mediaSession;
  ms.setActionHandler("play", () => $("np-play").click());
  ms.setActionHandler("pause", () => $("np-play").click());
  ms.setActionHandler("previoustrack", () => playAt(state.index - 1));
  ms.setActionHandler("nexttrack", () => playAt(state.index + 1));
}

async function loadDevices() {
  try {
    const devs = (await api("/api/devices?peers=1")).devices || [];
    const sel = $("np-device");
    const cur = sel.value;
    while (sel.options.length > 1) sel.remove(1);
    for (const d of devs) {
      const o = document.createElement("option");
      o.value = encodeTarget(d.host, d.via);
      o.textContent = d.via ? `${d.name} (via ${d.via_name || d.via})` : d.name;
      sel.appendChild(o);
    }
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  } catch { /* no devices */ }
}

// -- wiring -----------------------------------------------------------------

$("search").addEventListener("submit", (e) => { e.preventDefault(); const q = $("search-input").value.trim(); if (q) doSearch(q); });
document.querySelectorAll("#nav li[data-view]").forEach((el) => {
  el.addEventListener("click", () => setView(el.dataset.view));
  el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setView(el.dataset.view); } });
});
document.querySelectorAll("#mobilenav button").forEach((el) => el.addEventListener("click", () => setView(el.dataset.view)));
$("accounts").addEventListener("click", () => setView("accounts"));
loadAccounts();
loadDevices();

// Progressive web app: install + offline shell.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => { /* non-fatal */ }));
}
