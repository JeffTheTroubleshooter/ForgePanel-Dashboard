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
        "subnet": s.get("subnet", "auto"),
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


def resolve_hosts(spec, max_hosts):
    if spec.strip().lower() == "auto":
        ip = local_ip()
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


def http_probe(host, port, use_https, timeout):
    """Return (scheme, title, server) or (None, '', '') when not HTTP."""
    order = ["https", "http"] if use_https else ["http", "https"]
    for scheme in order:
        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # probing only, nothing sensitive is sent
                conn = http.client.HTTPSConnection(host, port, timeout=timeout * 3, context=ctx)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout * 3)
            conn.request("GET", "/", headers={"User-Agent": "ForgePanel-Scan/1"})
            r = conn.getresponse()
            server = r.getheader("Server", "") or ""
            body = r.read(16384).decode("utf-8", "replace")
            conn.close()
            m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""
            return scheme, title, server[:120]
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
            continue
    return None, "", ""


def identify(port, title, server, conf):
    text = f"{title} {server}".lower()
    for key, name in conf["fingerprints"].items():
        if key and key in text:
            return name, "fingerprint"
    cands = conf["ports"].get(port, [])
    if cands:
        return cands[0], "port"
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
        scheme, title, server = (None, "", "")
        if conf["http_probe"]:
            scheme, title, server = http_probe(h, p, p in conf["https_ports"], conf["timeout"])
        name, how = identify(p, title, server, conf)
        official = {n.lower(): n for n in conf["official"]}
        return {
            "ip": h, "port": p, "name": name,
            "match": official.get(name.lower(), "custom"),
            "identified_by": how,
            "scheme": scheme or ("https" if p in conf["https_ports"] else "http"),
            "http": scheme is not None,
            "title": title, "server": server,
        }

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
        print(f"  {s['ip']}:{s['port']:<5} {s['name']:<16} [{tag}] {extra}")
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
