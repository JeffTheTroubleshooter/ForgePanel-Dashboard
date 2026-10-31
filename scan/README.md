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

You are asked for your **dashboard login password** (via `getpass`, never as a CLI argument). Then in the dashboard click **Import Scan**: it loads `../scan/results.enc` from the server (or lets you pick the file), decrypts it with your session password, fills empty official tiles and adds the rest as custom services. Existing IP:port entries are skipped.

## Encryption

Results are written to `scan/results.enc` with AES-256-GCM, key from PBKDF2-SHA256 (salt `homelab-v2`, 100000 iterations), the same as `js/auth.js`. Format: `{"format":"forgepanel-scan","version":1,"iv":[...],"ct":[...]}`.

This needs the Python `cryptography` package (`apt install python3-cryptography`, `dnf install python3-cryptography`, `pacman -S python-cryptography`, `apk add py3-cryptography`). Without it, or with `--plain`, the scanner writes an **unencrypted** `scan/results.json` and prints a warning.

Browsers only have WebCrypto on HTTPS or `http://localhost`. If you open the dashboard over plain `http://<ip>`, `results.enc` cannot be decrypted there; use `--plain` to produce `results.json` for that case (anyone who can reach the web server can read it, so delete it after importing).

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
