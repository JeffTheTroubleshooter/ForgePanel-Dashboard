#!/usr/bin/env python3
"""ForgePanel server: static files + JSON API backed by SQLite.

Python 3 standard library only (http.server + sqlite3 + hashlib). The optional
"cryptography" package enables AES-256-GCM encryption of dashboard data, custom
services and scan results at rest. Without it data is stored as plain JSON and
a warning is logged.

Usage: python3 server.py [--port 8084] [--host 0.0.0.0] [--db database/forgepanel.db]
"""
import argparse
import base64
import hashlib
import hmac
import http.server
import importlib.util
import json
import logging
import os
import posixpath
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAVE_CRYPTO = True
except ImportError:  # optional
    AESGCM = None
    HAVE_CRYPTO = False

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "database", "forgepanel.db")
SESSION_TTL = 12 * 3600
COOKIE = "fp_session"
MAX_BODY = 12 * 1024 * 1024
RATE_MAX, RATE_WINDOW = 5, 300          # 5 failed attempts per 5 minutes per IP
COMPAT_SALT, COMPAT_ITER = b"homelab-v2", 100000  # js/auth.js + scan.py export format

log = logging.getLogger("forgepanel")

# ── crypto helpers ─────────────────────────────────────────────────────────
def kdf(secret, salt):
    """Strong server-side KDF (scrypt, N=2^14, r=8, p=1) -> 32 bytes."""
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)

def b64e(b): return base64.b64encode(b).decode()
def b64d(s): return base64.b64decode(s)

def aes_seal(key, raw, aad=b""):
    iv = secrets.token_bytes(12)
    return iv + AESGCM(key).encrypt(iv, raw, aad)

def aes_open(key, blob, aad=b""):
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad)

def seal(dek, obj):
    raw = json.dumps(obj).encode()
    if dek is None:
        return 0, raw.decode()
    return 1, b64e(aes_seal(dek, raw, b"forgepanel-data"))

def unseal(dek, enc, blob):
    if not enc:
        return json.loads(blob)
    if dek is None:
        raise ValueError("encrypted row but no data key (is 'cryptography' installed?)")
    return json.loads(aes_open(dek, b64d(blob), b"forgepanel-data"))

def compat_key(password):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), COMPAT_SALT, COMPAT_ITER, 32)

def compat_decrypt(payload, password):
    """Open a {iv, ct} payload made by the old browser export or scan.py, or {plain, data}."""
    if isinstance(payload, dict) and payload.get("plain"):
        return payload.get("data")
    if not HAVE_CRYPTO:
        raise ValueError("Encrypted file needs the Python 'cryptography' package on the server")
    iv, ct = bytes(payload["iv"]), bytes(payload["ct"])
    return json.loads(AESGCM(compat_key(password)).decrypt(iv, ct, None))

def compat_encrypt(obj, password):
    if not HAVE_CRYPTO:
        return {"plain": True, "data": obj}
    iv = secrets.token_bytes(12)
    ct = AESGCM(compat_key(password)).encrypt(iv, json.dumps(obj).encode(), None)
    return {"iv": list(iv), "ct": list(ct)}

