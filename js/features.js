// ForgePanel dashboard extras: settings (notifications, public status page), Docker import,
// per-service check types + widgets, YAML export/import. Uses dashboard globals
// (saved, api, showModal, getAllServices, cfgFor, persist, render, checkAll, layout, newId).
let svcSecrets = {};
const SECRET_RE = /(key|token|secret|password|passwd|webhook|auth)/i;

function fxEl(id) { return document.getElementById(id); }
function fxInject() {
  if (fxEl("fx-settings")) return;
  const wrap = document.createElement("div");
  wrap.innerHTML = `
<div id="fx-settings" class="overlay" onclick="if (event.target === this) closeOverlay('fx-settings')">
  <div class="panel fx">
    <h3><span>Settings</span><button class="btn" onclick="closeOverlay('fx-settings')">Close</button></h3>
    <h4>Notifications</h4>
    <div class="sub">Alerts when a service goes down or recovers, and when a scan finds new services. Secrets are stored encrypted and never shown again in full.</div>
    <div class="fx-grid">
      <label class="fx-chk"><input type="checkbox" id="nt-dc-on"> Discord webhook</label>
      <input id="nt-dc-url" type="password" autocomplete="off" placeholder="https://discord.com/api/webhooks/…">
      <label class="fx-chk"><input type="checkbox" id="nt-nt-on"> ntfy</label>
      <div class="fx-row3"><input id="nt-nt-server" placeholder="https://ntfy.sh"><input id="nt-nt-topic" placeholder="topic"><input id="nt-nt-token" type="password" autocomplete="off" placeholder="access token (optional)"></div>
    </div>
    <div class="fx-opts">
      <label class="fx-chk"><input type="checkbox" id="nt-down"> Service down</label>
      <label class="fx-chk"><input type="checkbox" id="nt-up"> Service recovered</label>
      <label class="fx-chk"><input type="checkbox" id="nt-scan"> New hosts found by scans</label>
    </div>
    <div class="actions fx-act">
      <button class="btn primary" onclick="fxSaveNotify()">Save</button>
      <button class="btn" onclick="fxTestNotify()">Send test</button>
      <span class="fx-msg" id="nt-msg"></span>
    </div>
    <h4>Public status page</h4>
    <div class="sub">A read-only page at <a id="ps-link" href="/status" target="_blank" rel="noopener">/status</a> that anyone who can reach this server can open without signing in. Off by default.</div>
    <div class="fx-opts">
      <label class="fx-chk"><input type="checkbox" id="ps-on"> Enable /status</label>
      <label class="fx-chk"><input type="checkbox" id="ps-ips"> Show IPs and ports</label>
      <label class="fx-chk"><input type="checkbox" id="ps-map"> Show Core Map</label>
    </div>
    <div class="fx-grid"><span class="fx-l">Title</span><input id="ps-title" maxlength="80" placeholder="Service status"></div>
    <div class="actions fx-act"><button class="btn primary" onclick="fxSavePublic()">Save</button><span class="fx-msg" id="ps-msg"></span></div>
  </div>
</div>
<div id="fx-docker" class="overlay" onclick="if (event.target === this) closeOverlay('fx-docker')">
  <div class="panel fx">
    <h3><span>Import from Docker</span><button class="btn" onclick="closeOverlay('fx-docker')">Close</button></h3>
    <div class="sub" id="dk-sub">Reading the Docker socket…</div>
    <div id="dk-list"></div>
    <div class="actions fx-act" id="dk-act" hidden><button class="btn primary" onclick="fxDockerImport()">Import selected</button><span class="fx-msg" id="dk-msg"></span></div>
  </div>
</div>`;
  document.body.append(...wrap.children);
}

function fxMsg(id, text, bad) { const m = fxEl(id); if (m) { m.textContent = text; m.className = "fx-msg" + (bad ? " bad" : ""); } }

