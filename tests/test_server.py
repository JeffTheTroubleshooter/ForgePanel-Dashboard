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
            "secQ": "first pet?", "secA": "rex"}, cookie=cookie)
        self.assertEqual(st, 200, d)
        return cookie

    def put_data(self, saved, cookie):
        st, d, _, _ = self.req("PUT", "/api/data", {"saved": saved}, cookie=cookie)
        self.assertEqual(st, 200, d)

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
        for _ in range(server.RATE_MAX):
            st, _, _, _ = self.req("POST", "/api/login", {"user": "admin", "pw": "wrong"})
            self.assertEqual(st, 401)
        st, d, _, _ = self.req("POST", "/api/login", {"user": "admin", "pw": "wrong"})
        self.assertEqual(st, 429)
        self.assertIn("Too many", d["error"])

    def test_trusted_proxy_uses_forwarded_for_and_proto(self):
        server.TRUSTED_PROXIES = [ipaddress.ip_network("127.0.0.1/32")]
        st, _, _, _ = self.req("POST", "/api/login", {"user": "admin", "pw": "wrong"},
                               {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"})
        self.assertEqual(st, 401)
        self.assertIn("203.0.113.7", server.STATE.fails)
        self.assertNotIn("127.0.0.1", server.STATE.fails)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
