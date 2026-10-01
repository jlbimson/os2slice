"""Pluggable slicer and target modules (docs/MODULES.md, D-27).

`base` holds the contract. `registry` maps `kind` strings to module classes. Each
module lives in its own file and depends only on `base`, `os2slice.settings`,
`os2slice.errors`, `os2slice.files` and httpx.
"""
