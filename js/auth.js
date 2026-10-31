// ForgePanel client helpers. All data lives on the server (server.py + SQLite);
// the browser only holds an HttpOnly session cookie. No IndexedDB, no
// sessionStorage passwords, no crypto.subtle, so plain http://<ip> works.

async function api(path, method = "GET", body, signal) {
  const opts = { method, credentials: "same-origin", headers: {} };
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
