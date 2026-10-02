#!/usr/bin/with-contenv bash
# Runs as root before the desktop starts (linuxserver custom init hook).
set -u

# Files handed over by os2slice, and where the session keeps them. os2slice writes the
# inbox; with Docker Compose it runs as uid 1000, like abc (PUID=1000).
mkdir -p "${OS2SLICE_INBOX}" /config/os2slice-files
chown abc:abc "${OS2SLICE_INBOX}" /config/os2slice-files

# One OrcaSlicer window: later launches hand their file to it.
/usr/local/bin/os2slice-inbox --set-single-instance || true
