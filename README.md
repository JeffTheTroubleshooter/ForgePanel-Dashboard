# ForgePanel-Dashboard

Self-hosted dashboard for accessing all your local and private services from one place. Add your own services, set names, IPs, ports, and icons, and check status at a glance.

## Features

- Service cards with online/offline status
- Custom services (name, IP, port, icon)
- Encrypted local storage (IndexedDB)
- Export / Import / Reset
- Forgot password and username recovery
- Animated background

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
└── custom_services/
```

## Automatic Install (with service)

`forgepanel-install.sh` installs ForgePanel and sets it up as a service that starts on boot. It works on Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE and Alpine. It installs python3 and git if needed, clones the repo (default `/opt/forgepanel`), asks which port to use (default `8080`), and creates a `forgepanel` systemd service (OpenRC on Alpine).

```bash
curl -fsSL https://raw.githubusercontent.com/JeffTheTroubleshooter/ForgePanel-Dashboard/main/forgepanel-install.sh -o forgepanel-install.sh
bash forgepanel-install.sh
```

Then open `http://<your-server-ip>:8080`. To manage the service:

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

It stops and removes the `forgepanel` service (systemd, or OpenRC on Alpine), asks which install directory to delete (default `/opt/forgepanel`), and then asks whether to remove python3 and git too. Say no to that last question if other software on the machine uses them.

## Manual Run

Serve the folder with any web server and open `index.html`.

```bash
cd ForgePanel-Dashboard
python3 -m http.server 8080
```

Then open `http://localhost:8080`.

## License

MIT. See [LICENSE](LICENSE).

Service logos in `assets/icons/` are trademarks of their respective projects and are not covered by the MIT license.
