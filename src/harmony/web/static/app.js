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
  section: "search", // last non-detail view (restored when a detail page is left)
  detail: false,     // true while an artist/album/track detail page is showing
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

// -- navigation: hash router + floating context menu ------------------------

// Detail pages are addressable so the browser Back button and deep links work:
// #/artist/<svc>/<id>, #/album/<svc>/<id>, #/track/<svc>/<id>. Ids are encoded
// (Qobuz ids are numeric, YT browseIds are opaque) so slashes never split a route.
const routeHref = (kind, service, id) =>
  `#/${kind}/${encodeURIComponent(service)}/${encodeURIComponent(id)}`;
const navigateArtist = (service, id) => { location.hash = routeHref("artist", service, id); };
const navigateAlbum = (service, id) => { location.hash = routeHref("album", service, id); };
const navigateTrack = (service, id) => { location.hash = routeHref("track", service, id); };

function parseHash() {
  const m = (location.hash || "").match(/^#\/(artist|album|track)\/([^/]+)\/(.+)$/);
  if (!m) return null;
  return { kind: m[1], service: decodeURIComponent(m[2]), id: decodeURIComponent(m[3]) };
}

// Swap the placeholder icon in any [data-art] box for its cover once it loads
// (mirrors the lazy-load used for playlist cards; a broken URL keeps the icon).
function hydrateArt(scope) {
  scope.querySelectorAll("[data-art]").forEach((el) => {
    const url = el.dataset.art;
    if (!url) return;
    const img = new Image();
    img.alt = "";
    img.className = "artimg";
    img.onload = () => { el.classList.remove("fallback"); el.replaceChildren(img); };
    img.src = url;
  });
}

// A floating menu that mirrors openAddMenu (outside-click + Escape close,
// keyboard reachable). `items` is [{label, fn}]; anchored at viewport (x, y).
function openContextMenu(x, y, items) {
  document.querySelectorAll(".addmenu").forEach((m) => m.remove());
  if (!items.length) return;
  const menu = document.createElement("div");
  menu.className = "addmenu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = items.map((it, i) =>
    `<div role="menuitem" tabindex="0" data-i="${i}">${esc(it.label)}</div>`).join("");
  document.body.appendChild(menu);
  const close = () => menu.remove();
  menu.querySelectorAll("[data-i]").forEach((el) => {
    const it = items[Number(el.dataset.i)];
    el.addEventListener("click", () => { close(); it.fn(); });
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); close(); it.fn(); }
    });
  });
  menu.style.top = `${Math.min(y, window.innerHeight - menu.offsetHeight - 8)}px`;
  menu.style.left = `${Math.min(Math.max(8, x), window.innerWidth - menu.offsetWidth - 8)}px`;
  document.addEventListener("keydown", function onEsc(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", onEsc); }
  });
  setTimeout(() => document.addEventListener("click", close, { once: true }), 0);
  const first = menu.querySelector("[data-i]");
  if (first) first.focus();
}

function openTrackContextMenu(e, t) {
  const items = [];
  if (t.artist_ids && t.artist_ids[0])
    items.push({ label: "Go to artist", fn: () => navigateArtist(t.service, t.artist_ids[0]) });
  if (t.album_id)
    items.push({ label: "Go to album", fn: () => navigateAlbum(t.service, t.album_id) });
  if (!items.length) return;
  e.preventDefault();
  openContextMenu(e.clientX, e.clientY, items);
}

function openAlbumContextMenu(e, a) {
  const items = [];
  if (a.id) items.push({ label: "Go to album", fn: () => navigateAlbum(a.service, a.id) });
  if (a.artist_ids && a.artist_ids[0])
    items.push({ label: "Go to artist", fn: () => navigateArtist(a.service, a.artist_ids[0]) });
  if (!items.length) return;
  e.preventDefault();
  openContextMenu(e.clientX, e.clientY, items);
}

