// Core Map: full-screen animated map of every service around the ForgePanel core.
// Reads statuses from the dashboard's status dots (filled by checkMany via /api/check)
// and re-checks every 5 s while open. Plain canvas, no libraries.
(function () {
  const COL = { on: "#22c55e", off: "#ff2a2a", unk: "#facc15" };
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let ov, cv, ctx, tip, raf = 0, poll = 0, nodes = [], hover = -1, t0 = 0, W = 0, H = 0, dpr = 1;

  function statusOf(i) {
    const el = document.getElementById("status-" + i);
    if (!el) return "unk";
    if (el.classList.contains("online")) return "on";
    if (el.classList.contains("offline")) return "off";
    return "unk";
  }
  function build() {
    const all = getAllServices();
    const old = new Map(nodes.map(n => [n.key, n]));
    nodes = all.map((s, i) => {
      const key = s.key || ("c:" + i);
      const o = old.get(key);
      return { key, i, name: s.name, url: urlFor(s), st: statusOf(i), born: o ? o.born : performance.now() + i * 40 };
    });
    layout();
  }
  function layout() {
    const cx = W / 2, cy = H / 2, n = nodes.length;
    const minR = Math.min(W, H) * 0.22, gap = 78;
    let idx = 0, ring = 0;
    while (idx < n) {
      const r = minR + ring * gap;
      const cap = Math.max(6, Math.floor((2 * Math.PI * r) / 96));
      const cnt = Math.min(cap, n - idx);
      for (let k = 0; k < cnt; k++, idx++) {
        const a = -Math.PI / 2 + (k / cnt) * Math.PI * 2 + ring * 0.35;
        nodes[idx].x = cx + Math.cos(a) * r;
        nodes[idx].y = cy + Math.sin(a) * r;
      }
      ring++;
    }
  }
  function resize() {
    dpr = window.devicePixelRatio || 1; W = innerWidth; H = innerHeight;
    cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + "px"; cv.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); layout();
  }
  function rr(x, y, w, h, r) { ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x, y, w, h, r) : ctx.rect(x, y, w, h); }
  function frame(now) {
    const still = reduce.matches, t = still ? 0 : (now - t0) / 1000;
    const cx = W / 2, cy = H / 2;
    ctx.fillStyle = "#0a0a0a"; ctx.fillRect(0, 0, W, H);
    // lines
    ctx.lineWidth = 2;
    for (const n of nodes) {
      const a = still ? 1 : Math.min(1, Math.max(0, (now - n.born) / 600));
      if (a <= 0) continue;
      const c = COL[n.st];
      ctx.save(); ctx.strokeStyle = c;
      if (n.st === "on") { ctx.globalAlpha = 0.55 * a; ctx.setLineDash([]); }
      else if (n.st === "off") { ctx.globalAlpha = a * (still ? 0.7 : (Math.random() < 0.08 ? 0.15 : 0.75)); ctx.setLineDash([10, 6]); ctx.lineDashOffset = -t * 30; }
      else { ctx.globalAlpha = a * (still ? 0.6 : 0.35 + 0.3 * Math.sin(t * 1.5 + n.i)); ctx.setLineDash([4, 8]); ctx.lineDashOffset = -t * 8; }
      ctx.beginPath(); ctx.moveTo(n.x, n.y); ctx.lineTo(cx, cy); ctx.stroke(); ctx.restore();
      if (n.st === "on" && !still) {          // data pulses toward the core
        for (let p = 0; p < 2; p++) {
          const f = ((t * 0.5 + n.i * 0.13 + p * 0.5) % 1);
          const px = n.x + (cx - n.x) * f, py = n.y + (cy - n.y) * f;
          ctx.save(); ctx.globalAlpha = a * (1 - f * 0.6); ctx.fillStyle = c; ctx.shadowColor = c; ctx.shadowBlur = 10;
          ctx.beginPath(); ctx.arc(px, py, 3, 0, Math.PI * 2); ctx.fill(); ctx.restore();
        }
      }
    }
    // core
    const br = still ? 0.5 : 0.5 + 0.5 * Math.sin(t * 1.6), S = 84;
    ctx.save(); ctx.shadowColor = "#b91c1c"; ctx.shadowBlur = 18 + br * 22;
    rr(cx - S / 2, cy - S / 2, S, S, 16); ctx.fillStyle = "#1f1f23"; ctx.fill();
    ctx.lineWidth = 3; ctx.strokeStyle = "#b91c1c"; ctx.stroke(); ctx.restore();
    rr(cx - 17, cy - 17, 34, 34, 6); ctx.fillStyle = "#a1a1aa"; ctx.fill();
    // tiles
    ctx.font = "500 11px Inter, system-ui, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const n of nodes) {
      const a = still ? 1 : Math.min(1, Math.max(0, (now - n.born) / 600));
      if (a <= 0) continue;
      const c = COL[n.st], w = 84, h = 30, hv = nodes[hover] === n;
      ctx.save(); ctx.globalAlpha = a;
      if (hv) { ctx.shadowColor = c; ctx.shadowBlur = 16; }
      rr(n.x - w / 2, n.y - h / 2, w, h, 8); ctx.fillStyle = "#141414"; ctx.fill();
      ctx.lineWidth = hv ? 2.5 : 1.5; ctx.strokeStyle = c; ctx.stroke(); ctx.shadowBlur = 0;
      ctx.fillStyle = "#e5e5e5";
      let label = n.name; while (label.length > 3 && ctx.measureText(label).width > w - 12) label = label.slice(0, -2) + "…";
      ctx.fillText(label, n.x, n.y); ctx.restore();
    }
    raf = still ? 0 : requestAnimationFrame(frame);
  }
  function kick() { if (!raf) raf = requestAnimationFrame(frame); }
  function hit(e) {
    for (let k = nodes.length - 1; k >= 0; k--) { const n = nodes[k]; if (Math.abs(e.clientX - n.x) <= 42 && Math.abs(e.clientY - n.y) <= 15) return k; }
    return -1;
  }
  const LBL = { on: "Connected", off: "Not connected", unk: "Not detected" };
  function onMove(e) {
    const k = hit(e);
    if (k !== hover) { hover = k; if (reduce.matches) { raf = 0; kick(); } }
    cv.style.cursor = k >= 0 ? "pointer" : "default";
    if (k < 0) { tip.style.display = "none"; return; }
    const n = nodes[k];
    tip.innerHTML = "";
    const b = document.createElement("b"); b.textContent = n.name;
    const u = document.createElement("div"); u.textContent = n.url || "(no address set)";
    const s = document.createElement("div"); s.textContent = LBL[n.st]; s.style.color = COL[n.st];
    tip.append(b, u, s);
    tip.style.display = "block"; tip.style.left = (e.clientX + 14) + "px"; tip.style.top = (e.clientY + 14) + "px";
  }
  function onClick(e) {
    const k = hit(e); if (k < 0) return;
    const n = nodes[k]; if (n.url) window.open(n.url, "_blank", "noopener");
  }
  async function refresh() {
    try { await checkAll(); } catch (e) {}
    nodes.forEach(n => { n.st = statusOf(n.i); });
    if (reduce.matches) { raf = 0; kick(); }
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  function open() {
    if (ov) return;
    ov = document.createElement("div"); ov.id = "coremap";
    ov.innerHTML = '<canvas></canvas><button class="cm-close" title="Close (Esc)" aria-label="Close Core Map">✕</button>' +
      '<div class="cm-legend"><span style="--c:#22c55e">Connected</span><span style="--c:#ff2a2a">Not connected</span><span style="--c:#facc15">Not detected</span></div><div class="cm-tip"></div>';
    document.body.appendChild(ov);
    cv = ov.querySelector("canvas"); ctx = cv.getContext("2d"); tip = ov.querySelector(".cm-tip");
    ov.querySelector(".cm-close").onclick = close;
    cv.addEventListener("mousemove", onMove); cv.addEventListener("click", onClick);
    addEventListener("resize", onResize); addEventListener("keydown", onKey);
    t0 = performance.now(); resize(); build(); kick(); refresh();
    poll = setInterval(refresh, 5000);
  }
  function onResize() { resize(); if (reduce.matches) { raf = 0; kick(); } }
  function close() {
    if (!ov) return;
    cancelAnimationFrame(raf); raf = 0; clearInterval(poll);
    removeEventListener("resize", onResize); removeEventListener("keydown", onKey);
    ov.remove(); ov = null; nodes = []; hover = -1;
  }
  window.openCoreMap = open; window.closeCoreMap = close;
})();
