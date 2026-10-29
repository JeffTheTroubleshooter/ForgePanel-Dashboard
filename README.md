# ForgePanel

Local homelab service dashboard.

## Structure

```
forgepanel/
├── index.html              # Entry point (redirects to login)
├── pages/
│   ├── login.html          # Authentication
│   ├── dashboard.html      # Main dashboard
│   └── dots.html           # Background animation
├── js/
│   └── auth.js             # Encryption + IndexedDB
├── css/
│   └── main.css            # Shared styles
├── assets/
│   └── icons/              # Service logos
└── config/
    └── forgepanel.conf        # Config template
```

## Default login

- Username: `admin`
- Password: `forgepanel`

Change credentials on first login.
