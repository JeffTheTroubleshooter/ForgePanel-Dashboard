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
import shutil
import subprocess
import re
import socket
import ssl
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
CUSTOM_DIR = os.path.join(ROOT, "custom_services")
GEN_MARK = "forgepanel-scan"   # "generated_by" value of scan-written custom_services/*.json
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
CREATE TABLE IF NOT EXISTS uptime (key TEXT NOT NULL, ts INTEGER NOT NULL, up INTEGER NOT NULL, ms REAL);
CREATE INDEX IF NOT EXISTS uptime_key_ts ON uptime (key, ts);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
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
        sync_custom_files(customs)

    def add_scan(self, dek, result, source):
        e, b = seal(dek, result)
        with self.lock:
            self.db.execute("INSERT INTO scan_results (created, source, enc, blob) VALUES (?,?,?,?)",
                            (time.strftime("%Y-%m-%dT%H:%M:%S%z"), source, e, b))
            self.db.execute("DELETE FROM scan_results WHERE id NOT IN (SELECT id FROM scan_results ORDER BY id DESC LIMIT 50)")

    def get_setting(self, dek, name, default=None):
        rows = self.q("SELECT enc, blob FROM settings WHERE name=?", (name,))
        return unseal(dek, *rows[0]) if rows else default

    def set_setting(self, dek, name, obj):
        e, b = seal(dek, obj)
        self.q("INSERT OR REPLACE INTO settings (name, enc, blob) VALUES (?,?,?)", (name, e, b))

    def meta(self, name, default=None):
        rows = self.q("SELECT value FROM meta WHERE name=?", (name,))
        return rows[0][0] if rows else default

    def set_meta(self, name, value):
        self.q("INSERT OR REPLACE INTO meta (name, value) VALUES (?,?)", (name, str(value)))

    def scan_history(self, dek, limit=50):
        out = []
        for i, created, source, e, b in self.q("SELECT id, created, source, enc, blob FROM scan_results ORDER BY id DESC LIMIT ?", (limit,)):
            try:
                r = unseal(dek, e, b)
                out.append({"id": i, "created": created, "source": source, "subnet": r.get("subnet"),
                            "count": len(r.get("services", []))})
            except Exception:
                out.append({"id": i, "created": created, "source": source, "error": "cannot decrypt"})
        return out

    def latest_scan(self, dek):
        rows = self.q("SELECT created, source, enc, blob FROM scan_results ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        created, source, e, b = rows[0]
        return {"created": created, "source": source, "result": unseal(dek, e, b)}

# ── custom_services/ files for scan-found services ─────────────────────────
def slug(text):
    return re.sub(r"[^a-z0-9.]+", "-", str(text).lower()).strip("-.")[:80] or "service"

def custom_file_record(c):
    """Shareable, secret-free description of a scan-found custom service, or None."""
    if not isinstance(c, dict) or c.get("source") != "scan":
        return None
    host = str(c.get("ip") or "").strip()
    try:
        port = int(c.get("port") or 0)
    except (TypeError, ValueError):
        return None
    if not host or not re.fullmatch(r"[A-Za-z0-9.:\-\[\]]+", host) or not 0 < port < 65536:
        return None
    scheme = "https" if c.get("scheme") == "https" else "http"
    name = str(c.get("name") or f"Service {port}")[:80]
    default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    fname = f"{slug(name.split(' (')[0])}-{slug(host)}-{port}.json"
    return fname, {
        "generated_by": GEN_MARK, "name": name, "host": host, "port": port, "scheme": scheme,
        "url": f"{scheme}://{host}{'' if default else ':' + str(port)}",
        "source": "scan", "found_at": c.get("found_at"), "matched_by": c.get("matched_by"),
    }

def sync_custom_files(customs, folder=None):
    """Write one JSON per scan-found custom service; remove stale scan-written files.
    Hand-made files (without generated_by=forgepanel-scan) are never touched."""
    folder = folder or CUSTOM_DIR
    try:
        os.makedirs(folder, exist_ok=True)
        want = {}
        for c in customs or []:
            rec = custom_file_record(c)
            if rec and rec[0] not in want:
                want[rec[0]] = rec[1]
        for fn in os.listdir(folder):
            if not fn.endswith(".json") or fn in want:
                continue
            path = os.path.join(folder, fn)
            try:
                with open(path) as fh:
                    gen = json.load(fh).get("generated_by") == GEN_MARK
            except (OSError, ValueError, AttributeError):
                gen = False
            if gen:
                os.remove(path)
        for fn, data in want.items():
            path = os.path.join(folder, fn)
            text = json.dumps(data, indent=2) + "\n"
            try:
                with open(path) as fh:
                    if fh.read() == text:
                        continue
            except OSError:
                pass
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(text)
            os.replace(tmp, path)
    except OSError as e:
        log.warning("Could not update %s: %s", folder, e)

# ── server-side status check (no browser CORS / self-signed TLS issues) ────
def check_service(host, port, scheme, timeout=3.0):
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return {"online": False}
    detected = None
    try:
        sock.settimeout(timeout)
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                with ctx.wrap_socket(sock, server_hostname=host):
                    detected = "https"
            except (ssl.SSLError, OSError):
                detected = "http"   # TCP open but not TLS
        else:
            sock.sendall(f"HEAD / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
            try:
                head = sock.recv(64)
            except OSError:
                head = b""
            if head.startswith(b"HTTP/"):
                detected = "http"
            elif head[:1] == b"\x15" or b"\x15\x03" in head[:3]:
                detected = "https"  # TLS alert: server speaks HTTPS
            else:
                detected = None
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if detected is None and scheme == "http":
        # no HTTP reply: see whether the port speaks TLS instead
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=host):
                    detected = "https"
        except (ssl.SSLError, OSError):
            pass
    return {"online": True, "scheme": detected}

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
        self.scan = {"running": False, "error": None, "started": None, "source": None}
        # Data key for background jobs (auto-scan, uptime). Memory only; set at the first
        # sign-in after the server starts. Plain mode needs no key.
        self.worker = {"ready": not HAVE_CRYPTO, "dek": None}

    def set_worker(self, dek):
        self.worker = {"ready": True, "dek": dek}

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
        if len(parts) >= 2 and parts[0] == "custom_services" and parts[-1].endswith(".json") and parts[-1] != "example.json":
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
        secure = "; Secure" if getattr(self.server, "is_https", False) else ""
        return f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}"

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
    dek = STATE.store.unlock(pw)
    STATE.set_worker(dek)
    tok = STATE.new_session(dek)
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
    STATE.set_worker(new_dek)
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
    STATE.set_worker(st.rotate(old, np, ans))
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

