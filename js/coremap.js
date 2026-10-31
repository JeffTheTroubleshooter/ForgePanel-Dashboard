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
  const imgs = new Map();
  function imgFor(src) {
    if (!src) return null;
    if (!imgs.has(src)) { const im = new Image(); im.onload = () => { if (reduce.matches) { raf = 0; kick(); } }; im.src = src; imgs.set(src, im); }
    return imgs.get(src);
  }
  let groups = [];   // group cores: { name, members: [node], x, y, st, born }
  function build() {
    const all = getAllServices();
    const old = new Map(nodes.map(n => [n.key, n]));
    let sec = null;
    try { sec = sections(); } catch (e) { sec = null; }
    const mk = (i) => {
      const s = all[i], key = s.key || ("c:" + i), o = old.get(key);
      let src = null; try { src = safeImgSrc(cfgFor(s).logo || s.logo, ""); } catch (e) { src = s.logo || null; }
      return { key, i, name: s.name, img: imgFor(src), url: urlFor(s), st: statusOf(i), born: o ? o.born : performance.now() + i * 40, g: null };
    };
    const oldG = new Map(groups.map(g => [g.name, g]));
    groups = []; nodes = [];
    const ung = sec ? sec.ung : all.map((s, i) => i);
    for (const i of ung) nodes.push(mk(i));
    if (sec) sec.groups.forEach(({ g, items }, gi) => {
      if (!items.length) return;
      const nm = g.name || "Group", o = oldG.get(nm);
      const G = { name: nm, members: [], born: o ? o.born : performance.now() + gi * 80 };
      for (const i of items) { const n = mk(i); n.g = G; G.members.push(n); nodes.push(n); }
      groups.push(G);
    });
    groupStatus(); layout();
  }
  // Worst member status: down > not detected > online.
  function groupStatus() {
    for (const G of groups) {
      const ss = G.members.map(n => n.st);
      G.st = ss.includes("off") ? "off" : ss.includes("unk") ? "unk" : "on";
    }
  }
  function ring(list, cx, cy, minR, gap, rot) {
    let idx = 0, r_ = 0; const n = list.length;
    while (idx < n) {
      const r = minR + r_ * gap;
      const cap = Math.max(6, Math.floor((2 * Math.PI * r) / 96));
      const cnt = Math.min(cap, n - idx);
      for (let k = 0; k < cnt; k++, idx++) {
        const a = -Math.PI / 2 + rot + (k / cnt) * Math.PI * 2 + r_ * 0.35;
        list[idx].x = cx + Math.cos(a) * r; list[idx].y = cy + Math.sin(a) * r;
        list[idx].px = cx; list[idx].py = cy;
      }
      r_++;
    }
  }
  function layout() {
    const cx = W / 2, cy = H / 2, m = Math.min(W, H);
    const ung = nodes.filter(n => !n.g);
    if (!groups.length) { ring(ung, cx, cy, m * 0.22, 78, 0); return; }
    ring(ung, cx, cy, Math.max(95, m * 0.14), 60, 0);
    const R = Math.max(m * 0.34, ung.length ? m * 0.3 : m * 0.22);
    groups.forEach((G, k) => {
      const a = -Math.PI / 2 + Math.PI / Math.max(groups.length, 1) + (k / groups.length) * Math.PI * 2;
      G.x = cx + Math.cos(a) * R * (W > H ? Math.min(1.5, W / H) : 1); G.y = cy + Math.sin(a) * R;
      G.x = Math.min(W - 140, Math.max(140, G.x)); G.y = Math.min(H - 110, Math.max(150, G.y));
      const gr = Math.max(85, Math.min(m * 0.15, 14 * G.members.length + 40));
      ring(G.members, G.x, G.y, gr, 50, a + Math.PI / G.members.length);
    });
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
    // group core -> main core links (worst member status)
    ctx.lineWidth = 2.5;
    for (const G of groups) {
      const a = still ? 1 : Math.min(1, Math.max(0, (now - G.born) / 600)), c = COL[G.st];
      ctx.save(); ctx.strokeStyle = c;
      if (G.st === "on") { ctx.globalAlpha = 0.6 * a; }
      else if (G.st === "off") { ctx.globalAlpha = a * (still ? 0.7 : 0.75); ctx.setLineDash([12, 6]); ctx.lineDashOffset = -t * 30; }
      else { ctx.globalAlpha = a * (still ? 0.6 : 0.4 + 0.3 * Math.sin(t * 1.5)); ctx.setLineDash([4, 8]); ctx.lineDashOffset = -t * 8; }
      ctx.beginPath(); ctx.moveTo(G.x, G.y); ctx.lineTo(cx, cy); ctx.stroke(); ctx.restore();
    }
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
      ctx.beginPath(); ctx.moveTo(n.x, n.y); ctx.lineTo(n.px, n.py); ctx.stroke(); ctx.restore();
      if (n.st === "on" && !still) {          // data pulses toward the core
        for (let p = 0; p < 2; p++) {
          const f = ((t * 0.5 + n.i * 0.13 + p * 0.5) % 1);
          const px = n.x + (n.px - n.x) * f, py = n.y + (n.py - n.y) * f;
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
    // group cores (smaller copy of the core, labelled with the group name)
    for (const G of groups) {
      const a = still ? 1 : Math.min(1, Math.max(0, (now - G.born) / 600)), s2 = 46;
      ctx.save(); ctx.globalAlpha = a; ctx.shadowColor = "#b91c1c"; ctx.shadowBlur = 10 + br * 12;
      rr(G.x - s2 / 2, G.y - s2 / 2, s2, s2, 10); ctx.fillStyle = "#1f1f23"; ctx.fill();
      ctx.lineWidth = 2; ctx.strokeStyle = "#b91c1c"; ctx.stroke(); ctx.shadowBlur = 0;
      rr(G.x - 9, G.y - 9, 18, 18, 4); ctx.fillStyle = "#a1a1aa"; ctx.fill();
      ctx.font = "600 11px Inter, system-ui, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "top";
      const lw = Math.min(140, ctx.measureText(G.name).width + 14);
      rr(G.x - lw / 2, G.y + s2 / 2 + 5, lw, 18, 6); ctx.fillStyle = "#141414"; ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = "#2a2a2a"; ctx.stroke();
      ctx.fillStyle = "#e5e5e5"; ctx.fillText(G.name, G.x, G.y + s2 / 2 + 8, 130); ctx.restore();
    }
    // tiles
    ctx.font = "500 11px Inter, system-ui, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const n of nodes) {
      const a = still ? 1 : Math.min(1, Math.max(0, (now - n.born) / 600));
      if (a <= 0) continue;
      const c = COL[n.st], S = 46, hv = nodes[hover] === n;
      ctx.save(); ctx.globalAlpha = a;
      ctx.shadowColor = c; ctx.shadowBlur = hv ? 18 : 8;
      rr(n.x - S / 2, n.y - S / 2, S, S, 10); ctx.fillStyle = "#141414"; ctx.fill();
      ctx.lineWidth = hv ? 2.5 : 1.8; ctx.strokeStyle = c; ctx.stroke(); ctx.shadowBlur = 0;
      const im = n.img;
      if (im && im.complete && im.naturalWidth) {
        const k = Math.min(30 / im.naturalWidth, 30 / im.naturalHeight), iw = im.naturalWidth * k, ih = im.naturalHeight * k;
        ctx.drawImage(im, n.x - iw / 2, n.y - ih / 2, iw, ih);
      } else {
        ctx.font = "600 18px Inter, system-ui, sans-serif"; ctx.fillStyle = "#e5e5e5";
        ctx.fillText((n.name || "?").trim().charAt(0).toUpperCase() || "?", n.x, n.y + 1);
      }
      ctx.font = "500 10px Inter, system-ui, sans-serif"; ctx.fillStyle = "#a1a1aa";
      let label = n.name || ""; while (label.length > 3 && ctx.measureText(label).width > 80) label = label.slice(0, -2) + "…";
      ctx.fillText(label, n.x, n.y + S / 2 + 9); ctx.restore();
    }
    raf = still ? 0 : requestAnimationFrame(frame);
  }
  function kick() { if (!raf) raf = requestAnimationFrame(frame); }
  function hit(e) {
    for (let k = nodes.length - 1; k >= 0; k--) { const n = nodes[k]; if (Math.abs(e.clientX - n.x) <= 24 && Math.abs(e.clientY - n.y) <= 24) return k; }
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
    nodes.forEach(n => { n.st = statusOf(n.i); }); groupStatus();
    if (reduce.matches) { raf = 0; kick(); }
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  // Clip-path circle centred on the header logo, so the map grows out of / shrinks into it.
  function logoClip(r) {
    const b = document.getElementById("brand-btn");
    const rc = b ? b.getBoundingClientRect() : { left: innerWidth / 2, top: 40, width: 0, height: 0 };
    return "circle(" + r + " at " + (rc.left + rc.width / 2) + "px " + (rc.top + rc.height / 2) + "px)";
  }
  function setMode(on) {
    document.body.classList.toggle("cm-on", on);
    const b = document.getElementById("brand-btn"); if (b) b.setAttribute("aria-pressed", on ? "true" : "false");
    try { sessionStorage.setItem("fp_view", on ? "map" : "dash"); } catch (e) {}
  }
  let closing = 0;
  function open(animate) {
    if (closing) { clearTimeout(closing); closing = 0; teardown(); }
    if (ov) return;
    setMode(true);
    ov = document.createElement("div"); ov.id = "coremap";
    ov.innerHTML = '<canvas></canvas>' +
      '<div class="cm-legend"><span style="--c:#22c55e">Connected</span><span style="--c:#ff2a2a">Not connected</span><span style="--c:#facc15">Not detected</span></div><div class="cm-tip"></div>';
    document.body.appendChild(ov);
    cv = ov.querySelector("canvas"); ctx = cv.getContext("2d"); tip = ov.querySelector(".cm-tip");
    cv.addEventListener("mousemove", onMove); cv.addEventListener("click", onClick);
    addEventListener("resize", onResize); addEventListener("keydown", onKey);
    t0 = performance.now(); resize(); build(); kick(); refresh();
    if (animate !== false && !reduce.matches) {
      ov.style.clipPath = logoClip("24px"); ov.classList.add("cm-shut");
      ov.getBoundingClientRect();
      requestAnimationFrame(() => { if (!ov) return; ov.style.clipPath = logoClip("150vmax"); ov.classList.remove("cm-shut"); });
    }
    poll = setInterval(refresh, 5000);
  }
  function onResize() { resize(); if (reduce.matches) { raf = 0; kick(); } }
  function close() {
    if (!ov || closing) return;
    setMode(false);
    removeEventListener("keydown", onKey);
    if (reduce.matches) { teardown(); return; }
    ov.style.pointerEvents = "none";
    ov.style.clipPath = logoClip("150vmax"); ov.getBoundingClientRect();
    ov.style.clipPath = logoClip("24px"); ov.classList.add("cm-shut");
    closing = setTimeout(() => { closing = 0; teardown(); }, 560);
  }
  function teardown() {
    if (!ov) return;
    cancelAnimationFrame(raf); raf = 0; clearInterval(poll);
    removeEventListener("resize", onResize); removeEventListener("keydown", onKey);
    ov.remove(); ov = null; nodes = []; groups = []; hover = -1;
  }
  function toggle() { (ov && !closing) ? close() : open(); }
  window.openCoreMap = open; window.closeCoreMap = close; window.toggleCoreMap = toggle;
  // Initial view: a fresh login expands into the Core Map; a refresh keeps the session's last view.
  addEventListener("load", () => {
    let fresh = false, view = null;
    try { fresh = sessionStorage.getItem("fp_fresh_login") === "1"; sessionStorage.removeItem("fp_fresh_login"); view = sessionStorage.getItem("fp_view"); } catch (e) {}
    if (fresh) setTimeout(() => open(true), 350);
    else if (view === "map") open(false);
  });
})();
