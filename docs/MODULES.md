# Modules: slicers, targets, printers

Decided 2026-10-01 (D-27). os2slice is becoming a general CAD → print broker: the part
comes from Onshape, a **slicer module** turns it into a print file, and a **target
module** hands that file to a printer or a print-farm service. BamBuddy, which did
both, becomes one slicer module and one target module among several. Slicing can
therefore move out of BamBuddy without changing anything else.

```
Onshape ──export──► orient (orientation.py) ──► SliceInput ──► Slicer.slice() ──► SliceOutput
                                                                                      │
                     panel / page / CLI  ◄── Submission ◄── Target.submit() ◄─────────┘
```

The contract is `src/os2slice/modules/base.py`. Read it before touching a module.

## Module kinds

| kind | role | technology | makes / accepts | status |
|---|---|---|---|---|
| `bambuddy` | slicer + target | fdm | makes `gcode.3mf`; accepts `gcode.3mf` | today's path, wrapped |
| `bambu-studio-api` | slicer | fdm | `gcode.3mf`, `gcode` | sidecar on the NUC, port 3001 |
| `orca-slicer-api` | slicer | fdm | `gcode.3mf`, `gcode` | same REST shape as above |
| `prusaslicer-cli` | slicer | fdm | `gcode`, `bgcode` | subprocess, later |
| `moonraker` | target | fdm | `gcode` | Klipper |
| `prusalink` | target | fdm | `gcode`, `bgcode` | Prusa MK4 / XL / Core One / MINI |
| `octoprint` | target | fdm | `gcode` | later |
| `preform-server` | slicer + target (pair only with itself) | sla | `form` | Formlabs Form 4, issue #2 |
| `desktop` | hand-off | any | n/a | the local mode (`[slicers.*]` with `argv`), unchanged |

A slicer and a target pair when the slicer makes a medium the target accepts and the
technologies match; `pairs_only_with` restricts further (PreForm). Config validation
refuses a printer whose pair can't work.

## Config

```toml
[slicers.studio]                 # server-side module: has `kind`
kind = "bambu-studio-api"
url = "http://172.30.32.1:3001"

[slicers.orca]                   # desktop hand-off: has `argv`, no `kind` (kind = "desktop")
argv = ["flatpak", "run", "--file-forwarding", "com.orcaslicer.OrcaSlicer", "@@", "{file}", "@@"]

[targets.farm]
kind = "bambuddy"
url = "http://172.30.32.1:8000"
folder = "Onshape"
manual_start = true
public_url = "https://print.example.duckdns.org:8000"
# api key: secret store entry "targets.farm.api_key" (never in this file)

[targets.farm.models."X1C"]      # defaults for discovered printers, by model
slicer = "studio"
profiles = { printer = "Bambu Lab X1 Carbon 0.4 nozzle", process = "0.20mm Standard @BBL X1C", filament = "Generic PLA @BBL X1C" }
bed_type = "Textured PEI Plate"

[targets.voron]
kind = "moonraker"
url = "http://voron.lan:7125"
# api key: "targets.voron.api_key" (optional; Moonraker trusts LAN ranges by default)

[printers.voron]                 # a hand-configured printer (non-discovering target)
target = "voron"
slicer = "orca"
model = "Voron 2.4 350"
bed_mm = [350, 350]
profiles = { printer = "Voron 2.4 350 0.4 nozzle", process = "0.20mm Standard @Voron", filament = "Generic PLA @System" }
materials = ["PLA black", "PETG grey"]   # when the target can't report what's loaded

[printers."X1C_01"]              # optional overrides for a discovered printer, by name
slicer = "orca"
```

Secrets: every `type = "secret"` field is looked up as `<section>.<name>.<key>` in the
secret store (`auth.get_secret`): keyring on a desktop, the add-on's options or a 0600
file in the state dir on the NUC, `OS2SLICE_SECRET_<SECTION>_<NAME>_<KEY>` in the
environment for dev. The config page writes secrets to the store and never shows them.

Compatibility: the old `[bambuddy]` table (with `[bambuddy.presets.<model>]`) is read
as `[targets.bambuddy]` + `[slicers.bambuddy]` with per-model defaults, so existing
configs and the add-on keep working until they're migrated. `[print_defaults]`,
`[onshape]`, `[server]`, `[export]`, `[web_studio]` are unchanged.

## What the core does with a printer

1. `printers()` of every configured target → one list for the printer menu, each with
   `technology`, which selects the settings schema (FDM today; SLA later), the bed for
   the preview, and `ui_url` for the "watch it" link.
2. `status()` for the chosen printer → state line and loaded `materials` for the filament
   menu. A target that can't tell returns none, and the menu falls back to the printer's
   configured `materials`.
3. On Print: export + orient as today → `SliceInput` → the printer's slicer →
   `SliceOutput` → the printer's target `submit(start=False)` unless the target is
   configured to start (BamBuddy `manual_start = false`). The job page shows the
   `Submission`.
4. Multi-material (D-20) and copies (D-25) stay in the core: the core assembles the
   parts and offsets; a slicer that wants a 3MF builds it with `threemf.build_3mf`
   (Bambu/Orca), one that wants STL per part gets `parts` as is.

## Module checklist

- One file per module in `src/os2slice/modules/`, registered in `registry.py`.
- `spec` with every config field, so the config page and `doctor` need no module code.
- Only the configured URL is called; nothing from a request. Timeouts on every call.
- `ModuleError` with the service's own reason text; never swallow it.
- A fake transport in `tests/fakes_<module>.py` built from recorded shapes, and a
  `docs/<MODULE>_API.md` with what was verified live (✅) versus read from docs.
- Live tests behind `OS2SLICE_LIVE_<MODULE>_URL`; printing behind an explicit flag only.
