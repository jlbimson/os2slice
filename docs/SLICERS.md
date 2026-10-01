# Launching slicers

This page is about the desktop hand-off (local mode, `[slicers.<key>]` with `argv`): os2slice exports the part and opens it in a slicer on your own machine. Slicing on the server, for printing, is done by slicer modules (`[slicers.<key>]` with `kind`); see [`MODULES.md`](MODULES.md) and [`SLICERAPI_API.md`](SLICERAPI_API.md).

Every desktop slicer is launched as `argv + [file]` from config. No shell.

## Linux

Each slicer may be installed as a native package, an AppImage, or a Flatpak. `os2slice doctor` should detect which one and suggest a config entry.

| Slicer | Typical native / AppImage | Flatpak ID (verify with `flatpak list`) | Notes |
|---|---|---|---|
| OrcaSlicer | `~/Applications/OrcaSlicer*.AppImage`, `/usr/bin/orca-slicer` | `com.orcaslicer.OrcaSlicer` (older installs: `io.github.softfever.OrcaSlicer`) | Preferences has a single-instance option so new files join the open window. |
| Bambu Studio | `~/Applications/Bambu_Studio*.AppImage` | `com.bambulab.BambuStudio` | Same single-instance preference as Orca. |
| PrusaSlicer | `/usr/bin/prusa-slicer`, AppImage | `com.prusa3d.PrusaSlicer` | Supports the `--single-instance` CLI flag. |
| PreForm | **no native Linux build that I know of** | none | Windows/macOS only. On Linux, either skip it or configure a Wine command. See D-2. |

### Flatpak file access

Flatpak apps are sandboxed and may not see `~/OnshapeExports`. Options, best first:

1. `flatpak run --file-forwarding <APP_ID> @@ <file> @@`: the portal grants access to just that file. Use this as the default for flatpak entries.
2. Grant the folder once: `flatpak override --user --filesystem=~/OnshapeExports <APP_ID>`.

### AppImages

Launch the `.AppImage` directly. It must be executable. Config should hold an absolute path; `doctor` should warn if a glob matches several versions.

## Windows / macOS (Phase 4)

- Windows: `C:\Program Files\OrcaSlicer\orca-slicer.exe <file>`, `...\Bambu Studio\bambu-studio.exe`, `...\Prusa3D\PrusaSlicer\prusa-slicer.exe`, `...\PreForm\PreForm.exe <file> --silentRepair --modelUnits mm`.
- macOS: `open -a OrcaSlicer <file>` (and similar for the others); PreForm binary at `PreForm.app/Contents/MacOS/PreForm`.
- PreForm CLI reference: https://formlabs.com/support/Launching-PreForm-with-Command-Line-Options/ (`--silentRepair`, `--ignoreRepair`, `--modelUnits mm|in`, `--autoSetup`, `--printer`, `--upload`).

## Josh's machine

Probed 2026-09-30 (CachyOS/Arch, Python 3.14, Wine `wine-cachyos` 10.0). Tested by launching each with a 20 mm test cube STL.

| Slicer | Install type | Working argv |
|---|---|---|
| OrcaSlicer | Flatpak `com.orcaslicer.OrcaSlicer` (system) | `["flatpak", "run", "--file-forwarding", "com.orcaslicer.OrcaSlicer", "@@", "{file}", "@@"]`. The app received the portal path `/run/user/1000/doc/<id>/test_cube.stl`. |
| Bambu Studio | native (`bambustudio-bin` 2.8.2 from AUR; `/usr/bin/bambu-studio` is a wrapper for `/opt/bambustudio-bin/AppRun`) | `["/usr/bin/bambu-studio", "{file}"]`. The process starts with the file argument. |
| PrusaSlicer | Flatpak `com.prusa3d.PrusaSlicer` (system) | `["flatpak", "run", "--file-forwarding", "com.prusa3d.PrusaSlicer", "--single-instance", "@@", "{file}", "@@"]`. Loads the file. |
| PreForm | Wine, prefix `~/.wine`, PreForm 3.62.1.648 | **Not supported on Linux** (D-2). The launcher started under Wine, but the part did not open. |

Notes:

- Both Flatpaks already have `filesystems=home`, so a plain `~/OnshapeExports/...` path would also work. `--file-forwarding` is still the default because it also works for sandboxes without home access.
- Launching Orca and Prusa with `--file-forwarding` for the **same file at the same instant** failed for Prusa ("No such file: /run/user/1000/doc/..."). Launched alone, it worked. One right-click launches one slicer, so this doesn't matter in practice.
- PrusaSlicer's `--single-instance` goes before the first `@@`.
