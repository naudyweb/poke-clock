from __future__ import annotations

import sys
import unittest
from unittest import mock

import poke_ctl


class TestPokeCtl(unittest.TestCase):
    def test_display_command_turns_screen_on(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["poke-ctl", "display", "on"]),
            mock.patch.object(poke_ctl, "call_api", return_value={"ok": True}) as call_api,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(poke_ctl.main(), 0)

        call_api.assert_called_once_with("POST", "/api/display", {"on": True})

    def test_display_command_turns_screen_off(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["poke-ctl", "display", "off"]),
            mock.patch.object(poke_ctl, "call_api", return_value={"ok": True}) as call_api,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(poke_ctl.main(), 0)

        call_api.assert_called_once_with("POST", "/api/display", {"on": False})


if __name__ == "__main__":
    unittest.main()