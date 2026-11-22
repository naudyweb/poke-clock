"""Passive D-Bus desktop notification watcher with system DND awareness."""

from __future__ import annotations

import glob
import hashlib
import html
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Callable

try:
    import gi

    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gio, GLib  # type: ignore

    _HAS_GIO = True
except Exception:
    Gio = None  # type: ignore
    GLib = None  # type: ignore
    _HAS_GIO = False

logger = logging.getLogger(__name__)

VALID_POKEMON = frozenset(
    {
        "pikachu",
        "charmander",
        "squirtle",
        "bulbasaur",
        "gengar",
        "snorlax",
        "jigglypuff",
    }
)

IGNORED_APPS = frozenset({"poke-hub", "poke-ctl", "pokehub", "pokeclock"})

_GENERIC_APPS = frozenset({"", "notify-send", "desktop", "system", "notification", "unknown"})

_MAIL_KEYWORDS = (
    "thunderbird",
    "geary",
    "evolution",
    "mailspring",
    "kmail",
    "betterbird",
    "protonmail",
    "gmail",
    "outlook",
    "mail",
)

_MESSAGING_BADGES: tuple[tuple[str, str], ...] = (
    ("discord", "DISCORD"),
    ("telegram", "TELEGRAM"),
    ("slack", "SLACK"),
    ("whatsapp", "WHATSAPP"),
    ("signal", "SIGNAL"),
    ("element", "ELEMENT"),
    ("teams", "TEAMS"),
    ("messenger", "MESSENGER"),
)

_CALENDAR_KEYWORDS = (
    "calendar",
    "calendario",
    "reminder",
    "recordatorio",
    "alarm",
    "clock",
)

_DEV_BADGES: tuple[tuple[str, str], ...] = (
    ("antigravity", "ANTIGRAV"),
    ("visual studio code", "VSCODE"),
    ("vscode", "VSCODE"),
    ("opencode", "OPENCODE"),
    ("alacritty", "ALACRITTY"),
    ("ghostty", "GHOSTTY"),
    ("kitty", "KITTY"),
    ("github", "GITHUB"),
    ("gitlab", "GITLAB"),
    ("claude", "CLAUDE"),
)

_CRITICAL_APP_KEYWORDS = (
    "upower",
    "battery",
    "power",
    "security",
    "seguridad",
    "firewall",
    "gufw",
    "polkit",
)

_TEXT_ALERT_RE = re.compile(
    r"\b(?:bater[ií]a(?:\s+baja)?|low\s+battery|seguridad|security\s+alert|alerta|critical\s+error|intrusion|malware|disk\s+full|overheat)\b",
    re.IGNORECASE,
)
_TEXT_CALENDAR_RE = re.compile(
    r"\b(?:calendar|calendario|google\s+calendar|reminder|recordatorio|reuni[oó]n|meeting|cita|alarm|alarma)\b",
    re.IGNORECASE,
)
_TEXT_MAIL_RE = re.compile(
    r"\b(?:thunderbird|geary|evolution|mailspring|kmail|betterbird|protonmail|gmail|outlook|nuevo\s+correo|new\s+email)\b",
    re.IGNORECASE,
)
_TEXT_MSG_RE = re.compile(
    r"\b(discord|telegram|slack|whatsapp|signal|element|teams|messenger)\b",
    re.IGNORECASE,
)
_TEXT_DEV_RE = re.compile(
    r"\b(vscode|antigravity|github|gitlab|opencode|claude(?:\s+code)?)\b",
    re.IGNORECASE,
)

