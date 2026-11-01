"""ForgePanel extensions (stdlib only): check types, push monitors, notifications,
Docker label discovery, tile widgets, public status page.

server.py calls install(module) at import time. Secrets (widget keys, push tokens,
webhook URLs, ntfy tokens) live in encrypted settings and are never logged or sent
back to the browser in full.
"""
import base64, hashlib, http.client, json, os, re, secrets, socket, ssl, struct, threading, time
import urllib.error, urllib.parse, urllib.request

S = None            # the server module
log = None
PUSH_PREFIX = "/api/push/"
PREV = {}           # svc id -> last up/down state (for down/recovered alerts)
WCACHE = {}         # svc id -> (ts, result)
PUB_CACHE = {"sig": None, "ts": 0, "data": None}   # short-lived /api/public/status payload
PUB_TTL = 5
HOST_RE = re.compile(r"[A-Za-z0-9.:\-\[\]]+")
DOCKER_SOCK = os.environ.get("FORGEPANEL_DOCKER_SOCK", "/var/run/docker.sock")

MIGRATIONS = """
CREATE TABLE IF NOT EXISTS push_state (svc TEXT PRIMARY KEY, token_hash TEXT NOT NULL, last INTEGER);
CREATE INDEX IF NOT EXISTS push_state_hash ON push_state (token_hash);
"""

def migrate():
    st = S.STATE.store
    with st.lock:
        st.db.executescript(MIGRATIONS)
        cols = {r[1] for r in st.db.execute("PRAGMA table_info(push_state)")}
        if "last" not in cols:   # guarded additive column
            st.db.execute("ALTER TABLE push_state ADD COLUMN last INTEGER")
        if "ping" not in cols:   # response time reported by ?ping=ms
            st.db.execute("ALTER TABLE push_state ADD COLUMN ping REAL")
        try:
            st.db.commit()
        except Exception:
            pass

def unverified():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def mask(v, keep=4):
    v = str(v or "")
    if not v:
        return ""
    return "••••" + (v[-keep:] if len(v) > keep * 3 else "")

def dek():
    w = S.STATE.worker
    return w["dek"] if w.get("ready") else None

# ── services ────────────────────────────────────────────────────────────────
def iter_services(saved):
    """Yield (id, name, cfg) for every configured service."""
    for k, v in saved.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        yield "o:" + k, k, v
    for i, c in enumerate(saved.get("_custom", []) or []):
        if isinstance(c, dict) and c.get("id"):
            yield "c:" + str(c["id"]), c.get("name") or f"Custom {i + 1}", c

def find(saved, sid):
    for i, n, c in iter_services(saved):
        if i == sid:
            return c
    return None

def ctype(c):
    t = ((c.get("check") or {}).get("type") or "http").lower()
    return t if t in ("http", "tcp", "keyword", "dns", "push") else "http"

def monitored(c):
    return ctype(c) == "push" or bool(c.get("ip"))

def series_key(c, sid):
    t = ctype(c)
    if t == "push":
        return S.uptime_key("push", sid)
    if t == "dns":
        return S.uptime_key("dns:" + str(c.get("ip")), str((c.get("check") or {}).get("host", "")))
    if t == "keyword":   # own series: a keyword check can fail while plain HTTP on the same port is up
        ck = c.get("check") or {}
        return S.uptime_key(str(c.get("ip", "")), f"{c.get('port')}#kw:{ck.get('path') or '/'}:{ck.get('keyword')}:{int(bool(ck.get('invert')))}")
    return S.uptime_key(str(c.get("ip", "")), c.get("port"))

# ── check types ─────────────────────────────────────────────────────────────
def _hp(c, default_port=80):
    host = str(c.get("ip") or "").strip()
    try:
        port = int(c.get("port") or default_port)
    except (TypeError, ValueError):
        return None, None
    if not host or not HOST_RE.fullmatch(host) or not 0 < port < 65536:
        return None, None
    return host, port

def scheme_of(c):
    if c.get("scheme") in ("http", "https"):
        return c["scheme"]
    return "https" if str(c.get("port")) == "443" else "http"