def svc_key(x):
    return f"{x.get('ip')}:{x.get('port')}"

def known_keys(saved):
    keys = set()
    for k, v in saved.items():
        if k == "_custom" or not isinstance(v, dict):
            continue
        if v.get("ip"):
            keys.add(svc_key(v))
    for c in saved.get("_custom", []):
        if isinstance(c, dict) and c.get("ip"):
            keys.add(svc_key(c))
    return keys

def flag_new(dek, result):
    """Add services not on the dashboard and not dismissed to the 'New' list."""
    st = STATE.store
    saved = st.load_saved(dek)
    known = known_keys(saved)
    dismissed = set(st.get_setting(dek, "scan_dismissed", []))
    pending = {svc_key(x): x for x in st.get_setting(dek, "scan_pending", [])}
    added = 0
    for x in result.get("services", []):
        k = svc_key(x)
        if k in known or k in dismissed or k in pending:
            continue
        pending[k] = dict(x, flagged_at=result.get("generated"))
        added += 1
    st.set_setting(dek, "scan_pending", list(pending.values()))
    return added

def run_scan_job(subnet, source="server", flag=False):
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
        if not STATE.worker["ready"]:
            raise ValueError("Data key not available (sign in once after the server starts)")
        dek = STATE.worker["dek"]
        STATE.store.add_scan(dek, result, source)
        if flag:
            n = flag_new(dek, result)
            log.info("Auto-scan found %d service(s), %d new", len(found), n)
        STATE.scan["error"] = None
    except Exception as e:
        log.warning("Scan failed: %s", e)
        STATE.scan["error"] = str(e)
    finally:
        STATE.scan["running"] = False
        STATE.store.set_meta("last_scan", int(time.time()))

