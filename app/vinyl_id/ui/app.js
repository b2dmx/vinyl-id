// Vinyl ID library page. Plain JS; every URL is relative so it works through Home Assistant ingress.
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const I = {
  close: "M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z",
  album: "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 14.5c-2.49 0-4.5-2.01-4.5-4.5S9.51 7.5 12 7.5s4.5 2.01 4.5 4.5-2.01 4.5-4.5 4.5zm0-5.5c-.55 0-1 .45-1 1s.45 1 1 1 1-.45 1-1-.45-1-1-1z",
  image: "M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z",
  play: "M8 5v14l11-7z",
  stop: "M6 6h12v12H6z",
};
const icon = (p, s = "") => `<svg class="i ${s}" viewBox="0 0 24 24" aria-hidden="true"><path d="${p}"/></svg>`;

let status = null, albums = [], query = "", artBust = Date.now();

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.body && !(opts.body instanceof FormData)) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch("api/" + path, init);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}
const post = (p, body) => api(p, { method: "POST", body });

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), 3500);
}
const fail = (e) => toast(String(e.message || e).slice(0, 160));

const mmss = (s) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
// v changes whenever the cover does (the album list says so), so a stale cover is never shown
const artUrl = (id) => { const a = albums.find((x) => x.id === id); return `api/albums/${id}/art?v=${a && a.art_v ? a.art_v : artBust}`; };
const art = (id, has = true) => `<div class="art">${id && has ? `<img src="${artUrl(id)}" alt="" loading="lazy" onerror="this.remove()">` : icon(I.album)}</div>`;

// ---- dialogs ---------------------------------------------------------------
const dlg = $("#dlg");
function dialog(title, body, foot = "") {
  dlg.innerHTML = `<div class="dlg-head"><h2>${esc(title)}</h2>
    <button class="icon-btn" data-close aria-label="Close">${icon(I.close)}</button></div>
    <div class="dlg-body">${body}</div>${foot ? `<div class="dlg-foot">${foot}</div>` : ""}`;
  if (!dlg.open) dlg.showModal();
  dlg.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => dlg.close()));
  return dlg;
}
dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });   // click on the scrim

// ---- Now Playing -------------------------------------------------------------
function renderNow() {
  const s = status, n = s.now, el = $("#now");
  if (!n) {
    el.innerHTML = `<div class="now"><div class="art">${icon(I.album)}</div><div class="info">
      <h2 class="title">${s.playing ? "Listening..." : "Nothing playing"}</h2>
      <p class="sub">${s.playing ? (s.unknown ? "Shazam doesn't know this one." : "Working out what's playing.") : "Drop the needle and it shows up here."}</p>
      ${!s.playing && s.level <= -88 ? `<p class="muted">No sound is reaching Vinyl ID. Check that the record is sent to its silent speaker (Vinyl ID Listener) in Music Assistant.</p>` : ""}
      ${s.unknown ? `<div class="row"><button class="btn tonal" id="nameit">Name it</button></div>` : ""}</div></div>`;
    if (s.unknown) $("#nameit").onclick = () => nameDialog(null);
    return;
  }
  const side = n.side ? `<span class="chip tonal">Side ${esc(n.side)} · ${n.track_number} of ${n.side_tracks}</span>` : "";
  const src = { local: "From library", shazam: "Shazam", manual: "Named by you", spin: "Album track list", unknown: "Unknown" }[n.source] || n.source;
  el.innerHTML = `<div class="now">${art(n.album_id)}<div class="info">
    <div class="row"><span class="chip good">Playing</span><span class="chip">${esc(src)}</span>${side}</div>
    <h2 class="title">${esc(n.title)}</h2>
    <p class="sub">${esc([n.artist, n.album].filter(Boolean).join(" · "))}</p>
    ${n.track_length ? `<div><div class="meter" id="meter"><span style="width:0"></span></div>
      <div class="times"><span id="t-now"></span><span>${mmss(n.track_length)}</span></div></div>` : ""}
    <div class="row"><button class="btn tonal" id="nameit">${n.source === "unknown" ? "Name it" : "Not right?"}</button></div>
  </div></div>`;
  $("#nameit").onclick = () => nameDialog(n);
  tickProgress();
}

