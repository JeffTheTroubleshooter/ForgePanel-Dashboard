// ForgePanel client helpers. All data lives on the server (server.py + SQLite);
// the browser only holds an HttpOnly session cookie. No IndexedDB, no
// sessionStorage passwords, no crypto.subtle, so plain http://<ip> works.

async function api(path, method = "GET", body, signal) {
  const opts = { method, credentials: "same-origin", headers: {} };
  // On pages guarded by fpIdleGuard, ordinary API calls (incl. background polling) must not
  // count as activity server-side; only the heartbeat sent on real input refreshes last-seen.
  if (typeof window !== "undefined" && window.FP_PASSIVE_API && path !== "heartbeat") opts.headers["X-FP-Passive"] = "1";
  if (signal) opts.signal = signal;
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  } else if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = "{}";
  }
  let res;
  try { res = await fetch("/api/" + path, opts); }
  catch (e) {
    if (e && e.name === "AbortError") throw Object.assign(new Error("Aborted"), { status: 0, aborted: true });
    throw Object.assign(new Error("Cannot reach the ForgePanel server. Is server.py running?"), { status: 0 });
  }
  let data = {};
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function escapeHtml(v) {
  return String(v == null ? "" : v).replace(/[&<>"'`]/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;"
  })[c]);
}

// Only allow image data URLs or relative/http(s) URLs as logo sources.
function safeImgSrc(v, fallback) {
  const s = String(v || "");
  if (/^data:image\/(png|jpe?g|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$/.test(s)) return s;
  if (/^(\.\.\/|\/|https?:\/\/)[^"'<>\s]*$/.test(s)) return s;
  return fallback;
}

// ── inactivity auto-logout ──────────────────────────────────────────────────
// After IDLE_TIMEOUT_MS without mousemove/mousedown/keydown/scroll/touchstart/wheel in ANY
// tab, call /api/logout (invalidates the server session) and go to login.html?reason=inactivity.
// Tabs share the last-activity time through localStorage. The server enforces the same
// timeout (server.py IDLE_TIMEOUT); real activity sends a throttled /api/heartbeat.
const IDLE_TIMEOUT_MS = 60 * 1000;
const FP_ACTIVITY_KEY = "fp_last_activity";
const FP_LOGOUT_KEY = "fp_idle_logout";

function fpIdleGuard(opts = {}) {
  // timeoutMs: 0 = off (Settings → Security); setTimeoutMs() applies a new value immediately.
  const norm = ms => (ms === 0 ? 0 : Math.max(5000, ms || IDLE_TIMEOUT_MS));
  let timeout = norm(opts.timeoutMs);
  const loginUrl = opts.loginUrl || "login.html?reason=inactivity";
  const hbIdle = 15000;
  let hbEvery = timeout ? Math.min(15000, Math.floor(timeout / 4)) : hbIdle;
  window.FP_PASSIVE_API = true;
  let last = Date.now(), lastHb = 0, done = false, lastWrite = 0;
  const readShared = () => { try { return Number(localStorage.getItem(FP_ACTIVITY_KEY)) || 0; } catch { return 0; } };
  const writeShared = t => { try { localStorage.setItem(FP_ACTIVITY_KEY, String(t)); } catch { /* ignore */ } };
  writeShared(last);
  function heartbeat(now) {
    if (now - lastHb < hbEvery) return;
    lastHb = now;
    api("heartbeat", "POST").catch(e => { if (e && e.status === 401) expire(false); });
  }
  function onActivity() {
    if (done) return;
    const now = Date.now();
    last = now;
    if (now - lastWrite > 1000) { lastWrite = now; writeShared(now); }
    heartbeat(now);
  }
  async function expire(callLogout = true) {
    if (done) return;
    done = true;
    try { localStorage.setItem(FP_LOGOUT_KEY, String(Date.now())); } catch { /* ignore */ }
    try { sessionStorage.removeItem("fp_view"); } catch { /* ignore */ }
    if (callLogout) { try { await api("logout", "POST"); } catch { /* ignore */ } }
    location.href = loginUrl;
  }
  ["mousemove", "mousedown", "keydown", "scroll", "touchstart", "wheel"].forEach(ev =>
    window.addEventListener(ev, onActivity, { passive: true, capture: true }));
  window.addEventListener("storage", e => {
    if (e.key === FP_ACTIVITY_KEY) last = Math.max(last, Number(e.newValue) || 0);
    else if (e.key === FP_LOGOUT_KEY && e.newValue) expire(false);
  });
  const timer = setInterval(() => {
    if (done) return clearInterval(timer);
    if (!timeout) return;
    const latest = Math.max(last, readShared());
    if (Date.now() - latest >= timeout) expire(true);
  }, 1000);
  function setTimeoutMs(ms) {
    timeout = norm(ms);
    hbEvery = timeout ? Math.min(15000, Math.floor(timeout / 4)) : hbIdle;
    last = Date.now(); writeShared(last);
  }
  const guard = { expire, touch: onActivity, setTimeoutMs };
  window.fpIdle = guard;
  return guard;
}
