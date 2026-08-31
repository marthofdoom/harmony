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
  devicePaused: false,
};

const onDevice = () => state.target !== "browser";

const fmtTime = (s) => {
  if (!s || s < 0 || !isFinite(s)) return "0:00";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
};
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
const serviceLabel = (s) => ({ ytmusic: "YT Music", qobuz: "Qobuz" }[s] || s);

async function api(path) {
  const r = await fetch(path);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

async function apiPost(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
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
    const title = prompt("Rename playlist to:", pl.title); if (!title) return;
    try { await apiPost(`/api/playlists/${encodeURIComponent(pl.service)}/${encodeURIComponent(pl.id)}/rename`, { title });
      pl.title = title; $("view-title").textContent = title; loadPlaylistsSilently(); }
    catch (e) { alert("Rename failed: " + e.message); }
  };
  if ($("pl-delete")) $("pl-delete").onclick = async () => {
    if (!confirm(`Delete playlist “${pl.title}”?`)) return;
    try { await apiPost(`/api/playlists/${encodeURIComponent(pl.service)}/${encodeURIComponent(pl.id)}/delete`, {});
      state.playlist = null; setView("playlists"); }
    catch (e) { alert("Delete failed: " + e.message); }
  };
}

async function removeFromPlaylist(track, i) {
  const pl = state.playlist; if (!pl) return;
  try {
    await apiPost(`/api/playlists/${encodeURIComponent(pl.service)}/${encodeURIComponent(pl.id)}/remove`, { track_ids: [track.id] });
    const rest = state.queue.slice(0, i).concat(state.queue.slice(i + 1));
    renderTracks(rest);
  } catch (e) { alert("Remove failed: " + e.message); }
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
    try { await apiPost(`/api/playlists/${encodeURIComponent(row.dataset.service)}/${encodeURIComponent(row.dataset.id)}/add`, { track_ids: [track.id] }); }
    catch (e) { alert("Add failed: " + e.message); }
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
  document.querySelectorAll("#nav li").forEach((li) => li.classList.toggle("active", li.dataset.view === view));
  if (view === "search") { $("view-title").textContent = "Search"; $("search-input").focus(); }
  else if (view === "playlists") { $("view-title").textContent = "Playlists"; loadPlaylists(); }
  else if (view === "accounts") { $("view-title").textContent = "Accounts"; renderAccounts(); }
}

async function renderAccounts() {
  const list = $("list");
  list.innerHTML = `<p class="hint">Loading accounts…</p>`;
  let accounts = [], prefs = { personal_key: "" };
  try { accounts = (await api("/api/accounts")).accounts || []; } catch { /* show forms anyway */ }
  try { prefs = await api("/api/preferences"); } catch { /* ignore */ }
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
        <h2>YouTube Music <span class="badge">${y.authenticated ? "signed in" + (y.account ? " · " + esc(y.account) : "") : "signed out"}</span></h2>
        <p class="muted">Paste the request headers from a logged-in music.youtube.com tab
        (DevTools → Network → a request → Copy → Copy request headers).</p>
        <textarea id="yt-headers" rows="5" style="width:100%;font-family:monospace;font-size:12px" placeholder="Cookie: …\nX-Goog-…"></textarea>
        <div style="margin-top:.5rem;display:flex;gap:.5rem">
          <button class="act" id="yt-save">Save</button>
          ${y.authenticated ? `<button class="act ghost" id="yt-out">Sign out</button>` : ""}
        </div>
      </div>

      <div class="card">
        <h2>Qobuz <span class="badge">${q.authenticated ? "signed in" + (q.account ? " · " + esc(q.account) : "") : "signed out"}</span></h2>
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
    try { await apiPost("/api/preferences", { personal_key: $("pk").value }); msg("Personal key saved.", true); }
    catch (e) { msg("Failed: " + e.message); }
  };
  $("yt-save").onclick = async () => {
    const h = $("yt-headers").value.trim(); if (!h) return msg("Paste headers first.");
    try { await apiPost("/api/accounts/ytmusic/browser", { headers: h }); msg("YouTube Music saved.", true); after(); }
    catch (e) { msg("Failed: " + e.message); }
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
  const title = prompt("New playlist title:"); if (!title) return;
  const service = prompt("Service (qobuz or ytmusic):", "qobuz"); if (!service) return;
  try { await apiPost("/api/playlists", { service: service.trim(), title: title.trim() }); loadPlaylists(); }
  catch (e) { alert("Create failed: " + e.message); }
}

async function loadAccounts() {
  try {
    const r = await api("/api/accounts");
    $("accounts").innerHTML = (r.accounts || []).map((a) =>
      `<div class="acct"><span class="dot ${a.authenticated ? "ok" : ""}"></span>${serviceLabel(a.service)}${a.account ? " · " + esc(a.account) : (a.authenticated ? "" : " · signed out")}</div>`).join("");
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
        { service: t.service, id: t.id, meta: { title: t.title, artist: t.artist, album: t.album, art_url: t.artwork_url, duration_s: t.duration_s } });
      state.devicePaused = false;
      $("np-play").textContent = "⏸";
    } else {
      const r = await api(`/api/resolve?service=${encodeURIComponent(t.service)}&id=${encodeURIComponent(t.id)}`);
      audio.src = `/stream/${r.token}`;
      await audio.play();
    }
  } catch (e) {
    $("np-title").textContent = `Couldn't play: ${e.message}`;
  }
}

$("np-device").addEventListener("change", (e) => {
  const prev = state.target;
  state.target = e.target.value;
  if (prev === "browser" && onDevice()) audio.pause();       // handing off to a device
});

$("np-play").addEventListener("click", async () => {
  if (onDevice()) {
    if (state.index < 0) { if (state.queue.length) return playAt(0); return; }
    try { await apiPost(`/api/devices/${encodeURIComponent(state.target)}/${state.devicePaused ? "resume" : "pause"}`, {});
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
  if (onDevice()) { apiPost(`/api/devices/${encodeURIComponent(state.target)}/volume`, { level: Number($("np-vol").value) }).catch(() => {}); }
  else { audio.volume = $("np-vol").value / 100; }
});

async function loadDevices() {
  try {
    const devs = (await api("/api/devices")).devices || [];
    const sel = $("np-device");
    for (const d of devs) {
      const o = document.createElement("option");
      o.value = d.host; o.textContent = d.name;
      sel.appendChild(o);
    }
  } catch { /* no devices */ }
}

// -- wiring -----------------------------------------------------------------

$("search").addEventListener("submit", (e) => { e.preventDefault(); const q = $("search-input").value.trim(); if (q) doSearch(q); });
document.querySelectorAll("#nav li[data-view]").forEach((li) => li.addEventListener("click", () => setView(li.dataset.view)));
$("accounts").addEventListener("click", () => setView("accounts"));
loadAccounts();
loadDevices();
