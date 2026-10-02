# Printer APIs: Moonraker and PrusaLink

What the `moonraker` and `prusalink` target modules (`src/os2slice/modules/`) rely on.
**Everything here is read from the vendors' documentation (📖). Nothing is verified
against a live printer yet (no ✅).** When a printer is tested, mark each line ✅ or
correct it, and update `tests/fakes_printers.py` to the recorded shapes.

Sources, read 2026-10-01:

- Moonraker: <https://moonraker.readthedocs.io/en/latest/external_api/> (the markdown
  under `docs/external_api/` in Arksine/moonraker: `introduction`, `server`, `printer`,
  `file_manager`, `authorization`, `integrations` (Spoolman), and `printer_objects`).
- PrusaLink: `spec/openapi.yaml` in prusa3d/Prusa-Link-Web (version `1.0.0-draft`), and
  <https://help.prusa3d.com/article/prusalink-and-prusa-connect-explained_302608>
  (overview only; it says nothing about authentication).

Both modules call only the configured `url`, with a 10 s timeout on reads and a long
write timeout on uploads, never follow redirects, and raise `ModuleError` carrying the
service's own message. Filenames from `SliceOutput.filename` are cut down to
`[A-Za-z0-9._-]` (no leading dots, at most 80 characters) before they go into a form
field or URL path; the configured `folder` gets the same filter.

## Moonraker (Klipper), kind `moonraker`

### Envelope and auth

- 📖 Successful HTTP replies are wrapped: `{"result": <data>}`. The module unwraps
  `result` when present (and accepts a bare object, for older versions).
- 📖 Errors: `{"error": {"code": <int>, "message": "<text>"}}` with the HTTP status. The
  module reports `message`.
- 📖 Auth: trusted clients (by IP range, `[authorization] trusted_clients`) need nothing;
  others send `X-Api-Key: <key>` (or a JWT). The module sends `X-Api-Key` only when
  `api_key` is set in the secret store (`targets.<name>.api_key`). 401/403 become a
  `ModuleError` whose fix says to set the key or add the os2slice host to
  `trusted_clients`. Not `AuthError`: in the server that means "Onshape sign-in".

### Endpoints

| Use | Request | Fields read |
|---|---|---|
| `check()` | 📖 `GET /server/info` | `klippy_connected`, `klippy_state` (`ready`, `startup`, `error`, `shutdown`, `disconnected`), `moonraker_version` |
| `check()`, `printers()` | 📖 `GET /printer/info` | `state_message`, `hostname` (fills `model` when the printer entry has none), `software_version` (Klipper) |
| `status()` | 📖 `GET /printer/objects/query?webhooks&print_stats&virtual_sdcard&extruder&heater_bed&toolhead` (bare keys = all attributes) | `status.webhooks.state`/`state_message`; `status.print_stats.state` (`standby`, `printing`, `paused`, `complete`, `error`, `cancelled`), `.message`; `status.virtual_sdcard.progress` (0.0–1.0), `.is_active`; `status.extruder` and `status.heater_bed` `.temperature`/`.target` |
| `status()` materials | 📖 `GET /server/spoolman/spool_id` | `spool_id` (int, or `null` = no active spool) |
| | 📖 `POST /server/spoolman/proxy` `{"use_v2_response": true, "request_method": "GET", "path": "/v1/spool/<id>"}` | v2: `{"response": <spool>, "error": null \| {status_code, message}}`; spool `.filament.name`, `.filament.vendor.name`, `.filament.material`, `.filament.color_hex` (`RRGGBB`, no `#`) |
| `submit()` | 📖 `POST /server/files/upload`, `multipart/form-data`: `file` (the bytes, filename = the unique name), `root=gcodes`, `path=<folder>` (created if missing), and `print=true` **only when starting** | 201; `item.path` (relative to the root), `print_started`, `print_queued` |

Status mapping:

- Klippy not `ready` (webhooks state, or `/server/info` when the object query errors
  because Klippy is down): `state = "klipper <state>"`, `connected = ready = False`.
- Otherwise `connected = True`, `state` = `print_stats.state`, and `ready` when the state
  is `standby`, `complete` or `cancelled`, or `error` with no active file
  (`virtual_sdcard.is_active` false).
- `detail` while printing/paused: `"42%, nozzle 215/215 °C, bed 60/60 °C"`; in `error`,
  `print_stats.message`.
- Moonraker unreachable: `state = "offline"`, not an exception, so the printer menu
  still renders. HTTP errors other than Klippy-down raise.
- `materials`: the active Spoolman spool when `spoolman = true`, else empty. A Spoolman
  failure is logged and gives no materials; it never fails `status()`.

Filenames: Moonraker overwrites an upload of the same name, so every upload is
`<stem>_<YYYYmmdd-HHMMSS>.gcode`.

### D-13: waiting

