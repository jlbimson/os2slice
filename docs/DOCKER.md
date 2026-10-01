# Docker Compose deployment

Run os2slice on any Linux machine with Docker, no Home Assistant needed. It takes three pieces, all on the same machine on the printers' LAN:

| Service | What it does | Needed? |
|---|---|---|
| BamBuddy | talks to the printers, slices, queues | yes; installed with BamBuddy's own instructions |
| `os2slice` | the Onshape panel and print pages, HTTPS on port 8443 | yes |
| `duckdns` (profile) | points `<name>.duckdns.org` at this machine's LAN address and keeps its HTTPS certificate in `./certs` | unless you bring your own certificate |
| `bambustudio-web` (profile) | Bambu Studio in a browser tab on port 3443, one shared session | optional |

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
- `oauth_client_id`: from the Onshape app (step 4). Until you have it, set `auth = "keys"` and put a read-only Onshape API key pair in `.env`.
- `default_printer` and the `[bambuddy.presets."<model>"]` tables: your BamBuddy printer name and the slicer presets per printer model.
- `[web_studio]`: its URL, or delete the table if you won't run the web Bambu Studio.

Edit **`.env`**. It holds the secrets and is never committed:

- `ONSHAPE_OAUTH_CLIENT_SECRET`, `BAMBUDDY_API_KEY`.
- For `duckdns`: `DUCKDNS_DOMAIN`, `DUCKDNS_TOKEN` (from duckdns.org), `LAN_IP` (this machine's fixed LAN address), and `ACME_EMAIL`.
- `WEB_STUDIO_PASSWORD`: optional, a password for the web Bambu Studio.

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

## 4. Onshape

Follow [`ONSHAPE_SETUP.md`](ONSHAPE_SETUP.md), with `<host>` = your `hosts` entry. Then:

1. Put the client ID in `docker/config.toml` and the secret in `.env`.
2. Run `docker compose up -d` to apply them.

## Day to day

| Task | Command |
|---|---|
| Update | `git pull && docker compose --profile web-studio --profile duckdns up -d --build` |
| Logs | `docker compose logs -f os2slice` (also `bambustudio-web`, `duckdns`) |
| Check setup | `docker compose exec os2slice os2slice doctor` |
| Back up | the volumes `os2slice-data` (sign-ins) and `bambustudio-config` (Bambu Studio settings), plus `docker/config.toml`, `.env` and `./certs` |

**Certificate renewal:**
- `duckdns` renews about 30 days before expiry.
- os2slice picks up the new files by itself, within an hour.
- The web Bambu Studio only copies them when it starts, so restart it after a renewal.

**Security:**
- Keep ports 8443 and 3443 on the LAN only: don't forward them from the router.
- The web Bambu Studio runs without AppArmor and seccomp confinement (`security_opt` in `docker-compose.yml`), because Docker's default AppArmor profile makes Bambu Studio abort. Give it a password if the LAN isn't trusted.