# ── database ───────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  user TEXT NOT NULL,
  pw_salt BLOB NOT NULL, pw_hash BLOB NOT NULL,
  sec_q TEXT, sec_salt BLOB, sec_hash BLOB,
  must_change INTEGER NOT NULL DEFAULT 1,
  kek_salt BLOB, dek_wrapped_pw BLOB,
  sec_kek_salt BLOB, dek_wrapped_sec BLOB
);
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, enc INTEGER NOT NULL, blob TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custom_services (id INTEGER PRIMARY KEY AUTOINCREMENT, pos INTEGER NOT NULL, enc INTEGER NOT NULL, blob TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scan_results (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, source TEXT NOT NULL, enc INTEGER NOT NULL, blob TEXT NOT NULL);
"""

class Store:
    def __init__(self, path):
        self.path = path
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
        old = os.umask(0o077)
        try:
            self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self.lock = threading.RLock()
        self.db.executescript(SCHEMA)
        if not self.account():
            self.create_default()

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def account(self):
        cur = self.db.execute("SELECT * FROM account WHERE id=1")
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None

    def create_default(self):
        salt = secrets.token_bytes(16)
        with self.lock:
            self.db.execute("INSERT INTO account (id,user,pw_salt,pw_hash,must_change) VALUES (1,?,?,?,1)",
                            ("admin", salt, kdf("forgepanel", salt)))
        log.info("Created default account admin/forgepanel (must change on first login)")

    def check_password(self, user, pw):
        a = self.account()
        ok_user = hmac.compare_digest(a["user"].encode(), user.encode())
        ok_pw = hmac.compare_digest(kdf(pw, a["pw_salt"]), a["pw_hash"])
        return ok_user and ok_pw

    def check_answer(self, ans):
        a = self.account()
        if not a["sec_hash"]:
            return False
        return hmac.compare_digest(kdf(ans.strip().lower(), a["sec_salt"]), a["sec_hash"])

    # data key handling
    def unlock(self, pw):
        """Return the data key (or None in plain mode), creating/migrating it if needed."""
        a = self.account()
        if not HAVE_CRYPTO:
            return None
        if a["dek_wrapped_pw"]:
            return aes_open(kdf(pw, a["kek_salt"]), a["dek_wrapped_pw"], b"dek")
        dek = secrets.token_bytes(32)          # first login with crypto: encrypt existing plain rows
        self.reencrypt(None, dek)
        self.wrap(dek, pw, None)
        return dek

    def unlock_with_answer(self, ans):
        a = self.account()
        if not HAVE_CRYPTO or not a["dek_wrapped_sec"]:
            return None
        return aes_open(kdf(ans.strip().lower(), a["sec_kek_salt"]), a["dek_wrapped_sec"], b"dek")

    def wrap(self, dek, pw, ans):
        if dek is None:
            return
        s1 = secrets.token_bytes(16)
        with self.lock:
            self.db.execute("UPDATE account SET kek_salt=?, dek_wrapped_pw=? WHERE id=1",
                            (s1, aes_seal(kdf(pw, s1), dek, b"dek")))
            if ans is not None:
                s2 = secrets.token_bytes(16)
                self.db.execute("UPDATE account SET sec_kek_salt=?, dek_wrapped_sec=? WHERE id=1",
                                (s2, aes_seal(kdf(ans.strip().lower(), s2), dek, b"dek")))

    def reencrypt(self, old, new):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                for table, key in (("settings", "name"), ("custom_services", "id"), ("scan_results", "id")):
                    for k, enc, blob in self.db.execute(f"SELECT {key}, enc, blob FROM {table}").fetchall():
                        e, b = seal(new, unseal(old, enc, blob))
                        self.db.execute(f"UPDATE {table} SET enc=?, blob=? WHERE {key}=?", (e, b, k))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def set_password(self, pw):
        salt = secrets.token_bytes(16)
        with self.lock:
            self.db.execute("UPDATE account SET pw_salt=?, pw_hash=? WHERE id=1", (salt, kdf(pw, salt)))

    def set_security(self, q, ans):
        salt = secrets.token_bytes(16)
        with self.lock:
            self.db.execute("UPDATE account SET sec_q=?, sec_salt=?, sec_hash=? WHERE id=1",
                            (q, salt, kdf(ans.strip().lower(), salt)))

    def rotate(self, old_dek, new_pw, ans):
        """New data key, re-encrypt everything, wrap with new password (+ answer)."""
        if not HAVE_CRYPTO:
            return None
        new = secrets.token_bytes(32)
        self.reencrypt(old_dek, new)
        if ans is None:  # keep answer-wrap impossible without the answer: drop it
            with self.lock:
                self.db.execute("UPDATE account SET sec_kek_salt=NULL, dek_wrapped_sec=NULL WHERE id=1")
        self.wrap(new, new_pw, ans)
        return new

    # dashboard data
    def load_saved(self, dek):
        rows = self.q("SELECT enc, blob FROM settings WHERE name='dashboard'")
        saved = unseal(dek, *rows[0]) if rows else {}
        saved["_custom"] = [unseal(dek, e, b) for e, b in self.q("SELECT enc, blob FROM custom_services ORDER BY pos, id")]
        return saved

    def save_saved(self, dek, saved):
        saved = dict(saved or {})
        customs = saved.pop("_custom", []) or []
        with self.lock:
            self.db.execute("BEGIN")
            try:
                e, b = seal(dek, saved)
                self.db.execute("INSERT OR REPLACE INTO settings (name, enc, blob) VALUES ('dashboard', ?, ?)", (e, b))
                self.save_customs(dek, customs, in_tx=True)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def save_customs(self, dek, customs, in_tx=False):
        with self.lock:
            self.db.execute("DELETE FROM custom_services")
            for i, c in enumerate(customs):
                e, b = seal(dek, c)
                self.db.execute("INSERT INTO custom_services (pos, enc, blob) VALUES (?,?,?)", (i, e, b))

    def add_scan(self, dek, result, source):
        e, b = seal(dek, result)
        with self.lock:
            self.db.execute("INSERT INTO scan_results (created, source, enc, blob) VALUES (?,?,?,?)",
                            (time.strftime("%Y-%m-%dT%H:%M:%S%z"), source, e, b))
            self.db.execute("DELETE FROM scan_results WHERE id NOT IN (SELECT id FROM scan_results ORDER BY id DESC LIMIT 10)")

    def latest_scan(self, dek):
        rows = self.q("SELECT created, source, enc, blob FROM scan_results ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        created, source, e, b = rows[0]
        return {"created": created, "source": source, "result": unseal(dek, e, b)}

# ── scanner (imports scan/scan.py) ─────────────────────────────────────────
def load_scanner():
    spec = importlib.util.spec_from_file_location("fp_scan", os.path.join(ROOT, "scan", "scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── server state ───────────────────────────────────────────────────────────
class State:
    def __init__(self, store):
        self.store = store
        self.sessions = {}      # token -> {"exp": t, "dek": key|None}
        self.fails = {}         # ip -> [timestamps]
        self.lock = threading.Lock()
        self.scan = {"running": False, "error": None, "started": None}

    def new_session(self, dek):
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[tok] = {"exp": time.time() + SESSION_TTL, "dek": dek}
        return tok

    def get_session(self, tok):
        with self.lock:
            s = self.sessions.get(tok)
            if s and s["exp"] > time.time():
                return s
            self.sessions.pop(tok, None)
            return None

    def drop_others(self, keep):
        with self.lock:
            for t in [t for t in self.sessions if t != keep]:
                del self.sessions[t]

    def limited(self, ip):
        now = time.time()
        with self.lock:
            lst = [t for t in self.fails.get(ip, []) if now - t < RATE_WINDOW]
            self.fails[ip] = lst
            return int(RATE_WINDOW - (now - lst[0])) + 1 if len(lst) >= RATE_MAX else 0

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def clear_fails(self, ip):
        with self.lock:
            self.fails.pop(ip, None)

STATE = None

def valid_pw(pw):
    return isinstance(pw, str) and len(pw) >= 6

# ── HTTP handler ───────────────────────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "ForgePanel"
    sys_version = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # static files with a deny list
    def blocked(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        parts = [p for p in posixpath.normpath(path).split("/") if p]
        if any(p.startswith(".") for p in parts):
            return True
        if parts and parts[0] in ("database", "data", "config", "__pycache__"):
            return True
        low = path.lower()
        if low.endswith((".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal", ".py", ".pyc", ".sh", ".conf")):
            return True
        if len(parts) >= 2 and parts[0] == "scan" and parts[1].startswith("results.") and parts[1] != "results.example.json":
            return True
        real = os.path.realpath(self.translate_path(self.path))
        return not (real == ROOT or real.startswith(ROOT + os.sep))

    def list_directory(self, path):
        self.send_error(403, "Directory listing disabled")
        return None

    def do_HEAD(self):
        if self.path.startswith("/api/") or self.blocked():
            return self.send_error(404)
        super().do_HEAD()

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self.api("GET")
        if self.blocked():
            return self.send_error(404)
        super().do_GET()

    def do_POST(self): self.api("POST") if self.path.startswith("/api/") else self.send_error(405)
    def do_PUT(self): self.api("PUT") if self.path.startswith("/api/") else self.send_error(405)

    # helpers
    def ip(self): return self.client_address[0]

    def send_json(self, code, obj, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("Request too large")
        if not n:
            return {}
        data = json.loads(self.rfile.read(n))
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    def token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def cookie(self, tok, age=SESSION_TTL):
        return f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}"

    # API
    def api(self, method):
        route = urllib.parse.urlsplit(self.path).path.rstrip("/")
        try:
            if method != "GET" and "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.send_json(415, {"error": "Content-Type must be application/json"})  # CSRF guard
            data = self.body() if method != "GET" else {}
            fn = ROUTES.get((method, route))
            if not fn:
                return self.send_json(404, {"error": "Not found"})
            fn(self, data)
        except (ValueError, KeyError, TypeError) as e:
            self.send_json(400, {"error": str(e) or "Bad request"})
        except Exception:
            log.exception("API error")
            self.send_json(500, {"error": "Server error"})

    def need_session(self, allow_must_change=False):
        tok = self.token()
        s = STATE.get_session(tok) if tok else None
        if not s:
            self.send_json(401, {"error": "Not signed in"})
            return None, None
        if not allow_must_change and STATE.store.account()["must_change"]:
            self.send_json(403, {"error": "Change your credentials first", "mustChange": True})
            return None, None
        return tok, s

    def rate_check(self):
        wait = STATE.limited(self.ip())
        if wait:
            self.send_json(429, {"error": f"Too many attempts. Try again in {wait}s."})
            return False
        return True

# route functions
def r_status(h, d):
    a = STATE.store.account()
    tok = h.token()
    s = STATE.get_session(tok) if tok else None
    h.send_json(200, {"authenticated": bool(s), "user": a["user"] if s else None,
                      "mustChange": bool(a["must_change"]) if s else None,
                      "hasSecurity": bool(a["sec_hash"]), "encryption": HAVE_CRYPTO})

def r_login(h, d):
    if not h.rate_check():
        return
    user, pw = str(d.get("user", "")), str(d.get("pw", ""))
    if not STATE.store.check_password(user, pw):
        STATE.fail(h.ip())
        return h.send_json(401, {"error": "Invalid username or password"})
    STATE.clear_fails(h.ip())
    tok = STATE.new_session(STATE.store.unlock(pw))
    h.send_json(200, {"ok": True, "mustChange": bool(STATE.store.account()["must_change"])}, h.cookie(tok))

def r_logout(h, d):
    tok = h.token()
    if tok:
        with STATE.lock:
            STATE.sessions.pop(tok, None)
    h.send_json(200, {"ok": True}, h.cookie("", 0))

def r_credentials(h, d):
    tok, s = h.need_session(allow_must_change=True)
    if not tok:
        return
    st = STATE.store
    a = st.account()
    cur = str(d.get("currentPw", ""))
    nu, np, q, ans = (str(d.get(k, "")).strip() for k in ("newUser", "newPw", "secQ", "secA"))
    np = str(d.get("newPw", ""))
    if not h.rate_check():
        return
    if not hmac.compare_digest(kdf(cur, a["pw_salt"]), a["pw_hash"]):
        STATE.fail(h.ip())
        return h.send_json(401, {"error": "Current password is wrong"})
    if not nu or not valid_pw(np):
        return h.send_json(400, {"error": "Enter a username and a password of at least 6 characters"})
    if np == "forgepanel":
        return h.send_json(400, {"error": "Choose a different password"})
    if not q or not ans:
        return h.send_json(400, {"error": "Set a security question and answer"})
    new_dek = st.rotate(s["dek"], np, ans)
    st.set_password(np)
    st.set_security(q, ans)
    st.q("UPDATE account SET user=?, must_change=0 WHERE id=1", (nu,))
    s["dek"] = new_dek
    STATE.drop_others(tok)
    h.send_json(200, {"ok": True})

def r_secq(h, d):
    a = STATE.store.account()
    h.send_json(200, {"question": a["sec_q"] if a["sec_hash"] else None})

def r_forgot_pw(h, d):
    if not h.rate_check():
        return
    st = STATE.store
    ans, np = str(d.get("answer", "")), str(d.get("newPw", ""))
    if not st.check_answer(ans):
        STATE.fail(h.ip())
        return h.send_json(401, {"error": "Incorrect answer"})
    if not valid_pw(np):
        return h.send_json(400, {"error": "Password must be at least 6 characters"})
    STATE.clear_fails(h.ip())
    old = st.unlock_with_answer(ans)
    wiped = False
    if HAVE_CRYPTO and old is None and st.account()["dek_wrapped_pw"]:
        # no answer-wrapped key (e.g. set up before encryption was enabled): data is unrecoverable
        for t in ("settings", "custom_services", "scan_results"):
            st.q(f"DELETE FROM {t}")
        wiped = True
        log.warning("Password reset without a recoverable data key: encrypted dashboard data was cleared")
    st.rotate(old, np, ans)
    st.set_password(np)
    STATE.drop_others(None)
    h.send_json(200, {"ok": True, "dataCleared": wiped})

def r_forgot_user(h, d):
    if not h.rate_check():
        return
    st = STATE.store
    ans, nu = str(d.get("answer", "")), str(d.get("newUser", "")).strip()
    if not st.check_answer(ans):
        STATE.fail(h.ip())
        return h.send_json(401, {"error": "Incorrect answer"})
    if not nu:
        return h.send_json(400, {"error": "Enter a username"})
    STATE.clear_fails(h.ip())
    st.q("UPDATE account SET user=? WHERE id=1", (nu,))
    h.send_json(200, {"ok": True})

def r_get_data(h, d):
    tok, s = h.need_session()
    if tok:
        h.send_json(200, {"saved": STATE.store.load_saved(s["dek"]), "encryption": HAVE_CRYPTO})

def r_put_data(h, d):
    tok, s = h.need_session()
    if tok:
        saved = d.get("saved")
        if not isinstance(saved, dict):
            raise ValueError("saved must be an object")
        STATE.store.save_saved(s["dek"], saved)
        h.send_json(200, {"ok": True})

def r_get_custom(h, d):
    tok, s = h.need_session()
    if tok:
        h.send_json(200, {"custom": STATE.store.load_saved(s["dek"])["_custom"]})

def r_put_custom(h, d):
    tok, s = h.need_session()
    if tok:
        customs = d.get("custom")
        if not isinstance(customs, list):
            raise ValueError("custom must be a list")
        STATE.store.save_customs(s["dek"], customs)
        h.send_json(200, {"ok": True})

def r_export(h, d):
    tok, s = h.need_session()
    if not tok or not h.rate_check():
        return
    a = STATE.store.account()
    pw = str(d.get("password", ""))
    if not hmac.compare_digest(kdf(pw, a["pw_salt"]), a["pw_hash"]):
        STATE.fail(h.ip())
        return h.send_json(401, {"error": "Wrong password"})
    saved = STATE.store.load_saved(s["dek"])
    h.send_json(200, {"account": a["user"], "data": compat_encrypt(saved, pw)})

def r_import(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    f = d.get("file") or {}
    try:
        saved = compat_decrypt(f.get("data"), str(d.get("password", "")))
    except Exception as e:
        return h.send_json(400, {"error": "Wrong password or corrupted file" if not str(e).startswith("Encrypted") else str(e)})
    if not isinstance(saved, dict):
        return h.send_json(400, {"error": "Not a ForgePanel config"})
    STATE.store.save_saved(s["dek"], saved)
    h.send_json(200, {"ok": True})

def run_scan_job(tok, subnet):
    try:
        sc = load_scanner()
        conf = sc.load_conf(os.path.join(ROOT, "scan", "scan.conf"))
        try:
            spec, hosts = sc.resolve_hosts(subnet or conf["subnet"], conf["max_hosts"])
        except SystemExit as e:
            raise ValueError(str(e))
        found = sc.scan(conf, hosts)
        result = {"format": "forgepanel-scan", "version": 1, "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "subnet": spec, "hosts_scanned": len(hosts), "services": found}
        s = STATE.get_session(tok)
        if not s:
            raise ValueError("Session ended before the scan finished")
        STATE.store.add_scan(s["dek"], result, "server")
        STATE.scan["error"] = None
    except Exception as e:
        log.warning("Scan failed: %s", e)
        STATE.scan["error"] = str(e)
    finally:
        STATE.scan["running"] = False

def r_scan_start(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    with STATE.lock:
        if STATE.scan["running"]:
            return h.send_json(409, {"error": "A scan is already running"})
        STATE.scan.update(running=True, error=None, started=time.time())
    subnet = d.get("subnet")
    threading.Thread(target=run_scan_job, args=(tok, str(subnet) if subnet else None), daemon=True).start()
    h.send_json(202, {"ok": True, "running": True})

def r_scan_get(h, d):
    tok, s = h.need_session()
    if tok:
        h.send_json(200, {"running": STATE.scan["running"], "error": STATE.scan["error"],
                          "latest": STATE.store.latest_scan(s["dek"])})

def r_scan_import(h, d):
    """Import a CLI scan: uploaded payload, or scan/results.enc|json from the server folder."""
    tok, s = h.need_session()
    if not tok:
        return
    payload = d.get("payload")
    if payload is None and d.get("fromServer"):
        for name in ("results.enc", "results.json"):
            p = os.path.join(ROOT, "scan", name)
            if os.path.exists(p):
                with open(p) as fh:
                    payload = json.load(fh)
                break
        if payload is None:
            return h.send_json(404, {"error": "No scan/results.enc or scan/results.json on the server. Use Scan Network or run scan.py."})
    if not isinstance(payload, dict):
        raise ValueError("payload missing")
    try:
        result = compat_decrypt(payload, str(d.get("password", "")))
    except Exception as e:
        msg = str(e) if str(e).startswith("Encrypted") else "Could not decrypt. Use the password the scan was encrypted with."
        return h.send_json(400, {"error": msg})
    if not isinstance(result, dict) or not isinstance(result.get("services"), list):
        return h.send_json(400, {"error": "Not a ForgePanel scan file"})
    STATE.store.add_scan(s["dek"], result, "import")
    h.send_json(200, {"ok": True, "latest": STATE.store.latest_scan(s["dek"])})

ROUTES = {
    ("GET", "/api/status"): r_status,
    ("POST", "/api/login"): r_login,
    ("POST", "/api/logout"): r_logout,
    ("POST", "/api/credentials"): r_credentials,
    ("GET", "/api/security-question"): r_secq,
    ("POST", "/api/forgot-password"): r_forgot_pw,
    ("POST", "/api/forgot-username"): r_forgot_user,
    ("GET", "/api/data"): r_get_data,
    ("PUT", "/api/data"): r_put_data,
    ("GET", "/api/custom"): r_get_custom,
    ("PUT", "/api/custom"): r_put_custom,
    ("POST", "/api/export"): r_export,
    ("POST", "/api/import"): r_import,
    ("POST", "/api/scan"): r_scan_start,
    ("GET", "/api/scan"): r_scan_get,
    ("POST", "/api/scan/import"): r_scan_import,
}

def main():
    global STATE
    ap = argparse.ArgumentParser(description="ForgePanel server")
    ap.add_argument("--port", type=int, default=8084)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not HAVE_CRYPTO:
        log.warning("Python 'cryptography' not installed: dashboard data and scan results are stored "
                    "UNENCRYPTED in %s. Install python3-cryptography to enable encryption at rest.", args.db)
    STATE = State(Store(os.path.abspath(args.db)))
    srv = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("ForgePanel on http://%s:%d (database: %s)", args.host, args.port, args.db)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
