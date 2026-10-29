# ForgePanel-Dashboard

Self-hosted dashboard for accessing all your local and private services from one place. Add your own services, set names, IPs, ports, and icons, and check status at a glance.

## Features

- Service cards with online/offline status
- Custom services (name, IP, port, icon)
- Encrypted local storage (IndexedDB)
- Export / Import / Reset
- Forgot password and username recovery
- Animated background

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

## How to Run

Serve the folder with any web server and open `index.html`.

```bash
cd ForgePanel-Dashboard
python3 -m http.server 8080
```

Then open `http://localhost:8080`.

## License

MIT. See [LICENSE](LICENSE).

Service logos in `assets/icons/` are trademarks of their respective projects and are not covered by the MIT license.
