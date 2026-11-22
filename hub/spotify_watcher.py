"""Event-driven MPRIS Spotify watcher using native dbus-monitor + busctl."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Callable


def _busctl_json(args: list[str]) -> dict | list | None:
    try:
        res = subprocess.run(
            ["busctl", "--user", "--json=short", *args],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        return json.loads(res.stdout)
    except Exception:
        return None


def query_spotify_playing() -> dict | None:
    """
    Query MPRIS via busctl for active Spotify playback (native Spotify desktop app
    or Spotify Web in Brave/Chromium/Firefox).
    Returns {"title", "artist", "album"} ONLY when PlaybackStatus == "Playing".
    """
    names_data = _busctl_json(["list"])
    if not isinstance(names_data, list):
        return None

    mpris_players = [
        entry["name"]
        for entry in names_data
        if isinstance(entry, dict) and str(entry.get("name", "")).startswith("org.mpris.MediaPlayer2.")
    ]
    mpris_players.sort(key=lambda n: (0 if "spotify" in n.lower() else 1, n))

    for dest in mpris_players:
        status_obj = _busctl_json([
            "get-property", dest, "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player", "PlaybackStatus",
        ])
        if not isinstance(status_obj, dict) or status_obj.get("data") != "Playing":
            continue

        meta_obj = _busctl_json([
            "get-property", dest, "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player", "Metadata",
        ])
        meta = meta_obj.get("data") if isinstance(meta_obj, dict) else None
        if not isinstance(meta, dict):
            continue

        def get_str(k: str) -> str:
            v = meta.get(k)
            if isinstance(v, dict):
                d = v.get("data")
                if isinstance(d, list):
                    return ", ".join(str(x) for x in d if x)
                return str(d or "")
            return ""

        is_native = "spotify" in dest.lower()
        url = get_str("xesam:url").lower()
        art = get_str("mpris:artUrl").lower()
        trackid = get_str("mpris:trackid").lower()
        is_web = ("spotify.com" in url) or ("scdn.co" in art) or ("spotify" in trackid)
        if not (is_native or is_web):
            continue

        title = get_str("xesam:title").strip()
        artist = get_str("xesam:artist").strip()
        album = get_str("xesam:album").strip()
        if not title and not artist:
            continue
        return {"title": title, "artist": artist, "album": album}

    return None


class SpotifyWatcher:
    """
    Listens to session D-Bus MPRIS PropertiesChanged events via `dbus-monitor`
    for instant (<50ms) reaction to Play/Pause/Next/Prev, plus a 20s safety sweep.
    """

    def __init__(self, on_change: Callable[[dict | None], None]) -> None:
        self._on_change = on_change
        self._stop = threading.Event()
        self._last_track: dict | None = None
        self._has_emitted = False
        self._proc: subprocess.Popen[str] | None = None
        self._proc_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._check_lock = threading.Lock()

    def start(self) -> None:
        self._stop.clear()
        self._check_now()
        t_mon = threading.Thread(target=self._monitor_loop, name="spotify-dbus", daemon=True)
        t_poll = threading.Thread(target=self._fallback_loop, name="spotify-fallback", daemon=True)
        self._threads = [t_mon, t_poll]
        t_mon.start()
        t_poll.start()

    def stop(self) -> None:
        self._stop.set()
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=0.5)
                except Exception:
                    pass

    def _check_now(self) -> None:
        with self._check_lock:
            track = query_spotify_playing()
            if not self._has_emitted or track != self._last_track:
                self._last_track = track
                self._has_emitted = True
                try:
                    self._on_change(track)
                except Exception:
                    pass

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            proc: subprocess.Popen[str] | None = None
            try:
                proc = subprocess.Popen(
                    [
                        "dbus-monitor",
                        "--session",
                        "type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',path='/org/mpris/MediaPlayer2'",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                with self._proc_lock:
                    if self._stop.is_set():
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        break
                    self._proc = proc

                assert proc.stdout is not None
                last_trigger = 0.0
                for line in proc.stdout:
                    if self._stop.is_set():
                        break
                    if "org.mpris.MediaPlayer2.Player" in line or "PlaybackStatus" in line or "Metadata" in line:
                        now = time.monotonic()
                        if now - last_trigger >= 0.15:
                            last_trigger = now
                            self._check_now()
            except Exception:
                pass
            finally:
                with self._proc_lock:
                    if self._proc is proc:
                        self._proc = None
                if proc is not None:
                    if proc.stdout is not None:
                        try:
                            proc.stdout.close()
                        except Exception:
                            pass
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                        proc.wait(timeout=1.0)
                    except Exception:
                        try:
                            proc.kill()
                            proc.wait(timeout=0.5)
                        except Exception:
                            pass
                if not self._stop.is_set():
                    self._stop.wait(2.0)

    def _fallback_loop(self) -> None:
        while not self._stop.wait(20.0):
            self._check_now()
