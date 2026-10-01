"use strict";
// Harmony web client. Talks to the same HTTP API the mobile app uses; audio
// plays in the browser via <audio> pointed at the same-origin /stream proxy.

const $ = (id) => document.getElementById(id);
const audio = $("audio");

// -- the play-queue model ----------------------------------------------------
// A line-for-line mirror of src/harmony/playqueue.py so the browser output and
// the server-owned device queues follow the same rules. Index-based: `tracks`
// is the whole active list (history + current + up next), `index` the playing
// item. Items are tracked by identity through reorders (duplicates are fine).
// Every op returns the index to start now, or null to leave playback alone.
const REPEAT_MODES = ["off", "all", "one"];
const RESTART_AFTER_S = 3;   // Previous restarts the current track past this point

function shuffledCopy(a) {
  const r = a.slice();
  for (let i = r.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [r[i], r[j]] = [r[j], r[i]];
  }
  return r;
}

class PlayQueue {
  constructor() { this.tracks = []; this.index = -1; this.original = []; this.shuffle = false; this.repeat = "off"; }
  current() { return this.index >= 0 && this.index < this.tracks.length ? this.tracks[this.index] : null; }
  upcoming() { return this.index >= 0 ? this.tracks.slice(this.index + 1) : this.tracks.slice(); }
  // Index after the current one (null = done). Repeat-one only holds on a
  // natural track end — Next still moves on.
  following(manual = false) {
    if (!this.tracks.length) return null;
    if (this.repeat === "one" && !manual && this.index >= 0) return this.index;
    if (this.index + 1 < this.tracks.length) return this.index + 1;
    if (this.repeat === "all") return 0;
    return null;
  }
  // start=null → "shuffle play" (random first track when shuffle is on);
  // keepOrder → take the list as already arranged (an output hand-off).
  load(tracks, start = 0, shuffle = null, keepOrder = false) {
    tracks = tracks.slice();
    if (!tracks.length) return null;
    if (shuffle !== null && shuffle !== undefined) this.shuffle = !!shuffle;
    this.original = tracks.slice();
    if (this.shuffle && !keepOrder) {
      let rest;
      if (start == null) rest = shuffledCopy(tracks);
      else {
        start = Math.max(0, Math.min(start, tracks.length - 1));
        rest = shuffledCopy(tracks.slice(0, start).concat(tracks.slice(start + 1)));
        rest.unshift(tracks[start]);
      }
      this.tracks = rest; this.index = 0;
    } else {
      this.tracks = tracks; this.index = Math.max(0, Math.min(start || 0, tracks.length - 1));
    }
    return this.index;
  }
  jump(i) { if (!(i >= 0 && i < this.tracks.length)) return null; this.index = i; return i; }
  advance(manual = false) { const n = this.following(manual); if (n !== null) this.index = n; return n; }
  previous(pos) {
    if (!this.tracks.length) return null;
    if ((pos || 0) > RESTART_AFTER_S || this.index < 0) { this.index = Math.max(this.index, 0); return this.index; }
    if (this.index > 0) this.index -= 1;
    else if (this.repeat === "all") this.index = this.tracks.length - 1;
    return this.index;
  }
  enqueue(tracks, idle) {
    if (!tracks.length) return null;
    const first = this.tracks.length;
    this.tracks.push(...tracks); this.original.push(...tracks);
    if (idle) { this.index = first; return first; }
    return null;
  }
  playNext(tracks, idle) {
    if (!tracks.length) return null;
    const at = this.index >= 0 ? this.index + 1 : 0;
    this.tracks.splice(at, 0, ...tracks); this.original.push(...tracks);
    if (idle) { this.index = at; return at; }
    return null;
  }
  move(src, dst) {
    const n = this.tracks.length;
    if (!(src >= 0 && src < n && dst >= 0 && dst < n) || src === dst) return;
    const cur = this.current();
    this.tracks.splice(dst, 0, this.tracks.splice(src, 1)[0]);
    this._reanchor(cur);
    if (!this.shuffle) this.original = this.tracks.slice();   // a manual order IS the order now
  }
  // → [removedCurrent, indexToStart]
  remove(i) {
    if (!(i >= 0 && i < this.tracks.length)) return [false, null];
    const item = this.tracks.splice(i, 1)[0];
    this.original = this.original.filter((t) => t !== item);
    if (i < this.index) { this.index -= 1; return [false, null]; }
    if (i > this.index) return [false, null];
    if (this.index < this.tracks.length) return [true, this.index];
    this.index = this.tracks.length - 1;
    return [true, null];
  }
  // Drop everything but the current track (it keeps playing).
  clear() {
    const cur = this.current();
    this.tracks = cur ? [cur] : [];
    this.original = this.tracks.slice();
    this.index = cur ? 0 : -1;
  }
  setShuffle(on) {
    on = !!on;
    if (on === this.shuffle) return;
    this.shuffle = on;
    const cur = this.current();
    if (on) {
      const head = cur ? this.tracks.slice(0, this.index + 1) : [];
      this.tracks = head.concat(shuffledCopy(this.upcoming()));
    } else {
      const seen = new Set(this.original);
      this.tracks = this.original.concat(this.tracks.filter((t) => !seen.has(t)));
      this._reanchor(cur);
    }
  }
  setRepeat(mode) { if (!REPEAT_MODES.includes(mode)) throw new Error("repeat must be off|all|one"); this.repeat = mode; }
  _reanchor(cur) { if (!cur) return; const i = this.tracks.indexOf(cur); if (i >= 0) this.index = i; }
}

// The ACTIVE play queue: what auto-advance, prev/next and the Media Session act
// on. It survives navigation and changes only through explicit play / queue ops
// (or, on a device, the server's snapshot) — never as a side effect of browsing.
const pq = new PlayQueue();

const state = {
  // The currently DISPLAYED list (search results / album / playlist). Used only
  // for rendering + highlighting — NEVER for playback.
  queue: [],
  get activeQueue() { return pq.tracks; },
  get activeIndex() { return pq.index; },
  get shuffle() { return pq.shuffle; },
  get repeat() { return pq.repeat; },
  playlist: null, // {service, id, title} when viewing an editable playlist, else null
  target: "browser", // "browser" (this tab's <audio>) or a device host to cast to
  targetVia: null,   // peer "host:port" when the device lives on another instance's LAN
  section: "search", // last non-detail view (restored when a detail page is left)
  detail: false,     // true while an artist/album/track detail page is showing
  lidarr: null,      // cached GET /api/lidarr status ({enabled, configured, …}) or null
  library: null,     // cached GET /api/library status ({enabled, paths, stats, scan, …}) or null
  dragging: false,   // a Now Playing row is being dragged (hold off re-renders)
};

// Shuffle/repeat/volume are user prefs — persisted alongside the personal key.
let _prefVolume = 100;
try {
  const _p = JSON.parse(localStorage.getItem("harmonyPrefs") || "{}");
  if (_p && typeof _p === "object") {
    pq.shuffle = !!_p.shuffle;
    if (REPEAT_MODES.includes(_p.repeat)) pq.repeat = _p.repeat;
    if (typeof _p.volume === "number" && _p.volume >= 0 && _p.volume <= 100) _prefVolume = _p.volume;
  }
} catch (_e) { /* defaults */ }
function persistPrefs() {
  try { localStorage.setItem("harmonyPrefs", JSON.stringify({ shuffle: pq.shuffle, repeat: pq.repeat, volume: _prefVolume })); }
  catch (_e) { /* ignore */ }
}

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
const isTouch = () => !!(window.matchMedia && window.matchMedia("(hover: none)").matches);