function tickProgress() {
  const n = status && status.now, m = $("#meter");
  if (!n || !m || !n.track_length) return;
  if (!n.track_started_at) { m.classList.add("indeterminate"); $("#t-now").textContent = "Position unknown until the next song"; return; }
  m.classList.remove("indeterminate");
  const done = (Date.now() - Date.parse(n.track_started_at)) / 1000;
  const pct = Math.min(100, Math.max(0, (done / n.track_length) * 100));
  m.firstElementChild.style.width = pct + "%";
  $("#t-now").textContent = mmss(done);
}
setInterval(tickProgress, 1000);

function nameDialog(n) {
  dialog(n && n.source !== "unknown" ? "What is this really?" : "Name this song", `
    <p class="muted">Vinyl ID learns it from what it just heard, so next time it knows.</p>
    <label class="field"><span>Song</span><input id="f-title" value="${esc(n && n.source !== "unknown" ? n.title : "")}" autocomplete="off"></label>
    <label class="field"><span>Artist</span><input id="f-artist" value="${esc(n ? n.artist : "")}" autocomplete="off"></label>
    <label class="field"><span>Album</span><input id="f-album" value="${esc(n ? n.album : "")}" autocomplete="off"></label>`,
    `<button class="btn text" data-close>Cancel</button><button class="btn filled" id="save">Save</button>`);
  $("#save").onclick = async () => {
    const title = $("#f-title").value.trim(), artist = $("#f-artist").value.trim();
    if (!title || !artist) return toast("Song and artist are needed");
    try {
      await post("now/label", { title, artist, album: $("#f-album").value.trim() });
      dlg.close(); refresh();
    } catch (e) { fail(e); }
  };
}

