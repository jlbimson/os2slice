# OrcaSlicer / Bambu Studio API sidecar notes

What `src/os2slice/modules/slicerapi.py` (kinds `orca-slicer-api`, `bambu-studio-api`) relies on.

Marks: **✅ verified** = called on 2026-10-01 against the sidecars running locally in
Docker on Josh's desktop (loopback only, never barnassistant); **📖 source** = read from the
upstream repository and not exercised. Nothing here has been run against the HA add-on on
barnassistant yet.

## Upstreams

| | afk | resolver |
|---|---|---|
| Repo | `afkfelix/orca-slicer-api` (main) | `maziggy/orca-slicer-api`, branch `bambuddy/profile-resolver` |
| Who uses it | the original | BamBuddy; packaged by `griffinmartin/ha-app-bambu-studio-api` ("Bambu Studio API" HA add-on, Bambu Studio AppImage, host port 3001) and by BamBuddy's own compose (`orca-slicer-api` on 3003, `bambu-studio-api` on 3001) |
| Image verified | `ghcr.io/afkfelix/orca-slicer-api:latest-orca2.4.2` (`sha256:2a06064d…`, OrcaSlicer 2.4.2) | `ghcr.io/maziggy/bambu-studio-api:latest` (`sha256:eea0858b…`, Bambu Studio 02.08.02.61) |
| Container port | 3000 | 3000 |
| Auth | none (README: "No authentication"); CORS allows an `Authorization` header | same |