def check_tcp(c, timeout=3.0):
    host, port = _hp(c)
    if not host:
        return {"online": False}
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return {"online": True}
    except OSError:
        return {"online": False}

def check_keyword(c, timeout=5.0):
    host, port = _hp(c)
    chk = c.get("check") or {}
    kw = str(chk.get("keyword") or "")
    if not host or not kw:
        return {"online": False, "detail": "keyword not set" if host else "no address"}
    path = str(chk.get("path") or "/")
    if not path.startswith("/"):
        path = "/" + path
    url = f"{scheme_of(c)}://{host}:{port}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ForgePanel"})
        with urllib.request.urlopen(req, timeout=timeout, context=unverified()) as r:
            body = r.read(512 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(512 * 1024).decode("utf-8", "replace")
        except Exception:
            body = ""
    except Exception:
        return {"online": False, "detail": "no response"}
    found = kw.lower() in body.lower()
    if chk.get("invert"):
        found = not found
    return {"online": found, "detail": "keyword found" if found else "keyword missing"}

def dns_query(server, port, name, timeout=3.0):
    qid = secrets.randbits(16)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for part in name.strip(".").split("."):
        b = part.encode("idna")
        if not 0 < len(b) < 64:
            raise ValueError("bad name")
        q += bytes([len(b)]) + b
    q += b"\x00" + struct.pack(">HH", 1, 1)
    fam = socket.AF_INET6 if ":" in server else socket.AF_INET
    with socket.socket(fam, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(q, (server.strip("[]"), port))
        data, _ = s.recvfrom(2048)
    rid, flags, _qd, an = struct.unpack(">HHHH", data[:8])
    return rid == qid and (flags & 0x000F) == 0 and an > 0

def check_dns(c, timeout=3.0):
    host, _ = _hp(c, 53)
    chk = c.get("check") or {}
    name = str(chk.get("host") or "").strip()
    try:
        port = int(chk.get("dns_port") or 53)
    except (TypeError, ValueError):
        port = 53
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", name):
        return {"online": False, "detail": "DNS name not set"}
    try:
        ok = dns_query(host, port, name, timeout)
    except Exception:
        ok = False
    return {"online": ok, "detail": "resolved" if ok else "no answer"}

def push_interval(c):
    try:
        return max(10, int((c.get("check") or {}).get("interval") or 60))
    except (TypeError, ValueError):
        return 60

def check_push(c, sid):
    rows = S.STATE.store.q("SELECT last, ping FROM push_state WHERE svc=?", (sid,))
    last = rows[0][0] if rows else None
    ping = rows[0][1] if rows else None
    if not rows:
        return {"online": False, "detail": "no push URL"}
    if not last:
        return {"online": False, "detail": "waiting for first push"}
    age = int(time.time()) - int(last)
    ok = age <= push_interval(c) + 5
    return {"online": ok, "detail": f"last push {age}s ago", "last": int(last), "ms": ping}

def run_check(c, sid):
    """Run the service's configured check. Returns dict with online, ms."""
    t = ctype(c)
    t0 = time.monotonic()
    if t == "push":
        r = check_push(c, sid)
        r.setdefault("ms", None)
        r["type"] = t
        return r
    if t == "tcp":
        r = check_tcp(c)
    elif t == "keyword":
        r = check_keyword(c)
    elif t == "dns":
        r = check_dns(c)
    else:
        host, port = _hp(c)
        r = S.check_service(host, port, scheme_of(c)) if host else {"online": False}
    r["ms"] = round((time.monotonic() - t0) * 1000, 1) if r.get("online") else None
    r["type"] = t
    return r

# ── uptime loop (replaces server.uptime_loop) ───────────────────────────────
def uptime_loop():
    import concurrent.futures as cf
    while True:
        try:
            k = dek()
            if k is not None:
                saved = S.STATE.store.load_saved(k)
                targets = [(sid, name, c) for sid, name, c in iter_services(saved) if monitored(c)]
                if targets:
                    now = int(time.time())
                    with cf.ThreadPoolExecutor(16) as ex:
                        res = list(ex.map(lambda t: run_check(t[2], t[0]), targets))
                    rows, seen = [], set()
                    for (sid, name, c), r in zip(targets, res):
                        key = series_key(c, sid)
                        if key not in seen:
                            seen.add(key)
                            rows.append((key, now, int(bool(r["online"])), r.get("ms")))
                        transition(k, sid, name, c, bool(r["online"]), r)
                    st = S.STATE.store
                    with st.lock:
                        st.db.executemany("INSERT INTO uptime (key, ts, up, ms) VALUES (?,?,?,?)", rows)
                        st.db.execute("DELETE FROM uptime WHERE ts < ?", (now - S.UPTIME["retention_days"] * 86400,))
        except Exception:
            log.exception("uptime checker")
        time.sleep(S.UPTIME["interval"])

def transition(k, sid, name, c, up, r):
    prev = PREV.get(sid)
    PREV[sid] = up
    if prev is None or prev == up:
        return
    cfg = notify_cfg(k)
    if up and cfg.get("on_recover", True):
        notify(k, f"✅ {name} recovered", f"{name} is back up ({r.get('type') or ctype(c)} check).", "up")
    elif not up and cfg.get("on_down", True):
        notify(k, f"🔴 {name} is down", f"{name} failed its {ctype(c)} check" +
               (f": {r['detail']}." if r.get("detail") else "."), "down")

# ── notifications ───────────────────────────────────────────────────────────
NOTIFY_DEFAULT = {"discord": {"enabled": False, "url": ""},
                  "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": "", "token": ""},
                  "on_down": True, "on_recover": True, "on_scan": True}

def notify_cfg(k):
    c = S.STATE.store.get_setting(k, "notify", None) or {}
    out = json.loads(json.dumps(NOTIFY_DEFAULT))
    for key in ("on_down", "on_recover", "on_scan"):
        if key in c:
            out[key] = bool(c[key])
    for key in ("discord", "ntfy"):
        if isinstance(c.get(key), dict):
            out[key].update(c[key])
    return out

def post(url, body, headers, timeout=8):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status

def send_all(cfg, title, text, kind="info", only=None):
    """Send to enabled channels. Returns {channel: 'ok' | error}. Never logs URLs or tokens."""
    out = {}
    d = cfg["discord"]
    if (only in (None, "discord")) and d.get("enabled") and d.get("url"):
        try:
            if not re.match(r"^https?://", d["url"]):
                raise ValueError("webhook URL must start with http(s)://")
            post(d["url"], json.dumps({"content": f"**{title}**\n{text}", "username": "ForgePanel"}).encode(),
                 {"Content-Type": "application/json", "User-Agent": "ForgePanel"})
            out["discord"] = "ok"
        except urllib.error.HTTPError as e:
            out["discord"] = f"HTTP {e.code}"
        except Exception as e:
            out["discord"] = type(e).__name__
    n = cfg["ntfy"]
    if (only in (None, "ntfy")) and n.get("enabled") and n.get("topic"):
        try:
            server = str(n.get("server") or "https://ntfy.sh").rstrip("/")
            if not re.match(r"^https?://", server):
                raise ValueError("ntfy server must start with http(s)://")
            hd = {"Title": title.encode("ascii", "ignore").decode().strip() or "ForgePanel",
                  "Tags": {"down": "red_circle", "up": "white_check_mark", "scan": "mag"}.get(kind, "bell"),
                  "Priority": "high" if kind == "down" else "default", "User-Agent": "ForgePanel"}
            if n.get("token"):
                hd["Authorization"] = "Bearer " + n["token"]
            post(server + "/" + urllib.parse.quote(str(n["topic"]).strip("/")), text.encode(), hd)
            out["ntfy"] = "ok"
        except urllib.error.HTTPError as e:
            out["ntfy"] = f"HTTP {e.code}"
        except Exception as e:
            out["ntfy"] = type(e).__name__
    for ch, r in out.items():
        if r != "ok":
            log.warning("Notification via %s failed: %s", ch, r)
    return out

def notify(k, title, text, kind="info"):
    cfg = notify_cfg(k)
    threading.Thread(target=send_all, args=(cfg, title, text, kind), daemon=True).start()

def on_scan(k, result, flagged=None):
    try:
        cfg = notify_cfg(k)
        if not cfg.get("on_scan"):
            return
        known = S.known_keys(S.STATE.store.load_saved(k))
        new = [x for x in result.get("services", []) if S.svc_key(x) not in known]
        if flagged is not None:
            n = flagged
        else:
            n = len(new)
        if n:
            hosts = sorted({str(x.get("ip")) for x in new})
            notify(k, f"🔎 Scan found {n} new service(s)",
                   f"Scan of {result.get('subnet', 'network')}: {n} new service(s) on {len(hosts)} host(s). Open ForgePanel → Scan history to add them.", "scan")
    except Exception:
        log.exception("scan notification")

def notify_public(cfg):
    d, n = cfg["discord"], cfg["ntfy"]
    du = str(d.get("url") or "")
    m = re.match(r"^(https?://[^/]+)", du)
    return {"discord": {"enabled": bool(d.get("enabled")), "set": bool(du), "hint": (m.group(1) + "/…" + du[-4:]) if m else ""},
            "ntfy": {"enabled": bool(n.get("enabled")), "server": n.get("server") or "", "topic": n.get("topic") or "",
                     "tokenSet": bool(n.get("token")), "tokenHint": mask(n.get("token"))},
            "on_down": cfg["on_down"], "on_recover": cfg["on_recover"], "on_scan": cfg["on_scan"]}

def r_notify_get(h, d):
    tok, s = h.need_session()
    if tok:
        h.send_json(200, notify_public(notify_cfg(s["dek"])))

def r_notify_put(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    k = s["dek"]
    cfg = notify_cfg(k)
    for key in ("on_down", "on_recover", "on_scan"):
        if key in d:
            cfg[key] = bool(d[key])
    dd = d.get("discord") or {}
    if "enabled" in dd:
        cfg["discord"]["enabled"] = bool(dd["enabled"])
    if dd.get("clear"):
        cfg["discord"]["url"] = ""
    elif dd.get("url"):   # blank = keep the saved webhook
        u = str(dd["url"]).strip()
        if not re.match(r"^https?://\S+$", u) or len(u) > 500:
            raise ValueError("Discord webhook must be an http(s) URL")
        cfg["discord"]["url"] = u
    nn = d.get("ntfy") or {}
    if "enabled" in nn:
        cfg["ntfy"]["enabled"] = bool(nn["enabled"])
    if "server" in nn:
        sv = str(nn["server"] or "").strip() or "https://ntfy.sh"
        if not re.match(r"^https?://[^\s/]+(/\S*)?$", sv):
            raise ValueError("ntfy server must be an http(s) URL")
        cfg["ntfy"]["server"] = sv
    if "topic" in nn:
        tp = str(nn["topic"] or "").strip()
        if tp and not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", tp):
            raise ValueError("ntfy topic: letters, digits, - and _ only")
        cfg["ntfy"]["topic"] = tp
    if nn.get("clearToken"):
        cfg["ntfy"]["token"] = ""
    elif nn.get("token"):
        cfg["ntfy"]["token"] = str(nn["token"]).strip()[:300]
    S.STATE.store.set_setting(k, "notify", cfg)
    h.send_json(200, notify_public(cfg))

def r_notify_test(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    cfg = notify_cfg(s["dek"])
    only = d.get("channel") if d.get("channel") in ("discord", "ntfy") else None
    res = send_all(cfg, "ForgePanel test", "Test notification from ForgePanel. If you can read this, alerts work.", "info", only)
    if not res:
        return h.send_json(400, {"error": "No notification channel is enabled and configured."})
    h.send_json(200, {"results": res})

# ── per-service secrets (widget keys, push tokens) ──────────────────────────
def secrets_get(k):
    v = S.STATE.store.get_setting(k, "svc_secrets", None)
    return v if isinstance(v, dict) else {}

def push_path(token):
    return PUSH_PREFIX + token

def svc_public(sid, sec, push_row):
    x = sec.get(sid) or {}
    return {"keySet": bool(x.get("widget_key")), "keyHint": mask(x.get("widget_key")),
            "pushPath": push_path(x["push_token"]) if x.get("push_token") and push_row is not None else None,
            "lastPush": push_row}

def r_svc_secrets_get(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    sec = secrets_get(s["dek"])
    rows = {r[0]: r[1] for r in S.STATE.store.q("SELECT svc, last FROM push_state")}
    out = {sid: svc_public(sid, sec, rows.get(sid, 0) if sid in rows else None) for sid in sec}
    h.send_json(200, {"services": out})

def r_svc_secrets_post(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    k = s["dek"]
    sid = str(d.get("id") or "")
    if not re.fullmatch(r"[oc]:.{1,120}", sid):
        raise ValueError("bad service id")
    sec = secrets_get(k)
    x = sec.setdefault(sid, {})
    st = S.STATE.store
    if d.get("clearKey"):
        x.pop("widget_key", None)
    elif d.get("widgetKey"):
        x["widget_key"] = str(d["widgetKey"]).strip()[:500]
    if d.get("push") == "new":
        t = secrets.token_urlsafe(24)
        x["push_token"] = t
        st.q("INSERT OR REPLACE INTO push_state (svc, token_hash, last) VALUES (?,?,NULL)",
             (sid, hashlib.sha256(t.encode()).hexdigest()))
    elif d.get("push") == "revoke":
        x.pop("push_token", None)
        st.q("DELETE FROM push_state WHERE svc=?", (sid,))
    if not x:
        sec.pop(sid, None)
    st.set_setting(k, "svc_secrets", sec)
    WCACHE.pop(sid, None)
    rows = st.q("SELECT last FROM push_state WHERE svc=?", (sid,))
    h.send_json(200, svc_public(sid, sec, (rows[0][0] or 0) if rows else None))

def handle_push(h):
    """GET/POST /api/push/<token>[?status=up|down&ping=ms]. Auth = the token only."""
    tok = urllib.parse.urlsplit(h.path).path[len(PUSH_PREFIX):].strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,64}", tok):
        return h.send_json(404, {"ok": False})
    rows = S.STATE.store.q("SELECT svc FROM push_state WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),))
    if not rows:
        return h.send_json(404, {"ok": False})
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
    status = (qs.get("status") or ["up"])[0].lower()
    ping = None
    try:
        ping = max(0.0, min(float((qs.get("ping") or [""])[0]), 3600000.0))
    except (TypeError, ValueError):
        pass
    now = int(time.time())
    if status == "down":
        S.STATE.store.q("UPDATE push_state SET last=0, ping=NULL WHERE svc=?", (rows[0][0],))
    else:
        S.STATE.store.q("UPDATE push_state SET last=?, ping=? WHERE svc=?", (now, ping, rows[0][0]))
    n = int(h.headers.get("Content-Length") or 0)
    if 0 < n <= 65536:
        h.rfile.read(n)
    h.send_json(200, {"ok": True})

# ── /api/check with check types + response time ─────────────────────────────
def r_check(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    items = d.get("items")
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("items must be a list (max 200)")
    saved = S.STATE.store.load_saved(s["dek"])
    def one(it):
        if not isinstance(it, dict):
            return {"online": False}
        c = find(saved, str(it.get("id") or "")) if it.get("id") else None
        if c is not None and ctype(c) != "http":
            return run_check(c, str(it["id"]))
        return run_check({"ip": it.get("ip"), "port": it.get("port"),
                          "scheme": "https" if it.get("scheme") == "https" else "http"}, "")
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(16) as ex:
        results = list(ex.map(one, items))
    h.send_json(200, {"results": results})

def item_key(saved, it):
    c = find(saved, str(it.get("id") or "")) if it.get("id") else None
    if c is not None:
        return series_key(c, str(it["id"]))
    return S.uptime_key(str(it.get("ip", "")), it.get("port"))

# ── Docker discovery ────────────────────────────────────────────────────────
class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=4):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = path
    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.sock_path)
        self.sock = s

def docker_get(path):
    c = UnixHTTP(DOCKER_SOCK)
    try:
        c.request("GET", path, headers={"Host": "docker"})
        r = c.getresponse()
        body = r.read(4 * 1024 * 1024)
        if r.status != 200:
            raise RuntimeError(f"Docker API returned HTTP {r.status}")
        return json.loads(body)
    finally:
        c.close()

def docker_list(host_hint):
    if not os.path.exists(DOCKER_SOCK):
        return {"available": False, "error": f"Docker socket {DOCKER_SOCK} not found. Is Docker installed on the ForgePanel host?"}
    try:
        data = docker_get("/containers/json")
    except PermissionError:
        return {"available": False, "error": f"Permission denied on {DOCKER_SOCK}. Add the user running ForgePanel to the 'docker' group (then restart ForgePanel), or mount the socket read-only into its container."}
    except (OSError, RuntimeError, ValueError) as e:
        return {"available": False, "error": f"Could not talk to Docker: {e}"}
    out = []
    for ct in data if isinstance(data, list) else []:
        labels = ct.get("Labels") or {}
        fp = {k[len("forgepanel."):]: str(v) for k, v in labels.items() if k.startswith("forgepanel.")}
        name = ((ct.get("Names") or ["/?"])[0]).lstrip("/")
        ports = []
        for p in ct.get("Ports") or []:
            if p.get("PublicPort") and p.get("Type", "tcp") == "tcp":
                ports.append({"private": p.get("PrivatePort"), "public": p.get("PublicPort"), "ip": p.get("IP") or ""})
        uniq = sorted({p["public"] for p in ports})
        sug = {"name": fp.get("name") or name, "group": fp.get("group") or "", "icon": fp.get("icon") or "",
               "ip": host_hint, "port": uniq[0] if uniq else None, "scheme": None}
        if fp.get("url"):
            u = urllib.parse.urlsplit(fp["url"])
            if u.scheme in ("http", "https") and u.hostname:
                sug.update(ip=u.hostname, scheme=u.scheme, port=u.port or (443 if u.scheme == "https" else 80))
        elif uniq:
            sug["scheme"] = "https" if uniq[0] in (443, 8443, 9443) else "http"
        if not fp and not uniq:
            continue   # nothing reachable to import
        out.append({"id": str(ct.get("Id", ""))[:12], "container": name, "image": ct.get("Image", ""),
                    "state": ct.get("State", ""), "labeled": bool(fp), "ports": uniq, "suggested": sug})
    out.sort(key=lambda x: (not x["labeled"], x["suggested"]["name"].lower()))
    return {"available": True, "socket": DOCKER_SOCK, "containers": out}

def r_docker(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    host = (h.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
    if host in ("", "localhost", "0.0.0.0") or not HOST_RE.fullmatch(host):
        host = "127.0.0.1"
    h.send_json(200, docker_list(host))

# ── tile widgets ────────────────────────────────────────────────────────────
WIDGETS = ("jellyfin", "pihole", "adguard", "proxmox")

def wget(url, headers=None, data=None, timeout=5):
    req = urllib.request.Request(url, data=data, headers=dict({"User-Agent": "ForgePanel", "Accept": "application/json"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout, context=unverified()) as r:
        return json.loads(r.read(2 * 1024 * 1024))

def widget_base(c, w):
    u = str(w.get("url") or "").strip().rstrip("/")
    if not u:
        host, port = _hp(c)
        if not host:
            raise ValueError("no URL")
        u = f"{scheme_of(c)}://{host}:{port}"
    if not re.match(r"^https?://[^\s]+$", u):
        raise ValueError("bad URL")
    return u

def fmt_n(n):
    n = int(n or 0)
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e4 else f"{n:,}"

def widget_run(c, key):
    w = c.get("widget") or {}
    t = w.get("type")
    base = widget_base(c, w)
    if t == "jellyfin":
        if not key:
            raise ValueError("API key missing")
        ss = wget(base + "/Sessions?activeWithinSeconds=960", {"X-Emby-Token": key})
        n = sum(1 for x in ss if isinstance(x, dict) and x.get("NowPlayingItem"))
        return {"text": f"▶ {n} playing" if n else "Nothing playing", "value": n}
    if t == "adguard":
        user = str(w.get("user") or "")
        hd = {"Authorization": "Basic " + base64.b64encode(f"{user}:{key}".encode()).decode()} if (user or key) else {}
        st = wget(base + "/control/stats", hd)
        b, q = int(st.get("num_blocked_filtering") or 0), int(st.get("num_dns_queries") or 0)
        return {"text": f"⛔ {fmt_n(b)} blocked" + (f" ({100 * b / q:.1f}%)" if q else ""), "value": b}
    if t == "pihole":
        try:   # Pi-hole v6
            sid = None
            if key:
                a = wget(base + "/api/auth", {"Content-Type": "application/json"}, json.dumps({"password": key}).encode())
                sid = (a.get("session") or {}).get("sid")
            st = wget(base + "/api/stats/summary", {"X-FTL-SID": sid} if sid else {})
            b, q = int(st["queries"]["blocked"]), int(st["queries"]["total"])
            if sid:
                try:
                    urllib.request.urlopen(urllib.request.Request(base + "/api/auth", method="DELETE", headers={"X-FTL-SID": sid}), timeout=3, context=unverified()).close()
                except Exception:
                    pass
        except Exception:   # Pi-hole v5
            st = wget(base + "/admin/api.php?summaryRaw" + ("&auth=" + urllib.parse.quote(key) if key else ""))
            b, q = int(st["ads_blocked_today"]), int(st["dns_queries_today"])
        return {"text": f"⛔ {fmt_n(b)} blocked" + (f" ({100 * b / q:.1f}%)" if q else ""), "value": b}
    if t == "proxmox":
        if not key or "=" not in key:
            raise ValueError("token must be user@realm!id=secret")
        ns = wget(base + "/api2/json/nodes", {"Authorization": "PVEAPIToken=" + key}).get("data") or []
        want = str(w.get("node") or "").strip()
        if want:
            ns = [n for n in ns if n.get("node") == want]
        if not ns:
            raise ValueError("no node")
        parts = []
        for n in ns[:3]:
            mem = (100 * n.get("mem", 0) / n["maxmem"]) if n.get("maxmem") else 0
            parts.append(f"{n.get('node')}: CPU {100 * float(n.get('cpu') or 0):.0f}% · RAM {mem:.0f}%")
        return {"text": " | ".join(parts)}
    raise ValueError("unknown widget")

def r_widgets(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    ids = d.get("ids")
    if not isinstance(ids, list) or len(ids) > 200:
        raise ValueError("ids must be a list")
    saved = S.STATE.store.load_saved(s["dek"])
    sec = secrets_get(s["dek"])
    now = time.time()
    def one(sid):
        c = find(saved, str(sid))
        if not c or (c.get("widget") or {}).get("type") not in WIDGETS:
            return None
        sig = json.dumps(c.get("widget"), sort_keys=True) + str(c.get("ip")) + str(c.get("port"))
        hit = WCACHE.get(sid)
        if hit and now - hit[0] < 30 and hit[1] == sig:
            return hit[2]
        try:
            r = dict(widget_run(c, (sec.get(sid) or {}).get("widget_key", "")), ok=True)
        except Exception as e:
            r = {"ok": False, "error": "widget error"}
            log.info("widget %s failed: %s", (c.get("widget") or {}).get("type"), type(e).__name__)
        WCACHE[sid] = (now, sig, r)
        return r
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, [str(x) for x in ids]))
    h.send_json(200, {"results": {str(i): r for i, r in zip(ids, res) if r is not None}})

# ── public status page ──────────────────────────────────────────────────────
def pub_cfg():
    try:
        c = json.loads(S.STATE.store.meta("public_status") or "{}")
    except ValueError:
        c = {}
    return {"enabled": bool(c.get("enabled")), "showIps": bool(c.get("showIps")), "showMap": bool(c.get("showMap", True)),
            "title": str(c.get("title") or "Service status")[:80]}

def r_public_cfg_get(h, d):
    tok, s = h.need_session()
    if tok:
        h.send_json(200, pub_cfg())

def r_public_cfg_put(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    c = pub_cfg()
    for k in ("enabled", "showIps", "showMap"):
        if k in d:
            c[k] = bool(d[k])
    if "title" in d:
        c["title"] = str(d["title"] or "Service status").strip()[:80] or "Service status"
    S.STATE.store.set_meta("public_status", json.dumps(c))
    h.send_json(200, c)

def safe_logo(v):
    v = str(v or "")
    return v if re.fullmatch(r"(\.\./assets/[A-Za-z0-9_./\-]+|https?://[^\s\"'<>]{1,300}|data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=]{1,200000})", v) else ""

def r_public_status(h, d):
    c = pub_cfg()
    if not c["enabled"]:
        return h.send_json(404, {"error": "The public status page is turned off."})
    k = dek()
    if k is None:
        return h.send_json(503, {"error": "Status is unavailable until an admin signs in after a server restart.", "title": c["title"]})
    now = int(time.time())
    sig = hashlib.sha256(k + json.dumps(c, sort_keys=True).encode()).hexdigest()
    if PUB_CACHE["sig"] == sig and now - PUB_CACHE["ts"] < PUB_TTL:
        return h.send_json(200, PUB_CACHE["data"])
    saved = S.STATE.store.load_saved(k)
    L = saved.get("_layout") if isinstance(saved.get("_layout"), dict) else {}
    gname = {}
    for g in L.get("groups") or []:
        for key in g.get("items") or []:
            gname.setdefault(key, str(g.get("name") or "Group")[:60])
    now = int(time.time())
    out = []
    for sid, name, cfg in iter_services(saved):
        if not monitored(cfg):
            continue
        rows = S.STATE.store.q("SELECT ts, up, ms FROM uptime WHERE key=? AND ts>=? ORDER BY ts", (series_key(cfg, sid), now - 86400))
        last = rows[-1] if rows else None
        x = {"id": hashlib.sha256(sid.encode()).hexdigest()[:10], "name": str(name)[:80], "group": gname.get(sid, ""),
             "logo": safe_logo(cfg.get("logo")), "type": ctype(cfg),
             "up": (bool(last[1]) if last else None), "ms": last[2] if last else None,
             "uptime": round(100 * sum(r[1] for r in rows) / len(rows), 2) if rows else None,
             "bars": [None] * 24}
        for ts, up, ms in rows:
            i = min(23, max(0, (ts - (now - 86400)) // 3600))
            b = x["bars"][i] or [0, 0]
            b[0] += up; b[1] += 1
            x["bars"][i] = b
        x["bars"] = [round(b[0] / b[1], 3) if b else None for b in x["bars"]]
        if c["showIps"] and cfg.get("ip"):
            x["addr"] = f"{cfg.get('ip')}:{cfg.get('port')}"
        out.append(x)
    order = [g.get("name") for g in L.get("groups") or []]
    payload = {"title": c["title"], "showMap": c["showMap"], "generated": now, "interval": S.UPTIME["interval"],
               "groups": [str(g)[:60] for g in order if g], "services": out}
    PUB_CACHE.update(sig=sig, ts=now, data=payload)
    h.send_json(200, payload)

ROUTES = {
    ("GET", "/api/notify"): r_notify_get,
    ("PUT", "/api/notify"): r_notify_put,
    ("POST", "/api/notify/test"): r_notify_test,
    ("GET", "/api/service-secrets"): r_svc_secrets_get,
    ("POST", "/api/service-secrets"): r_svc_secrets_post,
    ("GET", "/api/docker"): r_docker,
    ("POST", "/api/widgets"): r_widgets,
    ("GET", "/api/public-status/config"): r_public_cfg_get,
    ("PUT", "/api/public-status/config"): r_public_cfg_put,
    ("GET", "/api/public/status"): r_public_status,
    ("POST", "/api/check"): r_check,
}

def install(mod):
    global S, log
    S, log = mod, mod.log
    mod.ROUTES.update(ROUTES)
    mod.uptime_loop = uptime_loop
