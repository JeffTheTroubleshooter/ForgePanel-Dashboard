# ForgePanel Network Scan

`scan.py` finds self-hosted services on your local network so the dashboard can add them for you.

- Pure Python 3 standard library for scanning (no pip needed).
- Concurrent TCP connect scan of the ports in `scan.conf`, then per-**host:port** fingerprinting (see below).
- Each result gets a `match`: the name of an official dashboard tile, or `custom`.

## How services are identified

Every open host:port is identified from **its own** HTTP(S) responses; nothing is copied from another port on the same host. HTTPS is tried first on TLS ports (443, 8006, 9443, `*443`), same-host redirects are followed. Evidence, by priority:

1. Page `<title>` (e.g. `Immich`, `pve - Proxmox Virtual Environment`).
2. Specific headers and cookie names (`Server: pve-api-daemon`, `X-Powered-By`, `trilium.sid`, `oc_sessionPassphrase`, `PVEAuthCookie`, ...).
3. Known API/health endpoints whose **content** is validated (an SPA that answers 200 to every path does not count): Jellyfin `/System/Info/Public`, Immich `/api/server/ping`, Nextcloud `/status.php`, Navidrome `/app/`, AdGuard `/control/status` or `/login.html`, Vaultwarden `/alive` / `/api/alive`, Trilium and FileBrowser `/login`, RomM `/api/heartbeat`, NPM `/api/`, Portainer `/api/status`, Pi-hole `/admin/`, ForgePanel `/api/health`, Grafana, Home Assistant, Plex.
4. Favicon md5 (optional, `[favicon_hashes]` in `scan.conf`).

Ports in `[ports]` are only weak hints (they decide which endpoints are tried first). A bare 400/401/403, or a page with no evidence, becomes `Unknown service (host:port)` with low confidence and a neutral icon.

Each result carries `name`, `icon` (the same logo file as the built-in tile, or null), `confidence` (`high` = two pieces of evidence or a validated endpoint, `medium` = one title/header/cookie, `low` = page text only or unknown), `evidence` and `identified_by` (e.g. `title + /api/server/ping`). The dashboard shows it on the tile: *Detected: Immich (title + /api/server/ping)*.

## Usage

```bash
cd ForgePanel-Dashboard
python3 scan/scan.py                       # uses scan/scan.conf (subnet = auto -> your /24)
python3 scan/scan.py --subnet 192.168.1.0/24
FORGEPANEL_PASSWORD='your-dashboard-password' python3 scan/scan.py   # non-interactive
```

Easiest: click **Scan Network** in the dashboard. `server.py` imports this script, runs it with `scan.conf`, and stores the results encrypted in `database/forgepanel.db`.

From the CLI you are asked for a password (via `getpass`, never as a CLI argument; normally your dashboard password). Then click **Import Scan** in the dashboard: the server reads `scan/results.enc` (or `results.json`) from this folder, or you upload the file, and it is decrypted on the server with the password you enter. Found official services fill empty tiles, the rest become custom services, and existing IP:port entries are skipped.

## Encryption

Results are written to `scan/results.enc` with AES-256-GCM, key from PBKDF2-SHA256 (salt `homelab-v2`, 100000 iterations), the same as `js/auth.js`. Format: `{"format":"forgepanel-scan","version":1,"iv":[...],"ct":[...]}`.

This needs the Python `cryptography` package (`apt install python3-cryptography`, `dnf install python3-cryptography`, `pacman -S python-cryptography`, `apk add py3-cryptography`). Without it, or with `--plain`, the scanner writes an **unencrypted** `scan/results.json` and prints a warning.

Decryption happens on the server, so this works over plain `http://<ip>` too. `server.py` never serves `scan/results.*` over HTTP.

`results.enc` / `results.json` are in `.gitignore`. See `results.example.json` for the format (fake data). If you change your dashboard password, re-run the scan.

## Configuration (`scan.conf`)

| Key | Meaning |
|---|---|
| `subnet` | `auto`, a CIDR, a host, or a comma list |
| `max_hosts` | safety cap on hosts scanned |
| `timeout`, `threads` | per-connect timeout (s) and concurrency |
| `http_probe` | grab HTTP title/Server header |
| `output`, `plain_output` | output file names (relative to `scan/`) |
| `[ports]` | `port = candidate names` (weak hints only) |
| `[fingerprints]` | legacy title keywords (not used for detection) |
| `[favicon_hashes]` | optional `md5 = service name` |
| `[official]` | tile names in `pages/dashboard.html` |

Only scan networks you own or are allowed to scan.