const _truncate = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : (s || ""));
function spanLabel(spans) {
  if (!spans || !spans.length) return "";
  return spans.map((sp) => `${sp[0] == null ? "?" : sp[0]}–${sp[1] == null ? "present" : sp[1]}`).join(", ");
}

// -- rendering --------------------------------------------------------------

// The artist name in a track row links to the artist page when the provider
// gave us an id (`artist_ids` runs parallel to the underlying artist list).
function trackArtistCell(t) {
  if (t.artist_ids && t.artist_ids[0])
    return `<a class="artist" href="${routeHref("artist", t.service, t.artist_ids[0])}">${esc(t.artist)}</a>`;
  return `<div class="artist">${esc(t.artist)}</div>`;
}

function trackRowHtml(t, i, opts = {}) {
  const pl = state.playlist;
  const num = opts.numbered ? `<span class="tnum">${t.track_number != null ? t.track_number : i + 1}</span>` : "";
  return `
    <div class="trow${opts.numbered ? " numbered" : ""}" data-i="${i}">
      <div class="tlead">${num}<button class="play" aria-label="Play ${esc(t.title)}">${ICON("play")}</button></div>
      <div class="title"><span class="tt">${esc(t.title)}</span>${opts.hideBadge ? "" : `<span class="badge">${esc(serviceLabel(t.service))}</span>`}</div>
      ${trackArtistCell(t)}
      <div class="dur">${fmtTime(t.duration_s)}</div>
      <div class="rowacts">
        <button class="mini add" aria-label="Add to playlist">${ICON("add")}</button>
        ${pl ? `<button class="mini rem" aria-label="Remove from this playlist">${ICON("remove")}</button>` : ""}
      </div>
    </div>`;
}

const tracksHtml = (tracks, opts = {}) =>
  `<div class="tracks">${tracks.map((t, i) => trackRowHtml(t, i, opts)).join("")}</div>`;

// Wire a rendered set of track rows to shared playback + row menus. The caller
// owns `state.queue` (so playAt indexes correctly); we only attach handlers.
function wireTrackRows(scope, tracks) {
  scope.querySelectorAll(".trow").forEach((row) => {
    const i = Number(row.dataset.i);
    row.querySelector(".play").addEventListener("click", () => playAt(i));
    const add = row.querySelector(".add");
    if (add) add.addEventListener("click", (e) => { e.preventDefault(); openAddMenu(e.currentTarget, tracks[i]); });
    const rem = row.querySelector(".rem");
    if (rem) rem.addEventListener("click", () => removeFromPlaylist(tracks[i], i));
    row.addEventListener("contextmenu", (e) => openTrackContextMenu(e, tracks[i]));
  });
}

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
  list.innerHTML = toolbar + tracksHtml(tracks, opts);
  state.queue = tracks;
  wireTrackRows(list, tracks);
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
  state.section = view;
  state.detail = false;
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

// -- shared detail-page building blocks -------------------------------------

// A chronological album list. Rows with a provider id navigate to the album
// page; rows whose id is null (a PERSON's MusicBrainz "performed-on" credits,
// with no provider match) are informational — shown, but not playable.
function albumRowsHtml(albums, opts = {}) {
  return albums.map((a) => {
    const nav = a.id != null && a.id !== "";
    const yr = a.year != null ? a.year : (a.date ? String(a.date).slice(0, 4) : "");
    const aids = (a.artist_ids || []).join(",");
    const inner = `
      <div class="alb-year">${esc(yr || "—")}</div>
      <div class="alb-art" data-art="${esc(a.artwork_url || "")}">${ICON("music")}</div>
      <div class="alb-meta">
        <div class="alb-title">${esc(a.title)}</div>
        ${opts.showArtist && a.artist ? `<div class="alb-artist muted">${esc(a.artist)}</div>` : ""}
      </div>`;
    const data = `data-svc="${esc(a.service)}" data-id="${esc(a.id || "")}" data-aids="${esc(aids)}"`;
    if (nav)
      return `<a class="albrow" ${data} href="${routeHref("album", a.service, a.id)}">${inner}</a>`;
    return `<div class="albrow info" ${data} title="From MusicBrainz credits — not available to play">${inner}<span class="badge">credit</span></div>`;
  }).join("");
}

