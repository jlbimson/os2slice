# os2slice

Print Onshape parts on Bambu Lab printers through [BamBuddy](https://github.com/maziggy/bambuddy), from inside Onshape.

- A **Print** panel in every Part Studio: select a part (or several, for multi-colour), click the face it stands on, pick a printer and a loaded AMS filament, set walls, infill and supports, and press **Slice and queue print**. BamBuddy slices it and queues it; nothing prints until someone presses Start.
- A live 3D preview of the part as it will sit on the plate.
- **Open in Bambu Studio**: in a shared Bambu Studio served in the browser, or in your own, with the printer and filament profiles already set.
- Each user signs in with their own Onshape account (read-only), so no documents need sharing.

It runs as Home Assistant add-ons on the machine that runs BamBuddy, and serves the office LAN only.

```
Onshape (browser) ──panel / right-click──▶ os2slice add-on ──▶ Onshape API (export, read-only)
                                                  │
                                                  ├──▶ BamBuddy: upload, slice, queue ──▶ printers
                                                  └──▶ Bambu Studio (web) add-on ──virtual printers──▶ BamBuddy
```

## Requirements

- **Home Assistant OS** on a machine on the printers' LAN, with these add-ons:
  - BamBuddy, with your printers added. Leave its auth off, or create an API key with *read status*, *library* and *queue*.
  - Duck DNS, with a certificate. Point the Duck DNS name at the machine's **LAN** address: browsers need HTTPS, but nothing is exposed to the internet.
  - An SSH add-on with root login (e.g. Advanced SSH & Web Terminal), for installing os2slice.
- **Onshape:** a developer account to create the app. Assigning it to coworkers needs a Professional or Enterprise plan.
- Users on the same LAN, using Chrome, Edge, Firefox or Safari with pop-ups allowed for the os2slice address.

## Install

### 1. The os2slice add-on

From a clone of this repo, with SSH access to Home Assistant as `root`:

```bash
OS2SLICE_HA_HOST=root@<ha-host> OS2SLICE_HA_KEY=~/.ssh/<key> scripts/deploy_addon.sh deploy os2slice
```

(Or put those two settings in an untracked `.deploy.env` at the repo root.)

This copies the add-on to `/local_apps/os2slice` on Home Assistant and installs, updates or rebuilds it. To do it by hand: run `scripts/deploy_addon.sh stage <dir> os2slice`, copy `<dir>` to `/local_apps/os2slice`, then install **os2slice** from *Local add-ons* in the add-on store.

Configure it (Settings → Add-ons → os2slice → Configuration). [`addon/os2slice/DOCS.md`](addon/os2slice/DOCS.md) explains each option. The essentials:

| Option | Value |
|---|---|
| `hosts` | `<name>.duckdns.org:8443` |
| `bambuddy_url` | `http://172.30.32.1:8000` (the Home Assistant host, seen from add-ons) |
| `bambuddy_api_key` | the BamBuddy key (any non-empty value if BamBuddy auth is off) |
| `onshape_auth` | `oauth` (per-user sign-in), after step 3; until then `keys` with a read-only Onshape API key pair |
| `presets` | slicer presets per printer model (examples included) |

Start it. The log opens with a pass/fail table; every line should say PASS.

### 2. Bambu Studio in the browser (optional)

```bash
scripts/deploy_addon.sh deploy bambustudio_web
```

Then open `https://<name>.duckdns.org:3443` and finish Bambu Studio's setup wizard. Restart the add-on once. See [`addon/bambustudio_web/DOCS.md`](addon/bambustudio_web/DOCS.md).

To send prints from Bambu Studio to BamBuddy, create one BamBuddy **virtual printer** per real printer:

1. Give the Home Assistant machine one extra fixed LAN address per virtual printer, e.g. `ha network update <iface> --ipv4-method static --ipv4-address <main>/24 --ipv4-address <extra>/24 … --ipv4-gateway <router> --ipv4-nameserver <dns>`.
2. In BamBuddy, open Settings → Virtual Printer and add one per printer: Print Queue mode, the matching model, its own address, and the real printer as the target. Each card's **Diagnose** should pass.
3. In the web Bambu Studio, open Device → Add printer → by IP, using each virtual printer's address and the real printer's LAN access code. In Bambu Studio, use **Send**, not Print.

The add-on makes Bambu Studio trust BamBuddy's virtual-printer certificate automatically.

### 3. The Onshape app

Follow [`docs/ONSHAPE_SETUP.md`](docs/ONSHAPE_SETUP.md). You'll create an OAuth app with two extensions, register its redirect URL, and put its client ID and secret in the add-on. Then you either assign it to users (company plan) or subscribe through a private store entry.

## Development

```bash
uv tool install -e .          # or: pipx install -e .
uv run pytest -q              # unit tests (Onshape and BamBuddy are faked)
uv run ruff check . && uv run ruff format --check .
os2slice doctor
```

The same package also has a terminal mode: `os2slice print --url <part URL> --part <id>` slices and queues after a `[y/N]`, and `os2slice send` opens a part in a desktop slicer. Keys come from `os2slice setup-keys`, stored in the system keyring.

Design notes:
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): the pieces and the security rules.
- [`docs/DECISIONS.md`](docs/DECISIONS.md): why things are the way they are.
- [`docs/ONSHAPE_API.md`](docs/ONSHAPE_API.md) and [`docs/BAMBUDDY_API.md`](docs/BAMBUDDY_API.md): the API shapes verified against the real services.

## Security

- os2slice only **reads** from Onshape, and only uploads, slices and queues in BamBuddy.
- A print needs a button press on os2slice's own page (single-use token); queued jobs wait for Start.
- Sign-in tokens stay on the server, in a file only the service can read.
- It serves HTTPS on the LAN only, and accepts requests only for the configured host names.
- Don't expose ports 8443 or 3443 to the internet.

Bundles [three.js](https://threejs.org) (MIT, `src/os2slice/static/vendor/`).
