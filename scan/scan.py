#!/usr/bin/env python3
"""ForgePanel network service scanner.

Pure Python 3 standard library for scanning. Encryption uses the optional
"cryptography" package (AES-256-GCM, PBKDF2-SHA256, salt 'homelab-v2',
100000 iterations), the same parameters as js/auth.js, so the dashboard can
decrypt results.enc with the login password. Without "cryptography" it writes
a plain results.json and warns. The password comes from the
FORGEPANEL_PASSWORD environment variable or an interactive getpass prompt.
"""
import argparse
import concurrent.futures as cf
import configparser
import datetime
import getpass
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SALT = b"homelab-v2"
ITERATIONS = 100000
FORMAT = "forgepanel-scan"


def load_conf(path):
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.optionxform = str  # keep case
    if not cp.read(path):
        sys.exit(f"Config not found: {path}")
    s = cp["scan"] if cp.has_section("scan") else {}
    csv = lambda v: [x.strip() for x in v.split(",") if x.strip()]
    conf = {
        # FORGEPANEL_SCAN_SUBNET (env) beats the file, so Docker/LXC installs can set it without editing scan.conf
        "subnet": os.environ.get("FORGEPANEL_SCAN_SUBNET", "").strip() or s.get("subnet", "auto"),
        "max_hosts": int(s.get("max_hosts", 1024)),
        "timeout": float(s.get("timeout", 0.6)),
        "threads": max(1, int(s.get("threads", 128))),
        "http_probe": str(s.get("http_probe", "yes")).lower() in ("1", "yes", "true", "on"),
        "output": s.get("output", "results.enc"),
        "plain_output": s.get("plain_output", "results.json"),
        "ports": {},
        "https_ports": set(),
        "fingerprints": {},
        "official": [],
    }
    if cp.has_section("ports"):
        for k, v in cp["ports"].items():
            conf["ports"][int(k)] = csv(v)
    if cp.has_section("https_ports"):
        conf["https_ports"] = {int(p) for p in csv(cp["https_ports"].get("ports", ""))}
    if cp.has_section("fingerprints"):
        conf["fingerprints"] = {k.lower(): v.strip() for k, v in cp["fingerprints"].items()}
    if cp.has_section("favicon_hashes"):
        for k, v in cp["favicon_hashes"].items():
            FAVICON_HASHES[k.strip().lower()] = v.strip()
    if cp.has_section("official"):
        conf["official"] = csv(cp["official"].get("names", ""))
    if not conf["ports"]:
        sys.exit("No ports configured in [ports]")
    return conf


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # UDP connect sends no packets; just picks the route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def in_docker_bridge(ip):
    """True when this process sits on a Docker bridge (container IP in 172.16.0.0/12 + /.dockerenv)."""
    try:
        bridge = ipaddress.ip_address(ip) in ipaddress.ip_network("172.16.0.0/12")
    except ValueError:
        return False
    return bridge and os.path.exists("/.dockerenv")


def resolve_hosts(spec, max_hosts):
    if spec.strip().lower() == "auto":
        ip = local_ip()
        if in_docker_bridge(ip):
            # Inside a Docker bridge network the "local IP" is 172.x: scanning its /24 finds only
            # this container, so the scan always came back empty. Fail loudly instead.
            sys.exit(f"ForgePanel runs in a Docker bridge network ({ip}), so subnet=auto would only scan "
                     f"the container's private network. Run the container with network_mode: host, or set "
                     f"FORGEPANEL_SCAN_SUBNET (e.g. 192.168.1.0/24) or subnet in scan/scan.conf.")
        spec = str(ipaddress.ip_network(f"{ip}/24", strict=False))
        print(f"Detected local IP {ip}, scanning {spec}")
    hosts = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        net = ipaddress.ip_network(part, strict=False)
        hs = list(net.hosts()) or [net.network_address]
        hosts.extend(str(h) for h in hs)
        if len(hosts) > max_hosts:
            sys.exit(f"{len(hosts)}+ hosts exceeds max_hosts={max_hosts}; narrow the subnet or raise max_hosts")
    return spec, list(dict.fromkeys(hosts))


