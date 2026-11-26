"""Abstract display driver interface for JUZIPi SD PRO."""

from __future__ import annotations

from abc import ABC, abstractmethod


class DisplayDriver(ABC):
    name: str = "base"

    @abstractmethod
    def push_frame(self, card_id: str, jpg_bytes: bytes, content_hash: str, force: bool = False) -> dict:
        """Display the given 240x240 JPEG frame immediately on the screen."""

    @abstractmethod
    def set_brightness(self, level: int) -> bool:
        """Set backlight brightness (0..100)."""

    def set_rotation(self, rotation: int) -> bool:
        """Set screen rotation (0..3) if supported."""
        return False

    @property
    def supports_display_power(self) -> bool:
        return False

    def check_display_power_support(self) -> bool:
        return self.supports_display_power

    def set_display_power(self, on: bool) -> bool:
        """Turn the physical display on or off without disconnecting the device."""
        return False

    @abstractmethod
    def get_status(self) -> dict:
        """Query live hardware status."""

    @abstractmethod
    def ping(self) -> bool:
        """Return True if display is reachable."""
