"""Integration tests for ForgePanel's server and API.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Each test gets its own temporary SQLite database and an HTTP server on an
ephemeral port, so nothing touches database/ or your real data.
"""
import http.client
import http.server
import ipaddress
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fp_ext          # noqa: E402
import server          # noqa: E402

logging.disable(logging.CRITICAL)


class ForgePanelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgepanel-test-")
        self.db = os.path.join(self.tmp, "forgepanel.db")
        server.STATE = server.State(server.Store(self.db))
        fp_ext.migrate()
        server.SYS_FORCED.clear()
        server.load_sys()
        self._old_custom_dir = server.CUSTOM_DIR
        server.CUSTOM_DIR = os.path.join(self.tmp, "custom_services")
        fp_ext.PREV.clear()
        fp_ext.WCACHE.clear()
        fp_ext.PUB_CACHE.update(sig=None, ts=0, data=None)
        server.TRUSTED_PROXIES = []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.srv.is_https = False
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        server.STATE.store.close()
        server.CUSTOM_DIR = self._old_custom_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ─────────────────────────────────────────────────────────────
    def req(self, method, path, body=None, headers=None, cookie=None):
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        if cookie:
            hdrs["Cookie"] = cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            r = conn.getresponse()
            raw = r.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            return r.status, payload, r.getheader("Set-Cookie") or "", dict(r.getheaders())
        finally:
            conn.close()

    def login(self, pw="forgepanel", headers=None):
        st, d, sc, _ = self.req("POST", "/api/login", {"user": "admin", "pw": pw}, headers)
        self.assertEqual(st, 200, d)
        return sc.split(";")[0]

    def complete_setup(self, headers=None):
        """Sign in, replace the default credentials, and return the session cookie."""
        cookie = self.login(headers=headers)
        st, d, _, _ = self.req("POST", "/api/credentials", {
            "currentPw": "forgepanel", "newUser": "admin", "newPw": "correct-horse",
            "secQ": "What was the name of your first pet?", "secA": "rex"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        return cookie

    def test_credentials_require_preset_security_question(self):
        cookie = self.login()
        st, d, _, _ = self.req("POST", "/api/credentials", {
            "currentPw": "forgepanel", "newUser": "admin", "newPw": "correct-horse",
            "secQ": "my own question?", "secA": "rex"}, cookie=cookie)
        self.assertEqual(st, 400, d)
        st, d, _, _ = self.req("POST", "/api/credentials", {
            "currentPw": "forgepanel", "newUser": "admin", "newPw": "correct-horse",
            "secQ": "What is your favorite movie?", "secA": "alien"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        st, d, _, _ = self.req("GET", "/api/security-question")
        self.assertEqual(d["question"], "What is your favorite movie?")

    def put_data(self, saved, cookie):
        st, d, _, _ = self.req("PUT", "/api/data", {"saved": saved}, cookie=cookie)
        self.assertEqual(st, 200, d)

    # ── user icons ──────────────────────────────────────────────────────────
    def test_icon_upload_serve_and_limits(self):
        import base64
        cookie = self.complete_setup()
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        st, d, _, _ = self.req("POST", "/api/icons", {"data": url})
        self.assertEqual(st, 401)
        st, d, _, _ = self.req("POST", "/api/icons", {"data": url}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertTrue(d["url"].startswith("/api/icons/"))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", d["url"], headers={"Cookie": cookie})
        r = conn.getresponse(); body = r.read(); conn.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(body, png)
        self.assertEqual(r.getheader("Content-Type"), "image/png")
        self.assertIn("sandbox", " ".join(v for k, v in r.getheaders() if k.lower() == "content-security-policy"))
        st, _, _, _ = self.req("GET", d["url"])
        self.assertEqual(st, 401)
        # persisted through the tiles storage
        self.put_data({"_custom": [{"id": "a1", "name": "X", "ip": "10.0.0.5", "port": 80, "logo": d["url"]}]}, cookie)
        st, g, _, _ = self.req("GET", "/api/data", cookie=cookie)
        self.assertEqual(g["saved"]["_custom"][0]["logo"], d["url"])
        big = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * (300 * 1024)).decode()
        st, d, _, _ = self.req("POST", "/api/icons", {"data": big}, cookie=cookie)
        self.assertEqual(st, 400)
        st, d, _, _ = self.req("POST", "/api/icons", {"data": "data:image/png;base64," + base64.b64encode(b"GIF89a....").decode()}, cookie=cookie)
        self.assertEqual(st, 400)

    def test_svg_icon_sanitized(self):
        import base64
        cookie = self.complete_setup()
        enc = lambda t: "data:image/svg+xml;base64," + base64.b64encode(t.encode()).decode()
        ok = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3" fill="#fff"/></svg>'
        st, d, _, _ = self.req("POST", "/api/icons", {"data": enc(ok)}, cookie=cookie)
        self.assertEqual(st, 200, d)
        for bad in ['<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil/x.png"/></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject></foreignObject></svg>']:
            st, d, _, _ = self.req("POST", "/api/icons", {"data": enc(bad)}, cookie=cookie)
            self.assertEqual(st, 400, bad)

    def test_bundled_icons_list_has_unknown_not_generic(self):
        cookie = self.complete_setup()
        st, d, _, _ = self.req("GET", "/api/icons/bundled", cookie=cookie)
        self.assertEqual(st, 200)
        names = [x["name"] for x in d["icons"]]
        self.assertIn("unknown", names)
        self.assertIn("nextcloud", names)
        self.assertNotIn("generic", names)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "assets", "icons", "unknown.svg")))

    # ── basic surface ───────────────────────────────────────────────────────
    def test_health_is_public_and_sets_security_headers(self):
        st, d, _, hdrs = self.req("GET", "/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["app"], "forgepanel")
        self.assertIn("version", d)
        self.assertEqual(hdrs.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("default-src 'self'", hdrs.get("Content-Security-Policy", ""))
        self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")

    def test_idle_session_expires_and_passive_polls_do_not_refresh(self):
        cookie = self.complete_setup()
        old = server.IDLE_TIMEOUT
        server.IDLE_TIMEOUT = 60
        try:
            tok = cookie.split("=", 1)[1]
            sess = server.STATE.sessions[tok]
            # Real activity (no passive header) refreshes last-seen.
            sess["seen"] = time.time() - 50
            st, _, _, _ = self.req("POST", "/api/heartbeat", {}, cookie=cookie)
            self.assertEqual(st, 200)
            self.assertGreater(sess["seen"], time.time() - 5)
            # Background polling with X-FP-Passive does not refresh it.
            sess["seen"] = time.time() - 50
            st, _, _, _ = self.req("GET", "/api/data", headers={"X-FP-Passive": "1"}, cookie=cookie)
            self.assertEqual(st, 200)
            self.assertLess(sess["seen"], time.time() - 45)
            # Idle past the limit: rejected and dropped.
            sess["seen"] = time.time() - 61
            st, d, _, _ = self.req("GET", "/api/data", headers={"X-FP-Passive": "1"}, cookie=cookie)
            self.assertEqual(st, 401)
            st, d, _, _ = self.req("GET", "/api/status", cookie=cookie)
            self.assertFalse(d["authenticated"])
            self.assertNotIn(tok, server.STATE.sessions)
        finally:
            server.IDLE_TIMEOUT = old

    def test_private_api_requires_a_session(self):
        for path in ("/api/data", "/api/custom", "/api/stats", "/api/schedule"):
            st, d, _, _ = self.req("GET", path)
            self.assertEqual(st, 401, path)
            self.assertEqual(d["error"], "Not signed in")

    def test_static_files_and_deny_list(self):
        st, _, _, _ = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertEqual(self.req("GET", "/pages/dashboard.html")[0], 200)
        self.assertEqual(self.req("GET", "/css/main.css")[0], 200)
        self.assertEqual(self.req("GET", "/js/auth.js")[0], 200)
        for path in ("/server.py", "/fp_ext.py", "/database/forgepanel.db",
                     "/config/forgepanel.conf", "/scan/results.enc",
                     "/pages/../server.py", "/assets/../../etc/passwd"):
            self.assertEqual(self.req("GET", path)[0], 404, path)
        self.assertEqual(self.req("GET", "/assets/")[0], 403)   # no directory listing

    # ── auth + data ─────────────────────────────────────────────────────────
    def test_must_change_gate_then_data_roundtrip(self):
        cookie = self.login()
        st, d, _, _ = self.req("GET", "/api/data", cookie=cookie)
        self.assertEqual(st, 403)
        self.assertTrue(d["mustChange"])

        cookie = self.complete_setup()
        saved = {
            "Jellyfin": {"ip": "192.168.1.10", "port": 8096, "scheme": "http"},
            "_custom": [{"id": "abc123", "name": "Grafana", "ip": "192.168.1.11", "port": 3000}],
            "_layout": {"groups": [], "ungrouped": ["o:Jellyfin"]},
        }
        self.put_data(saved, cookie)
        st, d, _, _ = self.req("GET", "/api/data", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertEqual(d["saved"]["Jellyfin"]["ip"], "192.168.1.10")
        self.assertEqual(d["saved"]["_custom"][0]["name"], "Grafana")

        st, d, _, _ = self.req("GET", "/api/custom", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertEqual(len(d["custom"]), 1)

    def test_export_import_roundtrip(self):
        cookie = self.complete_setup()
        original = {"Jellyfin": {"ip": "10.0.0.5", "port": 8096}}
        self.put_data(original, cookie)
        st, d, _, _ = self.req("POST", "/api/export", {"password": "correct-horse"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertEqual(d["account"], "admin")
        payload = d["data"]

        self.put_data({"Jellyfin": {"ip": "10.0.0.99", "port": 8096}}, cookie)
        st, d, _, _ = self.req("POST", "/api/import",
                               {"file": {"data": payload}, "password": "correct-horse"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        st, d, _, _ = self.req("GET", "/api/data", cookie=cookie)
        self.assertEqual(d["saved"]["Jellyfin"]["ip"], "10.0.0.5")

        st, d, _, _ = self.req("POST", "/api/import",
                               {"file": {"data": payload}, "password": "wrong"}, cookie=cookie)
        self.assertEqual(st, 400)

    def test_check_endpoint(self):
        cookie = self.complete_setup()
        st, d, _, _ = self.req("POST", "/api/check", {"items": [
            {"ip": "127.0.0.1", "port": self.port, "scheme": "http"},
            {"ip": "not a host!", "port": 80},
            {"ip": "127.0.0.1", "port": 1},
        ]}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertTrue(d["results"][0]["online"])
        self.assertFalse(d["results"][1]["online"])
        self.assertFalse(d["results"][2]["online"])

    # ── push monitors ───────────────────────────────────────────────────────
    def test_push_monitor_reports_status_and_latency(self):
        cookie = self.complete_setup()
        self.put_data({"_custom": [{"id": "p1", "name": "Cron", "check": {"type": "push", "interval": 60}}]}, cookie)

        st, d, _, _ = self.req("POST", "/api/service-secrets", {"id": "c:p1", "push": "new"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        path = d["pushPath"]
        self.assertTrue(path.startswith("/api/push/"))

        st, d, _, _ = self.req("GET", path + "?ping=42.5")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])

        st, d, _, _ = self.req("GET", "/api/service-secrets", cookie=cookie)
        self.assertGreater(d["services"]["c:p1"]["lastPush"], 0)

        st, d, _, _ = self.req("POST", "/api/check", {"items": [{"id": "c:p1"}]}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertTrue(d["results"][0]["online"])
        self.assertEqual(d["results"][0]["ms"], 42.5)

        self.assertEqual(self.req("GET", "/api/push/nope-not-a-real-token")[0], 404)

        st, d, _, _ = self.req("POST", "/api/service-secrets", {"id": "c:p1", "push": "revoke"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertIsNone(d["pushPath"])
        self.assertEqual(self.req("GET", path)[0], 404)

    # ── rate limiting / proxy awareness ─────────────────────────────────────
    def test_login_rate_limit(self):
        for _ in range(server.SYS["lockout_max"] - 1):
            st, _, _, _ = self.req("POST", "/api/login", {"user": "admin", "pw": "wrong"})
            self.assertEqual(st, 401)
        st, d, _, _ = self.req("POST", "/api/login", {"user": "admin", "pw": "wrong"})
        self.assertEqual(st, 429)
        self.assertIn("Too many", d["error"])
        self.assertTrue(d["locked"])
        # locked even with the right password, until the lockout expires
        st, d, _, _ = self.req("POST", "/api/login", {"user": "admin", "pw": "forgepanel"})
        self.assertEqual(st, 429)
        self.assertGreater(d["lockedUntil"], time.time() + 14 * 60)

    def test_trusted_proxy_uses_forwarded_for_and_proto(self):
        server.TRUSTED_PROXIES = [ipaddress.ip_network("127.0.0.1/32")]
        st, _, _, _ = self.req("POST", "/api/login", {"user": "admin", "pw": "wrong"},
                               {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"})
        self.assertEqual(st, 401)
        self.assertIn("admin|203.0.113.7", server.STATE.lockouts)
        self.assertNotIn("admin|127.0.0.1", server.STATE.lockouts)

        st, d, sc, _ = self.req("POST", "/api/login", {"user": "admin", "pw": "forgepanel"},
                                {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"})
        self.assertEqual(st, 200, d)
        self.assertIn("Secure", sc)

    # ── public status page ──────────────────────────────────────────────────
    def test_public_status_is_off_by_default_then_togglable(self):
        cookie = self.complete_setup()
        self.assertEqual(self.req("GET", "/api/public/status")[0], 404)
        st, d, _, _ = self.req("PUT", "/api/public-status/config",
                               {"enabled": True, "title": "Lab status"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        st, d, _, _ = self.req("GET", "/api/public/status")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["title"], "Lab status")
        self.assertEqual(d["services"], [])
        self.req("PUT", "/api/public-status/config", {"enabled": False}, cookie=cookie)
        self.assertEqual(self.req("GET", "/api/public/status")[0], 404)

    # ── units ───────────────────────────────────────────────────────────────
    def test_schedule_normalisation(self):
        n = server.normalize_schedule
        self.assertEqual(n({"mode": "hourly"})["interval"], 60)
        self.assertEqual(n({"mode": "daily"})["interval"], 1440)
        self.assertEqual(n({"mode": "custom", "minutes": 1})["minutes"], 5)
        self.assertEqual(n({"mode": "custom", "minutes": 99999})["minutes"], 7 * 1440)
        self.assertEqual(n({"mode": "bogus"})["mode"], "off")
        self.assertEqual(n({"mode": "off"})["interval"], 0)

    def test_service_keys_and_check_types(self):
        saved = {"Jellyfin": {"ip": "10.0.0.2", "port": 8096},
                 "_custom": [{"id": "x", "ip": "10.0.0.3", "port": 80}]}
        self.assertEqual(server.known_keys(saved), {"10.0.0.2:8096", "10.0.0.3:80"})
        self.assertEqual(fp_ext.ctype({"check": {"type": "tcp"}}), "tcp")
        self.assertEqual(fp_ext.ctype({"check": {"type": "bogus"}}), "http")
        self.assertNotEqual(fp_ext.series_key(saved["Jellyfin"], "o:Jellyfin"),
                            fp_ext.series_key({"ip": "10.0.0.2", "port": 8096, "check": {"type": "keyword", "keyword": "x"}},
                                              "o:Jellyfin"))

    @unittest.skipUnless(server.HAVE_CRYPTO, "cryptography is not installed")
    def test_data_is_encrypted_at_rest(self):
        st = server.STATE.store
        dek = st.unlock("forgepanel")
        self.assertIsNotNone(dek)
        st.save_saved(dek, {"Jellyfin": {"ip": "10.9.8.7", "port": 8096}})
        rows = st.q("SELECT enc, blob FROM settings WHERE name='dashboard'")
        self.assertEqual(rows[0][0], 1)
        self.assertNotIn("10.9.8.7", rows[0][1])
        self.assertEqual(st.load_saved(dek)["Jellyfin"]["ip"], "10.9.8.7")

    # ── Settings → Security / Monitoring / Backup ───────────────────────────
    def test_system_settings_require_session(self):
        self.assertEqual(self.req("GET", "/api/system-settings")[0], 401)
        self.assertEqual(self.req("PUT", "/api/system-settings", {"idle_timeout": 300})[0], 401)
        self.assertEqual(self.req("POST", "/api/system/restart", {})[0], 401)
        self.assertEqual(self.req("POST", "/api/backup", {"password": "x"})[0], 401)
        self.assertEqual(self.req("POST", "/api/restore", {"password": "x"})[0], 401)

    def test_system_settings_persist_and_validate(self):
        cookie = self.complete_setup()
        server.RUNTIME["protocol"] = "https"
        self.addCleanup(server.RUNTIME.clear)
        st, d, _, _ = self.req("GET", "/api/system-settings", cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertFalse(d["pendingRestart"])
        self.assertEqual(d["settings"]["idle_timeout"], 60)
        self.assertEqual(d["settings"]["protocol"], "https")
        for bad in ({"idle_timeout": 30}, {"idle_timeout": -1}, {"idle_timeout": "60"}, {"idle_timeout": 1.5},
                    {"protocol": "ftp"}, {"check_interval": 5}, {"check_timeout": 60}, {"lockout_max": 101},
                    {"lockout_minutes": 0}, {"bogus": 1}, {"check_interval": 10, "check_timeout": 10}):
            st, d, _, _ = self.req("PUT", "/api/system-settings", bad, cookie=cookie)
            self.assertEqual(st, 400, bad)
        st, d, _, _ = self.req("PUT", "/api/system-settings", {"idle_timeout": 900, "protocol": "http",
                               "check_interval": 30, "check_timeout": 5, "lockout_max": 0}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertEqual(server.SYS["idle_timeout"], 900)
        self.assertTrue(d["pendingRestart"])
        self.assertTrue(d["newUrl"].startswith("http://"))
        # status / heartbeat report the new idle timeout immediately
        self.assertEqual(self.req("GET", "/api/status", cookie=cookie)[1]["idleTimeout"], 900)
        # survives a restart: reopen the database
        server.STATE.store.close()
        server.STATE = server.State(server.Store(self.db))
        server.SYS.clear(); server.SYS.update(server.SYS_DEFAULTS)
        server.load_sys()
        self.assertEqual(server.SYS["idle_timeout"], 900)
        self.assertEqual(server.SYS["protocol"], "http")
        self.assertEqual(server.SYS["check_interval"], 30)
        self.assertEqual(server.SYS["lockout_max"], 0)

    def test_forced_protocol_wins_over_saved(self):
        server.STATE.store.set_meta(server.SYS_KEY, json.dumps({"protocol": "http"}))
        old = dict(server.SYS_DEFAULTS)
        try:
            server.SYS_DEFAULTS["protocol"] = "https"
            server.SYS_FORCED.add("protocol")
            self.assertEqual(server.load_sys()["protocol"], "https")
            server.SYS_FORCED.clear()
            self.assertEqual(server.load_sys()["protocol"], "http")
        finally:
            server.SYS_DEFAULTS.update(old)
            server.SYS_FORCED.clear()

    def test_idle_timeout_setting_enforced(self):
        cookie = self.complete_setup()
        self.req("PUT", "/api/system-settings", {"idle_timeout": 0}, cookie=cookie)
        tok = cookie.split("=", 1)[1]
        server.STATE.sessions[tok]["seen"] -= 10_000
        self.assertEqual(self.req("GET", "/api/system-settings", cookie=cookie)[0], 200)
        server.STATE.sessions[tok]["seen"] -= 10_000
        self.req("PUT", "/api/system-settings", {"idle_timeout": 60}, cookie=cookie)  # touches the session
        server.STATE.sessions[tok]["seen"] -= 120
        self.assertEqual(self.req("GET", "/api/system-settings", cookie=cookie)[0], 401)

    def test_lockout_per_user_and_ip_configurable_and_off(self):
        server.SYS.update(lockout_max=3, lockout_minutes=2)
        for _ in range(2):
            self.assertEqual(self.req("POST", "/api/login", {"user": "admin", "pw": "bad"})[0], 401)
        st, d, _, _ = self.req("POST", "/api/login", {"user": "admin", "pw": "bad"})
        self.assertEqual(st, 429)
        self.assertLessEqual(d["retryIn"], 120)
        # another username from the same IP is not locked
        self.assertEqual(self.req("POST", "/api/login", {"user": "someone", "pw": "bad"})[0], 401)
        # expire the lockout
        server.STATE.lockouts["admin|127.0.0.1"]["until"] = time.time() - 1
        self.assertEqual(self.req("POST", "/api/login", {"user": "admin", "pw": "forgepanel"})[0], 200)
        # off: unlimited attempts
        server.SYS.update(lockout_max=0)
        for _ in range(8):
            self.assertEqual(self.req("POST", "/api/login", {"user": "admin", "pw": "bad"})[0], 401)

    def test_backup_restore_round_trip(self):
        cookie = self.complete_setup()
        self.assertEqual(self.req("PUT", "/api/data", {"saved": {"svc1": {"ip": "10.0.0.5", "port": 80}}},
                                  cookie=cookie)[0], 200)
        self.req("PUT", "/api/system-settings", {"idle_timeout": 600}, cookie=cookie)
        self.assertEqual(self.req("POST", "/api/backup", {"password": "nope"}, cookie=cookie)[0], 401)
        st, d, _, _ = self.req("POST", "/api/backup", {"password": "correct-horse"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        b = d["backup"]
        self.assertEqual(b["format"], "forgepanel-backup")
        self.assertNotIn("uptime", b["tables"])
        self.assertNotIn("PRIVATE KEY", json.dumps(b))
        # change things, then restore
        self.req("PUT", "/api/data", {"saved": {"other": {"ip": "10.9.9.9", "port": 1}}}, cookie=cookie)
        self.req("PUT", "/api/system-settings", {"idle_timeout": 0}, cookie=cookie)
        # validation and confirmation
        for bad in ({"format": "x"}, dict(b, version=99), dict(b, tables={"evil": {}}),
                    dict(b, tables=dict(b["tables"], account={"columns": ["user"], "rows": []}))):
            st, _, _, _ = self.req("POST", "/api/restore", {"password": "correct-horse", "confirm": True,
                                                            "backup": bad}, cookie=cookie)
            self.assertEqual(st, 400)
        st, _, _, _ = self.req("POST", "/api/restore", {"password": "correct-horse", "backup": b}, cookie=cookie)
        self.assertEqual(st, 400)
        st, d, _, _ = self.req("POST", "/api/restore", {"password": "correct-horse", "confirm": True,
                                                        "backup": b}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "backups", d["preRestore"])))
        # sessions were dropped; sign in again and see the backed-up data
        self.assertEqual(self.req("GET", "/api/data", cookie=cookie)[0], 401)
        cookie = self.login("correct-horse")
        st, d, _, _ = self.req("GET", "/api/data", cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertIn("svc1", json.dumps(d))
        self.assertNotIn("10.9.9.9", json.dumps(d))
        self.assertEqual(server.SYS["idle_timeout"], 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
