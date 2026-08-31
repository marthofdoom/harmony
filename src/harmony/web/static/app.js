"use strict";
// Harmony web client. Talks to the same HTTP API the mobile app will use; audio
// plays in the browser via <audio> pointed at the same-origin /stream proxy.

const $ = (id) => document.getElementById(id);
const audio = $("audio");

const state = {
  queue: [],      // list of track objects currently loaded (search or playlist)
  index: -1,      // index of the playing track within queue
};

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

// -- rendering --------------------------------------------------------------

function renderTracks(tracks) {
  const list = $("list");
  if (!tracks.length) { list.innerHTML = `<p class="hint">No tracks.</p>`; return; }
  const rows = tracks.map((t, i) => `
    <div class="trow" data-i="${i}">
      <button class="play" title="Play">▶</button>
      <div class="title">${esc(t.title)}<span class="badge">${serviceLabel(t.service)}</span></div>
      <div class="artist">${esc(t.artist)}</div>
      <div class="dur">${fmtTime(t.duration_s)}</div>
    </div>`).join("");
  list.innerHTML = `<div class="tracks">${rows}</div>`;
  state.queue = tracks;
  list.querySelectorAll(".trow").forEach((row) => {
    row.querySelector(".play").addEventListener("click", () => playAt(Number(row.dataset.i)));
  });
  highlightPlaying();
}

function renderPlaylists(playlists) {
  const list = $("list");
  if (!playlists.length) { list.innerHTML = `<p class="hint">No playlists. Sign the server in to a service.</p>`; return; }
  list.innerHTML = `<div class="plgrid">${playlists.map((p) => `
    <div class="plcard" data-service="${esc(p.service)}" data-id="${esc(p.id)}">
      ${p.artwork_url ? `<img class="art" src="${esc(p.artwork_url)}" alt="" />` : `<div class="art"></div>`}
      <div class="t">${esc(p.title)}</div>
      <div class="s">${serviceLabel(p.service)} · ${p.track_count ?? "?"} tracks</div>
    </div>`).join("")}</div>`;
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
}

async function doSearch(q) {
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
  $("list").innerHTML = `<p class="hint">Loading playlists…</p>`;
  try { renderPlaylists((await api("/api/playlists")).playlists || []); }
  catch (e) { $("list").innerHTML = `<p class="hint">Couldn't load playlists: ${esc(e.message)}</p>`; }
}

async function openPlaylist(service, id, title) {
  $("view-title").textContent = title || "Playlist";
  $("list").innerHTML = `<p class="hint">Loading tracks…</p>`;
  try { renderTracks((await api(`/api/playlists/${encodeURIComponent(service)}/${encodeURIComponent(id)}/tracks`)).tracks || []); }
  catch (e) { $("list").innerHTML = `<p class="hint">Couldn't load tracks: ${esc(e.message)}</p>`; }
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
    const r = await api(`/api/resolve?service=${encodeURIComponent(t.service)}&id=${encodeURIComponent(t.id)}`);
    audio.src = `/stream/${r.token}`;
    await audio.play();
  } catch (e) {
    $("np-title").textContent = `Couldn't play: ${e.message}`;
  }
}

$("np-play").addEventListener("click", () => {
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
$("np-vol").addEventListener("input", () => { audio.volume = $("np-vol").value / 100; });

// -- wiring -----------------------------------------------------------------

$("search").addEventListener("submit", (e) => { e.preventDefault(); const q = $("search-input").value.trim(); if (q) doSearch(q); });
document.querySelectorAll("#nav li[data-view]").forEach((li) => li.addEventListener("click", () => setView(li.dataset.view)));
loadAccounts();