function wireAlbumRows(scope) {
  scope.querySelectorAll(".albrow").forEach((row) => {
    row.addEventListener("contextmenu", (e) => openAlbumContextMenu(e, {
      service: row.dataset.svc,
      id: row.dataset.id || null,
      artist_ids: row.dataset.aids ? row.dataset.aids.split(",").filter(Boolean) : [],
    }));
  });
}

// Members / bands / performers — clicking one runs a smart search for the name
// (these come from MusicBrainz and carry no provider id of their own).
function peopleChipsHtml(people, opts = {}) {
  return `<div class="chips">${people.map((p) => {
    const sub = opts.instruments && p.instruments && p.instruments.length
      ? p.instruments.join(", ") : spanLabel(p.spans);
    return `<button class="chip" data-name="${esc(p.name)}">
      <span class="chip-name">${esc(p.name)}${opts.current && p.is_current ? ` <span class="badge">current</span>` : ""}</span>
      ${sub ? `<span class="chip-sub muted">${esc(sub)}</span>` : ""}</button>`;
  }).join("")}</div>`;
}

function wireChips(scope) {
  scope.querySelectorAll(".chip[data-name]").forEach((c) =>
    c.addEventListener("click", () => { $("search-input").value = c.dataset.name; doSmartSearch(c.dataset.name); }));
}

function bioHtml(bio) {
  if (!bio || !bio.text) return "";
  const label = bio.source === "wikipedia" ? "Wikipedia" : "source";
  const src = bio.url
    ? `<p class="muted bio-src">From <a class="link" href="${esc(bio.url)}" target="_blank" rel="noopener">${label}</a></p>`
    : "";
  return `<section class="detail-sec"><h3>About</h3><p class="bio-text">${esc(bio.text)}</p>${src}</section>`;
}

function detailHeader(title, subHtml, artUrl, kind) {
  return `
    <div class="detail-head">
      <button class="backbtn" id="detail-back" aria-label="Go back">${ICON("prev")} Back</button>
    </div>
    <div class="detail-hero">
      <div class="detail-art" data-art="${esc(artUrl || "")}">${ICON("music")}</div>
      <div class="detail-herometa">
        ${kind ? `<div class="detail-kind">${esc(kind)}</div>` : ""}
        <h2 class="detail-title">${esc(title)}</h2>
        ${subHtml ? `<div class="detail-sub">${subHtml}</div>` : ""}
      </div>
    </div>`;
}

function wireBack() {
  const b = $("detail-back");
  if (!b) return;
  b.onclick = () => {
    if (history.length > 1) history.back();
    else { history.replaceState(null, "", location.pathname + location.search); showSection("search"); }
  };
}

// -- detail views -----------------------------------------------------------