`submit(start=False)` uploads **without** the `print` field, so Moonraker only stores the
file: `Submission(state="waiting", detail="Uploaded to <folder>/<name>; start it from
Mainsail or Fluidd", url=ui_url)`. With `start=True` it sends `print=true`; the reply's
`print_started` gives `started`; `print_queued` (Moonraker's job queue is enabled) is
also reported as `started`, since the queue will start it on its own; neither (printer
busy) is reported as `waiting`. The `job_queue` API isn't used.

## PrusaLink, kind `prusalink`

Covers the xBuddy firmware (MK4/S, XL, Core One, MINI, MK3.5/3.9: storage `usb`) and
PrusaLink on a Raspberry Pi (MK3S: storage `local`).

### Auth

- 📖 The OpenAPI spec lists only HTTP **digest** auth (`securitySchemes.digestAuth`).
- The printer also shows an **API key** (Settings > Network > PrusaLink on xBuddy
  printers), which PrusaSlicer and other clients send as `X-Api-Key`. The module uses
  that, from the secret store (`targets.<name>.api_key`, required). This comes from
  general knowledge of the firmware, not from the spec: **verify first** on a real
  printer. Digest auth (user `maker` + password) is a later addition if the key header
  turns out not to be accepted on some firmware.
- 401/403 → `ModuleError("PrusaLink refused the API key: <reason>", fix=where to find it)`.
- 📖 Errors carry either `text/plain` (the raw message) or JSON `{code, title, text, url}`;
  the module reports `title: text` or the plain text.

### Endpoints

| Use | Request | Fields read |
|---|---|---|
| `check()` | 📖 `GET /api/version` | `api`, `text`, `firmware` (falls back to `server`/`version`), `capabilities.upload-by-put` (when explicitly `false`, `check()` fails) |
| `check()`, `printers()` | 📖 `GET /api/v1/info` | `name`, `hostname`, `nozzle_diameter`. The spec has **no model field**: `model` is filled from `model` if a firmware sends one, else `hostname`. The nozzle goes into `PrinterInfo.extra["nozzle_diameter"]`. |
| `status()` | 📖 `GET /api/v1/status` | `printer.state` (`IDLE`, `BUSY`, `PRINTING`, `PAUSED`, `FINISHED`, `STOPPED`, `ERROR`, `ATTENTION`, `READY`), `printer.temp_nozzle`/`target_nozzle`/`temp_bed`/`target_bed`, `printer.status_printer.ok`/`.message`; `job.progress` (percent), `job.time_remaining` (s) |
| `submit()` | 📖 `PUT /api/v1/files/{storage}/{folder}/{name}`, body = the file, headers `Content-Type: application/octet-stream`, `Content-Length`, `Overwrite: ?0`, `Print-After-Upload: ?1` when starting else `?0` (RFC 8941 booleans) | 201; 401, 404, 409 per the spec; 413 and 507 handled as well |

Status mapping: `state` = the PrusaLink word; `connected` unless the state is `ERROR`
or `status_printer.ok` is `false`; `ready` = connected and `IDLE`, `FINISHED`, `STOPPED`
or `READY`. `detail` while `PRINTING`/`PAUSED`: `"42%, 1 h 02 min left, nozzle 215/215 °C,
bed 60/60 °C"`; `ATTENTION`: "needs attention at the printer". Unreachable → `offline`.
`materials` is always empty: PrusaLink doesn't report the loaded filament, so the menu
uses the printer's configured `materials`.

Upload errors: **409** (a file of that name exists; `Overwrite: ?0` keeps it) → one retry
as `<stem>_<YYYYmmdd-HHMMSS>.<ext>`, then a `ModuleError`. **413** → "too large". **507**
→ "storage full". **404** → "is the USB drive in?". Others → `ModuleError` with the
printer's text. Not known from the spec (verify): whether `PUT` creates the `folder` when
it doesn't exist (if it 404s, set `folder = ""` or create it on the printer), and
whether a 409 can also mean "printer busy" when `Print-After-Upload: ?1`.

### D-13: waiting

`submit(start=False)` sends `Print-After-Upload: ?0` explicitly (the default, sent anyway
so nothing depends on it), so the file is only saved: `Submission(state="waiting",
detail="Saved on the printer's USB as <name>; start it from the printer's screen or
PrusaLink", url=<printer url>)`. `start=True` sends `?1` and reports `started`.

## Live tests

`tests/test_moonraker.py::test_live_moonraker_read_only` and
`tests/test_prusalink.py::test_live_prusalink_read_only` call only `check()` and
`status()`. Run with `OS2SLICE_LIVE_MOONRAKER_URL` (optional
`OS2SLICE_LIVE_MOONRAKER_KEY`) or `OS2SLICE_LIVE_PRUSALINK_URL` +
`OS2SLICE_LIVE_PRUSALINK_KEY` set, and `pytest -q -m live`. No test uploads to or
starts a real printer.


## Happy Hare (MMU) through Moonraker

✅ 2026-10-02, joshprint (Kalico, Happy Hare, 5 gates). `GET /printer/objects/query?…&mmu`
returns Happy Hare's `mmu` object when the printer has one (Moonraker leaves out objects
Klipper doesn't have). os2slice reads `enabled`, `ttg_map` (tool → gate), and per gate
`gate_material`, `gate_color` (`RRGGBB` or `RRGGBBAA`), `gate_filament_name`,
`gate_status` (0 = empty) and `gate_spool_id` (-1 = none). A spool's vendor comes from
Spoolman through `POST /server/spoolman/proxy` (`/v1/spool/<id>`). Happy Hare's own
material for a gate wins over its name: gate 1 was named "PolyLite™ ASA Blue" with
material PETG, and was matched to a PETG profile.