const ICON = (name, extra) => `<svg class="ico${extra ? " " + extra : ""}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

const fmtTime = (s) => {
  if (!s || s < 0 || !isFinite(s)) return "0:00";
  const t = Math.floor(s), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  const mm = h ? String(m).padStart(2, "0") : m;
  return (h ? `${h}:` : "") + `${mm}:${String(sec).padStart(2, "0")}`;
};
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
const serviceLabel = (s) => ({ ytmusic: "YouTube Music", qobuz: "Qobuz", local: "Library" }[s] || s);
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

// -- Lidarr ("Get with Lidarr") ---------------------------------------------
// A right-click item that hands an album/artist to this instance's Lidarr to
// acquire. Status is fetched once and cached on state so the menu can decide
// synchronously whether to show the item; renderAccounts re-fetches on save.

async function loadLidarrStatus() {
  try { state.lidarr = await api("/api/lidarr"); }
  catch { state.lidarr = null; }
  return state.lidarr;
}
const lidarrEnabled = () => !!(state.lidarr && state.lidarr.enabled);

async function sendToLidarr(body, label) {
  try {
    const r = await apiPost("/api/lidarr/request", body);
    toast(`Sent “${r.title || label}” to Lidarr.`, "ok");
  } catch (e) { toastErr("Lidarr couldn’t take that: " + e.message); }
}

// Build the (0-or-1) "Get with Lidarr" menu items for an album/artist target.
// `mbid` (when the entity data carries one) is passed through for an exact match.
function lidarrMenuItems(o) {
  if (!lidarrEnabled()) return [];
  const body = { kind: o.kind };
  if (o.title) body.title = o.title;
  if (o.artist) body.artist = o.artist;
  if (o.mbid) body.mbid = o.mbid;
  const label = o.title || o.artist || "item";
  return [{ label: "Get with Lidarr", fn: () => sendToLidarr(body, label) }];
}

// Attach a "Get with Lidarr" (artist) right-click menu to any element carrying
// data-lidarr-artist (optional data-lidarr-mbid). Used for artist/people chips
// and the artist detail hero.
function wireLidarrArtistTargets(scope) {
  scope.querySelectorAll("[data-lidarr-artist]").forEach((el) => {
    el.addEventListener("contextmenu", (e) => {
      const items = lidarrMenuItems({
        kind: "artist", artist: el.dataset.lidarrArtist, mbid: el.dataset.lidarrMbid || "",
      });
      if (!items.length) return;
      e.preventDefault();
      openContextMenu(e.clientX, e.clientY, items);
    });
  });
}

// -- local library + the Lidarr loop ----------------------------------------
// The instance's music folders are the "local" service (search, pages, play,
// cast like any other). Albums found elsewhere get a state badge — in library /
// downloading / wanted — so "Get with Lidarr" visibly closes the loop.

async function loadLibraryStatus() {
  try { state.library = await api("/api/library"); }
  catch { state.library = null; }
  return state.library;
}
const libraryEnabled = () => !!(state.library && state.library.enabled);

const ALBUM_STATE = {
  library: { label: "In library", title: "In your library — right-click to play the local copy" },
  downloading: { label: "", title: "Lidarr is downloading this" },
  wanted: { label: "Wanted", title: "Monitored in Lidarr — not downloaded yet" },
  imported: { label: "In Lidarr", title: "Lidarr has the files, but they're not in this library — check the library folders / path mapping" },
};
function albumStateLabel(st) {
  if (st.state === "downloading") return `↓ ${Math.round(st.progress || 0)}%`;
  if (st.state === "wanted" && st.total) return `Wanted · ${st.have || 0}/${st.total}`;
  return (ALBUM_STATE[st.state] || {}).label || "";
}

// Badge every non-library album row in `scope` with where it stands
// (one batched request; silently skipped when neither library nor Lidarr is on).
async function annotateAlbumStates(scope) {
  if (!libraryEnabled() && !lidarrEnabled()) return;
  const rows = [...scope.querySelectorAll(".albrow")].filter((r) => r.dataset.svc !== "local" && r.dataset.title);
  if (!rows.length) return;
  let r;
  try {
    r = await apiPost("/api/library/state", { albums: rows.map((row) => ({
      title: row.dataset.title, artist: row.dataset.artist, mbid: row.dataset.mbid || null })) });
  } catch { return; }
  (r.states || []).forEach((st, i) => {
    const row = rows[i];
    if (!row || !row.isConnected || st.state === "none") return;
    const slot = row.querySelector(".alb-state");
    if (slot) slot.innerHTML = `<span class="badge st st-${esc(st.state)}" title="${esc((ALBUM_STATE[st.state] || {}).title || "")}">${esc(albumStateLabel(st))}</span>`;
    if (st.ref) row.dataset.localId = st.ref.id;
  });
}

// The status line under a (non-library) album's header.
async function renderAlbumState(slot, album) {
  if (!slot || (!libraryEnabled() && !lidarrEnabled())) return;
  let r;
  try { r = await apiPost("/api/library/state", { albums: [{ title: album.title, artist: album.artist, mbid: album.mbid || null }] }); }
  catch { return; }
  const st = (r.states || [])[0];
  if (!st || st.state === "none" || !slot.isConnected) return;
  const text = {
    library: "This album is in your library.",
    downloading: `Lidarr is downloading this album (${Math.round(st.progress || 0)}%).`,
    wanted: `Wanted in Lidarr${st.total ? ` — ${st.have || 0} of ${st.total} tracks so far` : ""}.`,
    imported: "Lidarr has this album, but it isn't in this library yet — check the library folders or path mapping.",
  }[st.state] || "";
  slot.innerHTML = `<div class="albstate st-${esc(st.state)}"><span>${esc(text)}</span>${st.ref
    ? `<button class="act small" id="als-play" type="button">${ICON("play")} Play library copy</button>
       <a class="link" href="${routeHref("album", "local", st.ref.id)}">Open</a>` : ""}</div>`;
  if (st.ref && $("als-play")) $("als-play").onclick = () => albumToQueue({ service: "local", id: st.ref.id, title: st.ref.title }, "play");
}

let _libTimer = null;
function downloadsHtml(items) {
  if (!items.length) return `<p class="muted">Nothing downloading right now.</p>`;
  return items.map((d) => `<div class="dl">
      <div class="dl-meta"><div class="dl-title">${esc(d.title || d.release || "Unknown")}</div>
        <div class="muted dl-sub">${esc([d.artist, d.status, d.timeleft ? "≈" + d.timeleft : "", d.client].filter(Boolean).join(" · "))}</div>
        ${d.error ? `<div class="dl-err">${esc(d.error)}</div>` : ""}</div>
      <div class="dl-bar" role="progressbar" aria-valuenow="${esc(Math.round(d.progress || 0))}" aria-valuemin="0" aria-valuemax="100"><span style="width:${Math.max(0, Math.min(100, d.progress || 0))}%"></span></div>
      <div class="dl-pct muted">${esc(Math.round(d.progress || 0))}%</div>
    </div>`).join("");
}
async function refreshDownloads() {
  const box = $("lib-dl");
  if (!box || !box.isConnected) { clearInterval(_libTimer); _libTimer = null; return; }
  try { box.innerHTML = downloadsHtml((await api("/api/lidarr/queue")).items || []); }
  catch (e) { box.innerHTML = `<p class="muted msg err">Couldn’t reach Lidarr: ${esc(e.message)}</p>`; }
}

function libraryStatsText(st) {
  const s = st.stats || {};
  const parts = [`${s.albums || 0} albums`, `${s.artists || 0} artists`, `${s.tracks || 0} tracks`];
  if (st.scan && st.scan.running) parts.push("scanning…");
  return parts.join(" · ");
}
async function waitForScan(onDone) {
  for (let i = 0; i < 600; i++) {
    await new Promise((res) => setTimeout(res, 1500));
    const st = await loadLibraryStatus();
    if (!st || !(st.scan && st.scan.running)) { loadAccounts(); onDone(st); return; }  // refresh the sidebar's "Library · N tracks"
  }
}

async function renderLibrary() {
  const list = $("list");
  clearInterval(_libTimer); _libTimer = null;
  list.innerHTML = loadingState("Loading your library…");
  const st = await loadLibraryStatus();
  if (!st || !st.enabled) {
    list.innerHTML = emptyState("music", "Your library is off",
      "Point Harmony at your music folders — where Lidarr files what it gets — to search and play them everywhere.",
      { id: "lib-setup", label: "Set up library" });
    $("lib-setup").onclick = () => goView("accounts");
    return;
  }
  let ov;
  try { ov = await api("/api/library/browse?order=recent&limit=60"); }
  catch (e) { list.innerHTML = errorState("Couldn’t load the library", e.message, "lib-retry"); $("lib-retry").onclick = renderLibrary; return; }
  const albums = ov.albums || [], artists = ov.artists || [];
  const dl = lidarrEnabled() ? `<section class="detail-sec"><h3>Downloads</h3><div id="lib-dl">${loadingState("Asking Lidarr…")}</div></section>` : "";
  const warn = (st.missing || []).length
    ? `<p class="muted msg err">Missing folder${st.missing.length > 1 ? "s" : ""}: ${st.missing.map(esc).join(", ")}</p>` : "";
  list.innerHTML = `<div class="detail">
    <div class="sec-head"><span class="muted" id="lib-stats">${esc(libraryStatsText(st))}</span>
      <button class="act ghost small" id="lib-rescan" type="button">${ICON("sync")} Rescan</button></div>
    ${warn}
    ${dl}
    ${albums.length ? `<section class="detail-sec"><h3>Recently added</h3>
      <div class="albrows">${albumRowsHtml(albums, { showArtist: true })}</div></section>`
      : emptyState("music", "No music found yet", st.paths && st.paths.length ? "Rescan once the folders have albums in them." : "Add a music folder in Accounts → Library.")}
    ${artists.length ? `<section class="detail-sec"><h3>Artists</h3><div class="chips">${artists.map(artistChipHtml).join("")}</div></section>` : ""}
  </div>`;
  hydrateArt(list);
  wireAlbumRows(list);
  wireArtistChips(list);
  $("lib-rescan").onclick = async () => {
    try { await apiPost("/api/library/scan", {}); } catch (e) { toastErr("Couldn’t rescan: " + e.message); return; }
    $("lib-stats").textContent = "Scanning…";
    waitForScan(() => { if (state.section === "library" && !state.detail) renderLibrary(); });
  };
  if (st.scan && st.scan.running) waitForScan(() => { if (state.section === "library" && !state.detail) renderLibrary(); });
  if (dl) { refreshDownloads(); _libTimer = setInterval(refreshDownloads, 5000); }
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

// Open a menu either at a right-click (an Event) or under a '⋯' button (an
// element) — the button is the path on touch, where iOS has no contextmenu.
function menuAt(src, items) {
  if (!items.length) {
    if (!(src instanceof Event)) toast("Nothing to do here.");
    return;
  }
  if (src instanceof Event) { src.preventDefault(); openContextMenu(src.clientX, src.clientY, items); return; }
  const r = src.getBoundingClientRect();
  openContextMenu(r.left, r.bottom + 4, items);
}
const moreBtn = (label) => `<button class="mini more" type="button" aria-label="${esc(label || "More actions")}" aria-haspopup="menu">${ICON("more")}</button>`;

// `opts.queueIndex` marks a Now Playing queue row (queue actions, never playlist ones).
function trackMenuItems(t, anchor, opts = {}) {
  const items = [];
  if (opts.queueIndex != null) {
    items.push({ label: "Remove from queue", fn: () => removeFromQueue(opts.queueIndex) });
  } else {
    items.push({ label: "Play next", fn: () => playNextTracks([t], t.title) });
    items.push({ label: "Add to queue", fn: () => enqueueTracks([t], t.title) });
  }
  if (opts.playlistIndex != null && state.playlist)
    items.push({ label: "Remove from this playlist", fn: () => removeFromPlaylist(t, opts.playlistIndex) });
  items.push({ label: "Add to playlist…", fn: () => openAddMenu(anchor, [t]) });
  if (t.id) items.push({ label: "Track details", fn: () => navigateTrack(t.service, t.id) });
  if (t.artist_ids && t.artist_ids[0])
    items.push({ label: "Go to artist", fn: () => navigateArtist(t.service, t.artist_ids[0]) });
  if (t.album_id)
    items.push({ label: "Go to album", fn: () => navigateAlbum(t.service, t.album_id) });
  return items;
}

function openTrackContextMenu(e, t, opts) {
  const at = { getBoundingClientRect: () => ({ left: e.clientX, right: e.clientX, top: e.clientY, bottom: e.clientY }) };
  menuAt(e, trackMenuItems(t, at, opts));
}

function albumMenuItems(a) {
  const items = [];
  if (a.id) {
    items.push({ label: "Play", fn: () => albumToQueue(a, "play") });
    items.push({ label: "Shuffle", fn: () => albumToQueue(a, "shuffle") });
    items.push({ label: "Play next", fn: () => albumToQueue(a, "next") });
    items.push({ label: "Add to queue", fn: () => albumToQueue(a, "end") });
    items.push({ label: "Go to album", fn: () => navigateAlbum(a.service, a.id) });
  }
  if (a.artist_ids && a.artist_ids[0])
    items.push({ label: "Go to artist", fn: () => navigateArtist(a.service, a.artist_ids[0]) });
  if (a.localId && a.service !== "local") {
    const lib = { service: "local", id: a.localId, title: a.title };
    items.unshift({ label: "Play library copy", fn: () => albumToQueue(lib, "play") },
                  { label: "Open library copy", fn: () => navigateAlbum("local", a.localId) });
  }
  if (a.service !== "local")
    items.push(...lidarrMenuItems({ kind: "album", title: a.title, artist: a.artist, mbid: a.mbid }));
  return items;
}
function openAlbumContextMenu(e, a) { menuAt(e, albumMenuItems(a)); }

// Artists (with a provider id) act through their top tracks.
async function artistTopTracks(service, id) {
  const d = await api(`/api/artist/${encodeURIComponent(service)}/${encodeURIComponent(id)}`);
  return d.top_tracks || [];
}
async function artistToQueue(ar, where) {
  try {
    const tracks = ar.tracks || await artistTopTracks(ar.service, ar.id);
    if (!tracks.length) { toastErr("No top tracks to play for this artist."); return; }
    collectionTo(tracks, where, ar.name ? `${ar.name} top tracks` : "top tracks");
  } catch (e) { toastErr("Couldn’t load that artist: " + e.message); }
}
function artistMenuItems(ar) {
  const items = [];
  if (ar.id) {
    items.push({ label: "Play top tracks", fn: () => artistToQueue(ar, "play") });
    items.push({ label: "Shuffle top tracks", fn: () => artistToQueue(ar, "shuffle") });
    items.push({ label: "Play next", fn: () => artistToQueue(ar, "next") });
    items.push({ label: "Add to queue", fn: () => artistToQueue(ar, "end") });
    if (!ar.here) items.push({ label: "Go to artist", fn: () => navigateArtist(ar.service, ar.id) });
  }
  items.push(...lidarrMenuItems({ kind: "artist", artist: ar.name, mbid: ar.mbid || "" }));
  return items;
}

// Playlists act through their track list.
async function playlistToQueue(p, where) {
  try {
    const tracks = p.tracks || (await api(`/api/playlists/${encodeURIComponent(p.service)}/${encodeURIComponent(p.id)}/tracks`)).tracks || [];
    if (!tracks.length) { toastErr("That playlist is empty."); return; }
    collectionTo(tracks, where, p.title || "playlist");
  } catch (e) { toastErr("Couldn’t load that playlist: " + e.message); }
}
function playlistMenuItems(p) {
  const items = [
    { label: "Play", fn: () => playlistToQueue(p, "play") },
    { label: "Shuffle", fn: () => playlistToQueue(p, "shuffle") },
    { label: "Play next", fn: () => playlistToQueue(p, "next") },
    { label: "Add to queue", fn: () => playlistToQueue(p, "end") },
  ];
  if (!p.here) items.push({ label: "Open playlist", fn: () => openPlaylist(p.service, p.id, p.title) });
  return items;
}

// One place that turns a whole list into a queue action.
function collectionTo(tracks, where, label) {
  if (where === "play") playFrom(tracks, 0);
  else if (where === "shuffle") playFrom(tracks, null, { shuffle: true });
  else if (where === "next") playNextTracks(tracks, label);
  else enqueueTracks(tracks, label);
}

// Play / Shuffle / ⋯ buttons for a collection header (album, playlist, artist).
function collectionActsHtml(prefix, opts = {}) {
  return `<div class="hero-acts">
    <button class="act" id="${prefix}-play" type="button">${ICON("play")} ${esc(opts.playLabel || "Play")}</button>
    <button class="act ghost" id="${prefix}-shuffle" type="button">${ICON("shuffle")} Shuffle</button>
    <button class="act ghost icon-only" id="${prefix}-more" type="button" aria-label="More actions" aria-haspopup="menu">${ICON("more")}</button>
  </div>`;
}
function wireCollectionActs(prefix, getTracks, moreItems) {
  const run = async (where) => {
    let tracks;
    try { tracks = await getTracks(); } catch (e) { toastErr(e.message); return; }
    if (!tracks || !tracks.length) { toastErr("Nothing playable here."); return; }
    collectionTo(tracks, where);
  };
  if ($(`${prefix}-play`)) $(`${prefix}-play`).onclick = () => run("play");
  if ($(`${prefix}-shuffle`)) $(`${prefix}-shuffle`).onclick = () => run("shuffle");
  if ($(`${prefix}-more`)) $(`${prefix}-more`).onclick = (e) => { e.stopPropagation(); menuAt(e.currentTarget, moreItems()); };
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

// Row options:
//   numbered        album-style track numbers (play button over the number)
//   reorder         a drag grip (Now Playing queue)
//   queueRow        a Now Playing queue row: queue actions only — NEVER playlist ones
//   playlistActions an editable playlist's rows (remove-from-playlist)
function trackRowHtml(t, i, opts = {}) {
  const num = opts.numbered ? `<span class="tnum">${t.track_number != null ? t.track_number : i + 1}</span>` : "";
  const grip = opts.reorder ? `<button class="mini grip" type="button" aria-label="Drag to reorder" tabindex="-1">${ICON("drag")}</button>` : "";
  const title = t.id
    ? `<a class="tt" href="${routeHref("track", t.service, t.id)}" title="${esc(t.title)}">${esc(t.title)}</a>`
    : `<span class="tt">${esc(t.title)}</span>`;
  return `
    <div class="trow${opts.numbered ? " numbered" : ""}${opts.reorder ? " reorder" : ""}" data-i="${i}" data-svc="${esc(t.service)}" data-tid="${esc(t.id)}">
      <div class="tlead">${num}<button class="play" type="button" aria-label="Play ${esc(t.title)}">${ICON("play")}</button></div>
      <div class="title">${title}${opts.hideBadge ? "" : `<span class="badge">${esc(serviceLabel(t.service))}</span>`}</div>
      ${trackArtistCell(t)}
      <div class="dur">${fmtTime(t.duration_s)}</div>
      <div class="rowacts">
        ${grip}
        ${opts.queueRow ? "" : `<button class="mini add" type="button" aria-label="Add to playlist">${ICON("add")}</button>`}
        ${opts.playlistActions && !opts.queueRow ? `<button class="mini rem" type="button" aria-label="Remove from this playlist">${ICON("remove")}</button>` : ""}
        ${opts.queueRow ? `<button class="mini qrem" type="button" aria-label="Remove from queue" title="Remove from queue">${ICON("close")}</button>` : ""}
        ${moreBtn(`More actions for ${t.title || "track"}`)}
      </div>
    </div>`;
}

const tracksHtml = (tracks, opts = {}) =>
  `<div class="tracks">${tracks.map((t, i) => trackRowHtml(t, i, opts)).join("")}</div>`;

// Is row `i` of this list the active track? Queue rows match by index (the
// queue may hold duplicates); every other list matches on service + id.
function rowIsCurrent(tracks, i, opts) {
  const cur = pq.current();
  if (!cur) return false;
  return opts.queueRow ? i === pq.index : sameTrack(tracks[i], cur);
}

// Wire a rendered set of track rows to shared playback + row menus. Playing a
// row loads THIS list into the active queue (playFrom) at the clicked index —
// so browsing never leaks into the active queue. `opts.onPlay(i)` overrides
// that (the Now Playing queue jumps within the active queue). Pressing the
// play button of the row that is already current toggles pause instead.
// Desktop: play button or double-click; the title links to the track page.
// Touch: a tap anywhere on the row (title included) plays; ⋯ has the rest.
function wireTrackRows(scope, tracks, opts = {}) {
  scope.querySelectorAll(".trow").forEach((row) => {
    const i = Number(row.dataset.i);
    const start = opts.onPlay ? () => opts.onPlay(i) : () => playFrom(tracks, i);
    const play = () => (rowIsCurrent(tracks, i, opts) && !isStopped() ? togglePlay() : start());
    const pbtn = row.querySelector(".play");
    if (pbtn) pbtn.addEventListener("click", (e) => { e.stopPropagation(); play(); });
    const add = row.querySelector(".add");
    if (add) add.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); openAddMenu(e.currentTarget, [tracks[i]]); });
    const rem = row.querySelector(".rem");
    if (rem) rem.addEventListener("click", (e) => { e.stopPropagation(); removeFromPlaylist(tracks[i], i); });
    const qrem = row.querySelector(".qrem");
    if (qrem) qrem.addEventListener("click", (e) => { e.stopPropagation(); removeFromQueue(i); });
    const menuOpts = opts.queueRow ? { queueIndex: i } : opts.playlistActions ? { playlistIndex: i } : {};
    const more = row.querySelector(".more");
    if (more) more.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      menuAt(e.currentTarget, trackMenuItems(tracks[i], e.currentTarget, menuOpts));
    });
    row.addEventListener("contextmenu", (e) => openTrackContextMenu(e, tracks[i], menuOpts));
    const tt = row.querySelector("a.tt");
    if (tt) tt.addEventListener("click", (e) => {
      if (opts.clickToPlay || isTouch()) { e.preventDefault(); play(); }
    });
    row.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      if (opts.clickToPlay || isTouch()) play();
    });
    row.addEventListener("dblclick", (e) => {
      if (opts.clickToPlay || isTouch() || e.target.closest("a, button")) return;
      play();
    });
  });
}

function renderTracks(tracks, opts = {}) {
  const list = $("list");
  const pl = state.playlist;
  const toolbar = pl ? `
    <div class="toolbar-row wrap">
      ${tracks.length ? collectionActsHtml("pl") : ""}
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
  const rowOpts = { ...opts, playlistActions: !!pl };
  list.innerHTML = toolbar + tracksHtml(tracks, rowOpts);
  state.queue = tracks;
  wireTrackRows(list, tracks, rowOpts);
  if (pl) wireCollectionActs("pl", () => tracks, () => playlistMenuItems({ ...pl, tracks, here: true }));
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