def port_open(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Resp:
    """One HTTP response: status, lower-cased headers (multi-valued), body text, raw body bytes."""
    def __init__(self, status, headers=None, body="", raw=None):
        self.status = int(status)
        self.headers = {}
        for k, v in (headers.items() if isinstance(headers, dict) else (headers or [])):
            self.headers.setdefault(k.lower(), []).append(str(v))
        self.body = body or ""
        self.raw = raw if raw is not None else self.body.encode("utf-8", "replace")

    def header(self, name):
        return " ".join(self.headers.get(name.lower(), []))

    def cookie_names(self):
        return {c.split("=", 1)[0].strip().lower() for c in self.headers.get("set-cookie", []) if "=" in c}

    def title(self):
        m = re.search(r"<title[^>]*>(.*?)</title>", self.body, re.I | re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def fetch(host, port, scheme, path, timeout, _redirects=3):
    """GET scheme://host:port/path. Follows same-host redirects. Returns Resp or None (not HTTP / error)."""
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # probing only, nothing sensitive is sent
            conn = http.client.HTTPSConnection(host, port, timeout=timeout * 3, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout * 3)
        conn.request("GET", path, headers={"User-Agent": "ForgePanel-Scan/2", "Accept": "*/*"})
        r = conn.getresponse()
        raw = r.read(65536)
        resp = Resp(r.status, r.getheaders(), raw.decode("utf-8", "replace"), raw)
        conn.close()
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
        return None
    loc = resp.header("location")
    if resp.status in (301, 302, 303, 307, 308) and loc and _redirects > 0:
        m = re.match(r"^(?:(https?)://([^/:]+)(?::(\d+))?)?(/[^\s]*)?$", loc.strip())
        if m and (not m.group(2) or m.group(2) == host) and (not m.group(3) or int(m.group(3)) == port):
            nxt = fetch(host, port, m.group(1) or scheme, m.group(4) or "/", timeout, _redirects - 1)
            if nxt is not None:
                return nxt
    return resp


def _j(r):
    d = r.json()
    return d if isinstance(d, dict) else {}


# Service signatures. Endpoint checks validate the response CONTENT (SPAs answer 200 to any path).
# Strong evidence: title, specific header/cookie, validated endpoint, favicon hash. Port = hint only.
SIGNATURES = [
    {"name": "Immich", "icon": "immich.png", "title": [r"\bimmich\b"],
     "endpoints": [("/api/server/ping", lambda r: _j(r).get("res") == "pong"),
                   ("/api/server-info/ping", lambda r: _j(r).get("res") == "pong")]},
    {"name": "Jellyfin", "icon": "jellyfin.png", "title": [r"\bjellyfin\b"],
     "endpoints": [("/System/Info/Public", lambda r: "jellyfin" in str(_j(r).get("ProductName", "")).lower()
                    or {"ServerName", "Version", "Id"} <= set(_j(r)))]},
    {"name": "Nextcloud", "icon": "nextcloud.png", "title": [r"\bnextcloud\b"],
     "cookies": ["nc_sameSiteCookielax", "nc_samesitecookiestrict", "oc_sessionpassphrase"],
     "headers": {"x-nextcloud-": None},
     "endpoints": [("/status.php", lambda r: "nextcloud" in str(_j(r).get("productname", "")).lower()
                    or ("installed" in _j(r) and "versionstring" in _j(r)))]},
    {"name": "Navidrome", "icon": "navidrome.png", "title": [r"\bnavidrome\b"],
     "endpoints": [("/app/", lambda r: r.status == 200 and "navidrome" in r.body.lower())]},
    {"name": "AdGuard", "icon": "adguard.png", "title": [r"adguard"], "headers": {"server": r"adguard"},
     "endpoints": [("/control/status", lambda r: "dns_addresses" in _j(r) or "protection_enabled" in _j(r)),
                   ("/login.html", lambda r: r.status == 200 and "adguard" in r.body.lower())]},
    {"name": "Vaultwarden", "icon": "vaultwarden.png", "title": [r"vaultwarden", r"bitwarden"],
     "endpoints": [("/alive", lambda r: r.status == 200 and re.fullmatch(r'\s*"?\d{4}-\d\d-\d\dT[^<]*"?\s*', r.body) is not None),
                   ("/api/alive", lambda r: r.status == 200 and re.fullmatch(r'\s*"?\d{4}-\d\d-\d\dT[^<]*"?\s*', r.body) is not None)]},
    {"name": "TriliumNext", "icon": "trilium.png", "title": [r"\btrilium\b"], "cookies": ["trilium.sid"],
     "endpoints": [("/login", lambda r: r.status == 200 and "trilium" in r.body.lower())]},
    {"name": "FileBrowser", "icon": "filebrowser.png", "title": [r"file ?browser"],
     "endpoints": [("/login", lambda r: r.status == 200 and re.search(r"file ?browser", r.body, re.I) is not None)]},
    {"name": "RomM", "icon": "romm.png", "title": [r"\bromm\b"], "cookies": ["romm_session"],
     "endpoints": [("/api/heartbeat", lambda r: "SYSTEM" in _j(r) or "romm" in r.body.lower()[:4000] and r.json() is not None)]},
    {"name": "NPM", "icon": "npm.png", "title": [r"nginx proxy manager"],
     "body": [r"nginx proxy manager"],
     "endpoints": [("/api/", lambda r: str(_j(r).get("status", "")).upper() == "OK" and "setup" in _j(r))]},
    {"name": "Proxmox", "icon": "proxmox.png", "title": [r"proxmox virtual environment", r"\bproxmox\b"],
     "headers": {"server": r"pve-api-daemon"}, "cookies": ["pveauthcookie"]},
    {"name": "Portainer", "icon": None, "title": [r"\bportainer\b"],
     "endpoints": [("/api/status", lambda r: "InstanceID" in _j(r) or "instanceid" in {k.lower() for k in _j(r)})]},
    {"name": "TrueNAS", "icon": "truenas.png", "title": [r"\btruenas\b", r"\bfreenas\b"],
     "body": [r"\btruenas\b"]},
    {"name": "Pi-hole", "icon": None, "title": [r"pi-hole"], "headers": {"x-pi-hole": None},
     "endpoints": [("/admin/", lambda r: r.status == 200 and "pi-hole" in r.body.lower())]},
    {"name": "ForgePanel", "icon": None, "title": [r"\bforgepanel\b"],
     "endpoints": [("/api/health", lambda r: _j(r).get("app") == "forgepanel")]},
    {"name": "Grafana", "icon": None, "title": [r"\bgrafana\b"],
     "endpoints": [("/api/health", lambda r: "database" in _j(r) and "version" in _j(r))]},
    {"name": "Uptime Kuma", "icon": None, "title": [r"uptime kuma"]},
    {"name": "Home Assistant", "icon": None, "title": [r"home assistant"],
     "endpoints": [("/manifest.json", lambda r: "home assistant" in str(_j(r).get("name", "")).lower())]},
    {"name": "Plex", "icon": None, "title": [r"\bplex\b"], "headers": {"x-plex-protocol": None},
     "endpoints": [("/identity", lambda r: "MediaContainer" in r.body and "machineIdentifier" in r.body)]},
    {"name": "Pterodactyl", "icon": None, "title": [r"pterodactyl"], "cookies": ["pterodactyl_session"]},
]
SIG_BY_NAME = {s["name"]: s for s in SIGNATURES}
ALIASES = {"trilium": "TriliumNext", "adguard home": "AdGuard", "adguardhome": "AdGuard",
           "nginxproxymanager": "NPM", "nginx proxy manager": "NPM", "bitwarden": "Vaultwarden"}

# Optional favicon md5 -> service, extendable from scan.conf [favicon_hashes].
FAVICON_HASHES = {}


def canonical(name):
    n = (name or "").strip()
    if n in SIG_BY_NAME:
        return n
    low = n.lower()
    for s in SIGNATURES:
        if s["name"].lower() == low:
            return s["name"]
    return ALIASES.get(low, n)


def _passive(sig, root):
    """Evidence from the root response only: title, headers, cookie names, body markers."""
    ev = []
    title = root.title()
    if title and any(re.search(p, title, re.I) for p in sig.get("title", [])):
        ev.append("title")
    for h, pat in sig.get("headers", {}).items():
        if h.endswith("-"):
            if any(k.startswith(h) for k in root.headers):
                ev.append("header " + h + "*")
        elif h in root.headers and (pat is None or re.search(pat, root.header(h), re.I)):
            ev.append("header " + h)
    xpb = root.header("x-powered-by")
    if xpb and re.search(r"\b" + re.escape(sig["name"]) + r"\b", xpb, re.I):
        ev.append("x-powered-by")
    names = root.cookie_names()
    for c in sig.get("cookies", []):
        if c.lower() in names:
            ev.append("cookie " + c)
            break
    if root.status < 400 and any(re.search(p, root.body, re.I) for p in sig.get("body", [])) and "title" not in ev:
        ev.append("page content")
    return ev


UNKNOWN_NAME = "Unknown service"
HTTPS_ONLY_RE = re.compile(r"plain HTTP request was sent to HTTPS port|HTTP request to an HTTPS server|"
                           r"speaking plain HTTP to an SSL-enabled server", re.I)
NPM_DEFAULT_RE = re.compile(r"successfully started the Nginx Proxy Manager", re.I)


def is_https_only(r):
    return r is not None and r.status == 400 and HTTPS_ONLY_RE.search(r.body or "") is not None


def is_npm_default_site(r):
    """NPM's catch-all 'Default Site' page (ports 80/443 with no matching proxy host)."""
    return r is not None and r.status < 400 and NPM_DEFAULT_RE.search(r.body or "") is not None


def fingerprint(host, port, conf, get=None):
    """Identify ONE host:port from its own responses. Returns a dict with name, confidence, evidence.

    get(scheme, path) -> Resp|None is injectable for tests; the default does real HTTP(S).
    Nothing is inferred from other ports on the same host.
    """
    timeout = conf.get("timeout", 0.6)
    get = get or (lambda sch, path: fetch(host, port, sch, path, timeout))
    tls_first = port in conf.get("https_ports", set()) or port % 1000 == 443
    scheme, root = None, None
    for sch in (["https", "http"] if tls_first else ["http", "https"]):
        r = get(sch, "/")
        if r is None:
            continue
        if sch == "http" and is_https_only(r):
            # nginx/openresty "plain HTTP request was sent to HTTPS port": retry over TLS, never label this page
            r2 = get("https", "/")
            if r2 is not None:
                scheme, root = "https", r2
                break
        scheme, root = sch, r
        break
    out = {"name": UNKNOWN_NAME, "where": f"{host}:{port}", "match": "custom", "icon": None,
           "confidence": "low", "evidence": [], "identified_by": "unknown",
           "scheme": scheme or ("https" if tls_first else "http"), "http": root is not None,
           "title": root.title() if root else "", "server": (root.header("server")[:120] if root else ""),
           "status": root.status if root else None}
    if root is None:
        out["evidence"] = ["open TCP port, no HTTP response"]
        return out

    if is_https_only(root):
        hn = conf.get("ports", {}).get(port, [])
        out["evidence"] = ["HTTP 400 plain-HTTP-to-HTTPS, HTTPS retry failed (not used for naming)"] + \
            ([f"port hint: {canonical(hn[0])}"] if hn else [])
        return out
    if is_npm_default_site(root):
        # The proxy's own placeholder: says nothing about what else runs on this host.
        out.update(name="NPM Default Site", icon="npm.png", confidence="low",
                   evidence=["NPM default page"], identified_by="NPM default page", match="custom")
        return out

    hints = [canonical(n) for n in conf.get("ports", {}).get(port, [])]
    order = sorted(SIGNATURES, key=lambda s: hints.index(s["name"]) if s["name"] in hints else len(hints))

    # 1) passive evidence from the root page
    scored = []
    for sig in order:
        ev = _passive(sig, root)
        if ev:
            scored.append((sig, ev))
    # Title outranks headers/cookies/body: if any candidate matched by title, keep only those.
    if any("title" in ev for _, ev in scored):
        scored = [(s, e) for s, e in scored if "title" in e]

    def confirm(sig):
        for path, ok in sig.get("endpoints", []):
            r = get(scheme, path)
            try:
                if r is not None and r.status < 400 and ok(r):
                    return path
            except Exception:  # a validator must never crash the scan
                continue
        return None

    best = None
    for sig, ev in scored:
        p = confirm(sig)
        if p:
            ev = ev + [p]
        if best is None or len(ev) > len(best[1]):
            best = (sig, ev)
        if p:
            break

    # 2) active endpoint checks when the page itself said nothing (port hints are tried first)
    if best is None:
        for sig in order:
            p = confirm(sig)
            if p:
                best = (sig, [p])
                break

    # 3) favicon hash
    if best is None and FAVICON_HASHES:
        r = get(scheme, "/favicon.ico")
        if r is not None and r.status == 200 and r.raw:
            name = FAVICON_HASHES.get(hashlib.md5(r.raw).hexdigest())
            if name and canonical(name) in SIG_BY_NAME:
                best = (SIG_BY_NAME[canonical(name)], ["favicon hash"])

    if best is None:
        why = f"HTTP {root.status}"
        if root.status in (400, 401, 403):
            why += " (auth/bad request, not used for naming)"
        out["evidence"] = [why] + ([f"port hint: {hints[0]}"] if hints else [])
        out["identified_by"] = "unknown"
        return out

    sig, ev = best
    endpoint = any(e.startswith("/") for e in ev)
    if len(ev) >= 2 or endpoint:
        conf_level = "high"
    elif ev == ["page content"]:
        conf_level = "low"
    else:
        conf_level = "medium"
    official = {n.lower(): n for n in conf.get("official", [])}
    out.update(name=sig["name"], icon=sig["icon"], confidence=conf_level, evidence=ev,
               identified_by=" + ".join(ev), match=official.get(sig["name"].lower(), "custom"))
    if sig["name"] in hints:
        out["evidence"] = ev + ["port hint"]
    return out


def http_probe(host, port, use_https, timeout):
    """Back-compat: return (scheme, title, server) or (None, '', '') when not HTTP."""
    for scheme in (["https", "http"] if use_https else ["http", "https"]):
        r = fetch(host, port, scheme, "/", timeout)
        if r is not None:
            return scheme, r.title(), r.header("server")[:120]
    return None, "", ""


def identify(port, title, server, conf):
    """Back-compat helper: title/Server text only, never a port guess."""
    text = f"{title} {server}".lower()
    for key, name in conf["fingerprints"].items():
        if key and key in text:
            return name, "fingerprint"
    return f"Service :{port}", "unknown"


def scan(conf, hosts):
    jobs = [(h, p) for h in hosts for p in sorted(conf["ports"])]
    print(f"Scanning {len(hosts)} host(s) x {len(conf['ports'])} port(s) = {len(jobs)} checks...")
    open_ports = []
    with cf.ThreadPoolExecutor(conf["threads"]) as ex:
        futs = {ex.submit(port_open, h, p, conf["timeout"]): (h, p) for h, p in jobs}
        for f in cf.as_completed(futs):
            if f.result():
                open_ports.append(futs[f])

    def describe(hp):
        h, p = hp
        if not conf["http_probe"]:
            hints = conf["ports"].get(p, [])
            return {"ip": h, "port": p, "name": UNKNOWN_NAME, "where": f"{h}:{p}", "match": "custom", "icon": None,
                    "confidence": "low", "evidence": ["open TCP port"] + ([f"port hint: {hints[0]}"] if hints else []),
                    "identified_by": "unknown", "scheme": "https" if p in conf["https_ports"] else "http",
                    "http": False, "title": "", "server": "", "status": None}
        return dict(fingerprint(h, p, conf), ip=h, port=p)

    with cf.ThreadPoolExecutor(min(32, conf["threads"])) as ex:
        found = list(ex.map(describe, open_ports))
    found.sort(key=lambda s: (ipaddress.ip_address(s["ip"]), s["port"]))
    return found


def get_password():
    pw = os.environ.get("FORGEPANEL_PASSWORD")
    if pw:
        return pw
    if not sys.stdin.isatty():
        sys.exit("No TTY for the password prompt; set FORGEPANEL_PASSWORD instead.")
    pw = getpass.getpass("Dashboard password (used to encrypt results): ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    if not pw:
        sys.exit("Empty password.")
    return pw


def encrypt_payload(obj, pw):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=SALT, iterations=ITERATIONS).derive(pw.encode("utf-8"))
    iv = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj).encode("utf-8"), None)  # ciphertext||tag, like WebCrypto
    return {"format": FORMAT, "version": 1, "iv": list(iv), "ct": list(ct)}


