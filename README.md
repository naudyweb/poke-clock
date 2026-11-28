# Poké-Hub · Control PC-as-Brain para JUZIPi SD PRO

Controlador local en tu PC (API REST + Web UI + CLI `poke-ctl` + eventos D-Bus en tiempo real) + firmware minimalista receptor de frames en RAM para tu pantalla **JUZIPi SD PRO** (ESP8266 + ST7789 240×240).

## Arquitectura
- **Tu PC (`http://localhost:8765`)** es el cerebro: renderiza todas las pantallas Pokémon GBC (`240×240`), gestiona prioridades (`Aviso > Fijo > Spotify exclusivo > Rotación`), escucha Spotify vía D-Bus en <50 ms y lee tokens locales de Antigravity.
- **El ESP8266 (`http://192.168.18.26`)** corre **PokeDisplay RAM** (`firmware/`): recibe cada JPEG por `POST /frame` directo en RAM y lo pinta por SPI a 40 MHz en ~150 ms, **sin desgaste de memoria flash**.

## Comandos rápidos (`poke-ctl`)

```bash
poke-ctl status
poke-ctl show clock | weather | gemini | spotify
poke-ctl auto
poke-ctl notify "BUILD OK" "Tests 100% verdes" --pokemon pikachu --ttl 12
poke-ctl brightness 85
poke-ctl display on           # Encender la pantalla; también admite `off`
poke-ctl rotation 0          # 0, 1, 2 (180°), 3
poke-ctl toggle weather off
poke-ctl toggle spotify_priority on
```

## Panel Web Local y REST API
- Dominio local corto (mDNS automático + puerto 80): **`http://pokeclock.local`** (funciona en PC y celular en el mismo Wi-Fi).
  - Por defecto Poké-Hub publica `pokeclock.local` automáticamente vía Avahi (`http://pokeclock.local:8765`).
  - Para usarlo **sin número de puerto** (`http://pokeclock.local`), abrir el firewall LAN y evitar que el DNS del ISP bloquee `.local` en tu PC, ejecuta una sola vez:
    ```bash
    sudo ./scripts/setup-domain.sh
    ```
- También disponible siempre en **`http://localhost:8765/`** (o `http://192.168.18.27:8765/`).
- Endpoints JSON: `GET /api/status`, `GET /api/preview/<card>`, `POST /api/show`, `POST /api/auto`, `POST /api/notify`, `POST /api/config`, `POST /api/brightness`, `POST /api/display` (ej. `{"on": false}`), `POST /api/rotation`, `POST /api/refresh`.

## Cuota real de Claude Code

La tarjeta Claude lee los periodos oficiales `five_hour` y `seven_day` que Claude Code proporciona a su status line; los conteos de tokens de archivos de sesión no se usan como cuota. En `~/.claude/settings.json`, configura `statusLine` para ejecutar `python3 /ruta/absoluta/al/proyecto/scripts/claude_usage_statusline.py`. El status line guarda únicamente esos porcentajes, sus horarios de reinicio y la hora de actualización en `~/.cache/poke-hub/claude-rate-limits.json`, con permisos privados; no copia conversaciones ni credenciales.

Los datos aparecen después de que Claude Code proporcione una actualización real. Si no se recibe o la última actualización supera 24 horas, la tarjeta dice **sin datos reales** en vez de mostrar una estimación.

## Servicio automático (`systemd --user`)

Al detener `poke-clock.service`, incluido el apagado normal de la computadora, Poké-Hub consulta `/status` y solo ordena apagar la pantalla si identifica `PokeDisplay` con firmware `1.5.0` o posterior y estado `display_on`. El reloj mantiene Wi‑Fi activo; requiere un apagado ordenado (no un corte de energía).

```bash
systemctl --user status poke-clock.service
journalctl --user -u poke-clock.service -f
```

## Firmware OTA y recuperación de fábrica
- Actualización OTA integrada siempre disponible en **`http://192.168.18.26/ota`**.
- Si alguna vez cambia el Wi-Fi y no puede conectarse en 25 s, abre el punto de acceso de recuperación **`PokeDisplay-OTA`** (`http://192.168.4.1/ota`).
- El firmware original de fábrica está respaldado en `backup-firmware/SDP_V1.1.8_20260611_174037.bin` (puedes restaurarlo en cualquier momento desde `/ota`).
