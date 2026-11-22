"""Abstract display driver interface for JUZIPi SD PRO."""

from __future__ import annotations

from abc import ABC, abstractmethod


class DisplayDriver(ABC):
    name: str = "base"

    @abstractmethod
    def push_frame(self, card_id: str, jpg_bytes: bytes, content_hash: str) -> dict:
        """Display the given 240x240 JPEG frame immediately on the screen."""

    @abstractmethod
    def set_brightness(self, level: int) -> bool:
        """Set backlight brightness (0..100)."""

    def set_rotation(self, rotation: int) -> bool:
        """Set screen rotation (0..3) if supported."""
        return False

    @abstractmethod
    def get_status(self) -> dict:
        """Query live hardware status."""

    @abstractmethod
    def ping(self) -> bool:
        """Return True if display is reachable."""
