# os2slice

Print Onshape parts on Bambu Lab printers through [BamBuddy](https://github.com/maziggy/bambuddy), from inside Onshape.

- A **Print** panel in every Part Studio: select a part (or several, for multi-colour), click the face it stands on, pick a printer and a loaded AMS filament, set walls, infill and supports, and press **Slice and queue print**. BamBuddy slices it and queues it; nothing prints until someone presses Start.
- A live 3D preview of the part as it will sit on the plate.
- **Open in Bambu Studio**: in a shared Bambu Studio served in the browser, or in your own, with the printer and filament profiles already set.
- Each user signs in with their own Onshape account (read-only), so no documents need sharing.

It runs next to BamBuddy on a machine on the printers' LAN, either with **Docker Compose** or as **Home Assistant** add-ons, and serves that LAN only.

```
Onshape (browser) ──panel / right-click──▶ os2slice ──▶ Onshape API (export, read-only)
                                              │
                                              ├──▶ BamBuddy: upload, slice, queue ──▶ printers
                                              └──▶ Bambu Studio (web) ──virtual printers──▶ BamBuddy
```

## Requirements

- **A Linux machine on the printers' LAN** running [BamBuddy](https://github.com/maziggy/bambuddy) with your printers added, plus either Docker Compose or Home Assistant OS. Leave BamBuddy's auth off, or create an API key with *read status*, *library* and *queue*.
- **A DNS name and certificate** for that machine's **LAN** address. Onshape only opens `https://` pages, but nothing needs to be reachable from the internet. Duck DNS works well; both install routes can manage it for you.
- **Onshape:** a developer account to create the app. Assigning it to coworkers needs a Professional or Enterprise plan.
- **Users** on the same LAN, in Chrome, Edge, Firefox or Safari, with pop-ups allowed for the os2slice address.

## Install

### Option A: Docker Compose

Any Linux machine with Docker. In short:

```bash
git clone https://github.com/jlbimson/os2slice.git && cd os2slice
cp docker/config.example.toml docker/config.toml   # edit: host name, presets, Onshape client ID
cp docker/env.example .env && chmod 600 .env       # edit: secrets, Duck DNS
docker compose --profile duckdns up -d duckdns     # DNS name + certificate
docker compose --profile web-studio up -d --build  # os2slice + Bambu Studio in the browser
```

[`docs/DOCKER.md`](docs/DOCKER.md) has the full steps.

### Option B: Home Assistant OS add-ons

You'll need the BamBuddy and Duck DNS add-ons (the Duck DNS name pointing at the machine's LAN address), and an SSH add-on with root login. From a clone of this repo:

```bash
OS2SLICE_HA_HOST=root@<ha-host> OS2SLICE_HA_KEY=~/.ssh/<key> scripts/deploy_addon.sh deploy os2slice
```

You can also put those two settings in an untracked `.deploy.env` at the repo root.

This copies the add-on to `/local_apps/os2slice` and installs, updates or rebuilds it. To do it by hand:
1. Run `scripts/deploy_addon.sh stage <dir> os2slice`.
2. Copy `<dir>` to `/local_apps/os2slice`.
3. Install **os2slice** from *Local add-ons* in the add-on store.

Configure it under Settings → Add-ons → os2slice → Configuration. [`addon/os2slice/DOCS.md`](addon/os2slice/DOCS.md) explains each option. The essentials:

| Option | Value |
|---|---|
| `hosts` | `<name>.duckdns.org:8443` |
| `bambuddy_url` | `http://172.30.32.1:8000` (the Home Assistant host, seen from add-ons) |
| `bambuddy_api_key` | the BamBuddy key (any non-empty value if BamBuddy auth is off) |
| `onshape_auth` | `oauth` (per-user sign-in), once the Onshape app exists; until then `keys` with a read-only Onshape API key pair |
| `presets` | slicer presets per printer model (examples included) |

Start it. The log opens with a pass/fail table, and every line should say PASS.

For Bambu Studio in the browser (optional):
1. Run `scripts/deploy_addon.sh deploy bambustudio_web`.
2. Open `https://<name>.duckdns.org:3443` and finish Bambu Studio's setup wizard.
3. Restart the add-on once.

See [`addon/bambustudio_web/DOCS.md`](addon/bambustudio_web/DOCS.md).

### Virtual printers (for sending from the web Bambu Studio)

To send prints from Bambu Studio to BamBuddy, create one BamBuddy **virtual printer** per real printer:

1. Give the machine one extra fixed LAN address per virtual printer:
   - **Home Assistant:** `ha network update <iface> --ipv4-method static --ipv4-address <main>/24 --ipv4-address <extra>/24 … --ipv4-gateway <router> --ipv4-nameserver <dns>`.
   - **Docker host:** see [BamBuddy's guide](https://github.com/maziggy/bambuddy-wiki/blob/main/docs/features/virtual-printer.md#dedicated-bind-ip).
2. In BamBuddy, open Settings → Virtual Printer and add one per printer: Print Queue mode, the matching model, its own address, and the real printer as the target. Each card's **Diagnose** should pass.
3. In the web Bambu Studio, open Device → Add printer → by IP. Use each virtual printer's address and the real printer's LAN access code. In Bambu Studio, use **Send**, not Print.

The web Bambu Studio trusts BamBuddy's virtual-printer certificate automatically.

### The Onshape app

Follow [`docs/ONSHAPE_SETUP.md`](docs/ONSHAPE_SETUP.md):
1. Create an OAuth app with two extensions.
2. Register its redirect URL.
3. Put its client ID and secret in os2slice's settings.
4. Assign it to users (company plan), or subscribe through a private store entry.

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