def start_scan(subnet=None, source="server", flag=False):
    with STATE.lock:
        if STATE.scan["running"]:
            return False
        STATE.scan.update(running=True, error=None, started=time.time(), source=source)
    threading.Thread(target=run_scan_job, args=(subnet, source, flag), daemon=True).start()
    return True

def r_scan_start(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    subnet = d.get("subnet")
    if not start_scan(str(subnet) if subnet else None, "server"):
        return h.send_json(409, {"error": "A scan is already running"})
    h.send_json(202, {"ok": True, "running": True})

def r_scan_get(h, d):
    tok, s = h.need_session()
    if tok:
        h.send_json(200, {"running": STATE.scan["running"], "error": STATE.scan["error"], "source": STATE.scan["source"],
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

def r_check(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    items = d.get("items")
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("items must be a list (max 200)")
    def one(it):
        host = str(it.get("ip") or "").strip()
        try:
            port = int(it.get("port"))
        except (TypeError, ValueError):
            return {"online": False}
        if not host or not re.fullmatch(r"[A-Za-z0-9.:\-\[\]]+", host) or not 0 < port < 65536:
            return {"online": False}
        return check_service(host, port, "https" if it.get("scheme") == "https" else "http")
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(16) as ex:
        results = list(ex.map(one, items))
    h.send_json(200, {"results": results})

# ── 1) scheduled scans ─────────────────────────────────────────────────────
SCHEDULE_PRESETS = {"off": 0, "hourly": 60, "daily": 1440}

def conf_schedule():
    import configparser
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read(os.path.join(ROOT, "scan", "scan.conf"))
    mode = cp.get("schedule", "interval", fallback="off").strip().lower()
    minutes = cp.getint("schedule", "minutes", fallback=360)
    return normalize_schedule({"mode": mode, "minutes": minutes})

def normalize_schedule(sch):
    mode = str((sch or {}).get("mode", "off")).lower()
    if mode not in ("off", "hourly", "daily", "custom"):
        mode = "off"
    try:
        minutes = int((sch or {}).get("minutes") or 360)
    except (TypeError, ValueError):
        minutes = 360
    minutes = max(5, min(minutes, 7 * 1440))
    return {"mode": mode, "minutes": minutes,
            "interval": SCHEDULE_PRESETS.get(mode, minutes) if mode != "custom" else minutes}

def current_schedule():
    if STATE.worker["ready"]:
        try:
            sch = STATE.store.get_setting(STATE.worker["dek"], "schedule")
            if sch:
                return normalize_schedule(sch), "dashboard"
        except Exception:
            pass
    return conf_schedule(), "scan.conf"

def scheduler_loop():
    while True:
        time.sleep(30)
        try:
            sch, _ = current_schedule()
            if not sch["interval"] or not STATE.worker["ready"]:
                continue
            last = int(STATE.store.meta("last_auto_scan", 0))
            if time.time() - last >= sch["interval"] * 60:
                STATE.store.set_meta("last_auto_scan", int(time.time()))
                start_scan(None, "auto", flag=True)
        except Exception:
            log.exception("scheduler")

def r_schedule_get(h, d):
    tok, s = h.need_session()
    if tok:
        sch, src = current_schedule()
        last = int(STATE.store.meta("last_auto_scan", 0))
        h.send_json(200, dict(sch, source=src, last_auto_scan=last or None,
                              next_auto_scan=(last + sch["interval"] * 60) if sch["interval"] and last else None))

def r_schedule_put(h, d):
    tok, s = h.need_session()
    if tok:
        sch = normalize_schedule(d)
        STATE.store.set_setting(s["dek"], "schedule", {"mode": sch["mode"], "minutes": sch["minutes"]})
        if sch["interval"]:
            STATE.store.set_meta("last_auto_scan", int(time.time()))  # first run after one interval
        h.send_json(200, dict(sch, ok=True))

def r_scan_history(h, d):
    tok, s = h.need_session()
    if tok:
        h.send_json(200, {"history": STATE.store.scan_history(s["dek"])})

def r_pending_get(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    st = STATE.store
    known = known_keys(st.load_saved(s["dek"]))
    pending = [x for x in st.get_setting(s["dek"], "scan_pending", []) if svc_key(x) not in known]
    st.set_setting(s["dek"], "scan_pending", pending)
    h.send_json(200, {"pending": pending})

def r_pending_post(h, d):
    """action=dismiss: hide for good; action=accept: remove from list (client adds the tiles)."""
    tok, s = h.need_session()
    if not tok:
        return
    action, keys = d.get("action"), d.get("keys")
    if action not in ("accept", "dismiss") or not isinstance(keys, list):
        raise ValueError("action must be accept|dismiss and keys a list")
    keys = {str(k) for k in keys}
    st = STATE.store
    pending = st.get_setting(s["dek"], "scan_pending", [])
    chosen = [x for x in pending if svc_key(x) in keys]
    st.set_setting(s["dek"], "scan_pending", [x for x in pending if svc_key(x) not in keys])
    if action == "dismiss":
        dismissed = set(st.get_setting(s["dek"], "scan_dismissed", [])) | {svc_key(x) for x in chosen}
        st.set_setting(s["dek"], "scan_dismissed", sorted(dismissed))
    h.send_json(200, {"ok": True, "services": chosen})

# ── 2) uptime + response time ──────────────────────────────────────────────
UPTIME = {"interval": 60, "retention_days": 8}

def uptime_key(ip, port):
    salt = STATE.store.meta("uptime_salt")
    if not salt:
        salt = secrets.token_hex(16)
        STATE.store.set_meta("uptime_salt", salt)
    # opaque key so the plain uptime table does not list your hosts
    return hmac.new(salt.encode(), f"{ip}:{port}".encode(), hashlib.sha256).hexdigest()[:32]

def monitor_targets():
    if not STATE.worker["ready"]:
        return []
    saved = STATE.store.load_saved(STATE.worker["dek"])
    out = {}
    for k, v in saved.items():
        if isinstance(v, dict) and v.get("ip") and k != "_custom":
            out[svc_key(v)] = v
    for c in saved.get("_custom", []):
        if isinstance(c, dict) and c.get("ip"):
            out[svc_key(c)] = c
    return list(out.values())

def probe_latency(c):
    host = str(c.get("ip", "")).strip()
    try:
        port = int(c.get("port") or 80)
    except (TypeError, ValueError):
        return None
    if not re.fullmatch(r"[A-Za-z0-9.:\-\[\]]+", host):
        return None
    t0 = time.monotonic()
    r = check_service(host, port, "https" if c.get("scheme") == "https" else "http", timeout=3.0)
    return host, port, r["online"], round((time.monotonic() - t0) * 1000, 1)

def uptime_loop():
    import concurrent.futures as cf
    while True:
        try:
            targets = monitor_targets()
            if targets:
                now = int(time.time())
                with cf.ThreadPoolExecutor(16) as ex:
                    rows = [r for r in ex.map(probe_latency, targets) if r]
                with STATE.store.lock:
                    STATE.store.db.executemany("INSERT INTO uptime (key, ts, up, ms) VALUES (?,?,?,?)",
                                               [(uptime_key(h_, p_), now, int(up), ms if up else None) for h_, p_, up, ms in rows])
                    STATE.store.db.execute("DELETE FROM uptime WHERE ts < ?", (now - UPTIME["retention_days"] * 86400,))
        except Exception:
            log.exception("uptime checker")
        time.sleep(UPTIME["interval"])

def r_uptime(h, d):
    tok, s = h.need_session()
    if not tok:
        return
    rng = d.get("range", "24h")
    span = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}.get(rng)
    if not span:
        raise ValueError("range must be 1h, 24h or 7d")
    buckets = max(10, min(int(d.get("buckets") or 48), 300))
    items = d.get("items")
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("items must be a list (max 200)")
    now = int(time.time())
    start, width = now - span, span / buckets
    out = []
    for it in items:
        key = uptime_key(str(it.get("ip", "")), it.get("port"))
        rows = STATE.store.q("SELECT ts, up, ms FROM uptime WHERE key=? AND ts>=? ORDER BY ts", (key, start))
        b = [{"n": 0, "up": 0, "ms": 0.0, "msn": 0} for _ in range(buckets)]
        for ts, up, ms in rows:
            x = b[min(buckets - 1, int((ts - start) / width))]
            x["n"] += 1
            x["up"] += up
            if ms is not None:
                x["ms"] += ms
                x["msn"] += 1
        series = [{"t": int(start + i * width), "up": (x["up"] / x["n"]) if x["n"] else None,
                   "ms": round(x["ms"] / x["msn"], 1) if x["msn"] else None} for i, x in enumerate(b)]
        total = len(rows)
        ups = sum(r[1] for r in rows)
        last = rows[-1] if rows else None
        out.append({"uptime": round(100 * ups / total, 2) if total else None, "samples": total,
                    "last": {"ts": last[0], "up": bool(last[1]), "ms": last[2]} if last else None, "series": series})
    h.send_json(200, {"range": rng, "interval": UPTIME["interval"], "results": out})

# ── 5) server stats from /proc ─────────────────────────────────────────────
STATS = {"history": [], "prev_cpu": None, "prev_net": None, "prev_t": None}

def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""

def sample_stats():
    now = time.time()
    cpu_pct = None
    line = _read("/proc/stat").split("\n", 1)[0].split()
    if line and line[0] == "cpu":
        vals = [int(x) for x in line[1:]]
        idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
        if STATS["prev_cpu"]:
            di, dt = idle - STATS["prev_cpu"][0], total - STATS["prev_cpu"][1]
            cpu_pct = round(100 * (1 - di / dt), 1) if dt > 0 else 0.0
        STATS["prev_cpu"] = (idle, total)
    mem = {}
    for l in _read("/proc/meminfo").splitlines():
        k, _, v = l.partition(":")
        if v.strip():
            mem[k] = int(v.split()[0]) * 1024
    mem_total = mem.get("MemTotal", 0)
    mem_used = mem_total - mem.get("MemAvailable", mem.get("MemFree", 0))
    rx = tx = 0
    for l in _read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = l.partition(":")
        f = rest.split()
        if name.strip() != "lo" and len(f) >= 9:
            rx += int(f[0]); tx += int(f[8])
    rx_rate = tx_rate = None
    if STATS["prev_net"] and STATS["prev_t"]:
        dt = now - STATS["prev_t"]
        if dt > 0:
            rx_rate = max(0, round((rx - STATS["prev_net"][0]) / dt))
            tx_rate = max(0, round((tx - STATS["prev_net"][1]) / dt))
    STATS["prev_net"], STATS["prev_t"] = (rx, tx), now
    try:
        du = shutil.disk_usage(ROOT)
        disk = {"total": du.total, "used": du.used, "free": du.free, "pct": round(100 * du.used / du.total, 1)}
    except OSError:
        disk = None
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = None
    up = _read("/proc/uptime").split()
    snap = {"t": int(now), "cpu": cpu_pct, "cores": os.cpu_count(),
            "mem": {"total": mem_total, "used": mem_used, "pct": round(100 * mem_used / mem_total, 1) if mem_total else None},
            "disk": disk, "load": load, "uptime": int(float(up[0])) if up else None,
            "net": {"rx_total": rx, "tx_total": tx, "rx_rate": rx_rate, "tx_rate": tx_rate}}
    STATS["history"] = (STATS["history"] + [snap])[-120:]
    return snap

def stats_loop():
    while True:
        try:
            sample_stats()
        except Exception:
            log.exception("stats")
        time.sleep(5)

def r_stats(h, d):
    tok, s = h.need_session()
    if tok:
        hist = STATS["history"] or [sample_stats()]
        h.send_json(200, {"current": hist[-1], "history": [
            {"t": x["t"], "cpu": x["cpu"], "mem": x["mem"]["pct"], "rx": x["net"]["rx_rate"], "tx": x["net"]["tx_rate"]} for x in hist]})

# ── 3) HTTPS ───────────────────────────────────────────────────────────────
def ensure_cert(cert, key):
    if os.path.exists(cert) and os.path.exists(key):
        return
    if not shutil.which("openssl"):
        sys.exit("--https needs a certificate. Install openssl or pass --cert/--key.")
    os.makedirs(os.path.dirname(cert), mode=0o700, exist_ok=True)
    host = socket.gethostname()
    ips = {"127.0.0.1"}
    try:
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_.connect(("192.0.2.1", 80))
        ips.add(s_.getsockname()[0])
        s_.close()
    except OSError:
        pass
    san = ",".join([f"DNS:{host}", "DNS:localhost"] + [f"IP:{i}" for i in sorted(ips)])
    old = os.umask(0o077)
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "825",
                        "-keyout", key, "-out", cert, "-subj", f"/CN={host}", "-addext", f"subjectAltName={san}"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        os.umask(old)
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)
    log.info("Generated self-signed certificate %s (%s)", cert, san)

ROUTES = {
    ("GET", "/api/schedule"): r_schedule_get,
    ("PUT", "/api/schedule"): r_schedule_put,
    ("GET", "/api/scan/history"): r_scan_history,
    ("GET", "/api/scan/pending"): r_pending_get,
    ("POST", "/api/scan/pending"): r_pending_post,
    ("POST", "/api/uptime"): r_uptime,
    ("GET", "/api/stats"): r_stats,
    ("POST", "/api/check"): r_check,
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
    ap.add_argument("--https", action="store_true", help="serve HTTPS (self-signed cert generated in database/certs/ if missing)")
    ap.add_argument("--cert", help="certificate file (default database/certs/server.crt)")
    ap.add_argument("--key", help="private key file (default database/certs/server.key)")
    ap.add_argument("--uptime-interval", type=int, default=60, help="seconds between uptime checks (default 60)")
    ap.add_argument("--uptime-retention", type=int, default=8, help="days of uptime history to keep (default 8)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not HAVE_CRYPTO:
        log.warning("Python 'cryptography' not installed: dashboard data and scan results are stored "
                    "UNENCRYPTED in %s. Install python3-cryptography to enable encryption at rest.", args.db)
    db = os.path.abspath(args.db)
    STATE = State(Store(db))
    UPTIME["interval"] = max(10, args.uptime_interval)
    UPTIME["retention_days"] = max(1, args.uptime_retention)
    srv = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    srv.is_https = False
    if args.https:
        certdir = os.path.join(os.path.dirname(db), "certs")
        cert = os.path.abspath(args.cert or os.path.join(certdir, "server.crt"))
        key = os.path.abspath(args.key or os.path.join(certdir, "server.key"))
        ensure_cert(cert, key)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        # handshake in the handler thread, so a slow client cannot block accept()
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        srv.is_https = True
    for fn in (scheduler_loop, uptime_loop, stats_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    log.info("ForgePanel on %s://%s:%d (database: %s)", "https" if srv.is_https else "http", args.host, args.port, db)
    if HAVE_CRYPTO:
        log.info("Auto-scans and uptime checks start after the first sign-in (the data key lives in memory only)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
