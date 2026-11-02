"""Unit tests for scan/scan.py service fingerprinting, using mocked HTTP responses.

Run from the repository root:  python3 -m unittest discover -s tests -v
No network access: every host:port is a dict of (scheme, path) -> response.
"""
import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_spec = importlib.util.spec_from_file_location("fp_scan_test", os.path.join(ROOT, "scan", "scan.py"))
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)
CONF = sc.load_conf(os.path.join(ROOT, "scan", "scan.conf"))
R = sc.Resp


def page(title, body="", status=200, headers=None):
    return R(status, headers or {}, f"<html><head><title>{title}</title></head><body>{body}</body></html>")


def js(obj, status=200):
    return R(status, {"Content-Type": "application/json"}, json.dumps(obj))


SPA = page("App", "<div id=app></div>")  # catch-all SPA: 200 for every path

# Jeffrey's layout (plus the cases that were mislabeled before)
NET = {
    ("10.0.0.15", 2283): {("http", "/"): page("Immich"), ("http", "/api/server/ping"): js({"res": "pong"})},
    ("10.0.0.15", 8080): {("http", "/"): page("Login – Nextcloud", headers={"Set-Cookie": "oc_sessionPassphrase=x; path=/"}),
                          ("http", "/status.php"): js({"installed": True, "versionstring": "29.0.0", "productname": "Nextcloud"})},
    ("10.0.0.15", 8081): {("http", "/"): page("RomM"), ("http", "/api/heartbeat"): js({"SYSTEM": {"VERSION": "3.5"}})},
    ("10.0.0.15", 81): {("http", "/"): page("Nginx Proxy Manager"), ("http", "/api/"): js({"status": "OK", "setup": True, "version": {}})},
    ("10.0.0.15", 80): {("http", "/"): page("Default Site", "Congratulations! You've successfully started the Nginx Proxy Manager.", headers={"Server": "openresty"})},
    ("10.0.0.15", 443): {("https", "/"): page("Default Site", "Congratulations! You've successfully started the Nginx Proxy Manager.", headers={"Server": "openresty"})},
    ("10.0.0.18", 8096): {("http", "/"): page("Jellyfin", headers={"Server": "Kestrel"}),
                          ("http", "/System/Info/Public"): js({"ServerName": "s", "Version": "10.9", "ProductName": "Jellyfin Server", "Id": "1"})},
    ("10.0.0.18", 4533): {("http", "/"): page("Navidrome"), ("http", "/app/"): page("Navidrome", "navidrome")},
    ("10.0.0.18", 8082): {("http", "/"): page("File Browser"), ("http", "/login"): page("File Browser")},
    ("10.0.0.18", 3000): {("http", "/"): page("Login", "AdGuard Home"), ("http", "/login.html"): page("Login", "AdGuard Home"),
                          ("http", "/control/status"): R(401, {}, "")},
    ("10.0.0.19", 8000): {("http", "/"): page("Vaultwarden Web"), ("http", "/alive"): R(200, {}, '"2026-11-02T12:00:00.000Z"')},
    ("10.0.0.19", 8083): {("http", "/"): page("Login", headers={"Set-Cookie": "trilium.sid=abc; Path=/"}),
                          ("http", "/login"): page("Login", "Trilium Notes")},
    ("10.0.0.23", 8084): {("http", "/"): page("ForgePanel"), ("http", "/api/health"): js({"ok": True, "app": "forgepanel"})},
    ("10.0.0.210", 8006): {("https", "/"): page("pve - Proxmox Virtual Environment", headers={"Server": "pve-api-daemon/3.0"})},
    ("10.0.0.17", 80): {("http", "/"): R(401, {"WWW-Authenticate": "Basic"}, "Unauthorized")},
    ("10.0.0.21", 9000): {("http", "/"): page("Portainer"), ("http", "/api/status"): js({"Version": "2.21", "InstanceID": "x"})},
    ("10.0.0.22", 9000): {("http", "/"): R(400, {}, "Client sent an HTTP request to an HTTPS server.")},
    ("10.0.0.30", 8080): "spa",
    ("10.0.0.31", 8443): {},  # open TCP, not HTTP
}


def run(host, port):
    routes = NET[(host, port)]
    calls = []

    def get(scheme, path):
        calls.append((scheme, path))
        if routes == "spa":
            return SPA if scheme == "http" else None
        if not any(k[0] == scheme for k in routes):
            return None  # this scheme isn't spoken on the port
        return routes.get((scheme, path), R(404, {}, "Not Found"))
    out = sc.fingerprint(host, port, CONF, get=get)
    out["_calls"] = calls
    return out