async function renderArtistView(service, id) {
  const list = $("list");
  state.detail = true;
  state.playlist = null;
  highlightNav("");
  $("view-title").textContent = "Artist";
  list.innerHTML = loadingState("Loading artist…");
  let d;
  try { d = await api(`/api/artist/${encodeURIComponent(service)}/${encodeURIComponent(id)}`); }
  catch (e) { list.innerHTML = errorState("Couldn’t load this artist", e.message, "ar-retry"); $("ar-retry").onclick = () => renderArtistView(service, id); return; }

  const a = d.artist || {};
  $("view-title").textContent = a.name || "Artist";
  const kindLabel = d.kind === "group" ? "Group" : "Artist";
  const isPerson = d.kind === "person";

  const albums = d.albums || [];
  const albumsSec = albums.length ? `
    <section class="detail-sec">
      <h3>${isPerson ? "Appears on" : "Discography"}</h3>
      <div class="albrows">${albumRowsHtml(albums, { showArtist: isPerson })}</div>
    </section>` : "";

  const chartSec = d.chronology ? `
    <section class="detail-sec">
      <h3>Timeline</h3>
      <div class="chrono-wrap">${buildChronologySvg(d.chronology)}</div>
    </section>` : "";

  const top = d.top_tracks || [];
  const topSec = top.length ? `<section class="detail-sec"><h3>Top tracks</h3>${tracksHtml(top)}</section>` : "";

  const members = d.members || [];
  const bands = d.member_of || [];
  let peopleSec = "";
  if (members.length) peopleSec += `<section class="detail-sec"><h3>Members</h3>${peopleChipsHtml(members, { instruments: true, current: true })}</section>`;
  if (bands.length) peopleSec += `<section class="detail-sec"><h3>Member of</h3>${peopleChipsHtml(bands, {})}</section>`;

  list.innerHTML = `<div class="detail">
    ${detailHeader(a.name || "Unknown artist", "", a.image_url, kindLabel)}
    ${bioHtml(a.bio)}
    ${chartSec}
    ${albumsSec}
    ${topSec}
    ${peopleSec}
  </div>`;
  wireBack();
  hydrateArt(list);
  wireAlbumRows(list);
  wireChips(list);
  if (top.length) { state.queue = top; wireTrackRows(list, top); highlightPlaying(); }
}

async function renderAlbumView(service, id) {
  const list = $("list");
  state.detail = true;
  state.playlist = null;
  highlightNav("");
  $("view-title").textContent = "Album";
  list.innerHTML = loadingState("Loading album…");
  let d;
  try { d = await api(`/api/album/${encodeURIComponent(service)}/${encodeURIComponent(id)}`); }
  catch (e) { list.innerHTML = errorState("Couldn’t load this album", e.message, "al-retry"); $("al-retry").onclick = () => renderAlbumView(service, id); return; }

  const al = d.album || {};
  const ref = d.artist_ref;
  $("view-title").textContent = al.title || "Album";
  const yr = al.year != null ? al.year : (al.date ? String(al.date).slice(0, 4) : "");
  const artistHtml = ref
    ? `<a class="link" href="${routeHref("artist", ref.service, ref.id)}">${esc(ref.name)}</a>`
    : esc(al.artist || "");
  const bits = [artistHtml, yr ? esc(String(yr)) : "", al.track_count != null ? esc(nTracks(al.track_count)) : ""]
    .filter(Boolean).join(" · ");
  const tracks = d.tracks || [];
  const tracksSec = tracks.length
    ? tracksHtml(tracks, { numbered: true, hideBadge: true })
    : emptyState("music", "No tracks", "This album has no playable tracks right now.");

  list.innerHTML = `<div class="detail">
    ${detailHeader(al.title || "Album", bits, al.artwork_url, "Album")}
    ${bioHtml(d.bio)}
    <section class="detail-sec">${tracksSec}</section>
  </div>`;
  wireBack();
  hydrateArt(list);
  if (tracks.length) { state.queue = tracks; wireTrackRows(list, tracks); highlightPlaying(); }
}