def have_crypto():
    try:
        import cryptography.hazmat.primitives.ciphers.aead  # noqa: F401
        return True
    except ImportError:
        return False


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser(description="Scan the local network for self-hosted services for ForgePanel.")
    ap.add_argument("--conf", default=os.path.join(HERE, "scan.conf"), help="config file (default scan/scan.conf)")
    ap.add_argument("--subnet", help="override subnet from the config (CIDR, host, or comma list)")
    ap.add_argument("--plain", action="store_true", help="write unencrypted results.json instead (not recommended)")
    args = ap.parse_args()

    conf = load_conf(args.conf)
    spec, hosts = resolve_hosts(args.subnet or conf["subnet"], conf["max_hosts"])
    started = datetime.datetime.now(datetime.timezone.utc)
    found = scan(conf, hosts)
    result = {"format": FORMAT, "version": 1, "generated": started.isoformat(timespec="seconds"),
              "subnet": spec, "hosts_scanned": len(hosts), "services": found}

    for s in found:
        tag = s["match"] if s["match"] != "custom" else "custom"
        extra = s["title"] or s["server"]
        print(f"  {s['ip']}:{s['port']:<5} {s['name']:<30} [{tag}, {s.get('confidence')}] {s.get('identified_by')} {extra}")
    print(f"Found {len(found)} open service port(s).")

    base = os.path.dirname(os.path.abspath(args.conf))
    if not args.plain and have_crypto():
        out = os.path.join(base, conf["output"])
        write_private(out, json.dumps(encrypt_payload(result, get_password())))
        print(f"Encrypted results written to {out}")
    else:
        if not args.plain:
            print("WARNING: Python 'cryptography' module not found, so results are NOT encrypted.\n"
                  "         Install it (e.g. apt install python3-cryptography) to write results.enc.", file=sys.stderr)
        out = os.path.join(base, conf["plain_output"])
        payload = {"format": FORMAT, "version": 1, "plain": True,
                   "warning": "Unencrypted scan results. Do not share or commit this file.", "data": result}
        write_private(out, json.dumps(payload, indent=2))
        print(f"PLAIN (unencrypted) results written to {out}")


if __name__ == "__main__":
    main()
