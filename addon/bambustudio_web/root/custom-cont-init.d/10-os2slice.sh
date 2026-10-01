#!/usr/bin/with-contenv bash
# Runs as root before the desktop starts (linuxserver custom init hook).
set -u

# Trusted HTTPS: use the Duck DNS certificate instead of the image's self-signed one.
if [ -f /ssl/fullchain.pem ] && [ -f /ssl/privkey.pem ]; then
  mkdir -p /config/ssl
  cp /ssl/fullchain.pem /config/ssl/cert.pem
  cp /ssl/privkey.pem /config/ssl/cert.key
  chmod 600 /config/ssl/cert.key
  chown -R abc:abc /config/ssl
fi

# Optional basic auth from the add-on's Configuration tab. (With Docker Compose there's no
# /data/options.json: the image's own PASSWORD/CUSTOM_USER variables do this instead.)
password=$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("password") or "")' 2>/dev/null)
# (The image's init-nginx does this only when PASSWORD is set at container start, which
# an add-on can't do; nginx itself starts after this hook, so mirror what it does.)
if [ -n "${password}" ]; then
  printf 'bambu:%s\n' "$(openssl passwd -apr1 "${password}")" > /etc/nginx/.htpasswd
  sed -i 's/#//g' /etc/nginx/sites-available/default
fi

# Files handed over by os2slice, and where the session keeps them.
mkdir -p /share/os2slice/inbox /config/os2slice-files
chown abc:abc /config/os2slice-files
# os2slice writes here; with Docker Compose it runs as uid 1000, like abc (PUID=1000).
chown abc:abc /share/os2slice/inbox

# One Bambu Studio window: later launches hand their file to it.
/usr/local/bin/os2slice-inbox --set-single-instance || true

# Trust BamBuddy's virtual printers (its CA appended to Bambu Studio's printer.cer).
bambuddy_url=$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("bambuddy_url") or "")' 2>/dev/null)
bambuddy_url="${bambuddy_url:-${BAMBUDDY_URL:-}}"  # Docker Compose: from the environment
/usr/local/bin/os2slice-trust-bambuddy "${bambuddy_url}" || true