async function renderTrackView(service, id) {
  const list = $("list");
  state.detail = true;
  state.playlist = null;
  highlightNav("");
  $("view-title").textContent = "Track";
  list.innerHTML = loadingState("Loading track…");
  let d;
  try { d = await api(`/api/track/${encodeURIComponent(service)}/${encodeURIComponent(id)}`); }
  catch (e) { list.innerHTML = errorState("Couldn’t load this track", e.message, "tk-retry"); $("tk-retry").onclick = () => renderTrackView(service, id); return; }

  const t = d.track || {};
  $("view-title").textContent = t.title || "Track";
  const refs = d.artist_refs || [];
  const artistsHtml = (list2) => list2.map((r) =>
    `<a class="link" href="${routeHref("artist", r.service, r.id)}">${esc(r.name)}</a>`).join(", ");
  const artistHtml = refs.length ? artistsHtml(refs) : esc(t.artist || "");
  const albumHtml = d.album_ref
    ? `<a class="link" href="${routeHref("album", d.album_ref.service, d.album_ref.id)}">${esc(d.album_ref.title)}</a>`
    : esc(t.album || "");
  const meta = [artistHtml, albumHtml, t.year ? esc(String(t.year)) : "", t.duration_s ? fmtTime(t.duration_s) : ""]
    .filter(Boolean).join(" · ");

  const perf = d.performers || [];
  let perfSec;
  if (perf.length) {
    perfSec = `<section class="detail-sec"><h3>Performers</h3>
      <div class="perf">${perf.map((p) => `
        <div class="perf-row">
          <button class="chip" data-name="${esc(p.name)}"><span class="chip-name">${esc(p.name)}</span></button>
          <span class="perf-roles muted">${esc((p.roles || []).join(", "))}</span>
        </div>`).join("")}</div></section>`;
  } else {
    const credited = refs.length ? artistsHtml(refs) : esc(t.artist || "");
    perfSec = `<section class="detail-sec"><h3>Performers</h3>
      ${credited ? `<p class="credited">${credited}</p>` : ""}
      <p class="muted">Detailed performer credits aren’t in MusicBrainz for this recording.</p></section>`;
  }

  list.innerHTML = `<div class="detail">
    ${detailHeader(t.title || "Track", meta, t.artwork_url, "Track")}
    <section class="detail-sec">
      <button class="act" id="tk-play">${ICON("play")} Play track</button>
    </section>
    ${perfSec}
  </div>`;
  wireBack();
  hydrateArt(list);
  wireChips(list);
  $("tk-play").onclick = () => { state.queue = [t]; playAt(0); };
}

// -- member-chronology timeline chart (inline SVG, theme-aware, scrollable) --

