# Architecture

## Target: print through BamBuddy (Phases 2–6)

```
Onshape (browser, any machine on Josh's tailnet)
  ├─ M3: right-click part → "Print with BamBuddy" → new tab GET /print?d=…&wv=…&wvid=…&e=…&p=…&c=…
  └─ M4: Element right panel iframe GET /panel  ⇄ postMessage (applicationInit, SELECTION: part + bed face)
       └─ https://<service>.<tailnet>.ts.net   (Tailscale add-on, HTTPS, Tailscale-User-Login)
            └─ os2slice add-on on homeassistant (HAOS), 127.0.0.1:8765
                      ├─ check Host, Tailscale user, params          (no side effects on GET)
                      ├─ Onshape: part + document names
                      ├─ BamBuddy: printers + status, presets
                      └─ page/panel: printer, orientation, walls, infill, supports
                              ──[POST + CSRF, same-origin]──► job
                                                                          ├─ Onshape: export STL
                                                                          ├─ orient: rotate mesh (face-down normal → −Z), drop to Z=0
                                                                          ├─ BamBuddy: upload to library folder
                                                                          ├─ BamBuddy: slice job with setting overrides → poll → .gcode.3mf
                                                                          ├─ BamBuddy: queue/start on chosen printer
                                                                          └─ /jobs/<id> page shows progress
```

- BamBuddy runs on the same NUC (HA add-on, host network, port 8000), so os2slice reaches it at its local URL from config (default API base `/api/v1`, `X-API-Key` header, from BamBuddy's API reference). **Exact upload, slice and queue shapes are unverified until Phase 2**; they go in `BAMBUDDY_API.md`.
- Slicing uses BamBuddy's sidecar (Bambu Studio recommended by BamBuddy's docs). It accepts STL and 3MF, not STEP.
- Safety model: D-13. Access model: D-11. Packaging and secrets: D-12.
- Reused from Phase 1 unchanged: `request.py` (the IDs and config validation; the `slicer` param becomes optional for `/print`), `onshape.py`, `files.py`, `errors.py`, `config.py` (plus new tables).
- New modules: `bambuddy.py` (client), `orientation.py` (STL rotation), `settings.py` (validated print settings), `server.py` (page, panel, jobs, header/identity/CSRF/framing checks), `jobs.py` (background job state). Packaging: `addon/` (HA local add-on: Dockerfile, `config.yaml`).
- Orientation and settings: D-14. Panel as the primary UI: D-15.

## Secondary: local-slicer mode (Phases 0–1, built)

```
Onshape (browser)
  └─ right-click part → "Send to OrcaSlicer"   [Onshape extension, action: Open in new window]
       └─ new tab: http://localhost:8765/open?slicer=orca&d=…&wv=w&wvid=…&e=…&p=…&c=…&<Onshape's own params>
            └─ `os2slice serve`   (systemd user service, bound to 127.0.0.1:8765)
                 ├─ check Host + Sec-Fetch-* headers (D-9)
                 ├─ validate params → ExportRequest
                 ├─ keyring → API keys
                 ├─ Onshape REST API → part name + STL bytes
                 ├─ write ~/OnshapeExports/<doc>/<part>_<cfg>_<ts>.stl
                 ├─ Popen([slicer, file])   (detached)
                 ├─ notify-send "Sent <part> to OrcaSlicer"
                 └─ reply with a small HTML page: "Sent Part 1 to OrcaSlicer" or the error + fix
```

The only outbound network traffic is the helper calling the Onshape API. Nothing is hosted. See D-3 for why this is a localhost listener rather than a custom `os2slice://` scheme, and how that option stays open.

## Module layout (current)