// ── settings ──
async function openSettings() {
  fxInject();
  fxEl("fx-settings").classList.add("show");
  fxMsg("nt-msg", ""); fxMsg("ps-msg", "");
  try {
    const n = await api("notify");
    fxEl("nt-dc-on").checked = n.discord.enabled;
    fxEl("nt-dc-url").value = ""; fxEl("nt-dc-url").placeholder = n.discord.set ? `Saved (${n.discord.hint}). Leave blank to keep.` : "https://discord.com/api/webhooks/…";
    fxEl("nt-nt-on").checked = n.ntfy.enabled;
    fxEl("nt-nt-server").value = n.ntfy.server; fxEl("nt-nt-topic").value = n.ntfy.topic;
    fxEl("nt-nt-token").value = ""; fxEl("nt-nt-token").placeholder = n.ntfy.tokenSet ? `Token saved (${n.ntfy.tokenHint}). Blank = keep` : "access token (optional)";
    fxEl("nt-down").checked = n.on_down; fxEl("nt-up").checked = n.on_recover; fxEl("nt-scan").checked = n.on_scan;
    const p = await api("public-status/config");
    fxEl("ps-on").checked = p.enabled; fxEl("ps-ips").checked = p.showIps; fxEl("ps-map").checked = p.showMap; fxEl("ps-title").value = p.title;
    fxEl("ps-link").href = location.origin + "/status"; fxEl("ps-link").textContent = location.origin + "/status";
  } catch (e) { if (!handleAuthError(e)) fxMsg("nt-msg", e.message, true); }
}
function fxNotifyBody() {
  const b = { on_down: fxEl("nt-down").checked, on_recover: fxEl("nt-up").checked, on_scan: fxEl("nt-scan").checked,
    discord: { enabled: fxEl("nt-dc-on").checked }, ntfy: { enabled: fxEl("nt-nt-on").checked, server: fxEl("nt-nt-server").value.trim(), topic: fxEl("nt-nt-topic").value.trim() } };
  const u = fxEl("nt-dc-url").value.trim(); if (u) b.discord.url = u;
  const t = fxEl("nt-nt-token").value.trim(); if (t) b.ntfy.token = t;
  return b;
}
async function fxSaveNotify() {
  try { await api("notify", "PUT", fxNotifyBody()); fxMsg("nt-msg", "Saved."); await openSettings(); fxMsg("nt-msg", "Saved."); }
  catch (e) { if (!handleAuthError(e)) fxMsg("nt-msg", e.message, true); }
}
async function fxTestNotify() {
  fxMsg("nt-msg", "Sending…");
  try {
    await api("notify", "PUT", fxNotifyBody());
    const r = await api("notify/test", "POST", {});
    const parts = Object.entries(r.results).map(([k, v]) => `${k}: ${v === "ok" ? "sent" : v}`);
    fxMsg("nt-msg", parts.join(" · "), Object.values(r.results).some(v => v !== "ok"));
  } catch (e) { if (!handleAuthError(e)) fxMsg("nt-msg", e.message, true); }
}
async function fxSavePublic() {
  try {
    const p = await api("public-status/config", "PUT", { enabled: fxEl("ps-on").checked, showIps: fxEl("ps-ips").checked, showMap: fxEl("ps-map").checked, title: fxEl("ps-title").value });
    fxMsg("ps-msg", p.enabled ? "Saved. /status is public." : "Saved. /status is off.");
  } catch (e) { if (!handleAuthError(e)) fxMsg("ps-msg", e.message, true); }
}