function buildChronologySvg(c) {
  const start = c.start_year;
  const end = Math.max(c.end_year, start + 1);
  const span = end - start;
  const members = c.members || [];
  const albums = (c.albums || []).filter((a) => a.year != null);

  const labelW = 150, rightPad = 26;
  const plotW = Math.max(span * 42, 360);       // long timelines overflow → scroll
  const yearW = plotW / span;
  const X = (y) => labelW + (Math.max(start, Math.min(end, y)) - start) * yearW;

  const topLabels = albums.length ? 84 : 10;    // room for rotated album titles
  const axisH = 26, rowH = 38, barH = 18;
  const axisY = topLabels;
  const lanesTop = topLabels + axisH;
  const height = lanesTop + members.length * rowH + 14;
  const width = labelW + plotW + rightPad;

  const maxTicks = Math.max(2, Math.floor(plotW / 52));
  const step = [1, 2, 5, 10, 20, 25, 50, 100].find((s) => span / s <= maxTicks) || 100;
  const ticks = [];
  for (let y = Math.ceil(start / step) * step; y <= end; y += step) ticks.push(y);
  if (!ticks.length || ticks[0] !== start) ticks.unshift(start);

  let svg = "";
  members.forEach((m, k) => {
    if (k % 2 === 1)
      svg += `<rect class="chrono-stripe" x="${labelW}" y="${(lanesTop + k * rowH).toFixed(1)}" width="${plotW.toFixed(1)}" height="${rowH}"/>`;
  });
  albums.forEach((a) => {
    const x = X(a.year), ty = axisY - 8;
    svg += `<line class="chrono-albline" x1="${x.toFixed(1)}" y1="${axisY}" x2="${x.toFixed(1)}" y2="${height - 8}"/>`;
    svg += `<text class="chrono-albtitle" x="${x.toFixed(1)}" y="${ty}" transform="rotate(-40 ${x.toFixed(1)} ${ty})">${esc(_truncate(a.title, 22))} · ${esc(String(a.year))}</text>`;
  });
  svg += `<line class="chrono-axis" x1="${labelW}" y1="${axisY}" x2="${(width - rightPad).toFixed(1)}" y2="${axisY}"/>`;
  ticks.forEach((y) => {
    const x = X(y);
    svg += `<line class="chrono-tick" x1="${x.toFixed(1)}" y1="${axisY}" x2="${x.toFixed(1)}" y2="${axisY + 5}"/>`;
    svg += `<text class="chrono-year" x="${x.toFixed(1)}" y="${axisY + 18}" text-anchor="middle">${y}</text>`;
  });
  members.forEach((m, k) => {
    const cy = lanesTop + k * rowH + rowH / 2;
    const barY = cy - barH / 2;
    (m.spans || []).forEach((sp) => {
      const x1 = X(sp[0] == null ? start : sp[0]);
      const x2 = X(sp[1] == null ? end : sp[1]);
      svg += `<rect class="chrono-bar" x="${x1.toFixed(1)}" y="${barY.toFixed(1)}" width="${Math.max(6, x2 - x1).toFixed(1)}" height="${barH}" rx="4"><title>${esc(m.name)}: ${sp[0] == null ? "?" : sp[0]}–${sp[1] == null ? "present" : sp[1]}</title></rect>`;
    });
    const instr = (m.instruments && m.instruments.length) ? m.instruments[0] : "";
    const nameY = instr ? cy - 5 : cy;
    svg += `<text class="chrono-name" x="${labelW - 12}" y="${nameY.toFixed(1)}" text-anchor="end" dominant-baseline="middle">${esc(_truncate(m.name, 20))}</text>`;
    if (instr)
      svg += `<text class="chrono-instr" x="${labelW - 12}" y="${(cy + 9).toFixed(1)}" text-anchor="end" dominant-baseline="middle">${esc(_truncate(instr, 18))}</text>`;
  });

  return `<svg class="chrono" width="${width.toFixed(0)}" height="${height}" viewBox="0 0 ${width.toFixed(0)} ${height}" role="img" aria-label="Member timeline, ${start} to ${end}">${svg}</svg>`;
}

// -- smart search -----------------------------------------------------------

async function doSmartSearch(q) {
  state.playlist = null;
  state.detail = false;
  state.section = "search";
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  highlightNav("search");
  $("view-title").textContent = "Search";
  if ($("search-input").value !== q) $("search-input").value = q;
  $("list").innerHTML = loadingState(`Searching for “${q}”…`);
  let r;
  try { r = await api(`/api/search/smart?q=${encodeURIComponent(q)}`); }
  catch (e) { $("list").innerHTML = errorState("Search failed", e.message, "s-retry"); $("s-retry").onclick = () => doSmartSearch(q); return; }
  renderSmartResults(r, q);
}

