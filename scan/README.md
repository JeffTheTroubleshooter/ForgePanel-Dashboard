# ForgePanel Network Scan

`scan.py` finds self-hosted services on your local network so the dashboard can add them for you.

- Pure Python 3 standard library for scanning (no pip needed).
- Concurrent TCP connect scan of the ports in `scan.conf`, plus an optional HTTP `<title>` / `Server` grab to identify the service.
- Each result gets a `match`: the name of an official dashboard tile, or `custom`.

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
| `[ports]` | `port = candidate names`, most likely first |
| `[fingerprints]` | text in title/Server -> service name |
| `[official]` | tile names in `pages/dashboard.html` |

Only scan networks you own or are allowed to scan.
