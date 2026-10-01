# Bambu Studio (web)

The real Bambu Studio desktop app, running on this machine and shown in a browser tab at
`https://<duckdns name>:3443` (host port; the container listens on 3001). **One shared session**: everyone who opens it sees and
controls the same window. The os2slice panel shows whether someone is in it and offers
"open on this computer" instead.

- **First start:** open the page and finish Bambu Studio's setup wizard (region, printers,
  filaments), then restart this add-on once so files from os2slice join the open window.
- **Files from os2slice** arrive via `/share/os2slice/inbox`, are opened automatically, and
  are kept in this add-on's config folder (`os2slice-files/`) for 7 days.
- **Password** (optional): basic auth for user `bambu`. It keeps casual visitors out; it is
  not internet-grade security. Don't expose port 3443 outside the LAN.
- HTTPS uses the Duck DNS certificate from `/ssl` (copied in at start).
- Runs **without AppArmor confinement**: Docker's default profile makes Bambu Studio abort at
  start on Home Assistant OS. If Bambu Studio's window is closed or crashes, it's restarted
  after about 20 s. Its output goes to `/share/os2slice/web-studio-app.log`.
- Includes a small preloaded library that fixes Bambu Studio's single-instance D-Bus path on
  Linux, so parts sent from Onshape open in the running window (see `shim/`).
- Trusts BamBuddy's virtual printers: at start it appends BamBuddy's virtual-printer CA
  (from `bambuddy_url`) to Bambu Studio's `printer.cer`. Add virtual printers in Bambu
  Studio by IP (discovery doesn't reach the add-on), and use **Send**, not Print.
