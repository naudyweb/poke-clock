"""
Local mDNS (.local) domain publisher for Poké-Hub using Avahi.

Publishes `pokeclock.local` (configurable via POKE_HUB_DOMAIN) on the LAN
without requiring root privileges, and automatically updates the published
IPv4 address if the host changes Wi-Fi networks or gets a new DHCP lease.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading


def detect_lan_ipv4(target_host: str | None = None) -> str:
    """Return the active LAN IPv4 address used to reach the local network."""
    probes: list[tuple[str, int]] = []
    if target_host and target_host not in ("127.0.0.1", "localhost"):
        probes.append((target_host, 80))
    probes.extend([("192.168.18.1", 80), ("1.1.1.1", 80)])

    for host, port in probes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((host, port))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    return ""


def is_local_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    """Check whether a local TCP port (e.g. port 80 proxy) is accepting connections."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


class MdnsPublisher:
    def __init__(
        self,
        domain: str = "pokeclock.local",
        port: int = 8765,
        target_host: str | None = None,
        check_interval: float = 15.0,
    ) -> None:
        clean = (domain or "pokeclock.local").strip().rstrip(".")
        if "." not in clean:
            clean = f"{clean}.local"
        self.domain = clean
        self.port = int(port)
        self.target_host = target_host
        self.check_interval = max(5.0, float(check_interval))

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._addr_proc: subprocess.Popen[bytes] | None = None
        self._svc_proc: subprocess.Popen[bytes] | None = None
        self._published_ip: str = ""
        self._published_http_port: int = self.port
        self._avahi_bin: str | None = shutil.which("avahi-publish")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._sync_once()
        self._thread = threading.Thread(target=self._watch_loop, name="poke-mdns", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._terminate_procs_locked()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _terminate_procs_locked(self) -> None:
        for attr in ("_svc_proc", "_addr_proc"):
            proc = getattr(self, attr)
            setattr(self, attr, None)
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _detect_http_port(self) -> int:
        if self.port == 80 or is_local_port_open(80):
            return 80
        return self.port

    def _sync_once(self) -> None:
        ip = detect_lan_ipv4(self.target_host)
        http_port = self._detect_http_port()

        with self._lock:
            addr_dead = self._addr_proc is None or self._addr_proc.poll() is not None
            svc_dead = self._svc_proc is None or self._svc_proc.poll() is not None
            changed = (ip != self._published_ip) or (http_port != self._published_http_port)

            if not self._avahi_bin or not ip:
                if not ip:
                    self._terminate_procs_locked()
                    self._published_ip = ""
                self._published_http_port = http_port
                return

            if not changed and not addr_dead and not svc_dead:
                return

            self._terminate_procs_locked()
            self._published_ip = ip
            self._published_http_port = http_port

            try:
                self._addr_proc = subprocess.Popen(
                    [self._avahi_bin, "-f", "-a", "-R", self.domain, ip],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._svc_proc = subprocess.Popen(
                    [
                        self._avahi_bin,
                        "-f",
                        "-s",
                        "-H",
                        self.domain,
                        "PokeClock",
                        "_http._tcp",
                        str(http_port),
                        "path=/",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                url = self._format_url(self.domain, http_port)
                print(
                    f"[poke-mdns] Publishing {self.domain} -> {ip} ({url})",
                    flush=True,
                )
            except OSError as exc:
                print(f"[poke-mdns] Failed to start avahi-publish: {exc}", flush=True)
                self._terminate_procs_locked()

    def _watch_loop(self) -> None:
        while not self._stop_event.wait(self.check_interval):
            try:
                self._sync_once()
            except Exception:
                pass

    @staticmethod
    def _format_url(host: str, port: int) -> str:
        return f"http://{host}" if port == 80 else f"http://{host}:{port}"

    def get_info(self) -> dict:
        with self._lock:
            ip = self._published_ip or detect_lan_ipv4(self.target_host)
            http_port = self._published_http_port
            active = bool(self._addr_proc is not None and self._addr_proc.poll() is None)
        return {
            "domain": self.domain,
            "ip": ip,
            "port": self.port,
            "http_port": http_port,
            "url": self._format_url(self.domain, http_port),
            "lan_url": self._format_url(ip, http_port) if ip else "",
            "mdns_active": active,
        }
