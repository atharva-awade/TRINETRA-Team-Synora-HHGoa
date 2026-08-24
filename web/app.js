/* VERIFIED — front-end controller (vanilla JS, no build step) */
(() => {
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; };
  const fmt = (n, d = 3) => (typeof n === "number" ? n.toFixed(d) : "–");
  const short = (h, n = 10) => (h && h.length > 2 * n + 2 ? `${h.slice(0, n + 2)}…${h.slice(-n)}` : h || "–");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = { stream: null, previewTimer: null, session: Math.random().toString(36).slice(2), runId: null, es: null, matches: [], selected: null, source: "webcam", uploadBlob: null, livenessPassed: false, lastFace: null, anchored: false };

  // ------------------------------------------------------------ status
  async function loadStatus() {
    try {
      const s = await (await fetch("/api/status")).json();
      const chain = $("#pill-chain");
      chain.className = "pill " + (s.rpc ? "ok" : "bad");
      chain.textContent = s.rpc ? `${s.chain} · block ${s.block}${s.contract ? " · registry " + short(s.contract, 4) : " · registry: deploy on first anchor"}${s.records != null ? " · " + s.records + " records" : ""}` : `${s.chain} · RPC unreachable`;
      const w = $("#pill-wallet");
      if (s.wallet) { w.className = "pill " + (s.balance_eth > 0.002 ? "ok" : "warn"); w.textContent = `${short(s.wallet, 4)} · ${s.balance_eth.toFixed(4)} ETH`; }
      else { w.className = "pill bad"; w.textContent = "no wallet — run: verified wallet new"; }
      const on = Object.entries(s.engines).filter(([, v]) => v).map(([k]) => k.replace("google_", "g-"));
      const e = $("#pill-engines");
      e.className = "pill " + (s.engines.google_lens ? "ok" : "warn");
      e.textContent = `engines: ${on.join(" · ") || "none (set SERPAPI_KEY)"}${s.ipfs ? " · ipfs" : ""}${s.eas ? " · eas" : ""}${s.ots ? " · ots" : ""}`;
    } catch (err) { $("#pill-chain").textContent = "backend offline"; }
  }

  // ------------------------------------------------------------ camera
  const video = $("#video"), overlay = $("#overlay"), ctx = overlay.getContext("2d");
  async function startCam() {
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 800 }, facingMode: "user" }, audio: false });
    } catch (e) { alert("Camera unavailable: " + e.message + "\nUse 'Upload photo' instead."); return; }
    video.srcObject = state.stream;
    state.source = "webcam";
    $(".cam-wrap").classList.remove("upload");
    $("#cam-idle").hidden = true; $("#cam-hud").hidden = false; $("#btn-stop").hidden = false;
    await fetch("/api/preview/reset", { method: "POST", body: new URLSearchParams({ session: state.session }) });
    state.livenessPassed = false;
    video.onloadedmetadata = () => { overlay.width = video.videoWidth; overlay.height = video.videoHeight; previewLoop(); };
  }
  function stopCam() {
    if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
    state.stream = null; clearTimeout(state.previewTimer);
    $("#cam-idle").hidden = false; $("#cam-hud").hidden = true; $("#btn-stop").hidden = true; $("#btn-capture").disabled = true;
    ctx.clearRect(0, 0, overlay.width, overlay.height);
  }
  function grabFrame(maxW) {
    const c = document.createElement("canvas");
    const s = Math.min(1, maxW / video.videoWidth);
    c.width = Math.round(video.videoWidth * s); c.height = Math.round(video.videoHeight * s);
    c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
    return new Promise((res) => c.toBlob(res, "image/jpeg", 0.92));
  }
  async function previewLoop() {
    if (!state.stream) return;
    try {
      const blob = await grabFrame(480);
      const fd = new FormData(); fd.append("frame", blob, "f.jpg"); fd.append("session", state.session);
      const r = await (await fetch("/api/preview", { method: "POST", body: fd })).json();
      drawPreview(r);
    } catch (e) { /* ignore transient */ }
    state.previewTimer = setTimeout(previewLoop, 120);
  }
  function drawPreview(r) {
    const sx = overlay.width / r.size[0], sy = overlay.height / r.size[1];
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    const f = r.faces[0];
    const live = $("#live"); live.classList.toggle("passed", r.liveness.passed);
    live.querySelector(".l").classList.toggle("on", r.liveness.left); live.querySelector(".r").classList.toggle("on", r.liveness.right);
    state.livenessPassed = r.liveness.passed;
    if (!f) { $("#hint").textContent = "looking for a face…"; $("#btn-capture").disabled = true; state.lastFace = null; return; }
    state.lastFace = f;
    const [x1, y1, x2, y2] = f.bbox.map((v, i) => v * (i % 2 ? sy : sx));
    const q = f.quality.overall; const good = q >= 0.55;
    const col = r.liveness.passed ? (good ? "#35e29a" : "#ffc857") : "#ff8a3d";
    ctx.lineWidth = 3; ctx.strokeStyle = col; ctx.shadowColor = col; ctx.shadowBlur = 18;
    const w = x2 - x1, h = y2 - y1, L = Math.min(w, h) * 0.22;
    [[x1, y1, 1, 1], [x2, y1, -1, 1], [x1, y2, 1, -1], [x2, y2, -1, -1]].forEach(([cx, cy, dx, dy]) => { ctx.beginPath(); ctx.moveTo(cx, cy + dy * L); ctx.lineTo(cx, cy); ctx.lineTo(cx + dx * L, cy); ctx.stroke(); });
    ctx.shadowBlur = 0; ctx.fillStyle = "#38e0ff";
    f.kps.forEach(([px, py]) => { ctx.beginPath(); ctx.arc(px * sx, py * sy, 3, 0, Math.PI * 2); ctx.fill(); });
    // yaw indicator
    ctx.fillStyle = "rgba(0,0,0,.55)"; ctx.fillRect(x1, y2 + 8, w, 6);
    ctx.fillStyle = col; ctx.fillRect(x1 + w / 2 + Math.max(-1, Math.min(1, f.yaw_ratio * 2)) * (w / 2 - 6), y2 + 8, 12, 6);
    $("#q-bar").style.width = `${q * 100}%`; $("#q-val").textContent = q.toFixed(2);
    $("#q-sharp").style.width = `${f.quality.sharpness * 100}%`; $("#q-size").style.width = `${f.quality.size * 100}%`; $("#q-front").style.width = `${f.quality.frontal * 100}%`;
    const hints = [...f.hints]; if (!r.liveness.passed) hints.unshift("liveness: turn your head left, then right");
    if (f.others) hints.push(`${f.others} other face(s) in frame — largest is used`);
    $("#hint").textContent = hints.join(" · ") || "ready — hold still and capture";
    $("#btn-capture").disabled = !(good && r.liveness.passed);
  }

  // ------------------------------------------------------------ upload
  function handleFile(file) {
    if (!file) return;
    state.uploadBlob = file; state.source = "upload";
    stopCam();
    const url = URL.createObjectURL(file);
    $(".cam-wrap").classList.add("upload");
    video.srcObject = null; video.src = url; video.poster = url; video.load();
    $("#cam-idle").hidden = true; $("#cam-hud").hidden = true; $("#btn-capture").disabled = false;
    // show the image on the overlay canvas
    const img = new Image();
    img.onload = () => { overlay.width = img.width; overlay.height = img.height; ctx.drawImage(img, 0, 0); };
    img.src = url;
  }

  // ------------------------------------------------------------ run
  async function capture() {
    let blob;
    if (state.source === "webcam") { blob = await grabFrame(1280); stopCam(); const img = new Image(); img.onload = () => { overlay.width = img.width; overlay.height = img.height; ctx.drawImage(img, 0, 0); }; img.src = URL.createObjectURL(blob); }
    else blob = state.uploadBlob;
    resetRunUI();
    const fd = new FormData(); fd.append("image", blob, "query.jpg"); fd.append("source", state.source); fd.append("auto_anchor", $("#auto-anchor").checked ? "true" : "false");
    $("#btn-capture").disabled = true;
    const { run_id } = await (await fetch("/api/scan", { method: "POST", body: fd })).json();
    state.runId = run_id; $("#run-id").textContent = run_id;
    subscribe(run_id);
  }
  function resetRunUI() {
    document.querySelectorAll(".step").forEach((s) => (s.className = "step"));
    $("#panel-search").hidden = true; $("#panel-chain").hidden = true; $("#face-card").hidden = true; $("#identity-card").hidden = true;
    $("#cands").innerHTML = ""; $("#matches").innerHTML = '<div class="empty">candidates stream in as engines respond…</div>'; $("#engines").innerHTML = ""; $("#log").innerHTML = ""; $("#checks").innerHTML = "";
    $("#verify-bar").style.width = "0%"; $("#verify-count").textContent = "0 / 0"; $("#match-count").textContent = ""; $("#search-sub").textContent = "";
    $("#tamper-note").hidden = true; $("#verdict").className = "verdict"; $("#verdict").textContent = "…";
    state.matches = []; state.selected = null; state.anchored = false; state.candSeen = 0; state.candTotal = 0;
  }
  function subscribe(runId, since) {
    if (state.es) state.es.close();
    const es = new EventSource(`/api/events/${runId}${since ? "?since=" + since : ""}`); state.es = es;
    const on = (ev, fn) => es.addEventListener(ev, (e) => { const d = JSON.parse(e.data); log(ev, d); try { fn(d); } catch (err) { console.error(ev, err); } });
    on("run.start", () => {});
    on("stage", (d) => { const s = document.querySelector(`.step[data-stage="${d.stage}"]`); if (s) s.className = `step ${d.status}`; if (d.stage === "search" && d.status === "running") $("#panel-search").hidden = false; if (d.stage === "evidence" && d.status === "running") $("#panel-chain").hidden = false; });
    on("face.detected", onFace);
    on("search.prepare", () => {});
    on("search.hosted", (d) => { $("#search-sub").textContent = `query crop hosted on ${d.host} (expires ${d.ttl})`; });
    on("search.fanout", (d) => { d.engines.forEach((e) => chip(e, "run", `${e} …`)); });
    on("search.engine", (d) => { if (d.error) chip(d.engine, "err", `${d.engine} ✕`); else chip(d.engine, "ok", `${d.engine} ${d.candidates}${d.query ? " · “" + d.query + "”" : ""}`); });
    on("search.verify.start", (d) => { state.candTotal = (state.candTotal || 0) + d.shortlist; $("#verify-count").textContent = `${state.candSeen || 0} / ${state.candTotal}`; });
    on("search.verify.progress", onCandidate);
    on("search.identity", (d) => { if (d.names && d.names.length) { $("#identity-card").hidden = false; $("#names").innerHTML = d.names.map((n) => `<span>${esc(n)}</span>`).join(""); } });
    on("search.warning", () => {});
    on("search.done", onSearchDone);
    on("anchor.selected", (d) => { state.selected = d.index; renderMatches(); renderSelected(d.match); });
    on("evidence.built", (d) => { $("#bundle-card").hidden = false; $("#bundle").textContent = JSON.stringify(d.bundle, null, 1); $("#bundle-meta").textContent = `${d.leaf_count} Merkle leaves · ${d.canonical_size} bytes · keccak ${short(d.record_hash, 6)}`; hashRow("recordHash", d.record_hash); hashRow("merkleRoot", d.merkle_root); });
    on("ipfs.done", (d) => { hashRow("evidence CID", d.cid_on_chain + (d.pinned ? " (pinned · Pinata)" : " (local CIDv1)")); if (d.gateway_url) link("IPFS bundle", d.gateway_url); });
    on("chain.wallet", (d) => { hashRow("submitter", `${d.address} · ${d.balance_eth} ETH`); });
    on("chain.log", () => {});
    on("chain.deployed", (d) => { link("Registry contract", d.explorer); });
    on("chain.anchored", onAnchored);
    on("eas.done", (d) => { if (d.attestation_uid) { hashRow("EAS attestation", d.attestation_uid); link("EAS attestation", d.explorer_attestation); link("EAS schema", d.explorer_schema); } });
    on("ots.done", (d) => { if (d.calendars) hashRow("Bitcoin (OTS)", `sha256 ${short(d.sha256, 8)} → ${d.calendars.length} calendars · pending confirmation`); });
    on("verify.start", (d) => { $("#checks").innerHTML = ""; $("#verdict").className = "verdict"; $("#verdict").textContent = "checking…"; if (d.tamper) { $("#tamper-note").hidden = false; $("#tamper-note").textContent = `TAMPER TEST: ${d.tamper.field} changed ${JSON.stringify(d.tamper.old)} → ${JSON.stringify(d.tamper.new)} in the local bundle. Re-running every check against the immutable on-chain record…`; } else $("#tamper-note").hidden = true; });
    on("verify.check", onCheck);
    on("verify.done", (d) => { const v = $("#verdict"); v.className = "verdict " + (d.verdict === "VERIFIED" ? "ok" : "bad"); v.textContent = d.verdict; });
    on("run.done", (d) => { es.close(); loadLedger(); if (d.status === "no_match") noMatch(d); if (d.status === "no_face") $("#hint").textContent = "no face detected — try again"; $("#btn-capture").disabled = false; });
    on("run.error", (d) => { es.close(); logErr(d.message); $("#btn-capture").disabled = false; alert("Pipeline error:\n" + d.message); });
  }

  // ------------------------------------------------------------ renderers
  function onFace(d) {
    $("#face-card").hidden = false; $("#face-crop").src = d.face_crop_b64;
    $("#f-det").textContent = fmt(d.det_score); $("#f-q").textContent = fmt(d.quality.overall, 2); $("#f-ag").textContent = `${d.age ?? "?"} · ${d.gender ?? "?"}`; $("#f-n").textContent = d.faces_in_frame; $("#f-commit").textContent = d.commitment;
    if (d.annotated_b64) { const img = new Image(); img.onload = () => { overlay.width = img.width; overlay.height = img.height; ctx.drawImage(img, 0, 0); }; img.src = d.annotated_b64; $(".cam-wrap").classList.add("upload"); }
  }
  function chip(id, cls, text) {
    let c = document.getElementById("chip-" + id); if (!c) { c = el("span", "chip"); c.id = "chip-" + id; $("#engines").appendChild(c); }
    c.className = "chip " + cls; c.textContent = text;
  }
  function onCandidate(d) {
    state.candSeen = d.done; $("#verify-count").textContent = `${d.done} / ${state.candTotal || d.total}`; $("#verify-bar").style.width = `${(d.done / (state.candTotal || d.total)) * 100}%`;
    if (d.similarity === undefined) return;
    const c = el("div", `cand ${d.band}${d.band === "reject" ? " reject" : ""}`);
    c.title = `${d.title || ""}\n${d.link}`;
    c.innerHTML = `<img src="${esc(d.face_crop_b64)}" alt=""/><span class="plat">${esc(d.platform || "web")}</span><div class="sim"><span>${fmt(d.similarity, 2)}</span><span>${d.band}</span></div>`;
    const grid = $("#cands"); if (d.band === "reject") grid.appendChild(c); else grid.prepend(c);
  }
  function onSearchDone(d) {
    $("#match-count").textContent = `${d.matches} verified · ${d.rejected} rejected · ${d.candidates} candidates · ${d.timings.total_s}s`;
    fetch(`/api/run/${state.runId}`).then((r) => r.json()).then((run) => { state.matches = run.search.matches; renderMatches(); }).catch(() => {});
  }
  function gauge(sim) {
    const pct = Math.max(0, Math.min(1, sim)); const r = 26, c = 2 * Math.PI * r; const col = sim >= 0.5 ? "#35e29a" : sim >= 0.4 ? "#38e0ff" : "#ffc857";
    return `<div class="gauge"><svg viewBox="0 0 62 62"><circle cx="31" cy="31" r="${r}" fill="none" stroke="rgba(255,255,255,.1)" stroke-width="5"/><circle cx="31" cy="31" r="${r}" fill="none" stroke="${col}" stroke-width="5" stroke-linecap="round" stroke-dasharray="${c * pct} ${c}" transform="rotate(-90 31 31)"/></svg><b>${sim.toFixed(3)}</b><small>cosine</small></div>`;
  }
  function renderMatches() {
    const box = $("#matches"); box.innerHTML = "";
    if (!state.matches.length) { box.innerHTML = '<div class="empty">no biometrically verified matches — try a clearer photo or a person with public posts</div>'; return; }
    state.matches.forEach((m, i) => {
      const div = el("div", "match" + (state.selected === i ? " selected" : ""));
      const meta = m.metadata || {};
      div.innerHTML = `<img src="${esc(m.face_crop_b64 || m.thumbnail)}" alt=""/><div class="t"><b>${esc(meta.title || m.title || m.link)}</b><a href="${esc(m.link)}" target="_blank" rel="noopener">${esc(m.canonical_link)}</a><div class="badges"><span class="badge plat">${esc(m.platform)}</span>${m.is_post ? '<span class="badge post">post</span>' : ""}${meta.author ? `<span class="badge">by ${esc(meta.author)}</span>` : ""}${(m.engines || []).map((e) => `<span class="badge">${esc(e)}</span>`).join("")}</div></div><div>${gauge(m.similarity)}${!state.anchored && !$("#auto-anchor").checked ? `<button class="btn primary" data-i="${i}">Anchor this</button>` : ""}</div>`;
      box.appendChild(div);
    });
    box.querySelectorAll("button[data-i]").forEach((b) => b.addEventListener("click", () => anchorMatch(+b.dataset.i)));
  }
  async function anchorMatch(i) {
    state.selected = i; renderMatches();
    subscribe(state.runId, Date.now() / 1000 - 1);
    await fetch(`/api/anchor/${state.runId}`, { method: "POST", body: new URLSearchParams({ match_index: i }) });
  }
  function renderSelected(m) {
    $("#panel-chain").hidden = false;
    const meta = m.metadata || {};
    $("#r-selected").innerHTML = `<img src="${esc(m.face_crop_b64 || m.thumbnail || m.image_used)}" alt=""/><div><b>${esc(m.platform)} · ${esc(meta.title || m.title)}</b><a href="${esc(m.link)}" target="_blank" rel="noopener">${esc(m.canonical_link)}</a><div class="badges"><span class="badge">sim ${m.similarity.toFixed(3)} · ${m.band}</span>${meta.author ? `<span class="badge">by ${esc(meta.author)}</span>` : ""}${m.posted_at || meta.published ? `<span class="badge">${esc(m.posted_at || meta.published)}</span>` : ""}</div></div>`;
    $("#r-hashes").innerHTML = ""; $("#r-links").innerHTML = "";
  }
  function hashRow(k, v) { const row = el("div"); row.innerHTML = `<span>${esc(k)}</span><code>${esc(v)}</code>`; $("#r-hashes").appendChild(row); }
  function link(label, href) { if (!href) return; const a = el("a"); a.href = href; a.target = "_blank"; a.rel = "noopener"; a.textContent = label + " ↗"; $("#r-links").appendChild(a); }
  function onAnchored(d) {
    state.anchored = true;
    $("#r-id").textContent = `#${d.record_id}`; $("#r-chain").textContent = `${d.chain} · block ${d.block_number} · gas ${d.gas_used}`;
    hashRow("tx", d.tx_hash); hashRow("contract", d.contract); hashRow("faceCommitment", d.face_commitment); hashRow("contentHash", d.content_hash);
    if (d.explorer_tx && d.explorer_tx.startsWith("http")) { link("Transaction on Etherscan", d.explorer_tx); link("Contract", d.explorer_contract); const qr = $("#r-qr"); qr.src = `/api/qr?text=${encodeURIComponent(d.explorer_tx)}`; qr.hidden = false; }
    renderMatches();
  }
  function onCheck(c) {
    const li = el("li", c.ok ? "ok" : "bad");
    li.innerHTML = `<b>${c.ok ? "PASS" : "FAIL"}</b><code>${esc(c.name)}</code><span class="d">${esc(c.detail)}${c.expected !== undefined && !c.ok ? `<br/>on-chain ${esc(short(String(c.expected), 8))} ≠ local ${esc(short(String(c.actual), 8))}` : ""}</span>`;
    $("#checks").appendChild(li);
  }
  async function reverify(tamper) {
    if (!state.runId || !state.anchored) return;
    subscribe(state.runId, Date.now() / 1000 - 1);
    const body = new URLSearchParams({ tamper: tamper ? "true" : "false", field: "match.similarity", refetch: tamper ? "false" : "true" });
    await fetch(`/api/verify/${state.runId}`, { method: "POST", body });
    setTimeout(() => state.es && state.es.close(), 2500);
  }
  function noMatch(d) {
    $("#match-count").textContent = "no verified match";
    const box = $("#matches"); box.innerHTML = '<div class="empty">Search ran but no candidate passed biometric verification (cosine ≥ threshold). Nothing was anchored — the chain never receives unverified claims.</div>';
    if (d.rejected_top && d.rejected_top.length) box.innerHTML += `<div class="empty">closest rejected: ${d.rejected_top.map((m) => `${esc(m.platform)} ${m.similarity.toFixed(2)}`).join(" · ")}</div>`;
  }

  // ------------------------------------------------------------ log & ledger
  function log(ev, d) {
    const row = el("div"); const t = new Date().toLocaleTimeString([], { hour12: false });
    let msg = "";
    if (ev === "stage") msg = `${d.stage} → ${d.status}${d.message ? " · " + d.message : ""}`;
    else if (ev === "search.verify.progress") { if (d.similarity === undefined) return; msg = `${d.similarity.toFixed(3)} ${d.band} ${d.platform || "web"} ${d.link}`; }
    else if (ev === "chain.log") msg = d.message;
    else if (ev === "verify.check") msg = `${d.ok ? "PASS" : "FAIL"} ${d.name} — ${d.detail}`;
    else msg = JSON.stringify(d, (k, v) => (typeof v === "string" && v.startsWith("data:image") ? "[img]" : v)).slice(0, 220);
    row.innerHTML = `<span class="t">${t}</span><span class="e">${esc(ev)}</span><span>${esc(msg)}</span>`;
    const L = $("#log"); L.appendChild(row); L.scrollTop = L.scrollHeight;
  }
  function logErr(m) { const row = el("div"); row.innerHTML = `<span class="t"></span><span class="e err">error</span><span class="err">${esc(m)}</span>`; $("#log").appendChild(row); }
  async function loadLedger() {
    try {
      const runs = await (await fetch("/api/runs")).json();
      const tb = $("#ledger tbody"); tb.innerHTML = "";
      runs.filter((r) => r.status === "anchored").forEach((r) => { const tr = el("tr"); tr.innerHTML = `<td>${esc(r.run_id)}</td><td>${esc(r.platform)}</td><td><a href="${esc(r.link)}" target="_blank" rel="noopener">${esc(short(r.link, 28))}</a></td><td>${fmt(r.similarity)}</td><td>${r.tx ? `<a href="${esc(r.tx)}" target="_blank" rel="noopener">#${r.record_id} ↗</a>` : "#" + r.record_id}</td><td>${esc(r.verdict || "")}</td>`; tb.appendChild(tr); });
      $("#panel-ledger").hidden = !tb.children.length;
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------ wire up
  $("#btn-cam").addEventListener("click", startCam);
  $("#btn-stop").addEventListener("click", stopCam);
  $("#btn-upload").addEventListener("click", () => $("#file").click());
  $("#file").addEventListener("change", (e) => handleFile(e.target.files[0]));
  $("#btn-capture").addEventListener("click", capture);
  $("#btn-reverify").addEventListener("click", () => reverify(false));
  $("#btn-tamper").addEventListener("click", () => reverify(true));
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });
  loadStatus(); loadLedger(); setInterval(loadStatus, 30000);
})();