// A playlist can only hold its own service's tracks.
async function addTracksToPlaylist(service, id, title, tracks) {
  const ids = tracks.filter((t) => t && t.service === service && t.id).map((t) => t.id);
  const skipped = tracks.length - ids.length;
  if (!ids.length) { toastErr(`Only ${serviceLabel(service)} tracks can go in “${title}”.`); return false; }
  try {
    await apiPost(`/api/playlists/${encodeURIComponent(service)}/${encodeURIComponent(id)}/add`, { track_ids: ids });
    toast((ids.length === 1 ? `Added to “${title}”.` : `Added ${nTracks(ids.length)} to “${title}”.`)
      + (skipped ? ` Skipped ${skipped} from other services.` : ""), "ok");
    return true;
  } catch (e) { toastErr("Couldn’t add to the playlist: " + e.message); return false; }
}

async function openAddMenu(anchor, tracks) {
  if (!_playlistCache) await loadPlaylistsSilently();
  document.querySelectorAll(".addmenu").forEach((m) => m.remove());
  const menu = document.createElement("div");
  menu.className = "addmenu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = (_playlistCache || []).map((p) =>
    `<div role="menuitem" tabindex="0" data-service="${esc(p.service)}" data-id="${esc(p.id)}" data-title="${esc(p.title)}">${esc(p.title)} <span class="s">${esc(serviceLabel(p.service))}</span></div>`).join("")
    + `<div role="menuitem" tabindex="0" class="new" data-new>＋ New playlist…</div>`;
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${Math.max(8, Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 8))}px`;
  menu.style.left = `${Math.max(8, Math.min(Math.max(8, r.left - 160), window.innerWidth - menu.offsetWidth - 8))}px`;
  const close = () => menu.remove();
  menu.querySelectorAll("div[data-service]").forEach((row) => row.addEventListener("click", async () => {
    close();
    await addTracksToPlaylist(row.dataset.service, row.dataset.id, row.dataset.title, tracks);
  }));
  menu.querySelector("[data-new]").addEventListener("click", async () => { close(); await newPlaylist(tracks); });
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
  list.innerHTML = bar + `<div class="plgrid">${playlists.map(playlistCardHtml).join("")}</div>`;
  $("pl-new").onclick = () => newPlaylist();
  wirePlaylistCards(list);
}

function playlistCardHtml(p) {
  return `
    <div class="plcard" tabindex="0" role="link" data-service="${esc(p.service)}" data-id="${esc(p.id)}" data-art="${esc(p.artwork_url || "")}">
      <div class="art">${ICON("music")}</div>
      <div class="t">${esc(p.title)}</div>
      <div class="s">${esc(serviceLabel(p.service))}${p.track_count != null ? " · " + nTracks(p.track_count) : ""}</div>
      ${moreBtn(`More actions for ${p.title || "playlist"}`)}
    </div>`;
}

// Playlist cards: click opens; ⋯ / right-click → play, shuffle, queue.
function wirePlaylistCards(scope) {
  scope.querySelectorAll(".plcard").forEach((card) => {
    const url = card.dataset.art;
    if (url) {
      const img = new Image();
      img.className = "art"; img.alt = "";
      img.onload = () => { const slot = card.querySelector(".art"); if (slot) slot.replaceWith(img); };
      img.src = url;
    }
    const p = { service: card.dataset.service, id: card.dataset.id, title: card.querySelector(".t").textContent };
    const open = () => openPlaylist(p.service, p.id, p.title);
    card.addEventListener("click", (e) => { if (!e.target.closest(".more")) open(); });
    card.addEventListener("keydown", (e) => { if (e.target === card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(); } });
    card.addEventListener("contextmenu", (e) => menuAt(e, playlistMenuItems(p)));
    const more = card.querySelector(".more");
    if (more) more.addEventListener("click", (e) => { e.stopPropagation(); menuAt(e.currentTarget, playlistMenuItems(p)); });
  });
}

// Two track objects refer to the same track. Highlighting matches on this
// (service + id), never on index, since the displayed list is now decoupled
// from the active queue — the playing track may sit at a different index (or
// not appear at all) in whatever list is on screen.
const sameTrack = (a, b) => !!(a && b && a.service === b.service && String(a.id) === String(b.id));
const activeTrack = () => pq.current();

// Light up the current track wherever it's listed: animated bars while
// playing, still bars while paused/stopped. Queue rows match by index, every
// other list by service + id.
function highlightPlaying() {
  const cur = pq.current();
  const playing = isPlaying();
  document.querySelectorAll(".trow").forEach((row) => {
    const inQueue = !!row.closest(".npview");
    const active = !!cur && (inQueue
      ? Number(row.dataset.i) === pq.index
      : row.dataset.svc === String(cur.service) && row.dataset.tid === String(cur.id));
    const mode = !active ? "" : playing ? "playing" : "paused";
    row.classList.toggle("playing", active);
    row.classList.toggle("paused", active && !playing);
    const btn = row.querySelector(".play");
    if (!btn || btn.dataset.mode === mode) return;
    btn.dataset.mode = mode;
    const title = row.querySelector(".tt") ? row.querySelector(".tt").textContent : "track";
    btn.setAttribute("aria-label", mode === "playing" ? "Pause" : mode === "paused" ? "Resume" : `Play ${title}`);
    btn.innerHTML = mode ? `<span class="eq${mode === "paused" ? " still" : ""}"><span></span><span></span><span></span></span>` : ICON("play");
  });
}

// -- Now Playing view: big cover art + the whole current queue -------------
// Mirrors the desktop's Now Playing page. Reuses the track-row machinery so the
// current row lights up (highlightPlaying) and clicking one plays that index.

const artOf = (t) => (t && (t.artwork_url || t.art_url)) || "";
const trackKey = (t) => (t ? `${t.service}:${t.id}` : "");

// Shuffle / repeat toggles, shared by the bottom bar and the Now Playing view
// (paintModes keeps every copy in step).
function modeBtnsHtml(cls) {
  return `<button class="${cls} js-shuffle" type="button" aria-label="Shuffle" title="Shuffle">${ICON("shuffle")}</button>
    <button class="${cls} js-repeat" type="button" aria-label="Repeat" title="Repeat">${ICON("repeat")}</button>`;
}
function paintModes() {
  document.querySelectorAll(".js-shuffle").forEach((b) => {
    b.classList.toggle("on", pq.shuffle);
    b.setAttribute("aria-pressed", String(pq.shuffle));
    b.title = pq.shuffle ? "Shuffle: on" : "Shuffle: off";
  });
  document.querySelectorAll(".js-repeat").forEach((b) => {
    b.classList.toggle("on", pq.repeat !== "off");
    const label = `Repeat: ${pq.repeat === "one" ? "this track" : pq.repeat}`;
    b.setAttribute("aria-label", label); b.title = label;
    const ico = pq.repeat === "one" ? "repeat-one" : "repeat";
    if (b.dataset.ico !== ico) { b.dataset.ico = ico; b.innerHTML = ICON(ico); }
  });
}

function renderNowPlaying() {
  const list = $("list");
  if (!pq.tracks.length) {
    list.innerHTML = emptyState("music", "Nothing playing",
      "Play a track, album, or playlist and it shows up here.");
    _npKey = null;
    return;
  }
  const t = pq.current() || {};
  const title = t.id ? `<a class="link-plain" href="${routeHref("track", t.service, t.id)}">${esc(t.title || "")}</a>` : esc(t.title || "Not started");
  list.innerHTML = `<div class="detail npview">
    <div class="detail-hero">
      <div class="detail-art" id="np-view-art" data-art="${esc(artOf(t))}">${ICON("music")}</div>
      <div class="detail-herometa">
        <div class="detail-kind" id="np-view-kind">Now Playing</div>
        <h2 class="detail-title" id="np-view-title">${title}</h2>
        <div class="detail-sub" id="np-view-artist">${esc(t.artist || "")}</div>
        <div class="npvol" id="np-view-volrow">
          ${ICON("speaker")}<input id="np-view-vol" type="range" min="0" max="100" value="${esc($("np-vol").value)}" aria-label="Device volume" />
        </div>
      </div>
    </div>
    <section class="detail-sec">
      <div class="qhead">
        <h3>Queue <span class="muted qcount">${esc(nTracks(pq.tracks.length))}</span></h3>
        <div class="qctrls">
          ${modeBtnsHtml("qbtn")}
          <button class="act ghost small" id="np-clear" type="button" title="Remove everything except the current track">Clear</button>
          <button class="act ghost small" id="np-save" type="button">Save as playlist</button>
        </div>
      </div>
      ${tracksHtml(pq.tracks, { reorder: true, queueRow: true })}
    </section>
  </div>`;
  _npKey = null;
  wireTrackRows(list, pq.tracks, { onPlay: jumpTo, clickToPlay: true, queueRow: true });
  wireQueueReorder(list);
  wireModeButtons(list);
  $("np-clear").onclick = clearQueue;
  $("np-save").onclick = saveQueueAsPlaylist;
  $("np-view-vol").addEventListener("input", (e) => setVolume(Number(e.target.value)));
  updateNowPlayingView();
  paintModes();
}

// Keep an open Now Playing view in step with a track change without rebuilding
// the whole list (so the queue's scroll position survives a next/prev).
let _npKey = null;
function updateNowPlayingView() {
  const wrap = $("list").querySelector(".npview");
  if (!wrap) return;
  if (!pq.tracks.length) { renderNowPlaying(); return; }
  const t = pq.current() || {};
  const key = `${pq.index}|${trackKey(t)}`;
  const kind = $("np-view-kind");
  if (kind) kind.textContent = onDevice() ? `Now Playing · ${currentDeviceName()}` : "Now Playing";
  const vr = $("np-view-volrow");
  if (vr) vr.classList.toggle("show", onDevice());
  if (key !== _npKey) {
    _npKey = key;
    const titleEl = $("np-view-title"), artistEl = $("np-view-artist"), artEl = $("np-view-art");
    if (titleEl) titleEl.innerHTML = t.id ? `<a class="link-plain" href="${routeHref("track", t.service, t.id)}">${esc(t.title || "")}</a>` : esc(t.title || "");
    if (artistEl) artistEl.textContent = t.artist || "";
    if (artEl) { artEl.classList.add("fallback"); artEl.innerHTML = ICON("music"); artEl.dataset.art = artOf(t); hydrateArt(artEl.parentElement); }
  }
  highlightPlaying();
}

function wireModeButtons(scope) {
  scope.querySelectorAll(".js-shuffle").forEach((b) => { b.onclick = toggleShuffle; });
  scope.querySelectorAll(".js-repeat").forEach((b) => { b.onclick = cycleRepeat; });
}

// Pointer-based drag-to-reorder for the Now Playing queue. Pointer events (not
// HTML5 drag) so it works with touch; the grip carries touch-action:none so a
// drag never scrolls the page. On drop we rebuild the active queue from the new
// DOM order and re-anchor activeIndex on the still-playing track (by identity).
function wireQueueReorder(scope) {
  const container = scope.querySelector(".tracks");
  if (!container) return;
  container.querySelectorAll(".trow.reorder .grip").forEach((grip) => {
    grip.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const row = grip.closest(".trow");
      if (!row) return;
      row.classList.add("dragging");
      state.dragging = true;
      try { grip.setPointerCapture(e.pointerId); } catch (_e) { /* ignore */ }
      const move = (ev) => {
        const others = [...container.querySelectorAll(".trow")].filter((r) => r !== row);
        const after = others.find((r) => ev.clientY < r.getBoundingClientRect().top + r.offsetHeight / 2);
        if (after) container.insertBefore(row, after);
        else container.appendChild(row);
      };
      const up = () => {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", up);
        grip.removeEventListener("pointercancel", up);
        row.classList.remove("dragging");
        state.dragging = false;
        commitQueueOrder(container, row);
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up);
      grip.addEventListener("pointercancel", up);
    });
  });
}

// A drag moved one row: from its old index to its new DOM position.
function commitQueueOrder(container, row) {
  const from = Number(row.dataset.i);
  const to = [...container.querySelectorAll(".trow")].indexOf(row);
  if (to < 0 || from === to) { renderNowPlaying(); return; }
  moveInQueue(from, to);
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
  state.playlist = null;   // only openPlaylist() sets it; never leaks into other views
  highlightNav(view);
  if (view === "search") { $("view-title").textContent = "Search"; $("search-input").focus(); }
  else if (view === "nowplaying") { $("view-title").textContent = "Now Playing"; renderNowPlaying(); }
  else if (view === "playlists") { $("view-title").textContent = "Playlists"; loadPlaylists(); }
  else if (view === "library") { $("view-title").textContent = "Library"; renderLibrary(); }
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
    switchOutput(b.dataset.target);   // sets the target synchronously; the hand-off runs on
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

// Explain a credential sync: {synced, kept, rolled_back} per service →
// "Synced Qobuz · kept YouTube Music · couldn't use YouTube Music here, kept
// your existing login". `worked` = something synced or an existing login kept.
function adoptResultText(r) {
  const names = (a) => (a || []).map(serviceLabel).join(" and ");
  if (!Array.isArray(r.synced)) {   // an older server: only the imported key list
    const n = (r.imported || []).length;
    return { text: n ? `Synced ${n} credential(s).` : "Nothing to sync.", worked: n > 0 };
  }
  const synced = r.synced || [], kept = r.kept || [], back = r.rolled_back || [];
  const parts = [];
  if (synced.length) parts.push(`Synced ${names(synced)}`);
  if (kept.length) parts.push(`${parts.length ? "kept" : "Kept"} ${names(kept)} (already working here)`);
  if (back.length) parts.push(`${parts.length ? "couldn’t" : "Couldn’t"} use ${names(back)} here, kept your existing login`);
  const worked = synced.length > 0 || kept.length > 0;
  if (!parts.length) return { text: "Nothing synced — that instance has no working logins to share.", worked: false };
  return { text: parts.join(" · ") + ".", worked };
}

async function renderAccounts() {
  const list = $("list");
  list.innerHTML = loadingState("Loading accounts…");
  let accounts = [], prefs = { personal_key: "" }, instances = [], lidarr = {};
  try { accounts = (await api("/api/accounts")).accounts || []; } catch { /* show forms anyway */ }
  try { prefs = await api("/api/preferences"); } catch { /* ignore */ }
  try { instances = (await api("/api/instances")).instances || []; } catch { /* none */ }
  try { lidarr = await api("/api/lidarr"); state.lidarr = lidarr; } catch { /* show form anyway */ }
  let lib = { enabled: false, paths: [], path_map: [], stats: {}, scan: {} };
  try { lib = await api("/api/library"); state.library = lib; } catch { /* show form anyway */ }
  // Options (root folders / profiles) only exist once Lidarr is reachable.
  let lopts = null;
  if (lidarr.configured) { try { lopts = await api("/api/lidarr/options"); } catch { /* degrade */ } }
  const status = (svc) => accounts.find((a) => a.service === svc) || { authenticated: false };
  const q = status("qobuz"), y = status("ytmusic");
  const badge = (a) => a.stale ? "session expired" : a.authenticated ? "signed in" + (a.account ? " · " + esc(a.account) : "") : "signed out";

  // Lidarr integration block: URL + API key + enable, plus root/profile
  // dropdowns once the instance is reachable. Degrades to just the form when
  // Lidarr is unconfigured or unreachable.
  const liBadge = lidarr.configured
    ? (lidarr.ok ? (lidarr.version ? "v" + esc(lidarr.version) : "connected") : "unreachable")
    : "not configured";
  const liStatusText = lidarr.configured
    ? (lidarr.ok ? `Connected to Lidarr${lidarr.version ? " v" + esc(lidarr.version) : ""}.`
                 : `Lidarr is unreachable: ${esc(lidarr.error || "unknown error")}`)
    : "";
  const liStatusCls = lidarr.configured ? (lidarr.ok ? " ok" : " err") : "";
  const pmText = (lib.path_map || []).map((m) => `${m.remote} => ${m.local}`).join("\n");
  const libBadge = lib.enabled ? esc(libraryStatsText(lib)) : "off";
  const libScan = lib.scan || {};
  const libScanText = libScan.running ? "Scanning…"
    : libScan.error ? `Last scan failed: ${esc(libScan.error)}`
    : libScan.finished ? `Last scan: ${libScan.added || 0} added, ${libScan.updated || 0} updated, ${libScan.removed || 0} removed.` : "";
  const libMissing = (lib.missing || []).length ? ` Missing: ${lib.missing.map(esc).join(", ")}.` : "";
  // The loop back from Lidarr: its root folders become library folders and an
  // import webhook keeps the index current. Only offered once Lidarr is reachable.
  const liLoop = lidarr.configured ? `
        <details class="field"${lib.enabled ? "" : " open"}><summary class="muted">Library loop — play what Lidarr gets</summary>
          <p class="muted field">Adds Lidarr’s root folders to this server’s library and registers a
          webhook in Lidarr, so every album it imports is searchable and playable everywhere moments later.</p>
          <label class="muted field">Path mappings, if Lidarr sees the files under a different path (one per line: <code>lidarr path =&gt; path on this server</code>)</label>
          <textarea id="li-pm" rows="2" class="mono" placeholder="/music => /srv/media/music">${esc(pmText)}</textarea>
          <label class="muted field">Address Lidarr can reach this server at</label>
          <input id="li-cb" type="text" class="field" value="${esc(location.origin)}" autocomplete="off" />
          <div class="field-acts"><button class="act" id="li-connect">Connect Lidarr to library</button></div>
          <p id="li-loop-msg" class="muted msg"></p>
        </details>` : "";
  const liSelect = (id, label, options) => `<label class="muted field">${esc(label)}</label>
        <select id="${id}" class="field" style="width:100%">
          <option value="">Use Lidarr’s default</option>${options}</select>`;
  const liOpts = lopts
    ? liSelect("li-root", "Root folder", (lopts.root_folders || []).map((p) =>
        `<option value="${esc(p)}">${esc(p)}</option>`).join(""))
      + liSelect("li-qual", "Quality profile", (lopts.quality_profiles || []).map((p) =>
        `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join(""))
      + liSelect("li-meta", "Metadata profile", (lopts.metadata_profiles || []).map((p) =>
        `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join(""))
    : "";
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

      <div class="card">
        <h2>Lidarr <span class="badge">${liBadge}</span></h2>
        <p class="muted">Hand an album or artist to your Lidarr instance to acquire it. Once enabled,
        right-click an album or artist anywhere and choose “Get with Lidarr”.</p>
        <label class="muted field">Server URL</label>
        <input id="li-url" type="text" class="field" placeholder="http://192.168.1.10:8686" value="${esc(lidarr.url || "")}" autocomplete="off" />
        <label class="muted field">API key</label>
        <input id="li-key" type="password" class="field" placeholder="${lidarr.configured ? "•••••••• (leave blank to keep)" : "Lidarr API key"}" autocomplete="off" />
        ${liOpts}
        <label class="field" style="display:flex;gap:.5rem;align-items:center">
          <input id="li-enabled" type="checkbox"${lidarr.enabled ? " checked" : ""} /> <span>Enable “Get with Lidarr”</span>
        </label>
        <div class="field-acts"><button class="act" id="li-save">Save</button></div>
        <p id="li-msg" class="muted msg${liStatusCls}">${liStatusText}</p>
        ${liLoop}
      </div>

      <div class="card">
        <h2>Library <span class="badge">${libBadge}</span></h2>
        <p class="muted">Music folders on this server — searched, browsed and played like any service
        (here, in the app, and on your speakers). Point it at the folder Lidarr files albums into.</p>
        <label class="muted field">Music folders (one per line, as paths on this server)</label>
        <textarea id="lib-paths" rows="2" class="mono" placeholder="/srv/media/music">${esc((lib.paths || []).join("\n"))}</textarea>
        <label class="field" style="display:flex;gap:.5rem;align-items:center">
          <input id="lib-enabled" type="checkbox"${lib.enabled ? " checked" : ""} /> <span>Enable the library</span>
        </label>
        <div class="field-acts">
          <button class="act" id="lib-save">Save</button>
          ${lib.enabled ? `<button class="act ghost" id="lib-scan">Rescan</button>` : ""}
        </div>
        <p id="lib-msg" class="muted msg${libMissing ? " err" : ""}">${libScanText}${libMissing}${lib.enabled && !lib.tags ? " Tag reading is off (install mutagen) — names come from folders." : ""}</p>
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
  // ok: true → success style, "error" → error style, otherwise neutral.
  const setAdoptMsg = (t, ok) => { const am = $("adopt-msg"); am.textContent = t; am.className = "muted msg" + (ok === true ? " ok" : ok === "error" ? " err" : ""); };
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
        const { text, worked } = adoptResultText(r);
        setAdoptMsg(text, worked ? true : "error");
        // Only remember an instance that actually gave us (or confirmed) a working login.
        if (worked && body.host) { try { await apiPost("/api/peers", body); } catch { /* best-effort */ } }
        if (worked) { loadAccounts(); setTimeout(renderAccounts, 2500); }
      } else {
        setAdoptMsg(r.reason || "Nothing to sync — pick an instance or enter host:port.", "error");
      }
    } catch (e) { setAdoptMsg("Couldn’t sync: " + e.message, "error"); }
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
  const parsePathMap = (text) => text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
    const i = l.indexOf("=>");
    return i < 0 ? null : { remote: l.slice(0, i).trim(), local: l.slice(i + 2).trim() };
  }).filter((m) => m && m.remote && m.local);
  const setMsg = (id, t, ok) => { const el = $(id); if (!el) return; el.textContent = t; el.className = "muted msg" + (ok === true ? " ok" : ok === "error" ? " err" : ""); };
  $("lib-save").onclick = async () => {
    setMsg("lib-msg", "Saving…");
    const paths = $("lib-paths").value.split("\n").map((l) => l.trim()).filter(Boolean);
    try {
      state.library = await apiPost("/api/library/config", { enabled: $("lib-enabled").checked, paths });
      setMsg("lib-msg", state.library.enabled && paths.length ? "Saved — scanning…" : "Saved.", true);
      loadAccounts();
      if (state.library.enabled && paths.length) waitForScan(() => { if (state.section === "accounts" && !state.detail) renderAccounts(); });
    } catch (e) { setMsg("lib-msg", "Couldn’t save: " + e.message, "error"); }
  };
  if ($("lib-scan")) $("lib-scan").onclick = async () => {
    try { await apiPost("/api/library/scan", {}); setMsg("lib-msg", "Scanning…"); }
    catch (e) { setMsg("lib-msg", "Couldn’t rescan: " + e.message, "error"); return; }
    waitForScan(() => { if (state.section === "accounts" && !state.detail) renderAccounts(); });
  };
  if ($("li-connect")) $("li-connect").onclick = async () => {
    setMsg("li-loop-msg", "Connecting…");
    try {
      const r = await apiPost("/api/lidarr/connect-library", {
        callback_url: $("li-cb").value.trim(), path_map: parsePathMap($("li-pm").value) });
      state.library = r.library;
      const bits = [`Library folders: ${(r.roots || []).join(", ") || "none"}.`,
        r.tested ? "Lidarr reached this server — imports will appear automatically."
                 : `Webhook registered, but Lidarr couldn’t reach ${r.webhook_url}${r.test_error ? ` (${r.test_error})` : ""} — check the address.`];
      if ((r.missing || []).length) bits.push(`Not found on this server: ${r.missing.join(", ")} — add a path mapping.`);
      setMsg("li-loop-msg", bits.join(" "), r.tested && !(r.missing || []).length ? true : "error");
      // Reflect the now-enabled library in its card below without a re-render
      // (which would drop this message).
      if (r.library) {
        $("lib-enabled").checked = !!r.library.enabled;
        $("lib-paths").value = (r.library.paths || []).join("\n");
        setMsg("lib-msg", "Scanning…");
        waitForScan((st) => setMsg("lib-msg", st ? `Indexed: ${libraryStatsText(st)}.` : "", true));
      }
      loadAccounts();
    } catch (e) { setMsg("li-loop-msg", "Couldn’t connect: " + e.message, "error"); }
  };
  if ($("li-save")) $("li-save").onclick = async () => {
    const lm = $("li-msg"); lm.textContent = "Saving…"; lm.className = "muted msg";
    const body = { url: $("li-url").value.trim(), enabled: $("li-enabled").checked };
    const key = $("li-key").value.trim();
    if (key) body.api_key = key;   // only overwrites the stored key when non-empty
    if ($("li-root")) body.root_folder = $("li-root").value;
    if ($("li-qual")) body.quality_profile_id = Number($("li-qual").value || 0);
    if ($("li-meta")) body.metadata_profile_id = Number($("li-meta").value || 0);
    try {
      state.lidarr = await apiPost("/api/lidarr/config", body);
      renderAccounts();   // re-render to reflect status + load option dropdowns
    } catch (e) { lm.textContent = "Couldn’t save: " + e.message; lm.className = "muted msg err"; }
  };
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
    const data = `data-svc="${esc(a.service)}" data-id="${esc(a.id || "")}" data-aids="${esc(aids)}" data-title="${esc(a.title || "")}" data-artist="${esc(a.artist || "")}" data-mbid="${esc(a.mbid || "")}"`;
    const more = moreBtn(`More actions for ${a.title || "album"}`);
    if (nav)
      return `<a class="albrow" ${data} href="${routeHref("album", a.service, a.id)}">${inner}<span class="alb-state"></span>${more}</a>`;
    return `<div class="albrow info" ${data} title="From MusicBrainz credits — not available to play">${inner}<span class="alb-state"><span class="badge">credit</span></span>${more}</div>`;
  }).join("");
}

function wireAlbumRows(scope) {
  scope.querySelectorAll(".albrow").forEach((row) => {
    const a = () => ({
      service: row.dataset.svc,
      id: row.dataset.id || null,
      artist_ids: row.dataset.aids ? row.dataset.aids.split(",").filter(Boolean) : [],
      title: row.dataset.title || "",
      artist: row.dataset.artist || "",
      mbid: row.dataset.mbid || "",
      localId: row.dataset.localId || "",
    });
    row.addEventListener("contextmenu", (e) => openAlbumContextMenu(e, a()));
    const more = row.querySelector(".more");
    // The ⋯ sits inside the row's link: keep its click from navigating.
    if (more) more.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); menuAt(e.currentTarget, albumMenuItems(a())); });
  });
}

// Artist chips that carry a provider id (smart-search "Artists"): ⋯ and
// right-click → play / queue the artist's top tracks (+ Lidarr).
function artistChipHtml(ar) {
  return `<span class="chipwrap" data-artist-svc="${esc(ar.service)}" data-artist-id="${esc(ar.id)}" data-artist-name="${esc(ar.name)}">
    <a class="chip" href="${routeHref("artist", ar.service, ar.id)}"><span class="chip-name">${esc(ar.name)}</span><span class="chip-sub muted">${esc(serviceLabel(ar.service))}</span></a>
    ${moreBtn(`More actions for ${ar.name || "artist"}`)}</span>`;
}
function wireArtistChips(scope) {
  scope.querySelectorAll(".chipwrap[data-artist-id]").forEach((w) => {
    const ar = { service: w.dataset.artistSvc, id: w.dataset.artistId, name: w.dataset.artistName };
    w.addEventListener("contextmenu", (e) => menuAt(e, artistMenuItems(ar)));
    const more = w.querySelector(".more");
    if (more) more.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); menuAt(e.currentTarget, artistMenuItems(ar)); });
  });
}

// Members / bands / performers — clicking one runs a smart search for the name
// (these come from MusicBrainz and carry no provider id of their own).
function peopleChipsHtml(people, opts = {}) {
  return `<div class="chips">${people.map((p) => {
    const sub = opts.instruments && p.instruments && p.instruments.length
      ? p.instruments.join(", ") : spanLabel(p.spans);
    return `<button class="chip" data-name="${esc(p.name)}" data-lidarr-artist="${esc(p.name)}"${p.mbid ? ` data-lidarr-mbid="${esc(p.mbid)}"` : ""}>
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

function detailHeader(title, subHtml, artUrl, kind, actsHtml) {
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
        ${actsHtml || ""}
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
  const topSec = top.length ? `<section class="detail-sec">
      <div class="sec-head"><h3>Top tracks</h3>
        <button class="act ghost small" id="top-play" type="button">${ICON("play")} Play</button>
        <button class="act ghost small" id="top-shuffle" type="button">${ICON("shuffle")} Shuffle</button></div>
      ${tracksHtml(top)}</section>` : "";

  const members = d.members || [];
  const bands = d.member_of || [];
  let peopleSec = "";
  if (members.length) peopleSec += `<section class="detail-sec"><h3>Members</h3>${peopleChipsHtml(members, { instruments: true, current: true })}</section>`;
  if (bands.length) peopleSec += `<section class="detail-sec"><h3>Member of</h3>${peopleChipsHtml(bands, {})}</section>`;

  const artistRef = { service, id, name: a.name || "", mbid: d.mbid || "", tracks: top, here: true };
  list.innerHTML = `<div class="detail">
    ${detailHeader(a.name || "Unknown artist", "", a.image_url, kindLabel,
      top.length ? collectionActsHtml("ar", { playLabel: "Play top tracks" })
        : `<div class="hero-acts"><button class="act ghost icon-only" id="ar-more" type="button" aria-label="More actions" aria-haspopup="menu">${ICON("more")}</button></div>`)}
    ${bioHtml(a.bio)}
    ${chartSec}
    ${albumsSec}
    ${topSec}
    ${peopleSec}
  </div>`;
  wireBack();
  hydrateArt(list);
  wireAlbumRows(list);
  annotateAlbumStates(list);
  wireChips(list);
  // Right-click the artist hero → play/queue the top tracks, "Get with Lidarr".
  const hero = list.querySelector(".detail-hero");
  if (hero) hero.addEventListener("contextmenu", (e) => menuAt(e, artistMenuItems(artistRef)));
  wireCollectionActs("ar", () => top, () => artistMenuItems(artistRef));
  if (!top.length && $("ar-more")) $("ar-more").onclick = (e) => menuAt(e.currentTarget, artistMenuItems(artistRef));
  wireLidarrArtistTargets(list);
  if (top.length) {
    state.queue = top; wireTrackRows(list, top); highlightPlaying();
    $("top-play").onclick = () => playFrom(top, 0);
    $("top-shuffle").onclick = () => playFrom(top, null, { shuffle: true });
  }
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
    ${detailHeader(al.title || "Album", bits, al.artwork_url, "Album", tracks.length ? collectionActsHtml("al") : "")}
    <div id="al-state"></div>
    ${bioHtml(d.bio)}
    <section class="detail-sec">${tracksSec}</section>
  </div>`;
  wireBack();
  hydrateArt(list);
  if (service !== "local") renderAlbumState($("al-state"), { title: al.title || "", artist: al.artist || "", mbid: al.mbid || "" });
  if (tracks.length) {
    const albumRef = { service, id, title: al.title || "", artist: al.artist || "", mbid: al.mbid || "",
                       artist_ids: ref ? [ref.id] : [] };
    const items = () => [
      { label: "Play next", fn: () => playNextTracks(tracks, albumRef.title || "album") },
      { label: "Add to queue", fn: () => enqueueTracks(tracks, albumRef.title || "album") },
      { label: "Add all to playlist…", fn: () => openAddMenu($("al-more"), tracks) },
      ...(ref ? [{ label: "Go to artist", fn: () => navigateArtist(ref.service, ref.id) }] : []),
      ...(service !== "local" ? lidarrMenuItems({ kind: "album", title: albumRef.title, artist: albumRef.artist, mbid: albumRef.mbid }) : []),
    ];
    wireCollectionActs("al", () => tracks, items);
    state.queue = tracks; wireTrackRows(list, tracks, { numbered: true }); highlightPlaying();
  }
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
          <button class="chip" data-name="${esc(p.name)}" data-lidarr-artist="${esc(p.name)}"><span class="chip-name">${esc(p.name)}</span></button>
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
    <section class="detail-sec hero-acts">
      <button class="act" id="tk-play" type="button">${ICON("play")} Play track</button>
      <button class="act ghost" id="tk-next" type="button">Play next</button>
      <button class="act ghost" id="tk-queue" type="button">Add to queue</button>
      <button class="act ghost icon-only" id="tk-more" type="button" aria-label="More actions" aria-haspopup="menu">${ICON("more")}</button>
    </section>
    ${perfSec}
  </div>`;
  wireBack();
  hydrateArt(list);
  wireChips(list);
  wireLidarrArtistTargets(list);
  const tk = { ...t, service: t.service || service, id: t.id || id };
  $("tk-play").onclick = () => (sameTrack(pq.current(), tk) && !isStopped() ? togglePlay() : playFrom([tk], 0));
  $("tk-next").onclick = () => playNextTracks([tk], tk.title);
  $("tk-queue").onclick = () => enqueueTracks([tk], tk.title);
  $("tk-more").onclick = (e) => menuAt(e.currentTarget,
    trackMenuItems(tk, e.currentTarget).filter((it) => it.label !== "Track details"));
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
    html += `<section class="detail-sec"><h3>Artists</h3><div class="chips">${inc.artists.map(artistChipHtml).join("")}</div></section>`;
  }
  if (inc.playlists && inc.playlists.length) {
    html += `<section class="detail-sec"><h3>Playlists</h3><div class="plgrid">${inc.playlists.map(playlistCardHtml).join("")}</div></section>`;
  }

  if (!html) {
    list.innerHTML = emptyState("search", `No results for “${q}”`, "Try a different title or artist.");
    return;
  }
  list.innerHTML = html;
  hydrateArt(list);
  wireAlbumRows(list);
  annotateAlbumStates(list);
  wireLidarrArtistTargets(list);
  wireArtistChips(list);
  if (tracks.length) { state.queue = tracks; wireTrackRows(list, tracks); highlightPlaying(); }
  wirePlaylistCards(list);
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

// Create a playlist (optionally seeded with `addTracks` — only the chosen
// service's tracks can go in). Returns {service, id, title} or null.
async function newPlaylist(addTracks, opts = {}) {
  const tracks = (addTracks || []).filter(Boolean);
  // Default to the service most of the tracks come from.
  const counts = {};
  tracks.forEach((t) => { counts[t.service] = (counts[t.service] || 0) + 1; });
  const major = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
  const r = await modalPrompt({ title: opts.dialogTitle || "New playlist", okText: "Create", fields: [
    { name: "title", label: "Title", type: "text", placeholder: "Playlist name", value: opts.title || "" },
    { name: "service", label: "Service", type: "select", value: major === "ytmusic" ? "ytmusic" : "qobuz",
      options: [{ value: "qobuz", label: "Qobuz" }, { value: "ytmusic", label: "YouTube Music" }] },
  ] });
  if (!r || !r.title) return null;
  if (tracks.length && !tracks.some((t) => t.service === r.service)) {
    toastErr(`None of these tracks are on ${serviceLabel(r.service)} — pick the other service.`);
    return null;
  }
  let created;
  try { created = await apiPost("/api/playlists", { service: r.service, title: r.title }); }
  catch (e) { toastErr("Couldn’t create the playlist: " + e.message); return null; }
  _playlistCache = null;
  if (tracks.length && created && created.id) await addTracksToPlaylist(r.service, created.id, r.title, tracks);
  else toast("Playlist created.", "ok");
  if (state.section === "playlists" && !state.detail && !state.playlist && !$("list").querySelector(".npview")) loadPlaylists();
  else loadPlaylistsSilently();
  return created && created.id ? { service: r.service, id: created.id, title: r.title } : null;
}

async function saveQueueAsPlaylist() {
  if (!pq.tracks.length) { toast("The queue is empty."); return; }
  const d = new Date();
  await newPlaylist(pq.tracks.slice(), { dialogTitle: "Save queue as playlist",
    title: `Queue ${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` });
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
//
// Two outputs share one PlayQueue (`pq`):
//   * the BROWSER (this tab's <audio>) runs the queue client-side, with the
//     same rules as src/harmony/playqueue.py;
//   * a DEVICE (WiiM/UPnP/Chromecast) has its queue OWNED by the instance next
//     to it, which auto-advances it. We hand it the whole list once (queue/load),
//     map every control to a queue op, and poll GET /queue every ~2s to mirror
//     the server's snapshot into `pq` (so auto-advance and other clients show).
// Switching output hands the queue + position over (switchOutput).

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
  const want = playing ? "pause" : "play";
  if (b.dataset.ico === want) return;
  b.dataset.ico = want;
  b.setAttribute("aria-label", playing ? "Pause" : "Play");
  b.innerHTML = ICON(want);
}

function currentDeviceName() {
  if (!onDevice()) return "This browser";
  const sel = $("np-device");
  const v = encodeTarget(state.target, state.targetVia);
  const o = sel && [...sel.options].find((x) => x.value === v);
  return o ? o.textContent : state.target;
}

function updateCastChip() {
  const via = $("np-via");
  const np = $("nowplaying");
  if (onDevice()) {
    via.classList.add("show");
    via.querySelector("span").textContent = dev.pending ? `Ready on ${currentDeviceName()} — press play` : `Playing on ${currentDeviceName()}`;
    np.classList.add("casting");
  } else { via.classList.remove("show"); np.classList.remove("casting"); }
}

// Browser output: this tab's <audio>, driven by the local PlayQueue.
const br = {
  gen: 0,          // bumps on every play request; a stale /api/resolve reply is dropped
  loaded: null,    // the queue item whose stream is in <audio> (by identity), or null
  stopped: true,   // stopped / finished / never started — Play (re)starts pq.index
  resumePos: 0,    // where Play starts when nothing is loaded (restore / hand-off)
  failures: 0,     // consecutive tracks that failed to start
};
// Device output: the server owns the queue; `pq` mirrors its snapshot.
const dev = {
  running: false,  // the server's queue is playing (not stopped / finished)
  paused: false,
  pos: 0,          // seconds, interpolated between polls, reconciled to position_s
  dur: 0,
  sig: "",         // last snapshot's queue signature (re-render the queue on change)
  lastErr: null,
  pending: null,   // null | "handoff" | "restore": our queue isn't on the device yet
  pendingPos: 0,   // …and where it would start
  gen: 0, applied: 0, inflight: 0,   // ordering of snapshot responses
  volSetAt: 0,     // user moved the volume recently: don't let a poll yank it back
  holdUntil: 0,    // after a user pause/resume: the server's status lags ~2s, keep ours
};

// A user action that changes device state optimistically: drop any poll that
// was already in flight (it predates the action).
function devFence() { dev.applied = ++dev.gen; }

const onDeviceLive = () => onDevice() && !dev.pending;

function isPlaying() {
  if (onDevice()) return !dev.pending && dev.running && !dev.paused;
  return !!br.loaded && !audio.paused;
}
function isStopped() {
  if (onDevice()) return !!dev.pending || !dev.running;
  return br.stopped;
}
function currentPos() {
  if (onDevice()) return dev.pending ? dev.pendingPos : dev.pos;
  return br.loaded ? (audio.currentTime || 0) : br.resumePos;
}
function currentDur() {
  const t = pq.current();
  if (onDevice()) return dev.dur || (t && t.duration_s) || 0;
  const d = audio.duration;
  return br.loaded && isFinite(d) && d > 0 ? d : ((t && t.duration_s) || 0);
}

// -- painting -----------------------------------------------------------------

let seeking = false;       // the user is dragging the seek bar
let _msPosAt = 0;

function msPosition(pos, dur, force) {
  if (!("mediaSession" in navigator) || !navigator.mediaSession.setPositionState) return;
  const now = Date.now();
  if (!force && now - _msPosAt < 1000) return;
  _msPosAt = now;
  try {
    if (!dur || !isFinite(dur) || dur <= 0) return;
    navigator.mediaSession.setPositionState({ duration: dur, position: Math.min(Math.max(0, pos || 0), dur), playbackRate: 1 });
  } catch { /* ignore */ }
}

function paintProgress(force) {
  const pos = currentPos(), dur = currentDur();
  const seek = $("np-seek");
  if (!seeking) {
    seek.max = Math.max(1, Math.floor(dur || 1));
    seek.value = Math.floor(dur ? Math.min(pos, dur) : pos);
    $("np-pos").textContent = fmtTime(pos);
  }
  $("np-dur").textContent = fmtTime(dur);
  msPosition(pos, dur, force);
}

function paintPlayState() {
  const playing = isPlaying();
  setPlayIcon(playing);
  if ("mediaSession" in navigator) {
    try { navigator.mediaSession.playbackState = pq.current() ? (playing ? "playing" : "paused") : "none"; } catch { /* ignore */ }
  }
  highlightPlaying();
}

function updateMediaSession(t) {
  if (!("mediaSession" in navigator)) return;
  try {
    const art = artOf(t);
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title || "", artist: t.artist || "", album: t.album || "",
      artwork: art ? [{ src: art, sizes: "512x512" }] : [],
    });
  } catch { /* ignore */ }
}

let _barKey = null;
// Bring the bar, the Now Playing view, row indicators and Media Session in
// line with pq / the output state. Cheap enough to call after any change.
function paintCurrent() {
  const t = pq.current();
  const key = t ? `${pq.index}|${trackKey(t)}` : "";
  if (t) {
    $("np-title").textContent = t.title || "";
    $("np-artist").textContent = t.artist || "";
    $("nowplaying").classList.remove("empty");
    if (key !== _barKey) { setArt(artOf(t)); updateMediaSession(t); }
  } else {
    $("np-title").textContent = "Nothing playing";
    $("np-artist").textContent = "";
    $("nowplaying").classList.add("empty");
    if (key !== _barKey) setArt("");
  }
  _barKey = key;
  updateCastChip();
  updateNowPlayingView();
  paintModes();
  paintPlayState();
  paintProgress();
  savePlayback();
}

// Re-render the Now Playing queue when it is the open view (queue changed).
function refreshQueueUi() { if (!state.dragging && $("list").querySelector(".npview")) renderNowPlaying(); }
function paintAll() { refreshQueueUi(); paintCurrent(); }

// -- browser output -------------------------------------------------------------

function clearAudio() {
  br.loaded = null;
  try { audio.pause(); } catch { /* ignore */ }
  if (audio.getAttribute("src")) {
    audio.removeAttribute("src");
    try { audio.load(); } catch { /* ignore */ }
  }
}

function stopBrowser() {
  br.gen++;
  clearAudio();
  br.stopped = true;
  br.resumePos = 0;
  paintCurrent();
}

// Play queue item `i` in this tab. A newer request always wins: every call
// bumps br.gen and a reply for an older generation is ignored.
async function playBrowserAt(i, { seek = 0, autoplay = true } = {}) {
  if (pq.jump(i) === null) return;
  const gen = ++br.gen;
  const t = pq.current();
  clearAudio();
  br.stopped = false;
  br.resumePos = seek || 0;
  paintCurrent();
  let r;
  try { r = await api(`/api/resolve?service=${encodeURIComponent(t.service)}&id=${encodeURIComponent(t.id)}`); }
  catch (e) { if (gen === br.gen && !onDevice()) trackFailed(t, e.message); return; }
  if (gen !== br.gen || onDevice()) return;      // superseded (newer play / output switch)
  br.loaded = t;
  audio.src = `/stream/${r.token}${keyParam()}`;
  if (seek > 0) {
    audio.addEventListener("loadedmetadata", () => {
      if (gen === br.gen) { try { audio.currentTime = seek; } catch { /* ignore */ } }
    }, { once: true });
  }
  if (!autoplay) { paintPlayState(); return; }
  try { await audio.play(); }
  catch (e) {
    if (gen !== br.gen) return;
    if (e && e.name === "NotAllowedError") { toast("Press play to start."); }
    // Media errors surface through the 'error' event (which skips ahead).
  }
  if (gen === br.gen) paintPlayState();
}

// A track wouldn't start: skip to the next one, and stop once every track in
// the queue has failed in a row (so a dead account can't spin forever).
function trackFailed(t, msg) {
  br.failures += 1;
  const n = pq.following(true);
  const title = (t && t.title) || "track";
  if (n === null || br.failures >= pq.tracks.length) {
    br.failures = 0;
    stopBrowser();
    toastErr(pq.tracks.length > 1
      ? `Couldn’t play “${title}” — stopped, nothing left in the queue would play. (${msg})`
      : `Couldn’t play “${title}”: ${msg}`);
    return;
  }
  toastErr(`Couldn’t play “${title}” — skipping. (${msg})`);
  playBrowserAt(n);
}

audio.addEventListener("error", () => {
  if (onDevice() || !br.loaded || !audio.getAttribute("src")) return;
  const t = br.loaded, err = audio.error;
  const msg = err ? (err.message || `media error ${err.code}`) : "stream error";
  br.gen++;
  clearAudio();
  trackFailed(t, msg);
});
audio.addEventListener("playing", () => { br.failures = 0; paintPlayState(); });
audio.addEventListener("play", () => { if (!onDevice()) paintPlayState(); });
audio.addEventListener("pause", () => { if (!onDevice()) { paintPlayState(); savePlayback(); } });
audio.addEventListener("ended", () => {
  if (onDevice() || !br.loaded) return;
  const n = pq.following(false);      // repeat-one holds only on a natural end
  if (n === null) { stopBrowser(); return; }   // finished: stays on the last track
  playBrowserAt(n);
});
audio.addEventListener("loadedmetadata", () => { if (!onDevice()) paintProgress(true); });
let _posSavedAt = 0;
audio.addEventListener("timeupdate", () => {
  if (onDevice()) return;
  paintProgress();
  if (Date.now() - _posSavedAt > 5000) { _posSavedAt = Date.now(); savePlayback(); }
});

// -- device output ---------------------------------------------------------------

const tgtNow = () => ({ host: state.target, via: state.targetVia });
const sameTgt = (a) => a.host === state.target && (a.via || null) === (state.targetVia || null);
const devUrl = (tgt, op) => `/api/devices/${encodeURIComponent(tgt.host)}/${op}`;
const viaBody = (tgt, body) => (tgt.via ? { ...body, via: tgt.via } : body);
// The server's cast meta reads art_url; the web client's tracks carry artwork_url.
const wireTrack = (t) => ({ ...t, art_url: t.art_url || t.artwork_url || null, artwork_url: t.artwork_url || t.art_url || null });
const normTrack = (t) => ({ ...t, artwork_url: t.artwork_url || t.art_url || "" });

// Like api()/apiPost(), but a queue snapshot's own `error` field (the last
// track that failed to start) is data, not a failed request.
async function apiSnap(path, body, _retry) {
  const init = body === undefined ? { headers: keyHeaders() }
    : { method: "POST", headers: keyHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) };
  const r = await fetch(path, init);
  if (r.status === 401 && !_retry && await promptKey()) return apiSnap(path, body, true);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error((j && j.error) || `HTTP ${r.status}`);
  if (!j || typeof j !== "object" || !Array.isArray(j.tracks)) throw new Error((j && j.error) || "unexpected reply");
  return j;
}

async function devQueue(op, body = {}, tgt = tgtNow()) {
  const g = ++dev.gen;
  dev.inflight++;
  try {
    const snap = await apiSnap(devUrl(tgt, `queue/${op}`), viaBody(tgt, body));
    if (sameTgt(tgt) && g >= dev.applied) { dev.applied = g; applySnapshot(snap); }
    return snap;
  } finally { dev.inflight--; }
}
async function devOp(op, body, what) {
  try { return await devQueue(op, body || {}); }
  catch (e) { toastErr(`Couldn’t ${what || op} on ${currentDeviceName()}: ${e.message}`); return null; }
}

let _pollBusy = false;
async function pollDevice() {
  if (!onDevice() || dev.inflight || _pollBusy) return;
  const tgt = tgtNow(), g = ++dev.gen;
  _pollBusy = true;
  try {
    const snap = await apiSnap(devUrl(tgt, "queue") + (tgt.via ? `?via=${encodeURIComponent(tgt.via)}` : ""));
    if (sameTgt(tgt) && g >= dev.applied && !dev.inflight) { dev.applied = g; applySnapshot(snap); }
  } catch { /* device mid-buffer / peer blip — the next poll catches up */ }
  finally { _pollBusy = false; }
}

// Mirror a server snapshot {tracks, index, shuffle, repeat, playing, error,
// state, position_s, duration_s, volume} into pq + the device state.
function applySnapshot(s) {
  if (!s || !Array.isArray(s.tracks)) return;
  applyDeviceVolume(s.volume);
  if (state.dragging) return;          // don't pull rows out from under a drag
  const running = !!s.playing;
  if (dev.pending) {
    // Our queue is waiting for Play. A device busy with its own queue wins; on
    // a restore, so does any queue the server still holds.
    if (!(running || (dev.pending === "restore" && s.tracks.length))) { paintCurrent(); return; }
    if (running && dev.pending === "handoff") toast(`${currentDeviceName()} is already playing — showing its queue.`);
    dev.pending = null;
  }
  const idx = Number.isInteger(s.index) && s.index < s.tracks.length ? s.index : -1;
  const sig = s.tracks.map(trackKey).join("|") + `#${idx}`;
  const changed = sig !== dev.sig;
  if (changed) {
    const tracks = s.tracks.map(normTrack);
    pq.tracks = tracks; pq.original = tracks.slice(); pq.index = idx;
    dev.sig = sig;
  }
  pq.shuffle = !!s.shuffle;
  if (REPEAT_MODES.includes(s.repeat)) pq.repeat = s.repeat;
  dev.running = running;
  if (!running || Date.now() >= dev.holdUntil) dev.paused = running && /paus/i.test(s.state || "");
  const cur = pq.current();
  dev.dur = Number(s.duration_s) || (cur && cur.duration_s) || 0;
  dev.pos = running ? Math.max(0, Number(s.position_s) || 0) : 0;
  if (s.error && s.error !== dev.lastErr) toastErr(`${currentDeviceName()}: ${s.error}`);
  dev.lastErr = s.error || null;
  persistPrefs();
  if (changed) refreshQueueUi();
  paintCurrent();
}

// 1s ticker while on a device: interpolate the bar, poll the queue every 2s.
let devTimer = null, _devTick = 0;
function stopDevicePoll() { if (devTimer) { clearInterval(devTimer); devTimer = null; } }
function startDevicePoll() {
  stopDevicePoll();
  _devTick = 0;
  devTimer = setInterval(() => {
    if (!onDevice()) { stopDevicePoll(); return; }
    _devTick++;
    if (isPlaying()) {
      dev.pos = dev.dur ? Math.min(dev.pos + 1, dev.dur) : dev.pos + 1;
      paintProgress();
    }
    if (_devTick % 2 === 0) pollDevice();
  }, 1000);
}

// Put our local queue on the device (a hand-off, or Play on a pending queue):
// the list exactly as the listener has it, then seek to where they were.
async function pushQueueToDevice(pos = 0) {
  if (!pq.current()) return null;
  dev.pending = null;
  dev.running = true; dev.paused = false; dev.pos = pos || 0;
  paintCurrent();
  const tgt = tgtNow();
  const snap = await devOp("load", {
    tracks: pq.tracks.map(wireTrack), start: pq.index,
    shuffle: pq.shuffle, repeat: pq.repeat, keep_order: true,
  }, "start playback");
  if (!snap) {                                   // keep the queue ready for another try
    if (sameTgt(tgt)) { dev.pending = "handoff"; dev.pendingPos = pos || 0; dev.running = false; paintCurrent(); }
    return null;
  }
  // Give the renderer a moment to start the stream before seeking into it.
  if (pos > 1) setTimeout(() => { if (sameTgt(tgt) && dev.running) seekTo(pos); }, 1500);
  return snap;
}

// -- volume ----------------------------------------------------------------------

function setVolumeSliders(v) {
  $("np-vol").value = v;
  const nv = $("np-view-vol");
  if (nv) nv.value = v;
}
function applyDeviceVolume(v) {
  if (v == null || !onDevice() || Date.now() - dev.volSetAt < 4000) return;
  const n = Number(v);
  if (isFinite(n)) setVolumeSliders(Math.round(n));
}
let _volSentAt = 0, _volTimer = null;
function setVolume(v) {
  v = Math.max(0, Math.min(100, Math.round(v)));
  setVolumeSliders(v);
  if (!onDevice()) { audio.volume = v / 100; _prefVolume = v; persistPrefs(); return; }
  dev.volSetAt = Date.now();
  const tgt = tgtNow();
  const send = () => {
    _volSentAt = Date.now();
    apiPost(devUrl(tgt, "volume"), viaBody(tgt, { level: v })).catch((e) => toastErr("Couldn’t set the volume: " + e.message));
  };
  clearTimeout(_volTimer);
  // Throttled with a trailing send, so a drag doesn't flood the device.
  if (Date.now() - _volSentAt > 300) send(); else _volTimer = setTimeout(send, 300);
}

// -- transport + queue ops (dispatch per output) -----------------------------------

async function seekTo(sec) {
  if (!pq.current()) return;
  const dur = currentDur();
  sec = Math.max(0, dur ? Math.min(sec, Math.max(0, dur - 1)) : sec);
  if (onDevice()) {
    if (dev.pending) { dev.pendingPos = sec; paintProgress(true); return; }
    if (!dev.running) return;
    dev.pos = sec; paintProgress(true);
    devFence();
    const tgt = tgtNow();
    dev.inflight++;
    try { await apiPost(devUrl(tgt, "seek"), viaBody(tgt, { level: Math.floor(sec) })); }
    catch (e) { toastErr("Couldn’t seek: " + e.message); }
    finally { dev.inflight--; }
    return;
  }
  if (br.loaded) { try { audio.currentTime = sec; } catch { /* ignore */ } }
  else br.resumePos = sec;
  paintProgress(true);
  savePlayback();
}
const seekBy = (d) => seekTo(currentPos() + d);

// Start item `i` when the queue lives here (the browser, or a device our queue
// isn't on yet — which then receives it).
function startLocal(i, pos = 0) {
  if (onDevice()) { if (pq.jump(i) === null) return null; return pushQueueToDevice(pos); }
  return playBrowserAt(i, { seek: pos });
}

async function togglePlay() {
  if (!pq.tracks.length) return;
  if (onDevice()) {
    if (dev.pending) { if (pq.index < 0) pq.jump(0); return pushQueueToDevice(dev.pendingPos); }
    if (!dev.running) return devOp("jump", { index: Math.max(0, pq.index) }, "start playback");
    const action = dev.paused ? "resume" : "pause";
    dev.paused = !dev.paused; paintCurrent();      // optimistic; later polls confirm
    devFence();
    dev.holdUntil = Date.now() + 4000;
    const tgt = tgtNow();
    dev.inflight++;
    try { await apiPost(devUrl(tgt, action), viaBody(tgt, {})); }
    catch (e) { if (sameTgt(tgt)) { dev.paused = !dev.paused; dev.holdUntil = 0; paintCurrent(); } toastErr(`Couldn’t ${action}: ${e.message}`); }
    finally { dev.inflight--; }
    return;
  }
  // Only resume <audio> if it holds the CURRENT queue item — never a stale src.
  if (br.loaded && br.loaded === pq.current() && audio.getAttribute("src")) {
    if (audio.paused) { br.stopped = false; audio.play().catch(() => { /* 'error' handles it */ }); }
    else audio.pause();
    return;
  }
  return playBrowserAt(Math.max(0, pq.index), { seek: br.resumePos });
}
const doPlay = () => { if (!isPlaying()) togglePlay(); };
const doPause = () => { if (isPlaying()) togglePlay(); };

function doNext() {
  if (!pq.tracks.length) return;
  if (onDeviceLive()) return devOp("next", {}, "skip");
  const n = pq.advance(true);
  if (n === null) { if (!onDevice()) stopBrowser(); return; }
  return startLocal(n);
}

function doPrev() {
  if (!pq.tracks.length) return;
  if (onDeviceLive()) return devOp("prev", {}, "go back");   // server restarts if >3s in
  const before = pq.current();
  const i = pq.previous(currentPos());
  if (i === null) return;
  // Restarting the loaded track: just rewind it.
  if (!onDevice() && pq.current() === before && br.loaded === before && audio.getAttribute("src")) {
    audio.currentTime = 0;
    if (audio.paused) { br.stopped = false; audio.play().catch(() => {}); }
    return;
  }
  return startLocal(i);
}

function jumpTo(i) {
  if (onDeviceLive()) return devOp("jump", { index: i }, "play that track");
  return startLocal(i);
}

// Stop: halt playback but keep the queue (and the stopped index) — Play then
// restarts that track, not the first one.
function stopPlayback() {
  if (onDevice()) {
    if (dev.pending) { dev.pendingPos = 0; paintCurrent(); return; }
    dev.running = false; dev.paused = false; dev.pos = 0;
    paintCurrent();
    devOp("stop", {}, "stop");
    return;
  }
  stopBrowser();
}

// Load a displayed list as the active queue and play from `index`
// (index null / opts.shuffle → shuffle-play from a random track, shuffle on).
function playFrom(list, index, opts = {}) {
  const keep = [];
  let start = null;
  (list || []).forEach((t, i) => {
    if (t && t.service && t.id != null && t.id !== "") { if (i === index) start = keep.length; keep.push({ ...t }); }
  });
  if (!keep.length) { toastErr("Nothing playable here."); return; }
  if (opts.shuffle) { pq.shuffle = true; persistPrefs(); }
  const shufflePlay = !!opts.shuffle || index == null;
  if (!shufflePlay && start === null) start = 0;
  if (onDevice()) {
    // Optimistic: show the list now; the server's snapshot (its order) replaces it.
    pq.load(keep, shufflePlay ? null : start);
    dev.pending = null; dev.running = true; dev.paused = false; dev.pos = 0; dev.dur = 0;
    paintAll();
    // The UNshuffled list: the server applies shuffle itself, and we adopt its order.
    devOp("load", { tracks: keep.map(wireTrack), start: shufflePlay ? null : start,
                    shuffle: pq.shuffle, repeat: pq.repeat }, "start playback");
    return;
  }
  pq.load(keep, shufflePlay ? null : start);
  refreshQueueUi();
  playBrowserAt(pq.index);
}

// Nothing is playing (so an enqueue starts what it adds).
const localIdle = () => (onDevice() ? pq.index < 0 : (br.stopped || pq.index < 0));
const playable = (tracks) => (tracks || []).filter((t) => t && t.service && t.id != null && t.id !== "").map((t) => ({ ...t }));

// Append tracks to the END of the active queue (starts them if idle).
function enqueueTracks(tracks, label) {
  const add = playable(tracks);
  if (!add.length) return;
  const msg = label ? `Added “${label}” to the queue.` : `Added ${nTracks(add.length)} to the queue.`;
  if (onDeviceLive()) {
    devOp("enqueue", { tracks: add.map(wireTrack) }, "add to the queue").then((s) => { if (s) toast(msg, "ok"); });
    return;
  }
  const start = pq.enqueue(add, localIdle());
  refreshQueueUi(); paintCurrent();
  if (start !== null) startLocal(start); else toast(msg, "ok");
}

// Insert tracks right after the current one (starts them if idle).
function playNextTracks(tracks, label) {
  const add = playable(tracks);
  if (!add.length) return;
  const msg = label ? `“${label}” plays next.` : `${nTracks(add.length)} play next.`;
  if (onDeviceLive()) {
    devOp("play_next", { tracks: add.map(wireTrack) }, "queue that").then((s) => { if (s) toast(msg, "ok"); });
    return;
  }
  const start = pq.playNext(add, localIdle());
  refreshQueueUi(); paintCurrent();
  if (start !== null) startLocal(start); else toast(msg, "ok");
}

// Remove one queue item; removing the playing one plays whatever slid into its slot.
function removeFromQueue(i) {
  if (onDeviceLive()) return devOp("remove", { index: i }, "remove that track");
  const idle = onDevice() || br.stopped;
  const [removedCur, start] = pq.remove(i);
  if (removedCur) {
    if (!idle && start !== null) { startLocal(start); refreshQueueUi(); return; }
    if (!idle) stopBrowser();
    else if (onDevice()) dev.pendingPos = 0;
    else { clearAudio(); br.resumePos = 0; }
  }
  refreshQueueUi(); paintCurrent();
}

// Clear keeps the current track.
function clearQueue() {
  if (onDeviceLive()) return devOp("clear", {}, "clear the queue");
  pq.clear();
  refreshQueueUi(); paintCurrent();
}

function moveInQueue(from, to) {
  if (onDeviceLive()) {
    devOp("move", { from, to }, "reorder the queue").then((s) => { if (!s) renderNowPlaying(); });
    return;
  }
  pq.move(from, to);
  refreshQueueUi(); paintCurrent();
}

function toggleShuffle() {
  const on = !pq.shuffle;
  if (onDeviceLive()) { pq.shuffle = on; paintModes(); devOp("shuffle", { on }, "change shuffle"); return; }
  pq.setShuffle(on);
  persistPrefs();
  refreshQueueUi(); paintCurrent();
}

function cycleRepeat() {
  const mode = pq.repeat === "off" ? "all" : pq.repeat === "all" ? "one" : "off";
  if (onDeviceLive()) { pq.repeat = mode; paintModes(); devOp("repeat", { mode }, "change repeat"); return; }
  pq.setRepeat(mode);
  persistPrefs(); paintModes(); savePlayback();
}

// Enqueue / play-next / play an album: its tracks aren't on the row, so fetch them.
async function albumToQueue(a, where) {
  try {
    const d = await api(`/api/album/${encodeURIComponent(a.service)}/${encodeURIComponent(a.id)}`);
    const tracks = d.tracks || [];
    if (!tracks.length) { toastErr("That album has no playable tracks."); return; }
    collectionTo(tracks, where === "next" ? "next" : where === "play" || where === "shuffle" ? where : "end", a.title || "album");
  } catch (e) { toastErr("Couldn’t load that album: " + e.message); }
}

// -- output switching (hand-offs) ------------------------------------------------

function syncDeviceSelect() {
  const sel = $("np-device");
  if (!sel) return;
  const v = onDevice() ? encodeTarget(state.target, state.targetVia) : "browser";
  if (![...sel.options].some((o) => o.value === v)) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = state.targetVia ? `${state.target} (via ${state.targetVia})` : state.target;
    sel.appendChild(o);
  }
  sel.value = v;
}

// Move playback to another output, carrying the queue and position over:
//   browser → device: pause <audio>, load the queue there, seek to where we were
//   device → browser: stop the device, play here from its last position
//   device → device:  stop the old one, load the new one
// A paused/stopped queue isn't pushed — it waits on the new output for Play.
function switchOutput(value) {
  value = value || "browser";
  const from = tgtNow();
  if (value === (from.host === "browser" ? "browser" : encodeTarget(from.host, from.via))) return;
  const fromDevice = from.host !== "browser";
  const hadQueue = !!pq.current();
  const pos = currentPos();
  const wasPlaying = isPlaying();
  const wasStopped = fromDevice ? (!dev.pending && !dev.running) : br.stopped;
  const oldRunning = fromDevice && !dev.pending && dev.running;

  setTargetValue(value);
  syncDeviceSelect();
  stopDevicePoll();
  dev.gen++; dev.applied = dev.gen;       // drop replies meant for the old output
  if (oldRunning) devQueue("stop", {}, from).catch(() => { /* best-effort */ });
  Object.assign(dev, { running: false, paused: false, pos: 0, dur: 0, sig: "", lastErr: null, pending: null, pendingPos: 0, volSetAt: 0, holdUntil: 0 });
  if (!fromDevice) { br.gen++; clearAudio(); br.stopped = true; }

  if (onDevice()) {
    startDevicePoll();
    if (hadQueue && wasPlaying) pushQueueToDevice(pos);
    else {
      if (hadQueue) { dev.pending = "handoff"; dev.pendingPos = wasStopped ? 0 : pos; }
      pollDevice();
    }
  } else {
    setVolumeSliders(_prefVolume); audio.volume = _prefVolume / 100;
    br.stopped = wasStopped || !hadQueue;
    br.resumePos = wasStopped ? 0 : pos;
    if (hadQueue && wasPlaying) playBrowserAt(pq.index, { seek: pos });
  }
  paintAll();
  savePlaybackNow();
}

// -- persistence: the queue survives a reload ---------------------------------------

const QKEY = "harmonyQueue";
const SLIM_KEYS = ["service", "id", "title", "artist", "album", "artwork_url", "art_url", "duration_s",
                   "artist_ids", "album_id", "track_number"];
const slimTrack = (t) => { const o = {}; SLIM_KEYS.forEach((k) => { if (t[k] != null) o[k] = t[k]; }); return o; };
let _saveT = null;
function savePlayback() { clearTimeout(_saveT); _saveT = setTimeout(savePlaybackNow, 400); }
function savePlaybackNow() {
  clearTimeout(_saveT);
  try {
    const orig = pq.original.map((t) => pq.tracks.indexOf(t));
    localStorage.setItem(QKEY, JSON.stringify({
      v: 1, tracks: pq.tracks.map(slimTrack), index: pq.index,
      original: orig.length === pq.tracks.length && orig.every((i) => i >= 0) ? orig : null,
      pos: Math.floor(currentPos() || 0),
      target: onDevice() ? encodeTarget(state.target, state.targetVia) : "browser",
    }));
  } catch { /* storage full / blocked: not fatal */ }
}
window.addEventListener("pagehide", savePlaybackNow);
document.addEventListener("visibilitychange", () => { if (document.hidden) savePlaybackNow(); });

// Restore the last queue shown PAUSED (no autoplay). On a device, the server's
// queue is the truth — ours is only a fallback when the device has none.
function restorePlayback() {
  let s = null;
  try { s = JSON.parse(localStorage.getItem(QKEY) || "null"); } catch { s = null; }
  if (s && Array.isArray(s.tracks) && s.tracks.length) {
    const tracks = s.tracks.filter((t) => t && t.service && t.id != null && t.id !== "");
    if (tracks.length === s.tracks.length) {
      pq.tracks = tracks;
      pq.index = Number.isInteger(s.index) && s.index >= 0 && s.index < tracks.length ? s.index : 0;
      const orig = Array.isArray(s.original) && s.original.length === tracks.length
        ? s.original.map((i) => tracks[i]).filter(Boolean) : null;
      pq.original = orig && orig.length === tracks.length ? orig : tracks.slice();
    }
  }
  const pos = s && Number(s.pos) > 0 ? Number(s.pos) : 0;
  setTargetValue(s && typeof s.target === "string" && s.target ? s.target : "browser");
  setVolumeSliders(_prefVolume);
  audio.volume = _prefVolume / 100;
  if (onDevice()) {
    if (pq.current()) { dev.pending = "restore"; dev.pendingPos = pos; }
    startDevicePoll();
    pollDevice();
  } else {
    br.stopped = !pq.current();
    br.resumePos = pos;
  }
  syncDeviceSelect();
  paintCurrent();
}

// -- wiring: bar, keyboard, Media Session -------------------------------------------

$("np-play").addEventListener("click", () => togglePlay());
$("np-prev").addEventListener("click", () => doPrev());
$("np-next").addEventListener("click", () => doNext());
$("np-stop").addEventListener("click", () => stopPlayback());
wireModeButtons($("nowplaying"));
$("np-seek").addEventListener("input", () => { seeking = true; $("np-pos").textContent = fmtTime(Number($("np-seek").value)); });
$("np-seek").addEventListener("change", () => { seeking = false; seekTo(Number($("np-seek").value)); });
$("np-vol").addEventListener("input", () => setVolume(Number($("np-vol").value)));
$("np-device").addEventListener("change", (e) => switchOutput(e.target.value));

// Keyboard: Space play/pause, ←/→ seek ±10s, Shift+←/→ prev/next, S shuffle,
// R repeat — unless typing in a field or a dialog/menu is open.
document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
  const el = e.target;
  if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
  if (document.querySelector(".modal-back, .addmenu")) return;
  const k = e.key;
  if (k === " " || k === "Spacebar") { e.preventDefault(); togglePlay(); }
  else if (k === "ArrowRight" && e.shiftKey) { e.preventDefault(); doNext(); }
  else if (k === "ArrowLeft" && e.shiftKey) { e.preventDefault(); doPrev(); }
  else if (k === "ArrowRight") { e.preventDefault(); seekBy(10); }
  else if (k === "ArrowLeft") { e.preventDefault(); seekBy(-10); }
  else if (k === "s" || k === "S") { e.preventDefault(); toggleShuffle(); }
  else if (k === "r" || k === "R") { e.preventDefault(); cycleRepeat(); }
});

if ("mediaSession" in navigator) {
  const set = (action, fn) => { try { navigator.mediaSession.setActionHandler(action, fn); } catch { /* unsupported */ } };
  set("play", doPlay);
  set("pause", doPause);
  set("stop", stopPlayback);
  set("previoustrack", doPrev);
  set("nexttrack", doNext);
  set("seekto", (d) => { if (d && d.seekTime != null) seekTo(d.seekTime); });
  set("seekbackward", (d) => seekBy(-((d && d.seekOffset) || 10)));
  set("seekforward", (d) => seekBy((d && d.seekOffset) || 10));
}

async function loadDevices() {
  try {
    const devs = (await api("/api/devices?peers=1")).devices || [];
    const sel = $("np-device");
    while (sel.options.length > 1) sel.remove(1);
    for (const d of devs) {
      const o = document.createElement("option");
      o.value = encodeTarget(d.host, d.via);
      o.textContent = d.via ? `${d.name} (via ${d.via_name || d.via})` : d.name;
      sel.appendChild(o);
    }
  } catch { /* no devices */ }
  syncDeviceSelect();
  updateCastChip();
  updateNowPlayingView();
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
restorePlayback();   // last queue, shown paused (a device's comes from the server)
loadAccounts();
loadDevices();
loadLidarrStatus();
loadLibraryStatus();
if (parseHash()) renderRoute();   // deep link → render the detail page on load

// Progressive web app: install + offline shell.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => { /* non-fatal */ }));
}