// ── Docker import ──
let dkItems = [];
async function openDocker() {
  fxInject();
  fxEl("fx-docker").classList.add("show");
  fxEl("dk-sub").textContent = "Reading the Docker socket…"; fxEl("dk-list").innerHTML = ""; fxEl("dk-act").hidden = true; fxMsg("dk-msg", "");
  let r;
  try { r = await api("docker"); } catch (e) { if (!handleAuthError(e)) fxEl("dk-sub").textContent = e.message; return; }
  if (!r.available) { fxEl("dk-sub").innerHTML = `<span class="fx-warn">Docker not available.</span> ${escapeHtml(r.error)}`; return; }
  const have = new Set(getAllServices().map(s => { const c = cfgFor(s); return c.ip ? `${c.ip}:${c.port}` : ""; }));
  dkItems = r.containers;
  if (!dkItems.length) { fxEl("dk-sub").textContent = "No running containers with forgepanel.* labels or published ports."; return; }
  fxEl("dk-sub").textContent = `${dkItems.length} container(s) on ${r.socket}. Labeled containers (forgepanel.name/url/icon/group) are pre-selected.`;
  fxEl("dk-list").innerHTML = dkItems.map((x, k) => {
    const s = x.suggested, dup = have.has(`${s.ip}:${s.port}`);
    const portSel = x.ports.length > 1 && !x.labeled ? `<select id="dk-port-${k}">${x.ports.map(p => `<option ${p === s.port ? "selected" : ""}>${p}</option>`).join("")}</select>` : `<span class="mono">${escapeHtml(s.port || "-")}</span>`;
    return `<div class="row"><input type="checkbox" id="dk-${k}" ${x.labeled && !dup ? "checked" : ""}>
      <span class="grow"><b>${escapeHtml(s.name)}</b> <span class="mono muted">${escapeHtml(x.image)}</span>${s.group ? ` <span class="tag">${escapeHtml(s.group)}</span>` : ""}${x.labeled ? ` <span class="tag new">label</span>` : ""}${dup ? ` <span class="muted">(already on dashboard)</span>` : ""}</span>
      <span class="mono">${escapeHtml(s.ip)}:</span>${portSel}</div>`;
  }).join("");
  fxEl("dk-act").hidden = false;
}
async function fxDockerImport() {
  const L = layout(); let n = 0;
  saved._custom = saved._custom || [];
  dkItems.forEach((x, k) => {
    if (!fxEl(`dk-${k}`).checked) return;
    const s = x.suggested, ps = fxEl(`dk-port-${k}`), port = ps ? parseInt(ps.value) : s.port;
    const icon = s.icon && !/^(\.\.\/|\/|https?:\/\/)/.test(s.icon) ? `../assets/icons/${s.icon.replace(/[^A-Za-z0-9_.\-]/g, "")}${/\./.test(s.icon) ? "" : ".png"}` : s.icon;
    const c = { id: newId(), name: s.name, desc: `Docker: ${x.image}`.slice(0, 120), ip: s.ip, port: port || 80, logo: safeImgSrc(icon, null), source: "docker" };
    if (s.scheme) c.scheme = s.scheme;
    saved._custom.push(c); n++;
    if (s.group) {
      let g = L.groups.find(g => (g.name || "").toLowerCase() === s.group.toLowerCase());
      if (!g) { g = { id: "g" + newId(), name: s.group.slice(0, 60), items: [], collapsed: false }; L.groups.push(g); }
      g.items = (g.items || []).concat(["c:" + c.id]);
    }
  });
  if (!n) { fxMsg("dk-msg", "Nothing selected.", true); return; }
  await persist(); render(); checkAll(); fxRefreshWidgets();
  closeOverlay("fx-docker");
  await showModal("Docker Import", `Added ${n} service tile(s).`, "OK");
}

