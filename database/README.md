# database/

Created and used by `server.py`. Everything here except this README and `.gitkeep` is gitignored and never served over HTTP.

| File | What it is |
|---|---|
| `forgepanel.db` | SQLite database (mode 0600, folder 0700): account (username, scrypt password hash with a random salt, security question + scrypt answer hash, must-change flag, wrapped data keys + their salts), dashboard settings, custom services, last 10 scan results |
| `forgepanel.db-journal` / `-wal` / `-shm` | SQLite temporary files, may appear while the server runs |
| `server.log` | Only when started without systemd/OpenRC by the installer |

With the Python `cryptography` package installed, dashboard settings, custom services and scan results are encrypted with AES-256-GCM using a random data key. That key is stored only wrapped (encrypted) with a key derived from your password and one derived from your security answer, and it is unwrapped in server memory only while you are signed in. Without `cryptography` these rows are stored as plain JSON and the server logs a warning.

## Back up

```bash
sudo systemctl stop forgepanel          # or make sure no one is saving
cp -a /opt/forgepanel/database ~/forgepanel-db-backup-$(date +%F)
sudo systemctl start forgepanel
```

Online alternative (safe while running): `sqlite3 /opt/forgepanel/database/forgepanel.db ".backup '$HOME/forgepanel.db.bak'"`.

You can also use **Export** in the dashboard for a portable, password-encrypted `forgepanel.conf`.

## Restore

```bash
sudo systemctl stop forgepanel
cp ~/forgepanel-db-backup-YYYY-MM-DD/forgepanel.db /opt/forgepanel/database/forgepanel.db
chmod 600 /opt/forgepanel/database/forgepanel.db
sudo systemctl start forgepanel
```

A restored database uses the username/password that were current when it was backed up. Deleting `forgepanel.db` resets ForgePanel to the default `admin` / `forgepanel` login.