// ---- Now spinning --------------------------------------------------------------
function renderSpin() {
  const sp = status.spin, el = $("#spin");
  if (!sp) {
    el.innerHTML = `<div class="card row" style="justify-content:space-between"><div><h3>Now spinning</h3>
      <p class="muted">Pick an album to follow its track list, even without a connection.</p></div>
      <button class="btn tonal" id="choose">Choose album</button></div>`;
    $("#choose").onclick = chooseAlbum;
    return;
  }
  let side = "";
  const rows = sp.tracks.map((t, i) => {
    const head = t.side !== side ? `<div class="side-label">${t.disc > 1 || sp.tracks.some((x) => x.disc > 1) ? `LP ${t.disc} · ` : ""}Side ${esc(t.side)}</div>` : "";
    side = t.side;
    return `${head}<div class="trk ${i === sp.index ? "now-on" : ""}"><span>${esc(t.side)}${t.pos}</span>
      <span>${esc(t.title)}</span><span class="len">${t.length ? mmss(t.length) : ""}</span>
      <button class="icon-btn" data-go="${esc(t.side)}|${t.pos}" aria-label="Start here">${icon(I.play)}</button></div>`;
  }).join("");
  el.innerHTML = `<div class="card" style="display:grid;gap:12px"><div class="section-head"><div><h3>Now spinning</h3>
    <p class="muted">${esc(sp.album)} · ${esc(sp.artist)}${sp.auto ? " · found by itself" : ""}</p></div>
    <button class="btn tonal" id="unspin">Stop following</button></div>
    ${sp.mismatch ? `<div class="notice"><b>${esc(sp.mismatch.title)}</b> by ${esc(sp.mismatch.artist)} isn't on this album.
      <div class="row"><button class="btn tonal sm" id="unspin2">It's a different record</button></div></div>` : ""}
    <div class="list" style="gap:2px">${rows}</div></div>`;
  for (const id of ["unspin", "unspin2"]) if ($("#" + id)) $("#" + id).onclick = () => api("spin", { method: "DELETE" }).then(refresh, fail);
  el.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => {
    const [sd, pos] = b.dataset.go.split("|");
    post("spin", { album_id: sp.album_id, side: sd, pos: Number(pos) }).then(refresh, fail);
  }));
}

function chooseAlbum() {
  const withTracks = albums.filter((a) => a.tracks > 0);
  dialog("Choose album", withTracks.length
    ? `<div class="list">${withTracks.map((a) => `<button class="pick" data-id="${a.id}"><span><b>${esc(a.album)}</b><div class="sub">${esc(a.artist)}</div></span></button>`).join("")}</div>`
    : `<p class="muted">None of your albums has a track list yet. Add one with <b>Add album</b>.</p>`);
  dlg.querySelectorAll(".pick").forEach((b) => (b.onclick = () => albumDialog(Number(b.dataset.id))));
}

// ---- Albums ------------------------------------------------------------------------
function renderAlbums() {
  $("#empty").hidden = albums.length > 0 || query !== "";
  $("#albums").innerHTML = albums.map((a) => `<button class="tile" data-id="${a.id}">${art(a.id, a.has_art)}
    <div class="t">${esc(a.album)}</div><div class="a">${esc(a.artist)}</div>
    <div class="chips">${a.tracks ? `<span class="chip tonal">${a.tracks} tracks</span>` : ""}${a.songs ? `<span class="chip">${a.songs} learned</span>` : ""}</div></button>`).join("");
  $("#albums").querySelectorAll(".tile").forEach((b) => (b.onclick = () => albumDialog(Number(b.dataset.id))));
}

async function loadAlbums() {
  try { albums = await api("albums?q=" + encodeURIComponent(query)); renderAlbums(); } catch (e) { fail(e); }
}

async function albumDialog(id) {
  let tracks = [], a = albums.find((x) => x.id === id);
  let discArt = [];
  try { tracks = await api(`albums/${id}/tracks`); discArt = await api(`albums/${id}/discs`); if (!a) a = (await api("albums")).find((x) => x.id === id); } catch (e) { return fail(e); }
  let side = "";
  const multi = tracks.some((t) => t.disc > 1);
  const discs = multi ? [...new Set(tracks.map((t) => t.disc))] : [];
  const rows = tracks.map((t) => {
    const head = t.side !== side ? `<div class="side-label">${multi ? `LP ${t.disc} · ` : ""}Side ${esc(t.side)}</div>` : "";
    side = t.side;
    return `${head}<div class="trk"><span>${esc(t.side)}${t.pos}</span><span>${esc(t.title)}</span>
      <span class="len">${t.length ? mmss(t.length) : ""}</span>
      <button class="icon-btn" data-go="${esc(t.side)}|${t.pos}" aria-label="Spin from here">${icon(I.play)}</button></div>`;
  }).join("");
  dialog(a ? a.album : "Album", `
    <div class="cover-row">${art(id, a && a.has_art)}<div style="display:grid;gap:12px">
      <label class="field"><span>Album</span><input id="f-album" value="${esc(a && a.album)}"></label>
      <label class="field"><span>Artist</span><input id="f-artist" value="${esc(a && a.artist)}"></label></div></div>
    <div class="row"><button class="btn tonal" id="cover">Change cover</button>
      <button class="btn tonal" id="find">${tracks.length ? "Pick another pressing" : "Find track list"}</button>
      ${tracks.length ? `<button class="btn tonal" id="lengths">Track lengths</button>` : ""}
      ${a && a.songs ? `<button class="btn tonal" id="songs">Learned songs</button>` : ""}</div>
    ${discs.length ? `<div><h3>A cover for each LP</h3><p class="muted">Pick an LP to add its sleeve. Then choose which cover shows while this album plays.</p>
      <div class="row"><button class="btn ${a && a.lp_covers ? "text" : "tonal"}" data-lp="0">Album cover</button><button class="btn ${a && a.lp_covers ? "tonal" : "text"}" data-lp="1">Each LP's cover</button></div>
      <div class="row">${discs.map((d) => `<button class="btn ${discArt.includes(d) ? "tonal" : "text"}" data-disc="${d}">LP ${d}${discArt.includes(d) ? " · set" : ""}</button>`).join("")}</div>
      <input type="file" id="disc-file" accept="image/*" hidden></div>` : ""}
    ${tracks.length ? `<div><h3>Tracks</h3><div class="list" style="gap:2px">${rows}</div></div>` : `<p class="muted">No track list yet. Find the pressing to follow it side by side.</p>`}`,
    `<button class="btn text" data-close>Close</button>${tracks.length ? `<button class="btn tonal" id="spinit">${icon(I.play, "s")}Spin this album</button>` : ""}<button class="btn filled" id="save">Save</button>`);
  $("#save").onclick = async () => {
    try {
      await api(`albums/${id}`, { method: "PATCH", body: { artist: $("#f-artist").value, album: $("#f-album").value } });
      dlg.close(); await loadAlbums(); refresh();
    } catch (e) { fail(e); }
  };
  $("#cover").onclick = () => coverDialog(id, a);
  $("#find").onclick = () => addAlbumDialog(id, a ? `${a.artist} ${a.album}` : "");
  if ($("#lengths")) $("#lengths").onclick = () => lengthsDialog(id, tracks);
  if ($("#songs")) $("#songs").onclick = () => songsDialog(id);
  let disc = 0;
  dlg.querySelectorAll("[data-lp]").forEach((b) => (b.onclick = async () => {
    try { await api(`albums/${id}/lp-covers`, { method: "PUT", body: { on: b.dataset.lp === "1" } }); await loadAlbums(); refresh(); albumDialog(id); } catch (er) { fail(er); }
  }));
  dlg.querySelectorAll("[data-disc]").forEach((b) => (b.onclick = () => { disc = Number(b.dataset.disc); $("#disc-file").click(); }));
  if ($("#disc-file")) $("#disc-file").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try { await api(`albums/${id}/discs/${disc}/art`, { method: "POST", body: fd }); toast(`Cover saved for LP ${disc}`); refresh(); albumDialog(id); } catch (er) { fail(er); }
  };
  const spinTo = (sd, pos) => post("spin", { album_id: id, side: sd, pos }).then(() => { dlg.close(); refresh(); }, fail);
  if ($("#spinit")) $("#spinit").onclick = () => spinTo("", 1);
  dlg.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => { const [sd, p] = b.dataset.go.split("|"); spinTo(sd, Number(p)); }));
}

// How long each track is: the progress bar and songs that run together both need it.
function lengthsDialog(id, tracks) {
  let side = "";
  const rows = tracks.map((t, i) => {
    const head = t.side !== side ? `<div class="side-label">Side ${esc(t.side)}</div>` : "";
    side = t.side;
    return `${head}<div class="trk"><span>${esc(t.side)}${t.pos}</span><span>${esc(t.title)}</span>
      <input class="len-in" data-i="${i}" value="${t.length ? mmss(t.length) : ""}" placeholder="m:ss" inputmode="numeric" aria-label="Length of ${esc(t.title)}"><span></span></div>`;
  }).join("");
  dialog("Track lengths", `<p class="muted">Minutes and seconds, as printed on the sleeve (for example 3:41). Leave a track empty if you don't know it.</p>
    <div class="list" style="gap:2px">${rows}</div>`,
    `<button class="btn text" id="back">Back</button><button class="btn filled" id="save">Save</button>`);
  $("#back").onclick = () => albumDialog(id);
  $("#save").onclick = async () => {
    const lengths = {};
    let bad = "";
    dlg.querySelectorAll(".len-in").forEach((inp) => {
      const t = tracks[Number(inp.dataset.i)], v = inp.value.trim();
      if (!v || v === (t.length ? mmss(t.length) : "")) return;
      if (!/^\d{1,2}:[0-5]\d$/.test(v)) { bad = `${t.side}${t.pos}`; return; }
      lengths[`${t.side}|${t.pos}`] = v;
    });
    if (bad) return toast(`${bad}: write the length as minutes:seconds, like 3:41`);
    if (!Object.keys(lengths).length) return albumDialog(id);
    try {
      const r = await api(`albums/${id}/tracks`, { method: "PATCH", body: { lengths } });
      toast(r.not_on_album.length ? `${r.set} saved, ${r.not_on_album.length} could not be matched to a track` : "Lengths saved");
      refresh(); albumDialog(id);
    } catch (e) { fail(e); }
  };
}

// The songs Vinyl ID has learned from this album: fix a name, or forget one.
async function songsDialog(id) {
  let songs = [];
  try { songs = await api(`albums/${id}/songs`); } catch (e) { return fail(e); }
  dialog("Learned songs", songs.length ? `<p class="muted">Forgetting a song removes what Vinyl ID learned of it. It is learned again the next time it plays.</p>
    <div class="list">${songs.map((s) => `<div class="song-row" data-id="${s.id}">
      <label class="field"><span>Song</span><input class="s-title" value="${esc(s.title)}"></label>
      <label class="field"><span>Artist</span><input class="s-artist" value="${esc(s.artist)}"></label>
      <div class="row"><button class="btn tonal sm s-save">Save</button><button class="btn text sm s-forget">Forget</button></div></div>`).join("")}</div>`
    : `<p class="muted">Nothing learned from this album yet.</p>`,
    `<button class="btn text" id="back">Back</button>`);
  $("#back").onclick = () => albumDialog(id);
  dlg.querySelectorAll(".song-row").forEach((row) => {
    const sid = row.dataset.id;
    row.querySelector(".s-save").onclick = async () => {
      const title = row.querySelector(".s-title").value.trim(), artist = row.querySelector(".s-artist").value.trim();
      if (!title || !artist) return toast("Song and artist are needed");
      try { await api(`songs/${sid}`, { method: "PATCH", body: { title, artist } }); toast("Saved"); refresh(); } catch (e) { fail(e); }
    };
    row.querySelector(".s-forget").onclick = async () => {
      try { await api(`songs/${sid}`, { method: "DELETE" }); toast("Forgotten"); await loadAlbums(); loadLeftovers(); refresh(); albums.some((x) => x.id === id) ? songsDialog(id) : dlg.close(); } catch (e) { fail(e); }
    };
  });
}

// ---- Covers of removed albums ------------------------------------------------------------
async function loadLeftovers() {
  let left = [];
  try { left = await api("leftovers"); } catch (e) { return; }
  const el = $("#leftovers");
  if (!el) return;
  el.hidden = !left.length;
  if (!left.length) return;
  el.innerHTML = `<div class="section-head"><h2>Covers of removed albums</h2></div>
    <p class="muted" style="margin:8px 0 16px">When an album goes, its covers are kept here for 30 days in case you want them back. Then they are deleted.</p>
    <div class="grid">${left.map((c) => `<div class="tile left" data-id="${c.id}"><div class="art"><img src="api/leftovers/${c.id}/art" alt="" loading="lazy"></div>
      <div class="t">${esc(c.album)}${c.disc ? ` · LP ${c.disc}` : ""}</div><div class="a">${esc(c.artist)}</div>
      <div class="chips"><span class="chip">${c.days_left} ${c.days_left === 1 ? "day" : "days"} left</span></div>
      <div class="row"><button class="btn tonal sm l-use">Put on an album</button><button class="btn text sm l-del">Delete</button></div></div>`).join("")}</div>`;
  el.querySelectorAll(".left").forEach((tile) => {
    const lid = tile.dataset.id;
    tile.querySelector(".l-del").onclick = () => api(`leftovers/${lid}`, { method: "DELETE" }).then(loadLeftovers, fail);
    tile.querySelector(".l-use").onclick = async () => {
      let all = [];
      try { all = await api("albums"); } catch (e) { return fail(e); }     // every album, whatever is typed in the search
      dialog("Put this cover on", all.length
        ? `<p class="muted">If that album already has a cover, the old one moves here for 30 days.</p><div class="list">${all.map((a) => `<button class="pick" data-id="${a.id}"><span><b>${esc(a.album)}</b><div class="sub">${esc(a.artist)}</div></span></button>`).join("")}</div>`
        : `<p class="muted">You have no albums to put it on.</p>`);
      dlg.querySelectorAll(".pick").forEach((b) => (b.onclick = async () => {
        try { await post(`leftovers/${lid}/restore`, { album_id: Number(b.dataset.id) }); artBust = Date.now(); dlg.close(); toast("Cover put back"); await loadAlbums(); loadLeftovers(); refresh(); } catch (e) { fail(e); }
      }));
    };
  });
}

async function coverDialog(id, a) {
  dialog("Change cover", `<div class="row"><button class="btn tonal" id="upload">${icon(I.image, "s")}Upload your own</button>
    <input type="file" id="file" accept="image/*" hidden></div>
    <div id="covers" class="muted">Looking for covers...</div>`, `<button class="btn text" id="back">Back</button>`);
  $("#back").onclick = () => albumDialog(id);
  $("#upload").onclick = () => $("#file").click();
  $("#file").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try { await api(`albums/${id}/art`, { method: "POST", body: fd }); done(); } catch (er) { fail(er); }
  };
  const done = async () => { artBust = Date.now(); toast("Cover saved. It stays as you set it."); await loadAlbums(); refresh(); albumDialog(id); };
  try {
    const found = await api("art-search?q=" + encodeURIComponent(a ? `${a.artist} ${a.album}` : ""));
    $("#covers").className = "covers";
    $("#covers").innerHTML = found.length ? found.map((c, i) => `<button data-i="${i}" title="${esc(c.album)} · ${esc(c.artist)}"><img src="${esc(c.art)}" alt="${esc(c.album)}"></button>`).join("") : "No covers found. Try uploading your own.";
    $("#covers").querySelectorAll("button").forEach((b) => (b.onclick = async () => {
      try { await post(`albums/${id}/art`, { url: found[Number(b.dataset.i)].art }); done(); } catch (er) { fail(er); }
    }));
  } catch (e) { $("#covers").textContent = "Couldn't search for covers right now."; }
}

// ---- Add album (MusicBrainz) ------------------------------------------------------------
function addAlbumDialog(forId = null, preset = "") {
  dialog(forId ? "Find track list" : "Add album", `
    <label class="field"><span>Artist and album</span><input id="f-q" value="${esc(preset)}" placeholder="e.g. artist name album title" autocomplete="off"></label>
    <div id="res" class="muted">Search MusicBrainz and pick your exact pressing.</div>`,
    `<button class="btn text" id="manual">Add by name only</button><button class="btn filled" id="go">Search</button>`);
  const input = $("#f-q");
  const search = async () => {
    const q = input.value.trim(); if (!q) return;
    $("#res").textContent = "Searching...";
    try {
      const r = await api("mb/search?q=" + encodeURIComponent(q));
      $("#res").className = "";
      $("#res").innerHTML = r.length ? `<div class="list">${r.map((x, i) => `<button class="pick" data-i="${i}"><span><b>${esc(x.album)}</b>
        <div class="sub">${esc(x.artist)} · ${esc([x.year, x.country, x.format, x.tracks ? x.tracks + " tracks" : ""].filter(Boolean).join(" · "))}${x.label ? " · " + esc(x.label) : ""}</div></span></button>`).join("")}</div>` : `<p class="muted">Nothing found. Try fewer words.</p>`;
      $("#res").querySelectorAll(".pick").forEach((b) => (b.onclick = async () => {
        b.disabled = true; b.querySelector(".sub").textContent = "Adding...";
        try {
          const added = await post("albums/from-mb", { mbid: r[Number(b.dataset.i)].mbid, album_id: forId });
          artBust = Date.now(); toast(`Added with ${added.tracks} tracks`);
          await loadAlbums(); albumDialog(added.id);
        } catch (e) { b.disabled = false; fail(e); }
      }));
    } catch (e) { $("#res").textContent = "MusicBrainz didn't answer. Try again in a moment."; }
  };
  $("#go").onclick = search;
  input.onkeydown = (e) => { if (e.key === "Enter") search(); };
  $("#manual").hidden = !!forId;
  $("#manual").onclick = () => {
    const [artist, album] = input.value.split(/\s+[-–]\s+/);
    if (!album) return toast("Type it as: Artist - Album");
    post("albums", { artist: artist.trim(), album: album.trim() }).then(async (r) => { await loadAlbums(); albumDialog(r.id); }, fail);
  };
  input.focus();
}

// ---- wiring -------------------------------------------------------------------------------
// what the Now Playing and Now spinning cards are drawn from
const sig = (s) => JSON.stringify([s.now && [s.now.id, s.now.title, s.now.track_started_at, s.now.source, s.now.spin_mismatch], s.playing, s.unknown, !s.playing && s.level <= -88, s.spin && [s.spin.index, s.spin.album_id, s.spin.mismatch]]);

async function refresh() {
  try {
    const s = await api("status");
    const changed = !status || sig(s) !== sig(status);
    const grew = status && s.library !== status.library;
    status = s;
    $("#sig").textContent = s.signature;
    $("#ai").textContent = s.ai_note;
    if (s.library_mb !== undefined && $("#size")) {
      const size = s.library_mb < 10 ? s.library_mb.toFixed(1) : Math.round(s.library_mb);
      $("#size").textContent = s.library_full
        ? `The library is full (${size} of ${s.library_limit_mb} MB): new songs are not learned. Forget songs you don't need, or raise the limit on the app's Configuration tab.`
        : `Library: ${s.library} ${s.library === 1 ? "song" : "songs"}, ${size} MB${s.library_limit_mb ? ` of ${s.library_limit_mb} MB` : ""}`;
      if ($("#lvl")) $("#lvl").textContent = `Input level ${s.level} dB. A record starts at ${s.start_level} dB.`;
      $("#size").classList.toggle("notice", !!s.library_full);
    }
    if (changed || !$("#now").firstChild) { renderNow(); renderSpin(); }
    if (grew) loadAlbums();   // a new record was learned: show it without a reload
  } catch (e) { /* the server restarting: try again next tick */ }
}

fetch("ui/logo.svg").then((r) => r.text()).then((t) => {
  const svg = t.slice(t.indexOf("<svg"));
  $("#logo").innerHTML = svg; $("#logo2").innerHTML = svg;
});
$("#add-album").onclick = () => addAlbumDialog();
let qTimer;
$("#q").oninput = (e) => { query = e.target.value.trim(); clearTimeout(qTimer); qTimer = setTimeout(loadAlbums, 200); };
refresh(); loadAlbums(); loadLeftovers();
setInterval(refresh, 3000);