// ── per-service check type + widget ──
async function fxLoadSecrets() { try { svcSecrets = (await api("service-secrets")).services || {}; } catch (e) { svcSecrets = {}; } }
async function configureService(i) {
  document.querySelectorAll(".cmenu.open").forEach(x => x.classList.remove("open"));
  const s = getAllServices()[i]; if (!s) return;
  if (!s.custom && !saved[s.name]) saved[s.name] = getConfig(s.name);
  const c = cfgFor(s), ck = c.check || {}, w = c.widget || {}, sec = svcSecrets[s.key] || {};
  const opt = (v, cur, label) => `<option value="${v}" ${v === cur ? "selected" : ""}>${label}</option>`;
  const html = `<div class="fx-cfg">
    <label>Check type <select id="cf-type" onchange="fxCfgShow()">${opt("http", ck.type || "http", "HTTP(S) (default)")}${opt("tcp", ck.type, "TCP port open")}${opt("keyword", ck.type, "HTTP keyword")}${opt("dns", ck.type, "DNS query")}${opt("push", ck.type, "Push (service calls ForgePanel)")}</select></label>
    <div class="cf-keyword"><label>Keyword <input id="cf-kw" value="${escapeHtml(ck.keyword || "")}" placeholder="text the page must contain"></label>
      <label>Path <input id="cf-path" value="${escapeHtml(ck.path || "/")}"></label><label class="fx-chk"><input type="checkbox" id="cf-inv" ${ck.invert ? "checked" : ""}> Down when found</label></div>
    <div class="cf-dns"><label>Name to resolve <input id="cf-host" value="${escapeHtml(ck.host || "")}" placeholder="example.com"></label><span class="muted">Queries the service IP as a DNS server (port <input id="cf-dport" style="width:4.5rem" value="${escapeHtml(ck.dns_port || 53)}">).</span></div>
    <div class="cf-push"><label>Expect a push every <input id="cf-int" style="width:5rem" value="${escapeHtml(ck.interval || 60)}"> seconds</label>
      <div class="mono fx-push" id="cf-pushurl">${sec.pushPath ? escapeHtml(location.origin + sec.pushPath) : "No push URL yet."}</div>
      <div><button type="button" class="btn" onclick="fxPush('${escapeHtml(s.key)}','new')">${sec.pushPath ? "Regenerate URL" : "Create push URL"}</button> <button type="button" class="btn" onclick="fxPush('${escapeHtml(s.key)}','revoke')">Revoke</button></div>
      <div class="muted">Call it with <span class="mono">curl -fsS URL</span> from a cron job or script. Add <span class="mono">?status=down</span> to report a failure.</div></div>
    <hr>
    <label>Tile widget <select id="cf-wtype" onchange="fxCfgShow()">${opt("", w.type || "", "None")}${opt("jellyfin", w.type, "Jellyfin – now playing")}${opt("pihole", w.type, "Pi-hole – blocked")}${opt("adguard", w.type, "AdGuard Home – blocked")}${opt("proxmox", w.type, "Proxmox – node CPU/RAM")}</select></label>
    <div class="cf-w"><label>API URL <input id="cf-wurl" value="${escapeHtml(w.url || "")}" placeholder="blank = this tile's address"></label>
      <label class="cf-wuser">Username <input id="cf-wuser" value="${escapeHtml(w.user || "")}"></label>
      <label class="cf-wnode">Node <input id="cf-wnode" value="${escapeHtml(w.node || "")}" placeholder="all nodes"></label>
      <label><span id="cf-klabel">API key</span> <input id="cf-wkey" type="password" autocomplete="off" placeholder="${sec.keySet ? `saved ${escapeHtml(sec.keyHint)} (blank = keep)` : ""}"></label>
      ${sec.keySet ? `<label class="fx-chk"><input type="checkbox" id="cf-wclear"> Remove saved key</label>` : ""}</div>
  </div>`;
  const pr = showModal(`${s.name}: check & widget`, "", "Save", false, null, null, null, {
    html, collect: () => ({
      check: { type: fxEl("cf-type").value, keyword: fxEl("cf-kw").value.trim(), path: fxEl("cf-path").value.trim() || "/", invert: fxEl("cf-inv").checked,
        host: fxEl("cf-host").value.trim(), dns_port: parseInt(fxEl("cf-dport").value) || 53, interval: Math.max(10, parseInt(fxEl("cf-int").value) || 60) },
      widget: { type: fxEl("cf-wtype").value, url: fxEl("cf-wurl").value.trim(), user: fxEl("cf-wuser").value.trim(), node: fxEl("cf-wnode").value.trim() },
      key: fxEl("cf-wkey").value, clear: !!(fxEl("cf-wclear") && fxEl("cf-wclear").checked) }) });
  fxCfgShow();
  const r = await pr;
  if (!r) return;
  const ck2 = { type: r.check.type };
  if (r.check.type === "keyword") Object.assign(ck2, { keyword: r.check.keyword, path: r.check.path, invert: r.check.invert });
  if (r.check.type === "dns") Object.assign(ck2, { host: r.check.host, dns_port: r.check.dns_port });
  if (r.check.type === "push") ck2.interval = r.check.interval;
  if (ck2.type === "http") delete c.check; else c.check = ck2;
  if (r.widget.type) { const w2 = { type: r.widget.type }; ["url", "user", "node"].forEach(k => { if (r.widget[k]) w2[k] = r.widget[k]; }); c.widget = w2; } else delete c.widget;
  if (ck2.type === "push" && !(svcSecrets[s.key] || {}).pushPath) await fxPush(s.key, "new", true);
  if (r.key || r.clear) { try { svcSecrets[s.key] = await api("service-secrets", "POST", { id: s.key, widgetKey: r.key || undefined, clearKey: r.clear && !r.key }); } catch (e) { handleAuthError(e); } }
  await persist(); render(); checkAll(); refreshUptime(); fxRefreshWidgets();
  if (ck2.type === "push") await showModal("Push URL", `Have ${s.name} call this URL at least every ${ck2.interval} s:\n${location.origin + svcSecrets[s.key].pushPath}`, "OK");
}
function fxCfgShow() {
  const t = fxEl("cf-type"), w = fxEl("cf-wtype"); if (!t) return;
  document.querySelectorAll(".fx-cfg .cf-keyword").forEach(e => e.hidden = t.value !== "keyword");
  document.querySelectorAll(".fx-cfg .cf-dns").forEach(e => e.hidden = t.value !== "dns");
  document.querySelectorAll(".fx-cfg .cf-push").forEach(e => e.hidden = t.value !== "push");
  document.querySelectorAll(".fx-cfg .cf-w").forEach(e => e.hidden = !w.value);
  document.querySelectorAll(".fx-cfg .cf-wuser").forEach(e => e.hidden = w.value !== "adguard");
  document.querySelectorAll(".fx-cfg .cf-wnode").forEach(e => e.hidden = w.value !== "proxmox");
  const kl = fxEl("cf-klabel"); if (kl) kl.textContent = { jellyfin: "API key", pihole: "Password / API token", adguard: "Password", proxmox: "API token (user@realm!id=secret)" }[w.value] || "API key";
}
async function fxPush(key, what, quiet) {
  try {
    const r = await api("service-secrets", "POST", { id: key, push: what });
    svcSecrets[key] = r;
    const el = fxEl("cf-pushurl"); if (el) el.textContent = r.pushPath ? location.origin + r.pushPath : "No push URL.";
  } catch (e) { if (!handleAuthError(e) && !quiet) await showModal("Push URL", e.message, "OK"); }
}

