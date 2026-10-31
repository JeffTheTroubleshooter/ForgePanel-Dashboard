# custom_services/

When **Scan Network** or **Import Scan** adds a service that has no official dashboard tile, `server.py` writes one JSON file here per service, for example `portainer-10.0.0.21-9000.json`:

```json
{
  "generated_by": "forgepanel-scan",
  "name": "Portainer",
  "host": "10.0.0.21",
  "port": 9000,
  "scheme": "http",
  "url": "http://10.0.0.21:9000",
  "source": "scan",
  "found_at": "2026-10-31T15:40:00-0400",
  "matched_by": "fingerprint"
}
```

- File names are `<name>-<host>-<port>.json`, lowercased, with anything other than `a-z 0-9 .` replaced by `-`. There is one file per host and port, so no duplicates.
- **Save & Check** on that tile rewrites the file (a renamed service or a new IP/port gets a new file and the old one is removed). **Delete** removes the file. **Reset** removes all scan-written files.
- `matched_by` is `fingerprint` (HTTP title/Server header), `port` (port number only) or `unknown`.
- Only files containing `"generated_by": "forgepanel-scan"` are managed. Your own notes, icons, or other files here are never touched.

## Security

These files are **plain JSON and not encrypted**, so the folder stays readable and easy to share. They contain only a name, host, port, URL and scan metadata. No passwords, tokens or logos ever go here. The encrypted copy of your services lives in `database/forgepanel.db`.

They do reveal your LAN layout, so:
- scan-written `*.json` files are gitignored (only `example.json` is committed), and
- `server.py` does not serve `custom_services/*.json` over HTTP (except `example.json`).
