# os2slice

Print Onshape parts through BamBuddy from inside Onshape: pick the part, the face it stands on, the filament, walls, infill and supports, then queue it on a printer. Jobs wait in BamBuddy until someone presses Start.

## Configuration

| Option | What to enter |
|---|---|
| `onshape_auth` | `oauth`: each user signs in with Onshape (recommended). `keys`: the service reads Onshape with one API key pair |
| `onshape_oauth_client_id` / `onshape_oauth_client_secret` | From the os2slice OAuth app's **Keys and secret** tab (`oauth` mode); see `docs/ONSHAPE_SETUP.md` |
| `onshape_access_key` / `onshape_secret_key` | A **read-only** Onshape API key pair (`keys` mode only) |
| `bambuddy_api_key` | A BamBuddy API key with only *read status*, *library* and *queue* (any value if BamBuddy auth is off) |
| `bambuddy_url` | How this add-on reaches BamBuddy; `http://172.30.32.1:8000` is the Home Assistant host |
| `bambuddy_public_url` | BamBuddy as browsers reach it, for the panel's links (default: this host, port 8000) |
| `hosts` | The name and port browsers use, e.g. `print.example.duckdns.org:8443`; other names are refused. The first one forms the sign-in redirect URL |
| `web_studio_url` | The Bambu Studio (web) add-on's address, or empty to hide its link |
| `default_printer`, `presets` | BamBuddy printer name, and slicer presets per printer model |
| `walls`, `infill`, `supports`, `build_plate_only`, `top_layers`, `bottom_layers`, `brim`, `copies` | Starting values shown on the print page |
| `default_plate` | Build plate used unless a print or a preset's `bed_type` says otherwise. `High Temp Plate` is Bambu's name for smooth PEI |
| `manual_start` | Keep `true`: queued jobs wait for Start in BamBuddy |

The HTTPS certificate is the Duck DNS add-on's `/ssl/fullchain.pem` + `/ssl/privkey.pem` (override with `certfile`/`keyfile`). It is reloaded automatically after renewal.

Sign-in tokens are kept in this add-on's private data (`/data/state/os2slice/signins.json`, readable only by the service).

The log starts with a pass/fail table from `os2slice doctor`.