// Sections render top-to-bottom in the spec order: artist discography (if a
// confident name match), then album-title matches, then incidental hits.
function renderSmartResults(r, q) {
  const list = $("list");
  const inc = r.incidental || {};
  let html = "";

  if (r.artist) {
    const a = r.artist;
    html += `<section class="detail-sec">
      <div class="sec-head">
        <h3>${esc(a.ref.name)}</h3>
        <a class="link" href="${routeHref("artist", a.ref.service, a.ref.id)}">View artist →</a>
      </div>
      <div class="muted sec-sub">${a.kind === "person" ? "Appears on" : "Discography"}</div>
      ${a.albums && a.albums.length
        ? `<div class="albrows">${albumRowsHtml(a.albums, { showArtist: a.kind === "person" })}</div>`
        : `<p class="muted">No albums found.</p>`}
    </section>`;
  }
  if (r.albums && r.albums.length) {
    html += `<section class="detail-sec"><h3>Albums</h3>
      <div class="albrows">${albumRowsHtml(r.albums, { showArtist: true })}</div></section>`;
  }
  const tracks = inc.tracks || [];
  if (tracks.length) html += `<section class="detail-sec"><h3>Tracks</h3>${tracksHtml(tracks)}</section>`;
  if (inc.artists && inc.artists.length) {
    html += `<section class="detail-sec"><h3>Artists</h3><div class="chips">${inc.artists.map((ar) =>
      `<a class="chip" href="${routeHref("artist", ar.service, ar.id)}"><span class="chip-name">${esc(ar.name)}</span><span class="chip-sub muted">${esc(serviceLabel(ar.service))}</span></a>`).join("")}</div></section>`;
  }
  if (inc.playlists && inc.playlists.length) {
    html += `<section class="detail-sec"><h3>Playlists</h3><div class="plgrid">${inc.playlists.map((p) =>
      `<div class="plcard" data-service="${esc(p.service)}" data-id="${esc(p.id)}" data-art="${esc(p.artwork_url || "")}">
        <div class="art">${ICON("music")}</div>
        <div class="t">${esc(p.title)}</div>
        <div class="s">${esc(serviceLabel(p.service))}${p.track_count != null ? " · " + nTracks(p.track_count) : ""}</div>
      </div>`).join("")}</div></section>`;
  }

  if (!html) {
    list.innerHTML = emptyState("search", `No results for “${q}”`, "Try a different title or artist.");
    return;
  }
  list.innerHTML = html;
  hydrateArt(list);
  wireAlbumRows(list);
  if (tracks.length) { state.queue = tracks; wireTrackRows(list, tracks); highlightPlaying(); }
  list.querySelectorAll(".plcard").forEach((card) => {
    const url = card.dataset.art;
    if (url) { const img = new Image(); img.className = "art"; img.alt = ""; img.onload = () => { const slot = card.querySelector(".art"); if (slot) slot.replaceWith(img); }; img.src = url; }
    card.addEventListener("click", () => openPlaylist(card.dataset.service, card.dataset.id, card.querySelector(".t").textContent));
  });
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

// Switch to a section view, leaving any detail page. Detail pages live in the
// URL hash; clearing it (replaceState — no extra history entry) returns here.
function showSection(view) {
  const leavingDetail = state.detail || !!$("list").querySelector(".detail");
  state.detail = false;
  // setView("search") intentionally leaves #list untouched (search keeps its
  // results), so coming from a detail page we reset it to the hint first.
  if (view === "search" && leavingDetail)
    $("list").innerHTML = `<p class="hint">Search for a song, or open your playlists.</p>`;
  setView(view);
}
function goView(view) {
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  showSection(view);
}

function renderRoute() {
  const r = parseHash();
  if (!r) { if (state.detail) showSection(state.section || "search"); return; }
  if (r.kind === "artist") renderArtistView(r.service, r.id);
  else if (r.kind === "album") renderAlbumView(r.service, r.id);
  else if (r.kind === "track") renderTrackView(r.service, r.id);
}

$("search").addEventListener("submit", (e) => { e.preventDefault(); const q = $("search-input").value.trim(); if (q) doSmartSearch(q); });
document.querySelectorAll("#nav li[data-view]").forEach((el) => {
  el.addEventListener("click", () => goView(el.dataset.view));
  el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); goView(el.dataset.view); } });
});
document.querySelectorAll("#mobilenav button").forEach((el) => el.addEventListener("click", () => goView(el.dataset.view)));
$("accounts").addEventListener("click", () => goView("accounts"));
window.addEventListener("hashchange", renderRoute);
loadAccounts();
loadDevices();
if (parseHash()) renderRoute();   // deep link → render the detail page on load

// Progressive web app: install + offline shell.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => { /* non-fatal */ }));
}