```
src/os2slice/
  __init__.py
  cli.py          argparse entry point; serve/handle/send/setup-keys/install-service/uninstall-service/doctor
  request.py      query params / Onshape browser URL → ExportRequest (validation lives here)
  pipeline.py     ExportRequest → export → save → launch → notify; returns a Result (used by serve, handle, send)
  server.py       stdlib ThreadingHTTPServer: routing, header checks, HTML result page
  config.py       TOML config load/default/validate
  auth.py         keyring + env fallback
  onshape.py      httpx client: part name, document name, STL export (later: translations)
  files.py        naming, sanitizing, pruning
  slicers.py      argv building + detached launch (the desktop hand-off)
  notify.py       desktop notifications
  logsetup.py     rotating file log
  printing.py     the print path: plan (read-only) → export, orient → slicer → target
  bambuddy.py     BamBuddy REST client (used by modules/bambuddy.py)
  filaments.py    AMS slots / external spools, preset matching, sliced-nozzle check
  threemf.py      Bambu-style 3MF writer and project-settings transplant
  modules/        slicer and target modules (docs/MODULES.md, D-27)
    base.py           the contract: ModuleSpec, PrinterInfo, Material, SliceInput, protocols
    registry.py       kind → class, spec_for, Modules (the configured modules of a process)
    bambuddy.py       BamBuddy: slicer + target (role "both")
    bambu_project.py  Bambu/Orca project layout (centring, copies, filaments, prime tower)
  platform/
    __init__.py   picks the implementation by sys.platform
    linux.py      systemd user unit install/uninstall/status
tests/
scripts/          spikes, one-offs
```

`request.py` takes a mapping of query params, not a URL of a particular transport. The HTTP listener, `os2slice handle <url>` (accepts `http://localhost:…/open?…` and, for a future option A, `os2slice://open?…`) and `send` all feed the same parser, so adding a custom-scheme handler later touches only `cli.py` and `platform/`.

## ExportRequest

```python
@dataclass(frozen=True)
class ExportRequest:
    slicer: str  # key into config [slicers.*]
    document_id: str
    wvm: Literal["w", "v", "m"]
    wvm_id: str
    element_id: str
    part_id: str | None  # None = whole Part Studio (Phase 4)
    configuration: str  # "" if default; decoded once, httpx encodes once when sending
    fmt: Literal["stl", "3mf", "step"] = "stl"
```

## Validation rules (the security boundary)

The param rules apply to both modes. The first HTTP-layer table below is for the local-slicer listener. The print service (`server.py`) enforces:

| Check | Rule |
|---|---|
| bind | `127.0.0.1`, except `identity = "lan"` (D-17), which serves HTTPS itself with `server.tls_cert`/`tls_key` (DuckDNS certificate, reloaded when renewed) and may bind the LAN |
| `Host` | must be in `server.hosts`, else 403 |
| identity | `"lan"`: anyone on the LAN (D-17). `"tailscale"`: `Tailscale-User-Login` must be in `server.allowed_users`. `"none"`: refused by config unless every host is localhost |
| `GET /print` | `Sec-Fetch-Dest: document`; validates the Onshape params (`parse_print_query`: no `slicer`, Onshape's own params allowed). Reads only: part/document names, printers + status. Issues a CSRF token bound to user + part + configuration |
| `POST /print` | `application/x-www-form-urlencoded` ≤ 16 KB, only known fields, `Sec-Fetch-Site: same-origin`, matching `Origin`. Settings and orientation are validated first, then the single-use token (30 min) is redeemed, then a job starts |
| `/jobs/<id>` | random 128-bit id, visible only to the user who started it; refreshes itself while running |
| sign-in (D-23) | Only with `[onshape] auth = "oauth"` (needs `identity = "lan"` + TLS). `GET /auth/start` and `/auth/callback`: `Sec-Fetch-Dest: document` only; `state` single use, 10 min, bound to a nonce cookie (`Path=/auth/`) from the same browser; the `next` page must be `/print?…` or `/panel?…`. `POST /auth/claim` and `/auth/sign-out`: same-origin form POSTs; claim codes single use, 2 min. Session cookies: random 256-bit ids, `Secure; HttpOnly`, `SameSite=Lax` (top-level) or `SameSite=None; Partitioned` (panel), 30 days; stored server-side as hashes. Tokens: `signins.json`, 0600, never logged or sent to browsers. Onshape is read with the signed-in user's token; not signed in → the panel/page shows Sign in, other routes 401 |
| responses | CSP `default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'`, `nosniff`, `no-referrer`, `no-store`. Every interpolated value is HTML-escaped |

Any web page can navigate a browser to `http://localhost:8765/open?…`, so every request is hostile until proven otherwise.

### HTTP layer (`server.py`)

| Check | Rule |
|---|---|
| bind | `127.0.0.1` only, port from config (default 8765) |
| method | `GET` only; anything else → 405 |
| `Host` | exactly `localhost:<port>` or `127.0.0.1:<port>`, else 421 (DNS rebinding) |
| path | `/open` (export), `/health` (for `doctor`: returns version, no work done), `/favicon.ico` → 204. Anything else → 404. |
| `/open` headers | `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, `Sec-Fetch-User: ?1`. Missing or different → 403 with a page explaining that the link must be clicked from Onshape. |
| concurrency | one export at a time; a second `/open` while busy → 429 page |
| response | `Content-Type: text/html; charset=utf-8`, `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`, `X-Frame-Options: DENY`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`. All interpolated text is HTML-escaped. |

### Params (`request.py`)

| Param | Rule |
|---|---|
| `slicer` | must be a key that exists in the user's config |
| `d`, `wvid`, `e` | `^[0-9a-f]{24}$` |
| `wv` | one of `w`, `v`, `m` |
| `p` | optional; `^[A-Za-z0-9_+\-]{1,32}$` (Onshape part IDs look like `JHD`) |
| `c` | optional; ≤ 2048 chars after decoding. The literal `{$configuration}` (sent when the Part Studio has no configurations) means default → `""`. Passed to Onshape only as a query param, never used in paths, filenames (hash it) or commands. |
| `fmt` | optional; `stl` / `3mf` / `step` |
| Onshape-added | `companyId`, `sessionCompanyId`, `server`, `userId`, `clientId`, `locale`, `theme`: allowed and ignored. **`server` is never used as the API host.** |
| anything else | reject unknown params, so typos and injection attempts fail loudly |
| repeated params | reject (e.g. two `d=`) |

Also:

- Any other unresolved placeholder like `{$partId}` means the extension context was wrong. Reject it with a clear message.
- The Onshape host comes only from config (default `https://cad.onshape.com`, must match `https://*.onshape.com`).
- **Redirects:** the STL export answers 307 to another Onshape host (e.g. `cad-usw2.onshape.com`). Follow it by hand and re-attach auth only if the target is `https://<sub>.onshape.com`; refuse anything else.
- Slicer argv comes only from config. The only thing interpolated is the file path we created.
- Worst case for a malicious link: it opens one of your own parts in your slicer. Keep it that way.

## Service (Linux, local mode)

`os2slice install-service` writes `~/.config/systemd/user/os2slice.service`:

```ini
[Unit]
Description=os2slice: Onshape → slicer listener
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=<abs path to os2slice> serve
Restart=on-failure

[Install]
WantedBy=graphical-session.target
```

then runs `systemctl --user daemon-reload` and `systemctl --user enable --now os2slice.service`. Tying it to `graphical-session.target` gives it `WAYLAND_DISPLAY`/`DISPLAY`/`DBUS_SESSION_BUS_ADDRESS`, which the slicers and `notify-send` need (verified present on Josh's KDE Wayland session).

## Failure handling

The service has no terminal, so every error path in `/open` must do all of these:

1. Write the traceback to the log.
2. Send a notification with a one-line cause and the fix, e.g. "No API keys; run `os2slice setup-keys`" or "OrcaSlicer not found at /opt/…; edit config.toml".
3. Return the same message in the HTML result page (with a non-200 status).

CLI exit codes (`handle`, `send`, `print`): 0 ok, 1 config/unexpected, 2 bad request, 3 auth, 4 Onshape API error, 5 slicer launch error, 6 BamBuddy error. `/open` maps them to HTTP status: bad request 400, auth 500, Onshape API error 502, slicer launch error 500.
