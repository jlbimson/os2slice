#!/bin/sh
# Optional `duckdns` service (docs/DOCKER.md): points <name>.duckdns.org at this machine's
# LAN address and keeps a Let's Encrypt certificate for it in /certs, renewing daily.
# The DNS-01 challenge goes through Duck DNS, so nothing has to be reachable from outside.
set -eu
: "${DUCKDNS_DOMAIN:?set DUCKDNS_DOMAIN, e.g. print.duckdns.org}"
: "${DUCKDNS_TOKEN:?set DUCKDNS_TOKEN}"
: "${LAN_IP:?set LAN_IP, this machine's LAN address}"
: "${ACME_EMAIL:?set ACME_EMAIL for Let's Encrypt}"
sub="${DUCKDNS_DOMAIN%.duckdns.org}"
owner="${CERT_UID:-1000}"

while :; do
  # Duck DNS takes the token in the URL; -q keeps it out of the log.
  if wget -qO- "https://www.duckdns.org/update?domains=${sub}&token=${DUCKDNS_TOKEN}&ip=${LAN_IP}" \
      | grep -q '^OK'; then
    echo "duckdns: ${DUCKDNS_DOMAIN} -> ${LAN_IP}"
  else
    echo "duckdns: address update failed (check DUCKDNS_DOMAIN and DUCKDNS_TOKEN)" >&2
  fi
  # Gets the certificate the first time; afterwards renews it when 30 days are left.
  if /lego run --accept-tos --email "${ACME_EMAIL}" --dns duckdns \
      --domains "${DUCKDNS_DOMAIN}" --path /data --renew-days 30; then
    crt="$(find /data -name "${DUCKDNS_DOMAIN}.crt" | head -n 1)"
    if [ -n "${crt}" ] && [ -f "${crt%.crt}.key" ]; then
      install -m 644 -o "${owner}" -g "${owner}" "${crt}" /certs/fullchain.pem
      install -m 600 -o "${owner}" -g "${owner}" "${crt%.crt}.key" /certs/privkey.pem
      echo "certificate: /certs/fullchain.pem and /certs/privkey.pem updated"
    else
      echo "certificate: lego succeeded but its files weren't found under /data" >&2
    fi
  else
    echo "certificate: lego failed; retrying tomorrow" >&2
  fi
  sleep 86400
done
