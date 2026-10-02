# os2slice

Print Onshape parts from inside Onshape: pick the part, the face it stands on, the printer, the filament, walls, infill and supports, then send it. Each printer has a slicer (BamBuddy itself, or the Bambu Studio API add-on) and a target (BamBuddy's queue; Klipper/Moonraker and PrusaLink printers are built but not yet tested on hardware). Whoever prints chooses whether the job starts by itself or waits until someone presses Start in BamBuddy (the **Wait for Start** checkbox). Besides each printer, the printer menu offers **Any <model>**: BamBuddy then sends the job to the first idle printer of that model with the chosen filament (type and colour) loaded.

The options below set up one BamBuddy that slices and queues. Other slicers and printers are added on the config page `/admin` (below); [`docs/MODULES.md`](https://github.com/jlbimson/os2slice/blob/main/docs/MODULES.md) explains the modules.

## Configuration

| Option | What to enter |
|---|---|
| `onshape_auth` | `oauth`: each user signs in with Onshape (recommended). `keys`: the service reads Onshape with one API key pair |
| `onshape_oauth_client_id` / `onshape_oauth_client_secret` | From the os2slice OAuth app's **Keys and secret** tab (`oauth` mode); see `docs/ONSHAPE_SETUP.md` |
| `onshape_access_key` / `onshape_secret_key` | A **read-only** Onshape API key pair (`keys` mode only); fill in both or neither |
| `bambuddy_api_key` | A BamBuddy API key with only *read status*, *library* and *queue* (any value if BamBuddy auth is off) |
| `bambuddy_url` | How this add-on reaches BamBuddy; `http://172.30.32.1:8000` is the Home Assistant host |
| `bambuddy_public_url` | BamBuddy as browsers reach it, for the panel's links (default: this host, port 8000) |
| `hosts` | The name and port browsers use, e.g. `print.example.duckdns.org:8443`; other names are refused. The first one forms the sign-in redirect URL |
| `web_studio_url` | The Bambu Studio (web) add-on's address, or empty to hide its link |
| `default_printer`, `presets` | BamBuddy printer name, and slicer presets per printer model |
| `walls`, `infill`, `supports`, `build_plate_only`, `top_layers`, `bottom_layers`, `brim`, `copies` | Starting values shown on the print page |
| `default_plate` | Build plate used unless a print or a preset's `bed_type` says otherwise. `High Temp Plate` is Bambu's name for smooth PEI |
| `manual_start` | Deprecated, has no effect (kept so existing settings still load). Prints start by themselves once the printer is free; whoever prints ticks **Wait for Start** on the page to make that print wait for Start in BamBuddy (the CLI has `--wait-for-start`) |
| `admin_password` | Turns on the config page `/admin` (at least 12 characters); see below |
| `reset_config` | Leave off. On: the next start writes `config.toml` again from these options, discarding edits made on `/admin`; turn it off again afterwards |

The HTTPS certificate is the Duck DNS add-on's `/ssl/fullchain.pem` + `/ssl/privkey.pem` (override with `certfile`/`keyfile`). It is reloaded automatically after renewal.

Sign-in tokens are kept in this add-on's private data (`/data/state/os2slice/signins.json`, readable only by the service).

The log starts with a pass/fail table from `os2slice doctor`.

## The config page `/admin`

The service has a config page for printers and slicers (modules), print defaults, the Onshape and server settings, secrets, `doctor`, jobs and the log, at `https://<host>:8443/admin` (the first of `hosts`, e.g. `https://print.example.duckdns.org:8443/admin`). The add-on's info page in Home Assistant has an **Open Web UI** button for it: it opens a plain-HTTP port (8444 on the host) that only redirects to that address.

**Password.** The page is off until you set `admin_password` on the Configuration tab and restart the add-on. The add-on stores only a hash of it (`/data/state/os2slice/admin.json`); the password is never logged. A restart with the same password keeps everyone signed in; a new one signs every admin session out. Clearing the option later doesn't turn the page off: the stored password keeps working (delete `admin.json` to remove it). A password shorter than 12 characters stops the add-on with an error saying so. There is no way to set the password from a browser.

**What persists.** `config.toml` (`/data/os2slice/config.toml`) is written from the options on the first start and whenever the options it is built from change (or a new add-on version builds it differently); otherwise it is kept as it is, so edits made on `/admin` persist across restarts. Changing any of those options on the Configuration tab replaces the file, and with it the page's edits, so after you start using `/admin` treat the page as the place for settings. `reset_config` forces that rewrite once. Settings on the page's Server and Onshape sections need a restart; everything else applies at once.

**Secrets.** Secrets saved on the page go to `/data/state/os2slice/secrets.json` (readable only by the service), never into `config.toml`. Which one is used:

1. A secret option filled in on the Configuration tab (`bambuddy_api_key`, the Onshape keys, the OAuth client secret) always wins: at each start it replaces the same secret saved on the page. A value saved on the page while the option is filled in is used only until the next start.
2. When that option is empty, the secret saved on the page is used. So to manage a secret from the page, clear its option.
3. Secrets of other modules (e.g. a Moonraker or PrusaLink printer's API key) have no option; they live only on the page.

A secret the add-on needs (the BamBuddy key, and the Onshape keys or OAuth client secret for the chosen `onshape_auth`) must be in one of the two places, or the add-on doesn't start and says which option to fill in.

**Adding the Bambu Studio API add-on as a slicer.** On `/admin/slicers`, add a `bambu-studio-api` slicer with URL `http://172.30.32.1:3001` (the Home Assistant host, seen from this add-on), press **Test connection**, save, and name it as the `slicer` of a printer or of a BamBuddy model on the Targets or Printers page. Printers using it are sliced there and BamBuddy only queues. On barnassistant it was added this way on 2026-10-01.

**The legacy `[bambuddy]` table.** The options write BamBuddy as the legacy `[bambuddy]` table; the Targets page shows it as "bambuddy (legacy table)" with **Migrate**, which rewrites it as `[targets.bambuddy]`. Changing an option that goes into the file later rewrites `config.toml` from the options, legacy table included, and the page's edits are gone (see "What persists").
