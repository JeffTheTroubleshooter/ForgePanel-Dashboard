// Core Map: full-screen animated map of every service around the ForgePanel core.
// Ungrouped services ring the main core; each tile group gets its own smaller core on an
// outer orbit with its members fanned outward. Statuses come from the dashboard's status
// dots (filled via /api/check) and are re-checked every 5 s while open.
// Wheel = zoom, drag = pan, double-click = reset view. Plain canvas, no libraries.
(function () {
  const COL = { on: "#22c55e", off: "#ff2a2a", unk: "#facc15" };
  const LBL = { on: "Connected", off: "Not connected", unk: "Not detected" };
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  const TILE = 46, GAP = 96, GCORE = 48, MCORE = 84;
  let ov, cv, ctx, tip, raf = 0, poll = 0, t0 = 0, W = 0, H = 0, dpr = 1;
  let nodes = [], groups = [], hover = null, sig = "";
  let fcx = 0, fcy = 0, fit = 1, zoom = 1, panX = 0, panY = 0, drag = null;

  // ── data ──
  function statusOf(i) {
    const el = document.getElementById("status-" + i);
    if (!el) return "unk";
    if (el.classList.contains("online")) return "on";
    if (el.classList.contains("offline")) return "off";
    return "unk";
  }
  function msOf(i) {
    const el = document.getElementById("status-" + i);
    const v = el && el.dataset ? parseFloat(el.dataset.ms) : NaN;
    return isNaN(v) ? null : v;
  }
  // up -> down: remember when, so the link pulses red for a few seconds
  function setSt(n, st, now) {
    if (n.st === "on" && st === "off") n.failAt = now;
    n.st = st; n.ms = msOf(n.i);
  }
  const imgs = new Map();
  function imgFor(src) {
    if (!src) return null;
    if (!imgs.has(src)) { const im = new Image(); im.onload = redraw; im.src = src; imgs.set(src, im); }
    return imgs.get(src);
  }
  function build() {
    const all = getAllServices();
    let sec = null;
    try { sec = sections(); } catch (e) { sec = null; }
    const ungIdx = sec ? sec.ung : all.map((s, i) => i);
    const grp = sec ? sec.groups.filter(x => x.items.length) : [];
    const newSig = JSON.stringify([ungIdx.map(i => all[i].key), grp.map(x => [x.g.id, x.g.name, x.items.map(i => all[i].key)])]);
    if (newSig === sig) { const t = performance.now(); nodes.forEach(n => setSt(n, statusOf(n.i), t)); groupStatus(); return; }
    sig = newSig;
    const now = performance.now();
    const oldN = new Map(nodes.map(n => [n.key, n])), oldG = new Map(groups.map(g => [g.id, g]));
    let seq = 0;
    const mk = (i, G) => {
      const s = all[i], key = s.key || ("c:" + i), o = oldN.get(key);
      let src = null; try { src = safeImgSrc(cfgFor(s).logo || s.logo, ""); } catch (e) { src = s.logo || null; }
      const n = o || { key, x: G ? G.x : 0, y: G ? G.y : 0, born: now + 120 + (seq++) * 55, ph: Math.random() * 6.28 };
      Object.assign(n, { i, name: s.name, img: imgFor(src), url: urlFor(s), g: G });
      setSt(n, statusOf(i), now);
      return n;
    };
    nodes = []; groups = [];
    grp.forEach(({ g }, k) => {
      const id = g.id || g.name, o = oldG.get(id);
      const G = o || { id, x: 0, y: 0, born: now + k * 90, ph: Math.random() * 6.28 };
      G.name = g.name || "Group"; G.members = []; groups.push(G);
    });
    for (const i of ungIdx) nodes.push(mk(i, null));
    grp.forEach(({ items }, k) => { for (const i of items) { const n = mk(i, groups[k]); groups[k].members.push(n); nodes.push(n); } });
    groupStatus(); layout();
  }
  function groupStatus() {   // worst member status: down > not detected > online
    for (const G of groups) {
      const ss = G.members.map(n => n.st);
      G.st = ss.includes("off") ? "off" : ss.includes("unk") ? "unk" : "on";
    }
  }

  // ── layout (world coordinates, main core at 0,0) ──
  function layout() {
    const ung = nodes.filter(n => !n.g), G = groups.length;
    const slots = ung.length + G;
    // ring of slots around the main core; each group takes one slot so its link line runs through a gap
    const r0 = Math.max(MCORE / 2 + 95, (slots * GAP) / (2 * Math.PI));
    const gSlot = new Set();
    for (let k = 0; k < G; k++) gSlot.add(Math.round((k * slots) / G + (slots / G) / 2) % slots);
    let ui = 0, gi = 0;
    const gAng = [];
    for (let s = 0; s < slots; s++) {
      const a = -Math.PI / 2 + (s / Math.max(slots, 1)) * Math.PI * 2;
      if (gSlot.has(s) && gi < G) { gAng.push(a); gi++; }
      else if (ui < ung.length) { const n = ung[ui++]; n.tx = Math.cos(a) * r0; n.ty = Math.sin(a) * r0; n.px = 0; n.py = 0; }
    }
    while (ui < ung.length) { const n = ung[ui++]; n.tx = 0; n.ty = r0; n.px = n.py = 0; }   // safety
    // group fans: members on an arc facing away from the main core
    const ext = groups.map(Gr => {
      const m = Gr.members.length;
      Gr.rg = Math.max(GCORE / 2 + 62, m > 1 ? ((m - 1) * GAP) / (1.25 * Math.PI) : 0);
      Gr.span = m > 1 ? Math.min(1.25 * Math.PI, ((m - 1) * GAP) / Gr.rg) : 0;
      return Gr.rg + TILE / 2 + 26;
    });
    const eMax = Math.max(0, ...ext);
    let R = r0 + TILE / 2 + 40 + eMax;
    if (G > 1) R = Math.max(R, (2 * eMax + 40) / (2 * Math.sin(Math.PI / G)));
    groups.forEach((Gr, k) => {
      const a = gAng[k];
      Gr.tx = Math.cos(a) * R; Gr.ty = Math.sin(a) * R; Gr.ang = a;
      const m = Gr.members.length;
      Gr.members.forEach((n, j) => {
        const b = a + (m > 1 ? -Gr.span / 2 + (j / (m - 1)) * Gr.span : 0);
        n.tx = Gr.tx + Math.cos(b) * Gr.rg; n.ty = Gr.ty + Math.sin(b) * Gr.rg;
      });
    });
    relax();
    for (const n of nodes) if (n.g) { n.px = n.g.tx; n.py = n.g.ty; }
    computeFit();
    if (reduce.matches) { nodes.forEach(n => { n.x = n.tx; n.y = n.ty; }); groups.forEach(g => { g.x = g.tx; g.y = g.ty; }); }
  }
  // Light force pass: push overlapping tiles apart (cores are fixed obstacles), keep tiles off link lines.
  function relax() {
    const obs = [{ x: 0, y: 0, r: MCORE / 2 + 20 }].concat(groups.map(g => ({ x: g.tx, y: g.ty, r: GCORE / 2 + 30 })));
    const segs = groups.map(g => [0, 0, g.tx, g.ty]);
    const MIN = GAP - 8;
    for (let it = 0; it < 60; it++) {
      let moved = 0;
      for (let a = 0; a < nodes.length; a++) {
        const A = nodes[a];
        for (let b = a + 1; b < nodes.length; b++) {
          const B = nodes[b]; let dx = B.tx - A.tx, dy = B.ty - A.ty, d = Math.hypot(dx, dy);
          if (d < MIN) { if (d < 0.01) { dx = 1; dy = 0; d = 1; } const p = (MIN - d) / 2; A.tx -= dx / d * p; A.ty -= dy / d * p; B.tx += dx / d * p; B.ty += dy / d * p; moved++; }
        }
        for (const o of obs) {
          let dx = A.tx - o.x, dy = A.ty - o.y, d = Math.hypot(dx, dy), m = o.r + TILE / 2 + 10;
          if (d < m) { if (d < 0.01) { dx = 0; dy = 1; d = 1; } A.tx = o.x + dx / d * m; A.ty = o.y + dy / d * m; moved++; }
        }
        for (const [x1, y1, x2, y2] of segs) {
          const vx = x2 - x1, vy = y2 - y1, L = vx * vx + vy * vy || 1;
          const t = Math.max(0, Math.min(1, ((A.tx - x1) * vx + (A.ty - y1) * vy) / L));
          const qx = x1 + vx * t, qy = y1 + vy * t; let dx = A.tx - qx, dy = A.ty - qy, d = Math.hypot(dx, dy);
          const m = TILE / 2 + 12;
          if (d < m && t > 0.02 && t < 0.98) { if (d < 0.01) { dx = -vy; dy = vx; d = Math.hypot(dx, dy); } A.tx = qx + dx / d * m; A.ty = qy + dy / d * m; moved++; }
        }
      }
      if (!moved) break;
    }
  }
  function computeFit() {
    let x0 = -MCORE, x1 = MCORE, y0 = -MCORE, y1 = MCORE;
    for (const p of nodes.concat(groups)) { x0 = Math.min(x0, p.tx); x1 = Math.max(x1, p.tx); y0 = Math.min(y0, p.ty); y1 = Math.max(y1, p.ty); }
    const bw = x1 - x0 + TILE + 60, bh = y1 - y0 + TILE + 80;
    fcx = (x0 + x1) / 2; fcy = (y0 + y1) / 2 + 8;
    fit = Math.min(1.15, (W - 40) / bw, (H - topPad() - 70) / bh);
  }
  const topPad = () => 110;   // room for the header logo + bubble
  const baseX = () => W / 2 - fcx * fit * zoom, baseY = () => topPad() + (H - topPad() - 50) / 2 - fcy * fit * zoom;
  const originX = () => baseX() + panX, originY = () => baseY() + panY;
  const scale = () => fit * zoom;
  const toWorld = (sx, sy) => [(sx - originX()) / scale(), (sy - originY()) / scale()];

  // ── drawing ──
  function rr(x, y, w, h, r) { ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x, y, w, h, r) : ctx.rect(x, y, w, h); }
  function appear(now, born) { return reduce.matches ? 1 : Math.min(1, Math.max(0, (now - born) / 650)); }
  const ease = a => 1 - Math.pow(1 - a, 3);
  function drawLink(x1, y1, x2, y2, st, alpha, t, k, width, hot) {
    const c = COL[st], still = reduce.matches;
    ctx.save(); ctx.strokeStyle = c; ctx.lineWidth = width * (hot ? 1.8 : 1);
    if (st === "on") ctx.globalAlpha = (hot ? 1 : 0.55) * alpha;
    else if (st === "off") { ctx.globalAlpha = alpha * (hot ? 1 : still ? 0.7 : (Math.random() < 0.06 ? 0.15 : 0.75)); ctx.setLineDash([10, 6]); ctx.lineDashOffset = -t * 30; }
    else { ctx.globalAlpha = alpha * (hot ? 1 : still ? 0.6 : 0.35 + 0.3 * Math.sin(t * 1.5 + k)); ctx.setLineDash([4, 8]); ctx.lineDashOffset = -t * 8; }
    if (hot) { ctx.shadowColor = c; ctx.shadowBlur = 10; }
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); ctx.restore();
    if (st === "on" && !still) {
      for (let p = 0; p < 2; p++) {
        const f = (t * 0.5 + k * 0.13 + p * 0.5) % 1, px = x1 + (x2 - x1) * f, py = y1 + (y2 - y1) * f;
        ctx.save(); ctx.globalAlpha = alpha * (1 - f * 0.6); ctx.fillStyle = c; ctx.shadowColor = c; ctx.shadowBlur = 10;
        ctx.beginPath(); ctx.arc(px, py, 3, 0, Math.PI * 2); ctx.fill(); ctx.restore();
      }
    }
  }
  function drawCore(x, y, S, glow, a) {
    ctx.save(); ctx.globalAlpha = a; ctx.shadowColor = "#b91c1c"; ctx.shadowBlur = glow;
    rr(x - S / 2, y - S / 2, S, S, S * 0.19); ctx.fillStyle = "#1f1f23"; ctx.fill();
    ctx.lineWidth = S > 60 ? 3 : 2; ctx.strokeStyle = "#b91c1c"; ctx.stroke(); ctx.shadowBlur = 0;
    const s = S * 0.4; rr(x - s / 2, y - s / 2, s, s, s * 0.18); ctx.fillStyle = "#a1a1aa"; ctx.fill(); ctx.restore();
  }
  function frame(now) {
    raf = 0;
    const still = reduce.matches, t = still ? 0 : (now - t0) / 1000;
    let moving = false;
    // glide toward targets
    for (const p of groups.concat(nodes)) {
      if (still || now < p.born) { if (still) { p.x = p.tx; p.y = p.ty; } continue; }
      const dx = p.tx - p.x, dy = p.ty - p.y;
      if (Math.abs(dx) + Math.abs(dy) > 0.3) { p.x += dx * 0.11; p.y += dy * 0.11; moving = true; } else { p.x = p.tx; p.y = p.ty; }
    }
    const drift = (p, k) => still ? [p.x, p.y] : [p.x + Math.sin(t * 0.7 + p.ph) * 3, p.y + Math.cos(t * 0.55 + p.ph * 1.3) * 3 * k];
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#0a0a0a"; ctx.fillRect(0, 0, W, H);
    const sc = scale();
    ctx.setTransform(dpr * sc, 0, 0, dpr * sc, dpr * originX(), dpr * originY());
    const gpos = new Map(groups.map(g => [g, drift(g, 0.6)]));
    // group -> main links
    groups.forEach((g, k) => { const [x, y] = gpos.get(g); drawLink(x, y, 0, 0, g.st, ease(appear(now, g.born)), t, k, 2.5, hover && hover.g === g); });
    // service links
    const npos = new Map(nodes.map(n => [n, drift(n, 1)]));
    nodes.forEach(n => {
      const a = ease(appear(now, n.born)); if (a <= 0) return;
      const [x, y] = npos.get(n), [px, py] = n.g ? gpos.get(n.g) : [0, 0];
      drawLink(x, y, px, py, n.st, a, t, n.i, 2, hover === n || (hover && hover.link === n));
      const f = failPulse(n, now);
      if (f > 0) {   // red pulse along the link right after an up -> down transition
        const w = still ? 1 : 0.5 + 0.5 * Math.sin((now - n.failAt) / 110);
        ctx.save(); ctx.globalAlpha = a * f * w; ctx.strokeStyle = COL.off; ctx.shadowColor = COL.off; ctx.shadowBlur = 18; ctx.lineWidth = 6;
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(px, py); ctx.stroke();
        ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(x, y, TILE * 0.62 + (1 - f) * 14, 0, Math.PI * 2); ctx.stroke(); ctx.restore();
        moving = true;
      }
    });
    // cores
    const br = still ? 0.5 : 0.5 + 0.5 * Math.sin(t * 1.6);
    drawCore(0, 0, MCORE, 18 + br * 22, 1);
    ctx.font = "600 11px Inter, system-ui, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const g of groups) {
      const a = ease(appear(now, g.born)), [x, y] = gpos.get(g);
      drawCore(x, y, GCORE, 10 + br * 12, a);
      ctx.save(); ctx.globalAlpha = a; ctx.font = "600 11px Inter, system-ui, sans-serif";
      const lw = Math.min(140, ctx.measureText(g.name).width + 14);
      rr(x - lw / 2, y + GCORE / 2 + 5, lw, 18, 6); ctx.fillStyle = "#141414"; ctx.fill(); ctx.lineWidth = 1; ctx.strokeStyle = "#2a2a2a"; ctx.stroke();
      ctx.fillStyle = "#e5e5e5"; ctx.fillText(g.name, x, y + GCORE / 2 + 14, 130); ctx.restore();
    }
    // tiles (hovered one last so it sits on top)
    const hn = hover && !hover.link ? hover : null;
    const order = hn ? nodes.filter(n => n !== hn).concat([hn]) : nodes;
    for (const n of order) {
      const a = ease(appear(now, n.born)); if (a <= 0) continue;
      const [x, y] = npos.get(n), c = COL[n.st], hv = hover === n;
      n.hs = (n.hs || 1) + ((hv ? 1.22 : 1) - (n.hs || 1)) * (still ? 1 : 0.25);
      if (Math.abs(n.hs - (hv ? 1.22 : 1)) > 0.005) moving = true;
      const S = TILE * n.hs * (0.6 + 0.4 * a);
      ctx.save(); ctx.globalAlpha = a; ctx.shadowColor = c; ctx.shadowBlur = hv ? 22 : 8;
      rr(x - S / 2, y - S / 2, S, S, 10 * n.hs); ctx.fillStyle = "#141414"; ctx.fill();
      ctx.lineWidth = hv ? 2.5 : 1.8; ctx.strokeStyle = c; ctx.stroke(); ctx.shadowBlur = 0;
      const im = n.img, I = 30 * n.hs * (0.6 + 0.4 * a);
      if (im && im.complete && im.naturalWidth) {
        const k = Math.min(I / im.naturalWidth, I / im.naturalHeight); ctx.drawImage(im, x - im.naturalWidth * k / 2, y - im.naturalHeight * k / 2, im.naturalWidth * k, im.naturalHeight * k);
      } else {
        ctx.font = "600 18px Inter, system-ui, sans-serif"; ctx.fillStyle = "#e5e5e5";
        ctx.fillText(((n.name || "?").trim().charAt(0) || "?").toUpperCase(), x, y + 1);
      }
      ctx.font = (hv ? "600 " : "500 ") + "10px Inter, system-ui, sans-serif"; ctx.fillStyle = hv ? "#e5e5e5" : "#a1a1aa";
      let label = n.name || ""; while (label.length > 3 && ctx.measureText(label).width > 82) label = label.slice(0, -2) + "…";
      ctx.fillText(label, x, y + S / 2 + 9); ctx.restore();
    }
    if (!still || moving) raf = requestAnimationFrame(frame);
  }
  const PULSE = 4500;
  function failPulse(n, now) { return n.failAt && n.st === "off" ? Math.max(0, 1 - (now - n.failAt) / PULSE) : 0; }
  function kick() { if (!raf && cv) raf = requestAnimationFrame(frame); }
  function redraw() { if (reduce.matches) kick(); }

  // ── interaction ──
  function hit(sx, sy) {
    const [wx, wy] = toWorld(sx, sy);
    for (let k = nodes.length - 1; k >= 0; k--) { const n = nodes[k]; if (Math.abs(wx - n.x) <= TILE / 2 + 3 && Math.abs(wy - n.y) <= TILE / 2 + 3) return n; }
    for (const g of groups) if (Math.abs(wx - g.x) <= GCORE / 2 && Math.abs(wy - g.y) <= GCORE / 2) return { g, group: true };
    const tol = 6 / scale();
    for (const n of nodes) {   // hovering a link shows its service
      const px = n.g ? n.g.x : 0, py = n.g ? n.g.y : 0, dx = px - n.x, dy = py - n.y, L2 = dx * dx + dy * dy || 1;
      const u = Math.max(0, Math.min(1, ((wx - n.x) * dx + (wy - n.y) * dy) / L2));
      if (Math.hypot(wx - (n.x + u * dx), wy - (n.y + u * dy)) <= tol) return { link: n };
    }
    return null;
  }
  function showTip(e, n) {
    if (n && n.link) n = n.link;
    if (!n || n.group) { tip.style.display = "none"; return; }
    tip.innerHTML = "";
    const b = document.createElement("b"); b.textContent = n.name;
    const u = document.createElement("div"); u.textContent = n.url || "(no address set)";
    const s = document.createElement("div"); s.textContent = LBL[n.st]; s.style.color = COL[n.st];
    tip.append(b, u, s);
    const m = document.createElement("div"); m.className = "cm-ms";
    m.textContent = n.ms != null ? "Response: " + Math.round(n.ms) + " ms" : (n.st === "on" ? "Response: n/a" : "Response: -");
    tip.append(m);
    if (n.g) { const g = document.createElement("div"); g.textContent = "Group: " + n.g.name; g.style.color = "#a1a1aa"; tip.append(g); }
    tip.style.display = "block"; tip.style.left = (e.clientX + 14) + "px"; tip.style.top = (e.clientY + 14) + "px";
  }
  function onDown(e) {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, px: panX, py: panY, moved: false };
    cv.setPointerCapture && cv.setPointerCapture(e.pointerId);
  }
  function onMove(e) {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (drag.moved || Math.abs(dx) + Math.abs(dy) > 4) { drag.moved = true; panX = drag.px + dx; panY = drag.py + dy; cv.style.cursor = "grabbing"; tip.style.display = "none"; kick(); return; }
    }
    const h = hit(e.clientX, e.clientY), n = h && !h.group ? h : null;
    const same = n && hover && ((n.link && n.link === hover.link) || n === hover);
    if (!same && n !== hover) { hover = n; kick(); }
    cv.style.cursor = n && !n.link ? "pointer" : "grab";
    showTip(e, n);
  }
  function onUp(e) {
    const d = drag; drag = null; cv.style.cursor = "grab";
    if (!d || d.moved) return;
    const h = hit(e.clientX, e.clientY);
    if (h && !h.group && !h.link && h.url) window.open(h.url, "_blank", "noopener");
  }
  function onWheel(e) {
    e.preventDefault();
    const [wx, wy] = toWorld(e.clientX, e.clientY);
    zoom = Math.min(3, Math.max(0.4, zoom * Math.exp(-e.deltaY * 0.0015)));
    panX = e.clientX - baseX() - wx * scale(); panY = e.clientY - baseY() - wy * scale();
    kick();
  }
  function resetView() { zoom = 1; panX = 0; panY = 0; kick(); }
  function onLeave() { hover = null; tip.style.display = "none"; kick(); }
  function resize() {
    dpr = window.devicePixelRatio || 1; W = innerWidth; H = innerHeight;
    cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + "px"; cv.style.height = H + "px";
    computeFit();
  }
  function onResize() { resize(); kick(); }
  async function refresh() {
    try { await checkAll(); } catch (e) {}
    if (!ov) return;
    build(); kick();
  }
  function onKey(e) { if (e.key === "Escape") close(); }

  // ── view mode ──
  function logoClip(r) {
    const b = document.getElementById("brand-btn");
    const rc = b ? b.getBoundingClientRect() : { left: innerWidth / 2, top: 40, width: 0, height: 0 };
    return "circle(" + r + " at " + (rc.left + rc.width / 2) + "px " + (rc.top + rc.height / 2) + "px)";
  }
  function setMode(on) {
    document.body.classList.toggle("cm-on", on);
    const b = document.getElementById("brand-btn"); if (b) b.setAttribute("aria-pressed", on ? "true" : "false");
    const bub = document.getElementById("cm-bubble"); if (bub) bub.textContent = on ? "Return" : "Click me";
    if (!window.CM_READONLY) { try { sessionStorage.setItem("fp_view", on ? "map" : "dash"); } catch (e) {} }
  }
  let closing = 0;
  function open(animate) {
    if (closing) { clearTimeout(closing); closing = 0; teardown(); }
    if (ov) return;
    setMode(true);
    ov = document.createElement("div"); ov.id = "coremap";
    ov.innerHTML = '<canvas></canvas>' +
      '<div class="cm-legend"><span style="--c:#22c55e">Connected</span><span style="--c:#ff2a2a">Not connected</span><span style="--c:#facc15">Not detected</span><span class="cm-hint">Scroll to zoom · drag to pan · double-click to reset</span></div><div class="cm-tip"></div>';
    document.body.appendChild(ov);
    cv = ov.querySelector("canvas"); ctx = cv.getContext("2d"); tip = ov.querySelector(".cm-tip");
    cv.style.cursor = "grab"; cv.style.touchAction = "none";
    cv.addEventListener("pointerdown", onDown); cv.addEventListener("pointermove", onMove); cv.addEventListener("pointerup", onUp);
    cv.addEventListener("pointerleave", onLeave); cv.addEventListener("wheel", onWheel, { passive: false }); cv.addEventListener("dblclick", resetView);
    addEventListener("resize", onResize); addEventListener("keydown", onKey);
    zoom = 1; panX = panY = 0; sig = ""; nodes = []; groups = [];
    t0 = performance.now(); resize(); build(); kick(); refresh();
    if (animate !== false && !reduce.matches) {
      ov.style.clipPath = logoClip("24px"); ov.classList.add("cm-shut");
      ov.getBoundingClientRect();
      requestAnimationFrame(() => { if (!ov) return; ov.style.clipPath = logoClip("150vmax"); ov.classList.remove("cm-shut"); });
    }
    poll = setInterval(refresh, 5000);
  }
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
    ov.remove(); ov = null; cv = null; nodes = []; groups = []; hover = null; sig = "";
  }
  function toggle() { (ov && !closing) ? close() : open(); }
  window.openCoreMap = open; window.closeCoreMap = close; window.toggleCoreMap = toggle;
    addEventListener("load", () => {
    if (window.CM_READONLY) return;
    // A fresh login always lands on the dashboard; a refresh keeps the session's last view.
    let fresh = false, view = null;
    try { fresh = sessionStorage.getItem("fp_fresh_login") === "1"; sessionStorage.removeItem("fp_fresh_login"); view = sessionStorage.getItem("fp_view"); } catch (e) {}
    if (fresh) { try { sessionStorage.setItem("fp_view", "dash"); } catch (e) {} }
    else if (view === "map") open(false);
  });
})();