async function fxRefreshWidgets() {
  const all = getAllServices();
  const ids = all.filter(s => (cfgFor(s).widget || {}).type).map(s => s.key);
  all.forEach((s, i) => { const el = fxEl(`widget-${i}`); if (el && !(cfgFor(s).widget || {}).type) el.hidden = true; });
  if (!ids.length) return;
  let res;
  try { res = (await api("widgets", "POST", { ids })).results; } catch (e) { handleAuthError(e); return; }
  all.forEach((s, i) => {
    const el = fxEl(`widget-${i}`), r = res[s.key];
    if (!el || !r) return;
    el.hidden = false;
    el.className = "widget" + (r.ok ? "" : " err");
    el.textContent = r.ok ? r.text : "widget error";
    el.title = r.ok ? "" : "The widget could not read its API. Check the URL and key (tile menu → Check & widget).";
  });
}

// ── YAML export / import ──
function fxYamlDoc() {
  const clean = o => {
    if (Array.isArray(o)) return o.map(clean);
    if (o && typeof o === "object") { const r = {}; for (const [k, v] of Object.entries(o)) { if (SECRET_RE.test(k)) continue; r[k] = clean(v); } return r; }
    return o;
  };
  const svc = c => { const o = {}; ["ip", "port", "scheme", "desc", "logo", "check", "widget", "source"].forEach(k => { if (c[k] != null && c[k] !== "") o[k] = c[k]; }); if (typeof o.logo === "string" && o.logo.startsWith("data:")) delete o.logo; return clean(o); };
  const official = {}; Object.keys(saved).forEach(k => { if (k[0] !== "_" && saved[k] && typeof saved[k] === "object" && saved[k].ip) official[k] = svc(saved[k]); });
  const custom = (saved._custom || []).map(c => Object.assign({ id: c.id, name: c.name || "" }, svc(c)));
  const L = layout();
  return { format: "forgepanel-yaml", version: 1, exported: new Date().toISOString(), services: official, custom,
    layout: { groups: L.groups.map(g => ({ id: g.id, name: g.name || "Group", collapsed: !!g.collapsed, items: g.items || [] })), ungrouped: L.ungrouped } };
}
function fxExportYaml() {
  const text = FPYaml.stringify(fxYamlDoc(), "ForgePanel configuration (plain text).\nNo secrets: widget keys, push tokens, notification settings and uploaded logos are not included.");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "application/yaml" }));
  a.download = "forgepanel.yaml"; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
async function fxImportYaml(text) {
  let doc;
  try { doc = FPYaml.parse(text); } catch (e) { await showModal("Import Failed", "Could not read the YAML: " + e.message, "OK"); return; }
  if (!doc || doc.format !== "forgepanel-yaml") { await showModal("Import Failed", "That YAML is not a ForgePanel export (format: forgepanel-yaml).", "OK"); return; }
  const ok = await showModal("Import YAML", `Merge ${Object.keys(doc.services || {}).length} configured service(s) and ${(doc.custom || []).length} custom tile(s) into the dashboard? Matching tiles are updated; the group layout is replaced.`, "Import");
  if (!ok) return;
  const pick = o => { const r = {}; ["ip", "port", "scheme", "desc", "logo", "check", "widget", "source"].forEach(k => { if (o && o[k] != null) r[k] = o[k]; }); if (r.logo) r.logo = safeImgSrc(r.logo, null); return r; };
  for (const [name, c] of Object.entries(doc.services || {})) saved[name] = Object.assign(saved[name] || { logo: null }, pick(c));
  saved._custom = saved._custom || [];
  for (const c of doc.custom || []) {
    const cur = saved._custom.find(x => (c.id && x.id === c.id) || (!c.id && x.name === c.name));
    if (cur) Object.assign(cur, pick(c), { name: c.name || cur.name });
    else saved._custom.push(Object.assign({ id: c.id || newId(), name: c.name || "", logo: null }, pick(c)));
  }
  if (doc.layout && Array.isArray(doc.layout.groups)) saved._layout = { groups: doc.layout.groups.map(g => ({ id: String(g.id || "g" + newId()), name: String(g.name || "Group"), collapsed: !!g.collapsed, items: (g.items || []).map(String) })), ungrouped: (doc.layout.ungrouped || []).map(String) };
  await persist(); render(); checkAll(); fxRefreshWidgets();
}
