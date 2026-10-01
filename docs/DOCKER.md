# Docker Compose deployment

Run os2slice on any Linux machine with Docker, no Home Assistant needed. The pieces, all on the same machine on the printers' LAN:

| Service | What it does | Needed? |
|---|---|---|
| BamBuddy | talks to Bambu printers, slices, queues | for Bambu printers (the example config uses it as its one target); installed with BamBuddy's own instructions |
| `os2slice` | the Onshape panel and print pages, HTTPS on port 8443 | yes |
| `duckdns` (profile) | points `<name>.duckdns.org` at this machine's LAN address and keeps its HTTPS certificate in `./certs` | unless you bring your own certificate |
| `bambustudio-web` (profile) | Bambu Studio in a browser tab on port 3443, one shared session | optional |
| a slicer sidecar | Bambu Studio or OrcaSlicer as a REST service, for slicing outside BamBuddy | optional; not in this compose file (see "Other printers and slicers") |

Onshape only talks to `https://` pages with a real certificate, so os2slice needs a DNS name and a certificate. The `duckdns` service handles both: the name resolves to the machine's private LAN address, and the certificate comes from Let's Encrypt through a DNS challenge. Nothing is opened to the internet.

## 1. BamBuddy

Install BamBuddy with [its Docker instructions](https://github.com/maziggy/bambuddy). It must use **host networking** for printer discovery and virtual printers. Add your printers, and note:

- **API key:** leave BamBuddy auth off, or create an API key with *read status*, *library* and *queue*.
- **Port:** its port is 8000 on the host. The os2slice containers reach it as `http://host.docker.internal:8000`. If the host runs a firewall, allow the Docker bridge to reach port 8000.

## 2. Settings

```bash
git clone https://github.com/jlbimson/os2slice.git && cd os2slice
cp docker/config.example.toml docker/config.toml
cp docker/env.example .env && chmod 600 .env
```

Edit **`docker/config.toml`**:

- `hosts`: your name and port, e.g. `["print.example.duckdns.org:8443"]`.
- `oauth_client_id`: from the Onshape app (step 5). Until you have it, set `auth = "keys"` and put a read-only Onshape API key pair in `.env`.
- `default_printer` and the `[bambuddy.presets."<model>"]` tables: your BamBuddy printer name and the slicer presets per printer model. `[bambuddy]` is the legacy form of one BamBuddy target; the module form (`[targets.<key>]`, [`MODULES.md`](MODULES.md)) works too, and is how other printers and slicers are added.
- `[web_studio]`: its URL, or delete the table if you won't run the web Bambu Studio.

Edit **`.env`**. It holds the secrets and is never committed:

- `ONSHAPE_OAUTH_CLIENT_SECRET`, `BAMBUDDY_API_KEY`.
- For `duckdns`: `DUCKDNS_DOMAIN`, `DUCKDNS_TOKEN` (from duckdns.org), `LAN_IP` (this machine's fixed LAN address), and `ACME_EMAIL`.
- `WEB_STUDIO_PASSWORD`: optional, a password for the web Bambu Studio.
- Secrets of other modules, as `OS2SLICE_SECRET_<SECTION>_<KEY>_<FIELD>`, e.g. `OS2SLICE_SECRET_TARGETS_VORON_API_KEY` for `targets.voron.api_key` (dots and dashes become underscores).

Give the machine a fixed LAN address, e.g. a DHCP reservation in the router, so the name keeps pointing at it.

## 3. Start

```bash
docker compose --profile duckdns up -d duckdns     # first, so ./certs exists
docker compose logs duckdns                        # wait for "certificate: … updated"
docker compose --profile web-studio up -d --build  # os2slice + web Bambu Studio
docker compose logs os2slice                       # every line of the table should say PASS
```

If you bring your own certificate instead of `duckdns`, put `fullchain.pem` and `privkey.pem` in `./certs`. The private key must be readable by user 1000.

Leave out `--profile web-studio` to run os2slice alone.

For the **web Bambu Studio**:

1. Open `https://<name>.duckdns.org:3443` and finish Bambu Studio's setup wizard.
2. Run `docker compose restart bambustudio-web` once.
3. To send prints from it to BamBuddy, set up one BamBuddy **virtual printer** per printer, as described in the README ("Bambu Studio in the browser"). Each needs its own extra LAN address on this machine; [BamBuddy's guide](https://github.com/maziggy/bambuddy-wiki/blob/main/docs/features/virtual-printer.md#dedicated-bind-ip) shows how to add them on Linux. Add them in the web Bambu Studio by IP.

The container trusts BamBuddy's virtual-printer certificate automatically.

## 4. The config page `/admin` (optional)

`https://<name>.duckdns.org:8443/admin` shows the configured slicers, targets and printers with their health, the Onshape and server settings, print defaults, secrets, jobs and the log. It answers 503 until an admin password is set, and the password can't be set from a browser. Set it in the running container (it asks twice, at least 12 characters):

```bash
docker compose exec os2slice os2slice admin-password
```

The running service picks it up without a restart. What the page keeps, all in the `os2slice-data` volume (`/data/state/os2slice/` in the container):

- `admin.json`: the password's scrypt hash (0600). Admin sessions live in memory and end on a restart. Run the command again to change the password; delete the file to turn the page off.
- `secrets.json`: secrets saved on the page (0600). The image has no keyring (`OS2SLICE_ADDON=1`, null keyring backend), so this file is the secret store. It is read before the environment, so a secret saved on the page wins over the same one in `.env`; remove it from the file to go back to `.env`. Pages show secrets only as "set" or "not set".

**Saving config from the page doesn't work with this compose file as shipped.** `docker/config.toml` is mounted read-only (`:ro`), and the page saves by writing a new file and renaming it over the old one, which a single-file bind mount refuses either way (read from the code; not tried in a container). So the page's forms, Migrate included, fail to save there; edit `docker/config.toml` and run `docker compose restart os2slice` instead. Secrets, the password, the checks, **Test connection**, jobs and the log work.

## 5. Onshape

Follow [`ONSHAPE_SETUP.md`](ONSHAPE_SETUP.md), with `<host>` = your `hosts` entry. Then:

1. Put the client ID in `docker/config.toml` and the secret in `.env`.
2. Run `docker compose up -d` to apply them.

## Other printers and slicers

os2slice can slice with a Bambu Studio or OrcaSlicer API sidecar instead of BamBuddy, and send to Klipper (Moonraker) or Prusa (PrusaLink) printers ([`MODULES.md`](MODULES.md)). This compose file doesn't run a sidecar. The images exist (`ghcr.io/maziggy/bambu-studio-api`, `ghcr.io/afkfelix/orca-slicer-api`; versions and ports in [`SLICERAPI_API.md`](SLICERAPI_API.md)); run one yourself, or use the one BamBuddy's own compose file starts, and point a slicer entry at it:

```toml
[slicers.studio-api]
kind = "bambu-studio-api"                  # or "orca-slicer-api"
url = "http://host.docker.internal:3001"   # the sidecar's published port on this host
```

Then name `studio-api` as the `slicer` of a printer (`[printers.<key>]`) or of a model (`[targets.<key>.models."<model>"]`). Moonraker and PrusaLink targets are built from their API docs and not yet tested on a printer.

## Day to day

| Task | Command |
|---|---|
| Update | `git pull && docker compose --profile web-studio --profile duckdns up -d --build` |
| Logs | `docker compose logs -f os2slice` (also `bambustudio-web`, `duckdns`) |
| Check setup | `docker compose exec os2slice os2slice doctor` |
| Back up | the volumes `os2slice-data` (sign-ins, admin password, page-saved secrets, log) and `bambustudio-config` (Bambu Studio settings), plus `docker/config.toml`, `.env` and `./certs` |

**Certificate renewal:**
- `duckdns` renews about 30 days before expiry.
- os2slice picks up the new files by itself, within an hour.
- The web Bambu Studio only copies them when it starts, so restart it after a renewal.

**Security:**
- Keep ports 8443 and 3443 on the LAN only: don't forward them from the router.
- The web Bambu Studio runs without AppArmor and seccomp confinement (`security_opt` in `docker-compose.yml`), because Docker's default AppArmor profile makes Bambu Studio abort. Give it a password if the LAN isn't trusted.