class FingerprintTests(unittest.TestCase):
    def check(self, host, port, name, icon, conf="high", ev=()):
        r = run(host, port)
        self.assertEqual(r["name"], name, r)
        self.assertEqual(r["icon"], icon, r)
        self.assertEqual(r["confidence"], conf, r)
        for e in ev:
            self.assertIn(e, r["evidence"], r)
        return r

    def test_docker_stack_each_port_identified_on_its_own(self):
        self.check("10.0.0.15", 2283, "Immich", "immich.png", ev=["title", "/api/server/ping"])
        self.check("10.0.0.15", 8080, "Nextcloud", "nextcloud.png", ev=["title", "/status.php"])
        self.check("10.0.0.15", 8081, "RomM", "romm.png", ev=["title", "/api/heartbeat"])
        self.check("10.0.0.15", 81, "NPM", "npm.png", ev=["title", "/api/"])

    def test_npm_80_443_are_not_nextcloud(self):
        for port in (80, 443):
            r = run("10.0.0.15", port)
            self.assertNotEqual(r["name"], "Nextcloud")
            self.assertEqual(r["name"], "NPM")
            self.assertEqual(r["confidence"], "low")  # proxy default page only
        self.assertEqual(run("10.0.0.15", 443)["scheme"], "https")

    def test_source_stack(self):
        self.check("10.0.0.18", 8096, "Jellyfin", "jellyfin.png", ev=["/System/Info/Public"])
        self.check("10.0.0.18", 4533, "Navidrome", "navidrome.png", ev=["title", "/app/"])
        self.check("10.0.0.18", 8082, "FileBrowser", "filebrowser.png", ev=["title"])
        self.check("10.0.0.18", 3000, "AdGuard", "adguard.png", ev=["/login.html"])

    def test_light_stack_and_forgepanel(self):
        self.check("10.0.0.19", 8000, "Vaultwarden", "vaultwarden.png", ev=["title", "/alive"])
        self.check("10.0.0.19", 8083, "TriliumNext", "trilium.png", ev=["cookie trilium.sid", "/login"])
        self.check("10.0.0.23", 8084, "ForgePanel", None, ev=["title", "/api/health"])

    def test_proxmox_uses_https_first(self):
        r = self.check("10.0.0.210", 8006, "Proxmox", "proxmox.png", ev=["title", "header server"])
        self.assertEqual(r["scheme"], "https")
        self.assertEqual(r["_calls"][0], ("https", "/"))
        self.assertEqual(r["match"], "Proxmox")

    def test_bare_401_and_400_are_unknown(self):
        r = run("10.0.0.17", 80)
        self.assertEqual(r["name"], "Unknown service (10.0.0.17:80)")
        self.assertEqual(r["confidence"], "low")
        self.assertIsNone(r["icon"])
        self.assertTrue(any("401" in e for e in r["evidence"]))
        r = run("10.0.0.22", 9000)
        self.assertEqual(r["name"], "Unknown service (10.0.0.22:9000)")
        self.assertIn("port hint: Portainer", r["evidence"])

    def test_portainer_gets_no_npm_icon(self):
        r = self.check("10.0.0.21", 9000, "Portainer", None, ev=["title", "/api/status"])
        self.assertEqual(r["match"], "custom")

    def test_catch_all_spa_is_not_guessed_from_port(self):
        r = run("10.0.0.30", 8080)  # 8080 hints Nextcloud/RomM/... but nothing validates
        self.assertTrue(r["name"].startswith("Unknown service"))
        self.assertEqual(r["confidence"], "low")

    def test_non_http_port(self):
        r = run("10.0.0.31", 8443)
        self.assertFalse(r["http"])
        self.assertTrue(r["name"].startswith("Unknown service"))

    def test_official_match_and_identified_by(self):
        r = run("10.0.0.15", 2283)
        self.assertEqual(r["match"], "Immich")
        self.assertEqual(r["identified_by"], "title + /api/server/ping")

    def test_favicon_hash(self):
        import hashlib
        raw = b"\x00\x01fakeicon"
        sc.FAVICON_HASHES[hashlib.md5(raw).hexdigest()] = "Jellyfin"
        try:
            def get(scheme, path):
                if scheme != "http":
                    return None
                if path == "/favicon.ico":
                    return R(200, {}, "", raw)
                return SPA
            r = sc.fingerprint("10.0.0.40", 1234, CONF, get=get)
            self.assertEqual(r["name"], "Jellyfin")
            self.assertEqual(r["evidence"], ["favicon hash"])
        finally:
            sc.FAVICON_HASHES.clear()


class DedupeTests(unittest.TestCase):
    def test_known_keys_are_host_port(self):
        import server
        saved = {"Immich": {"ip": "10.0.0.15", "port": 2283},
                 "_custom": [{"ip": "10.0.0.15", "port": 8080, "name": "Nextcloud"}]}
        keys = server.known_keys(saved)
        self.assertIn("10.0.0.15:2283", keys)
        self.assertIn("10.0.0.15:8080", keys)
        self.assertNotIn("10.0.0.15:8081", keys)


if __name__ == "__main__":
    unittest.main()
