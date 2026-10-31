# ForgePanel-Dashboard

Self-hosted dashboard for accessing all your local and private services from one place. Add your own services, set names, IPs, ports, and icons, and check status at a glance.

## Features

- Service cards with online/offline status
- Custom services (name, IP, port, icon)
- Python backend (`server.py`, standard library) with a SQLite database in `database/`
- Data encrypted at rest (AES-256-GCM, optional `cryptography` package)
- Export / Import / Reset
- Forgot password and username recovery
- Animated background
- Network scan that finds services and adds them automatically

## Screenshots

### Login
![ForgePanel login screen](screenshots/login.png)

### Dashboard
![ForgePanel dashboard with service cards](screenshots/dashboard.png)

### Settings toolbar
Check All, Export, Import, Reset and Log Out.

![ForgePanel settings toolbar](screenshots/toolbar.png)

### Adding a custom service
<img src="screenshots/custom-service.gif" alt="Adding a custom service in ForgePanel" width="400">

[Full-quality MP4](screenshots/custom-service.mp4)

## Default Login

- **Username:** `admin`
- **Password:** `forgepanel`

Change credentials on first login.

## Structure

```
ForgePanel-Dashboard/
├── index.html
├── server.py            # static files + JSON API + SQLite
├── database/            # forgepanel.db lives here (gitignored, never served)
├── pages/
│   ├── login.html
│   ├── dashboard.html
│   └── dots.html
├── js/
│   └── auth.js
├── css/
│   └── main.css
├── assets/
│   └── icons/
├── config/
│   └── forgepanel.conf
├── custom_services/
└── scan/
    ├── scan.py
    ├── scan.conf
    └── results.example.json
```

## Network Scan

Click **Scan Network** in the dashboard toolbar. The server runs `scan/scan.py` (Python standard library), which TCP-scans your LAN for well-known self-hosted ports (Jellyfin, Immich, Nextcloud, Proxmox, Portainer, Home Assistant, Plex, Grafana and more, set in `scan/scan.conf`) and identifies services by HTTP title or Server header. Results are stored (encrypted) in the database; found official services fill their tiles, everything else is added as a custom service, and IP:port duplicates are skipped.

The CLI still works and writes `scan/results.enc` (encrypted with a password you type) for **Import Scan**:

```bash
python3 scan/scan.py
```

Details in [scan/README.md](scan/README.md).

## Database and Security

- `server.py` keeps everything in `database/forgepanel.db` (SQLite, file 0600, folder 0700). The server never serves `database/`, `scan/results.*`, `config/`, `.py`/`.sh`/`.conf` files or dotfiles, and has no directory listings.
- Passwords and security answers are hashed with scrypt and a random per-user salt. Sessions are random tokens in an `HttpOnly; SameSite=Strict` cookie that expires after 12 hours. Login, export and forgot-password attempts are limited to 5 failures per 5 minutes per IP.
- With the `cryptography` package (`python3-cryptography`) installed, dashboard settings, custom services and scan results are encrypted with AES-256-GCM. The data key is wrapped by keys derived from your password and your security answer, and is unwrapped only in server memory while you are signed in. Changing or resetting the password re-encrypts everything with a new key.
- **Without `cryptography` the data is stored unencrypted**; the server logs a warning at startup. Passwords are still hashed.
- The browser no longer stores anything except the session cookie, so the dashboard works over plain `http://<ip>`. Traffic itself is not encrypted on plain HTTP; put ForgePanel behind a reverse proxy with HTTPS (e.g. Nginx Proxy Manager) if you reach it over untrusted networks.

### Backup

Back up the `database/` folder (stop the service first), or use **Export** in the dashboard for a password-encrypted `forgepanel.conf`. See [database/README.md](database/README.md) for backup and restore steps.

### Upgrading from the IndexedDB version

Older versions kept data in the browser. Before upgrading, open the old dashboard and click **Export**; after upgrading, sign in (default `admin` / `forgepanel`, then set new credentials) and **Import** that file with the old password. Re-running the installer switches the service from `python3 -m http.server` to `server.py` automatically.

## Automatic Install (with service)

`forgepanel-install.sh` installs ForgePanel and sets it up as a service that starts on boot. It works on Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE and Alpine. It installs python3 (with sqlite3), git, curl and python3-cryptography, checks that `import sqlite3` / `import cryptography` work, clones or updates the repo (default `/opt/forgepanel`), creates `database/` (0700), asks which port to use (default `8084`), opens that port in ufw/firewalld if active, and creates a `forgepanel` systemd service (OpenRC on Alpine) running `python3 server.py --port <port> --host 0.0.0.0`. Running it again on an existing install pulls the latest code and restarts the service.

```bash
curl -fsSL https://raw.githubusercontent.com/JeffTheTroubleshooter/ForgePanel-Dashboard/main/forgepanel-install.sh -o forgepanel-install.sh
bash forgepanel-install.sh
```

Then open `http://<your-server-ip>:8084`. To manage the service:

```bash
sudo systemctl status forgepanel
sudo systemctl restart forgepanel
sudo systemctl disable --now forgepanel   # stop and remove from boot
```

## Uninstall

Run the same script with `uninstall`:

```bash
bash forgepanel-install.sh uninstall
```

It stops and removes the `forgepanel` service (systemd, or OpenRC on Alpine), asks whether to delete the database (`database/`), asks which install directory to delete (default `/opt/forgepanel`; if you keep the database it is first copied to `~/forgepanel-database-backup-<date>`), and then asks whether to remove python3 and git too. Say no to that last question if other software on the machine uses them.

## Manual Run

```bash
cd ForgePanel-Dashboard
python3 server.py                 # http://localhost:8084, database in database/
python3 server.py --port 8084 --host 0.0.0.0 --db database/forgepanel.db
```

Then open `http://localhost:8084` (or `http://<server-ip>:8084`). A plain static server (`python3 -m http.server`) no longer works, because the dashboard needs the API.

## License

MIT. See [LICENSE](LICENSE).

Service logos in `assets/icons/` are trademarks of their respective projects and are not covered by the MIT license.
