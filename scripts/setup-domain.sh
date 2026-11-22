#!/usr/bin/env bash
set -euo pipefail

# Configure http://pokeclock.local (port 80 -> 127.0.0.1:8765) + /etc/hosts + UFW LAN rules.
# Usage:
#   sudo ./scripts/setup-domain.sh [domain] [esp_ip]
#   sudo ./scripts/setup-domain.sh --uninstall

DOMAIN="${1:-pokeclock.local}"
ESP_IP="${2:-192.168.18.26}"
HUB_PORT="${POKE_HUB_PORT:-8765}"
HOSTS_FILE="/etc/hosts"
MARKER_START="# --- POKECLOCK LOCAL DOMAIN ---"
MARKER_END="# --- END POKECLOCK LOCAL DOMAIN ---"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Ejecuta con sudo: sudo $0 $*" >&2
  exit 1
fi

remove_hosts_block() {
  if grep -qF "${MARKER_START}" "${HOSTS_FILE}"; then
    local tmp
    tmp="$(mktemp)"
    awk -v s="${MARKER_START}" -v e="${MARKER_END}" '
      $0 == s { skip = 1; next }
      $0 == e { skip = 0; next }
      !skip { print }
    ' "${HOSTS_FILE}" > "${tmp}"
    cat "${tmp}" > "${HOSTS_FILE}"
    rm -f "${tmp}"
  fi
}

if [[ "${DOMAIN}" == "--uninstall" ]]; then
  echo "Desinstalando configuración de dominio local PokeClock..."
  systemctl disable --now pokeclock-http.socket pokeclock-http.service 2>/dev/null || true
  rm -f /etc/systemd/system/pokeclock-http.socket /etc/systemd/system/pokeclock-http.service
  systemctl daemon-reload
  remove_hosts_block
  echo "Listo. Configuración revertida."
  exit 0
fi

# Strip trailing dot / ensure .local if no dot
DOMAIN="${DOMAIN%.}"
if [[ "${DOMAIN}" != *.* ]]; then
  DOMAIN="${DOMAIN}.local"
fi

if [[ ! -x /usr/lib/systemd/systemd-socket-proxyd ]]; then
  echo "No se encontró /usr/lib/systemd/systemd-socket-proxyd" >&2
  exit 1
fi

echo "==> 1/4 Configurando proxy transparente de puerto 80 -> 127.0.0.1:${HUB_PORT}..."
cat > /etc/systemd/system/pokeclock-http.socket <<'EOF'
[Unit]
Description=PokeClock HTTP Port 80 Socket

[Socket]
ListenStream=80
FreeBind=true

[Install]
WantedBy=sockets.target
EOF

cat > /etc/systemd/system/pokeclock-http.service <<EOF
[Unit]
Description=PokeClock HTTP Proxy (80 -> 127.0.0.1:${HUB_PORT})
Requires=pokeclock-http.socket
After=pokeclock-http.socket

[Service]
ExecStart=/usr/lib/systemd/systemd-socket-proxyd 127.0.0.1:${HUB_PORT}
Restart=on-failure
RestartSec=1
PrivateTmp=yes
NoNewPrivileges=yes
EOF

systemctl daemon-reload
systemctl enable --now pokeclock-http.socket
systemctl try-restart pokeclock-http.service 2>/dev/null || true

echo "==> 2/4 Registrando ${DOMAIN} en ${HOSTS_FILE} (resolución local garantizada incluso si el ISP intercepta .local)..."
remove_hosts_block
cat >> "${HOSTS_FILE}" <<EOF

${MARKER_START}
127.0.0.1 ${DOMAIN}
${ESP_IP} poke-display.local pokedisplay.local
${MARKER_END}
EOF
resolvectl flush-caches 2>/dev/null || true

echo "==> 3/4 Asegurando Avahi mDNS activo y reglas UFW solo para red local..."
systemctl enable --now avahi-daemon.service >/dev/null 2>&1 || true

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  for subnet in 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12; do
    ufw allow from "${subnet}" to any port 80 proto tcp comment 'PokeClock HTTP LAN' >/dev/null
    ufw allow from "${subnet}" to any port "${HUB_PORT}" proto tcp comment 'PokeClock Hub LAN' >/dev/null
  done
  ufw allow 5353/udp comment 'Avahi mDNS' >/dev/null
  echo "    UFW actualizado (acceso LAN en puertos 80, ${HUB_PORT} y mDNS 5353/udp)."
fi

echo "==> 4/4 Verificando http://${DOMAIN}/api/status ..."
if curl -fsS --max-time 3 "http://${DOMAIN}/api/status" >/dev/null 2>&1; then
  echo ""
  echo "TODO LISTO!"
  echo "  • Desde tu PC o celular en el mismo Wi-Fi: http://${DOMAIN}"
  echo "  • Pantalla ESP8266 / OTA:                  http://pokedisplay.local/ota"
else
  echo "Proxy de puerto 80 instalado. Asegúrate de que 'systemctl --user status poke-clock.service' esté activo."
fi