_BR_TAG_RE = re.compile(r"<\s*(?:br\s*/?|/\s*(?:p|div|li|tr))\s*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_DBUS_HEADER_RE = re.compile(r"^(?:method call|signal|method return|error)\s+(?:time=|sender=)")
_RE_STRING_START = re.compile(r'^\s+string "(.*)$')
_RE_STRING_NEXT = re.compile(r'^\s{1,4}string\s+"')
_RE_UINT32 = re.compile(r"^\s{1,4}uint32\s+(\d+)\s*$")
_RE_ARRAY = re.compile(r"^(\s{1,4})array\s*\[(\]?)\s*$")
_RE_ARRAY_OR_INT32 = re.compile(r"^\s{1,4}(?:array\s*\[|int32\s+-?\d+)")
_RE_INT32_TOP = re.compile(r"^\s{1,4}int32\s+-?\d+\s*$")
_RE_URGENCY_VAL = re.compile(r"\b(?:byte|uint32|int32|uint16|int16)\s+(0x[0-9a-fA-F]+|\d+)\b")
_RE_UNESCAPE = re.compile(r'\\(["\\])')


def _safe_int(val: object, default: int = 0) -> int:
    try:
        if _HAS_GIO and isinstance(val, GLib.Variant):
            val = val.unpack()
        return int(val)  # type: ignore[arg-type]
    except Exception:
        return default


def _clamp_urgency(val: int) -> int:
    return max(0, min(2, val))


def _extract_urgency_from_hints(hints: dict) -> int:
    if not isinstance(hints, dict) or "urgency" not in hints:
        return 1
    return _clamp_urgency(_safe_int(hints["urgency"], 1))


def _candidate_omarchy_sockets() -> list[str]:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    candidates: list[str] = []
    omarchy_path = os.environ.get("OMARCHY_PATH")
    wayland_display = os.environ.get("WAYLAND_DISPLAY")
    if omarchy_path and wayland_display:
        # Real Omarchy IPC socket hash (without trailing newline)
        digest_exact = hashlib.md5(f"{omarchy_path}/shell\n{wayland_display}".encode("utf-8")).hexdigest()[:16]
        candidates.append(os.path.join(runtime_dir, f"omarchy-shell-{digest_exact}.sock"))
        # Alternate hash if caller/test includes trailing newline
        digest_nl = hashlib.md5(f"{omarchy_path}/shell\n{wayland_display}\n".encode("utf-8")).hexdigest()[:16]
        candidates.append(os.path.join(runtime_dir, f"omarchy-shell-{digest_nl}.sock"))

    for path in sorted(glob.glob(os.path.join(runtime_dir, "omarchy-shell-*.sock"))):
        if path not in candidates:
            candidates.append(path)
    return [p for p in candidates if os.path.exists(p)]


def _query_omarchy_socket(sock_path: str, timeout: float = 0.3) -> bool | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(sock_path)
        sock.sendall(b"notifications\x1fisDnd\x1e")
        chunks: list[bytes] = []
        total = 0
        while total < 4096:
            chunk = sock.recv(1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"\x1e" in chunk:
                break
        raw = b"".join(chunks).decode("utf-8", errors="replace")
        if "\x1e" not in raw:
            return None
        record = raw.split("\x1e", 1)[0]
        if not record.startswith("OK\x1f"):
            return None
        val = record.split("\x1f", 1)[1].strip().lower()
        if val in ("on", "true", "1"):
            return True
        if val in ("off", "false", "0"):
            return False
        return None
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _run_quick_cmd(cmd: list[str], timeout: float = 0.8) -> tuple[int, str] | None:
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return res.returncode, (res.stdout or "").strip()
    except Exception:
        return None


def is_system_dnd_active() -> bool:
    """
    Detect whether system-wide Do Not Disturb (DND) is active.
    Checks Omarchy Shell UNIX socket IPC first, then CLI fallbacks (omarchy-shell,
    makoctl, swaync-client, dunstctl, and gsettings if none of the above exist).
    Never raises; safely fails open to False.
    """
    try:
        for sock_path in _candidate_omarchy_sockets():
            res = _query_omarchy_socket(sock_path, timeout=0.3)
            if res is not None:
                return res

        omarchy_bin = shutil.which("omarchy-shell")
        if not omarchy_bin and os.path.exists("/usr/share/omarchy/bin/omarchy-shell"):
            omarchy_bin = "/usr/share/omarchy/bin/omarchy-shell"
        if omarchy_bin:
            cmd_res = _run_quick_cmd([omarchy_bin, "notifications", "isDnd"], timeout=0.8)
            if cmd_res is not None and cmd_res[0] == 0:
                val = cmd_res[1].strip().lower()
                if val in ("on", "true", "1"):
                    return True
                if val in ("off", "false", "0"):
                    return False

        mako_bin = shutil.which("makoctl")
        if mako_bin:
            cmd_res = _run_quick_cmd([mako_bin, "mode"], timeout=0.8)
            if cmd_res is not None and cmd_res[0] == 0:
                modes = {ln.strip().lower() for ln in cmd_res[1].splitlines() if ln.strip()}
                return bool(modes & {"do-not-disturb", "dnd"})

        swaync_bin = shutil.which("swaync-client")
        if swaync_bin:
            cmd_res = _run_quick_cmd([swaync_bin, "-D"], timeout=0.8)
            if cmd_res is not None and cmd_res[0] == 0:
                val = cmd_res[1].strip().lower()
                if val in ("true", "1", "on"):
                    return True
                if val in ("false", "0", "off"):
                    return False

        dunst_bin = shutil.which("dunstctl")
        if dunst_bin:
            cmd_res = _run_quick_cmd([dunst_bin, "is-paused"], timeout=0.8)
            if cmd_res is not None and cmd_res[0] == 0:
                val = cmd_res[1].strip().lower()
                if val in ("true", "1", "on"):
                    return True
                if val in ("false", "0", "off"):
                    return False

        if not (omarchy_bin or mako_bin or swaync_bin or dunst_bin):
            gsettings_bin = shutil.which("gsettings")
            if gsettings_bin:
                cmd_res = _run_quick_cmd(
                    [gsettings_bin, "get", "org.gnome.desktop.notifications", "show-banners"],
                    timeout=0.8,
                )
                if cmd_res is not None and cmd_res[0] == 0:
                    val = cmd_res[1].strip().lower()
                    if val == "false":
                        return True
                    if val == "true":
                        return False
    except Exception:
        return False

    return False


def clean_notification_text(raw: str) -> str:
    """Strip HTML/Pango tags, decode HTML entities, and normalize whitespace/newlines."""
    if not raw:
        return ""
    text = str(raw)
    text = _BR_TAG_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _short_app_badge(app_name: str, fallback: str = "AVISO!") -> str:
    raw = (app_name or "").strip()
    if not raw or raw.lower() in _GENERIC_APPS:
        return fallback

    # Trim reverse-DNS identifiers like org.telegram.desktop -> telegram
    if "." in raw and " " not in raw:
        parts = [p for p in raw.split(".") if p and p.lower() not in {"org", "com", "net", "io", "desktop", "app"}]
        if parts:
            raw = parts[-1]

    raw = re.sub(r"\s+desktop$", "", raw, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"[^A-Za-z0-9!+\-]+", " ", raw).strip().upper()
    if not cleaned:
        return fallback
    compact = cleaned.split()[0] if len(cleaned) > 9 else cleaned
    return compact[:9]


def classify_desktop_notification(
    app_name: str,
    summary: str,
    body: str,
    urgency: int = 1,
    default_pokemon: str = "pikachu",
) -> tuple[str, str]:
    """
    Map a desktop notification to one of the 7 real Poké-Hub sprites and a short banner badge.
    Returns (pokemon, badge).
    """
    fallback_poke = default_pokemon if default_pokemon in VALID_POKEMON else "pikachu"
    app_clean = (app_name or "").strip()
    app_low = app_clean.lower()
    app_tokens = set(re.findall(r"[a-z0-9]+", app_low))

    if urgency >= 2:
        if app_low in _GENERIC_APPS or any(k in app_low for k in _CRITICAL_APP_KEYWORDS):
            return "charmander", "ALERTA"
        badge = _short_app_badge(app_clean, "ALERTA")
        if len(app_clean) > 9 and badge != app_clean.upper():
            badge = "ALERTA"
        return "charmander", badge

    # 1. Explicit app_name classification (avoids message-body false positives)
    if ("hey" in app_tokens) or any(k in app_low for k in _MAIL_KEYWORDS):
        return "pikachu", "MAIL"

    for key, badge in _MESSAGING_BADGES:
        if key in app_low:
            return "jigglypuff", badge

    if any(k in app_low for k in _CALENDAR_KEYWORDS):
        return "snorlax", "CITA"

    if any(k in app_low for k in _CRITICAL_APP_KEYWORDS):
        return "charmander", "ALERTA"

    for key, badge in _DEV_BADGES:
        if key in app_low:
            return "gengar", badge
    if "code" in app_tokens:
        return "gengar", "CODE"
    if "foot" in app_tokens:
        return "gengar", "FOOT"

    # 2. Content/text classification for browsers, notify-send, or generic callers
    text_blob = f"{summary or ''} {body or ''}"
    if _TEXT_ALERT_RE.search(text_blob):
        return "charmander", "ALERTA"

    if _TEXT_CALENDAR_RE.search(text_blob):
        return "snorlax", "CITA"

    if _TEXT_MAIL_RE.search(text_blob):
        return "pikachu", "MAIL"

    m_msg = _TEXT_MSG_RE.search(text_blob)
    if m_msg:
        matched = m_msg.group(1).lower()
        for key, badge in _MESSAGING_BADGES:
            if key == matched:
                return "jigglypuff", badge
        return "jigglypuff", "CHAT"

    m_dev = _TEXT_DEV_RE.search(text_blob)
    if m_dev:
        matched = m_dev.group(1).lower()
        if matched.startswith("claude"):
            return "gengar", "CLAUDE"
        for key, badge in _DEV_BADGES:
            if key == matched:
                return "gengar", badge
        return "gengar", matched.upper()[:9]

    return fallback_poke, _short_app_badge(app_clean, "AVISO!")


def _has_odd_trailing_backslashes(text: str) -> bool:
    count = 0
    for ch in reversed(text):
        if ch == "\\":
            count += 1
        else:
            break
    return (count % 2) == 1


def _read_dbus_string(
    lines: list[str],
    idx: int,
    next_line_re: re.Pattern[str] | None,
) -> tuple[str, int] | None:
    if idx >= len(lines):
        return None
    m = _RE_STRING_START.match(lines[idx])
    if not m:
        return None

    parts = [m.group(1)]
    j = idx + 1

    while True:
        tail = parts[-1]
        if tail.endswith('"'):
            at_eof = j >= len(lines)
            next_matches = (not at_eof) and (next_line_re is None or bool(next_line_re.match(lines[j])))
            # Handle single-line `""` or line closing right before the expected next D-Bus arg
            if next_matches or (at_eof and not _has_odd_trailing_backslashes(tail[:-1])):
                break
        if j >= len(lines):
            # Tolerate synthetic blocks without trailing arrays after body
            if tail.endswith('"'):
                break
            return None
        parts.append(lines[j])
        j += 1

    parts[-1] = parts[-1][:-1]
    raw_val = "\n".join(parts)
    unescaped = _RE_UNESCAPE.sub(r"\1", raw_val)
    return unescaped, j


def _skip_dbus_array(lines: list[str], idx: int, parse_urgency: bool = False) -> tuple[int, int]:
    """Skip a top-level D-Bus array block and optionally extract urgency from hints."""
    urgency = 1
    if idx >= len(lines):
        return idx, urgency
    m = _RE_ARRAY.match(lines[idx])
    if not m:
        return idx, urgency
    indent = m.group(1)
    inline_close = m.group(2)
    j = idx + 1
    if inline_close == "]":
        return j, urgency

    closing_line = f"{indent}]"
    in_urgency_entry = False
    while j < len(lines):
        line = lines[j]
        if line.rstrip() == closing_line:
            j += 1
            break
        if parse_urgency:
            stripped = line.strip()
            if stripped == 'string "urgency"':
                in_urgency_entry = True
            elif in_urgency_entry:
                mv = _RE_URGENCY_VAL.search(stripped)
                if mv:
                    urgency = _clamp_urgency(int(mv.group(1), 0))
                    in_urgency_entry = False
                elif stripped.startswith(")") or stripped.startswith("dict entry("):
                    in_urgency_entry = False
        j += 1

    return j, urgency


def parse_dbus_monitor_block(lines: list[str]) -> dict | None:
    """
    Pure parser for a `dbus-monitor` `org.freedesktop.Notifications.Notify` block.
    Returns {"app_name", "replaces_id", "app_icon", "summary", "body", "urgency"} or None.
    """
    if not lines:
        return None
    raw_lines: list[str] = []
    for ln in lines:
        raw_lines.extend(str(ln).rstrip("\r\n").split("\n"))

    idx = 0
    while idx < len(raw_lines) and not raw_lines[idx].strip():
        idx += 1
    if idx >= len(raw_lines):
        return None

    first = raw_lines[idx]
    if _DBUS_HEADER_RE.match(first) or first.startswith(("method call ", "signal ", "method return ", "error ")):
        if not first.startswith("method call "):
            return None
        if "org.freedesktop.Notifications" not in first or "member=Notify" not in first:
            return None
        idx += 1

    while idx < len(raw_lines) and not raw_lines[idx].strip():
        idx += 1

    res = _read_dbus_string(raw_lines, idx, _RE_UINT32)
    if res is None:
        return None
    app_name, idx = res

    if idx >= len(raw_lines):
        return None
    m_rep = _RE_UINT32.match(raw_lines[idx])
    if not m_rep:
        return None
    replaces_id = int(m_rep.group(1))
    idx += 1

    res = _read_dbus_string(raw_lines, idx, _RE_STRING_NEXT)
    if res is None:
        return None
    app_icon, idx = res

    res = _read_dbus_string(raw_lines, idx, _RE_STRING_NEXT)
    if res is None:
        return None
    summary, idx = res

    res = _read_dbus_string(raw_lines, idx, _RE_ARRAY_OR_INT32)
    if res is None:
        return None
    body, idx = res

    idx, _ = _skip_dbus_array(raw_lines, idx, parse_urgency=False)
    idx, urgency = _skip_dbus_array(raw_lines, idx, parse_urgency=True)

    return {
        "app_name": app_name,
        "replaces_id": replaces_id,
        "app_icon": app_icon,
        "summary": summary,
        "body": body,
        "urgency": urgency,
    }


class DesktopNotifyWatcher:
    """
    Passive listener for desktop `org.freedesktop.Notifications.Notify` calls.
    Uses a dedicated Gio D-Bus monitor connection (`BecomeMonitor`) when available,
    falling back to `dbus-monitor --session` without claiming the bus name.
    """

    def __init__(
        self,
        on_notification: Callable[[dict], None],
        default_pokemon: str = "pikachu",
        dedup_window_s: float = 5.0,
    ) -> None:
        self._on_notification = on_notification
        self._default_pokemon = default_pokemon if default_pokemon in VALID_POKEMON else "pikachu"
        self._dedup_window_s = max(0.0, float(dedup_window_s))

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[str] | None = None

        self._gio_conn = None
        self._gio_loop = None
        self._gio_filter_id: int = 0
        self._gio_closed_sig: int = 0

        self._running = False
        self._backend = "none"
        self._received_count = 0
        self._dropped_dnd_count = 0

        self._recent_events: dict[tuple[str, str, str], float] = {}
        self._last_by_replaces_id: dict[int, tuple[str, str, str]] = {}

    def start(self) -> None:
        with self._lock:
            if self._running and self._thread and self._thread.is_alive():
                return
            self._stop.clear()

            if self._start_gio_backend_locked():
                self._backend = "gio"
                self._running = True
                return

            self._backend = "dbus-monitor"
            self._running = True
            self._thread = threading.Thread(
                target=self._monitor_loop,
                name="desktop-notify-dbus",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._running = False
            proc = self._proc
            self._proc = None
            gio_conn = self._gio_conn
            gio_loop = self._gio_loop
            filter_id = self._gio_filter_id
            closed_sig = self._gio_closed_sig
            self._gio_conn = None
            self._gio_loop = None
            self._gio_filter_id = 0
            self._gio_closed_sig = 0
            thread = self._thread
            self._thread = None

        if gio_conn is not None:
            try:
                if closed_sig:
                    gio_conn.disconnect(closed_sig)
            except Exception:
                pass
            try:
                if filter_id and not gio_conn.is_closed():
                    gio_conn.remove_filter(filter_id)
            except Exception:
                pass
            try:
                if not gio_conn.is_closed():
                    gio_conn.close_sync(None)
            except Exception:
                pass

        if gio_loop is not None:
            try:
                if gio_loop.is_running():
                    gio_loop.quit()
            except Exception:
                pass

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def get_status(self) -> dict:
        with self._lock:
            running = self._running
            backend = self._backend
            received_count = self._received_count
            dropped_dnd_count = self._dropped_dnd_count
        return {
            "running": running,
            "backend": backend,
            "dnd_active": is_system_dnd_active(),
            "received_count": received_count,
            "dropped_dnd_count": dropped_dnd_count,
        }

    def _start_gio_backend_locked(self) -> bool:
        if not _HAS_GIO:
            return False

        conn = None
        filter_id = 0
        closed_sig = 0
        try:
            addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            if not addr:
                return False
            conn = Gio.DBusConnection.new_for_address_sync(
                addr,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
            conn.set_exit_on_close(False)
            filter_id = conn.add_filter(self._on_dbus_message, None)
            closed_sig = conn.connect("closed", self._on_gio_closed)
            conn.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus.Monitoring",
                "BecomeMonitor",
                GLib.Variant(
                    "(asu)",
                    (["type='method_call',interface='org.freedesktop.Notifications',member='Notify'"], 0),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            loop = GLib.MainLoop()
            self._gio_conn = conn
            self._gio_filter_id = filter_id
            self._gio_closed_sig = closed_sig
            self._gio_loop = loop
            self._thread = threading.Thread(
                target=self._gio_loop_runner,
                args=(loop,),
                name="desktop-notify-dbus",
                daemon=True,
            )
            self._thread.start()
            return True
        except Exception as exc:
            logger.debug("Gio BecomeMonitor unavailable, falling back to dbus-monitor: %s", exc)
            if conn is not None:
                try:
                    if closed_sig:
                        conn.disconnect(closed_sig)
                except Exception:
                    pass
                try:
                    if filter_id and not conn.is_closed():
                        conn.remove_filter(filter_id)
                except Exception:
                    pass
                try:
                    if not conn.is_closed():
                        conn.close_sync(None)
                except Exception:
                    pass
            return False

    def _on_gio_closed(self, _conn, _remote_peer_vanished, _error) -> None:
        with self._lock:
            loop = self._gio_loop
        if loop is not None:
            try:
                if loop.is_running():
                    loop.quit()
            except Exception:
                pass

    def _gio_loop_runner(self, loop) -> None:
        try:
            loop.run()
        except Exception:
            logger.debug("GLib MainLoop exited with error", exc_info=True)
        if not self._stop.is_set():
            with self._lock:
                self._backend = "dbus-monitor"
            self._monitor_loop()

    def _on_dbus_message(self, _conn, message, incoming: bool, _user_data=None):
        is_incoming_call = False
        try:
            if incoming and message is not None:
                msg_type = message.get_message_type()
                is_incoming_call = msg_type == Gio.DBusMessageType.METHOD_CALL
                if (
                    is_incoming_call
                    and not self._stop.is_set()
                    and message.get_interface() == "org.freedesktop.Notifications"
                    and message.get_member() == "Notify"
                ):
                    body = message.get_body()
                    if body is not None:
                        args = body.unpack()
                        if isinstance(args, tuple) and len(args) >= 7:
                            app_name = str(args[0] or "")
                            replaces_id = _safe_int(args[1], 0)
                            summary = str(args[3] or "")
                            body_text = str(args[4] or "")
                            hints = args[6] if isinstance(args[6], dict) else {}
                            urgency = _extract_urgency_from_hints(hints)
                            self._handle_raw_notify(
                                app_name=app_name,
                                replaces_id=replaces_id,
                                summary=summary,
                                body=body_text,
                                urgency=urgency,
                            )
        except Exception:
            logger.debug("Error processing Gio D-Bus Notify message", exc_info=True)
        # Consume monitored METHOD_CALL messages so GDBus does not send an
        # UnknownObject error reply back on the read-only BecomeMonitor socket.
        if is_incoming_call:
            return None
        return message

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            proc: subprocess.Popen[str] | None = None
            try:
                proc = subprocess.Popen(
                    [
                        "dbus-monitor",
                        "--session",
                        "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                with self._lock:
                    if self._stop.is_set():
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        break
                    self._proc = proc

                assert proc.stdout is not None
                buf: list[str] = []
                in_notify = False
                arrays_closed = 0
                in_top_array = False
                top_array_indent = ""

                while not self._stop.is_set():
                    raw_line = proc.stdout.readline()
                    if not raw_line:
                        break
                    line = raw_line.rstrip("\r\n")

                    if _DBUS_HEADER_RE.match(line):
                        if in_notify and buf:
                            parsed = parse_dbus_monitor_block(buf)
                            if parsed is not None:
                                self._dispatch_parsed(parsed)
                        buf = []
                        in_notify = False
                        arrays_closed = 0
                        in_top_array = False
                        top_array_indent = ""

                        if (
                            line.startswith("method call ")
                            and "org.freedesktop.Notifications" in line
                            and "member=Notify" in line
                        ):
                            in_notify = True
                            buf = [line]
                        continue

                    if not in_notify:
                        continue

                    buf.append(line)
                    if not in_top_array:
                        m_arr = _RE_ARRAY.match(line)
                        if m_arr:
                            if m_arr.group(2) == "]":
                                arrays_closed += 1
                            else:
                                in_top_array = True
                                top_array_indent = m_arr.group(1)
                    elif line.rstrip() == f"{top_array_indent}]":
                        in_top_array = False
                        top_array_indent = ""
                        arrays_closed += 1

                    if arrays_closed >= 2 and _RE_INT32_TOP.match(line):
                        parsed = parse_dbus_monitor_block(buf)
                        if parsed is not None:
                            self._dispatch_parsed(parsed)
                            buf = []
                            in_notify = False
                            arrays_closed = 0
                            in_top_array = False
                            top_array_indent = ""

                if in_notify and buf and not self._stop.is_set():
                    parsed = parse_dbus_monitor_block(buf)
                    if parsed is not None:
                        self._dispatch_parsed(parsed)
            except Exception as exc:
                logger.debug("dbus-monitor loop error: %s", exc)
            finally:
                with self._lock:
                    if self._proc is proc:
                        self._proc = None
                if proc is not None:
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                        proc.wait(timeout=1.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                if not self._stop.is_set():
                    self._stop.wait(2.0)

    def _dispatch_parsed(self, parsed: dict) -> None:
        self._handle_raw_notify(
            app_name=str(parsed.get("app_name") or ""),
            replaces_id=_safe_int(parsed.get("replaces_id"), 0),
            summary=str(parsed.get("summary") or ""),
            body=str(parsed.get("body") or ""),
            urgency=_clamp_urgency(_safe_int(parsed.get("urgency"), 1)),
        )

    def _handle_raw_notify(
        self,
        *,
        app_name: str,
        replaces_id: int,
        summary: str,
        body: str,
        urgency: int,
    ) -> None:
        if self._stop.is_set():
            return

        app_clean = app_name.strip()
        if app_clean.lower() in IGNORED_APPS:
            return

        clean_summary = clean_notification_text(summary)
        clean_message = clean_notification_text(body)
        if not clean_summary and not clean_message:
            return

        if is_system_dnd_active():
            with self._lock:
                self._dropped_dnd_count += 1
            return

        title = clean_summary or app_clean or "AVISO"
        urg = _clamp_urgency(urgency)
        rep_id = max(0, int(replaces_id))
        dedup_key = (app_clean.lower(), title, clean_message)
        now_mono = time.monotonic()

        with self._lock:
            cutoff = now_mono - self._dedup_window_s
            stale = [k for k, ts_m in self._recent_events.items() if ts_m <= cutoff]
            for k in stale:
                del self._recent_events[k]

            allow_replaced_update = False
            if rep_id > 0:
                prev_for_id = self._last_by_replaces_id.get(rep_id)
                if prev_for_id is not None and prev_for_id != dedup_key:
                    allow_replaced_update = True

            last_seen = self._recent_events.get(dedup_key)
            if not allow_replaced_update and last_seen is not None and (now_mono - last_seen) < self._dedup_window_s:
                return

            self._recent_events[dedup_key] = now_mono
            if rep_id > 0:
                self._last_by_replaces_id[rep_id] = dedup_key
                if len(self._last_by_replaces_id) > 256:
                    oldest_key = next(iter(self._last_by_replaces_id))
                    del self._last_by_replaces_id[oldest_key]

            default_poke = self._default_pokemon
            self._received_count += 1

        pokemon, badge = classify_desktop_notification(
            app_name=app_clean,
            summary=clean_summary,
            body=clean_message,
            urgency=urg,
            default_pokemon=default_poke,
        )
        if pokemon not in VALID_POKEMON:
            pokemon = "pikachu"

        event = {
            "app_name": app_clean,
            "badge": badge,
            "title": title,
            "message": clean_message,
            "pokemon": pokemon,
            "urgency": urg,
            "replaces_id": rep_id,
            "source": "desktop",
            "ts": time.time(),
        }
        try:
            self._on_notification(event)
        except Exception:
            logger.debug("on_notification callback failed", exc_info=True)