The kind (`orca-slicer-api` / `bambu-studio-api`) picks the label and default port only. The
**flavour** (afk or resolver) is what changes the request shape, and the module probes it once
with `GET /profiles/bundled` (resolver: 200 with an object; afk: 400 "Invalid or missing
category") ✅. The HA add-on and BamBuddy's compose are both the resolver flavour.

## Endpoints

| Request | afk | resolver |
|---|---|---|
| `GET /health` | ✅ 200 `{status: "healthy", timestamp, checks: {orcaslicer: {available, version: "2.4.2"}, systemProfilePath: {accessible}}}`, 503 when unhealthy | ✅ same shape with `dataPath` instead of `systemProfilePath`. Without a writable `/app/data` (`DATA_PATH`) it answers **503 unhealthy** although slicing works; `check()` passes that case (slicer available, only `dataPath` failing) with a warning. `version` is `"unknown"` on the Bambu Studio build (the regex looks for `OrcaSlicer-`), a known cosmetic issue |
| `GET /profiles/bundled` | ✅ 400 | ✅ `{printer: [{name, base_id}], process: [{name, base_id, compatible_printers}], filament: [{name, base_id, compatible_printers, filament_type, filament_colour}]}` (system presets, instantiable only, sorted) |
| `GET /profiles/{printers\|presets\|filaments}` | ✅ `["name", …]`, all system profiles | ✅ `[]`: **stored user profiles** (`DATA_PATH/<category>/<name>.json`, names `[A-Za-z0-9]+`), not system ones |
| `GET /profiles/{category}/{name}` | ✅ the resolved profile JSON; ✅ 404 `{message: "Profile \"…\" not found in category \"…\"."}` (used to explain a failure) | 📖 a stored user profile |
| `POST /profiles/resolve` `{category: machine\|process\|filament, profile}` | – | 📖 `{profile: <flattened>}` |
| `POST /profiles/bundle`, `GET /profiles/bundles[/id]` | – | 📖 `.bbscfg` preset bundles (not used) |
| `POST /slice` (multipart) | ✅ | ✅ |
| `GET /slice/progress/{requestId}` | ✅ 404 HTML (no such route) | ✅ 200 `{stage, total_percent, plate_percent, plate_index, plate_count, updated_at}` while a `POST /slice` that sent `requestId` runs (and 30 s after); 404 `{"error": "not_found"}` otherwise |
| `POST /slice-async` (multipart) | ✅ 202 `{requestId, status: "pending", statusUrl: "/slice-async/<id>"}` | ✅ same, but see the filament bug below |
| `GET /slice-async/{id}` | ✅ `{requestId, status: pending\|processing}`; `{…, status: "failed", message}`; `{…, status: "completed", metadata: {printTime, filamentUsedG, filamentUsedMm}, downloadUrl}`. Unknown id: 404 `{message: "Slice request not found"}` | ✅ same |
| `GET /slice-async/{id}/result` | ✅ the file, with the metadata headers | 📖 same |
| `DELETE /slice-async/{id}` | 📖 204; 400 while running. Jobs are kept in memory for `ASYNC_SLICE_RETENTION_MS` (1 h) otherwise | 📖 same |

## `POST /slice` and `/slice-async` form

Multipart, all text fields are strings.

| Field | Meaning |
|---|---|
| `file` | The model. ✅ The part's content type must be `model/stl` or `model/3mf` (and `.stl`/`.3mf` extension) or it's refused with 400 "Invalid file type…". afk also takes STEP (OrcaSlicer ≤ 2.3.0 only); resolver refuses STEP. |
| `printerProfile`, `presetProfile`, `filamentProfile` | ✅ Profile JSON uploads, content type `application/json`, `.json` filename. afk uses the uploaded printer/process **only when both are sent**. Filaments: afk 1; resolver **up to 16 on `/slice`**, joined in order for `--load-filaments`. |
| `printer`, `preset`, `filament` | 📖 Names: system profiles on afk, **stored user profiles** on resolver. Not used by os2slice. Resolver also has `filaments` (comma/semicolon list) and `bundle` + `printerName`/`processName`/`filamentName(s)`. |
| `resolveProfileInheritance` | afk only: `"true"` merges each upload over the system profile its `inherits` names (one level; system profiles are stored already flattened). ✅ Needed for os2slice's stubs. Resolver flattens any upload with `inherits` automatically. |
| `exportType` | ✅ `"3mf"` → a `.gcode.3mf` (Bambu-style 3MF with `Metadata/plate_1.gcode`, `slice_info.config`, `project_settings.config`); omitted → plain `.gcode`. |
| `plate` | 📖 Plate to slice (default `"1"`; `"0"` = all, which returns a ZIP of G-codes when there are several). os2slice sends `"1"`. |
| `arrange`, `orient` | 📖 `--arrange 1` / `--orient 1`. **Upstream tests `!== undefined` then truthiness, so the string `"false"` turns them on**: send `"true"` or omit (BamBuddy's client documents the same). |
| `bedType` | ✅ `--curr-bed-type` (e.g. `"Textured PEI Plate"`; appeared as `curr_bed_type` in the output). |
| `multicolorOnePlate` | 📖 `--allow-multicolor-oneplate`. Not sent. |
| `requestId` | resolver only: ✅ publishes progress at `/slice/progress/<id>`. |

Response (`/slice`, `/slice-async/{id}/result`): ✅ the file as `application/octet-stream`
with `X-Print-Time-Seconds`, `X-Filament-Used-g`, `X-Filament-Used-mm`. ✅ **The gram header is
the first filament only on multi-filament slices** (the wrapper's regex takes the first number
of `; total filament weight [g] : 5.64,5.31`; `slice_info.config` said 10.95 g), so the module
reads `Metadata/slice_info.config` (`prediction`, `weight`) first, then the G-code header
(summing the per-filament list), then the HTTP headers.

## Profiles as stubs

Both flavours accept a minimal upload that inherits a system preset (BamBuddy sends the same
for its "standard" presets) ✅:

```json
{"name": "0.20mm Standard @BBL A1M", "inherits": "0.20mm Standard @BBL A1M",
 "from": "system", "type": "process", "wall_loops": "3", "sparse_infill_density": "25%"}
```

- `type` must be `machine` / `process` / `filament` (afk refuses others), `from` must be set
  (afk), and `name` should be the system name (compatibility checks compare names).
- **Process overrides** are just keys in the process stub; child keys win over the parent.
  ✅ Both flavours: `wall_loops 3`, `sparse_infill_density 25%`, `brim_type outer_only`,
  `top_shell_layers 6` showed up in the output's `project_settings.config`. Values are written
  as strings (`"1"` for true), as presets store them. Key names are the Bambu Studio ones,
  which OrcaSlicer shares for everything os2slice sends (`settings.process_overrides()`).
- **Filament colour**: `"filament_colour": ["#RRGGBB"]` in the filament stub. ✅ resolver
  (colours red/blue appeared in `slice_info.config` and `project_settings.config`).
- The resolver strips `"-1"` and `""` values from uploads and rewrites `from: "User"` to
  `system` (📖).

## Async versus sync

- afk: `/slice-async` works with uploads ✅, so the module uses it: POST, poll every second,
  download, DELETE.
- resolver: **`/slice-async` drops uploaded filament profiles** (📖 its async route builds
  `{filament: …}` while the slicing service reads `filaments[]`, and it accepts only one
  `filamentProfile`). The module therefore uses `POST /slice` with a `requestId` (held open in
  a worker thread) and polls `/slice/progress/<id>` for the progress line, as BamBuddy does ✅.
- Timeouts: `timeout_s` (default 900) bounds the whole slice on both paths.

## Errors

✅ Error bodies are `{"message": "…"}`; the resolver adds `"details"` (the CLI's stdout/stderr,
mostly `[trace]` lines; the module keeps only the `[error]` lines). Examples:

- Unknown profile in a stub: afk 500 `{"message": "Failed to prepare slicing"}` (cause hidden;
  the module then looks the names up with `GET /profiles/{category}/{name}` and names the
  missing one); resolver: the dangling `inherits` is dropped and the CLI fails with 500
  `"Slicing failed with error from slicer: The selected printer is not compatible with the
  process preset in the 3mf."` + details `run 3002: process not compatible with printer.`
- Async failures arrive as `status: "failed"` with the same `message`.
- Multi-filament plate without a prime tower position: 500 `"Found G-code outside of the
  printable area…"` (A1 Mini) or `"Found G-code in unprintable area of multi-extruder
  printers…"` + `Invalid T command` (H2D). ✅ Setting `wipe_tower_x`/`wipe_tower_y` in the
  process stub to the core's `tower_spots()` first choice fixed both (A1 Mini 2 filaments,
  H2D 2 filaments on both nozzles, `filament_maps "1 2"`). The module places the tower
  with `bambu_project.tower_spots()` and retries the next spot on these errors through
  `bambu_project.with_tower_retries()`, the same helper BamBuddy's module uses (D-30);
  extra keys from the core arrive as `SliceInput.process_overrides`.
- Uploads over the size cap (resolver: `MAX_MODEL_UPLOAD_MB`, default 512) answer 413 📖.

## Verified slices (2026-10-01, local Docker, slice only)

`scripts/spike_JHD.stl` (50.8 × 25.4 × 6.35 mm), A1 Mini presets (`Bambu Lab A1 mini 0.4
nozzle` / `0.20mm Standard @BBL A1M` / `Bambu PLA Basic @BBL A1M`), 3 walls, 25 %, brim:

| Sidecar | Input | Media | Result |
|---|---|---|---|
| afk (Orca 2.4.2) | STL | gcode.3mf | 972 s, 5.9 g, 32 layers, inside the plate |
| afk | 3MF, 4 copies | gcode.3mf | 4 objects, 2539 s, 23.08 g |
| afk | STL | gcode | 972 s, 5.9 g, 32 layers |
| resolver (Bambu Studio 02.08.02.61) | STL | gcode.3mf | 905 s, 5.88 g, 32 layers |
| resolver | 3MF, 4 copies | gcode.3mf | 4 objects, 2281 s, 22.98 g |
| resolver | STL | gcode | 905 s, 5.88 g |
| resolver | 3MF, 2 parts, 2 colours, A1 Mini, tower set | gcode.3mf | 5084 s, 13.7 g |
| resolver | 3MF, 2 parts, H2D pinned left/right, tower set | gcode.3mf | 1662 s, 10.95 g, two nozzles |

`tests/test_slicerapi.py::test_live_slice_spike_part` (`OS2SLICE_LIVE_SLICERAPI_URL`) passed
against both containers.
