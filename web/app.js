/* ============================================================================
   VERIFIED — front-end controller.
   Vanilla JS, no build step. Drives the sunrise scene from pipeline progress,
   streams pipeline events over SSE, renders candidates / matches / receipt /
   verification checks with staged motion.
   ========================================================================== */
(() => {
  "use strict";

  // ---------------------------------------------------------------- helpers
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; };
  const fmt = (n, d = 3) => (typeof n === "number" ? n.toFixed(d) : "–");
  const short = (h, n = 10) => (h && h.length > 2 * n + 2 ? `${h.slice(0, n + 2)}…${h.slice(-n)}` : h || "–");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeHref = (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "#");
  const safeImg = (u) => (/^(https?:\/\/|data:image\/)/i.test(String(u || "")) ? String(u) : "");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const STAGES = ["face", "search", "evidence", "ipfs", "chain", "eas", "ots", "verify"];

  const state = {
    stream: null, previewTimer: null, session: Math.random().toString(36).slice(2),
    runId: null, es: null, lastT: 0, matches: [], selected: null, source: "webcam", uploadBlob: null,
    livenessPassed: false, anchored: false, candSeen: 0, candTotal: 0, verified: 0, rejected: 0,
    best: 0, stageState: {}, ticker: [], busy: false,
  };

  // ------------------------------------------------------- sunrise progress
  function setProgress() {
    const done = STAGES.filter((s) => ["done", "skipped"].includes(state.stageState[s])).length;
    const running = STAGES.some((s) => state.stageState[s] === "running") ? 0.5 : 0;
    const p = Math.min(1, (done + running) / STAGES.length);
    document.documentElement.style.setProperty("--progress", p.toFixed(3));
  }

  // ---------------------------------------------------------------- ticker
  function tick(msg, kind = "") {
    state.ticker.unshift({ msg, kind });
    state.ticker = state.ticker.slice(0, 6);
    const track = $("#ticker-track");
    track.innerHTML = state.ticker.map((t) => `<span class="${t.kind}">${esc(t.msg)}</span>`).join("");
  }

  // ------------------------------------------------------------ count-up
  function countTo(node, to) {
    const from = Number(node.dataset.v || 0);
    if (from === to) return;
    node.dataset.v = to;
    const t0 = performance.now(), dur = 480;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      node.textContent = Math.round(from + (to - from) * e);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ------------------------------------------------------------ status bar
  async function loadStatus() {
    try {
      const s = await (await fetch("/api/status")).json();
      const chain = $("#pill-chain");
      chain.className = "pill " + (s.rpc ? "ok" : "bad");
      chain.textContent = s.rpc
        ? `${s.chain} · block ${s.block}${s.contract ? " · registry " + short(s.contract, 4) : " · registry deploys on first anchor"}${s.records != null ? " · " + s.records + " records" : ""}`
        : `${s.chain} · RPC unreachable`;
      const w = $("#pill-wallet");
      if (s.wallet) { w.className = "pill " + (s.balance_eth > 0.002 ? "ok" : "warn"); w.textContent = `${short(s.wallet, 4)} · ${Number(s.balance_eth).toFixed(4)} ETH`; }
      else { w.className = "pill bad"; w.textContent = "no wallet · python -m verified.cli wallet new"; }
      const on = Object.entries(s.engines || {}).filter(([, v]) => v).map(([k]) => k.replace("google_", "g-").replace("name_expansion", "name-sweep"));
      const e = $("#pill-engines");
      e.className = "pill " + (s.engines && s.engines.google_lens ? "ok" : "warn");
      e.textContent = `engines: ${on.join(" · ") || "none — set SERPAPI_KEY"}${s.ipfs ? " · ipfs" : ""}${s.eas ? " · eas" : ""}${s.ots ? " · btc" : ""}`;
    } catch { $("#pill-chain").className = "pill bad"; $("#pill-chain").textContent = "backend offline"; }
  }

  // ---------------------------------------------------------------- camera
  const video = $("#video"), overlay = $("#overlay"), ctx = overlay.getContext("2d");

  async function startCam() {
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 800 }, facingMode: "user" }, audio: false });
    } catch (e) { tick(`camera unavailable: ${e.message} — use upload`, "bad"); alert("Camera unavailable: " + e.message + "\n\nUse “upload photo” instead — the pipeline is identical."); return; }
    video.srcObject = state.stream;
    state.source = "webcam"; state.livenessPassed = false;
    $("#cam-wrap").classList.remove("upload");
    $("#cam-idle").hidden = true; $("#cam-hud").hidden = false; $("#btn-stop").hidden = false;
    try { await fetch("/api/preview/reset", { method: "POST", body: new URLSearchParams({ session: state.session }) }); } catch {}
    tick("camera live · detecting face", "hot");
    video.onloadedmetadata = () => { overlay.width = video.videoWidth; overlay.height = video.videoHeight; previewLoop(); };
  }

  function stopCam() {
    if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
    state.stream = null; clearTimeout(state.previewTimer);
    $("#cam-hud").hidden = true; $("#btn-stop").hidden = true;
  }

  function grabFrame(maxW) {
    const c = document.createElement("canvas");
    const s = Math.min(1, maxW / (video.videoWidth || maxW));
    c.width = Math.round((video.videoWidth || maxW) * s); c.height = Math.round((video.videoHeight || maxW) * s);
    c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
    return new Promise((res) => c.toBlob(res, "image/jpeg", 0.92));
  }

  async function previewLoop() {
    if (!state.stream) return;
    try {
      const blob = await grabFrame(480);
      const fd = new FormData(); fd.append("frame", blob, "f.jpg"); fd.append("session", state.session);
      const r = await (await fetch("/api/preview", { method: "POST", body: fd })).json();
      if (state.stream) drawPreview(r);
    } catch { /* transient */ }
    state.previewTimer = setTimeout(previewLoop, 110);
  }

  function drawPreview(r) {
    const sx = overlay.width / r.size[0], sy = overlay.height / r.size[1];
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    const f = r.faces[0];
    const live = $("#live");
    live.classList.toggle("passed", !!r.liveness.passed);
    live.querySelector(".l").classList.toggle("on", !!r.liveness.left);
    live.querySelector(".r").classList.toggle("on", !!r.liveness.right);
    if (r.liveness.passed && !state.livenessPassed) tick("liveness passed · head turn detected", "good");
    state.livenessPassed = !!r.liveness.passed;

    if (!f) { $("#hint").textContent = "looking for a face…"; $("#btn-capture").disabled = true; return; }

    const [x1, y1, x2, y2] = f.bbox.map((v, i) => v * (i % 2 ? sy : sx));
    const q = f.quality.overall, good = q >= 0.55;
    const col = r.liveness.passed ? (good ? "#3ddc97" : "#ffc857") : "#ffb347";
    const w = x2 - x1, h = y2 - y1, L = Math.min(w, h) * 0.22;

    ctx.lineWidth = 3; ctx.strokeStyle = col; ctx.shadowColor = col; ctx.shadowBlur = 20; ctx.lineCap = "round";
    [[x1, y1, 1, 1], [x2, y1, -1, 1], [x1, y2, 1, -1], [x2, y2, -1, -1]].forEach(([cx, cy, dx, dy]) => {
      ctx.beginPath(); ctx.moveTo(cx, cy + dy * L); ctx.lineTo(cx, cy); ctx.lineTo(cx + dx * L, cy); ctx.stroke();
    });
    // scanning sweep line inside the box
    const ph = (Date.now() % 2200) / 2200;
    ctx.shadowBlur = 12; ctx.strokeStyle = "rgba(56,224,255,.75)"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x1 + 4, y1 + h * ph); ctx.lineTo(x2 - 4, y1 + h * ph); ctx.stroke();
    // landmarks
    ctx.shadowBlur = 8; ctx.shadowColor = "#38e0ff"; ctx.fillStyle = "#38e0ff";
    f.kps.forEach(([px, py]) => { ctx.beginPath(); ctx.arc(px * sx, py * sy, 3, 0, Math.PI * 2); ctx.fill(); });
    // yaw slider
    ctx.shadowBlur = 0; ctx.fillStyle = "rgba(0,0,0,.5)";
    roundRect(ctx, x1, y2 + 10, w, 6, 3); ctx.fill();
    ctx.fillStyle = col;
    const px = x1 + w / 2 + Math.max(-1, Math.min(1, f.yaw_ratio * 2)) * (w / 2 - 7) - 6;
    roundRect(ctx, px, y2 + 10, 12, 6, 3); ctx.fill();

    $("#q-bar").style.width = `${q * 100}%`; $("#q-val").textContent = q.toFixed(2);
    $("#q-sharp").style.width = `${f.quality.sharpness * 100}%`;
    $("#q-size").style.width = `${f.quality.size * 100}%`;
    $("#q-front").style.width = `${f.quality.frontal * 100}%`;

    const hints = [...(f.hints || [])];
    if (!r.liveness.passed) hints.unshift("liveness · turn your head left, then right");
    if (f.others) hints.push(`${f.others} other face(s) — the largest is used`);
    $("#hint").textContent = hints.join(" · ") || "ready — hold still and capture";
    $("#btn-capture").disabled = state.busy || !(good && r.liveness.passed);
  }

  function setShot(dataUrl) {
    if (safeImg(dataUrl)) $("#cam-wrap").style.setProperty("--shot", `url("${dataUrl}")`);
  }

  function roundRect(c, x, y, w, h, r) {
    c.beginPath();
    c.moveTo(x + r, y); c.lineTo(x + w - r, y); c.quadraticCurveTo(x + w, y, x + w, y + r);
    c.lineTo(x + w, y + h - r); c.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    c.lineTo(x + r, y + h); c.quadraticCurveTo(x, y + h, x, y + h - r);
    c.lineTo(x, y + r); c.quadraticCurveTo(x, y, x + r, y); c.closePath();
  }

  // ---------------------------------------------------------------- upload
  function handleFile(file) {
    if (!file || !/^image\//.test(file.type)) return;
    stopCam();
    state.uploadBlob = file; state.source = "upload";
    const url = URL.createObjectURL(file);
    $("#cam-wrap").classList.add("upload");
    $("#cam-idle").hidden = true; $("#cam-hud").hidden = true;
    $("#btn-capture").disabled = state.busy;
    const img = new Image();
    img.onload = () => { overlay.width = img.width; overlay.height = img.height; ctx.drawImage(img, 0, 0); setShot(overlay.toDataURL("image/jpeg", .5)); URL.revokeObjectURL(url); };
    img.src = url;
    tick(`photo loaded · ${file.name || "image"}`, "hot");
  }

  // ------------------------------------------------------------------- run
  async function capture() {
    if (state.busy) return;
    let blob;
    if (state.source === "webcam") {
      blob = await grabFrame(1280);
      stopCam();
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.onload = () => { $("#cam-wrap").classList.add("upload"); overlay.width = img.width; overlay.height = img.height; ctx.drawImage(img, 0, 0); setShot(overlay.toDataURL("image/jpeg", .5)); URL.revokeObjectURL(url); };
      img.src = url;
    } else blob = state.uploadBlob;
    if (!blob) return;

    resetRunUI();
    state.busy = true; $("#btn-capture").disabled = true;
    const fd = new FormData();
    fd.append("image", blob, "query.jpg");
    fd.append("source", state.source);
    fd.append("auto_anchor", $("#auto-anchor").checked ? "true" : "false");
    try {
      const r = await fetch("/api/scan", { method: "POST", body: fd });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const { run_id } = await r.json();
      state.runId = run_id; $("#run-id").textContent = run_id;
      tick(`run ${run_id} started`, "hot");
      subscribe(run_id);
    } catch (e) {
      state.busy = false; $("#btn-capture").disabled = false;
      tick(`scan failed: ${e.message}`, "bad");
      alert("Could not start the scan: " + e.message);
    }
  }

  function resetRunUI() {
    STAGES.forEach((s) => { state.stageState[s] = null; const n = document.querySelector(`.step[data-stage="${s}"]`); if (n) n.className = "step"; });
    setProgress();
    $("#panel-search").hidden = true; $("#panel-chain").hidden = true;
    $("#face-card").hidden = true; $("#identity-card").hidden = true;
    $("#cands").innerHTML = ""; $("#engines").innerHTML = ""; $("#log").innerHTML = "";
    $("#checks").innerHTML = ""; $("#r-hashes").innerHTML = ""; $("#r-links").innerHTML = "";
    $("#matches").innerHTML = '<div class="empty skeleton">candidates stream in as engines respond…</div>';
    $("#verify-bar").style.width = "0%"; $("#verify-count").textContent = "0 / 0";
    $("#match-count").textContent = ""; $("#search-sub").textContent = "";
    $("#tamper-note").hidden = true; $("#stamp").classList.remove("on");
    $("#verdict").className = "verdict"; $("#verdict").textContent = "…";
    $("#r-qr").hidden = true; $("#r-id").textContent = "#–"; $("#r-chain").textContent = "–"; $("#r-selected").innerHTML = "";
    ["st-cand", "st-ver", "st-rej"].forEach((id) => { const n = $("#" + id); n.dataset.v = 0; n.textContent = "0"; });
    $("#st-best").textContent = "–";
    Object.assign(state, { matches: [], selected: null, anchored: false, candSeen: 0, candTotal: 0, verified: 0, rejected: 0, best: 0 });
  }

  // ---------------------------------------------------------------- events
  function subscribe(runId, since) {
    if (state.es) state.es.close();
    const q = since === "live" ? "?since=-1" : since ? "?since=" + since : "";
    const es = new EventSource(`/api/events/${runId}${q}`);
    state.es = es;
    const opened = new Promise((res) => { es.addEventListener("open", () => res(true), { once: true }); setTimeout(() => res(false), 4000); });
    const on = (ev, fn) => es.addEventListener(ev, (e) => {
      let d; try { d = JSON.parse(e.data); } catch { return; }
      if (typeof d.__t === "number") state.lastT = Math.max(state.lastT, d.__t);
      log(ev, d);
      try { fn(d); } catch (err) { console.error(ev, err); }
    });

    on("run.start", () => {});
    on("stage", onStage);
    on("face.detected", onFace);
    on("search.prepare", (d) => tick(`query crop ${Math.round(d.crop_bytes / 1024)} KB prepared`));
    on("search.hosted", (d) => { $("#search-sub").textContent = `query crop hosted on ${d.host} · expires ${d.ttl}`; tick(`crop hosted on ${d.host} (${d.ttl} ttl)`); });
    on("search.fanout", (d) => { (d.engines || []).forEach((e) => chip(e, "run", `${e} …`)); tick(`fan-out · ${(d.engines || []).join(", ")}`, "hot"); });
    on("search.engine", onEngine);
    on("search.warning", (d) => tick(d.message, "bad"));
    on("search.verify.start", (d) => { state.candTotal = Math.max(state.candTotal, 0) + (d.shortlist || 0); updateVerifyCount(); tick(`verifying ${d.shortlist} candidates biometrically${d.phase ? " (expansion)" : ""}`, "hot"); });
    on("search.verify.progress", onCandidate);
    on("search.identity", (d) => { if (d.names && d.names.length) { $("#identity-card").hidden = false; $("#names").innerHTML = d.names.map((n) => `<span>${esc(n)}</span>`).join(""); tick(`identity inferred · ${d.names[0]}`, "good"); } });
    on("search.done", onSearchDone);
    on("scan.done", (d) => { state.matches = d.matches || []; renderMatches(); });
    on("anchor.selected", (d) => { state.selected = d.index; renderMatches(); renderSelected(d.match); });
    on("evidence.built", onEvidence);
    on("ipfs.done", (d) => { hashRow("evidence CID", d.cid_on_chain + (d.pinned ? "  (pinned · Pinata)" : "  (local CIDv1)")); if (d.gateway_url) link("IPFS bundle", d.gateway_url); tick(`ipfs ${short(d.cid_on_chain, 8)}${d.pinned ? " pinned" : " (local cid)"}`); });
    on("chain.wallet", (d) => { hashRow("submitter", `${d.address} · ${d.balance_eth} ETH`); tick(`wallet ${short(d.address, 5)} · ${d.balance_eth} ETH`); });
    on("chain.log", (d) => { if (/tx sent/.test(d.message)) tick(d.message.replace(" - waiting for inclusion...", " · waiting"), "hot"); });
    on("chain.deployed", (d) => { link("Registry contract", d.explorer); tick(`registry deployed ${short(d.contract, 5)}`, "good"); });
    on("chain.anchored", onAnchored);
    on("eas.done", (d) => { if (d.attestation_uid) { hashRow("EAS attestation", d.attestation_uid); link("EAS attestation", d.explorer_attestation); link("EAS schema", d.explorer_schema); tick(`eas attestation ${short(d.attestation_uid, 6)}`, "good"); } else if (d.error) tick(`eas skipped: ${String(d.error).slice(0, 60)}`, "bad"); });
    on("ots.done", (d) => { if (d.calendars) { hashRow("Bitcoin (OTS)", `sha256 ${short(d.sha256, 8)} → ${d.calendars.length} calendars · confirmation pending`); tick(`bitcoin timestamp submitted to ${d.calendars.length} calendars`, "good"); } });
    on("verify.start", onVerifyStart);
    on("verify.check", onCheck);
    on("verify.done", (d) => { const v = $("#verdict"); v.className = "verdict " + (d.verdict === "VERIFIED" ? "ok" : "bad"); v.textContent = d.verdict; tick(`verdict ${d.verdict}`, d.verdict === "VERIFIED" ? "good" : "bad"); });
    on("run.done", onRunDone);
    on("run.error", (d) => { es.close(); logErr(d.message); state.busy = false; $("#btn-capture").disabled = false; tick(`error: ${String(d.message).slice(0, 80)}`, "bad"); alert("Pipeline error:\n\n" + d.message); });
    return opened;
  }

  function onStage(d) {
    state.stageState[d.stage] = d.status;
    const n = document.querySelector(`.step[data-stage="${d.stage}"]`);
    if (n) n.className = `step ${d.status}`;
    setProgress();
    if (d.stage === "search" && d.status === "running") reveal("#panel-search");
    if (d.stage === "evidence" && d.status === "running") reveal("#panel-chain");
    if (d.status === "running") tick(`${d.stage} · running`, "hot");
    if (d.status === "failed" && d.message) tick(`${d.stage} failed · ${String(d.message).slice(0, 70)}`, "bad");
  }

  function reveal(sel) {
    const n = $(sel);
    if (!n.hidden) return;
    n.hidden = false;
    n.style.animation = "none"; void n.offsetWidth; n.style.animation = "";
    setTimeout(() => n.scrollIntoView({ behavior: "smooth", block: "nearest" }), 120);
  }

  function onFace(d) {
    $("#face-card").hidden = false;
    const crop = safeImg(d.face_crop_b64);
    if (crop) $("#face-crop").src = crop;
    $("#f-det").textContent = fmt(d.det_score);
    $("#f-q").textContent = fmt(d.quality.overall, 2);
    $("#f-ag").textContent = `${d.age ?? "?"} · ${d.gender ?? "?"}`;
    $("#f-n").textContent = d.faces_in_frame;
    $("#f-commit").textContent = d.commitment;
    const ann = safeImg(d.annotated_b64);
    if (ann) { const img = new Image(); img.onload = () => { $("#cam-wrap").classList.add("upload"); overlay.width = img.width; overlay.height = img.height; ctx.drawImage(img, 0, 0); setShot(ann); }; img.src = ann; }
    tick(`face encoded · 512-d template · quality ${fmt(d.quality.overall, 2)}`, "good");
  }

  function chip(id, cls, text) {
    let c = document.getElementById("chip-" + id);
    if (!c) { c = el("span", "chip"); c.id = "chip-" + id; $("#engines").appendChild(c); }
    c.className = "chip " + cls; c.textContent = text;
  }

  function onEngine(d) {
    if (d.error) { chip(d.engine, "err", `${d.engine} ✕`); chipTitle(d.engine, d.error); tick(`${d.engine} error · ${String(d.error).slice(0, 60)}`, "bad"); }
    else { chip(d.engine, "ok", `${d.engine} ${d.candidates}`); chipTitle(d.engine, `${d.candidates} candidates · ${d.social} social · ${d.posts} posts${d.query ? "\nquery: " + d.query : ""}`); tick(`${d.engine} → ${d.candidates} candidates (${d.posts} posts)`); }
  }
  function chipTitle(id, t) { const c = document.getElementById("chip-" + id); if (c) c.title = String(t); }

  function updateVerifyCount() {
    $("#verify-count").textContent = `${state.candSeen} / ${state.candTotal || "?"}`;
    if (state.candTotal) $("#verify-bar").style.width = `${Math.min(100, (state.candSeen / state.candTotal) * 100)}%`;
    countTo($("#st-cand"), state.candSeen);
    countTo($("#st-ver"), state.verified);
    countTo($("#st-rej"), state.rejected);
    $("#st-best").textContent = state.best ? fmt(state.best) : "–";
  }

  function onCandidate(d) {
    state.candSeen = Math.max(state.candSeen, d.done);
    if (d.similarity !== undefined) {
      if (d.band === "reject") state.rejected++; else state.verified++;
      if (d.similarity > state.best) state.best = d.similarity;
      const c = el("div", `cand ${d.band}${d.band === "reject" ? " reject" : ""}`);
      c.title = `${d.title || ""}\n${d.link}\ncosine ${fmt(d.similarity)} · ${d.band} · via ${d.engine}`;
      const img = safeImg(d.face_crop_b64);
      c.innerHTML = `${img ? `<img src="${esc(img)}" alt=""/>` : ""}<span class="plat">${esc(d.platform || "web")}</span><div class="sim"><span>${fmt(d.similarity, 2)}</span><span>${esc(d.band)}</span></div>`;
      const grid = $("#cands");
      if (d.band === "reject") grid.appendChild(c); else grid.prepend(c);
      if (d.band === "strong" || d.band === "match") tick(`match ${fmt(d.similarity)} · ${d.platform || "web"}`, "good");
    }
    updateVerifyCount();
  }

  function onSearchDone(d) {
    $("#match-count").textContent = `${d.matches} verified · ${d.rejected} rejected · ${d.candidates} candidates · ${d.timings && d.timings.total_s}s`;
    tick(`search complete · ${d.matches} verified of ${d.candidates} candidates`, d.matches ? "good" : "bad");
  }

  function gauge(sim) {
    const pct = Math.max(0, Math.min(1, sim)), r = 26, c = 2 * Math.PI * r;
    const col = sim >= 0.5 ? "#3ddc97" : sim >= 0.4 ? "#38e0ff" : "#ffc857";
    return `<div class="gauge"><svg viewBox="0 0 62 62"><circle cx="31" cy="31" r="${r}" fill="none" stroke="rgba(244,226,200,.1)" stroke-width="5"/><circle class="val" cx="31" cy="31" r="${r}" fill="none" stroke="${col}" stroke-width="5" stroke-linecap="round" stroke-dasharray="${(c * pct).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 31 31)"/></svg><b>${sim.toFixed(3)}</b><small>cosine</small></div>`;
  }

  function renderMatches() {
    const box = $("#matches");
    box.innerHTML = "";
    if (!state.matches.length) {
      box.innerHTML = '<div class="empty">no biometrically verified match yet — engines returned look-alikes only.<br/>nothing is anchored unless the face verifies.</div>';
      return;
    }
    const manual = !$("#auto-anchor").checked && !state.anchored;
    state.matches.forEach((m, i) => {
      const meta = m.metadata || {};
      const div = el("div", "match" + (state.selected === i ? " selected" : ""));
      div.style.animationDelay = `${Math.min(i, 8) * 45}ms`;
      const thumb = safeImg(m.face_crop_b64) || safeImg(m.thumbnail);
      div.innerHTML =
        `${thumb ? `<img src="${esc(thumb)}" alt=""/>` : "<div></div>"}` +
        `<div class="t"><b>${esc(meta.title || m.title || m.canonical_link)}</b>` +
        `<a href="${esc(safeHref(m.link))}" target="_blank" rel="noopener noreferrer">${esc(m.canonical_link)}</a>` +
        `<div class="badges"><span class="badge plat">${esc(m.platform)}</span>` +
        `${m.is_post ? '<span class="badge post">post</span>' : ""}` +
        `${meta.author ? `<span class="badge">by ${esc(meta.author)}</span>` : ""}` +
        `${(m.engines || []).map((e) => `<span class="badge">${esc(e)}</span>`).join("")}</div></div>` +
        `<div>${gauge(m.similarity)}${manual ? `<button class="btn primary" data-i="${i}">anchor</button>` : ""}</div>`;
      box.appendChild(div);
    });
    box.querySelectorAll("button[data-i]").forEach((b) => b.addEventListener("click", () => anchorMatch(+b.dataset.i)));
  }

  async function anchorMatch(i) {
    state.selected = i; renderMatches();
    await subscribe(state.runId, "live");
    try {
      const r = await fetch(`/api/anchor/${state.runId}`, { method: "POST", body: new URLSearchParams({ match_index: String(i) }) });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state.busy = true;
    } catch (e) { tick(`anchor failed: ${e.message}`, "bad"); alert("Anchor failed: " + e.message); }
  }

  function renderSelected(m) {
    reveal("#panel-chain");
    const meta = m.metadata || {};
    const thumb = safeImg(m.face_crop_b64) || safeImg(m.thumbnail) || safeImg(m.image_used);
    $("#r-selected").innerHTML =
      `${thumb ? `<img src="${esc(thumb)}" alt=""/>` : "<div></div>"}` +
      `<div><b>${esc(m.platform)} · ${esc(meta.title || m.title || "")}</b>` +
      `<a href="${esc(safeHref(m.link))}" target="_blank" rel="noopener noreferrer">${esc(m.canonical_link)}</a>` +
      `<div class="badges"><span class="badge post">cosine ${m.similarity.toFixed(3)} · ${esc(m.band)}</span>` +
      `${meta.author ? `<span class="badge">by ${esc(meta.author)}</span>` : ""}` +
      `${m.posted_at || meta.published ? `<span class="badge">${esc(m.posted_at || meta.published)}</span>` : ""}</div></div>`;
    $("#r-hashes").innerHTML = ""; $("#r-links").innerHTML = "";
    tick(`anchoring ${m.platform} post · cosine ${m.similarity.toFixed(3)}`, "hot");
  }

  function onEvidence(d) {
    $("#bundle").textContent = JSON.stringify(d.bundle, null, 1);
    $("#bundle-meta").textContent = `${d.leaf_count} Merkle leaves · ${d.canonical_size} bytes · keccak ${short(d.record_hash, 6)}`;
    hashRow("recordHash", d.record_hash);
    hashRow("merkleRoot", d.merkle_root);
    tick(`evidence bundle · ${d.leaf_count} leaves · ${d.canonical_size} bytes`);
  }

  function hashRow(k, v) {
    const row = el("div");
    row.innerHTML = `<span>${esc(k)}</span><code>${esc(v)}</code>`;
    row.style.animationDelay = `${Math.min($("#r-hashes").children.length, 8) * 40}ms`;
    $("#r-hashes").appendChild(row);
  }

  function link(label, href) {
    if (!href || !/^https?:\/\//i.test(href)) return;
    const a = el("a"); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = label + " ↗";
    a.style.animationDelay = `${Math.min($("#r-links").children.length, 6) * 60}ms`;
    $("#r-links").appendChild(a);
  }

  function onAnchored(d) {
    state.anchored = true;
    $("#r-id").textContent = `#${d.record_id}`;
    $("#r-chain").textContent = `${d.chain} · block ${d.block_number} · gas ${d.gas_used}`;
    hashRow("tx", d.tx_hash); hashRow("contract", d.contract);
    hashRow("faceCommitment", d.face_commitment); hashRow("contentHash", d.content_hash);
    if (d.explorer_tx && /^https?:\/\//.test(d.explorer_tx)) {
      link("transaction on explorer", d.explorer_tx);
      link("registry contract", d.explorer_contract);
      const qr = $("#r-qr"); qr.src = `/api/qr?text=${encodeURIComponent(d.explorer_tx)}`; qr.hidden = false;
    }
    setTimeout(() => $("#stamp").classList.add("on"), 200);
    try { history.replaceState(null, "", `#run=${state.runId}`); } catch {}
    tick(`ANCHORED · record #${d.record_id} · block ${d.block_number}`, "good");
    renderMatches(); loadStatus();
  }

  function onVerifyStart(d) {
    $("#checks").innerHTML = "";
    $("#verdict").className = "verdict"; $("#verdict").textContent = "checking…";
    if (d.tamper) {
      $("#tamper-note").hidden = false;
      $("#tamper-note").textContent = `TAMPER TEST — ${d.tamper.field}: ${JSON.stringify(d.tamper.old)} → ${JSON.stringify(d.tamper.new)} in the local bundle. Re-running every check against the immutable on-chain record…`;
      tick(`tamper test · ${d.tamper.field} altered`, "bad");
    } else $("#tamper-note").hidden = true;
  }

  function onCheck(c) {
    const li = el("li", c.ok ? "ok" : "bad");
    li.style.animationDelay = `${Math.min($("#checks").children.length, 12) * 35}ms`;
    li.innerHTML = `<b>${c.ok ? "PASS" : "FAIL"}</b><code>${esc(c.name)}</code><span class="d">${esc(c.detail)}` +
      `${c.expected !== undefined && !c.ok ? `<br/>on-chain <code>${esc(short(String(c.expected), 8))}</code> ≠ local <code>${esc(short(String(c.actual), 8))}</code>` : ""}</span>`;
    $("#checks").appendChild(li);
  }

  function onRunDone(d) {
    if (state.es) state.es.close();
    state.es = null;
    state.busy = false; $("#btn-capture").disabled = false;
    loadLedger();
    if (d.status === "no_match") noMatch(d);
    if (d.status === "no_face") { $("#hint").textContent = "no face detected — try again with better light"; tick("no face detected", "bad"); }
    if (d.status === "matches" && d.manual) tick("pick a match and press anchor", "hot");
  }

  function noMatch(d) {
    $("#match-count").textContent = "no verified match";
    const box = $("#matches");
    box.innerHTML = '<div class="empty">the search ran, but no candidate passed biometric verification.<br/><b>nothing was anchored</b> — the chain never receives unverified claims.</div>';
    if (d.rejected_top && d.rejected_top.length) {
      box.innerHTML += `<div class="empty">closest rejected: ${d.rejected_top.map((m) => `${esc(m.platform)} ${fmt(m.similarity, 2)}`).join(" · ")}</div>`;
    }
  }

  // ------------------------------------------------------------ re-verify
  async function reverify(tamper) {
    if (!state.runId || !state.anchored) { tick("open an anchored run from the ledger first", "bad"); return; }
    $("#btn-reverify").disabled = true; $("#btn-tamper").disabled = true;
    await subscribe(state.runId, "live");
    try {
      const body = new URLSearchParams({ tamper: tamper ? "true" : "false", field: "match.similarity", refetch: tamper ? "false" : "true" });
      const r = await fetch(`/api/verify/${state.runId}`, { method: "POST", body });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      // the response carries the full report: render from it so the panel is
      // correct even if the event stream was still connecting.
      const rep = await r.json();
      if (!$("#checks").children.length) { onVerifyStart({ tamper: rep.tamper }); (rep.checks || []).forEach(onCheck); }
      const v = $("#verdict");
      v.className = "verdict " + (rep.verdict === "VERIFIED" ? "ok" : "bad");
      v.textContent = rep.verdict;
      tick(`verdict ${rep.verdict}`, rep.verdict === "VERIFIED" ? "good" : "bad");
    } catch (e) { tick(`verify failed: ${e.message}`, "bad"); }
    $("#btn-reverify").disabled = false; $("#btn-tamper").disabled = false;
    setTimeout(() => { if (state.es) { state.es.close(); state.es = null; } }, 2500);
  }

  // -------------------------------------------------- open a previous run
  async function openRun(runId) {
    try {
      const run = await (await fetch(`/api/run/${runId}`)).json();
      if (run.status !== "anchored") { tick(`run ${runId} was never anchored`, "bad"); return; }
      resetRunUI();
      state.runId = runId; $("#run-id").textContent = runId;
      state.matches = run.search.matches || [];
      state.selected = run.selected_match ?? null;
      state.anchored = true; state.lastT = 0;
      STAGES.forEach((st) => (state.stageState[st] = "done")); setProgress();
      document.querySelectorAll(".step").forEach((n) => (n.className = "step done"));
      reveal("#panel-search"); reveal("#panel-chain");
      const f = run.face || {};
      if (f.commitment) { $("#face-card").hidden = false; $("#f-commit").textContent = f.commitment; $("#f-det").textContent = fmt(f.det_score); $("#f-q").textContent = fmt((f.quality || {}).overall, 2); $("#f-ag").textContent = `${f.age ?? "?"} · ${f.gender ?? "?"}`; $("#f-n").textContent = f.faces_in_frame ?? "–"; }
      $("#face-crop").src = `/api/run/${runId}/file/query_face.jpg`;
      setShot(`/api/run/${runId}/file/query_annotated.jpg`);
      $("#cam-wrap").classList.add("upload");
      const im = new Image();
      im.onload = () => { overlay.width = im.width; overlay.height = im.height; ctx.drawImage(im, 0, 0); };
      im.src = `/api/run/${runId}/file/query_annotated.jpg`;
      renderMatches();
      if (state.selected != null && state.matches[state.selected]) renderSelected(state.matches[state.selected]);
      const a = run.anchor || {}, dg = run.bundle_digest || {};
      $("#r-id").textContent = `#${a.record_id}`; $("#r-chain").textContent = `${a.chain} · block ${a.block_number} · gas ${a.gas_used}`;
      ["recordHash|" + dg.record_hash, "merkleRoot|" + dg.merkle_root, "tx|" + a.tx_hash, "contract|" + a.contract, "faceCommitment|" + a.face_commitment, "contentHash|" + a.content_hash, "evidence CID|" + a.evidence_cid].forEach((kv) => { const [k, v] = kv.split("|"); if (v && v !== "undefined") hashRow(k, v); });
      if (a.explorer_tx && /^https?:/.test(a.explorer_tx)) { link("transaction on explorer", a.explorer_tx); link("registry contract", a.explorer_contract); const qr = $("#r-qr"); qr.src = `/api/qr?text=${encodeURIComponent(a.explorer_tx)}`; qr.hidden = false; }
      if ((run.eas || {}).explorer_attestation) link("EAS attestation", run.eas.explorer_attestation);
      if ((run.ipfs || {}).gateway_url) link("IPFS bundle", run.ipfs.gateway_url);
      $("#stamp").classList.add("on");
      const v = run.verification || {};
      (v.checks || []).forEach(onCheck);
      $("#verdict").className = "verdict " + (v.verdict === "VERIFIED" ? "ok" : "bad"); $("#verdict").textContent = v.verdict || "—";
      $("#bundle").textContent = "loading bundle…";
      fetch(`/api/run/${runId}/file/bundle.pretty.json`).then((r) => r.text()).then((t) => { $("#bundle").textContent = t; $("#bundle-meta").textContent = `${dg.leaf_count || "?"} Merkle leaves · ${dg.canonical_size || "?"} bytes · keccak ${short(dg.record_hash, 6)}`; }).catch(() => {});
      $("#match-count").textContent = `${state.matches.length} verified · record #${a.record_id}`;
      try { history.replaceState(null, "", `#run=${runId}`); } catch {}
      tick(`opened run ${runId} · re-verify and tamper test are live`, "hot");
      $("#panel-chain").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) { tick(`could not open ${runId}: ${e.message}`, "bad"); }
  }

  // ------------------------------------------------------------ log/ledger
  function log(ev, d) {
    const t = new Date().toLocaleTimeString([], { hour12: false });
    let msg = "";
    if (ev === "stage") msg = `${d.stage} → ${d.status}${d.message ? " · " + d.message : ""}`;
    else if (ev === "search.verify.progress") { if (d.similarity === undefined) return; msg = `${fmt(d.similarity)} ${d.band} ${d.platform || "web"} ${d.link}`; }
    else if (ev === "chain.log") msg = d.message;
    else if (ev === "verify.check") msg = `${d.ok ? "PASS" : "FAIL"} ${d.name} — ${d.detail}`;
    else if (ev === "scan.done") msg = `${(d.matches || []).length} matches persisted`;
    else msg = JSON.stringify(d, (k, v) => (typeof v === "string" && v.startsWith("data:image") ? "[img]" : v)).slice(0, 230);
    const row = el("div");
    row.innerHTML = `<span class="t">${t}</span><span class="e">${esc(ev)}</span><span class="m">${esc(msg)}</span>`;
    const L = $("#log");
    L.appendChild(row);
    while (L.children.length > 400) L.removeChild(L.firstChild);
    L.scrollTop = L.scrollHeight;
  }

  function logErr(m) {
    const row = el("div");
    row.innerHTML = `<span class="t"></span><span class="e err">error</span><span class="err">${esc(m)}</span>`;
    $("#log").appendChild(row); $("#log").scrollTop = 1e9;
  }

  async function loadLedger() {
    try {
      const runs = await (await fetch("/api/runs")).json();
      const rows = runs.filter((r) => r.status === "anchored");
      const tb = $("#ledger tbody"); tb.innerHTML = "";
      rows.forEach((r, i) => {
        const tr = el("tr");
        tr.style.animationDelay = `${Math.min(i, 10) * 35}ms`;
        tr.style.cursor = "pointer";
        tr.title = "open this run — enables re-verify / tamper test";
        tr.addEventListener("click", (ev) => { if (ev.target.tagName !== "A") openRun(r.run_id); });
        tr.innerHTML =
          `<td><span class="open">↗ ${esc(r.run_id)}</span></td><td>${esc(r.platform || "")}</td>` +
          `<td><a href="${esc(safeHref(r.link))}" target="_blank" rel="noopener noreferrer">${esc(short(r.link, 26))}</a></td>` +
          `<td>${fmt(r.similarity)}</td>` +
          `<td>${r.tx && /^https?:/.test(r.tx) ? `<a href="${esc(r.tx)}" target="_blank" rel="noopener noreferrer">#${esc(String(r.record_id))} ↗</a>` : "#" + esc(String(r.record_id ?? "–"))}</td>` +
          `<td>${esc(r.verdict || "")}</td>`;
        tb.appendChild(tr);
      });
      $("#panel-ledger").hidden = !rows.length;
    } catch { /* ignore */ }
  }

  // ---------------------------------------------------------------- wiring
  $("#btn-cam").addEventListener("click", startCam);
  $("#btn-stop").addEventListener("click", () => { stopCam(); $("#cam-idle").hidden = false; $("#btn-capture").disabled = true; });
  $("#btn-upload").addEventListener("click", () => $("#file").click());
  $("#file").addEventListener("change", (e) => handleFile(e.target.files[0]));
  $("#btn-capture").addEventListener("click", capture);
  $("#btn-reverify").addEventListener("click", () => reverify(false));
  $("#btn-tamper").addEventListener("click", () => reverify(true));
  $("#auto-anchor").addEventListener("change", () => renderMatches());
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });
  document.addEventListener("keydown", (e) => { if (e.key === " " && e.target === document.body && !$("#btn-capture").disabled) { e.preventDefault(); capture(); } });
  window.addEventListener("beforeunload", () => { if (state.es) state.es.close(); stopCam(); });

  loadStatus(); setProgress();
  loadLedger().then(() => {
    const m = /(?:^|[#&])run=([0-9a-f-]+)/i.exec(location.hash || "");
    if (m) openRun(m[1]);
  });
  setInterval(loadStatus, 30000);
  tick("ready · waiting for a face");
})();
